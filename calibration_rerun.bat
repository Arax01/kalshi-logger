@echo off
REM Pre-registered clean re-test of the calibration study (see docs\preregistration-calibration-rerun.md).
REM Run it on or after January 15, 2027 (first look) and again on or after July 15, 2027 (final look).
REM Before the first look date it only shows how much fresh data has accumulated, never results.
REM Needs calibration.bat to have been run once first. Read-only; resumable; safe while the logger runs.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Please run setup.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m kalshi_logger calibration --rerun
if exist reports start "" reports
pause
