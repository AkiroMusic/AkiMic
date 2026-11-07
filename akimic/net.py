"""Network helpers: LAN IP discovery, free-port scan and firewall rule (P0)."""

import ipaddress
import socket
import subprocess
import sys

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
    """Return this machine's LAN IPv4, robust against TUN adapters."""
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if _is_usable_lan_ip(ip):
                return ip
    except OSError:
        pass
    # fallback: routing-table trick (no packets actually sent)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        return ip if _is_usable_lan_ip(ip) else "127.0.0.1"
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def find_free_port(start: int = PORT_RANGE_START, end: int = PORT_RANGE_END) -> int:
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    return start


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


def ensure_firewall_rules() -> tuple[bool, str]:
    """Allow phone→PC traffic: TCP 5000-5100 for the signaling server and
    UDP ephemeral ports for WebRTC ICE (WebView2 binds them when the phone
    connects — without this Windows pops the security alert every
    connection, and cancelling it blocks the audio stream entirely).
    Both rules are scoped to the local subnet. Recreated each boot so
    definition upgrades apply cleanly; needs elevation.
    """
    if sys.platform != "win32":
        return True, "firewall management is Windows-only, skipped"

    for name in (FIREWALL_RULE_NAME, FIREWALL_RULE_ICE):
        _netsh(["delete", "rule", f"name={name}"])  # ignore "not found"

    code, out = _netsh([
        "add", "rule", f"name={FIREWALL_RULE_NAME}",
        "dir=in", "action=allow", "protocol=TCP",
        f"localport={PORT_RANGE_START}-{PORT_RANGE_END}",
        "remoteip=localsubnet",
        "description=AkiMic LAN signaling",
    ])
    if code != 0:
        return False, out.strip()
    code, out = _netsh([
        "add", "rule", f"name={FIREWALL_RULE_ICE}",
        "dir=in", "action=allow", "protocol=UDP",
        "localport=49152-65535",
        "remoteip=localsubnet",
        "description=AkiMic WebRTC ICE connectivity checks",
    ])
    return code == 0, out.strip() or "rules added"


def remove_firewall_rules() -> tuple[bool, str]:
    if sys.platform != "win32":
        return True, "skipped"
    ok = True
    for name in (FIREWALL_RULE_NAME, FIREWALL_RULE_ICE):
        code, out = _netsh(["delete", "rule", f"name={name}"])
        ok = ok and code == 0
    return ok, out.strip() or "rules removed"
