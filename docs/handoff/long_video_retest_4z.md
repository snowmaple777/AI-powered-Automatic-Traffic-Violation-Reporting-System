# 長片測試修復與重跑（4z）

## 1. 任務與狀態

- 使用者要求依 Claude 最新交接重新測試。本次完成 4y 審查 prompt 第 1 項修復，接續第 2 項階段 1；第 3、4 項另案交接，不混入穩定性基準。
- Astra 開發／驗證，交 Opus 審查。HEAD `48875bc0e5a383640591081bb4d1b9e414127e44`，分支 `feature/conservative-marking-schedule`。
- 保留全部 4t–4y 未提交修改，包含 Opus 的 `tools/real_replay.read_mask` 與 `tests/test_real_replay_masks.py`。未提交、推送或合併。
- 工作狀態及測量數字以第 5 節與 `outputs/analysis/long_video_20261108/run_01/progress.jsonl` 為準，排程啟動不等於整片完成。
- 修改前快照：`outputs/analysis/long_video_20261108/run_01/4z_before/`。本次增量差異：`outputs/patches/4z_long_video_retest.diff`，不以 HEAD diff 冒充本次修改。

## 2. 變更與責任位置

| 檔案／函式 | 原問題 | 修改後行為 |
| --- | --- | --- |
| `tools/batch_eval.py::validate_outputs` | imread 讀不到含中文的絕對路徑 | 共用 Opus 的 read_mask，以 fromfile＋imdecode 讀取，損壞則失敗 |
| `tests/test_batch_integrity.py` | 未覆蓋中文輸出資料夾 | 新增真實 PNG 中文路徑通過、同檔遭破壞被拒絕的回歸測試 |
| 附件 `worker.py::roi` | 空遮罩仍完成重播，最後統計才失敗；抽圖未檢查寫入 | 先驗來源全部遮罩，兩組 JSONL 均要求每幀 mask_available、直接標線與非空標線紀錄；抽圖使用 imencode＋tofile |
| 附件 `run_suite.py` | 僅看 result.json 存在就略過 | 重用 GPU 結果前重驗輸出、影片指紋及範圍；新增 --phase0-only，便於先覆核 ROI |
| 附件 `worker.py::offline` | 事件不同也會發布 result.json | 保存每支影片逐事件雜湊（僅排除 plate），不一致則停止，不發布成功 |
| 附件 `report.py` | 首末取樣不足以看長期變化 | 完成後列出每五分鐘 RAM／整卡顯存中位數、EOF、finalize；未完成案例明示尚無結果 |

只修復測試與驗證工具；未增加生產模組或替代辨識主流程。附件沿用 run_pipeline、real_replay、rule_eval。

## 3. 介面交接

- 觀測 schema 仍為 1.9。無新生產參數、規則或預設；confirmed、快取證據、事件 ID／時間／去重與 finalize 語意不變。
- batch_eval 與 real_replay 共用既有遮罩解碼函式。缺少檔案由批次遮罩數量／連續性驗證攔截；檔案存在卻無法解碼必須失敗。
- 新增欄位僅在壓測附件結果：遮罩與標線覆蓋、運動有效比例、五分鐘資源統計；消費端只有附件 report.py。
- ROI 是實驗設定，只調整道路運動／停止線補償的 roi_top／roi_bottom。**不會裁掉分割模型的引擎蓋像素，也不會替斑馬線／停等區自動裁切遮罩。**
- 4:3 深度仍用既有壓測專用 392×700 拉伸版本；不得將成功跑完當成產品已支援正確距離。

## 4. 可重現環境

Windows、本機 `.venv`、RTX 5060 Laptop；本輪 nvidia-smi 顯示驅動 610.62、8151 MiB 顯存。各 GPU 案例 `result.json.runtime_devices` 才是實際後端資料，不能由可用裝置推論。
沿用原設定及權重，GPU 案例的 request.json、effective_config.json 保存指紋／設定。資料集未標註，未修改 ground_truth，無 TP／FP／FN。
ROI 使用原 A5 首分鐘 interval 1 的 1,800 個遮罩及 JSONL；原無效 ROI 資料夾由排程保留為 `phase0/roi_failed_1`。
階段 1 依序 A5 18,174 幀及 A1 47,396 幀，合計 65,570 幀，無逐幀遮罩、無剪輯、plate-backfill=0。

```powershell
# 專案根目錄；背景工作尚在執行時不要重複啟動。
$env:PYTHONIOENCODING='utf-8'
.venv\Scripts\python.exe -m unittest discover -s tests -p test_batch_integrity.py
.venv\Scripts\python.exe -m unittest discover -s tests
.venv\Scripts\python.exe -u outputs\analysis\long_video_20261108\run_01\run_suite.py --phase0-only
# 上一步完成並覆核後，接續 A5、A1 全片與 SQLite。已驗證短段會重用。
.venv\Scripts\python.exe -u outputs\analysis\long_video_20261108\run_01\run_suite.py
.venv\Scripts\python.exe outputs\analysis\long_video_20261108\run_01\report.py
```

