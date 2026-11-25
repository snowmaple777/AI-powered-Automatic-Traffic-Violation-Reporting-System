@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 用法：直接雙擊 = clips 底下每個帳號都換；把某個帳號資料夾拖到這個檔案上 = 只換那個帳號
echo 更新根憑證套件 certifi...
python -m pip install -q -U certifi
echo.
if not "%~1"=="" goto one
for /d %%D in (clips\*) do call :run "%%D"
goto done
:one
call :run "%~1"
goto done
:run
echo ==== %~nx1 ====
python tools\make_road_gpx.py "%~1"
echo.
exit /b
:done
pause
