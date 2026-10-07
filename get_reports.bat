@echo off
REM Downloads the latest reports from the server into this laptop's reports folder and opens it.
cd /d "%~dp0"
call server\_connect.cmd
if errorlevel 1 goto :fail
if not exist reports mkdir reports
echo Downloading reports...
scp -r %SCP_OPTS% "kalshi@%SERVER_IP%:kalshi-logger/reports/*" reports\
if errorlevel 1 goto :fail
start "" reports
exit /b 0
:fail
pause
exit /b 1
