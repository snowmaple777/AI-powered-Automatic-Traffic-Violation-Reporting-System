# 4ai：雙線持久診斷拆分（Astra，2026-11-11 實作／11-12 完成交接）

## 1. 任務與狀態

- 需求：將 4ah 依賴 Python trace 的拒絕分支、候選清除與側別觀測改為可關閉的正式診斷，不改判定。
- 範圍：雙黃／雙白線規則、共用幾何函式的可選診斷參數、三個入口、測試及文件。未做曲線延伸、線段記憶、車身遮罩排除、壓線／錯側占用新類型或 4aa 尺度實驗。
- 負責：Astra；交接 Opus。狀態：實作與驗證完成，未提交，待 Opus 審查／整合。
- 基準：`feature/conservative-marking-schedule`，HEAD `1cc045a88bbad942c76c2fbaa80df2048a4e3520`，工作目錄含既有未提交 4ad–4ah、Opus 清理與展示工具。
- 交付：未提交；增量差異 `outputs/patches/4ai_double_line_diagnostics.diff`，15 個檔案的前後雜湊及隔離副本套用驗證見 `delivery_manifest.json`。修改前快照及 SHA-256 在 `outputs/analysis/double_diagnostics_4ai/before/`、`before_hashes.json`，完整既有 dirty diff 為 `before_worktree.diff`。
- 不 reset／還原／提交。以下差異只針對本次開始時的工作目錄；不把既有修改混入 4ai。

## 2. 變更與責任位置

| 檔案／函式 | 原問題 | 修改後行為 | 性質 |
| --- | --- | --- | --- |
| `double_lines.py:vehicle_line_position` | `None` 無法區分原因 | 既有回傳不變；可選 out-dict 記錄拒絕分支與距離 | 診斷新增 |
| `violation_engine.py:DoubleLineCrossingRule` | 清除不留原因、僅有存活候選 phase | 詳細位置、清除前摘要、各側量測統計；不寫進判定 state | 診斷新增 |
| `configs/double_line_rules.json` | 無開關 | `detailed_diagnostics: false` | 預設不變 |
| 三入口與 `write_final_double_line_diagnostics` | 片尾清除未保存 | 開啟才於既有診斷檔補寫 finalize 列 | 診斷新增 |
| `tools/rule_eval.py:rule_options/main` | 只能讀預設雙線設定 | 新增 `--double-line-config`，與另外兩個入口同一設定讀取函式 | 可選介面 |
| `tests/test_double_line_diagnostics.py` | 無上述契約測試 | 原因碼、生命週期、開關與三入口一致性，共 12 項 | 驗證 |
| `tools/batch_eval.py`、`tests/test_batch_integrity.py` | 片尾診斷會被當成額外影格 | 彙總略過 finalize 列；完整性檢查只容許完整幀序之後的一筆合法片尾列，拒絕截斷、重複或後續資料 | 消費端相容 |

沒有新增生產模組或類別。新測試獨立成檔以集中診斷契約。模型結果 → 原後處理 → 原規則 → 既有診斷檔已接通；事件、影片疊圖及剪輯仍讀原事件。

## 3. 介面交接

| 欄位／函式／設定 | 生產端 | 消費端 | 契約 | 相容方式 |
| --- | --- | --- | --- | --- |
| `detailed_diagnostics` | JSON 設定／options | 雙線規則 | 僅 bool，預設 false | 建構後從 `settings` 移出，不進事件 `evidence.parameters` 或舊診斷 settings |
| `vehicle_line_position(..., diagnostics=None)` | 幾何拒絕分支 | 雙線規則 | 可選 keyword-only dict；失敗填原因，成功不填 | 原回傳值、四個位置參數與判定算式不變 |
| `rules.<雙線>.details` | 規則 evaluate | 逐幀 JSONL／離線分析 | `version:1`；`position_failures`、`positions`、`state_changes`、`candidates` | 原彙總欄位及合併碼保留；關閉完全不產生 |
| `finalize:true` 列 | 共用片尾 writer | 三入口診斷檔 | 使用最後幀 index，只含啟用規則的 `frame/state_changes` | 僅開啟新增一列，不代表新影格；逐幀統計須略過此列 |

