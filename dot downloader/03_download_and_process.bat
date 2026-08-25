@echo off
setlocal
cd /d "%~dp0"
echo ========================================
echo Download + Process by ID
echo ========================================
echo CDN version example:
echo https://api.abyss-prod-r18.dotabyss.dmmgames.com/resources/webgl/r18/aas/8374/aa/
echo Input only the version field, e.g. 8374
set /p VERSION=Enter CDN version: 
if "%VERSION%"=="" (
  echo Empty version.
  pause
  exit /b 1
)
set /p ID=Enter character/story/resource ID: 
if "%ID%"=="" (
  echo Empty ID.
  pause
  exit /b 1
)
python catalog_downloader.py "%ID%" --version "%VERSION%"
set DOWNLOAD_CODE=%ERRORLEVEL%
if not "%DOWNLOAD_CODE%"=="0" (
  echo Some downloads failed. Continuing with all successfully downloaded bundles...
)
python run_pipeline.py --request-id "%ID%" --version "%VERSION%"
set PROCESS_CODE=%ERRORLEVEL%
if not "%PROCESS_CODE%"=="0" (
  echo Processing completed with some errors.
)
set FINAL_CODE=%PROCESS_CODE%
if not "%DOWNLOAD_CODE%"=="0" set FINAL_CODE=%DOWNLOAD_CODE%
echo.
echo ========================================
echo Done. Exit code: %FINAL_CODE%
echo ========================================
pause
exit /b %FINAL_CODE%
