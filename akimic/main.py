"""AkiMic startup orchestration.

Boot order: single-instance lock → platform detect → settings → firewall
rule → signal server (HTTPS) → pywebview window → tray.
"""

import ctypes
import os
import sys
import threading
import time

from . import APP_NAME, __version__, certs, net
from .driver import get_platform
from .server import ServerContext, start_server_thread
from .settings import Settings
from .tray import Tray


def _base_path() -> str:
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _alert_already_running() -> None:
    try:
        ctypes.windll.user32.MessageBoxW(
            0, f"{APP_NAME} is already running.\nCheck the system tray.",
            APP_NAME, 0x40,
        )
    except Exception:
        print(f"[{APP_NAME}] another instance is already running.")


def run() -> None:
    base_path = _base_path()

    if not net.acquire_single_instance_lock():
        # surface the existing instance instead of confusing the user with
        # "nothing happened" while the old exe sits in the tray
        if not net.notify_running_instance():
            _alert_already_running()
        return

    platform = get_platform()
    # self-heal any endpoint rename residue left by an older build
    ok, msg = platform.cleanup_rename_residue()
    if not ok:
        print(f"[{APP_NAME}] rename-residue cleanup failed: {msg}")
    for key, value in platform.get_webview_env().items():
        os.environ[key] = value

    settings = Settings()

    # P0: let phones on the LAN reach the signaling server. TCP for the
    # page, UDP for WebRTC ICE — without the UDP rule Windows pops a
    # security alert on every phone connection and cancelling it kills
    # the audio. Best effort; the packaged exe is elevated via its manifest.
    fw_ok, fw_msg = net.ensure_firewall_rules()
    if not fw_ok:
        print(f"[{APP_NAME}] firewall rules skipped: {fw_msg}")

    lan_ip = net.get_lan_ip()
    port = net.find_free_port()

    # Persistent self-signed cert, trusted on this PC once (admin install).
    # Falls back to werkzeug's adhoc cert when generation/install fails.
    ssl_cert = ssl_key = None
    pair = certs.ensure_cert(settings.dir(), lan_ip)
    if pair:
        ssl_cert, ssl_key = pair
        if not certs.ensure_trusted(ssl_cert):
            print(f"[{APP_NAME}] root-store install skipped (need admin)")

    ctx = ServerContext(settings=settings, platform=platform, port=port, lan_ip=lan_ip,
                        fw_ok=fw_ok, ssl_cert=ssl_cert, ssl_key=ssl_key)
    socketio = start_server_thread(ctx)
    time.sleep(1.0)  # give HTTPS a moment before the window loads it

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
