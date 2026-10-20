@echo off
rem Double-click: starts the OpenMusic workbench and opens it in the browser, or opens the one already running.
rem To stop it, close this window or press Ctrl+C in it.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo No .venv here: install first ^(README, Installation^).
  pause
  exit /b 1
)
".venv\Scripts\python.exe" experiments\app.py --reuse
if errorlevel 1 pause
