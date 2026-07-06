@echo off
setlocal

cd /d "%~dp0"
set "APP_URL=http://127.0.0.1:8000/lendingbot.html"

powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if "%ERRORLEVEL%"=="0" (
    start "" "%APP_URL%"
    exit /b 0
)

echo Starting Bitfinex Lending Bot dashboard...
echo URL: %APP_URL%
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process '%APP_URL%'"

where py >nul 2>nul
if "%ERRORLEVEL%"=="0" (
    py -3 lendingbot.py --dashboard
) else (
    python lendingbot.py --dashboard
)

if not "%ERRORLEVEL%"=="0" (
    echo.
    echo Startup failed. Please confirm Python is installed and python --version works.
    pause
)
