@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist ".venv\Scripts\python.exe" (
  echo Please run the environment setup file first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" tools\profile_models.py %*
".venv\Scripts\python.exe" tools\onnx_bench.py
pause
