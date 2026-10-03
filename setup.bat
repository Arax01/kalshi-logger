@echo off
REM One-time setup: creates a private Python environment in this folder and installs what is needed.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")
echo Creating Python environment...
%PY% -m venv .venv
if errorlevel 1 (
  echo.
  echo Could not create the environment. Is Python installed? See README.md, step 1.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
if errorlevel 1 (
  echo Installing packages failed. Check your internet connection and run setup.bat again.
  pause
  exit /b 1
)
echo.
echo Setup complete. Double-click start.bat to start logging.
pause
