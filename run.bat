@echo off
rem Dev launcher for the desktop app (Windows).
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  py -3 -m venv .venv || python -m venv .venv
  if errorlevel 1 exit /b 1
)
.venv\Scripts\python.exe -m pip install -q -r requirements.txt
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe app_main.py %*
