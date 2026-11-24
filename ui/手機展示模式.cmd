@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 手機展示模式：手機連同一個 Wi-Fi，打開下面顯示的網址
echo 第一次執行若跳出 Windows 防火牆，請按「允許存取」
python server.py --lan
pause
