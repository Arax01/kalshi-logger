@echo off
REM Starts the logger in its own minimized window. Use stop.bat to stop it.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Please run setup.bat first.
  pause
  exit /b 1
)
if exist MOVED_TO_SERVER (
  echo The logger now runs on the server, so it won't start on this laptop. Use server_status.bat.
  echo ^(To log on this laptop again anyway, delete the file MOVED_TO_SERVER first - never run both at once.^)
  pause
  exit /b 1
)
start "Kalshi Logger - use stop.bat to stop" /min .venv\Scripts\python.exe -m kalshi_logger run
echo The logger is starting in a minimized window called "Kalshi Logger".
echo You can close this window. Use stop.bat to stop logging, status.bat to check on it.
timeout /t 8 >nul
