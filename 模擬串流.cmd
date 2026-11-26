@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 測試用：把影片當成即時串流播出（HTTP MJPEG），再到網頁「即時串流」貼上顯示的網址
rem   模擬串流.cmd "input\影片.mp4" [--loop] [--port 8790]
rem   也可以直接把影片拖到這個檔案上
if exist ".venv\Scripts\python.exe" goto venv_ok
echo [!] 找不到 .venv，請先執行 安裝環境.cmd
pause
exit /b 1
:venv_ok
if not "%~1"=="" goto run
echo 用法：模擬串流.cmd "input\影片.mp4" [--loop]
echo 也可以直接把影片拖到這個檔案上。
pause
exit /b 1
:run
".venv\Scripts\python.exe" tools\simulate_stream.py %*
pause
