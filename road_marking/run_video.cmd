@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto missing
if not exist "environment_ready.json" goto missing
".venv\Scripts\python.exe" launch_video.py "%~1"
set "RLMD_RESULT=%ERRORLEVEL%"
pause
exit /b %RLMD_RESULT%
:missing
echo Local environment is missing or not verified.
echo First double-click install_environment.cmd and wait for INSTALLATION COMPLETE.
echo Then use this file to choose a video, or drag a video onto it.
pause
exit /b 2
