@echo off
REM Server step 3: sets up the server (security, the logger service, backups timer). Takes 5-10 minutes.
REM Safe to run again. See SERVER.md, part 3.
cd /d "%~dp0"
call server\_connect.cmd
if errorlevel 1 goto :fail
echo Copying the setup script to the server at %SERVER_IP% ...
scp %SCP_OPTS% server\setup.sh root@%SERVER_IP%:/root/kalshi-setup.sh
if errorlevel 1 goto :noconnect
ssh -t %SSH_OPTS% root@%SERVER_IP% "sed -i 's/\r$//' /root/kalshi-setup.sh && bash /root/kalshi-setup.sh"
if errorlevel 1 (
  echo.
  echo Setup stopped before the end. Read the last messages above; it is safe to run setup_server.bat again.
  goto :fail
)
pause
exit /b 0
:noconnect
echo.
echo Couldn't connect to the server. Check the address in server_address.txt and that the server is on.
:fail
pause
exit /b 1
