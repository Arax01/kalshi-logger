@echo off
REM Shows how the logger on the server is doing: running or not, what it collected, gaps, disk, backups.
cd /d "%~dp0"
call server\_connect.cmd
if errorlevel 1 goto :done
ssh %SSH_OPTS% kalshi@%SERVER_IP% "bash ~/kalshi-logger/server/check.sh"
:done
pause
