# 4ah：雙黃線漏報原因覆核（2026-11-11）

## 1. 任務與狀態

- 使用者要求：覆核跨越雙黃線的漏報原因。本輪完成分析與影像抽查，不修規則、不調門檻、不更換模型。
- Astra 分析，交 Opus 審查後續方案；狀態：分析完成、後續修正待決策。
- 基準 HEAD `1cc045a88bbad942c76c2fbaa80df2048a4e3520`，分支 `feature/conservative-marking-schedule`。
- 保留既有 4ad–4ag、Opus 展示工具／清理與所有未提交修改；未提交、推送或合併。
- 本次只改文件：AGENTS、refactor_log、兩份使用說明、handoff/README、本文件；分析腳本及輸出留在 `outputs/analysis/double_yellow_4ah`，不納入 git。
- 增量文件差異：`outputs/patches/4ah_double_yellow_review.diff`；套用基準為目前含 4ag 與 Opus 清理的工作目錄。

## 2. 變更與責任位置

| 位置 | 工作 | 性質 |
| --- | --- | --- |
| `outputs/analysis/double_yellow_4ah/audit.py` | 正式 run_rules 重播；讀取原函式 return 時的 locals、候選狀態前後與目標 ID 診斷 | 分析腳本，不改生產函式 |
| `recheck_geometry.py` | 保存遮罩 → 現行 attach_double_lines，逐幀比較幾何 | 隔離雙線後處理核對，非全套 FrameProcessor 重播 |
| `review_frames.py`、`review/` | 每事件 6 幀原圖與遮罩／足跡放大圖，共 8 組、16 張圖板 | agent 目視覆核，不改人工 GT |
| `detail_stats.py`、`make_report.py` | 支援範圍細分、狀態時序、逐片報表與雜湊 | 分析附件 |
| 本文件、refactor_log、操作說明 | 記錄歸因、限制與下一步 | 文件變更 |

沒有新增生產模組、類別、模型或未接線功能。分析的 instrumentation 只讀取原函式局部變數；回傳值及規則演算法保持原樣。

## 3. 介面交接

- schema 1.9、事件契約、預設值、confirmed、suspected、證據、快取語意、ID、去重、finalize 全不變。
- **尚未實作持久診斷欄位**。本次分支原因只在分析附件，正式診斷仍是 `unsupported_footprint_or_invalid_box`。
- Opus 後續需決定：「完整跨越」、「壓線」、「首次可見已占用錯側」是否應為不同候選類型。不能把缺少起點的情境宣稱為已觀察完整跨越，也不能因此放寬 confirmed。
- 目前 `area_geometry.clipped_vehicle` 是區域模組診斷，不是雙黃線規則的直接阻擋碼；跨模組統計不可混用。

## 4. 可重現環境

- Windows、既有 `.venv`、CPU。未載入模型權重、未執行 GPU 推論、未安裝套件；效能不是本次量測目的。
- 來源 `outputs/eval/baseline_18/cdy01` 至 `cdy07` 的完整 2,082 幀及逐幀 masks；原影片依 `eval/ground_truth.json` 路徑讀取，cdy07 在 Windows 能成功解碼。
- GT：7 支影片均 reviewed、8 筆雙黃線標註，其中 cdy05 有前／後鏡頭兩筆；GT 完全未改。目標 ID 只適用於此批輸出，非跨版本身份。
- 事件對照：`outputs/eval/motion_layers_4ag/<影片>/<影片>_violations.jsonl`，不是 baseline_18 原始舊規則輸出。
- 分析結果：`outputs/analysis/double_yellow_4ah/report.md`、`report.json`、`覆核結果.txt`；逐幀 trace、來源 SHA-256、影像及完整命令腳本同目錄。

