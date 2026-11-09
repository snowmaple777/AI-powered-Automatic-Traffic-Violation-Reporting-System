# AI Agent 先備須知（Traffic AI）

給參與本專案開發的 AI agent 閱讀。開始任何工作前請讀完本文件；細節以程式碼與
[docs/refactor_log.md](docs/refactor_log.md)（每次修改的原因、驗證方式與數字）為準。
最後核對：2026-10-28；本次閱讀基準為 commit `4b88629`，實際狀態以工作目錄為準。

閱讀順序：本文件 → 自己的角色指引（[Astra](docs/ASTRA.md)／[Opus](CLAUDE.md)）→ 任務相關原始碼與測試。
交接使用 [共用範本](docs/handoff/TEMPLATE.md)。角色名稱依使用者指定，並非對模型產品版本或能力的驗證。

### 每次開始工作的核對

1. 讀取使用者最新要求，確認是實作、分析還是文件工作；不要把舊待辦當成本次授權。
2. 檢查 `git status --short`、目前分支與 HEAD；保留其他人已修改但未提交的檔案，不自行還原。
3. 讀取目標模組、直接呼叫端、設定與相關測試；本文件描述與程式不同時，記錄差異再處理，不照舊聊天內容覆蓋新架構。
4. 確認本機是否有權重、輸入影片、基準 JSONL 與遮罩。歷史報告的路徑不保證仍存在。
5. 在交接中寫明任務範圍、基準版本、預期行為改變及驗證方式。只有缺少必需資訊時才向使用者確認。

使用者當次明確指示優先於本文的預設分工；分工不是要求 agent 為一般可逆工作反覆申請許可。

---

## 1. 專案一句話

行車紀錄器影片 → 感知模型（車輛、紅綠燈、道路標線、車距、車牌、車牌文字）→ 逐幀觀測紀錄（JSONL）
→ 違規規則（闖紅燈、跨越雙黃／雙白線、汽車侵入機車停等區、停留斑馬線）→ 違規候選事件、剪輯、
（選用）自動填表。**所有事件都是待人工覆核的候選**：`suspected`（疑似）或 `confirmed`（符合設定條件），
沒有任何事件是最終判決。

---

## 2. 角色分工

| | GPT-6 Astra | Claude Opus 5.5 |
| --- | --- | --- |
| 主責 | 新功能開發與驗證 | 架構與接口整合 |
| 典型工作 | 新規則、新模型後端、規則門檻與後處理改良、評估實驗、測試 | 資料契約（觀測紀錄、模型與規則介面）、主流程、設定預設值、效能、合併到 master、整體回歸驗證 |
| 工作位置 | `feature/<功能名>` 分支或隔離 worktree | 整合分支；驗證後依授權合併至 master |
| 交付物 | 程式＋測試＋評估數字＋交接文件（第 9 節） | 介面調整、預設值切換、`refactor_log.md` 正式紀錄、合併 |

### 邊界規則
- **Astra 不直接修改共用介面**（第 5 節列出的契約）。需要新欄位或新介面時，先在交接文件提出「介面變更需求」，
  由 Opus 設計後落地。允許的例外：在既有介面下新增可選欄位，且不影響既有讀取端（仍需在交接文件中列出）。
- **新功能預設關閉**：以設定參數開關（例如 `configs/*.json` 的模型參數、規則 options），預設值維持現行行為。
  切換預設值屬於整合決策，由 Opus 依評估結果決定。
- **Opus 不重寫 Astra 已驗證的功能邏輯**；整合時若需要改動行為，必須重新評估並在紀錄中說明差異。
- 任何一方都**不得在沒有評估數字的情況下宣稱「改善」**。
- 優先擴充既有模組與共用函式。新增類別／檔案須有清楚的責任邊界；不要另建平行主流程、臨時補丁鏈或複製整套幾何工具。
- 功能開發者應交付可執行的最小垂直流程（資料產生 → 規則 → 診斷／輸出 → 測試），不要只交未接線的框架並聲稱功能完成。
- 介面修改先記錄生產端、所有消費端、舊資料相容策略及整合次序；在兩個 agent 的交接文件中確認。這不是一律要求使用者批准小型介面新增。

