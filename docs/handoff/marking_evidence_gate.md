# 標線證據閘門與降頻介面交接（2026-11-01）

依 [保守標線排程交接](conservative_marking_schedule.md) 的「給 Opus 的 prompt」實作。共用約定見 [AGENTS.md](../../AGENTS.md)，
開發紀錄見 [refactor_log 4l](../refactor_log.md)。本文件同時是 Opus 的工作筆記與給 Astra 的評估交接。

## 1. 任務與狀態

- 使用者需求：依保守標線排程交接的需求更新專案、回報後續改進方案、保留工作筆記與給 Astra 的交接。
- 本次範圍：直接觀測／推估證據閘門（生產端、全部消費端、離線重播、schema 遷移）；運動失效時要求重新推論；
  1/2/4 幀降頻的實驗設定與評估工具。
- 不在本次範圍：真實影片評估（本機 VM 沒有 GPU／torch，`outputs/` 沒有基準）；改變 interval 預設；4k 活動升頻的評估。
- 負責角色：Opus（介面整合）→ 交接對象：Astra（真實影片評估）、使用者（Windows 驗證與是否提交）。
- 狀態：**已驗證、待整合提交**（2026-11-01 Windows：閘門在 31 支影片上不改變任何事件；結果見 refactor_log「4l／4m Windows 驗證結果」）。
- 基準：commit `4b88629`，分支 `feature/conservative-marking-schedule`，其上有 4k 的未提交修改。
- 交付：未提交、未推送。只含本次修改的差異：`outputs/patches/marking_evidence_gate.patch`（LF 換行，相對於 4k 狀態）。
  修改前的檔案快照：`outputs/patches/pre_evidence_gate/`。4k 的差異仍在 `outputs/patches/activity_boost.patch`。
- 他人既有修改：4k 的 `model_library/builtin.py`、`run_pipeline.py`、`tests/test_marking_interval.py`、文件修改全部保留；
  本次在其上擴充，沒有還原或重寫 4k 的邏輯（`activity_boost` 行為不變，只把統計欄位一般化）。

## 2. 變更與責任位置

