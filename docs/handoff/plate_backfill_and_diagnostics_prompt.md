# 交接給 Astra（GPT）：車牌補充、診斷欄位與後續開發 prompt（Opus，2026-11-04）

本文件是 Opus 執行完 [評估報告](failure_fix_and_dynamic_sampling_review.md) 第 4 節的自身項目後（refactor_log 4v），
整理給 Astra 的後續開發。第 1 節是可直接貼給 GPT 的 prompt；第 2 節起是 prompt 引用的介面契約與背景，**以本文件為準**。

---

## 1. Prompt（直接貼給 GPT）

> 你是 Astra，接手 traffic_ai 的後續開發。先讀 `AGENTS.md`、`docs/ASTRA.md`、`docs/handoff/TEMPLATE.md`、
> `docs/handoff/recognition_failure_analysis.md`、`docs/handoff/failure_fix_and_dynamic_sampling_review.md`、
> 本文件（`docs/handoff/plate_backfill_and_diagnostics_prompt.md`）與 `docs/refactor_log.md` 的 4t–4v。
>
> **工作區現況**：HEAD 48875bc 之後有兩批未提交修改並存——你在 4t／4u 的批次完整性修改（`run_pipeline.py`、`tools/batch_eval.py`、
> `tools/compare_sampling.py`、`tests/test_batch_integrity.py`、文件），以及 Opus 在 4v 的修改（停止線短暫運動失效容忍、
> `tools/simulate_sampling.py`、`tests/test_motion_gap.py`、schema 1.9；差異檔 `outputs/patches/4v_opus.diff`）。兩批都要保留，不要還原、不要 reset。
> 開始前跑一次 `.venv\Scripts\python.exe -m unittest discover -s tests` 確認全綠（Opus 在 VM 上 171 項通過）。
>
> **共同限制**：不修改 `eval/ground_truth.json`；不放寬任何 confirmed 條件，放寬只能產生 suspected；新功能預設關閉，
> 先以離線重播量化，再由 Opus 決定是否改預設；不推送、不合併；不提交 `outputs/`、`input/`；不改 `third_party/`、不自動安裝套件；
> 每個任務一份交接（照 TEMPLATE），refactor_log 從 4w 接續編號；`docs/usage.md` 與 `使用說明.txt` 同步。
> 宣稱「事件不變」時要附逐片事件比對，不是只看指標。
>
> **任務 A：同追蹤的後續車牌補充（最優先）**
> 依本文件第 2 節的欄位契約實作 `plate` 事件欄位與補充邏輯（建議新模組 `plate_backfill.py`，由 `run_pipeline.py`、
> `evaluate_violations.py`、`tools/rule_eval.py` 共用同一個實作，確保線上與離線一致）。預設關閉（`--plate-backfill-seconds 0`）。
> 正例：baseline_18 的 ctm02（事件 8.267 秒，同 ID 8.900 秒讀到 RDB-0178）、ctm04（6.180 秒 → 6.760 秒讀到 5363-XT）。
> 反例必須各有測試：(1) 換 ID／車框跳動；(2) 事件當下已有車牌、之後讀到不同車牌（不得覆寫，記 `conflict`）；
> (3) 視窗內同一字串也出現在另一個 object_id（不得補充）；(4) 追蹤中斷超過上限後才讀到；(5) 快取框（`source_frame != index`）的車牌不算新讀數。
> 用 `tools/rule_eval.py` 在 baseline_18（或 `outputs/eval/_gap_g05`，含 4v 的重播）量化：車牌正確數要分開報「事件當下」與「含補充」，
> 逐片列出所有被補充、被拒絕（含原因）的事件；事件的 status、conditions、event_id、時間必須與關閉時逐筆相同。
> 同步修改消費端（第 2.4 節清單）。
>
> **任務 B：可觀測性診斷（預設關閉）**
> 依第 3 節實作三組診斷：OCR 每次嘗試、雙線 `unsupported_footprint_or_invalid_box` 拆分、停等區／斑馬線狀態清除原因。
> 驗證：開關前後 `_violations.jsonl` 逐位元相同（OCR 診斷需 GPU 重跑幾支影片：建議 ms03、ctm05、ms04、s04、mm01）；
> 雙線與區域的診斷可用 `real_replay` 重播 baseline_18 取得。產出一份依原因分類的統計，作為任務 C、D 與雙線修正的依據。
>
> **任務 C：小車牌試讀（任務 B 之後，需 GPU）**
> 只針對「已成立事件的車」在 45–57 px 寬度試讀，配合清晰度挑幀，不全域降低 `min_width=58`。
> 目標：ms03（50.2 px）、ctm05（51.0 px）；觀察 cdy06、soc03、s05。報告新增的正確與錯誤讀數；錯讀率不可接受就不採用。
>
> **任務 D：燈號疑點（分析為主）**
> s05 第 65 幀把整排暗的號誌外殼判成 green（0.424），ms06 黃燈框約 12×8 px。先用前後幀與燈泡位置確認，
> 提出不放寬 confirmed 的修正（例如燈泡區域顏色驗證只用於否決錯誤的綠燈，或標為 unknown），並估計對 green_veto 與疑似事件的影響。
>
> **任務 E：refresh 改用前一幀鏡頭運動（低優先，預設關閉）**
> 依第 4 節規格在 `MarkingModel` 加入 `refresh_motion_source: "current"|"previous"`，`FrameContext` 帶入前一幀運動有效性。
> 以 `tools/simulate_sampling.py --policy fixed:2+refresh_prev` 的推論次數（cdy05 505、ms01 50、ms07 76、soc01 192）作為對照，
> 在 Windows 實跑 sampling 五支影片確認次數一致，並報告 `motion_wait_seconds` 應為 0。不改預設（interval 2、refresh 關閉）。
>
> 每完成一個任務就停下交接，不要一次合併多個任務的修改。遇到需要改事件 schema 或規則語意以外的決定，寫進交接請 Opus 決定。

