"""Pick the platform implementation for the current OS."""

import sys

from .base import BasePlatform


def get_platform() -> BasePlatform:
    if sys.platform == "win32":
        from .windows import WindowsPlatform
        return WindowsPlatform()
    # AkiMic is Windows-first; other platforms are not implemented yet.
    raise RuntimeError(f"unsupported platform: {sys.platform}")