| 檔案／函式 | 原問題 | 修改後行為 | 性質 |
| --- | --- | --- | --- |
| `observation_schema.py`：`SCHEMA_VERSION`、`marking_evidence()`、`marking_geometry_source()` | 只有推移幀有來源欄位，沒有讀取函式 | 1.8：每幀寫來源；讀取函式含舊紀錄退回 | 契約變更（向後相容） |
| `frame_processing.py`：`_propagate_markings`、`process` | 推移遮罩交給下游時與直接觀測無法區分；推移鏈中一步失效後仍被視為對齊 | 每幀寫 `direct`／`aligned`／`age_frames`／`motion_valid`；把 `direct_observation` 傳給補償層；重播的推移遮罩不再推移 | 功能變更（只增欄位） |
| `road_compensation/compensator.py`：`_Track`、`process`、`_annotate` | 推移遮罩的配對讓 `observations` 加一，可湊足 `rule_eligible` | 既有欄位不變；另計直接觀測與 `direct_rule_eligible` | 只增欄位 |
| `double_lines.py`：`attach_double_lines`、`RULE_DEFAULTS` | `observed_frames` 計入推移幀 | 另計 `direct_observed_frames`；線標記 `geometry_source`；新設定 `require_direct_marking_evidence` | 只增欄位＋新設定 |
| `area_rules.py`：`attach_area_geometry`、事件條件 | 推移遮罩的區域與直接觀測無法區分 | 區域標記 `marking_source`；事件條件 `direct_region_observation` | 只增欄位（規則本來只輸出疑似） |
| `violation_engine.py`：闖紅燈 | 確認可依靠推移累積的停止線 | `require_direct_marking_evidence`（預設 true）、條件 `stop_line_direct_evidence`、阻擋原因 `stop_line_not_directly_observed`、證據 `marking_evidence` | 行為變更（只降級確認） |
| `violation_engine.py`：雙線 | 同上 | 條件 `direct_marking_evidence`（設定可關閉） | 行為變更（只降級確認） |
| `model_library/base.py`：`FrameContext.motion_probe` | 模型無法得知本幀鏡頭運動 | 可選欄位，預設 None | 介面變更（向後相容） |
| `model_library/builtin.py`：`MarkingModel` | 運動失效時下游拿到未對齊的遮罩 | `refresh_on_invalid_motion`（預設 false）；`schedule_enabled`；統計加 `motion_wait_seconds` | 新功能（預設關閉） |
| `run_pipeline.py` | — | 提供 `motion_probe`（等待背景 future）；任何排程選項啟用時輸出 `road_marking_schedule` | 接線 |
| `tools/real_replay.py` | 推移遮罩在重播時被當成直接觀測 | 依記錄的 `road_marking_source` 傳入 `prealigned_source` | 修正 |
| `compensate_recording.py` | 從 JSONL 重算補償時把推移幀當成直接觀測 | 依 `marking_evidence` 傳 `direct_observation` | 修正 |
| `tools/batch_eval.py` | 固定使用 default.json；`done.json` 不能分辨設定 | `--model-config`；`done.json` 記錄設定指紋與程式版本，不同設定拒絕續跑 | 工具 |
| `tools/rule_eval.py` | — | `--no-direct-marking-gate` 對照 | 工具 |
| `configs/marking_interval_{1,2_refresh,4_refresh}.json` | — | 降頻對照設定（由 default.json 以 json 模組複製，只改標線參數） | 新增（實驗） |
| `configs/double_line_rules.json` | — | 明列 `require_direct_marking_evidence: true` | 設定 |
| `tests/test_marking_evidence.py`（新）、`tests/test_marking_interval.py` | — | 17＋2 項新測試，2 項改為 1.8 欄位 | 測試 |

- 新增物件／檔案：只新增測試檔與三個實驗設定。沒有新增類別；讀取函式放在既有的 `observation_schema.py`（契約所在）。
- 接線：模型 → `FrameProcessor`（來源）→ 補償／雙線／區域（直接觀測計數）→ 規則（確認條件、診斷）→ JSONL／事件 → `real_replay`／`rule_eval`。

## 3. 介面交接

| 欄位／函式 | 生產端 | 消費端 | 語意與失效條件 | 舊資料相容 |
| --- | --- | --- | --- | --- |
| `postprocessing.road_marking_source` | `FrameProcessor.process` | `marking_evidence()`（補償、雙線、區域、闖紅燈、雙線規則）、`real_replay` | 每幀（有遮罩時）：`source_frame`、`direct`、`propagated`、`warped`、`aligned`、`age_frames`、`motion_valid` | 沒有欄位＝直接觀測（`legacy: true`）；1.7 推移幀以 `warped` 推定 `aligned` |
| `compensation.direct_observed_frames`／`last_direct_observed_frame`／`direct_age_seconds`／`direct_rule_eligible` | `RoadMarkingCompensator` | 闖紅燈 `_stop_line_direct_evidence` | 只計直接觀測；資格公式同 `rule_eligible` 但用直接觀測的次數與年齡 | 欄位不存在時規則沿用 `rule_eligible` |
| `double_lines[].direct_observed_frames`／`geometry_source`／`marking_source_frame` | `attach_double_lines` | 雙線規則 `_direct_marking_evidence` | `geometry_source`：direct／propagated（推移且對齊）／stale（未對齊） | 次數退回 `observed_frames`；來源退回本幀 `marking_evidence` |
| `traffic_regions[].marking_source` | `attach_area_geometry` | 區域事件條件 | 同上標籤；`predicted` 區域另有既有旗標 | 退回 direct |
| 闖紅燈 `require_direct_marking_evidence` | 規則參數 | `rule_eval --no-direct-marking-gate` | 預設 true；false＝加入閘門前的行為（條件字典也不含新鍵） | — |
| 雙線 `require_direct_marking_evidence` | `configs/double_line_rules.json` | 規則 | 同上 | — |
| `FrameContext.motion_probe` | `run_pipeline`（背景鏡頭運動 future） | `MarkingModel._motion_reason` | 呼叫後阻塞至估計完成；沒有 future（車輛階段停用等）回傳 None | 預設 None；其他模型忽略 |
| `road_markings.refresh_on_invalid_motion` | 模型設定 | `MarkingModel.infer` | 只在快取幀詢問；無效或未知時重新推論 | 預設 false＝原行為 |
| `road_marking_schedule.motion_wait_seconds`、`mode` | `MarkingModel` | `*_timings.json` | `mode` 為啟用的排程選項以 `+` 連接 | 舊設定不寫此欄位 |