## 5. 驗證與結果

- 修改前沙箱測試：191 項，107.782 秒，3 errors／1 failure，均屬既有暫存檔原子改名 WinError 5 及其連帶失敗；保留 pre_4z_tests.log。
- 正常 Windows 環境相關測試：15 項全部通過，1.164 秒。
- 修改後正常 Windows 完整測試：**192 項全部通過，97.002 秒**，保留 post_4z_tests.log；中文主控台編碼有亂碼，英文結尾與退出碼正常。
- 原五個 GPU 短段重驗通過，重用其模型輸出；速度數字仍為 4y 歷史測量，不冒充本次重測。
- ROI：2026-11-08 12:12（台北）驗證通過，兩組遮罩、原始標線、規則用標線及直接觀測皆覆蓋 1,800／1,800 幀。

| A5 首分鐘指標 | 原 ROI 0.4–1.0 | dashcam ROI 0.3–0.73 |
| --- | ---: | ---: |
| 運動有效幀／比例 | 1,798／99.889% | 1,799／99.944% |
| 補償後 stop line 出現幀 | 1,503 | 1,504 |
| 規則 usable_stop_lines 幀（含可能的斑馬線代理） | 1,533 | 1,534 |
| 事件 | 1 suspected | 1 suspected |

- 逐片逐事件對照只有本次 A5 一支、每組一個事件，無排除欄位。`collection → default → dashcam` 完整事件物件與 `_violations.jsonl` 逐位元相同：`A5:crosswalk_stopping:vehicle-000098:area-000020:290`，9.667 秒／第 290 幀，疑似斑馬線停留。逐事件 SHA-256、完整 conditions／evidence 見 `phase0/roi/comparison.json`，人讀版 `comparison.md`。不將此結論外推至其他影片。
- 畫面高 0.73 以下：1,800／1,800 幀有非背景標線像素，累計 25,762,274 pixel-frames。這是包含部分真實道路的水平帶，**不能將總數直接當成引擎蓋誤分割像素數**。
- 抽查像素最多的第 1,340 幀／44.667 秒，`hood_001340.jpg` 明顯顯示停等區分類擴至引擎蓋；該水平帶非背景共 118,997 像素，其中 110,042 為 motor waiting zone。ROI 變更不裁分割遮罩，這個問題仍存在。前五張高像素抽圖已保存。
- 階段 1：ROI 通過後以隱藏背景程序接續，suite PID 9356；實際當前案例與 PID 讀 progress.jsonl。RAM／顯存趨勢、EOF、SQLite 容量尚未取得，預估辨識迴圈 8.36–9.74 小時，不宣稱完成。

## 6. 整合決策、回退與後續 prompt

建議採用中文路徑驗證修復；ROI 在本段只增加一幀有效運動與停止線，尚不足以決定改預設，規則／模型與 ROI 預設維持原樣。回退只反向套用本次增量差異，不得 reset 工作區或還原 Opus 既有修正。
已同步 docs/usage.md、使用說明.txt、AGENTS.md 編號及 refactor_log 4z。outputs 附件不提交，交接須連同增量 diff 與附件一起閱讀。

給 Opus／接手 agent：

> 先讀 AGENTS.md、docs/ASTRA.md、long_video_phase0_phase1.md 第 7 節及本交接。保留 4t–4z 所有未提交修改。
> 先確認 progress.jsonl 末筆。若階段 1 正在執行，只讀日誌，不另開 GPU 工作，也不要修改受指紋涵蓋的 Python 程式／設定／權重，避免本次長片結果驗證失敗。
> phase1_complete 之後查 A5／A1 result.json：expected_frames、實際幀數、EOF、finalize、RAM 與整卡顯存五分鐘區段及 SQLite 容量；逐片檢查 event_comparison.json 的原事件是否相同。失敗則讀該 worker.log，不把存在檔案當成成功。
> 後續依 4y 審查第 3 項做標線尺度實驗，單獨交接編號 4aa。A5 有 interval 1 收集組遮罩；**A3 目前只有 interval 2 生產組且未存遮罩**，必須補原尺度的標線單模型基準，不能拿不存在的 A3 收集組做 IoU，也不能把推移幀當直接觀測。
> A5／A3 的原尺度、[960,720]、[1280,960] 應用相同原片範圍和 interval 1，只重跑標線；保存遮罩與原始標線供 real_replay，逐類別 IoU、出現幀、耗時、逐事件比較都要交付。不得用只有 FPS 進步決定採用。階段 2 規模等該實驗再定。
> 第 4 項深度補邊另案：先比對拉伸與 letterbox 的 distance_m、有效距離比例、OCR 嘗試數，再交 Opus 決定預設修正；不要混入本次穩定性基準或直接套用未驗證距離。
> A5 9.6 秒的遠端白車斑馬線疑似待人工覆核；無完整標註不得報召回率。保持 plate-backfill=0、不放寬 confirmed、不改 GT、不推送／合併。

## 7. Opus 審查（2026-11-08，A1 執行中）

