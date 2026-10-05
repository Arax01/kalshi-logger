@echo off
REM Samples about 3 million historical Kalshi trades (read-only) and writes reports\calibration_study.txt.
REM Takes about 1.5-2 hours the first time and about 250 MB of disk. You can close it and run it again
REM later: it continues where it stopped. Safe to run while the logger is running.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Please run setup.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m kalshi_logger calibration
if exist reports start "" reports
pause