- schema 版本：1.7 → 1.8（每幀寫來源、新增直接觀測欄位）。舊 JSONL 仍可讀；`validate_observation` 不變。
- 快取／推移資料是否增加證據：不增加「確認」所需的直接觀測；既有計數（`observations`、`observed_frames`、`stable_frames`）語意不變，仍支持追蹤與疑似。
- suspected／confirmed：只可能把確認降為疑似。雙線事件 ID 與幀不變；闖紅燈不成立時依原機制在 finalize 輸出最強疑似快照（時間可能不同）。
- 線上與離線入口：`run_pipeline`、`real_replay`（已修正）、`rule_eval`（讀 JSONL 欄位）一致；排程（refresh、activity_boost）只在線上執行，重播不重跑。
- 需要使用者／Opus 決定：interval 預設、refresh 是否啟用、是否提交 4k＋4l 到 feature 分支（尚未授權）。

## 4. 可重現環境

- 本次開發：Linux VM，Python 3.10.12、numpy 2.2.6、OpenCV 5.0.0，無 torch、無 GPU；只能跑單元測試與假模型流程。
- Windows（AGENTS.md 第 3 節）：`.venv`，RTX 5060 Laptop。各後端實際 provider 本次未查。
- 權重：未新增、未更換；標線權重 SHA-256 沿用設定檔 `75ccd13c…96b36`。
- 輸入：`input/` 有影片；`outputs/` 沒有基準 JSONL／遮罩（2026-10-28 清空）。

```powershell
$env:PYTHONIOENCODING='utf-8'
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_marking_*.py"
.venv\Scripts\python.exe -m unittest discover -s tests
```

## 5. 驗證與結果

- 單元測試（VM）：133 項，132 通過、1 略過（沒有既有輸出）。修改前同一環境為 114 項通過（1 略過）。
- 合成影片整合（實際 `run_pipeline`、背景鏡頭運動、補償、全部規則；12 幀，第 6–7 幀全黑使運動失效）：
  - interval 2：推移幀 `direct=false`；運動失效時的推移幀 `aligned=false`。停止線 `rule_eligible` 第 2 幀成立，`direct_rule_eligible` 第 4 幀成立。
  - interval 4＋refresh：推論 5 次（固定 3＋`motion_invalid` 2），`motion_wait_seconds` 0.141（VM CPU，不代表 Windows）。12 幀內未達直接資格（interval 4 約需 9 幀連續可見）。
  - `real_replay` 重播 interval 2 輸出：12 幀逐幀相同。
- 真實影片（Windows，2026-11-01；對照組＝關閉閘門 `baseline_18_no_gate`，實驗組＝預設 `baseline_18`，同一份 JSONL）：

