@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please run setup.cmd first.
  pause
  exit /b 1
)
set "GKSHOT_CAPTURE_ROOT=%~dp0"
powershell.exe -NoProfile -Command "$taskPython = Join-Path $env:GKSHOT_CAPTURE_ROOT '.venv\Scripts\pythonw.exe'; try { Start-Process -FilePath $taskPython -ArgumentList 'run.py capture-gui' -WorkingDirectory $env:GKSHOT_CAPTURE_ROOT -Verb RunAs -WindowStyle Normal -ErrorAction Stop } catch { Write-Host $_.Exception.Message; exit 1 }"
if errorlevel 1 pause
