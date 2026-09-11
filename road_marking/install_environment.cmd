@echo off
setlocal
cd /d "%~dp0"
echo RLMD environment setup - requires Internet and several GB of free space.
echo This creates a local .venv. It does not use another person's environment.
set "RLMD_PYTHON="
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" install_environment.py --check-only >nul 2>&1
  if not errorlevel 1 set "RLMD_PYTHON=%~dp0.venv\Scripts\python.exe"
)
if defined RLMD_PYTHON goto found
py -3.10 -c "import struct; assert struct.calcsize('P') == 8" >nul 2>&1
if not errorlevel 1 goto launcher
set "RLMD_PYTHON=%LocalAppData%\Programs\Python\Python310\python.exe"
if exist "%RLMD_PYTHON%" (
  "%RLMD_PYTHON%" install_environment.py --check-only >nul 2>&1
  if not errorlevel 1 goto found
)
set "RLMD_PYTHON=%ProgramFiles%\Python310\python.exe"
if exist "%RLMD_PYTHON%" (
  "%RLMD_PYTHON%" install_environment.py --check-only >nul 2>&1
  if not errorlevel 1 goto found
)
echo.
echo STOP: no working Python 3.10 64-bit was found.
echo Install Python 3.10 x64 with the Python launcher, then run this file again.
echo Official download - choose Windows installer 64-bit:
echo https://www.python.org/downloads/release/python-31011/
echo Do not run the video command until setup prints INSTALLATION COMPLETE.
pause
exit /b 2
:launcher
py -3.10 install_environment.py
goto result
:found
"%RLMD_PYTHON%" install_environment.py
:result
set "RLMD_RESULT=%ERRORLEVEL%"
if not "%RLMD_RESULT%"=="0" echo Setup failed. See install_logs and README.md.
pause
exit /b %RLMD_RESULT%