| 指標 | 對照組（無閘門） | 實驗組（閘門） | 分母／資料範圍／限制 |
| --- | --- | --- | --- |
| 事件 TP／FP／FN | 紅燈 9/4/5、停等區 5/5/2、雙黃線 1/0/7、斑馬線 0/2/5 | 相同 | 31 支 reviewed，34 個標註事件 |
| 確認 → 疑似的事件數 | — | 0 | 31 支影片事件逐筆相同 |
| 影片命中 | 紅燈 8/13、停等區 4/6、雙黃線 1/7、斑馬線 1/5 | 相同 | 不等於事件準確率 |
| FPS | — | ms01 1.21、cdy01 3.31、soc03 2.21 | ms01 含約 32 秒冷啟動；穩態模型耗時與 4i 相同 |

  interval 1／4 與 `refresh_on_invalid_motion` 的實驗尚未執行（第 9 節步驟 3）。

## 6. 整合決策與回退

- 決策：閘門預設啟用（只會把確認降為疑似，符合 AGENTS.md 第 6 節原則 1、2；Windows 驗證在 interval 2 下不改變任何事件）；`refresh_on_invalid_motion` 與 1/2/4 設定維持實驗，預設 interval 仍為 2。
- 回退：
  - 只關閉閘門：雙線設定 `require_direct_marking_evidence: false`；闖紅燈以規則選項 `require_direct_marking_evidence=False`（`rule_eval --no-direct-marking-gate`）。
  - 整體回退：以 `outputs/patches/pre_evidence_gate/` 的快照覆蓋對應檔案，刪除新增的測試檔與三個實驗設定。沒有權重或資料遷移；1.8 JSONL 對舊程式只是多出欄位。
- 未解決問題見第 8 節。已同步文件：`observation_schema.py`、`AGENTS.md`（5.1、5.3、7.3、第 8 節）、`docs/usage.md`、`使用說明.txt`、`docs/refactor_log.md` 4l、本文件、`conservative_marking_schedule.md` 回填。
- 推送／合併：未授權，停留本機未提交。

## 7. 工作筆記（設計取捨）

- **平行計數而非改寫既有計數**：補償的 `observations`、雙線的 `observed_frames`、規則的 `stable_frames` 都保持原語意，另加直接觀測的計數。
  理由：既有計數同時決定追蹤延續與疑似路徑，改寫會讓疑似事件與追蹤一起改變，影響面難以評估。平行計數讓變更只落在確認條件，interval 1 時完全等同舊行為（測試有覆蓋）。
- **捨棄的做法**：
  - 推移幀在補償層「完全不算觀測」：未確認的新線在推移幀會被當成遺失而刪除，疑似與追蹤都會變。
  - 每個越線樣本都必須是直接觀測：interval ≥ 2 時幾乎不可能確認，等於禁止降頻。現行要求是前、後各至少一個直接樣本，且不得有未對齊樣本。
  - 以「前一幀」的鏡頭運動決定是否重新推論（不必等待）：會晚一幀才發現失效。先以正確性為準，等 Windows 量到 `motion_wait_seconds` 再決定是否改。
- **`aligned` 的定義**：推移鏈任一步失效，直到下一次推論前都不對齊（interval ≥ 3 時，失效之後即使下一步推移有效也一樣）。這比 1.7 只看當步的 `warped` 嚴格。
- **未啟用補償時的闖紅燈門檻**：直接觀測幀數門檻沿用 `min_line_stable_frames`（5）。interval 2 時約需 9 幀，可能偏嚴，評估時留意。
- **已知缺口**：
  - 闖紅燈只在確認當幀檢查停止線的 `direct_rule_eligible`，不追溯線前階段每一幀；補償層的資格本身已含多幀直接觀測與年齡衰減，暫視為足夠。
  - 舊紀錄的雙線線帶內快照沒有 `geometry_source`，退回 direct。
  - 區域的 `observed_frame` 在推移幀仍是當幀（語意未改），判斷來源請看 `marking_source`。
  - 推移鏈每一步都對「上一幀處理後」的遮罩做最近鄰重取樣；interval 4 時誤差會累積（見第 8 節第 4 項）。

