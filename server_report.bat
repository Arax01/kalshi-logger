@echo off
REM Writes any due reports on the server (plus a preview of the latest data), then downloads them all
REM into this laptop's reports folder and opens it.
cd /d "%~dp0"
call server\_connect.cmd
if errorlevel 1 goto :fail
echo Writing reports on the server...
ssh %SSH_OPTS% kalshi@%SERVER_IP% "cd ~/kalshi-logger && .venv/bin/python -m kalshi_logger report"
if errorlevel 1 goto :fail
call :download
if errorlevel 1 goto :fail
exit /b 0
:download
if not exist reports mkdir reports
echo Downloading reports...
scp -r %SCP_OPTS% "kalshi@%SERVER_IP%:kalshi-logger/reports/*" reports\
if errorlevel 1 exit /b 1
start "" reports
exit /b 0
:fail
pause
exit /b 1
