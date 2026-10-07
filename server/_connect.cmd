@echo off
REM Shared settings for the server .bat files (not meant to be double-clicked).
REM Sets SERVER_IP, SSH_OPTS and SCP_OPTS, or explains what is missing and returns an error.
set "KEY=%USERPROFILE%\.ssh\kalshi_server"
where ssh >nul 2>nul
if errorlevel 1 (
  echo Windows' built-in SSH client wasn't found. See SERVER.md, "If something goes wrong".
  exit /b 1
)
if not exist "%KEY%" (
  echo No server key on this laptop yet. Double-click make_server_key.bat first ^(SERVER.md, part 1^).
  exit /b 1
)
if not exist "%~dp0..\server_address.txt" (
  echo The server's address isn't saved yet. Double-click set_server_address.bat ^(SERVER.md, part 2^).
  exit /b 1
)
set "SERVER_IP="
set /p SERVER_IP=<"%~dp0..\server_address.txt"
if defined SERVER_IP set "SERVER_IP=%SERVER_IP: =%"
if not defined SERVER_IP (
  echo server_address.txt is empty. Double-click set_server_address.bat ^(SERVER.md, part 2^).
  exit /b 1
)
REM ~ is expanded by SSH itself, so this works even if the Windows user name contains spaces.
set SSH_OPTS=-i ~/.ssh/kalshi_server -o UserKnownHostsFile=~/.ssh/kalshi_known_hosts -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ConnectTimeout=20
set SCP_OPTS=%SSH_OPTS%
exit /b 0