---

## 2. 任務 A 的介面契約：事件的 `plate` 欄位

### 2.1 原則

- **事件當下的證據不改寫**：`evidence.vehicle.plate_text` 維持現行意義（規則產生事件時車輛紀錄上的車牌），補充永遠不寫回這裡。
- 補充只新增頂層 `plate` 物件，明確標示來源、讀到的幀與延遲；不能把較晚讀到的車牌偽裝成事件當下的讀數。
- 補充**不改變**事件的 `status`、`conditions`、`met_conditions`、`missing_conditions`、`event_id`、`frame`、`timestamp_sec`、`merged_events`。
- 只在同一個 `object_id`、可信的連續追蹤內補充；有任何不確定就不補充，並在 `plate.rejected` 記錄原因。

### 2.2 欄位

```json
"plate": {
  "text": "RDB-0178",
  "source": "same_track_after_event",
  "object_id": "vehicle-000001",
  "observed_frame": 267,
  "observed_timestamp_sec": 8.9,
  "delay_seconds": 0.633,
  "text_confidence": 0.95,
  "window_seconds": 1.0,
  "conflict": false,
  "rejected": []
}
```

| 欄位 | 型別 | 意義 |
| --- | --- | --- |
| `text` | str 或 null | 回報用車牌（與 `vehicle.plate_text` 同格式的原字串）；沒有可信讀數時 null |
| `source` | `event_frame`／`same_track_after_event`／`none` | `event_frame`：取自 `evidence.vehicle.plate_text`；`same_track_after_event`：事件後同追蹤讀到；`none`：都沒有 |
| `object_id` | str | 讀到車牌的追蹤 ID，必須等於事件的 `object_id` |
| `observed_frame`、`observed_timestamp_sec` | int、float 或 null | 讀到該車牌的幀（`event_frame` 時等於事件的 `frame`／`timestamp_sec`） |
| `delay_seconds` | float 或 null | `observed_timestamp_sec − timestamp_sec`，`event_frame` 時為 0 |
| `text_confidence` | float 或 null | 該幀車輛紀錄上的 `text_confidence` |
| `window_seconds` | float | 本次使用的補充視窗（設定值；0＝停用，此時只會有 `event_frame`／`none`） |
| `conflict` | bool | 事件當下已有車牌，但視窗內同追蹤讀到不同車牌（正規化後不同）；`text` 仍為事件當下的車牌 |
| `rejected` | list | 被拒絕的候選：`{"text", "frame", "reason"}`，reason 見 2.3 |

