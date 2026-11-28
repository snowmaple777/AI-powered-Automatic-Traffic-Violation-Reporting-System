@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 啟動馬路三寶檢舉輔助系統介面...
rem 有專題的 .venv 就用它（裡面有 opencv、onnxruntime，畫面浮水印讀取才能用）；沒有才用系統的 python
if exist "..\.venv\Scripts\python.exe" (
  "..\.venv\Scripts\python.exe" server.py %*
) else (
  python server.py %*
)
if errorlevel 1 (
  echo.
  echo 啟動失敗：請確認已安裝 Python，且 opencv-python 可用（pip install opencv-python）
)
pause