觀測 schema 保持 **1.9**；不新增事件欄位。關閉時不建立側別歷史、不寫片尾列。suspected／confirmed、條件旗標、事件 ID、時間、去重、片尾事件均不變。三入口共用設定解析、規則及片尾 writer。

### 車底拒絕與距離

- `invalid_bbox`：`bbox_reason` 另分 `conversion_failed`／`invalid_shape_or_extent`。無合法框時無法計算車底，點／距離為 null。
- `outside_near_support`／`outside_far_support`：依實際使用線段的影像 y 近端／遠端判別。
- `endpoint_margin`：原有限投影與列投影回退後仍不足端點安全距離。
- `unsupported_line_projection`：投影仍不可用、但點不在上述兩端之外，例如非單調線段；不假裝是範圍不足。
- 保留原 `skipped_vehicles.reason=unsupported_footprint_or_invalid_box`。每次呼叫只記第一個被拒絕的車底點，遵守原提前 return 次序；每車每線可能有一筆，同幀可多筆。
- `failed_point`、`line_endpoints` 是當次使用的原圖像素座標；`original_line_endpoints` 保留外插前端點，`extrapolated` 標示當次是否已外插。
- `support_gap_px` 是點到原線段 y 範圍的不足量；`required_extension_px` 額外包括原程式 `endpoint_margin_px + 1` 的 y 安全墊，比例除以車框高度。這是幾何描述，不保證延伸後通過、不授權近端外插，也不是規則的新閾值。
- `extension_limit_ratio` 為原設定；`extension_limit_px=ratio×框高+安全墊` 是原外插函式使用的預算算式。ratio=0 表示外插停用，不能把安全墊解讀成允許外插；`extension_policy=occluded_far_endpoint_only` 明示只限被車身遮住的遠端。

### 候選生命週期與側別

`state_changes` 在原刪除／clear／reset／轉移前快照，包含 object_id、line_id、phase、origin、elapsed_seconds、before_count、after_count、destination_count、destination_seconds、since_last_observation_seconds、sides，以及 reason/action。低信心附實際 `confidence`。

- 刪除：`motion_unavailable`、`discontinuous_input`、`line_unavailable`、`observation_gap`、`vehicle_missing`、`projection_invalid`、`low_confidence`、`invalid_vehicle`、`position_unavailable`、`box_jump`、`origin_not_stable`、`crossing_timeout`、`finalize`。
- 重設：`returned_to_origin`、`rearmed`，`action=reset`。
- 線段換號：`line_handoff`，`action=transferred`，帶 `new_line_id`，摘要跟隨原本實際成功的轉移。車輛換號沒有重辨識機制，只按原缺失／間隔分支記錄，不能猜測兩個 ID 是同一車。
- `elapsed_seconds` 是距目前 origin_since 的經過時間；`before_count` 是既有滑動視窗樣本數，不是整片累計。不得把這些值當確認成立的新證據。
- `sides` 是同車同線量測段的 first/last side、frame、timestamp，以及 `counts/seconds`（鍵 `-1`、`0`、`1`）。它可以包含較早候選的觀測，不等於目前 phase 的有效證據。各側 seconds 只累加連續幀且相同側別的時間間隔，不跨低信心／快取／無足跡的缺口；仍可包含原規則允許的運動近似／外插位置。
- 每次幾何成功即寫 `positions`，包括 side=0 尚未形成候選的情況；box_jump 等後續規則拒絕可在同幀 state_changes 查到。整個 ID 的側別應彙總逐幀 positions，而非把多次累計摘要相加。多條線同幀是多筆量測，唯一幀數需去重。
- 歷史只保留活躍候選或最近 max_observation_gap_sec 的量測段；間隔過長／時間倒退後新量測重新計數。先保存被清除候選摘要，再丟棄過期歷史，避免清除理由存在而證據遺失。記憶體不依整片 ID 數增長。

需 Opus 留意：啟用詳細診斷的外部讀取端要識別 finalize 列；專案內 batch_eval 的彙總與完整性讀取已同步，舊彙總原因不變。事件讀取端無須遷移；不申請規則語意或預設值變更。

