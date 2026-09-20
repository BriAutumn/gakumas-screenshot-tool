@echo off
setlocal
cd /d "%~dp0"
python -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)"
if errorlevel 1 goto :failed
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto :failed
)
".venv\Scripts\python.exe" -m pip install -r requirements-recognition.txt
if errorlevel 1 goto :failed
echo Setup complete. Double-click launch.cmd to start.
pause
exit /b 0
:failed
echo Setup failed. Please check Python 3.12+ and your network connection.
pause
exit /b 1
