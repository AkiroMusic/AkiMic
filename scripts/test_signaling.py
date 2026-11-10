"""Signaling security & state-machine tests (Flask-SocketIO test client).

Covers the review matrices without a network: token auth for both roles,
loopback gating, busy rejection, role-gated unicast of offer/answer/ICE,
payload validation, mute-state callback, origin edge cases, empty-secret
fail-closed behavior, and the management/settings HTTP auth matrix.

Run: .venv/Scripts/python scripts/test_signaling.py
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import akimic.server as server_mod  # noqa: E402
from akimic.server import ServerContext, create_server  # noqa: E402
from akimic.settings import Settings  # noqa: E402

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


tmp = tempfile.TemporaryDirectory()
ctx = ServerContext(settings=Settings(base_dir=tmp.name), platform=None,
                    port=5399, lan_ip="192.168.0.2",
                    pairing_secret=PAIR, desktop_token=DTOK)
app, socketio = create_server(ctx)
mute_calls = []
ctx.on_mute_change = lambda muted: mute_calls.append(muted)

# the flask test client has no REMOTE_ADDR — simulate the WebView's
# loopback for the main run. A dedicated branch below restores it to
# prove the non-loopback desktop rejection really fires.
orig_loopback = server_mod._is_loopback
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

d1.emit("join", {"role": "desktop", "token": DTOK})
check("duplicate desktop join is a no-op (no second room_state)",
      first(d1, "room_state") is None)

# --- role-gated unicast ---
offer = {"type": "offer", "sdp": "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\n"}
mobile.emit("offer", offer)
check("mobile offer unicast to desktop", first(d1, "offer") == offer)

mobile.emit("answer", {"type": "answer", "sdp": "x"})
check("mobile cannot send answer (desktop got nothing)", first(d1, "answer") is None)

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

# --- mute_state gating (observable callback) ---
mobile.emit("mute_state", {"muted": True})
check("mobile mute_state reaches callback (True)", mute_calls == [True])
mobile.emit("mute_state", {"muted": False})
check("mobile mute_state reaches callback (False)", mute_calls == [True, False])
d1.emit("mute_state", {"muted": True})
check("desktop cannot trigger mute callback", mute_calls == [True, False])

# --- lifecycle ---
d1.disconnect()
pl = first(mobile, "peer_left")
check("desktop disconnect notifies mobile (peer_left)", pl and pl.get("role") == "desktop")

mobile.disconnect()
third = socketio.test_client(app)
third.emit("join", {"role": "mobile", "token": PAIR})
check("mobile slot freed after disconnect", first(third, "room_state") is not None)

# --- non-loopback desktop rejection (restore real _is_loopback) ---
server_mod._is_loopback = lambda: False  # simulate a LAN client
lan_desktop = socketio.test_client(app)
lan_desktop.emit("join", {"role": "desktop", "token": DTOK})
rej = first(lan_desktop, "join_rejected")
check("desktop join from non-loopback rejected even with correct token",
      rej and rej.get("reason") == "desktop_auth_failed")
server_mod._is_loopback = orig_loopback

# --- origin unit checks (exact scheme+host+port) ---
from akimic.server import _origin_allowed  # noqa: E402

origin_cases = [
    ("https://127.0.0.1:5399", True),
    ("https://localhost:5399", True),
    ("https://192.168.0.2:5399", True),
    ("http://127.0.0.1:5399", True),            # dev server runs http
    ("http://evil.com:5399", False),            # wrong host
    ("https://127.0.0.1:8443", False),          # other localhost port
    ("https://127.0.0.1:5399.evil.com", False),  # malformed port (regression)
    ("https://127.0.0.1:notaport", False),       # malformed port (regression)
    ("ftp://127.0.0.1:5399", False),             # wrong scheme
]
for origin, want in origin_cases:
    got = _origin_allowed(origin, ctx)
    check(f"origin {origin} -> {want}", got == want)
tmp.cleanup()

# --- empty-secret / empty-desktop-token fail-closed (fresh instances) ---
tmp2 = tempfile.TemporaryDirectory()
ctx2 = ServerContext(settings=Settings(base_dir=tmp2.name), platform=None,
                     port=5398, lan_ip="127.0.0.1",
                     pairing_secret="", desktop_token="")
app2, socketio2 = create_server(ctx2)
m2 = socketio2.test_client(app2)
m2.emit("join", {"role": "mobile", "token": ""})
rej = first(m2, "join_rejected")
check("empty server secret: mobile with empty token fail-closed",
      rej and rej.get("reason") == "bad_token")
d2 = socketio2.test_client(app2)
d2.emit("join", {"role": "desktop", "token": ""})
rej = first(d2, "join_rejected")
check("empty desktop token: desktop fail-closed",
      rej and rej.get("reason") == "desktop_auth_failed")
tmp2.cleanup()

# --- HTTP auth matrix (management + settings + pair_url) ---
tmp3 = tempfile.TemporaryDirectory()
ctx3 = ServerContext(settings=Settings(base_dir=tmp3.name), platform=None,
                     port=5397, lan_ip="192.168.0.2",
                     pairing_secret=PAIR, desktop_token=DTOK)
app3, _sio3 = create_server(ctx3)
http = app3.test_client()


def post(path, body=None, token=None, remote="127.0.0.1", pairing=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-AkiMic-Token"] = token
    if pairing:
        headers["X-AkiMic-Pairing-Token"] = pairing
    return http.post(path, data=json.dumps(body or {}), headers=headers,
                     environ_base={"REMOTE_ADDR": remote})


# pair_url: loopback OK, LAN 403
r = http.get("/api/pair_url", environ_base={"REMOTE_ADDR": "127.0.0.1"})
check("pair_url from loopback OK", r.status_code == 200 and "desktop_token" in r.get_json())
r = http.get("/api/pair_url", environ_base={"REMOTE_ADDR": "192.168.1.9"})
check("pair_url from LAN rejected (403)", r.status_code == 403)

# management: token required, wrong token 403, correct token passes
r = post("/api/mute", token=None)
check("/api/mute without token 403", r.status_code == 403)
r = post("/api/mute", token="wrong")
check("/api/mute with wrong token 403", r.status_code == 403)
r = post("/api/mute", token=DTOK)
check("/api/mute with correct token 200", r.status_code == 200)

# exit endpoint: token-gated as well (never actually exits in test —
# correct token would schedule exit, so we only assert the rejection paths)
r = post("/api/exit", token=None)
check("/api/exit without token 403", r.status_code == 403)
r = post("/api/exit", token="wrong")
check("/api/exit with wrong token 403", r.status_code == 403)

# settings: loopback full write needs token; LAN needs pairing token
r = post("/api/settings", {"desktop_gain": 2.0}, token=None)
check("settings loopback without token 403", r.status_code == 403)
r = post("/api/settings", {"desktop_gain": 2.0}, token="wrong")
check("settings loopback wrong token 403", r.status_code == 403)
r = post("/api/settings", {"desktop_gain": 2.0}, token=DTOK)
check("settings desktop token full write 200",
      r.status_code == 200 and r.get_json().get("desktop_gain") == 2.0)
r = post("/api/settings", {"desktop_gain": 3.0}, token=None,
         remote="192.168.1.9")
check("settings LAN without pairing token 403", r.status_code == 403)
r = post("/api/settings", {"desktop_gain": 3.0}, token=None,
         remote="192.168.1.9", pairing=PAIR)
check("settings LAN with pairing token cannot write desktop fields",
      r.status_code == 200 and r.get_json().get("desktop_gain") == 2.0)
r = post("/api/settings", {"mobile_gain": 0.5}, token=None,
         remote="192.168.1.9", pairing=PAIR)
check("settings LAN with pairing token writes mobile_gain",
      r.status_code == 200 and r.get_json().get("mobile_gain") == 0.5)
r = post("/api/settings", {"mobile_gain": 0.5}, token=None,
         remote="192.168.1.9", pairing="wrong")
check("settings LAN with wrong pairing token 403", r.status_code == 403)
tmp3.cleanup()

print(f"\n{'ALL OK' if not failures else f'{len(failures)} FAILURES: {failures}'}")
sys.exit(0 if not failures else 1)