## 4. 可重現環境

- Windows／專案 `.venv`，Python 3.10.11、NumPy 1.26.4，僅 CPU 重算保存觀測；沒有載入模型／權重、GPU 推論、重新解碼或後處理。模型 provider／權重 SHA 不適用本輪，不能引用本輪時間為完整辨識 FPS。環境及 GT 雜湊另記於 delivery_manifest.json。
- 短片：`outputs/eval/baseline_18/*/*_detections.jsonl` 全 31 支，9,411 幀。長片：`outputs/analysis/long_video_20261108/run_01/phase1/{A5,A1}` 完整觀測，A5 18,174 幀、A1 47,396 幀。合計 74,981 幀；保留逐幀原 timestamp/FPS。
- 設定：現行所有規則＋原設定，車牌補充秒數 0、motion_notes false；僅詳細診斷開關不同。`enabled.json` 是預設設定的完整副本，只將 detailed_diagnostics 改 true。
- GT 沿用現有檔案，只讀取 4ah 的 7 筆漏報標註窗；不改 reviewed、不重標註。未使用遮罩重建幾何。
- 所有結果：`outputs/analysis/double_diagnostics_4ai/{legacy,off,on}`；正式 CLI 接線檢查：`outputs/eval/double_diagnostics_4ai_cli/cdy02`。

```powershell
$env:PYTHONIOENCODING='utf-8'
$env:PYTHONHASHSEED='0'
.venv\Scripts\python.exe -m unittest discover -s tests
.venv\Scripts\python.exe outputs/analysis/double_diagnostics_4ai/replay.py legacy
.venv\Scripts\python.exe outputs/analysis/double_diagnostics_4ai/replay.py off
.venv\Scripts\python.exe outputs/analysis/double_diagnostics_4ai/replay.py on
.venv\Scripts\python.exe outputs/analysis/double_diagnostics_4ai/reproduce_4ah.py
.venv\Scripts\python.exe outputs/analysis/double_diagnostics_4ai/verify.py
.venv\Scripts\python.exe tools/rule_eval.py --source outputs/eval/baseline_18 --label double_diagnostics_4ai_cli --only cdy02 --double-line-config outputs/analysis/double_diagnostics_4ai/enabled.json
```

`replay.py` 呼叫正式 `tools.rule_eval.run_rules`，只在 evaluate 外側計時與每千幀輸出 progress.json；沒有 trace 或修改決策。完整來源／產出 SHA-256、逐片事件數、逐片時間在 result.json 及 comparison.json。逐片比較表見分析目錄 report.md。

## 5. 驗證與結果

- 修改前一般 Windows 完整測試 **240 項 OK，62.981 秒**。沙箱初次因 Windows 暫存檔原子替換 WinError 5 出現失敗，於一般 Windows 重跑確認後才開發。
- 修改後既有雙線相關測試 32 項通過；新增診斷測試 12 項及批次片尾相容 1 項。最終一般 Windows 完整 **253 項 OK，56.417 秒**，見 `full_tests_final.txt`。
- 中途回歸發現 Mock 被當作片尾 dict，已限制 writer 只處理正式 dict；另補間隔清除摘要檢查時，測試誤將新量測的側別寫死，已改查新量測段總數為 1。最終測試涵蓋修正，不以早期失敗輸出作交付依據。
- 合成三入口測試使用同組觀測與真正規則，僅替換模型／影片 I/O：run_pipeline、evaluate_violations、run_rules 的事件及診斷 bytes 相同，ON/OFF 事件 bytes 相同。正式 CLI 的 cdy02 自訂設定已跑通，與來源的事件差異為 0。
- 初次未固定 PYTHONHASHSEED 的比較，事件相同，但部分診斷 `line_handoffs` 字典的鍵順序不同；屬原程式 set 迭代隨機化。保留在 `uncontrolled_seed/`，不作逐位元及效能結論。正式三組均以 `PYTHONHASHSEED=0` 重跑全部 33 片，沒有為了比較而修改原排序或幾何邏輯。
- A1 耗時為單次 OFF→ON，CPU 時間量測同一行程的 ViolationEngine.evaluate；牆鐘不等於 GPU 推論時間。正式三組依序執行，沒有同時跑單元測試／模型；仍受系統排程、快取與 CPU 頻率波動限制。

