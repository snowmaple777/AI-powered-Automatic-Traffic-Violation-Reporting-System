@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist ".venv\Scripts\python.exe" (
  echo Please run the environment setup file first.
  pause
  exit /b 1
)
set LABEL=%~1
if "%LABEL%"=="" set /p LABEL=Stage label (e.g. stage0): 
".venv\Scripts\python.exe" tools\verify_stage.py --label %LABEL%
pause