---

## 3. 環境

- Windows 11；專案路徑含空白與中文（`C:\Users\user\Downloads\新增資料夾 (3)\traffic_ai`），命令一律加引號。
- 歷史環境紀錄：Python 3.10.11，虛擬環境 `.venv\`（`.venv\Scripts\python.exe`）。主要套件：torch 2.11（CUDA 12.8）、
  ultralytics 8.4.146、mmcv 2.1.0、mmengine 0.10.7、onnxruntime-gpu 1.23.2、opencv 4.10、numpy 1.26.4、paddlepaddle 3.3.1（OCR，CPU）。
- MMSegmentation **不是 pip 套件**，原始碼在 `third_party/mmseg/`，由 `model_library/detectors.py` 加入 `sys.path`。不要修改 `third_party/`；
  需要改模型行為時在 `model_library/` 以替換方法的方式處理（範例：`enable_fast_attention`）。
- GPU：RTX 5060 Laptop（單張）。**效能量測時不要同時跑其他 GPU 工作**，否則數字失真。
- Windows 主控台可能是 cp950：PowerShell 可設定 `$env:PYTHONIOENCODING='utf-8'` 與 `$env:PYTHONUTF8='1'`；檔案讀寫仍須明確指定編碼。
- 套件版本與裝置需以本機實查為準；`torch.cuda.is_available()` 不代表 ONNX 或 Paddle 正在使用 GPU。交接需記錄各後端實際 provider／device。
- `ultralytics` 的自動安裝已關閉（`model_library/runtime.py` 設 `YOLO_AUTOINSTALL=false`）；不要讓任何工具自動安裝或升級套件。

---

## 4. 目錄地圖

| 位置 | 內容 |
| --- | --- |
| `run_pipeline.py` | 影片辨識主流程：模型推論 → `FrameProcessor` → 規則 → 輸出 |
| `frame_processing.py` | 逐幀後處理（模型輸出 → 觀測紀錄），順序固定，見 5.2 |
| `pipeline_outputs.py` | CSV 與標註畫面 |
| `observation_schema.py` | **觀測紀錄的資料契約**（欄位、寫入者、版本） |
| `model_library/` | 模型介面（`base.py`）、後端註冊（`registry.py`）、偵測器（`detectors.py`）、感知（`perception*.py`）、深度、OCR |
| `road_compensation/` | 鏡頭運動估計（`EgoMotionTracker`、`motion.py`）、停止線補償 |
| `core/` | 共用工具：單應矩陣驗證（`motion.py`）、影格連續性、投影（`geometry.py`）、快速拷貝、計時 |
| `violation_engine.py` | 規則介面、闖紅燈與雙線規則、`ViolationEngine`（含事件合併）、`RULE_FACTORIES` |
| `signal_state.py` | 燈號時間證據（強／弱／延續、黃燈確認、箭頭燈排除） |
| `double_lines.py`、`area_rules.py` | 雙線幾何與追蹤；停等區／斑馬線區域與規則 |
| `configs/` | 模型設定（`default.json`、`full.json`、`onnx_vehicle.json`）、ByteTrack、補償、雙線與區域規則參數 |
| `models/` | 權重（**不在 git 中**）、`模型清單.txt` 記錄檔名與 SHA-256 |
| `eval/` | `ground_truth.json`（人工標註）、`metrics.py`（指標） |
| `tools/` | 評估、重播、量測工具（第 7 節） |
| `tests/` | 單元與回歸測試；數量以本次實際執行結果為準 |
| `reporting/autoForm/` | 填表 GUI（獨立模組，個人設定與案件不進 git） |
| `docs/usage.md`、`使用說明.txt` | 使用者操作說明（兩份需同步） |
| `docs/refactor_log.md` | **開發紀錄**：每次修改的原因、驗證方式、數字、撤回的實驗 |
| `input/`、`outputs/` | 影片與輸出（不在 git 中；本次核對時 `outputs/` 無既有結果，使用前重新檢查，見 7.1） |

---

## 5. 介面契約（修改前必讀；變更屬於 Opus 的整合範圍）

### 5.1 模型階段
- 固定順序：`STAGES = ("vehicles", "traffic_lights", "road_markings", "depth", "plates", "ocr")`（`model_library/registry.py`）。
- 每個模型繼承 `PerceptionModel`，實作 `infer(frame, context: FrameContext, upstream) -> ModelResult(records, artifacts)`；
  `artifacts` 放大型陣列（遮罩等），**不寫入 JSONL**。可選實作 `reset()`、`close()`。
- 新後端：在 `BACKENDS` 註冊（或 `register_backend`），設定檔以 `"backend": "<名稱>"`、`"params": {...}` 使用；
  `weights`、`config` 等路徑相對於設定檔所在目錄解析，啟動時檢查檔案存在。
- **快取／降頻語意**：降頻的模型在未推論的幀回傳上次結果並標記 `source_frame`（車輛是每筆紀錄的 `source_frame`；標線是 `artifacts["source_frame"]`）。
  車輛 `source_frame != 當幀` 時**不能作為新的車輛觀測**累積證據或觸發事件。
  標線目前會由 `FrameProcessor` 推移到當幀再交給規則，來源每幀保存在 `postprocessing.road_marking_source`
  （schema 1.8：`direct`、`aligned`、`age_frames`、`source_frame`；讀取一律用 `observation_schema.marking_evidence()`）。
  推移只是對齊當幀的估計，並非模型重新觀測；運動無效時沿用未推移遮罩（`warped=false`、`aligned=false`）。
  **證據閘門（refactor_log 4l）**：停止線補償、雙線、區域另計直接觀測（`direct_observed_frames`、`direct_rule_eligible`、
  `geometry_source`、`marking_source`），闖紅燈與雙線的 `confirmed` 需要直接觀測的標線（`stop_line_direct_evidence`、
  `direct_marking_evidence`）。推移資料仍可支持幾何對齊與疑似，不得增加確認所需的直接觀測次數。
  1.8 以前的 JSONL 沒有直接觀測欄位，規則沿用原判定；需要閘門時以 `real_replay` 重播。
  2026-11-01 Windows 驗證：interval 2 下 31 支影片的事件與關閉閘門時完全相同；interval 3／4 的評估須在閘門下重做。
  **短暫運動失效容忍（refactor_log 4v，schema 1.9）**：`configs/marking_compensation.json` 的 `allow_motion_gaps`／
  `max_motion_gap_seconds`（true／0.5）讓停止線補償在鏡頭運動失效 ≤ 0.5 秒時以「不動」近似保留軌跡、不重設；
  期間輸出 `compensation.motion_gap=True`，未被觀測更新的線不提供給規則。闖紅燈條件 `camera_motion_compensated`
  使這段期間的越線只能是疑似；斑馬線代理線仍在失效時清除。
- 輸入是 `uint8 BGR`，輸出座標均為原圖像素；模型後端不可修改輸入 frame 或 upstream。
  `records` 必須可 JSON 序列化，遮罩等陣列留在 `artifacts`。跨影片須 `reset()`，結束須 `close()`。

### 5.2 逐幀後處理順序（`FrameProcessor.process`）
鏡頭運動（車輛偵測後即在背景執行緒估計）→ 標線推移（降頻時）→ 建立觀測紀錄 → 停止線補償 → 騎士配對 → 雙線 → 停等區／斑馬線區域。
`run_pipeline` 的 `--save-masks` 存的是**處理後**的遮罩，離線重播才能逐位元重現。

### 5.3 觀測紀錄（`observation_schema.py`，目前版本 1.9）
- 每幀一行 JSON，欄位與寫入者列在檔案開頭的說明；`validate_observation()` 檢查必要欄位。
- **新增欄位**：更新該說明與版本紀錄、`SCHEMA_VERSION` 加一個小版本，舊紀錄必須仍可被讀取（讀取端要有退回值，參考 `core.motion.road_motion()`）。
- 鏡頭運動一律以 `core.motion.road_motion(record)` 讀取、`core.motion.homography()` 驗證，不要直接讀欄位。
- 標線來源一律以 `observation_schema.marking_evidence(record)` 讀取（舊紀錄退回「直接觀測」）。
- 模型可透過 `FrameContext.motion_probe()` 取得本幀鏡頭運動（阻塞至背景估計完成；未提供時為 None），只在需要時呼叫。

### 5.4 規則
- 介面：`ViolationRule.evaluate(observation) -> list[event]`、`finalize() -> list[event]`；`rule_id` 唯一；建構參數 `(context: RuleContext, **options)`。
- 註冊：`violation_engine.RULE_FACTORIES`；`create_rules(names, context, rule_options)`，`"all"` 啟用全部。
- 規則 ID：`red_light_stop_line_crossing`、`double_yellow_line_crossing`、`double_white_line_crossing`、`car_motorcycle_waiting_area`、`crosswalk_stopping`。
- 事件必要欄位：`event_id`、`rule_id`、`status`（`suspected`／`confirmed`）、`frame`、`timestamp_sec`、`object_id`、`conditions`（條件名 → bool）、`evidence`。
  `conditions` 同時含證據成立旗標與估計／預測旗標，不可用 `all(conditions.values())` 決定確認狀態。
  必須依各規則的確認條件判斷；新增放寬路徑只能支持 `suspected`。
- `ViolationEngine` 會把同一車輛的停等區事件併入闖紅燈事件的 `merged_events`：停等區事件至少保留 3 秒，車仍在畫面中（離開未滿 1 秒）或闖紅燈規則仍有它的待定事件時繼續保留；已報告闖紅燈的車，同片之後的停等區事件直接併入（延後輸出的事件帶 `released_timestamp_sec`，refactor_log 4n）。
- 每幀診斷寫在 `rule.diagnostics`（阻擋原因），全片評估以此統計漏判原因；新規則也要提供。

### 5.5 事件車牌補充（4w；預設關閉）

- 三個入口 `run_pipeline.py`、`evaluate_violations.py`、`tools/rule_eval.py` 共用 `plate_backfill.PlateBackfill`，
  `--plate-backfill-seconds 0` 預設停用；停用仍產生頂層 `plate`（event_frame／none），不延遲事件、不建立暫存庫。
- 補充只作用於引擎合併後的事件，不處理 `merged_events` 子事件，不改 `evidence`、狀態、條件、ID 與時間。
  `plate` 欄位與來源契約見 [開發 prompt 第 2 節](docs/handoff/plate_backfill_and_diagnostics_prompt.md)。
- 候選須同 ID、新觀測 `source_frame == frame.index`，信心至少 0.8；相鄰新觀測間隔不得超過 0.3 秒，
  車框中心位移不得超過前框對角線 0.5 倍、IoU 至少 0.1。同字串在其他 ID 出現則拒絕，原牌衝突只標記而不覆寫。
- 視窗內失去連續追蹤或新的候選彼此矛盾時保守不補充；後者沿用 `plate_conflict` 原因，
  `conflict=true` 仍只表示與事件當下原牌不同。這些實驗策略交由 Opus 審查，不改規則的 confirmed 條件。
- 啟用時以 SQLite 磁碟暫存精簡觀測（支援引擎延後到片尾才輸出的事件），關閉自動清除，頁面快取設定 2 MiB；
  暫存磁碟用量隨影片長度增加。事件 FIFO 發布，片尾 flush；逐幀疊圖可先顯示未補充事件。
- 消費端用 `event_plate_text()`：存在 `plate` 就取其 text（含 null），舊事件才退回原證據。
  指標並列事件當下／含補充車牌，兩種配對分開計算；資訊卡與剪輯註明讀數延遲或原牌衝突。

### 5.6 燈號
`signal_state.CombinedSignalState`：`strong`（可支持確認）、`weak`／`held`（只支持疑似）。黃燈需持續 0.2 秒才中斷紅燈；
`arrow` 類別不參與紅／綠／黃判定；多個穩定燈顏色衝突時回報未知（以主燈判定的嘗試已撤回，見 refactor_log 4g）。

---

## 6. 不可違反的原則

1. **放寬只產生疑似**：新增的召回放寬路徑只能讓事件成為 `suspected`，不得暗中削弱 `confirmed` 的確認條件。這是開發約束，不代表既有程式已消除第 5.1 節的推移標線風險。
2. **快取資料不算證據**：見 5.1。
3. **可重現**：同樣輸入必須得到同樣輸出。不要走訪無序集合後依順序產生結果（`tests/test_determinism.py` 以多個雜湊種子檢查）。
4. **沒有數字不宣稱改善**：規則、後處理、模型的修改都要附評估結果（第 7 節），包括變差的部分；撤回的實驗也要記錄。
5. **標註極少，不要對答案調參**：2026-11-01 起 31 支評估影片全部 reviewed，共 34 個事件（來源 `input/違規參考.txt`），
   但每條規則只有 5–14 個事件。指標變動 1 個事件就是 7–20 個百分點，
   報告時寫絕對數（TP／FP／FN），並逐片檢查變動的事件畫面，不要只看百分比。
6. **權重不進 git**：`.pt`／`.pth`／`.onnx` 由 `.gitignore` 排除。更換模型時更新 `模型清單.txt`、`models/<類別>/metadata.json`（若有）、設定檔中的 `sha256`。
7. **外部檔案視為不受信任**：核對交付來源、大小、SHA-256 與部署格式，再按既有受控載入流程處理；不要任意執行未知 pickle。`pickletools` 檢視不等於安全驗證。Git LFS 指標檔不是權重。
8. **不修改 `third_party/`、不自動安裝套件、不提交 `outputs/`、`input/`、個人資料**（`reporting/autoForm/reporter_profile.json`、`cases/`）。

---

## 7. 驗證工具與流程

所有指令在專案根目錄執行，先設定 `PYTHONIOENCODING=utf-8`。

### 7.1 第一次使用：重建評估基準
歷史紀錄指出 `outputs/` 曾在 2026-10-28 清空。本次核對也沒有既有輸出。
只有任務需要實際影片評估且基準不存在時才重建；不要因首次閱讀文件就啟動長時間 GPU 工作。
規則與後處理的離線評估需要全片評估輸出（歷史耗時約 1–1.5 小時，依本機而異，可中斷續跑）：

```
.venv\Scripts\python.exe tools\batch_eval.py --label baseline
```

輸出 `outputs/eval/baseline/<影片>/`：JSONL、規則診斷、逐幀遮罩、`summary.txt`。

### 7.2 依修改類型選擇驗證

| 修改內容 | 必做驗證 |
| --- | --- |
| 程式修改 | 先跑相關測試，交接可執行程式變更時再跑 `.venv\Scripts\python.exe -m unittest discover -s tests`；記錄失敗與環境限制 |
| 純文件修改 | 核對原始碼、文件連結、命令與參數；不必重跑模型或為文字修改新增測試 |
| 只改規則（`violation_engine.py`、`area_rules.py` 規則部分、`signal_state.py`、規則設定） | `tools/rule_eval.py --source outputs/eval/baseline --label <名稱>`（約 1 分鐘，輸出 `rule_options.json` 記錄參數與 commit） |
| 改後處理（補償、雙線、區域幾何、鏡頭運動、`frame_processing.py`） | `tools/real_replay.py outputs/eval/baseline --all --out outputs/eval/<名稱>`（不需 torch；`--recompute-motion` 重新估計鏡頭運動） |
| 改模型或模型參數 | 只重跑該模型產生新 JSONL／遮罩，再用 `real_replay` 重播（做法見 refactor_log 4e、4h）；或重跑 `batch_eval` |
| 重構（宣稱行為不變） | `tools/equivalence_harness.py`：以舊版（`git worktree add ..\old <commit>`）與新版各跑一次再 `compare`，輸出須逐位元相同 |
| 標線取樣策略（排程） | `tools/simulate_sampling.py --source outputs/eval/sampling_20261102_i1 --out <資料夾> --policy fixed:2 --policy <策略>`：以每幀直接觀測的遮罩離線模擬，不需 GPU；與實際 interval 2／refresh 的推論次數、未對齊幀逐片相同（4v） |
| 效能 | `tools/verify_stage.py --label <名稱>`（測試＋三支短片完整辨識＋分段耗時＋與前次比較）；GPU 不可同時有其他工作 |

指標摘要：`batch_eval.summarize(label)` 或 `eval/metrics.py`。影片層級＝該片是否輸出目標規則事件；事件層級只計 `reviewed: true` 的影片。

### 7.3 回報格式
每次交付或整合，在 `docs/refactor_log.md` 新增一節（目前編號到 4ac；4aa 保留給 Astra 的標線尺度實驗），包含：修改內容、原因、驗證方式、
前後數字（影片層級、事件層級 TP／FP／FN、速度）、逐片變動的事件、已知副作用、是否採用。

---

## 8. 目前狀態（2026-10-28）

### 模型
| 階段 | 現行設定 |
| --- | --- |
| 車輛 | YOLO26s ONNX＋ByteTrack，每幀 |
| 紅綠燈 | YOLO26s（red／green／yellow／arrow），imgsz 1280 |
| 標線 | SegFormer B2 best56000，滑動視窗 1024/683、fp16＋fast_attention，**每 2 幀推論**（中間幀依鏡頭運動推移） |
| 深度／車牌／OCR | Depth Anything V2 Small（每 2 幀）、車牌 YOLO ONNX（stable_shapes）、台灣 PP-OCRv6（CPU） |

### 歷史速度（RTX 5060 Laptop，完整流程；不是本次重測）
1080p（ms01）約 1.62 FPS、720p（cdy01）約 2.78 FPS。標線約佔每幀一半；鏡頭運動 60–90 ms 在背景並行；ms01 區域幾何約 67 ms。

### 歷史準確度（21 支影片子集；來源 refactor_log 4h–4i，不是本次重測）
闖紅燈事件層級 TP 6／FP 3／FN 1，影片層級 7/8；停等區影片層級 3/5（無事件標註）；雙黃線 1/7；斑馬線 0/5；車牌完全正確 3/6。

### 已知問題
- 標註：2026-11-01 已補齊 31 支（34 個事件）；時間是約略區間，未列出的真實違規會被算成誤報。舊評估數字是在只有 6 支 reviewed 時算的，不能與新標註直接比較。
- 雙線漏報主因在感知：遠處小車、彎道雙線分割不穩、鏡頭運動在 cdy05／cdy07 常無效。
- 斑馬線規則：`low_confidence` 只是表面原因；主因是車身遮住斑馬線、移動攝影機下停留判定漂移（refactor_log 4p、4q）。4q 後 TP 1 FP 3 FN 3，暫不看機車。
- 燈號：遠處小號誌常被讀成黃燈，造成顏色衝突而判未知（ms03）；橘色招牌被讀成黃燈（ms02）。
- 停等區在行車紀錄器上以「車頭朝遠離鏡頭」假設判定，橫向穿越的車方向不正確（只產生疑似）。
- 闖紅燈誤報：ms02 同一時間兩台車的事件、mm01 14.9 秒事件，尚未逐片確認。

### 待辦（建議負責方）
| 項目 | 負責 |
| --- | --- |
| ~~補完 `eval/ground_truth.json` 標註~~（2026-11-01 完成；發現標註疑義時回報使用者，不自行改答案） | 使用者 |
| ~~斑馬線 `low_confidence` 原因拆分與門檻~~（4p／4q 完成）；斑馬線遠處小車誤報（ms03）、更準確的接地／停止判斷 | Astra |
| 闖紅燈誤報逐片分析與去重 | Astra |
| 燈號主燈選擇（遠處小燈號、顏色衝突） | Astra 提案，Opus 整合 |
| 標線模型 TensorRT／ONNX fp16 匯出（需逐像素驗證） | Opus |
| 區域幾何加速（只在啟用區域規則時計算或降解析度） | Opus |
| 剩餘 10 支影片以新標線模型完整評估 | Astra |
| 標線證據閘門的真實影片影響、標線 interval 1/2/4 與運動失效重新推論評估（docs/handoff/marking_evidence_gate.md） | Astra 評估，Opus 決定預設 |

---

## 9. 交接文件範本（Astra → Opus）

每個功能分支在合併前填寫 [完整交接範本](docs/handoff/TEMPLATE.md)，可放在 PR 描述或 `docs/handoff/<功能名>.md`。
以下是摘要格式；缺少數據時寫「未執行／原因」，不可填入過去測試數冒充本次結果：

```
## 功能
一句話說明與動機。

