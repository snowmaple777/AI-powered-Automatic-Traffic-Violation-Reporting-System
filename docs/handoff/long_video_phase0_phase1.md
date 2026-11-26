# 長片壓測階段 0／1（4y）

## 1. 任務與狀態

依 [Opus 長片測試方針](long_video_test_plan.md) 執行階段 0 和 1。使用本機背景程序依序執行，階段 0 通過驗證後接著執行階段 1。
HEAD 48875bc，feature/conservative-marking-schedule。4t–4x 與既有未提交修改保留。最新狀態讀 `outputs/analysis/long_video_20261108/run_01/progress.jsonl`。

## 2. 變更與責任位置

- `run_01/run_suite.py` 依獨立素材清單排程，將 GPU 工作序列化，並在個別案例的 `result.json` 通過後記錄 `verified`。失敗時保留該案例輸出和退出碼。
- `run_01/worker.py` 使用 `run_pipeline.run` 和 `batch_eval.validate_outputs`，抽樣程序樹 RSS 與全卡顯存，每 30 秒一筆。量測每個模型正常呼叫、暖機、載入、各處理階段與輸出大小。階段 1 另保存規則 finalize 時間與事件 ID。
- `run_01/report.py` 彙整已驗證案例。`report.md` 是可讀摘要，`report.json` 含原數字、分段吞吐及 RAM 趨勢。整組進度另由 `progress.jsonl` 記錄。

## 3. 介面與實驗決策

- 階段 0 採第一分鐘，A5、A3 各 1,800 幀，A9_cut 3,600 幀，A7 取 1,788 幀。A5 收集組沿用同一區間，以 interval 1 輸出遮罩。兩組 ROI 用同一批原始遮罩重新估計鏡頭運動，只調 ROI 範圍，兩組均開 `allow_motion_gaps`。
- 首次 A5 的深度模型輸入依長寬比變成 392×518，造成 `/Add` 的 1,037 與 1,401 tokens 不相容。實測本地 ONNX 接受 392×700。4:3 的壓測設定用 `depth_export_shape.py` 固定該尺寸，重用現有固定輸入前處理及原圖深度圖還原。4:3 深度輸入會被拉伸，對距離精度的影響待驗證。每次測試的有效設定在案例資料夾，原始 A5 失敗在 `phase0/A5_failed_1`。
- A5／A1 整片使用 interval 2，plate-backfill 0。SQLite 由兩支的 JSONL 呼叫 `rule_eval.run_rules` 各跑 0／1 秒並比對移除頂層 plate 後的事件。容量以頁數乘頁大小計算，同時記錄可見暫存檔大小，兩者分開。

## 4. 可重現環境

Windows、本機 RTX 5060 Laptop GPU、專案 `.venv`。各案例的 `request.json` 記錄來源、設定與權重指紋，`pipeline_argv.json` 記錄實際參數。資源抽樣包含 OCR 子程序，RSS 加總可能重計共享頁面，顯存是整張卡的採樣值。

```powershell
.venv\Scripts\python.exe -u outputs\analysis\long_video_20261108\run_01\run_suite.py
.venv\Scripts\python.exe outputs\analysis\long_video_20261108\run_01\report.py
```

已有背景程序時，以讀取 `progress.jsonl` 和當前 `worker.log` 追蹤。重新執行前先確認程序退出，避免同一組寫入。程式會保留失敗案例資料夾並略過已有結果的案例。

## 5. 驗證與結果

已驗證深度模型 392×700 可輸出有限值，以及重跑 A5 的影格和資源日誌持續增長。短段完整結果、ROI 對照、整片 EOF、資源趨勢和 SQLite 數字以各案例實際產物為準。只有 `phase1_complete` 代表整組收尾。素材沒有違規完整標註，無 TP／FP／FN 數字。

## 6. 整合與後續

固定深度輸入只供壓測設定使用。若將它整合進產品，應另驗證 4:3 的距離及下游車牌效果。下一次交接先查 `progress.jsonl` 的末筆狀態，再核對 `report.md`、ROI 圖與階段 1 的 `completion`。若失敗，從該案例 `worker.log` 與既有輸出定位原因。階段 2 獨立安排。

## 7. Opus 審查（2026-11-08）

結果與判斷見 refactor_log「4y 審查：階段 0 結果」。要點：階段 0 的 5 個 GPU 案例有效；ROI 對照因 Windows `cv2.imread` 讀不到非 ASCII 絕對路徑而在沒有標線的情況下完成，結果無效；階段 1 未執行。
`tools/real_replay.py` 已修正讀圖方式。

下一步給 Astra 的 prompt：

> 讀 refactor_log「4y 審查」與本交接第 7 節，保留 4t–4y 全部未提交修改，編號 4z 起。
> 1. 修正 worker 與 `tools/batch_eval.py` 第 174 行的遮罩讀取（同 `tools/real_replay.read_mask`：`np.fromfile`＋`imdecode`），重跑 ROI 對照（只需 CPU），先確認兩組重播的 JSONL 確實有標線紀錄，再報告運動有效率、停止線可用幀數、事件差異與引擎蓋標線像素。
> 2. 跑階段 1（A5、A1 整片，生產設定、壓測用深度尺寸），報告 RAM／顯存隨時間的趨勢與 EOF 收尾；SQLite 容量離線量測照原計畫。
> 3. 標線 `inference_scale` 實驗（GPU 約 30–60 分鐘）：A5、A3 首分鐘以原解析度（`[960, 720]`）與中間值（`[1280, 960]`）只重跑標線，對照收集組遮罩計算各類別 IoU、停止線／斑馬線／停等區的出現幀數差異，再用 `real_replay` 比較事件；報告標線 ms／幀。不改預設。
> 4. 深度模型 4:3 支援：提出補邊後裁回的實作（預設行為修正，需測試），比較拉伸與補邊對 `distance_m` 與車牌 OCR 啟動數的影響；交 Opus 審查後再整合。
> 階段 2 等第 3 項結果再決定規模。不放寬 confirmed、不改預設、不推送／合併。
