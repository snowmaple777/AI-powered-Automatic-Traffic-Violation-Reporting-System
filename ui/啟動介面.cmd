@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 啟動馬路三寶檢舉輔助系統介面...
python server.py %*
if errorlevel 1 (
  echo.
  echo 啟動失敗：請確認已安裝 Python，且 opencv-python 可用（pip install opencv-python）
)
pause
