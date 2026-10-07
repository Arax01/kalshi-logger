@echo off
REM Server step 5: moves this laptop's database to the server, once. Run it only after stop.bat.
REM It checks the upload against the laptop's copy before using it, and won't overwrite anything on
REM the server. Afterwards the laptop logger won't start again. See SERVER.md, part 5.
cd /d "%~dp0"
call server\_connect.cmd
if errorlevel 1 goto :fail
if not exist .venv\Scripts\python.exe (
  echo Please run setup.bat first.
  goto :fail
)
echo Step 1 of 4: checking the laptop logger is stopped, and making a checked copy of the database...
.venv\Scripts\python.exe -m kalshi_logger export-for-server
if errorlevel 1 goto :fail
> MOVED_TO_SERVER echo The database was copied to the server on %DATE% %TIME%. The laptop logger won't start while this file exists.
echo.
echo Step 2 of 4: uploading to the server (a few minutes per GB)...
ssh %SSH_OPTS% kalshi@%SERVER_IP% "mkdir -p ~/kalshi-logger/data/incoming"
if errorlevel 1 goto :upload_failed
scp -C %SCP_OPTS% data\kalshi_upload.db data\kalshi_upload.json kalshi@%SERVER_IP%:kalshi-logger/data/incoming/
if errorlevel 1 goto :upload_failed
echo.
echo Step 3 of 4: checking the upload on the server and putting it in place...
ssh %SSH_OPTS% kalshi@%SERVER_IP% "cd ~/kalshi-logger && .venv/bin/python -m kalshi_logger import-from-laptop && sudo systemctl start kalshi-logger"
if errorlevel 1 (
  echo.
  echo The server did not accept the upload; the messages above say why. Your laptop database is untouched.
  echo Don't start the laptop logger; tell Claude what the messages say.
  goto :fail
)
del data\kalshi_upload.db data\kalshi_upload.json
echo.
echo Step 4 of 4: checking the server's logger is running...
timeout /t 20 >nul
ssh %SSH_OPTS% kalshi@%SERVER_IP% "bash ~/kalshi-logger/server/check.sh"
echo.
echo All done. The logger now runs on the server. The laptop's database stays here as an extra copy.
echo Remove the start.bat shortcut from your Startup folder (SERVER.md, part 5).
pause
exit /b 0
:upload_failed
echo.
echo The upload didn't finish. Nothing on the server was changed. Double-click move_to_server.bat again.
echo Don't start the laptop logger in the meantime.
:fail
pause
exit /b 1
