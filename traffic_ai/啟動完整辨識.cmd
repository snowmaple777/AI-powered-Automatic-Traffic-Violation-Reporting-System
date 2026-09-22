@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" exit /b 1
if "%~1"=="" (
  echo Usage: drag a video onto this file, or pass a video path.
  exit /b 1
)
".venv\Scripts\python.exe" run_pipeline.py --model-config configs/full.json %*
