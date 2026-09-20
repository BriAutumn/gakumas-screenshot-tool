@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -X utf8 simulate.py web
if errorlevel 1 pause
