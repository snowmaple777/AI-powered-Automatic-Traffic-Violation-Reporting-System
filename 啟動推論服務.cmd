@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 推論服務：給網頁 UI 用的 AI 辨識＋自動填表（autoForm）
if exist ".venv\Scripts\python.exe" goto venv_ok
echo [!] 找不到 .venv，請先執行 安裝環境.cmd
pause
exit /b 1
:venv_ok
where ffmpeg >nul 2>nul
if not errorlevel 1 goto ffmpeg_ok
".venv\Scripts\python.exe" -c "import imageio_ffmpeg" >nul 2>nul
if not errorlevel 1 goto ffmpeg_ok
echo 安裝 imageio-ffmpeg（把違規片段轉成瀏覽器能播的 H.264，只需要一次）...
".venv\Scripts\python.exe" -m pip install imageio-ffmpeg
:ffmpeg_ok
".venv\Scripts\python.exe" -c "import playwright" >nul 2>nul
if not errorlevel 1 goto pw_ok
echo 安裝自動填表需要的 playwright（只需要一次）...
".venv\Scripts\python.exe" -m pip install -r requirements-reporting.txt
:pw_ok
".venv\Scripts\python.exe" -c "import os,sys;from playwright.sync_api import sync_playwright as s;p=s().start();ok=os.path.exists(p.chromium.executable_path);p.stop();sys.exit(0 if ok else 1)" >nul 2>nul
if not errorlevel 1 goto start
echo 下載自動填表用的 Chromium 瀏覽器（約 150 MB，只需要一次）...
".venv\Scripts\python.exe" -m playwright install chromium
:start
".venv\Scripts\python.exe" -m inference_service.server %*
pause
