@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Verify.ps1"
exit /b %errorlevel%
