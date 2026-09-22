@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please follow PARTNER_README.md to install the environment first.
  pause
  exit /b 1
)
if "%~1"=="" (
  echo Drag a video onto this file, or pass a video path.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" tools\verify_models.py
if errorlevel 1 (
  pause
  exit /b 1
)
".venv\Scripts\python.exe" run_pipeline.py %* --rule all --save-video
pause
