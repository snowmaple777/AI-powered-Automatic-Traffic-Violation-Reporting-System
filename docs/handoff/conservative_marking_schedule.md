# 保守標線排程交接（2026-11-01）

## 範圍與狀態

使用者要求先實作動態取樣最低風險部分，其他 agent 工作留下筆記與 prompt。
基準 4b8862975b420f8ce973d0bed4bd4c8190a3cc0b；分支 feature/conservative-marking-schedule。
本機未提交、未推送、未合併。完整差異：outputs/patches/activity_boost.patch（從專案根目錄套用）。
既有 AGENTS.md、CLAUDE.md、README.md 修改及 docs/ASTRA.md、handoff/TEMPLATE.md 保留，未納入差異。

本版只增加高風險提示下的直接觀測，沒有延長原本 interval，不能宣稱加速。
任何車輛或燈號（含快取資料）只用來觸發升頻，不代表判定違規，也不替代規則的證據檢查。
第一版沒有依車線距離、當幀鏡頭運動、規則狀態或畫面差分決策；空偵測也不宣稱低風險。

## 交付檔案

- model_library/builtin.py：MarkingModel 可選 activity_boost、activity_hold_seconds；保留固定推論幀，額外升頻；影片時間保持、跳幀／尺寸改變刷新、reset。
- run_pipeline.py：只有啟用時，在既有 *_timings.json 輸出 road_marking_schedule。
- configs/activity_boost.json：從現行 default 複製的獨立實驗設定，只新增上述兩個標線參數。
- tests/test_marking_interval.py：新增 7 項反例及模型→FrameProcessor 測試；既有假模型改用建構函式，避免遺漏初始化狀態。
- docs/usage.md、使用說明.txt：相同的啟用、限制、輸出與回退說明。
- docs/refactor_log.md：4k 本次紀錄；本交接文件。

## 契約與行為

不改 FrameContext、infer 簽名、觀測 schema 1.7、規則、source_frame 語意。
MarkingModel 新建構參數 activity_boost=false；activity_hold_seconds=1.0（非負有限數字）。
活動出現當幀立即推論，停止活動後在保持時間內每幀推論；之後回到原 index % interval。
保留固定推論幀是刻意的：額外刷新不延後下一個基準觀測，未採用可延長空窗的 last-infer 排程。
索引不連續、時間倒退／相同／間隔大於 0.2 秒或尺寸變更：啟用時清快取並重新觀測。
新可選模型屬性 schedule_diagnostics 由 MarkingModel 生產、run_pipeline 消費；每次 reset 清空。
耗時 JSON 可選新增 road_marking_schedule：mode、baseline_interval、hold_seconds、frames、inferences、extra_inferences、reasons。
extra_inferences 計算已有快取且非固定推論幀上的活動升頻，不含首次及不連續重建。
舊模型／停用狀態不寫新欄位。觀測重播不依賴此欄位，也不會重跑此排程；模型實驗須重新推論。
直接觀測變多仍可能改變追蹤幾何、suspected/confirmed、事件時間，不能宣稱啟用後行為等價。
既有推估標線可支持確認的問題仍存在，本版沒有修復。

## 本次驗證

```powershell
$env:PYTHONIOENCODING='utf-8'
.venv\Scripts\python.exe -m unittest discover -s tests -p test_marking_interval.py
.venv\Scripts\python.exe -m unittest discover -s tests
```

相關測試 10 項通過；全套 114 項，113 通過、1 略過（沒有既有輸出）。耗時 23.471 秒是測試時間，不是推論效能。
設定經 load_config 驗證路徑存在。使用本機既有 .venv，單元測試以假標線模型驗證，不載入 GPU 權重。
實際 torch/ONNX/Paddle provider 未測；真實影片 TP/FP/FN、車牌正確率、FPS、逐片事件變化皆未執行。
未新增權重，SHA256 沿用設定；未修改人工標註。本次沒有完整的前後準確率數字，因此維持預設關閉。

實驗命令（先檢查輸出目錄不存在，避免覆寫；單張 GPU 須依序執行）：
```powershell
.venv\Scripts\python.exe run_pipeline.py input/ms01.mp4 --model-config configs/default.json --output-dir outputs/schedule_baseline/ms01 --rules all --no-violation-clips
.venv\Scripts\python.exe run_pipeline.py input/ms01.mp4 --model-config configs/activity_boost.json --output-dir outputs/schedule_boost/ms01 --rules all --no-violation-clips
```
擴充至全部影片時使用相同輸入集合；不要把 batch_eval 的 done.json 當跨設定有效的完成指紋。
回退：改回 configs/default.json，或 activity_boost=false。無新增權重或資料遷移。

## 給 Opus 的 prompt：證據與介面整合

請先讀 AGENTS.md、CLAUDE.md、本交接及現行 diff。保留其他人的未提交變更。
在目前的保守標線排程基礎上，設計並實作「直接觀測／推估」證據閘門，再考慮真正降頻。
檢查 FrameProcessor._propagate_markings → RoadMarkingCompensator → double_lines / area_rules → violation_engine 全鏈路。
將 observation frame 與 geometry 對齊 frame 分開，保留原始 source_frame、資料年齡及運動有效性；不能把推移遮罩重建成當幀直接觀測。
先列出生產端、全部消費端（包括 tools/real_replay.py）、舊 JSONL 退回策略及 schema 版本遷移。
快取／推估不得增加確認條件所需的直接觀測次數；資料不足時不得偷偷放寬 confirmed。
讓當幀運動失效或場景切換能要求重新推論；評估等待背景 motion future 對並行效能的影響，不能讀未完成的結果。
保持實驗預設關閉，加入推估-only 不得確認、運動失效、間隔切換、短暫越線、跳幀與跨片 reset 測試。
介面整合完成後才建立 1/2/4 幀的低風險降頻實驗；不用事件 ground truth 或未來影格做即時排程決策。
跑相關與全套測試，重建相同輸入／設定基準並比較事件狀態及時間。交付實際數字和已知副作用，不自行推送或合併。

## 給 Astra／評估 agent 的 prompt：動態取樣驗證

請先讀 AGENTS.md、docs/ASTRA.md、本交接及 Opus 最新介面交接，確認前置證據閘門已完成。
先核對權重與各後端實際 provider、影片及輸出是否存在；單張 GPU 依序跑實驗。
對照 default、activity_boost 及後續低頻設定，固定影片、權重、解析度與規則參數，輸出各自獨立目錄。
包含現有 31 支影片和具有長時間無事件路段的長影片；若本機缺長影片，明列限制。
以影片疑似違規檢視.txt 定位候選但不可當完整真值；未 reviewed 的影片不可當完整負例。
逐片核對新增／消失事件與 suspected/confirmed 變化；分規則報 TP/FP/FN、事件時間偏移、車牌正確數。
報總 wall time、各模型推論次數、升降頻原因、資料年齡、最長觀測空窗與回看成本。
加入遠處小車、轉彎雙線、靜止占用斑馬線、曝光突變、鏡頭運動失效、燈號切換及短片事件。
只有在已標註測試集與長片上驗證後才能建議預設值；不能宣稱測試集零新增漏報等於所有影片零漏報。
如果前置閘門尚未完成，只評估現有保守升頻版本，不擅自延長標線間隔。

補充整合驗證：4 幀合成 MP4、假標線模型、實際 run_pipeline 解碼與輸出；關閉時模型呼叫 2 次且無新統計欄位，啟用時呼叫 4 次且統計 frames=4、inferences=4。皆成功寫出 JSONL/CSV/timings。此為流程驗證，非 GPU 或準確率測試。
