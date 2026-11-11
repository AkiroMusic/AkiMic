"""Virtual audio device management on Windows via VB-Audio Virtual Cable.

Audio path reminder: the desktop browser plays the WebRTC stream into the
"CABLE Input" playback device; VB-CABLE loops it to the "CABLE Output"
recording device, which apps pick as their microphone.

The default playback device is saved before install and restored after,
because the VB-CABLE installer likes to promote itself to default speaker.
"""

import ctypes
import json
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
# The VB-CABLE author's certificate CN (Vincent Burel / VB-Audio), compared
# EXACTLY via X.500 SimpleName — thumbprints rotate on cert renewal, so the
# CN is the stable pin. A mismatch must stop the elevated install.
_EXPECTED_SIGNER_CN = "BUREL VINCENT Entrepreneur individuel"

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


def _safe_extract(zf: zipfile.ZipFile, dest: str) -> None:
    """Extract entry by entry, refusing anything that escapes dest
    (absolute paths, .. traversal) and capping the blast radius of a
    swapped-in zip bomb. No blind extractall() on a zip we execute
    binaries from."""
    dest_real = os.path.realpath(dest)
    total_size = 0
    entries = zf.infolist()
    if len(entries) > 200:
        raise ValueError("zip has too many entries")
    for info in entries:
        if info.file_size > 64 * 1024 * 1024:
            raise ValueError(f"zip entry too large: {info.filename!r}")
        total_size += info.file_size
        if total_size > 256 * 1024 * 1024:
            raise ValueError("zip uncompressed total too large")
        name = info.filename
        parts = name.replace("\\", "/").split("/")
        if name.startswith(("/", "\\")) or ".." in parts or (len(parts) > 1 and parts[0].endswith(":")):
            raise ValueError(f"unsafe zip entry: {name!r}")
        target = os.path.realpath(os.path.join(dest, name))
        if target != dest_real and not target.startswith(dest_real + os.sep):
            raise ValueError(f"unsafe zip entry: {name!r}")
        zf.extract(info, dest)


def _authenticode_check(path: str) -> tuple[bool, str]:
    """Fail-closed signature + publisher verification.

    Returns (ok, message). Only a Valid signature by the expected VB-Audio
    CN passes; NotSigned, NotTrusted, HashMismatch, UnknownError, empty
    output and PowerShell failures all block the elevated install. The file
    path travels through an environment variable — never interpolated into
    the PowerShell command text — so a crafted TEMP path cannot inject
    commands into this elevated process.
    """
    try:
        env = {**os.environ, "AKIMIC_SIGN_PATH": path}
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "$s = Get-AuthenticodeSignature -LiteralPath $env:AKIMIC_SIGN_PATH; "
             "if ($null -eq $s) { "
             "Write-Output '{\"status\":null,\"cn\":null}' } "
             "else { "
             "$cn = $null; "
             "if ($null -ne $s.SignerCertificate) { "
             "$cn = $s.SignerCertificate.GetNameInfo("
             "[System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false) }; "
             "@{ status = [string]$s.Status; cn = $cn } | ConvertTo-Json -Compress }"],
            capture_output=True, text=True, timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW,
            env=env,
        )
        if proc.returncode != 0:
            return False, f"signature check failed (powershell exit {proc.returncode})"
        try:
            payload = json.loads(proc.stdout.strip() or "{}")
            status = payload.get("status")
            cn = payload.get("cn") or ""
        except (json.JSONDecodeError, AttributeError):
            return False, "signature check output unparseable — install aborted"
        if status != "Valid":
            return False, f"driver binary signature status: {status} — install aborted"
        if cn.lower() != _EXPECTED_SIGNER_CN.lower():
            return False, f"driver binary signed by unexpected publisher: {cn or '(none)'}"
        return True, f"signed by {cn}"
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"signature check failed: {e}"


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

    def _set_default_playback(self, device_id: str) -> bool:
        if not device_id:
            return False
        return _run_ps(_PS_SET_DEFAULT.replace("{__DEVICE_ID__}", device_id), timeout=30) is not None

    def _driver_zip_path(self) -> str:
        if getattr(sys, "frozen", False):
            base = sys._MEIPASS
        else:
            base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return os.path.join(base, "assets", "vbcable.zip")

    # --- BasePlatform ----------------------------------------------------

    def _cable_in_sound_devices(self) -> bool:
        return any(name == "VB-Audio Virtual Cable" for name in self._sound_devices())

    def is_driver_installed(self) -> bool:
        """True only when the real audio device is enumerable. Registry traces
        alone mean the package is present but the endpoint may be missing or
        disabled — that state must not pass as "installed"."""
        return self._cable_in_sound_devices()

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
        ok, msg = False, ""
        try:
            ok, msg = self._run_installer(zip_path)
        finally:
            # the vendor installer may promote itself to default speaker even
            # when it fails — restore the previous device on every outcome
            if old_default and not self._set_default_playback(old_default):
                msg = (msg or "") + " (warning: could not restore the previous default speaker)"
        return ok, msg

    def _run_installer(self, zip_path: str) -> tuple[bool, str]:
        temp_dir = tempfile.mkdtemp(prefix="akimic_")
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                _safe_extract(zf, temp_dir)
            setup = os.path.join(temp_dir, _SETUP_EXE)
            if not os.path.exists(setup):
                return False, "VBCABLE_Setup_x64.exe not found in package"

            # supply-chain check on the binary we are about to run elevated:
            # fail-closed on anything but a Valid signature by VB-Audio
            sig_ok, sig_msg = _authenticode_check(setup)
            if not sig_ok:
                return False, sig_msg
            note = f" ({sig_msg})"

            if self.is_admin():
                proc = subprocess.run(
                    [setup, "-i", "-h"], capture_output=True, timeout=300,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                ok = proc.returncode == 0
            else:
                # Elevate through PowerShell; the packaged exe has uac-admin,
                # this branch only covers bare dev runs. The path travels via
                # an env var — never interpolated into the command text.
                proc = subprocess.run(
                    ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
                     '$p = Start-Process -FilePath $env:AKIMIC_INSTALLER_PATH '
                     '-ArgumentList "-i -h" -Verb RunAs -Wait -WindowStyle Hidden '
                     "-PassThru; exit $p.ExitCode"],
                    capture_output=True, timeout=300,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    env={**os.environ, "AKIMIC_INSTALLER_PATH": setup},
                )
                ok = proc.returncode in (0, 1)

            if not ok:
                return False, f"installer exited with code {proc.returncode}"

            if not self._wait_for_device():
                return False, "installer finished but the device did not appear (try again or reboot)"
            return True, "installed" + note
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