### 2.3 補充規則（建議值由 Astra 以資料驗證後提出）

候選來自事件之後、`timestamp_sec + window_seconds` 以內的逐幀觀測 `observations.vehicles`（或 `rule_vehicles`）中，
`object_id` 相同、`source_frame == frame.index`（新偵測，不是快取框）、`plate_text` 非空的車輛紀錄。依時間順序檢查，第一個通過者採用。

| 拒絕原因 `reason` | 條件 |
| --- | --- |
| `track_gap` | 事件幀到候選幀之間，該 ID 連續缺席超過上限（建議 0.3 秒） |
| `track_jump` | 相鄰兩次出現的車框中心位移超過車框對角線的比例上限，或 IoU 過低（疑似換 ID） |
| `plate_conflict` | 事件當下已有車牌且正規化後不同（設定 `conflict=true`，不覆寫） |
| `shared_with_other_track` | 視窗內同一字串（正規化後）也出現在其他 object_id |
| `low_confidence` | `text_confidence` 低於門檻（若採用；ctm02／ctm04 的值要先查） |
| `after_window` | 只為診斷：視窗外第一個讀數（不採用） |

- 事件輸出時機：事件產生後保留到視窗結束、或追蹤中斷超過 `track_gap` 上限（以先到者為準）；`finalize()` 時全部輸出。
  逐幀影像疊圖可以先用未補充的事件（`recent_events`），但寫入 `_violations.jsonl` 的必須是補充後的版本。
- 補充作用在 `ViolationEngine` 合併之後的輸出事件上；被併入 `merged_events` 的子事件不另外補充。
- 設定：命令列 `--plate-backfill-seconds`（預設 0），程式內預設值寫在同一模組；不新增 schema 版本以外的欄位語意。

### 2.4 生產端與消費端

| 角色 | 檔案 | 需要的修改 |
| --- | --- | --- |
| 生產 | `plate_backfill.py`（新） | 保留與補充邏輯、`finalize()` |
| 生產 | `run_pipeline.py`、`evaluate_violations.py`、`tools/rule_eval.py` | 串接同一實作；`rule_options.json`／timings 記錄視窗值 |
| 消費 | `eval/metrics.py` | 配對與車牌正確數使用 `plate.text`（無此欄位時退回 `evidence.vehicle.plate_text`）；**另外**報告只用事件當下車牌的數字，兩者並列 |
| 消費 | `violation_clips.py`／`violation_visualization.py` | 資訊卡顯示 `plate.text`；來源是補充時加註「事件後 X.X 秒同追蹤讀到」；`conflict` 時兩個都顯示 |
| 消費 | `tools/compare_sampling.py`、`tools/verify_stage.py`、`tools/replay_regression.py` | 事件車牌欄位改讀 `plate.text`（相容舊檔） |
| 文件 | `observation_schema.py`（事件 schema 說明若有）、`docs/usage.md`、`使用說明.txt`、AGENTS.md 5.x | 欄位說明與「不改寫事件當下證據」的約定 |

舊事件檔沒有 `plate` 欄位：消費端一律退回 `evidence.vehicle.plate_text`，結果與現在相同。

---

## 3. 任務 B 的診斷需求

全部預設關閉（例如 `--ocr-diagnostics`、規則設定 `detailed_diagnostics`），開啟時事件輸出必須逐位元不變。