```powershell
.venv\Scripts\python.exe outputs/analysis/double_yellow_4ah/audit.py
.venv\Scripts\python.exe outputs/analysis/double_yellow_4ah/review_frames.py
.venv\Scripts\python.exe outputs/analysis/double_yellow_4ah/recheck_geometry.py
.venv\Scripts\python.exe outputs/analysis/double_yellow_4ah/detail_stats.py
.venv\Scripts\python.exe outputs/analysis/double_yellow_4ah/make_report.py
```

## 5. 驗證與結果

雙黃線仍 **TP 1／FP 0／FN 7**。本輪沒有修正準確率。7 支完整重播得到 2 筆全部規則事件（雙黃線 1、斑馬線 1），與 4ag 僅排除選用的 motion_note 後，所有原事件欄位逐筆相同；逐片／逐事件雜湊在 report.json。其餘影片不在本次重播範圍。

另以目前 `attach_double_lines` 從保存遮罩重建，**2,082/2,082 幀 double_lines 完全相同**。因此本次診斷不受舊雙線幾何版本差異影響。這不代表所有後處理或重新推論結果不變。

生產程式與設定未改，依文件任務規範未重跑單元測試；不把 4ag 的 240 項歷史通過記作本次測試。沒有新的 GPU、端到端 FPS 或長片召回量測。

### 七筆漏報的直接證據

下表分母為各自標註時間窗幀數；「可量測」指至少一條可用線得到非 None 的目標位置，不代表已跨越。

`side=0` 表示內縮底邊的三個代理點未全部明確落在線帶同一側，包含跨帶、接近邊界與不確定容許範圍；文中的「在線帶」是規則位置分類的簡稱，**不是已證實輪胎壓線**。±1 也是相對線點序的幾何符號，不是合法／違法車道標籤。

| 事件 | 目標存在 | 運動無效 | 因運動整條規則停止 | 可量測 | 關鍵歸因 |
| --- | ---: | ---: | ---: | ---: | --- |
| cdy02 5.0–6.3 秒，ID369 | 39/40 | 5/40 | 0/40 | 34/40 | 初始已在線帶上；後續只有同一側。152／154 幀低信心會清除狀態，並非 150 幀交接失敗 |
| cdy03 3.9–6.5 秒，ID1 | 78/79 | 0/79 | 0/79 | 24/79 | 彎道／車下線段斷裂與有限支援；23 次 side=0、1 次 -1，沒有穩定起點 |
| cdy04 1.0–4.1 秒，ID87 | 82/94 | 25/94 | 0/94 | 77/94 | 遠處框小，早段多為線帶內；3.967 秒才 armed，整條 ID 沒有另一側 +1 |
| cdy05 前 5.8–8.8 秒，ID18 | 90/91 | 56/91 | 4/91 | 66/91 | 窗前原側僅 159／160 幀，約 0.033 秒，不足起點要求；遮擋／網狀線／幾何問題並存 |
| cdy05 後 11.7–18.0 秒，ID27 | 190/190 | 187/190 | 147/190 | 0/190 | 長運動失效；未硬阻擋時的 43 次量測仍全被遠端範圍不足拒絕 |
| cdy06 3.1–4.2 秒，ID12 | 34/34 | 7/34 | 0/34 | 4/34 | 遮罩污染到機車／車牌、線段不穩；另有壓線與完整跨越語意差距 |
| cdy07 1.0–5.0 秒，ID1 | 121/121 | 111/121 | 9/121 | 25/121 | 車身遮線／裁切、線段不穩與缺起點；111 次運動無效不等於 111 幀規則停止 |

合計目標存在 634/649 幀；不能把主要漏報概括成「沒偵測到車」。相同 ID 的缺席仍不等於整張影像沒有該車，未在此自動合併其他 ID。

### 分支拆解與時序

