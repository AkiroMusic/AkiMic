@echo off
REM AkiMic build (Windows). Requires: py -3.13 available on PATH.
REM The venv is created automatically on first run; refresh deps by deleting .venv.

if not exist ".venv\Scripts\python.exe" (
    echo [AkiMic] creating Python 3.13 venv...
    py -3.13 -m venv .venv || exit /b 1
    .venv\Scripts\python -m pip install --upgrade pip || exit /b 1
    .venv\Scripts\pip install -r requirements.txt -r requirements-windows.txt pyinstaller || exit /b 1
)

.venv\Scripts\pyinstaller --noconfirm akimic.spec || exit /b 1

if not exist "release" mkdir release
copy /y "dist\AkiMic.exe" "release\AkiMic.exe" || exit /b 1

REM keep the tree clean: PyInstaller work dirs are disposable
rmdir /s /q build
rmdir /s /q dist

echo.
echo [AkiMic] build complete: release\AkiMic.exe
echo [AkiMic] run it as administrator (the manifest requests it automatically).