## 8. 後續改進方案（建議順序）

1. **Windows 建立 1.8 基準並量測閘門影響**（Astra）：`batch_eval --label baseline_18`，再以同一份 JSONL 跑 `rule_eval --no-direct-marking-gate`。逐片列出確認 → 疑似的事件並看畫面。
2. **效能回歸**（使用者／Astra）：`verify_stage --label stage4l`，確認預設設定的速度與 4i 相同（refresh 預設關閉，預期只多寫欄位）。
3. **降頻 1/2/4 實驗**（Astra）：以三個實驗設定各跑一個 label；比較事件狀態與時間、推論次數、`motion_wait_seconds`、最長觀測空窗。
4. **推移改由最近一次推論的遮罩一次推移**（Opus）：累乘單應矩陣，從原始遮罩直接推移到當幀，避免逐幀最近鄰重取樣的誤差累積。interval ≥ 3 前應先做。
5. **推移幀的標線紀錄改為幾何投影**（Opus）：目前從遮罩重建紀錄每幀 9–38 ms（4i、4j），改為投影上次的元件幾何可省下大半，但需逐像素比對驗證。
6. **refresh 的等待成本**（Opus）：若 `motion_wait_seconds` 明顯，改為讓鏡頭運動更早開始（例如以前一幀車框建立道路遮罩），或在等待期間先跑其他模型。
7. **4k 活動升頻的評估**（Astra）：與上述實驗共用同一套 label／指紋流程。
8. **標註**：2026-11-01 已補齊 31 支（34 個事件，refactor_log 4m）。時間是約略區間；評估時若發現系統抓到參考沒列的真實違規，回報使用者補標，不自行修改答案。

## 9. 給 Astra 的 prompt：證據閘門與動態取樣評估

請先讀 AGENTS.md、docs/ASTRA.md、本交接（docs/handoff/marking_evidence_gate.md）與 docs/handoff/conservative_marking_schedule.md，
並以 `git status --short` 與 `outputs/patches/marking_evidence_gate.patch` 核對工作目錄確實包含 4k＋4l 的未提交修改。
前置證據閘門已由 Opus 完成（schema 1.8），本次只做評估與報告，不修改共用介面、不改預設值、不推送或合併。

1. 環境：記錄 HEAD、dirty 狀態、torch／onnxruntime／Paddle 的實際 provider 與裝置、權重 SHA-256（`模型清單.txt`、設定檔）。
   單張 GPU：所有辨識依序執行，量速度時不跑其他 GPU 工作。先跑全套單元測試並記錄數量。
2. 基準與閘門影響（`驗證標線閘門.cmd` 依序執行 verify_stage、batch_eval、rule_eval；標註已於 4m 補齊，31 支皆 reviewed）：
   ```powershell
   .venv\Scripts\python.exe tools\batch_eval.py --label baseline_18
   .venv\Scripts\python.exe tools\rule_eval.py --source outputs\eval\baseline_18 --label baseline_18_no_gate --no-direct-marking-gate
   ```
   逐片列出兩者不同的事件（影片、秒數、車輛／車牌、確認 ↔ 疑似、`missing_conditions`），並看事件畫面判斷降級是否合理。
   分規則報 TP／FP／FN（只計 reviewed 影片）與影片層級命中。
3. 降頻與重新推論（各用獨立 label，`done.json` 會擋下混用）：
   ```powershell
   .venv\Scripts\python.exe tools\batch_eval.py --label marking_i1 --model-config configs\marking_interval_1.json
   .venv\Scripts\python.exe tools\batch_eval.py --label marking_i2_refresh --model-config configs\marking_interval_2_refresh.json
   .venv\Scripts\python.exe tools\batch_eval.py --label marking_i4_refresh --model-config configs\marking_interval_4_refresh.json
   ```
   以 `baseline_18`（interval 2）為對照，逐片比較新增／消失事件、確認 ↔ 疑似、事件時間偏移；
   從 `*_timings.json` 彙整總 wall time、標線 ms／幀、`road_marking_schedule`（推論次數、原因、`motion_wait_seconds`）、`ego_motion_wait`；
   從 JSONL 的 `road_marking_source` 統計 `age_frames` 分布、`aligned=false` 的幀數與最長連續推移空窗。
