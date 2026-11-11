"""Flask + SocketIO server: static web UI, REST API and WebRTC signaling.

Security model:
- Pairing: a per-run random secret is required for a client to join as
  "mobile". The secret is handed to the phone inside the QR URL
  (?p=...), so the QR itself is the credential; sockets without it are
  rejected. The secret is never exposed on LAN-readable endpoints —
  /api/pair_url (loopback-only) composes the QR URL for the desktop page.
- Desktop role: joins are only accepted from loopback (the desktop page
  always loads via 127.0.0.1, so local = trusted; LAN cannot claim it).
- Management REST endpoints (driver install/uninstall, exit, lang, mute
  sync, full settings writes) are loopback-only; phones may only write
  their own gain and the shared language.
- Signaling: single desktop slot (newer page replaces older), ONE mobile
  sender, SDP/ICE unicast between exact peer SIDs with strict role and
  payload (sdp type + length) validation. There is no inbound
  toggle_mute — the tray emits it server-side only.

The audio itself never passes through here; this is control-plane only.
"""

import os
import secrets
import socket
import threading
import time
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit

from . import __version__
from .driver.base import BasePlatform
from .settings import DEFAULTS, Settings

WEBUI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webui")

MAX_MOBILE_SENDERS = 1  # a second phone gets an explicit busy
_MAX_SDP_LEN = 65536


class ServerContext:
    """Shared state handed to routes and callbacks owned by main.py."""

    def __init__(self, settings: Settings, platform: BasePlatform, port: int, lan_ip: str,
                 fw_ok: bool = False, ssl_cert: str | None = None, ssl_key: str | None = None,
                 pairing_secret: str = "", desktop_token: str = ""):
        self.settings = settings
        self.platform = platform
        self.port = port
        self.lan_ip = lan_ip
        self.fw_ok = fw_ok
        self.ssl_cert = ssl_cert
        self.ssl_key = ssl_key
        self.pairing_secret = pairing_secret   # mobile credential (QR ?p=)
        self.desktop_token = desktop_token     # desktop credential (loopback API)
        self.on_lang_change = None   # callable(lang_code)
        self.on_mute_change = None   # callable(muted: bool) — phone source mute


def _is_loopback() -> bool:
    return (request.remote_addr or "") in ("127.0.0.1", "::1")


