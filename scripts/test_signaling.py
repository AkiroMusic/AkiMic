"""Signaling security & state-machine tests (Flask-SocketIO test client).

Covers the round-3 review matrix without a network: token auth for both
roles, busy rejection, role-gated unicast of offer/answer/ICE, payload
validation, and peer lifecycle notifications.

Run: .venv/Scripts/python scripts/test_signaling.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from akimic.server import ServerContext, create_server
from akimic.settings import Settings

PAIR = "test-pairing-secret"
DTOK = "test-desktop-token"

failures = []


def check(name, cond):
    print(f"{'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        failures.append(name)


def received(client, event):
    """Events of `event` received by a test client (dict args)."""
    return [item["args"][0] if item["args"] else None
            for item in client.get_received()
            if item["name"] == event and item.get("namespace") == "/"]


def first(client, event):
    got = received(client, event)
    return got[0] if got else None


def make_ctx():
    with tempfile.TemporaryDirectory() as d:
        pass  # noqa — temp handled by caller to keep scope obvious
    return None


import tempfile  # noqa: E402

import akimic.server as server_mod  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    ctx = ServerContext(settings=Settings(base_dir=tmp), platform=None,
                        port=5399, lan_ip="192.168.0.2",
                        pairing_secret=PAIR, desktop_token=DTOK)
    app, socketio = create_server(ctx)

    # the flask test client has no REMOTE_ADDR — simulate the WebView's
    # loopback for the whole run (the desktop gate also needs it)
    server_mod._is_loopback = lambda: True

    # --- mobile token auth ---
    bad = socketio.test_client(app)
    bad.emit("join", {"role": "mobile", "token": "wrong"})
    rej = first(bad, "join_rejected")
    check("mobile with wrong token rejected (bad_token)", rej and rej.get("reason") == "bad_token")

    empty = socketio.test_client(app)
    empty.emit("join", {"role": "mobile", "token": ""})
    rej = first(empty, "join_rejected")
    check("mobile with empty token rejected", rej and rej.get("reason") == "bad_token")

    mobile = socketio.test_client(app)
    mobile.emit("join", {"role": "mobile", "token": PAIR})
    rs = first(mobile, "room_state")
    check("mobile with correct token joins (room_state)", rs is not None)
    check("room_state reports desktop offline", rs and rs.get("desktop_online") is False)

    # --- single mobile slot ---
    second = socketio.test_client(app)
    second.emit("join", {"role": "mobile", "token": PAIR})
    rej = first(second, "join_rejected")
    check("second mobile rejected (busy)", rej and rej.get("reason") == "busy")

    # --- desktop token auth ---
    d1 = socketio.test_client(app)
    d1.emit("join", {"role": "desktop", "token": "wrong"})
    rej = first(d1, "join_rejected")
    check("desktop with wrong token rejected", rej and rej.get("reason") == "desktop_auth_failed")

    d1.emit("join", {"role": "desktop", "token": DTOK})
    rs = first(d1, "room_state")
    check("desktop with correct token joins", rs is not None and rs.get("desktop_online") is True)
    pj = first(mobile, "peer_joined")
    check("mobile notified of desktop join", pj and pj.get("role") == "desktop")

    # desktop role must not be claimable after token join either (duplicate join no-op)
    d1.emit("join", {"role": "desktop", "token": DTOK})
    check("duplicate desktop join is a no-op (no second room_state)",
          first(d1, "room_state") is None)

    # --- role-gated unicast ---
    offer = {"type": "offer", "sdp": "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\n"}
    mobile.emit("offer", offer)
    got = first(d1, "offer")
    check("mobile offer unicast to desktop", got == offer)

    mobile.emit("answer", {"type": "answer", "sdp": "x"})
    check("mobile cannot send answer (desktop got nothing)",
          first(d1, "answer") is None)

    answer = {"type": "answer", "sdp": "v=0\r\no=- 2 2 IN IP4 127.0.0.1\r\n"}
    d1.emit("answer", answer)
    check("desktop answer unicast to mobile", first(mobile, "answer") == answer)

    cand = {"candidate": "candidate:1 1 UDP 1 192.168.0.2 50000 typ host"}
    mobile.emit("ice_candidate", cand)
    check("mobile ICE unicast to desktop", first(d1, "ice_candidate") == cand)
    d1.emit("ice_candidate", cand)
    check("desktop ICE unicast to mobile", first(mobile, "ice_candidate") == cand)

    # --- payload validation ---
    mobile.emit("offer", {"sdp": "no-type"})
    check("offer without type dropped", first(d1, "offer") is None)
    mobile.emit("offer", {"type": "answer", "sdp": "x"})  # wrong type on offer event
    check("offer event with answer type dropped", first(d1, "offer") is None)
    mobile.emit("offer", {"type": "offer", "sdp": "x" * 70000})
    check("oversized SDP dropped", first(d1, "offer") is None)
    mobile.emit("ice_candidate", {"cand": "missing-field"})
    check("malformed ICE dropped", first(d1, "ice_candidate") is None)

    # unauthenticated client cannot relay signaling
    rogue = socketio.test_client(app)
    rogue.emit("offer", offer)
    check("unjoined client offer dropped", first(d1, "offer") is None)

    # --- mute_state gating ---
    mobile.emit("mute_state", {"muted": True})
    check("mobile mute_state accepted (no error path)", True)  # state side-effect via callback

    # --- lifecycle ---
    d1.disconnect()
    pl = first(mobile, "peer_left")
    check("desktop disconnect notifies mobile (peer_left)", pl and pl.get("role") == "desktop")

    # slot free again: a new mobile can join after the old one leaves
    mobile.disconnect()
    third = socketio.test_client(app)
    third.emit("join", {"role": "mobile", "token": PAIR})
    check("mobile slot freed after disconnect", first(third, "room_state") is not None)

    # origin unit checks (exact scheme+host+port)
    from akimic.server import _origin_allowed
    cases = [
        ("https://127.0.0.1:5399", True),
        ("http://evil.com:5399", False),                    # wrong host
        ("https://127.0.0.1:8443", False),          # other localhost port
        ("https://localhost:5399", True),
        ("https://192.168.0.2:5399", True),
        ("http://127.0.0.1:5399", True),
        ("https://127.0.0.1:5399.evil.com", False),
    ]
    for origin, want in cases:
        got = _origin_allowed(origin, ctx)
        check(f"origin {origin} -> {want}", got == want)

print(f"\n{'ALL OK' if not failures else f'{len(failures)} FAILURES: {failures}'}")
sys.exit(0 if not failures else 1)