4. 情境檢查：遠處小車、彎道雙線、靜止占用斑馬線、曝光突變、鏡頭運動失效（cdy05、cdy07）、燈號切換、短片事件；
   若有長時間無事件的長影片一併測，沒有就明列限制。4k 交接提到的「影片疑似違規檢視.txt」在本機的檔名是
   `input/違規參考.txt`（影片疑似違規初步檢視，2026-10-28）：只用來定位候選，不是完整真值；未 reviewed 的影片不可當完整負例。
5. 速度：`驗證階段.cmd stage4l`（`tools\verify_stage.py --label stage4l`，預設設定）。`outputs/` 已清空、沒有前次報告，
   請與 refactor_log 4i 的數字（ms01 1.62、cdy01 2.78、soc03 1.81 FPS）對照，並注意環境基準是否相近。
6. 報告：填寫 docs/handoff/TEMPLATE.md 格式的交接（建議 `docs/handoff/marking_interval_eval.md`），附實際數字與變差的案例。
   只有在已標註集合與長片上都驗證後才建議預設值；測試集沒有新增漏報不代表所有影片沒有漏報。
   需要介面變更（例如新欄位、改變閘門條件）時寫成「介面變更需求」交給 Opus，不要直接修改共用契約。

## 10. 同一輪後續整合（4m–4s，2026-11-02）

給接手者的現況摘要；細節與數字見 refactor_log 對應各節。

| 節 | 內容 | 狀態 |
| --- | --- | --- |
| 4m | `input/違規參考.txt` 轉入 `eval/ground_truth.json`（31 支 reviewed）；之後補 ctm01 闖紅燈，共 35 事件；soc04 以 `excluded` 暫時排除 | 採用 |
| 4n | 停等區：同車 5 秒內只報一次、換號車框 IoU ≥ 0.5 視為同車；停等區事件保留到車離開畫面或闖紅燈事件出現再合併 | 採用 |
| 4o | 闖紅燈疑似事件：越線前後 0.5 秒內有綠燈即不輸出；越線後退回線前取消待定事件 | 採用 |
| 4p | 斑馬線漏報分析（車身遮住斑馬線、移動攝影機下停留判定漂移） | 分析 |
| 4q | 斑馬線：遮擋延伸、依車寬放寬停留半徑、暫不看機車（使用者政策）、信心短暫下降不歸零 | 採用 |
| 4r | 斑馬線代理線改為逐車備援（每幀產生、與停止線分開追蹤） | 採用 |
| 4s | ms07：被遮住的停止線記憶、未合格停止線備援 | 未採用（瓶頸是鏡頭運動失效） |

目前（`rules_4r`，以 baseline_18 重算）：闖紅燈 TP 11／FP 3／FN 4（影片 9/13）、停等區 5／1／2、雙黃線 1／0／7、斑馬線 1／3／3（soc04 排除）、車牌完全正確 6/15。
Windows 已驗證到 stage4q（4r 為規則層，以 `rule_eval` 量化）。

建議 Astra 接續：
- 大型車占滿畫面時的鏡頭運動失效（ms07、cdy05、cdy07）：影響停止線補償與雙線，屬後處理。
- 車牌辨識：ms06、s03、s05 的違規車讀不到車牌（使用者推測），以及 mm01／ms04／s02／s04 的 OCR 誤讀。
- 斑馬線遠處小車誤報（ms03）與更準確的接地／停止判斷。
- 第 9 節的標線降頻實驗仍未執行。