正式三組各完成 **33 支、74,981 幀、31 筆事件**：31 支短片 23 筆、A5 1 筆、A1 7 筆。
ON/OFF 的 `_violations.jsonl` 全檔逐位元相同，不排除任何欄位；OFF 與修改前快照重算的事件及診斷也全部逐位元相同。
ON 每幀除新增 details 外，原彙總欄位逐項相同；另外只增加一筆片尾 finalize 診斷。
逐片表及全片 SHA-256：[`report.md`](../../outputs/analysis/double_diagnostics_4ai/report.md)、[`comparison.json`](../../outputs/analysis/double_diagnostics_4ai/comparison.json)。
未另重算準確率或車牌指標；本輪用全部事件 bytes 等價確認未新增／刪除／改動事件，沒有新增命中或誤報可宣稱。

| A1 全 47,396 幀 | OFF | ON | 差值 |
| --- | ---: | ---: | ---: |
| 規則 evaluate CPU 秒 | 977.422 | 983.953 | +6.531（+0.67%） |
| 規則 evaluate 牆鐘秒 | 1,076.247 | 1,069.368 | −6.880（−0.64%） |
| 全部重算秒，含 JSONL I/O | 1,116.002 | 1,115.185 | −0.817（−0.07%） |
| 診斷檔 bytes | 321,754,182 | 602,911,814 | +281,157,632（+87.38%） |

規則計時包含五項規則的 evaluate，不含 JSON 序列化、檔案 I/O、模型、影片或後處理。增加的 CPU 時間約每幀 0.138 ms；
牆鐘的小幅下降不能解讀為開啟診斷加速。這是單次量測，無重複樣本可估信賴區間；較明確的成本是診斷檔由約 322 MB 增至 603 MB（十進位）。

不用 trace 重現 4ah（逐筆結果在 `reproduced_4ah.json`）：

- 7 筆漏報標註窗：近端外 125＋遠端外 94＝219 次範圍不足，端點太近 3 次。
- cdy02 f152／154：low_confidence 清除，0.28525／0.34847；清除前分別 armed／seeding。
- cdy02 全 ID 為 -1／0／+1：52／5／0；cdy04 為 21／62／0，均無另一側。
- cdy05 前段 +1 只有 f159、f160。原側不足的結論不變。
- 全 33 片、兩項雙線規則的位置拒絕及候選變動原因統計在 report.md，不能將每車每線筆數當違規數或互斥幀數。

未執行 GPU、模型／遮罩重建、人工重新標註或 4aa。歷史 4ah 的圖板與 trace 只作結論對照；本次所有重現數字均由新診斷讀出。

## 6. 整合決策與回退

- 建議保留為診斷工具，**預設 false 不變**。這輪沒有追回漏報或改善準確率，不以更多診斷記錄冒充改善。
- 回退：將設定 detailed_diagnostics 設 false 或移除即回到舊輸出。若移除本輪程式，僅按增量差異逐塊處理，保留 4ad–4ah／Opus 既有未提交內容；不要 reset 工作目錄。無權重變更。
- 同步文件：AGENTS、usage、使用說明、refactor_log、handoff README；schema 無變更。
- 使用者未授權提交／推送／合併，全部停留本機。完成本交接後停止，不執行 4aa 或幾何修正。

給 Opus 的接續 prompt：

> 先讀 AGENTS、ASTRA、TEMPLATE、本交接與 outputs/analysis/double_diagnostics_4ai/report.md。工作目錄含既有 4ad–4ah、清理／展示工具及 4ai，全部保留。先覆核增量差異與 33 片事件／診斷相容證據，注意詳細診斷 finalize 列、側別量測段與候選有效證據的差異。這輪不改預設、規則語意或 confirmed。若另獲授權研究曲線延伸／線段歷史，另開一個編號與交接，使用新診斷定位近端／遠端差異並完整驗證短片及長片誤報。cdy06 壓線、錯側占用仍待使用者定義，不能自行轉成新事件。4aa 需獨立授權後再做。
