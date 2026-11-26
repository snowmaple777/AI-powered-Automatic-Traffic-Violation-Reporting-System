@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 手機與電腦連同一個 Wi-Fi，在 App 輸入下方顯示的網址。
if exist "..\.venv\Scripts\python.exe" (
  "..\.venv\Scripts\python.exe" -u mobile_host.py
) else (
  python -u mobile_host.py
)
pause
