@echo off
setlocal EnableExtensions
title Forward Scan
cd /d "%~dp0"

rem  Usage:  run.bat              menu
rem          run.bat live [port]  start with real OMSGuru orders   (default port 8000)
rem          run.bat demo [port]  start with sample orders          (default port 8001)
rem          run.bat rebuild      rebuild the web app after code changes
rem          run.bat test         run the automated tests
rem  Set FS_NO_BROWSER=1 to stop it opening the browser automatically.

set "MODE=%~1"
set "PORT=%~2"
set "PY=.venv\Scripts\python.exe"

rem ---------------- Python environment (first run only) ----------------
if exist "%PY%" goto :venv_ok
echo [setup] Creating the Python environment - first run only...
set "PYBOOT="
where python >nul 2>nul
if not errorlevel 1 set "PYBOOT=python"
if defined PYBOOT goto :have_py
where py >nul 2>nul
if not errorlevel 1 set "PYBOOT=py -3"
:have_py
if not defined PYBOOT goto :no_python
%PYBOOT% -m venv .venv
if errorlevel 1 goto :fail
:venv_ok
"%PY%" -c "import fastapi, sqlalchemy, httpx, truststore, jwt, bcrypt, openpyxl, dotenv, uvicorn" >nul 2>nul
if not errorlevel 1 goto :deps_ok
echo [setup] Installing Python packages...
"%PY%" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto :fail
:deps_ok

if /i "%MODE%"=="test" goto :test
if /i "%MODE%"=="rebuild" goto :rebuild

rem ---------------- Web app (built once) ----------------
if exist "frontend\dist\index.html" goto :web_ok
call :build_web
if errorlevel 1 goto :fail
:web_ok

if /i "%MODE%"=="live" goto :live
if /i "%MODE%"=="demo" goto :demo

:menu
cls
echo.
echo   ==================================================
echo      FORWARD SCAN  -  OMSGuru dispatch scanning
echo   ==================================================
echo.
echo      1.  Start LIVE    - real OMSGuru orders
echo      2.  Start DEMO    - sample orders, nothing sent to OMSGuru
echo      3.  Rebuild web app  - after code changes
echo      4.  Run tests
echo      5.  Exit
echo.
choice /c 12345 /n /m "   Choose 1-5: "
if errorlevel 5 goto :eof
if errorlevel 4 goto :test
if errorlevel 3 goto :rebuild
if errorlevel 2 goto :demo
goto :live

rem ---------------- start ----------------
:live
if not exist ".env" goto :no_env
if "%PORT%"=="" set "PORT=8000"
set "RUNARGS=--port %PORT%"
set "LABEL=LIVE"
goto :start

:demo
if "%PORT%"=="" set "PORT=8001"
set "RUNARGS=--demo --port %PORT%"
set "LABEL=DEMO"
goto :start

:start
netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul
if not errorlevel 1 goto :port_busy
cls
echo.
echo   Forward Scan %LABEL% is starting...
echo.
echo   On this PC:        http://localhost:%PORT%
for /f "tokens=2 delims=:" %%A in ('ipconfig ^| findstr /c:"IPv4"') do for /f "tokens=*" %%B in ("%%A") do echo   Other PCs/phones:  http://%%B:%PORT%
echo.
echo   Keep this window open while scanning. Press Ctrl+C to stop the server.
echo   First time only: if Windows Firewall asks, click "Allow" so other devices can connect.
echo.
if not "%FS_NO_BROWSER%"=="1" start "" /min powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 4; Start-Process 'http://localhost:%PORT%'"
"%PY%" backend\run.py %RUNARGS%
echo.
echo   Server stopped.
pause
goto :eof

rem ---------------- tools ----------------
:rebuild
call :build_web
if errorlevel 1 goto :fail
echo [ok] Web app rebuilt.
if defined MODE goto :eof
pause
goto :menu

:test
pushd backend
"..\%PY%" -m pytest -q -p no:warnings
popd
if defined MODE goto :eof
pause
goto :menu

:build_web
where npm >nul 2>nul
if errorlevel 1 (
  echo [error] Node.js is needed to build the web app. Install the LTS version from https://nodejs.org and run this again.
  exit /b 1
)
echo [setup] Building the web app...
pushd frontend
if not exist node_modules (
  call npm install --no-fund --no-audit
  if errorlevel 1 ( popd & exit /b 1 )
)
call npm run build
if errorlevel 1 ( popd & exit /b 1 )
popd
exit /b 0

rem ---------------- errors ----------------
:no_python
echo [error] Python 3.11 or newer is not installed.
echo         Download it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
goto :fail

:no_env
echo [error] The .env file is missing next to run.bat.
echo         Copy .env.example to .env and fill in OMSGURU_API_TOKEN, OMSGURU_CLIENT_ID, APP_SECRET_KEY and ADMIN_PASSWORD.
goto :fail

:port_busy
echo [error] Port %PORT% is already in use - Forward Scan is probably already running.
echo         Open http://localhost:%PORT% in the browser, or start on another port:  run.bat %MODE% 9000
goto :fail

:fail
echo.
echo   Something went wrong - see the message above.
pause
exit /b 1
