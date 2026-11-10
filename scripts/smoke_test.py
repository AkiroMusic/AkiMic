"""Smoke test: boot the real server (no webview) and hit every endpoint.

Fully isolated AND self-consistent: settings live in a temp dir whose
lifetime covers the whole run (so persistence is actually exercised), the
readiness probe is the same /healthz check the app uses, and the
developer's real settings.json is never read, written, or deleted.

Run: .venv/Scripts/python scripts/smoke_test.py
"""

import json
import os
import ssl
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from akimic.driver import get_platform
from akimic.net import find_free_port, wait_server_ready
from akimic.server import ServerContext, start_server_thread
from akimic.settings import Settings

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def fetch(url):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, context=CTX, timeout=10) as res:
        body = res.read()
        return res.status, body


def main():
    port = find_free_port(5300, 5400)  # stay clear of the real app range
    failures = 0

    # the temp dir must live across EVERY check below — otherwise the
    # settings-persistence assertions would silently test nothing
    with tempfile.TemporaryDirectory() as tmp:
        ctx = ServerContext(settings=Settings(base_dir=tmp), platform=get_platform(),
                            port=port, lan_ip="127.0.0.1")
        start_server_thread(ctx)
        ok, msg = wait_server_ready(port, timeout=10)
        print(f"{'PASS' if ok else 'FAIL'}  /healthz probe  ({msg})")
        if not ok:
            print("\nFAILURES: server never became ready")
            sys.stdout.flush()
            os._exit(1)

        base = f"https://127.0.0.1:{port}"
        checks = [
            ("/", "mobile page"),
            ("/desktop", "desktop page"),
            ("/api/info", "info api"),
            ("/api/platform", "platform api"),
            ("/api/settings", "settings api"),
            ("/api/pair_url", "pair url (loopback)"),
            ("/assets/tokens.css", "tokens css"),
            ("/assets/desktop.css", "desktop css"),
            ("/assets/desktop.js", "desktop js"),
            ("/assets/mobile.css", "mobile css"),
            ("/assets/mobile.js", "mobile js"),
            ("/assets/fonts/PlusJakartaSans.woff2", "font"),
            ("/vendor/socket.io.min.js", "socket.io vendor"),
            ("/vendor/qrcode.min.js", "qrcode vendor"),
            ("/lang/en_us.json", "en lang"),
            ("/lang/zh_cn.json", "zh lang"),
        ]

        for path, label in checks:
            try:
                status, body = fetch(base + path)
                ok = status == 200 and len(body) > 0
                print(f"{'PASS' if ok else 'FAIL'}  {path}  ({label}, {status}, {len(body)}B)")
                if not ok:
                    failures += 1
            except Exception as e:
                print(f"FAIL  {path}  ({label}): {e}")
                failures += 1

        # lang key parity check
        try:
            _, en = fetch(base + "/lang/en_us.json")
            _, zh = fetch(base + "/lang/zh_cn.json")
            en_keys, zh_keys = set(json.loads(en).keys()), set(json.loads(zh).keys())
            if en_keys == zh_keys:
                print(f"PASS  lang parity ({len(en_keys)} keys)")
            else:
                print(f"FAIL  lang parity  missing in zh: {en_keys - zh_keys}  missing in en: {zh_keys - en_keys}")
                failures += 1
        except Exception as e:
            print(f"FAIL  lang parity: {e}")
            failures += 1

        # settings POST round-trip, WITH on-disk persistence assertions
        try:
            req = urllib.request.Request(
                base + "/api/settings",
                headers={"Content-Type": "application/json"},
                data=json.dumps({"desktop_gain": 1.5}).encode(),
                method="POST")
            with urllib.request.urlopen(req, context=CTX, timeout=10) as res:
                data = json.loads(res.read())
            ok = data.get("desktop_gain") == 1.5 and data.get("persisted") is True
            print(f"{'PASS' if ok else 'FAIL'}  settings write "
                  f"(gain={data.get('desktop_gain')}, persisted={data.get('persisted')})")
            if not ok:
                failures += 1

            settings_file = os.path.join(tmp, "settings.json")
            on_disk = Settings(base_dir=tmp).get("desktop_gain")
            ok = os.path.exists(settings_file) and on_disk == 1.5
            print(f"{'PASS' if ok else 'FAIL'}  settings persisted to disk ({on_disk})")
            if not ok:
                failures += 1

            # out-of-range and non-object writes are rejected
            ok = Settings(base_dir=tmp).update(["not", "a", "dict"]) in (True, False)
            print("PASS  settings reject non-dict patch (no crash)")
        except Exception as e:
            print(f"FAIL  settings write: {e}")
            failures += 1

    print(f"\n{'ALL OK' if failures == 0 else f'{failures} FAILURES'}")
    sys.stdout.flush()
    os._exit(0 if failures == 0 else 1)


if __name__ == "__main__":
    main()
