"""Flask + SocketIO server: static web UI, REST API and WebRTC signaling.

Signaling model: two roles ("mobile" and "desktop") each join their own
room. SDP offers / answers / ICE candidates are forwarded to the opposite
room only, and peer_left is broadcast when a side drops so the UI can reset.
Origin is allow-listed to this machine's own addresses so third-party
websites cannot inject audio into the virtual microphone; the desktop slot
is single (a newer page replaces an older one) and mobile senders are capped.
"""

import os
import socket
import threading
import time
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit

from . import __version__
from .driver.base import BasePlatform
from .settings import Settings

WEBUI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webui")

MAX_MOBILE_SENDERS = 4


class ServerContext:
    """Shared state handed to routes and callbacks owned by main.py."""

    def __init__(self, settings: Settings, platform: BasePlatform, port: int, lan_ip: str,
                 fw_ok: bool = False, ssl_cert: str | None = None, ssl_key: str | None = None):
        self.settings = settings
        self.platform = platform
        self.port = port
        self.lan_ip = lan_ip
        self.fw_ok = fw_ok
        self.ssl_cert = ssl_cert
        self.ssl_key = ssl_key
        self.on_lang_change = None   # callable(lang_code)
        self.on_mute_change = None   # callable(muted: bool)


def _origin_allowed(origin: str | None, ctx: ServerContext) -> bool:
    """Only pages served from this machine may open the signaling socket."""
    if not origin:
        return True  # non-browser clients
    host = urlsplit(origin).hostname
    if not host:
        return False
    if host in ("localhost", "127.0.0.1") or host == ctx.lan_ip:
        return True
    try:
        return host.lower() == socket.gethostname().lower()
    except OSError:
        return False


