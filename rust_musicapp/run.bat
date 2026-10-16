@echo off
setlocal
cd /d "%~dp0"
if not exist "dist\MusicApp\MusicApp.exe" (
  call build.bat
  if errorlevel 1 exit /b 1
)
start "MusicApp Rust" "dist\MusicApp\MusicApp.exe" %*