def _json_object():
    """Parsed JSON body if it is an object, else None."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def _origin_allowed(origin: str | None, ctx: ServerContext) -> bool:
    """Exact match against the origins AkiMic itself serves (host AND port).
    Comparing only the hostname would let any other localhost port claim to
    be us. Scheme is matched but http/https both accepted for our own
    address — production is https; the http variant exists for dev/testing.
    Non-browser clients (no Origin) pass; Socket.IO roles are gated
    separately by tokens."""
    if not origin:
        return True  # non-browser clients
    try:
        parts = urlsplit(origin)
        if not parts.hostname:
            return False
        host = parts.hostname.lower()
        port = parts.port  # malformed port raises → rejected below
    except ValueError:
        return False
    if port != ctx.port:
        return False
    if parts.scheme not in ("https", "http"):
        return False
    return host in ("localhost", "127.0.0.1", ctx.lan_ip,
                    socket.gethostname().lower())


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
    state_lock = threading.Lock()  # threading mode: joins can race

    def _mgmt_allowed() -> bool:
        """Management POSTs: loopback AND our per-run CSRF token header.
        Cross-origin pages can hit loopback URLs but cannot set custom
        headers without a CORS preflight we never grant."""
        if not _is_loopback():
            return False
        sent = request.headers.get("X-AkiMic-Token", "")
        return bool(ctx.desktop_token) and secrets.compare_digest(sent, ctx.desktop_token)

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
        # deliberately WITHOUT the pairing secret (LAN-readable)
        return jsonify({
            "app": "AkiMic",
            "version": __version__,
            "ip": ctx.lan_ip,
            "port": ctx.port,
            "url": f"https://{ctx.lan_ip}:{ctx.port}",
        })

    @app.route("/api/pair_url")
    def api_pair_url():
        """QR target (with the mobile pairing secret) and the desktop token.
        Loopback only — the desktop page is the sole legitimate caller."""
        if not _is_loopback():
            return jsonify({"error": "local only"}), 403
        resp = jsonify({
            "url": f"https://{ctx.lan_ip}:{ctx.port}/?p={ctx.pairing_secret}",
            "desktop_token": ctx.desktop_token,
        })
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/healthz")
    def healthz():
        return jsonify({"app": "AkiMic", "version": __version__})

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
            if _is_loopback() and ctx.desktop_token and secrets.compare_digest(
                    request.headers.get("X-AkiMic-Token", ""), ctx.desktop_token):
                pass  # authenticated desktop: full settings write
            elif (not _is_loopback() and ctx.pairing_secret
                  and secrets.compare_digest(
                      request.headers.get("X-AkiMic-Pairing-Token", ""),
                      ctx.pairing_secret)):
                # authenticated phone: only its own gain and shared language
                patch = {k: v for k, v in patch.items() if k in ("mobile_gain", "lang")}
            else:
                return jsonify({"error": "not authorized"}), 403
            known = [k for k in patch if k in DEFAULTS]
            valid = {k: v for k, v in patch.items()
                     if ctx.settings.validate_key(k, v) is not None}
            if known and not valid:
                # every provided field was invalid — say so, don't fake success
                return jsonify({"error": "all provided settings invalid"}), 400
            persisted = ctx.settings.update(valid)
        data = ctx.settings.as_dict()
        data["persisted"] = bool(persisted) if request.method == "POST" else True
        return jsonify(data)

    @app.route("/api/driver/install", methods=["POST"])
    def api_driver_install():
        if not _mgmt_allowed():
            return jsonify({"error": "local only"}), 403
        ok, msg = ctx.platform.install_driver()
        return jsonify({"success": ok, "msg": msg})

    @app.route("/api/driver/uninstall", methods=["POST"])
    def api_driver_uninstall():
        if not _mgmt_allowed():
            return jsonify({"error": "local only"}), 403
        ok, msg = ctx.platform.uninstall_driver()
        if ok:
            from . import net
            net.remove_firewall_rules()
        return jsonify({"success": ok, "msg": msg})

    @app.route("/api/lang", methods=["POST"])
    def api_lang():
        if not _mgmt_allowed():
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
        if not _mgmt_allowed():
            return jsonify({"error": "local only"}), 403
        return jsonify({"success": True})

    @app.route("/api/exit", methods=["POST"])
    def api_exit():
        if not _mgmt_allowed():
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
        if not isinstance(data, dict):
            emit("join_rejected", {"reason": "malformed"})
            return
        if request.sid in sid_role:
            return  # already joined (no role switching)

        role = data.get("role", "unknown")
        if role == "desktop":
            # loopback alone proves nothing (any local page is loopback) —
            # require the per-run desktop token from /api/pair_url
            token = data.get("token", "")
            if (not _is_loopback() or not ctx.desktop_token
                    or not isinstance(token, str)
                    or not secrets.compare_digest(token, ctx.desktop_token)):
                emit("join_rejected", {"reason": "desktop_auth_failed"})
                return
        elif role == "mobile":
            token = data.get("token", "")
            # fail closed: an empty server-side secret must never open the door
            if (not ctx.pairing_secret or not isinstance(token, str)
                    or not secrets.compare_digest(token, ctx.pairing_secret)):
                emit("join_rejected", {"reason": "bad_token"})
                return
        else:
            emit("join_rejected", {"reason": "unknown_role"})
            return

        # all state judgment and mutation under the lock (a SID can race
        # itself with concurrent joins); every emit / disconnect outside it
        duplicate = False
        busy = False
        replace_sid = None
        snapshot = None
        with state_lock:
            if request.sid in sid_role:
                duplicate = True  # re-check under the lock: no role switching
            elif role == "desktop" and desktop_sid and desktop_sid != request.sid:
                replace_sid = desktop_sid  # single desktop slot: replace stale page
            elif role == "mobile":
                others = sum(1 for sid, r in sid_role.items() if r == "mobile")
                busy = others >= MAX_MOBILE_SENDERS
            if not duplicate and not busy:
                sid_role[request.sid] = role
                socketio.server.enter_room(request.sid, role)
                if role == "desktop":
                    desktop_sid = request.sid
                snapshot = {
                    "desktop_online": desktop_sid is not None,
                    "mobile_online": any(r == "mobile" for r in sid_role.values()),
                }
        if duplicate:
            return
        if busy:
            emit("join_rejected", {"reason": "busy"})
            return
        if replace_sid:
            socketio.server.disconnect(replace_sid, namespace="/")

        # state snapshot so a freshly (re)opened page learns who is present…
        emit("room_state", snapshot)
        # …and notify the counterpart (covers "desktop first, phone second")
        counterpart = "desktop" if role == "mobile" else "mobile"
        socketio.emit("peer_joined", {"role": role}, to=counterpart)

    @socketio.on("disconnect")
    def on_disconnect():
        nonlocal desktop_sid, active_offer_sid
        with state_lock:
            role = sid_role.pop(request.sid, None)
            if role == "desktop" and desktop_sid == request.sid:
                desktop_sid = None
            if request.sid == active_offer_sid:
                active_offer_sid = None
        if not role:
            return
        counterpart = "desktop" if role == "mobile" else "mobile"
        socketio.emit("peer_left", {"role": role}, to=counterpart)

    def _valid_sdp(data, expected_type: str) -> bool:
        return (isinstance(data, dict)
                and data.get("type") == expected_type
                and isinstance(data.get("sdp"), str)
                and 0 < len(data["sdp"]) <= _MAX_SDP_LEN)

    def _valid_candidate(data) -> bool:
        return (isinstance(data, dict)
                and isinstance(data.get("candidate"), str)
                and len(data["candidate"]) <= 2048)

    @socketio.on("offer")
    def on_offer(data):
        nonlocal active_offer_sid
        if not _valid_sdp(data, "offer"):
            return
        with state_lock:
            if sid_role.get(request.sid) != "mobile":
                return
            target = desktop_sid
            active_offer_sid = request.sid
        if target:  # emit outside the lock (callbacks mutate the same state)
            emit("offer", data, to=target)

    @socketio.on("answer")
    def on_answer(data):
        if not _valid_sdp(data, "answer"):
            return
        with state_lock:
            if sid_role.get(request.sid) != "desktop":
                return
            target = active_offer_sid
        if target:
            emit("answer", data, to=target)

    @socketio.on("ice_candidate")
    def on_ice_candidate(data):
        if not _valid_candidate(data):
            return
        with state_lock:
            role = sid_role.get(request.sid)
            if role == "mobile":
                target = desktop_sid
            elif role == "desktop":
                target = active_offer_sid
            else:
                return
        if target:
            emit("ice_candidate", data, to=target)

    @socketio.on("mute_state")
    def on_mute_state(data):
        # phone source mute → tray label
        with state_lock:
            role = sid_role.get(request.sid)
        if role != "mobile" or not isinstance(data, dict):
            return
        muted = data.get("muted")
        if type(muted) is not bool:  # strict: missing field and non-bools are dropped
            return
        if ctx.on_mute_change:
            ctx.on_mute_change(muted)

    # NOTE: no inbound toggle_mute — the tray emits it server-side only, so
    # LAN clients cannot flip the phone's mute.

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
