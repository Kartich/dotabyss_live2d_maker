@echo off
setlocal
cd /d "%~dp0"
echo ========================================
echo Catalog download by ID
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
pause
