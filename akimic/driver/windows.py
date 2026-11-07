"""Virtual audio device management on Windows via VB-Audio Virtual Cable.

Audio path reminder: the desktop browser plays the WebRTC stream into the
"CABLE Input" playback device; VB-CABLE loops it to the "CABLE Output"
recording device, which apps pick as their microphone.

The default playback device is saved before install and restored after,
because the VB-CABLE installer likes to promote itself to default speaker.
"""

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import winreg
import zipfile

from .base import BasePlatform

_SETUP_EXE = "VBCABLE_Setup_x64.exe"
_UNINSTALLER_PATH = r"C:\Program Files\VB\CABLE\VBCABLE_Setup_x64.exe"

# Registry property keys of an audio endpoint's PropertyStore.
_PKEY_FMT = "{a45c254e-df1c-4efd-8020-67d146a850e0}"
_PKEY_DESC = f"{_PKEY_FMT},2"       # DeviceDesc
_PKEY_FRIENDLY = f"{_PKEY_FMT},14"  # FriendlyName (explicit value beats synthesis)
_MMDEVICES = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
_RENAME_RESIDUE = "akimic output"
_RESTORE_DESC = "CABLE Output"

# PowerShell (inline C#) COM snippets ------------------------------------
# GetDefaultAudioEndpoint via the documented IMMDeviceEnumerator.
_PS_GET_DEFAULT = r"""
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IMMDevice {
    int a(); int o();
    int GetId([MarshalAs(UnmanagedType.LPWStr)] out string id);
}
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IMMDeviceEnumerator {
    int f();
    int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice endpoint);
}
[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
public class MMDeviceEnumeratorComObject { }
public class AkimicAudio {
    public static string GetDefaultAudioEndpoint() {
        var enumerator = new MMDeviceEnumeratorComObject() as IMMDeviceEnumerator;
        enumerator.GetDefaultAudioEndpoint(0, 0, out IMMDevice dev);
        dev.GetId(out string id);
        return id;
    }
}
"@
[AkimicAudio]::GetDefaultAudioEndpoint()
"""

# SetDefaultEndpoint via the undocumented-but-universal IPolicyConfig.
_PS_SET_DEFAULT = r"""
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
[ComImport, Guid("870AF99C-171D-4F9E-AF0D-E63DF40C2BC9")]
internal class _CPolicyConfigClient { }
[ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("F8679F50-850A-41CF-9C72-430F290290C8")]
internal interface IPolicyConfig {
    int a(); int b(); int c(); int d(); int e(); int f(); int g(); int h(); int i(); int j();
    int SetDefaultEndpoint(string wszDeviceId, uint role);
}
public class AkimicPolicy {
    public static void SetDefaultDevice(string deviceId) {
        IPolicyConfig p = (IPolicyConfig)new _CPolicyConfigClient();
        p.SetDefaultEndpoint(deviceId, 0);
        p.SetDefaultEndpoint(deviceId, 1);
        p.SetDefaultEndpoint(deviceId, 2);
    }
}
"@
[AkimicPolicy]::SetDefaultDevice('{__DEVICE_ID__}')
"""


def _run_ps(code: str, timeout: int = 120) -> str | None:
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", code],
            capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return proc.stdout.strip() if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


