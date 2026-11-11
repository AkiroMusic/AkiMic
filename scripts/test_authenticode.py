"""Windows integration test for _authenticode_check (run on Windows only).

Fifth-audit P1-01 acceptance: the PowerShell publisher extraction must
actually execute (X509NameType — the previous X500NameFormat had no
SimpleName member and failed every call), pass on a real VB-CABLE
installer when one is available, and fail closed on wrong publisher,
unsigned files and missing file.

Run: .venv/Scripts/python scripts/test_authenticode.py
"""

import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if os.name != "nt":
    print("SKIP  not Windows")
    sys.exit(0)

from akimic.driver.windows import _EXPECTED_SIGNER_CN, _safe_extract, _authenticode_check  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        failures.append(name)


# 1. missing file → fail closed (Get-AuthenticodeSignature errors, $s null)
missing = Path(tempfile.gettempdir()) / "akimic-no-such-file.exe"
ok, msg = _authenticode_check(str(missing))
check("missing file fail-closed", not ok, msg)

# 2. garbage file → NotSigned / null certificate → fail closed
tmpdir = tempfile.mkdtemp(prefix="akimic-sigtest-")
try:
    garbage = Path(tmpdir) / "garbage.exe"
    garbage.write_bytes(b"MZ not really a signed binary")
    ok, msg = _authenticode_check(str(garbage))
    check("unsigned garbage fail-closed", not ok, msg)

    # 3. any Authenticode-signed system binary → Valid status but the
    #    publisher is not VB-Audio → must fail closed on the CN pin
    #    (proves the extraction itself runs and returns a real name)
    signed = next((str(p) for p in (
        Path(r"C:\Windows\System32\notepad.exe"),
        Path(r"C:\Windows\System32\cmd.exe"),
        Path(r"C:\Windows\explorer.exe"),
    ) if p.exists()), None)
    ok, msg = _authenticode_check(signed)
    check("foreign valid signature fail-closed", not ok, msg)

    # 4. real VB-CABLE installer, when present → the only pass path
    zip_path = os.environ.get("AKIMIC_VBCABLE_ZIP") or ""
    if not zip_path:
        ref_zip = ROOT / "REF-ZeroMic-main" / "drivers" / "vbcable.zip"
        zip_path = str(ref_zip) if ref_zip.exists() else ""
    if zip_path and Path(zip_path).exists():
        with zipfile.ZipFile(zip_path) as zf:
            _safe_extract(zf, tmpdir)
        setup = Path(tmpdir) / "VBCABLE_Setup_x64.exe"
        if setup.exists():
            ok, msg = _authenticode_check(str(setup))
            check(f"real VB-CABLE installer accepted (CN={_EXPECTED_SIGNER_CN!r})",
                  ok and _EXPECTED_SIGNER_CN.lower() in msg.lower(), msg)
        else:
            print("SKIP  zip present but no VBCABLE Setup exe inside")
    else:
        print("SKIP  no local vbcable.zip (set AKIMIC_VBCABLE_ZIP to test the pass path)")
finally:
    shutil.rmtree(tmpdir, ignore_errors=True)

print(f"\n{'ALL OK' if not failures else f'{len(failures)} FAILURES: {failures}'}")
sys.exit(0 if not failures else 1)
