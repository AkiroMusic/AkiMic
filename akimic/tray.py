"""System tray icon (Windows): show window, mute toggle, exit."""

import json
import os

from . import __version__

WEBUI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webui")

DEFAULT_STRINGS = {
    "tray_show": "Show AkiMic",
    "tray_exit": "Exit",
    "tray_mute": "Mute",
    "tray_unmute": "Unmute",
    "tray_title": f"AkiMic {__version__}",
}


class Tray:
    """Wraps pystray; safe no-op if construction fails."""

    def __init__(self, icon_path: str, on_show, on_exit, on_mute_toggle):
        self._on_show = on_show
        self._on_exit = on_exit
        self._on_mute_toggle = on_mute_toggle
        self._strings = dict(DEFAULT_STRINGS)
        self._muted = False
        self._icon = None
        try:
            import pystray
            from PIL import Image

            image = Image.open(icon_path) if icon_path and os.path.exists(icon_path) \
                else Image.new("RGB", (64, 64), color="#0C1220")

            menu = pystray.Menu(
                pystray.MenuItem(lambda item: self._strings["tray_show"], self._show, default=True),
                pystray.MenuItem(lambda item: self._muted_label(), self._mute_toggle),
                pystray.MenuItem(lambda item: self._strings["tray_exit"], self._exit),
            )
            self._icon = pystray.Icon("AkiMic", image, self._strings["tray_title"], menu)
        except Exception:
            self._icon = None

    # --- menu handlers ----------------------------------------------------

    def _show(self, icon, item):
        if self._on_show:
            self._on_show()

    def _exit(self, icon, item):
        if self._icon:
            self._icon.stop()
        if self._on_exit:
            self._on_exit()

    def _mute_toggle(self, icon, item):
        if self._on_mute_toggle:
            self._on_mute_toggle()

    def _muted_label(self):
        return self._strings["tray_unmute"] if self._muted else self._strings["tray_mute"]

    # --- public API ---------------------------------------------------------

    def start(self) -> None:
        if self._icon:
            self._icon.run_detached()

    def stop(self) -> None:
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass

    def update_menu(self) -> None:
        if self._icon:
            try:
                self._icon.update_menu()
            except Exception:
                pass

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)
        self.update_menu()

    def apply_language(self, lang_code: str) -> None:
        """Reload tray strings from webui/lang/<code>.json when available."""
        try:
            lang_dir = os.path.join(WEBUI_DIR, "lang")
            lang_file = os.path.join(lang_dir, f"{lang_code}.json")
            if not os.path.exists(lang_file):
                prefix = lang_code.split("_")[0]
                for fname in os.listdir(lang_dir):
                    if fname.startswith(prefix):
                        lang_file = os.path.join(lang_dir, fname)
                        break
            if os.path.exists(lang_file):
                with open(lang_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for key in DEFAULT_STRINGS:
                    if key in data:
                        self._strings[key] = data[key]
                self.update_menu()
        except Exception:
            pass
