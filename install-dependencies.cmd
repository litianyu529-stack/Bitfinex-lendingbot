@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py -m pip install -r requirements.txt
) else (
    python -m pip install -r requirements.txt
)

if errorlevel 1 (
    echo Dependency installation failed.
    exit /b 1
)

echo Dependencies installed.