## 修改檔案
- path：做了什麼

## 介面影響
- 觀測紀錄新增／變更欄位：（無／列出，含讀取端退回方式）
- 新設定參數：名稱、預設值（預設必須維持原行為）
- 介面變更需求：（需要 Opus 處理的部分）

## 驗證
- 單元測試：N 項通過（新增 M 項：列出）
- 評估：工具與 label、前後數字（TP/FP/FN、影片層級、速度）
- 逐片變動的事件與檢查結果

## 已知問題與副作用
## 建議是否切換預設值
```

---

## 10. Git 與協作規範

- 遠端：`origin = github.com/snowmaple777/AI-powered-Automatic-Traffic-Violation-Reporting-System`。
  **本專案的整合分支是 `master`；遠端預設分支是 `main`（內容不同，不要混用）。** 其他遠端分支（`feature/perception`、`feature/road-marking` 等）是早期的獨立模組來源。
- 功能開發在 `feature/<名稱>` 分支；推送與合併到 master 須經使用者同意。不要 force push、不要改寫已推送的歷史。
- 分工本身不是發布授權；已獲得的當次授權持續有效，不要反覆確認。未受本次任務授權時，不推送、不提交填表案件或對外送出訊息。
- 多 agent 同時工作時各用獨立 worktree／分支，或在交接中分配檔案責任；不要在同一工作目錄替另一個 agent 切分支。
  共用介面一次由一方修改。開始與交接均檢查 diff，不得把其他人的變更當作自己的成果提交。
- 交付以明確 commit 或完整差異檔為準，不能只說「已在聊天中改好」。未提交變更須列出檔案與如何取得。
- commit 訊息：英文祈使句標題＋說明原因與驗證結果的內文（參考 `git log`）。
- 本機 tag `refactor-pre`、`refactor-stage1`、`refactor-stage2`、`refactor-stage4b`、`refactor-stage4c` 標記重構階段（未推送）。
- 文件與程式註解使用**繁體中文**；識別字使用英文。使用者文件 `docs/usage.md` 與 `使用說明.txt` 內容需同步。

---

## 11. 已知陷阱

- **文字取代改到不該改的地方**：設定檔中多個模型都有 `"interval"` 等同名欄位，曾因 `sed` 一次取代兩處讓量測作廢。修改 JSON 請用 `json` 模組載入、修改、驗證。
- **Windows 刪除**：路徑含空白與中文，`cd` 後用相對路徑或萬用字元刪除會被安全檢查擋下；使用完整絕對路徑。
- **mmseg 的 `test_cfg`** 需為 `ConfigDict`（以屬性存取）；標線模型對滑動視窗很敏感，只有 1024/683 能重現官方結果（其他視窗停止線 IoU 0.53–0.95）；整張 1080p 推論會讓停等區類別溢出。
- **fp16 只在 fast_attention 下才有加速**；6 個視窗合批推論反而較慢（GPU 已飽和）。
- **ONNX 輸入形狀改變成本很高**：車牌模型以 `stable_shapes` 讓每個 session 只看到單一形狀。
- **`project_points` 會拋出 ValueError**（投影越過地平線）：呼叫端必須處理（曾讓整個辨識中斷，見 refactor_log 4i）。
- **鏡頭運動在不同平台結果不同**（解碼與 OpenCV 差異）：離線重播預設採用 JSONL 記錄值，`--recompute-motion` 才重算。
- 測試輸出中的中文在 cp950 主控台會亂碼，不代表失敗；以 `Ran N tests` 與 `OK` 判斷。