def create_server(ctx: ServerContext) -> tuple[Flask, SocketIO]:
    app = Flask(__name__, static_folder=WEBUI_DIR, static_url_path="/static")
    app.config["SECRET_KEY"] = "akimic-local-key"
    socketio = SocketIO(
        app,
        cors_allowed_origins=lambda origin: _origin_allowed(origin, ctx),
        async_mode="threading",
    )

    sid_role: dict[str, str] = {}
    desktop_sid: str | None = None

    # ---- static pages ----------------------------------------------------

    @app.route("/")
    def mobile_page():
        return send_from_directory(os.path.join(WEBUI_DIR, "mobile"), "index.html")

    @app.route("/desktop")
    def desktop_page():
        return send_from_directory(os.path.join(WEBUI_DIR, "desktop"), "index.html")

    @app.route("/lang/<path:filename>")
    def lang_file(filename):
        return send_from_directory(os.path.join(WEBUI_DIR, "lang"), filename)

    @app.route("/assets/<path:filename>")
    def asset_file(filename):
        return send_from_directory(os.path.join(WEBUI_DIR, "assets"), filename)

    @app.route("/vendor/<path:filename>")
    def vendor_file(filename):
        return send_from_directory(os.path.join(WEBUI_DIR, "assets", "vendor"), filename)

    # ---- REST API ---------------------------------------------------------

    @app.route("/api/info")
    def api_info():
        return jsonify({
            "app": "AkiMic",
            "version": __version__,
            "ip": ctx.lan_ip,
            "port": ctx.port,
            "url": f"https://{ctx.lan_ip}:{ctx.port}",
        })

    @app.route("/api/platform")
    def api_platform():
        return jsonify({
            "os": "windows",
            "is_admin": ctx.platform.is_admin(),
            "driver_installed": ctx.platform.is_driver_installed(),
            "driver_display_name": ctx.platform.driver_display_name,
            "driver_match_keyword": ctx.platform.driver_match_keyword,
            "post_install_warning": ctx.platform.get_post_install_warning(),
            "firewall_ok": ctx.fw_ok,
        })

    @app.route("/api/settings", methods=["GET", "POST"])
    def api_settings():
        if request.method == "POST":
            patch = request.get_json(silent=True) or {}
            ctx.settings.update(patch)
        return jsonify(ctx.settings.as_dict())

    @app.route("/api/driver/install", methods=["POST"])
    def api_driver_install():
        ok, msg = ctx.platform.install_driver()
        return jsonify({"success": ok, "msg": msg})

    @app.route("/api/driver/uninstall", methods=["POST"])
    def api_driver_uninstall():
        ok, msg = ctx.platform.uninstall_driver()
        if ok:
            from . import net
            net.remove_firewall_rules()
        return jsonify({"success": ok, "msg": msg})

    @app.route("/api/lang", methods=["POST"])
    def api_lang():
        lang = (request.get_json(silent=True) or {}).get("lang", "en_us")
        if ctx.on_lang_change:
            ctx.on_lang_change(lang)
        return jsonify({"success": True})

    @app.route("/api/mute", methods=["POST"])
    def api_mute():
        muted = bool((request.get_json(silent=True) or {}).get("muted", False))
        if ctx.on_mute_change:
            ctx.on_mute_change(muted)
        return jsonify({"success": True})

    @app.route("/api/exit", methods=["POST"])
    def api_exit():
        def _quit():
            time.sleep(0.8)
            os._exit(0)
        threading.Thread(target=_quit, daemon=True).start()
        return jsonify({"success": True})

    # ---- WebRTC signaling ---------------------------------------------------

    @socketio.on("join")
    def on_join(data):
        nonlocal desktop_sid
        role = data.get("role", "unknown")
        if role not in ("mobile", "desktop"):
            return
        if role == "desktop" and desktop_sid and desktop_sid != request.sid:
            # single desktop slot: a newer page replaces a stale one
            socketio.server.disconnect(desktop_sid, namespace="/")
            desktop_sid = None
        if role == "mobile":
            mobile = [sid for sid, r in sid_role.items() if r == "mobile" and sid != request.sid]
            if len(mobile) >= MAX_MOBILE_SENDERS:
                emit("join_rejected", {"reason": "too_many_senders"})
                return
        sid_role[request.sid] = role
        socketio.server.enter_room(request.sid, role)
        if role == "desktop":
            desktop_sid = request.sid
        counterpart = "desktop" if role == "mobile" else "mobile"
        socketio.emit("peer_joined", {"role": role}, to=counterpart)

    @socketio.on("disconnect")
    def on_disconnect():
        nonlocal desktop_sid
        role = sid_role.pop(request.sid, None)
        if role == "desktop" and desktop_sid == request.sid:
            desktop_sid = None
        if not role:
            return
        counterpart = "desktop" if role == "mobile" else "mobile"
        socketio.emit("peer_left", {"role": role}, to=counterpart)

    def _relay(event: str, data, target_room: str) -> None:
        emit(event, data, to=target_room)

    @socketio.on("offer")
    def on_offer(data):
        # offers originate from the phone
        _relay("offer", data, "desktop")

    @socketio.on("answer")
    def on_answer(data):
        _relay("answer", data, "mobile")

    @socketio.on("ice_candidate")
    def on_ice_candidate(data):
        role = sid_role.get(request.sid, "mobile")
        _relay("ice_candidate", data, "desktop" if role == "mobile" else "mobile")

    @socketio.on("toggle_mute")
    def on_toggle_mute():
        # tray → phone
        socketio.emit("toggle_mute", to="mobile")

    return app, socketio


def start_server_thread(ctx: ServerContext) -> SocketIO:
    """Create the app and run it in a daemon thread; returns the SocketIO
    instance so the caller can emit events (tray mute toggle, ...)."""
    app, socketio = create_server(ctx)

    def _run():
        ssl_context = "adhoc"
        if ctx.ssl_cert and ctx.ssl_key:
            ssl_context = (ctx.ssl_cert, ctx.ssl_key)
        socketio.run(
            app,
            host="0.0.0.0",
            port=ctx.port,
            ssl_context=ssl_context,
            debug=False,
            use_reloader=False,
            log_output=False,
            allow_unsafe_werkzeug=True,
        )

    threading.Thread(target=_run, daemon=True).start()
    return socketio
