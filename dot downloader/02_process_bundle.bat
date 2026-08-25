@echo off
setlocal
cd /d "%~dp0"
echo ========================================
echo Process Bundle(s)
echo ========================================
if "%~1"=="" (
  echo Drag one or more .bundle files onto this BAT.
  pause
  exit /b 1
)
python run_pipeline.py %*
pause
