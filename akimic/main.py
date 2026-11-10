"""AkiMic startup orchestration.

Boot order: single-instance lock → platform detect → settings → firewall
rules → persistent certificate (trusted into the Windows Root store when
elevated) → signaling server (HTTPS) → readiness probe → pywebview window
→ tray. Any startup failure rolls back what was set up (firewall rules)
and reports a visible error instead of dying silently.
"""

import ctypes
import os
import secrets
import sys
import threading

from . import APP_NAME, __version__, certs, net
from .driver import get_platform
from .server import ServerContext, start_server_thread
from .settings import Settings
from .tray import Tray


def _base_path() -> str:
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _alert(title: str, text: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)
    except Exception:
        print(f"[{APP_NAME}] {text}")


def run() -> None:
    base_path = _base_path()

    if not net.acquire_single_instance_lock():
        # surface the existing instance instead of confusing the user with
        # "nothing happened" while the old exe sits in the tray
        if not net.notify_running_instance():
            try:
                ctypes.windll.user32.MessageBoxW(
                    0, f"{APP_NAME} is already running.\nCheck the system tray.",
                    APP_NAME, 0x40)
            except Exception:
                print(f"[{APP_NAME}] another instance is already running.")
        return

    platform = get_platform()
    # self-heal any endpoint rename residue left by an older build
    ok, msg = platform.cleanup_rename_residue()
    if not ok:
        print(f"[{APP_NAME}] rename-residue cleanup failed: {msg}")

    settings = Settings()

    # TCP for the page, UDP for WebRTC ICE — without the UDP rule Windows
    # pops a security alert on every phone connection and cancelling it
    # kills the audio. Best effort; the packaged exe runs elevated.
    fw_ok, fw_msg = net.ensure_firewall_rules()
    if not fw_ok:
        print(f"[{APP_NAME}] firewall rules skipped: {fw_msg}")

    lan_ip = net.get_lan_ip()
    try:
        port = net.find_free_port()
    except RuntimeError as e:
        _alert(APP_NAME, f"{APP_NAME} could not find a free port:\n{e}")
        return

    # Persistent self-signed cert, trusted on this PC once (admin install).
    # Falls back to werkzeug's adhoc cert when generation fails.
    ssl_cert = ssl_key = None
    cert_trusted = False
    pair = certs.ensure_cert(settings.dir(), lan_ip)
    if pair:
        ssl_cert, ssl_key = pair
        cert_trusted = certs.ensure_trusted(ssl_cert)
        if not cert_trusted:
            print(f"[{APP_NAME}] root-store install skipped (need admin)")

    # WebView2 flags: with the cert trusted, do NOT globally ignore TLS
    # errors; the ignore flag remains only as the untrusted fallback.
    env = platform.get_webview_env()
    if cert_trusted:
        env = {k: v.replace("--ignore-certificate-errors ", "").strip()
               for k, v in env.items()}
    for key, value in env.items():
        os.environ[key] = value

    pairing_secret = secrets.token_urlsafe(32)
    ctx = ServerContext(settings=settings, platform=platform, port=port, lan_ip=lan_ip,
                        fw_ok=fw_ok, ssl_cert=ssl_cert, ssl_key=ssl_key,
                        pairing_secret=pairing_secret)
    socketio = start_server_thread(ctx)
    # confirm OUR server (health endpoint) is actually listening — never a
    # blind sleep, and another HTTPS service on the port is detected
    ready, ready_msg = net.wait_server_ready(port, timeout=12)
    if not ready:
        net.remove_firewall_rules()  # roll back this boot's rules
        _alert(APP_NAME, f"{APP_NAME} server failed to start:\n{ready_msg}")
        return

    icon_path = os.path.join(base_path, "assets", "icon.ico")
    png_path = os.path.join(base_path, "assets", "icon.png")

    import webview

    class WindowApi:
        """JS bridge for the frameless window controls."""
        def minimize(self):
            window.minimize()

        def close_app(self):
            window.destroy()

    window = webview.create_window(
        title=f"{APP_NAME}",
        url=f"https://127.0.0.1:{port}/desktop",
        js_api=WindowApi(),
        width=860,
        height=600,
        min_size=(460, 420),
        resizable=True,
        frameless=True,
        easy_drag=False,
        background_color="#0C1220",
    )

    def _exit_app():
        tray.stop()
        net.remove_firewall_rules()  # graceful exit leaves no inbound rules behind
        os._exit(0)

    tray = Tray(
        png_path,
        on_show=lambda: (window.show(), window.restore()),
        on_exit=_exit_app,
        on_mute_toggle=lambda: socketio.emit("toggle_mute", to="mobile"),
    )
    ctx.on_lang_change = tray.apply_language
    ctx.on_mute_change = tray.set_muted

    def _watch_lock_socket():
        """Second launches ping us to bring the window back."""
        sock = net.get_lock_socket()
        if sock is None:
            return
        while True:
            try:
                conn, _ = sock.accept()
                try:
                    data = conn.recv(16)
                finally:
                    conn.close()
                if data.startswith(b"show"):
                    try:
                        window.show()
                        window.restore()
                    except Exception:
                        pass
            except OSError:
                return

    window.events.minimized += lambda: window.hide()
    window.events.closed += _exit_app
    threading.Thread(target=_watch_lock_socket, daemon=True).start()

    tray.apply_language(settings.get("lang", "auto"))
    tray.start()

    webview.start(gui=platform.gui_backend, icon=icon_path if os.path.exists(icon_path) else None)
    _exit_app()
