@echo off
REM Server step 2: saves the server's IP address (from DigitalOcean) so the other server .bat files can
REM find it. Run again if the address ever changes.
cd /d "%~dp0"
set "IP="
set /p IP=Paste the server's IP address (for example 203.0.113.25) and press Enter: 
if defined IP set "IP=%IP: =%"
if not defined IP (
  echo Nothing was entered; nothing saved.
  pause
  exit /b 1
)
> server_address.txt echo %IP%
echo Saved: %IP%
pause
