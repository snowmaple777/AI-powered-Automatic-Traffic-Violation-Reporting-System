@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" python -m venv --system-site-packages .venv
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip show onnxruntime >nul 2>&1
if not errorlevel 1 (
  ".venv\Scripts\python.exe" -m pip uninstall -y onnxruntime
  if errorlevel 1 exit /b 1
  ".venv\Scripts\python.exe" -m pip install --force-reinstall --no-deps onnxruntime-gpu==1.23.2
  if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" -m pip install -r requirements-perception.txt
if errorlevel 1 exit /b 1
echo Environment ready.

