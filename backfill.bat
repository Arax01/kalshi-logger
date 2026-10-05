@echo off
REM One-time download of 2026 NFL and college football games from Kalshi (read-only), then writes
REM reports\football_overreaction.txt. Safe to run while the logger is running; safe to re-run
REM (finished games are skipped, newly finished ones are added). Takes about 20-30 minutes the first time.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Please run setup.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m kalshi_logger backfill
if exist reports start "" reports
pause
