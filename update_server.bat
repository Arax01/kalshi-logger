@echo off
REM After you merge a pull request on GitHub: brings the server up to date. The new version is tested
REM on the server first; if its tests fail, the server keeps running the old version.
cd /d "%~dp0"
call server\_connect.cmd
if errorlevel 1 goto :done
ssh %SSH_OPTS% kalshi@%SERVER_IP% "bash ~/kalshi-logger/server/update.sh && echo. && bash ~/kalshi-logger/server/check.sh"
:done
pause
