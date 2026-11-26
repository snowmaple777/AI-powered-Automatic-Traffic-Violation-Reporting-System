# 同追蹤事件後車牌補充交接（4w，Astra，2026-11-04）

## 1. 任務與狀態

- 任務 A：在不改原事件證據與規則條件的前提下，提供同追蹤的事件後車牌補充；預設關閉。
- **狀態：開發與本機驗證完成，待 Opus 整合審查。到此停止；任務 B–E 未實作。**
- 基準：HEAD `48875bc`，分支 `feature/conservative-marking-schedule`；原工作區同時包含未提交的 4t／4u 與 Opus 4v。
- 沒有提交、推送、合併、reset；未修改 ground truth、模型權重、third_party 或安裝套件。
- 本批差異：[4w_plate_backfill.diff](../../outputs/patches/4w_plate_backfill.diff)；從開始前的 dirty 工作區快照產生，不是直接對 HEAD 的混合差異。
- 開始前副本 `outputs/patches/pre_4w/`、雜湊清單 `pre_4w/manifest.json`；本次交付清單／雜湊／保留檔核對 `outputs/patches/4w_manifest.json`。
  共用檔以增量修改；4v 規則、補償程式／設定、real_replay、兩批既有測試與交接原文均保留。
- 本批未提交檔：`plate_backfill.py`、`tests/test_plate_backfill.py`、本交接；修改的既有檔見第 2 節。outputs 與 input 不納入提交。

## 2. 變更與責任位置

| 檔案／函式 | 原問題 | 修改後行為 | 性質 |
| --- | --- | --- | --- |
| `plate_backfill.py`／`PlateBackfill` | 事件早於 OCR 讀數，後來同 ID 讀到也無法補充 | 引擎合併後新增頂層 plate；同 ID、連續新觀測、幾何與文字信心閘門；記錄拒絕原因 | 預設關閉的功能 |
| `run_pipeline.py`、`evaluate_violations.py`、`tools/rule_eval.py` | 線上與離線容易各自補充而不一致 | 共用 update／finalize，輸出事件與剪輯使用已補充版本；三入口 CLI 同一預設常數 | 功能接線 |
| `eval/metrics.py` | 只讀事件當下車牌 | 優先 plate.text；事件當下與含補充分別配對、計分 | 相容消費端 |
| `violation_visualization.py` | 資訊卡未標示後續讀數來源 | 顯示補充延遲；衝突時顯示原牌與後續不同文字 | 呈現變更 |
| `violation_clips.py`（不需改碼） | 需要一致資訊卡／摘要 | 已呼叫上述共用呈現函式，收到補充事件即使用新資訊 | 既有接線核對 |
| `tools/batch_eval.py`、`tools/compare_sampling.py`、`tools/verify_stage.py`、`tools/replay_regression.py` | 事件車牌來源與指標需遷移 | 摘要用共用 reader，評估並列原牌與含補充；原 batch 完整性邏輯保留 | 相容消費端 |
| `observation_schema.py`、AGENTS 5.x、兩份使用說明、refactor_log 4w | 新欄位與限制缺少說明 | 補齊來源、失效、預設、儲存與指標契約；兩份 usage 本次新增節逐位元相同，保留原 Markdown／純文字格式差異 | 文件 |

新增模組／類別負責獨立的「合併後事件補充」階段，避免三入口複製邏輯，不取代 FrameProcessor 或規則引擎。
引擎可能到片尾才輸出事件；僅保留最近 1 秒會漏掉 ctm02／ctm04，因此啟用時以 SQLite 暫存完整影片的精簡觀測索引。
資料庫自動清除，頁面快取設定 2 MiB，但整體程序記憶體不是 2 MiB 上限；暫存磁碟用量隨觀測數增加。停用不建立資料庫。
資料流已接通：模型／FrameProcessor → 規則合併 → 車牌補充 → JSONL、評估、剪輯／資訊卡；逐幀疊圖仍可先用未補充事件。

## 3. 介面交接