- 七筆漏報的 222 次 `unsupported_footprint_or_invalid_box`：**219 次 outside_line_support、3 次 endpoint_margin、0 次 invalid_bbox**。
- 219 次分成失敗點位於當次所用片段近端外 125、遠端外 94。屬每車每線呼叫，可同幀重複；不是 222 個互斥漏報幀，也不保證每條被拒片段就是目標下的實體線。
- 現有外插只允許被車遮住的遠端；近端外不外插。不能把所有支援失敗都當成調高遠端延伸比例即可解決。
- cdy02：4.367 秒首見 ID369，可量測時已 side=0；全 ID 的量測只有 -1／0。4.867 秒 armed，5.000 秒成功 handoff；5.067／5.133 秒車輛信心 0.28525／0.34847 低於 0.35，狀態被刪。5.267 秒可用線消失也取消 seeding。低信心保留是可試驗方向，但沒有另一側證據仍不能形成完整跨越。
- cdy05 前：5.300 秒首見 ID18，原側 +1 只有兩幀；5.367 秒 side=0 即取消 seeding。現行要求 3 次、0.15 秒原側穩定證據，並非單純在終點差一幀。
- cdy01 正例對照：3.233 秒 armed → 4.633 秒 crossing → 5.966 秒發布（事件時間 5.300 秒），保有兩側完整時序，缺口使狀態為 suspected。證明相同規則在起點、跨帶與終點可觀測時可成立。

### 原片抽查與限制

每筆標註均核對 6 張前後幀，另有遮罩／幾何／三個車底代理點。cdy03 車下及彎道標線分割中斷；cdy06 部分雙黃線遮罩延伸到車牌與車身；cdy05 後鏡頭車身及陰影下缺少線段支援。這些是 agent 抽查的可見問題，不是新增人工真值。

cdy04 遠處影像不足以精確標定輪胎跨線瞬間。cdy06 原 GT 已說壓線、未必完整跨越，本輪仍保留 FN 計數，不改答案。cdy07 車框裁切會降低位置可信度，但不能誤稱雙線規則直接因 clipped_vehicle 拒絕。

## 6. 整合決策與回退

建議順序：

1. **最低風險：持久診斷拆分。** 保存三種 footprint 原因、支持端點／失敗點、低信心與無可用線清除前的 phase／時間。預設關閉，正式重播輸出事件須逐位元相同。
2. **有限線段與足跡支援。** 優先研究 cdy03／cdy06 的彎道、車身分割污染，以及 cdy05／cdy07 的遮擋端點。使用可靠歷史與局部曲線一致性；不要全域加大外插。增加可量測幀不等於追回事件，須跑所有影片及誤報反例。
3. **另定義疑似細項。** 首見已在線上、壓線與錯側占用需要獨立語意；不得當作完整跨越證據，不放寬 confirmed。此決策交 Opus／使用者確認任務定義。
4. **後視鏡頭與運動。** cdy05 後段須分析切鏡、光照與 poor_spatial_coverage；單純把 1 秒容忍延長，仍有零可用足跡的問題，還會累積錯誤幾何。
5. **短暫低信心候選保留實驗。** 對 cdy02 不讓低信心增加證據，只在既有缺口上限內保留候選、標記缺口，最多 suspected；需正常碰線返回、錯 ID、連續低信心反例，且不能承諾單項就補回該片。

無生產程式可回退；移除本次分析文件即可，勿還原其他 agent 工作。操作說明兩份、AGENTS、refactor_log、handoff 索引同步；未獲推送／合併授權。

後續 Opus prompt：

> 讀 AGENTS、ASTRA、4ah 交接及 outputs/analysis/double_yellow_4ah/report.md。保留 4ad–4ag 與展示／清理修改。先做預設關閉的雙線持久診斷拆分，不改規則回傳值；以 7 支 cdy 完整重播逐位元比對事件，並跑完整測試。重點案例：cdy02 f150 handoff 成功，f152／154 因低信心清除；cdy05 前段只有 f159／160 的原側，不能補造起點；cdy05 後段 147/190 幀硬阻擋但其餘量測亦全失敗；cdy06 壓線語意須獨立處理。診斷完成後單獨交接，不一起放寬幾何／confirmed。GT 不改；任何壓線或錯側新候選先提介面與規則語意。
