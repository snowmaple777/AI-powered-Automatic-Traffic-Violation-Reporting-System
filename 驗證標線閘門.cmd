@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist ".venv\Scripts\python.exe" (
  echo Please run the environment setup file first.
  pause
  exit /b 1
)
rem 標線證據閘門（refactor_log 4l）的 Windows 驗證：
rem 1. 單元測試＋三支短片速度（verify_stage，label stage4l）
rem 2. 以現行預設設定跑 31 支評估影片（label baseline_18，約 1–1.5 小時，可中斷後重跑續跑）
rem 3. 同一份 JSONL 關閉閘門重算規則（label baseline_18_no_gate，約 1 分鐘），量測閘門本身的影響
rem 跑步驟 2 時請勿同時執行其他 GPU 工作。
".venv\Scripts\python.exe" tools\verify_stage.py --label stage4l
if errorlevel 1 (
  echo verify_stage failed; stop here.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" tools\batch_eval.py --label baseline_18
if errorlevel 1 (
  echo batch_eval failed; stop here.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" tools\rule_eval.py --source outputs\eval\baseline_18 --label baseline_18_no_gate --no-direct-marking-gate
echo.
echo 請回傳：outputs\verify\stage4l\summary.txt、outputs\eval\baseline_18\summary.txt、outputs\eval\baseline_18_no_gate\summary.txt
pause
