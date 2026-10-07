@echo off
REM Server step 4: connects the server's daily backup to your Backblaze B2 bucket, then runs the first
REM backup. Safe to run again (for example to change keys). See SERVER.md, part 4.
cd /d "%~dp0"
call server\_connect.cmd
if errorlevel 1 goto :fail
ssh -t %SSH_OPTS% kalshi@%SERVER_IP% "bash ~/kalshi-logger/server/setup_backups.sh"
if errorlevel 1 goto :fail
pause
exit /b 0
:fail
pause
exit /b 1
