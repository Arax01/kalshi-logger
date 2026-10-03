@echo off
REM Writes any reports that are due, plus a preview of the latest data, then opens the reports folder.
cd /d "%~dp0"
.venv\Scripts\python.exe -m kalshi_logger report
if exist reports start "" reports
pause
