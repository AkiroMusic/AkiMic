"""Platform abstraction for the virtual audio device (the "driver")."""


class BasePlatform:
    """Interface each platform must implement to manage a virtual microphone."""

    @property
    def use_system_browser(self) -> bool:
        """True = open the desktop page in the system browser instead of a window."""
        return False

    @property
    def gui_backend(self) -> str | None:
        """pywebview gui argument; None = autodetect."""
        return None

    @property
    def driver_display_name(self) -> str:
        """Name users should pick as *microphone* inside games / Discord."""
        raise NotImplementedError

    @property
    def driver_match_keyword(self) -> str:
        """Keyword (lowercase) that identifies the virtual *playback* device
        in the browser's enumerateDevices list (we play audio INTO it)."""
        raise NotImplementedError

    def get_webview_env(self) -> dict[str, str]:
        return {}

    def is_admin(self) -> bool:
        raise NotImplementedError

    def is_driver_installed(self) -> bool:
        raise NotImplementedError

    def install_driver(self) -> tuple[bool, str]:
        raise NotImplementedError

    def uninstall_driver(self) -> tuple[bool, str]:
        raise NotImplementedError

    def get_post_install_warning(self) -> str:
        return ""
