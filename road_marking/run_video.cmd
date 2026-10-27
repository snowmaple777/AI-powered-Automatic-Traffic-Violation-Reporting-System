@echo off
if "%~1"=="" (
  echo Usage: run_video.cmd "C:\videos\your_video.mp4"
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Run-Video.ps1" -Video "%~1"
exit /b %errorlevel%
