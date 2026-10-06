@echo off
REM Resting-order study: replays Kalshi's public trade history to see whether a ~$100 resting order would
REM have filled and what it would have earned. Writes reports\resting_orders.txt. Places no orders.
REM Needs calibration.bat to have been run once first. Takes about an hour; resumable; safe while the logger runs.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Please run setup.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m kalshi_logger resting
if exist reports start "" reports
pause