| 欄位／設定 | 生產端 | 消費端 | 時間、來源與失效條件 | 舊資料相容 |
| --- | --- | --- | --- | --- |
| 頂層 `plate` | 共用 PlateBackfill | metrics、資訊卡／剪輯、比較／驗證工具 | 欄位完全採 [契約 2.2](plate_backfill_and_diagnostics_prompt.md#22-欄位)；text 為原字串或 null，時間為秒，frame 為原影片幀號 | 沒有 plate 才退回 evidence.vehicle.plate_text；有 plate.text=null 不退回 |
| `--plate-backfill-seconds` | 三入口 parser，共用常數 0 | 補充器 | 有限非負值；0 不延遲、不補充，仍提供 event_frame／none 元資料 | 未傳參數即停用 |
| `plate_at_event`、`event_level_at_event` | metrics 與評估摘要 | 報告 | 與含補充版本各自配對；分母／TP/FP/FN 可能不同，不混用 | 原 plate／event_level 欄位保留為含補充；舊事件兩套相同 |

- 觀測 schema 維持 **1.9**：新增的是事件欄位，不改逐幀觀測契約；文件明確區分兩者。
- `rule_options.json` 與 pipeline timings 記視窗值；單片 evaluate_violations 的每筆 plate.window_seconds 也保留參數。
- 固定實驗閘門：新觀測間隔 ≤0.3 秒、中心位移 ≤前框對角線 0.5 倍、IoU ≥0.1、文字信心 ≥0.8。
  ctm02 第一個正確候選信心 0.9451338053，ctm04 0.9993827343，皆通過。取第一個有效候選，不挑未來最高分。
- `source_frame != frame.index` 不算新讀數，也不能填補追蹤間隔。OCR 文字可能是模型已有投票／快取最佳值；本功能不把重複出現當新 OCR 投票。
- 同字串在視窗其他 ID 出現（含快取紀錄）即拒絕。原牌永不覆寫；衝突設定 conflict=true。沒有原牌但多個新候選互相矛盾時，保守撤銷補充，使用 plate_conflict 拒絕原因，conflict 仍為 false（維持欄位的原牌衝突語意）。
- 中斷超過上限時提前收尾並不採用尚未核實完整視窗的候選；延後才到達的事件重現此中斷。EOF 以已取得的連續前綴收尾。
  `after_window` 只記發布當時已知的第一筆窗外讀數；立即發布與片尾才收到事件的診斷完整度可能不同，不為此延後事件。
- 依引擎交付順序 FIFO 輸出；後面的事件可能等待前一事件到期。只改頂層 plate，原 status、conditions、met／missing_conditions、ID、時間、evidence、merged_events 全部保留。
- 三入口的合成整合測試比較完整事件 dict 相等；實際影片本輪用 rule_eval 重播，未重跑 GPU。
- 交由 Opus 決定：上述保守中斷／候選互斥策略、是否限制可設最大視窗／長片暫存容量，以及實驗視窗的選擇。不得自行豁免「不同 ID 共用字串」來讓 ctm04 通過。

## 4. 可重現環境

- Windows build 26200；Python 3.10.11，NumPy 1.26.4，OpenCV 4.10.0.84；使用本機既有 `.venv`。
- 本輪規則重算不載入模型，無模型 provider／device 或新權重；未做 GPU 重跑、完整後處理重播或速度基準。舊 baseline 的實際模型 provider／權重雜湊不能由本次規則重播重新確認，不填推測值。
- 輸入 baseline_18 全部 31 支、9,411 幀既有 detections，涵蓋 cdy07；不是 `_gap_g05`，因此不宣稱含 4v 新後處理的 ms07 改善。
  FPS／區間沿用各 JSONL 的 frame。每片觀測 SHA-256 收在 comparison.json；本輪未讀遮罩推理。
- GT version 1，SHA-256 `894e0e89fd45dbed15d66b017191cc4139068f35fa689ba1421ce28c6dd658d9`；soc04 excluded，30 支計分、34 個標註事件。
- 對照／實驗各用獨立目錄 `outputs/eval/plate_4w_off`、`plate_4w_on`、`plate_4w_08`。相同規則與觀測，只改補充視窗。規則設定副本在各 rule_options.json。

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests
.venv\Scripts\python.exe tools\rule_eval.py --source outputs\eval\baseline_18 --label plate_4w_off --plate-backfill-seconds 0
.venv\Scripts\python.exe tools\rule_eval.py --source outputs\eval\baseline_18 --label plate_4w_on --plate-backfill-seconds 1
.venv\Scripts\python.exe tools\rule_eval.py --source outputs\eval\baseline_18 --label plate_4w_08 --plate-backfill-seconds 0.8
.venv\Scripts\python.exe outputs\patches\compare_4w.py
.venv\Scripts\python.exe outputs\patches\compare_4w.py plate_4w_08
.venv\Scripts\python.exe outputs\patches\review_4w.py
.venv\Scripts\python.exe outputs\patches\package_4w.py
```

上述 label 是本次產物；重跑時請改用新 label，對照腳本一起調整，不覆寫本輪證據。
outputs 內腳本為本機實驗附件，不提交 git；跨機請一併複製這些附件與基準資料。

## 5. 驗證與結果

- 修改前 **171/171**（85.158 秒），`outputs/patches/4w_initial_tests.txt`；修改後 **190/190**（86.097 秒），`4w_full_tests_unrestricted.txt`。
  Windows 沙箱內兩輪均遇既有 batch 原子改名的 WinError 5（3 error、1 failure）；依批准在沙箱外重跑後全綠，未修改測試規避錯誤。
- 新增 19 項測試：正例、停用、延後事件、換 ID、車框跳動、原牌衝突、其他 ID 同字串、追蹤中斷、提前收尾、快取框、低信心、互斥候選、窗外讀數、EOF、正規化、非法參數／幀序、舊消費端相容、雙來源指標與三入口一致。
  收尾共用預設常數整理後重跑此 19 項仍全綠（0.165 秒），`4w_targeted_tests.txt`。
- 三組均逐片比較 **31 支／25 筆**，只排除新增頂層 plate；其餘完整 dict（含 evidence、merged_events）逐筆相等，事件順序亦相同。
  不是只比較指標。每筆 core 前後 SHA-256、完整 plate／拒絕候選與來源 SHA 見下列報告。
- 規則診斷 JSON 在三組皆逐幀相同；1 秒組有 9 支檔案因 JSON 物件鍵序差異而非逐位元相同，因此不宣稱診斷檔逐位元一致。這不是任務 B 的逐位元驗證。

| 指標 | 關閉 0 秒 | 1 秒 | 0.8 秒 | 分母／限制 |
| --- | --- | --- | --- | --- |
| 事件當下車牌正確 | 6/15 | 6/15 | 6/15 | 匹配且 GT 有牌的 15 筆 |
| 含補充車牌正確 | 6/15 | 7/15 | 8/15 | 本批無新增錯牌；樣本小、不是獨立驗證集 |
| 闖紅燈 TP/FP/FN | 11/3/4 | 相同 | 相同 | reviewed 且未 excluded |
| 停等區 TP/FP/FN | 5/1/2 | 相同 | 相同 | 同上 |
| 雙黃線 TP/FP/FN | 1/0/7 | 相同 | 相同 | 同上 |
| 斑馬線 TP/FP/FN | 1/3/3 | 相同 | 相同 | 同上 |
| 影片命中 | 15/30 | 相同 | 相同 | 與事件準確率不同 |
| FPS／階段 ms | 未量測 | 未量測 | 未量測 | 離線規則重算，不推論模型 |

實際補充／拒絕重點：

- **ctm02**：停等區 suspected，8.267 秒，vehicle-000001；frame 267／8.900 秒讀 RDB-0178，延遲 0.633 秒，兩個啟用視窗都補充。原 evidence 仍無牌。
- **ctm04**：停等區 suspected，6.180 秒，vehicle-000017；frame 338／6.760 秒讀 5363-XT，延遲 0.580 秒。
  0.8 秒視窗補充；**1 秒視窗必須拒絕**：frame 354／7.080 秒另一個 vehicle-000316 也有 5363-XT，兩框幾乎重疊、car／truck 重複分類，仍是不同 ID。
  這是本次新發現，原 prompt 把此例列為正例未涵蓋整個 1 秒視窗；未為它放寬跨 ID 規則。
  0.8 秒是在看到此結果後追加的探索，不能把 8/15 當成獨立驗證或選預設值的充分證據。
- **s04**：14.633 秒，vehicle-000285；事件原牌 ADK-6626 保留，後續不同牌設 conflict=true；不把後續文字當更正原證據。
- 1 秒共 13 筆事件含 rejected：after_window 9 個、shared_with_other_track 22 個、track_gap 14 個、low_confidence 14 個、plate_conflict 24 個。
  這是逐幀候選記錄數，不是獨立 OCR 嘗試數；原本已有牌的事件也可記拒絕而保留原牌。
- 全部逐片事件、未補充事件、所有拒絕字串／幀／原因：
  `outputs/analysis/plate_4w/plate_4w_on/comparison.json` 與 `.txt`，
  `outputs/analysis/plate_4w/plate_4w_08/comparison.json` 與 `.txt`。
  雙 ID 原始欄位：`outputs/analysis/plate_4w/ctm04_shared_track_evidence.json`。
- 資訊卡已實際渲染並視覺核對 `outputs/analysis/plate_4w/cards.png`：補充延遲、原牌與衝突牌清楚顯示；第二張為合成衝突測試，並非真實車牌認定。
  共用剪輯文字摘要見 cards.txt；既有完整測試也覆蓋影片剪輯輸出。本輪未重新目視原影片／OCR 原圖，車牌對錯依未改動的 GT；不宣稱新增人工辨識覆核。

## 6. 整合決策與回退

- **建議維持實驗、預設 0 秒**。1 秒較保守可發現較晚的追蹤歧義；0.8 秒在本批多補一筆，但已用本資料探索視窗，須新資料驗證。
- Opus 先審查純 4w 差異與欄位讀取端，再審查中斷／衝突策略。若改門檻或窗長，重跑三入口一致性與逐片原事件比較；不修改 confirmed 條件。
- 功能回退：參數設 0 即停止補充；仍有新增 plate 元資料。若需要完全舊事件輸出契約，逐檔撤回 **4w 差異**；不得 reset／還原整個工作區，會損失 4t–4v。模型權重不涉及回退。
- 長片 SQLite 暫存容量／效能尚未壓測；幀與時間須嚴格遞增；EOF 後的未來讀數不可能檢查。這些限制與資料依賴需保留在整合說明。
- 已同步 observation_schema、AGENTS 5.x、docs/usage.md、使用說明.txt、refactor_log 4w。
- 未獲推送／合併授權，交付停留本機；不提交 outputs/input。

給 Opus 的接手 prompt：

> 先讀 AGENTS.md、docs/ASTRA.md、docs/handoff/TEMPLATE.md、本交接與原 plate_backfill_and_diagnostics_prompt.md。
> 保留 4t／4u、4v、4w 全部未提交修改。4w 增量差異與 manifest 在 outputs/patches，基準是 pre_4w dirty 快照，不是 HEAD。
> 任務 A 已通過 190 項測試，baseline_18 31 支／25 筆原事件欄位完全不變。預設 0；1 秒車牌 7/15，0.8 秒 8/15，原牌皆 6/15。
> 特別審查 ctm04：7.080 秒重複 ID 共用牌，所以 1 秒不得補，0.8 秒則不涵蓋該歧義；不能以合併 ID 豁免來拉高分數。
> 審查中斷提前收尾、互斥候選及 SQLite 長片容量策略，再決定是否需要獨立資料評估；不要自行改 confirmed 或 GT。
> 完成 A 整合審查後，若使用者繼續授權下一個任務，再依原 prompt 進行 B：預設關閉診斷、GPU OCR 小集合重跑與逐位元事件比對。
> 每個任務單獨交接，下一編號從 4x 接續；B–E 目前尚未做。不要推送或合併。

## 7. Opus 整合審查（2026-11-04）

接受，預設維持 0；程式未修改。重現數字、ctm04 跨 ID 判斷、中斷策略、視窗與長片限制的決定見 refactor_log「4w 整合審查」。
要點：不加重疊框豁免（同車牌跨 ID 在 7 支影片出現，IoU 0–0.79，車牌歸屬錯置真實存在）；開啟前需先加暫存保留期限並以長片壓測。

下一步給 Astra 的 prompt（使用者授權後）：

> 4w 已由 Opus 審查接受（refactor_log「4w 整合審查」），保留 4t–4w 全部未提交修改。依 `plate_backfill_and_diagnostics_prompt.md` 進行任務 B（診斷，預設關閉），編號 4x。
> 先做不需 GPU 的兩項：雙線 `unsupported_footprint_or_invalid_box` 拆分、區域狀態清除原因，以 `real_replay` 重播 baseline_18（cdy07 只能在 Windows 解碼），
> 驗證開關前後 `_violations.jsonl` 逐位元相同，並交出依原因分類的統計。OCR 每次嘗試的診斷放在最後，需要 GPU 重跑 ms03、ctm05、ms04、s04、mm01。
> 順帶處理（可選）：4w 的 `rejected` 改為每個（字串、原因）記首次幀與次數；PlateBackfill 暫存加保留期限設計提案（不必實作）。
