@echo off
REM Asks the logger to finish what it is doing and exit cleanly.
cd /d "%~dp0"
.venv\Scripts\python.exe -m kalshi_logger stop
timeout /t 8 >nul