class WindowsPlatform(BasePlatform):
    @property
    def use_system_browser(self) -> bool:
        return False

    @property
    def gui_backend(self) -> str:
        return "edgechromium"

    @property
    def driver_display_name(self) -> str:
        return "CABLE Output (VB-Audio Virtual Cable)"

    @property
    def driver_match_keyword(self) -> str:
        return "cable input"

    def get_webview_env(self) -> dict[str, str]:
        return {
            "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": (
                "--ignore-certificate-errors "
                "--use-fake-ui-for-media-stream "
                "--autoplay-policy=no-user-gesture-required"
            ),
        }

    # --- helpers ---------------------------------------------------------

    def is_admin(self) -> bool:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def _sound_devices(self) -> list[str]:
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_SoundDevice | Select-Object -ExpandProperty Name"],
                capture_output=True, text=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        except (OSError, subprocess.TimeoutExpired):
            return []

    def _get_default_playback_id(self) -> str:
        return _run_ps(_PS_GET_DEFAULT, timeout=30) or ""

    def _set_default_playback(self, device_id: str) -> None:
        if not device_id:
            return
        _run_ps(_PS_SET_DEFAULT.replace("{__DEVICE_ID__}", device_id), timeout=30)

    def _driver_zip_path(self) -> str:
        if getattr(sys, "frozen", False):
            base = sys._MEIPASS
        else:
            base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return os.path.join(base, "assets", "vbcable.zip")

    # --- BasePlatform ----------------------------------------------------

    # Registry traces the official VB-CABLE driver always leaves behind;
    # the device list can lag a fresh install or hide a disabled endpoint.
    _REGISTRY_TRACES = (
        r"SYSTEM\CurrentControlSet\Services\VBCABLE",
        r"SOFTWARE\VB-Audio\Cable",
        r"SOFTWARE\WOW6432Node\VB-Audio\Cable",
    )

    def _cable_in_sound_devices(self) -> bool:
        return any(name == "VB-Audio Virtual Cable" for name in self._sound_devices())

    def is_driver_installed(self) -> bool:
        # Exact match on the cable entry; "VB-Audio Voicemeeter VAIO" must not match.
        if self._cable_in_sound_devices():
            return True
        for path in self._REGISTRY_TRACES:
            try:
                winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0,
                               winreg.KEY_READ | winreg.KEY_WOW64_64KEY).Close()
                return True
            except OSError:
                continue
        return False

    def _wait_for_device(self, timeout_s: int = 20) -> bool:
        """Poll until the virtual device shows up after the installer exits."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self._cable_in_sound_devices():
                return True
            time.sleep(1)
        return self.is_driver_installed()

    def cleanup_rename_residue(self) -> tuple[bool, str]:
        """Restore any capture endpoint left renamed to "AkiMic Output".

        An earlier AkiMic build could rename the endpoint; after that feature
        was removed the registry value would survive with no way back from
        the UI, and app device pickers would show a name that no longer
        matches this app's hint. Boot-time self-heal: write the stock
        DeviceDesc back and drop the explicit FriendlyName so Windows
        resumes synthesizing it.
        """
        import winreg

        cleaned = 0
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _MMDEVICES, 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as root:
                for i in range(winreg.QueryInfoKey(root)[0]):
                    guid = winreg.EnumKey(root, i)
                    try:
                        with winreg.OpenKey(root, guid) as ek:
                            if winreg.QueryValueEx(ek, "DeviceState")[0] != 1:
                                continue
                            with winreg.OpenKey(ek, "Properties") as pk:
                                desc = str(winreg.QueryValueEx(pk, _PKEY_DESC)[0])
                                friendly = str(winreg.QueryValueEx(pk, _PKEY_FRIENDLY)[0])
                    except OSError:
                        continue
                    if _RENAME_RESIDUE not in desc.lower() and _RENAME_RESIDUE not in friendly.lower():
                        continue
                    try:
                        with winreg.OpenKey(
                            winreg.HKEY_LOCAL_MACHINE, f"{_MMDEVICES}\\{guid}\\Properties", 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY,
                        ) as pk:
                            winreg.SetValueEx(pk, _PKEY_DESC, 0, winreg.REG_SZ, _RESTORE_DESC)
                            try:
                                winreg.DeleteValue(pk, _PKEY_FRIENDLY)
                            except OSError:
                                pass
                        cleaned += 1
                    except OSError:
                        pass
            if cleaned:
                return True, f"restored {cleaned} renamed endpoint(s)"
            return True, "no residue"
        except OSError as e:
            return False, f"registry error: {e}"

    def install_driver(self) -> tuple[bool, str]:
        if self.is_driver_installed():
            return True, "already_installed"

        zip_path = self._driver_zip_path()
        if not os.path.exists(zip_path):
            return False, "driver package missing: " + zip_path

        old_default = self._get_default_playback_id()
        temp_dir = tempfile.mkdtemp(prefix="akimic_")
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(temp_dir)
            setup = os.path.join(temp_dir, _SETUP_EXE)
            if not os.path.exists(setup):
                return False, "VBCABLE_Setup_x64.exe not found in package"

            if self.is_admin():
                proc = subprocess.run(
                    [setup, "-i", "-h"], capture_output=True, timeout=300,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                ok = proc.returncode == 0
            else:
                # Elevate through PowerShell; the packaged exe has uac-admin,
                # this branch only covers bare dev runs.
                proc = subprocess.run(
                    ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
                     f'$p = Start-Process -FilePath "{setup}" -ArgumentList "-i -h" '
                     f"-Verb RunAs -Wait -WindowStyle Hidden -PassThru; exit $p.ExitCode"],
                    capture_output=True, timeout=300,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                ok = proc.returncode in (0, 1)

            if not ok:
                return False, f"installer exited with code {proc.returncode}"

            if not self._wait_for_device():
                return False, "installer finished but the device did not appear (try again or reboot)"

            if old_default:
                self._set_default_playback(old_default)
            return True, "installed"
        except Exception as e:
            return False, f"install error: {e}"
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def uninstall_driver(self) -> tuple[bool, str]:
        if not os.path.exists(_UNINSTALLER_PATH):
            return False, "uninstaller not found (already removed manually?)"
        try:
            if self.is_admin():
                proc = subprocess.run(
                    [_UNINSTALLER_PATH, "-u", "-h"], capture_output=True, timeout=300,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                code = proc.returncode
            else:
                proc = subprocess.run(
                    ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
                     f'$p = Start-Process -FilePath "{_UNINSTALLER_PATH}" -ArgumentList "-u -h" '
                     f"-Verb RunAs -Wait -WindowStyle Hidden -PassThru; exit $p.ExitCode"],
                    capture_output=True, timeout=300,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                code = proc.returncode

            if code in (0, 1, 2):
                return True, "uninstalled"
            return False, f"uninstaller exited with code {code}"
        except Exception as e:
            return False, f"uninstall error: {e}"

    def get_post_install_warning(self) -> str:
        return ""
