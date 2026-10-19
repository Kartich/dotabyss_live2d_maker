@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul 2>nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0launcher.ps1"
set "ERR=%ERRORLEVEL%"
if not "%ERR%"=="0" (
  echo.
  echo Launcher exited with code %ERR%.
  if exist "%~dp0launcher_error.log" echo See: "%~dp0launcher_error.log"
  pause
)
exit /b %ERR%
