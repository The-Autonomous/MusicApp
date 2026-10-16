@echo off
setlocal
cd /d "%~dp0"
if not exist "dist\MusicApp\MusicApp.exe" (
  echo Run build.bat first.
  pause
  exit /b 1
)
start "Connect MusicApp" "dist\MusicApp\MusicApp.exe" --connect-browser-profile %*
