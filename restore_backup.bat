@echo off
REM Restores the server's database from a Backblaze backup. It lists the backups and asks before changing
REM anything. See SERVER.md, "Backups and restoring".
cd /d "%~dp0"
call server\_connect.cmd
if errorlevel 1 goto :done
ssh -t %SSH_OPTS% kalshi@%SERVER_IP% "bash ~/kalshi-logger/server/restore.sh"
:done
pause
