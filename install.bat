@echo off
cd /d "%~dp0"
echo Installing dependencies...
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Install failed.
  pause
  exit /b 1
)
echo Done. Double-click run.vbs to start.
pause
