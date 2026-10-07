@echo off
REM Server step 1: creates the key this laptop uses to log in to the server, and copies its public
REM half to the clipboard so you can paste it into DigitalOcean. Run once. See SERVER.md, part 1.
cd /d "%~dp0"
where ssh-keygen >nul 2>nul
if errorlevel 1 (
  echo Windows' built-in SSH client wasn't found. See SERVER.md, "If something goes wrong".
  pause
  exit /b 1
)
if not exist "%USERPROFILE%\.ssh" mkdir "%USERPROFILE%\.ssh"
if exist "%USERPROFILE%\.ssh\kalshi_server" (
  echo You already have a server key; using it.
) else (
  ssh-keygen -q -t ed25519 -N "" -C "kalshi-logger laptop key" -f "%USERPROFILE%\.ssh\kalshi_server"
  if errorlevel 1 (
    echo Creating the key failed.
    pause
    exit /b 1
  )
  echo Created your server key.
)
type "%USERPROFILE%\.ssh\kalshi_server.pub" | clip
echo.
echo The PUBLIC half of your key is now on the clipboard (it's safe to paste into DigitalOcean):
echo.
type "%USERPROFILE%\.ssh\kalshi_server.pub"
echo.
echo The private half stays in %USERPROFILE%\.ssh\kalshi_server. Never share or upload that file.
pause
