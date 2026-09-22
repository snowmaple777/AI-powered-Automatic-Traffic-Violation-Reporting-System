@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please run the environment setup file first.
  exit /b 1
)
if "%~1"=="" (
  echo Usage: run_video.cmd "input\video.mp4" --save-video
  exit /b 1
)
".venv\Scripts\python.exe" run_pipeline.py %*
