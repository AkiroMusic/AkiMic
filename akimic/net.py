"""Network helpers: LAN IP discovery, free-port scan and firewall rule (P0)."""

import ipaddress
import socket
import ssl
import subprocess
import sys
import time

PORT_RANGE_START = 5000
PORT_RANGE_END = 5100
FIREWALL_RULE_NAME = "AkiMic"
FIREWALL_RULE_ICE = "AkiMic ICE"
LOCK_PORT = 51999  # single-instance mutex


def _is_usable_lan_ip(ip: str) -> bool:
    """Private IPv4 only, excluding proxy-TUN fake IPs (198.18.0.0/15, used
    by Clash et al.) — advertising one of those in the QR makes the phone
    unable to reach us."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if not isinstance(addr, ipaddress.IPv4Address):
        return False
    if addr.is_loopback or addr.is_link_local or not addr.is_private:
        return False
    return addr not in ipaddress.ip_network("198.18.0.0/15")


def get_lan_ip() -> str:
    """Return this machine's LAN IPv4, robust against TUN adapters.

    The default-route interface is tried first (the one phones on the same
    LAN actually reach), then hostname-resolution candidates; proxy-TUN
    fake IPs (198.18.0.0/15) are excluded throughout.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        if _is_usable_lan_ip(ip):
            return ip
    except OSError:
        pass
    finally:
        s.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if _is_usable_lan_ip(ip):
                return ip
    except OSError:
        pass
    return "127.0.0.1"


def wait_server_ready(port: int, timeout: float = 12.0) -> tuple[bool, str]:
    """Poll until OUR signaling server answers /healthz on loopback.

    A plain TLS handshake is not enough — another HTTPS service could own
    the port — so the probe issues a real GET and verifies the response
    identifies AkiMic. Per-attempt failures (server still starting) are
    retried until the deadline.
    """
    deadline = time.time() + timeout
    verify_off = ssl.create_default_context()
    verify_off.check_hostname = False
    verify_off.verify_mode = ssl.CERT_NONE
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.5) as raw:
                with verify_off.wrap_socket(raw) as ts:
                    ts.settimeout(2)
                    ts.sendall(b"GET /healthz HTTP/1.1\r\n"
                               b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n")
                    data = b""
                    while len(data) < 8192:
                        chunk = ts.recv(1024)
                        if not chunk:
                            break
                        data += chunk
                    if b'"app": "AkiMic"' in data or b'"app":"AkiMic"' in data:
                        return True, "ready"
                    return False, "port is open but it is not AkiMic (another service?)"
        except (ssl.SSLError, OSError):
            pass
        time.sleep(0.3)
    return False, f"server did not become ready within {timeout:.0f}s"


def find_free_port(start: int = PORT_RANGE_START, end: int = PORT_RANGE_END) -> int:
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"no free port in range {start}-{end}")


def acquire_single_instance_lock() -> bool:
    """True if we are the first AkiMic instance; False if one is already running."""
    global _lock_socket
    _lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _lock_socket.bind(("127.0.0.1", LOCK_PORT))
        _lock_socket.listen(1)
        return True
    except OSError:
        _lock_socket.close()
        return False


_lock_socket = None


def get_lock_socket():
    """The listening single-instance socket (None before acquire)."""
    return _lock_socket


def notify_running_instance() -> bool:
    """Ask the already-running AkiMic to show its window. Best effort."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(("127.0.0.1", LOCK_PORT))
            s.sendall(b"show")
            return True
    except OSError:
        return False


def _netsh(args: list[str]) -> tuple[int, str]:
    cmd = ["netsh", "advfirewall", "firewall", *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return proc.returncode, proc.stdout + proc.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def _rule_exists(name: str) -> bool:
    code, out = _netsh(["show", "rule", f"name={name}"])
    return code == 0 and name in out and "No rules match" not in out


def ensure_firewall_rules() -> tuple[bool, str, bool]:
    """Allow phone→PC traffic: TCP 5000-5100 for the signaling server and
    UDP ephemeral ports for WebRTC ICE (WebView2 binds them when the phone
    connects — without this Windows pops the security alert on every
    connection, and cancelling it blocks the audio stream entirely).
    Both rules are scoped to the local subnet. Per-rule idempotent, so a
    partial previous run is completed rather than wiped and rebuilt.

    Returns (ok, message, added_by_us) — added_by_us lets the caller roll
    back only rules this boot created, never pre-existing same-name rules.
    """
    if sys.platform != "win32":
        return True, "firewall management is Windows-only, skipped", False

    rules = (
        (FIREWALL_RULE_NAME, "TCP", f"{PORT_RANGE_START}-{PORT_RANGE_END}", "AkiMic LAN signaling"),
        (FIREWALL_RULE_ICE, "UDP", "49152-65535", "AkiMic WebRTC ICE connectivity checks"),
    )
    ok = True
    parts = []
    added = False
    for name, proto, port, desc in rules:
        if _rule_exists(name):
            parts.append(f"{proto}: present")
            continue
        code, out = _netsh([
            "add", "rule", f"name={name}",
            "dir=in", "action=allow", f"protocol={proto}",
            f"localport={port}",
            "remoteip=localsubnet",
            f"description={desc}",
        ])
        if code == 0:
            added = True
            parts.append(f"{proto}: added")
        else:
            ok = False
            parts.append(f"{proto}: FAILED ({out.strip()[:80]})")
    return ok, "; ".join(parts), added


def remove_firewall_rules() -> tuple[bool, str]:
    """Remove AkiMic's rules; missing rules count as success (idempotent)."""
    if sys.platform != "win32":
        return True, "skipped"
    ok = True
    removed = 0
    for name in (FIREWALL_RULE_NAME, FIREWALL_RULE_ICE):
        if not _rule_exists(name):
            continue
        code, _out = _netsh(["delete", "rule", f"name={name}"])
        if code == 0:
            removed += 1
        else:
            ok = False
    return ok, (f"removed {removed}" if removed else "none present")
