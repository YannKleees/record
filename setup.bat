@echo off
rem One-time setup: Python packages, ffmpeg and a desktop shortcut.
setlocal
cd /d "%~dp0"

set "PY=py -3.11"
%PY% --version >nul 2>nul || set "PY=py -3"
%PY% --version >nul 2>nul || (
  echo Python is missing. Install Python 3.11 from python.org, then run setup.bat again.
  goto end
)

echo [1/3] Installing Python packages...
if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt || (
  echo Installing packages failed. Check your internet connection and try again.
  goto end
)

echo [2/3] Checking ffmpeg...
".venv\Scripts\python.exe" -c "from record.engine import find_ffmpeg; raise SystemExit(not find_ffmpeg())" 2>nul || (
  winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
)

echo [3/3] Creating desktop shortcut...
powershell -NoProfile -Command "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop') + '\Record.lnk'); $s.TargetPath = '%CD%\.venv\Scripts\pythonw.exe'; $s.Arguments = '-m record'; $s.WorkingDirectory = '%CD%'; $s.IconLocation = '%CD%\record\ui\icon.ico'; $s.Save()"

echo.
echo Done. Open the app with start.bat or the "Record" shortcut on your desktop.

:end
if not "%~1"=="--quiet" pause
