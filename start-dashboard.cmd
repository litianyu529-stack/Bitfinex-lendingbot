@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-dashboard.ps1"
if not "%ERRORLEVEL%"=="0" pause
exit /b %ERRORLEVEL%