1. **OCR 每次嘗試**：每次實際送進 OCR 或被閘門擋下的車牌，記錄
   `object_id`、`frame`、`plate_bbox_xyxy`、裁切寬高、`distance_m`、閘門結果（`attempted`／`too_small`／`too_far`／`held`／`interval_skip`）、
   原始文字、分數、正規化文字、格式是否通過、投票結果（被採用／被否決及原因）。現有 `plates.raw_text` 是快取最佳讀數，不能取代。
   目的：區分「模型讀錯」與「投票／過濾選錯」（mm01、ms04、s02、s04），以及任務 C 的小牌候選。
2. **雙線車輛位置失敗分支**：把 `unsupported_footprint_or_invalid_box` 依 `double_lines.vehicle_line_position` 的 `return None` 分支拆成
   `invalid_bbox`、`outside_line_support`、`endpoint_margin`（或更細），附失敗的參考點、線段端點與外插限制。
   `tools/rule_eval.py` 摘要的阻擋原因統計要同步（舊名稱保留為彙總或在文件說明對照）。
3. **區域狀態清除原因**：停等區／斑馬線規則每次清除累積狀態時記錄 `object_id`、`region_id`、原因
   （追蹤消失、重疊低於門檻、低信心逾時、離開區域、重新武裝等）、已累積秒數與次數。soc03 的「第 20 幀重疊量測變 0 而清除」要能直接從診斷看出。

---

## 4. 任務 E 的規格：refresh 改用前一幀運動

- `FrameContext` 新增 `previous_motion_valid: Optional[bool] = field(default=None, compare=False, repr=False)`；
  `run_pipeline.py` 在每幀鏡頭運動完成後記住結果，下一幀建立 context 時帶入（新影片、`reset()`、不連續時為 None）。
- `MarkingModel` 新增 `refresh_motion_source`（`"current"` 預設＝現行行為；`"previous"`）。`previous` 時不呼叫 `motion_probe`、不等待；
  沿用快取的幀若 `previous_motion_valid is False` 則重新推論，排程原因記 `previous_motion_invalid`；`None` 不觸發（第一幀已由 cached 為空處理）。
- 參數驗證、`schedule_diagnostics.mode` 要能區分兩種來源；`refresh_on_invalid_motion=false` 時此參數無效果。
- 離線預期（`outputs/eval/sim_sampling_20261104/summary.txt`）：推論 cdy05 505、ms01 50、ms07 76、soc01 192，事件與 interval 2 相同；
  未對齊幀 18（現行 refresh 為 0，interval 2 為 224）。cdy07 在 Opus 的 VM 無法解碼，模擬未含，Windows 實跑時請一併比較。

---

## 5. Opus 這一輪已完成、Astra 不需重做的部分（4v）

| 項目 | 結果 |
| --- | --- |
| 停止線補償＋闖紅燈的短暫運動失效容忍（`allow_motion_gaps` true、0.5 秒） | 闖紅燈 TP 11→12（ms07）、FP 3→3；停等區 FP 1→0；confirmed 不變；斑馬線代理線仍在失效時清除 |
| `tools/simulate_sampling.py` | 與實際 i2、i2＋refresh 的推論次數與未對齊幀逐片相同；結論維持 interval 2、不開 refresh；靜止降頻不採用 |
| `tools/real_replay.py` 的 `--` 參數傳遞 | 修正 |
| schema 1.9 | `road_marking_compensation.motion_gap／motion_gap_seconds`、`compensation.motion_gap` |

## 6. 需要使用者決定或提供的事項（不由 Astra 自行決定）

1. **cdy06 的「壓線」**：標註描述為機車壓雙黃線，現行規則只認車底內縮邊完整跨到另一側。是否要把「壓線（未完全跨越）」列為違規？若要，屬新的規則語意，只能先產生 suspected。
2. **長時間行車影片**：動態取樣（車輛接近標線時才密集推論）只在長時間、大部分無事件的影片才有意義。需要 1 支以上分鐘級、未剪輯的行車影片，最好附違規時間標註。
3. cdy07 只能在 Windows 解碼（VM 的 OpenCV 不支援），涉及 cdy07 的重播請在 Windows 執行。
