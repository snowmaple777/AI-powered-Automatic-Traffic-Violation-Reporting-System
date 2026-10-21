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
if "%LABEL%"=="" set LABEL=baseline
".venv\Scripts\python.exe" tools\batch_eval.py --label %LABEL%
pause
