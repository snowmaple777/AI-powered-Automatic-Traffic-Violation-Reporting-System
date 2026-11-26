@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist ".venv\Scripts\python.exe" (
  echo Please run the environment setup file first.
  pause
  exit /b 1
)
rem 以保存的感知結果（outputs\eval\baseline_18）重播目前版本，輸出標註影片、違規片段、違規紀錄與動作備註。
rem 不需要 GPU；已完成的影片會略過。結果在 outputs\showcase_20261110，總覽為 index.html。
set OUT=outputs\showcase_20261110
if exist "%OUT%\short\cdy07\SKIPPED.txt" del "%OUT%\short\cdy07\SKIPPED.txt"
if exist "%OUT%\short\ctm06\SKIPPED.txt" del "%OUT%\short\ctm06\SKIPPED.txt"
".venv\Scripts\python.exe" tools\make_showcase.py short --out %OUT%
if errorlevel 1 goto failed
".venv\Scripts\python.exe" tools\make_showcase.py index --out %OUT%
if errorlevel 1 goto failed
echo.
echo 完成：%OUT%\index.html（影片需另行轉 H.264 時可裝 ffmpeg 後執行 tools\make_showcase.py encode）
pause
exit /b 0
:failed
echo 產生失敗，請回傳上方訊息。
pause
exit /b 1
