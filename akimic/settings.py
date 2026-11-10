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

_NUMBER_KEYS = ("desktop_gain", "mobile_gain")
_ENUM_KEYS = {"theme": ("dark", "amber", "mint", "pearl"), "lang": ("auto", "en_us", "zh_cn")}


def _validate(key: str, value):
    """Per-key type/range validation; returns the cleaned value or None.

    Out-of-range values are rejected (caller falls back to the default) —
    never clamped, so a corrupt "-999" cannot silently become a mute.
    """
    if key in _NUMBER_KEYS:
        if isinstance(value, bool):
            return None
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return value if 0.0 <= value <= 3.0 else None
    if key in _ENUM_KEYS:
        return value if value in _ENUM_KEYS[key] else None
    if key == "output_device_id":
        return value if isinstance(value, str) and len(value) <= 256 else None
    return None


class Settings:
    def __init__(self, base_dir: str | None = None):
        """base_dir pins the settings location (tests); None = portable search."""
        self._lock = threading.Lock()
        self._path = None
        self._data = dict(DEFAULTS)
        if base_dir is not None:
            os.makedirs(base_dir, exist_ok=True)
            self._path = os.path.join(base_dir, "settings.json")
            self._load_file(self._path)
        else:
            self._load()

    def _read(self, path: str) -> dict | None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            return loaded if isinstance(loaded, dict) else None
        except (OSError, ValueError):
            return None

    def _load_file(self, path: str) -> None:
        loaded = self._read(path)
        if loaded is None:
            return
        # loaded values pass the SAME validation as API writes — a stale or
        # hand-edited file can never inject out-of-range settings
        for key, value in loaded.items():
            if key in DEFAULTS:
                value = _validate(key, value)
                if value is not None:
                    self._data[key] = value

    def _load(self) -> None:
        for base in (_portable_dir(), _fallback_dir()):
            path = os.path.join(base, "settings.json")
            loaded = self._read(path)
            if loaded is not None:
                for key, value in loaded.items():
                    if key in DEFAULTS:
                        value = _validate(key, value)
                        if value is not None:
                            self._data[key] = value
                self._path = path
                return
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

    def validate_key(self, key: str, value):
        """Public wrapper so callers can pre-check a patch (server 400s when
        every provided field is invalid)."""
        return _validate(key, value)

    def update(self, patch) -> bool:
        """Validate + apply a patch; returns whether it persisted to disk."""
        if not isinstance(patch, dict):
            return False
        clean = {}
        for key, value in patch.items():
            if key in DEFAULTS:
                value = _validate(key, value)
                if value is not None:
                    clean[key] = value
        if not clean:
            return True
        with self._lock:
            self._data.update(clean)
            return self._save()

    def _save(self) -> bool:
        """Atomic write: temp file + replace, so a crash never truncates."""
        tmp_path = self._path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._path)
            return True
        except OSError:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return False


def _portable_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _fallback_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "AkiMic")
