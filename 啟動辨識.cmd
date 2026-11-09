@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 合併原本的「啟動辨識.cmd」與「啟動完整辨識.cmd」
rem   啟動辨識.cmd "input\影片.mp4" [其他參數]         使用 configs/default.json
rem   啟動辨識.cmd full "input\影片.mp4" [其他參數]    使用 configs/full.json（原本的 啟動完整辨識）
rem   也可以直接把影片拖到這個檔案上
if exist ".venv\Scripts\python.exe" goto venv_ok
echo [!] 找不到 .venv，請先執行 安裝環境.cmd
pause
exit /b 1
:venv_ok
set "MODEL_CFG="
if /i "%~1"=="full" goto use_full
if /i "%~1"=="--full" goto use_full
goto check_args
:use_full
set "MODEL_CFG=--model-config configs/full.json"
shift
:check_args
if not "%~1"=="" goto collect
echo 用法：
echo   啟動辨識.cmd "input\影片.mp4" --save-video
echo   啟動辨識.cmd full "input\影片.mp4" --save-video     （使用 configs/full.json）
echo 也可以直接把影片拖到這個檔案上。
pause
exit /b 1
:collect
set "ARGS="
:loop
if "%~1"=="" goto run
set ARGS=%ARGS% "%~1"
shift
goto loop
:run
".venv\Scripts\python.exe" run_pipeline.py %MODEL_CFG% %ARGS%