結果與判斷見 refactor_log「4z 審查」。要點：ROI 對照有效且兩組相同，不改預設；引擎蓋被分割成停等區，需遮罩層處理；A5 整片穩定完成；RSS 受 Windows 工作集修剪影響不可判讀，顯存約 120 分鐘處 +1.5 GB 待歸因；10 分鐘 5 個疑似事件初看都是橫向／對向車輛。

A1 完成後給 Astra 的 prompt（編號 4aa 起，原 4y 審查第 3、4 項順延）：

> 讀 refactor_log「4z 審查」與本交接第 7 節，保留 4t–4z 全部未提交修改。等 `phase1_complete` 後：
> 1. 審 A1：EOF、finalize、事件逐筆比對、SQLite 容量；資源改加記錄程序私有位元組與 nvidia-smi 每程序顯存（不需重跑 A5，A1 若已完成則在下一個 GPU 案例加上）。
> 2. 把 A5、A1 全部事件做成覆核清單（每筆事件時間、規則、狀態、車框、前後 ±1 秒縮圖與違規片段），交使用者標 TP／FP；不得自行判定為 FP 後改規則。
> 3. 引擎蓋遮蔽離線實驗：以 A5 收集組遮罩，在分割遮罩送入後處理前把畫面高 ≥ 0.73 的像素設為背景（參數只放實驗設定），用 real_replay 比較區域幾何、事件與停止線；報告是否影響停等區／斑馬線事件。
> 4. 再做標線 inference_scale 與深度 4:3 補邊（依 4z 交接第 6 節的條件）。
> 不放寬 confirmed、不改預設、不改 GT、plate-backfill=0、不推送／合併；A1 執行中不修改受指紋涵蓋的程式。

### 7.1 A1 審查與下一步（2026-11-08 晚）

A1 整片穩定完成，顯存沒有持續上升；事件約每小時 93 筆，主要誤報機制是紅轉綠後燈號軌跡衝突（unknown）使綠燈否決失效。詳見 refactor_log「4z 審查（續）」。更新後的優先順序：

> 1. 等 SQLite 量測完成後補報結果；顯存改記每程序用量。
> 2. 事件覆核清單（A5 5 筆＋A1 41 筆）交使用者判定，含每筆片段；這是之後所有規則調整的依據。
> 3. 燈號身分延續（編號另起）：`CombinedSignalState` 在 `conflicting_signal_tracks` 時，若先前已確立的主燈號軌跡仍被觀測且顏色改變，沿用該軌跡的顏色；只影響疑似與綠燈否決，confirmed 條件不變。
>    驗收：31 支短片闖紅燈必須維持 TP 11（mm01、ms03、ctm01、soc01 是敏感案例），並以 A1／A5 的使用者判定計算誤報減少量；用 `rule_eval` 離線比較，不需 GPU。
> 4. 標線 inference_scale 實驗（約 1 小時 GPU），之後才開階段 2；階段 2 的感知結果與規則修改無關，可在規則調整期間並行收集。
> 5. 深度 4:3 補邊、引擎蓋遮蔽離線實驗照原計畫。

### 7.2 使用者覆核後（2026-11-09）

使用者判定看過的 17 筆長片事件全部是誤報（含 A1 375.6 秒確認事件），記錄於 `eval/long_video_review.json`。原因與修正方向見 refactor_log「長片事件覆核結果」。7.1 的第 2 項改為：把 A1 其餘 29 筆也做成覆核清單交使用者，擴充已知誤報集。
規則修正（接地點遮擋、燈號身分延續、代理線需紅燈證據、斑馬線方向／壅塞過濾）每項單獨交付，同時以短片 GT 與 `long_video_review.json` 驗收。

### 7.3 Opus 已完成修正 1、2（4ab，2026-11-09）

接地點遮擋檢查（`occluded_contact`，預設 skip）與主燈號延續（`signal_selected_continuity`，預設開）已實作，未提交。短片 31 支事件逐筆不變；A5 事件 5→4、A1 41→24（闖紅燈 26→9，確認 1→0）。詳見 refactor_log 4ab。
事件時間軸（含修正後狀態）：`outputs/analysis/long_video_20261108/fp_timeline/fp_timeline.html`／`.csv`。
請 Astra 在 Windows 以 `rule_eval`（或 `real_replay`）對 A5、A1 重算確認與 Opus 的分段重算一致，再進行修正 3（代理線需紅燈證據）與 4aa 標線尺度實驗。

### 7.4 修正 3、4 完成（4ac，2026-11-09，未提交）

4t–4ab 已於本機提交（cced518）。4ac：斑馬線代理線需紅燈證據且排除橫向移動；斑馬線停留在主燈號綠燈後 3 秒內不報告、排除橫向駛入的車。
短片闖紅燈 11/3/4 → 11/1/4（只消失 2 筆既有誤報）；A5 事件 5→1、A1 41→7。請 Astra 在 Windows 以 `rule_eval` 重算 A5、A1、31 支短片確認，再進行 4aa。
