"""Persistent user settings for AkiMic.

Portable-first: settings.json lives next to the executable / project root.
Falls back to %APPDATA%/AkiMic when the portable location is not writable.
"""

import json
import os
import sys
import threading

DEFAULTS = {
    "lang": "auto",            # "auto" | "en_us" | "zh_cn"
    "theme": "dark",           # dark | amber | mint | pearl
    "desktop_gain": 1.0,       # output gain on the PC side
    "mobile_gain": 1.0,        # gain on the phone side
    "output_device_id": "",    # preferred audiooutput device (CABLE Input)
}


def _portable_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _fallback_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "AkiMic")


class Settings:
    def __init__(self):
        self._lock = threading.Lock()
        self._path = None
        self._data = dict(DEFAULTS)
        self._load()

    def _load(self) -> None:
        for base in (_portable_dir(), _fallback_dir()):
            path = os.path.join(base, "settings.json")
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self._data.update({k: v for k, v in loaded.items() if k in DEFAULTS})
                self._path = path
                return
            except (OSError, ValueError):
                continue
        # first run: try to pin the portable location, else the fallback
        try:
            os.makedirs(_portable_dir(), exist_ok=True)
            probe = os.path.join(_portable_dir(), ".akimic-write-test")
            with open(probe, "w") as f:
                f.write("t")
            os.remove(probe)
            self._path = os.path.join(_portable_dir(), "settings.json")
        except OSError:
            os.makedirs(_fallback_dir(), exist_ok=True)
            self._path = os.path.join(_fallback_dir(), "settings.json")

    def dir(self) -> str:
        """Directory holding settings.json (portable or fallback location)."""
        return os.path.dirname(self._path)

    def get(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, DEFAULTS.get(key, default))

    def as_dict(self) -> dict:
        with self._lock:
            return dict(self._data)

    def update(self, patch: dict) -> None:
        clean = {k: v for k, v in patch.items() if k in DEFAULTS}
        if not clean:
            return
        with self._lock:
            self._data.update(clean)
            try:
                with open(self._path, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, indent=2, ensure_ascii=False)
            except OSError:
                pass
