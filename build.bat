@echo off
cd /d "%~dp0"
echo Installing build dependencies...
python -m pip install -q -r requirements.txt pyinstaller
if errorlevel 1 (
  echo Dependency install failed.
  pause
  exit /b 1
)

echo Building CpuMemOverlay.exe ...
python -m PyInstaller --noconfirm --clean --onefile --windowed --name CpuMemOverlay main.py
if errorlevel 1 (
  echo Build failed.
  pause
  exit /b 1
)

echo.
echo Done: dist\CpuMemOverlay.exe
echo Tip: re-enable "开机启动" in the tray/menu so it points to the exe.
pause
