@echo off
rem Double-click to start all five services, or run: start.bat --check
rem cd to this file's folder so dev.py is always found.
cd /d "%~dp0"

set "PY=E:\python\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" dev.py %*
set "CODE=%ERRORLEVEL%"

echo.
echo [exit %CODE%] Press any key to close this window.
pause >nul
