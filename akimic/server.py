"""Flask + SocketIO server: static web UI, REST API and WebRTC signaling.

Security model:
- Management REST endpoints (driver install/uninstall, exit, lang, desktop
  mute sync, full settings writes) are loopback-only — the phone may only
  write its own gain and the shared language.
- The desktop page always loads via 127.0.0.1, so its requests originate
  from loopback and pass automatically.
- Signaling: the desktop slot is single (a newer page replaces an older
  one), only ONE mobile sender is accepted, and SDP/ICE is unicast between
  the exact peer SIDs — never broadcast into shared rooms. Role checks are
  enforced per event: only a joined mobile may send offers, only the
  desktop may send answers.

The audio itself never passes through here; this is control-plane only.
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

# One phone is the product: a second sender gets an explicit busy instead of
# sharing the desktop with another stream (which would also crosstalk answers).
MAX_MOBILE_SENDERS = 1
_MAX_SDP_LEN = 65536


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
        self.on_mute_change = None   # callable(muted: bool) — phone source mute


def _is_loopback() -> bool:
    return (request.remote_addr or "") in ("127.0.0.1", "::1")


def _json_object():
    """Parsed JSON body if it is an object, else None."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


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
    active_offer_sid: str | None = None  # the mobile currently negotiating

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
            patch = _json_object()
            if patch is None:
                return jsonify({"error": "expected a JSON object"}), 400
            if not _is_loopback():
                # phones may only touch their own gain and the shared language
                patch = {k: v for k, v in patch.items() if k in ("mobile_gain", "lang")}
            ctx.settings.update(patch)
        return jsonify(ctx.settings.as_dict())

    @app.route("/api/driver/install", methods=["POST"])
    def api_driver_install():
        if not _is_loopback():
            return jsonify({"error": "local only"}), 403
        ok, msg = ctx.platform.install_driver()
        return jsonify({"success": ok, "msg": msg})

    @app.route("/api/driver/uninstall", methods=["POST"])
    def api_driver_uninstall():
        if not _is_loopback():
            return jsonify({"error": "local only"}), 403
        ok, msg = ctx.platform.uninstall_driver()
        if ok:
            from . import net
            net.remove_firewall_rules()
        return jsonify({"success": ok, "msg": msg})

    @app.route("/api/lang", methods=["POST"])
    def api_lang():
        if not _is_loopback():
            return jsonify({"error": "local only"}), 403
        data = _json_object()
        if data is None:
            return jsonify({"error": "expected a JSON object"}), 400
        lang = data.get("lang", "en_us")
        if ctx.on_lang_change:
            ctx.on_lang_change(lang)
        return jsonify({"success": True})

    @app.route("/api/mute", methods=["POST"])
    def api_mute():
        # desktop-page playback-mute sync; the tray follows the PHONE source
        # mute via the mute_state socket event instead
        if not _is_loopback():
            return jsonify({"error": "local only"}), 403
        return jsonify({"success": True})

    @app.route("/api/exit", methods=["POST"])
    def api_exit():
        if not _is_loopback():
            return jsonify({"error": "local only"}), 403

        def _quit():
            time.sleep(0.8)
            os._exit(0)
        threading.Thread(target=_quit, daemon=True).start()
        return jsonify({"success": True})

    # ---- WebRTC signaling ---------------------------------------------------

    @socketio.on("join")
    def on_join(data):
        nonlocal desktop_sid, active_offer_sid
        if not isinstance(data, dict) or request.sid in sid_role:
            return  # malformed or already joined (no role switching)
        role = data.get("role", "unknown")
        if role not in ("mobile", "desktop"):
            return

        if role == "desktop":
            if desktop_sid and desktop_sid != request.sid:
                # single desktop slot: a newer page replaces a stale one
                socketio.server.disconnect(desktop_sid, namespace="/")
            desktop_sid = request.sid
        else:
            others = sum(1 for sid, r in sid_role.items() if r == "mobile")
            if others >= MAX_MOBILE_SENDERS:
                emit("join_rejected", {"reason": "busy"})
                return

        sid_role[request.sid] = role
        socketio.server.enter_room(request.sid, role)

        # state snapshot so a freshly (re)opened page learns who is present
        emit("room_state", {
            "desktop_online": desktop_sid is not None,
            "mobile_online": any(r == "mobile" for r in sid_role.values()),
        })

    @socketio.on("disconnect")
    def on_disconnect():
        nonlocal desktop_sid, active_offer_sid
        role = sid_role.pop(request.sid, None)
        if role == "desktop" and desktop_sid == request.sid:
            desktop_sid = None
        if request.sid == active_offer_sid:
            active_offer_sid = None
        if not role:
            return
        counterpart = "desktop" if role == "mobile" else "mobile"
        socketio.emit("peer_left", {"role": role}, to=counterpart)

    def _valid_sdp(data) -> bool:
        return (isinstance(data, dict)
                and isinstance(data.get("sdp"), str)
                and 0 < len(data["sdp"]) <= _MAX_SDP_LEN)

    def _valid_candidate(data) -> bool:
        return (isinstance(data, dict)
                and isinstance(data.get("candidate"), str)
                and len(data["candidate"]) <= 2048)

    @socketio.on("offer")
    def on_offer(data):
        nonlocal active_offer_sid
        if sid_role.get(request.sid) != "mobile" or not _valid_sdp(data):
            return
        active_offer_sid = request.sid
        if desktop_sid:
            emit("offer", data, to=desktop_sid)

    @socketio.on("answer")
    def on_answer(data):
        if sid_role.get(request.sid) != "desktop" or not _valid_sdp(data):
            return
        if active_offer_sid:
            emit("answer", data, to=active_offer_sid)

    @socketio.on("ice_candidate")
    def on_ice_candidate(data):
        role = sid_role.get(request.sid)
        if not _valid_candidate(data):
            return
        if role == "mobile":
            if desktop_sid:
                emit("ice_candidate", data, to=desktop_sid)
        elif role == "desktop":
            if active_offer_sid:
                emit("ice_candidate", data, to=active_offer_sid)

    @socketio.on("mute_state")
    def on_mute_state(data):
        # phone source mute → tray label
        if sid_role.get(request.sid) != "mobile" or not isinstance(data, dict):
            return
        if ctx.on_mute_change:
            ctx.on_mute_change(bool(data.get("muted", False)))

    @socketio.on("toggle_mute")
    def on_toggle_mute():
        # tray → phone (the tray button toggles the phone source)
        socketio.emit("toggle_mute", to="mobile")

    return app, socketio


def start_server_thread(ctx: ServerContext) -> SocketIO:
    """Create the app and run it in a daemon thread; returns the SocketIO
    instance so the caller can emit events (tray mute toggle, ...).
    Readiness is confirmed separately via net.wait_server_ready()."""
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
