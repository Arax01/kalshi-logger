@echo off
REM Starts the logger in its own minimized window. Use stop.bat to stop it.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Please run setup.bat first.
  pause
  exit /b 1
)
start "Kalshi Logger - use stop.bat to stop" /min .venv\Scripts\python.exe -m kalshi_logger run
echo The logger is starting in a minimized window called "Kalshi Logger".
echo You can close this window. Use stop.bat to stop logging, status.bat to check on it.
timeout /t 8 >nul
