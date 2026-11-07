"""One-off: remove "AkiMic Output" rename residue from CABLE capture endpoints.

Run: .venv/Scripts/python scripts/cleanup_endpoint_name.py
"""

import sys

sys.path.insert(0, ".")

from akimic.driver.windows import WindowsPlatform

p = WindowsPlatform()
ok, msg = p.cleanup_rename_residue()
print("cleanup:", ok, "|", msg)
