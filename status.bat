@echo off
cd /d "%~dp0"
.venv\Scripts\python.exe -m kalshi_logger status
echo.
pause
