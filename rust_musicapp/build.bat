@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Build.ps1" %*
if errorlevel 1 (
  echo Build failed. See the message above.
  exit /b 1
)
echo Ready: dist\MusicApp\MusicApp.exe
