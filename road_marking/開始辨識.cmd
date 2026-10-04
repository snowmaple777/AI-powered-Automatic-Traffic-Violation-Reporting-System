@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run install_environment.cmd first.
  pause
  exit /b 2
)
".venv\Scripts\python.exe" launch_video.py %*
set "RLMD_RESULT=%ERRORLEVEL%"
pause
exit /b %RLMD_RESULT%
