# 4aw：確認規則分階段實驗（2026-11-16–17）

> 4bh 更新（2026-11-21）：R2＋R3、斑馬線確認＋8px、停等區佔用確認已納入 `configs/*_rules.json` 預設；紅燈時段、運動失效確認、停等區車頭方向確認與雙線短缺口已移除，下文的選用設定檔與 stage2／3／5 manifest 已刪除（保留於 commit 3598167）。見 refactor_log 4bh。

## 任務與基準

使用者於 2026-11-16 授權依提案逐階段實作，要求量測實際收益並檢查新增誤報；
包含重新定義確認條件的實驗，不把先前「放寬僅疑似」當作禁止本次實驗的理由。
所有事件仍需人工覆核。沒有修改 GT、模型、感知觀測或生產預設，也沒有提交、推送或合併。

- 起始分支：feature/conservative-marking-schedule；HEAD e3873b3b2379ba95ad3316ed6c299b920feea9ce，起始工作目錄乾淨。
- 正式集合：outputs/eval/eval_4aj，31 支；eval/ground_truth.json。
- 粗略集合：outputs/eval/new_20261114，141 支；outputs/rough_truth_20261115.json。
  只評估各片主類別，其他類別事件保持未標註；排除 .attempts。
- 長片：outputs/analysis/long_video_20261108/run_01/phase1，A1／A5，約 36.45 分鐘；
  eval/long_video_review.json 只有部分人工覆核，不能將其餘時間當成完整負例。
- 全部使用既有 detections JSONL，CPU 重算，不重跑 GPU 感知。

## 實作與介面

### 1. R2＋R3

沿用 4av 原實作，新增可直接用於三個入口的 configs/red_light_confirm_r23.json。
主設定 configs/red_light_rules.json 不變，R4 保持關閉。

### 2. 停等區方向路徑

area_rules.py 選用 confirm_waiting_area：
直接區域、可見接地點、有效鏡頭補償、幾何四向擾動後仍侵入、強紅燈、可支持的車頭方向。
未標定車道時，須有道路補償後至少 0.3 秒的向前軌跡，前進超過 max(4px, 車寬×0.25)，
水平位移低於前進量的一半；不能靠預設「車頭朝遠方」確認。
直接觀測至少 3 次、首末至少 0.5 秒，直接樣本間距不能超過 max_observation_gap_sec。
未配對車道時還要求當幀直接強紅燈、同一燈號身分、沒有信心 ≥0.18 的綠／黃反證。

### 2b. 停等區深入占用路徑

另設 confirm_waiting_occupancy，測試是否可用不依賴車頭方向的強占用證據：
接地多邊形在四向誤差擾動後都至少 80% 位於區域內；最近一秒的有效軌跡至少 0.8 秒，
道路補償位移不超過 max(3px, 車寬×0.1)、車寬變化不超過 15%、排除最近三秒的橫向移動；
其餘直接觀測、紅燈、遮擋與時間條件同方向路徑。
它確認的是持續占用，不反推看不到的駛入過程。沒有車道／號誌歸屬模型，仍有跨車道風險。

### 3. R1 保守紅燈時段

violation_engine.py 選用 confirm_signal_episode：只橋接單一直接紅燈軌跡的短暫換號。
幀間隔 ≤0.2 秒、位置相容、尺度比 0.5–2、不能有其他當幀號誌軌跡或綠／黃反證；
不連續輸入、長缺口或顏色衝突切斷時段。weak／held 不增加直接紅燈證據。
只替代 same_signal，其餘確認條件不變，且與 R2／R3／R4 合計仍最多一條替代路徑。
這是完整「行進方向—號誌—紅燈時段」設計中可用現有觀測先驗證的子集；
尚未實作可靠的車道—號誌歸屬，也未將後來出現的紅燈反推成先前一直紅燈。

### 4. 斑馬線確認

area_rules.py 選用 confirm_crosswalk：保留 4au 候選條件，另要求可見接地點、有效運動、
直接區域觀測、四向誤差擾動仍重疊 ≥原 overlap_ratio，沒有綠燈與橫向反證。
至少 3 個直接樣本，首末 ≥1.5 秒；檢查整個候選起點的累積漂移 ≤max(4px, 車寬×0.2)，
及有尺度資訊時的全段車寬變化 ≤20%，避免只看短視窗而確認緩慢爬行。

4b 額外對照 confirmation_min_footprint_px=8：足跡在影像中的短邊至少 8px，
即絕對像素擾動下限 2px 的四倍。這是未校準的保守解析度閘門，不是可信度機率。
動機是 CIM019 遠處車輛的估計足跡只有約 7px 高，穩定小位移不足以支持確認；
SOZ019／020 的足跡約 27／29px 高。門檻沒有依車牌、影片 ID 或標註時間分支，
仍須以全部短片、跨類影片與長片驗證；不把這一輪資料視為獨立泛化驗證。

### 5. 雙線完整跨越的短缺口

double_lines.py／violation_engine.py 選用 confirm_short_position_gap：
僅當其餘完整跨越確認條件都成立，位置缺口兩端間隔 ≤0.2 秒時，替代 uninterrupted_positions。
仍須實際看見線帶過渡、穩定原側與另一側、直接雙線、直接標線、無外插、無換線與無運動失效。
不新增「首次看到已在另一側就是跨越」推論，也不將既有 line_straddle 疑似偷換成完整跨越。

### 輸出與發布

- 區域實驗保持原 event_id、frame、timestamp_sec、原始 conditions 與候選證據；
  升級另加 confirmation_basis、confirmation_frame／confirmation_timestamp_sec、
  evidence.confirmation_review（確認當下車輛／區域、直接幀與檢查結果）。
- 區域候選至多等待事件後 3 秒，確認即釋出；逾時保留疑似；片尾 flush 一次。
  試作初版曾保留至片尾，為避免影響跨規則合併已撤換；stage2_window 才是正式方向路徑結果。
- confirmed 仍可保留表示原條件不足的 false，例如 same_signal 或 uninterrupted_positions。
  消費端必須讀 confirmation_basis，不能對全部 conditions 做 all()。
- 所有新實驗選項預設關閉；明確傳 false 也不進入既有區域／雙線事件的參數。
  observation schema 未變，既有三個入口的設定讀取可直接使用這些選項。

## 評估工具與重現

tools/confirmation_eval.py 每支影片只讀一次觀測，各變體使用獨立引擎。
可用 --workers 4 讓不同影片的 CPU 規則重算並行；沒有同時量測 GPU 速度。
manifest.json 記錄展開後設定、原始 manifest 與核心程式 SHA-256，git.json 記錄 HEAD／dirty；
每支影片 done.json 記錄輸入大小／mtime 與片長。--resume 拒絕程式／設定或已完成輸入變動。
report.json 分開列全部候選與 confirmed 的 TP／FP／FN、總數、每小時事件與已知長片誤報。
tools/confirmation_review.py 產生新增確認的前中後三幀圖板，不修改人工標註。

```powershell
.venv\Scripts\python.exe tools/confirmation_eval.py eval/confirmation_experiments/stage1_r23.json --out outputs/confirmation_retest/stage1 --workers 4
.venv\Scripts\python.exe tools/confirmation_review.py outputs/confirmation_retest/stage1 --variant r23
```

其餘階段的 manifest 同目錄 stage2_waiting、stage2b_occupancy、stage3_signal、stage4_crosswalk、stage4b_crosswalk_resolution、stage5_double_line。
執行前須有上述未進 git 的觀測與粗略標註。只跑列出的規則；闖紅燈／停等區階段一起跑，以包含合併影響。
其他規則階段分開重算，不能把報告中的局部事件率當全部規則事件率。

## 結果與決策

七組對照均完成 31＋141＋2 支影片（每組 174 支）。以下 TP／FP 都是 **confirmed 子集**，
不是所有候選的指標；粗略集合只計影片主類別，跨類確認另列。

| 階段 | 正式集合 confirmed TP／FP | 粗略集合 confirmed TP／FP | 決策 |
| --- | --- | --- | --- |
| R2＋R3，闖紅燈 | 3／0 → 5／0 | 5／2 → 7／2 | 有收益，提供選用設定；R4 不啟用 |
| 停等區方向路徑 | 0／0 → 0／0 | 0／0 → 0／0 | 無收益，不採用為預設 |
| 停等區深入占用 | 0／0 → 0／0 | 自動配對 0／0 → 1／0，但對到非標註車輛 | 沒有可認列的已驗證收益，不採用 |
| R1（以 R2＋R3 為基準） | 5／0 → 5／0 | 7／2 → 7／2 | 無額外收益，不採用 |
| 斑馬線原確認實驗 | 0／0 → 0／0 | 0／0 → 2／0，另有 2 筆跨類疑義確認 | 原版不採用 |
| 斑馬線＋8px 足跡門檻 | 0／0 → 0／0 | 保留 2／0，兩筆跨類疑義退回疑似 | 小樣本有收益，提供選用設定 |
| 雙線短缺口 | 0／0 → 0／0 | 0／0 → 0／0 | 無收益，不採用 |

所有階段 A1／A5 都沒有新增 confirmed。長片的闖紅燈＋停等區維持 6 筆疑似（9.88 筆／小時）、
斑馬線維持 2 筆疑似（3.29 筆／小時）、雙黃線 0；合計 8 筆疑似、約 13.17 筆／小時。
已知誤報殘留：闖紅燈 2 筆、斑馬線 1 筆，各組不變。這是**事件率**及已知誤報殘留數，
因長片未完整覆核，不能稱為完整誤報率，也不能宣稱沒有其他誤報。

全部候選的 TP／FP／FN：
- 正式集合四規則不變：闖紅燈 12／1／3、停等區 5／0／2、斑馬線 1／3／3、雙黃線 1／0／7。
- 粗略集合 R2＋R3 讓闖紅燈 12／4／32 → 13／4／31；其餘不變：
  停等區 22／8／19、斑馬線 15／5／17、雙黃線 1／0／57。
- 選用確認主要提升既有候選的證據等級，不能把它描述為大量補回漏報。

### 逐件覆核與風險

- R2：ms05 6.624 秒、vr005 10.885 秒升級；vr017 約 6.98 秒原本因後續角落綠燈否決而取消，
  現在較早確認，成為新增 TP。R3：s01 原 0.767 秒疑似改在約 2.33 秒通過遠端時確認。
  前中後圖板與各自正式／粗略 GT 的目標相符。新集合既有 confirmed FP 仍為 vr002／vr011，
  未在本輪改標註或宣稱已消除。
- R2 另新增 CIM015 約 8.2 秒的機車事件；目視可見紅燈與向前移動，但它不在主類別粗略 GT 中，
  保留為跨類未標註事件，沒有計為已驗證收益。
- 停等區深入占用只升級 CIM016 12.022 秒（確認約 13.808 秒），目標是右側黑車 vehicle-000163，
  **不是 GT 的白色 Toyota 8969-L5**。預測兩車都沒有車牌，現有時間／車種配對會把黑車算成 TP。
  這不是可認列的「TP＋1」；黑車是否另有違規仍未獲人工 GT 確認。多幀與高重疊不能解決車道歸屬、
  區域範圍與接地估計的系統性偏差，因此不採用這條路徑。
- 斑馬線原版共升級 4 筆：SOZ019 3.632 秒（約 5.179 秒確認）、SOZ020 2.251 秒（約 3.615 秒確認），
  以及 CIM019 20.42 秒（約 21.088 秒確認）、vr021 7.8 秒（約 8.333 秒確認）。
  SOZ019 黑色 Mazda BPG-8836、SOZ020 紅色 Jaguar BPJ-3918 與 GT 目標一致，畫面支持停在斑馬線上。
  CIM019 是路口遠處車輛，車道與停止狀態難確認；vr021 是遠處路邊車，未見位於前景斑馬線，疑似誤報。
  8px 閘門保留兩筆 GT 命中，CIM019／vr021 均退回疑似，沒有新增其他確認。
- 雙黃線 cdy01／cy045 原本的兩筆疑似都不只缺位置連續性；還缺直接雙線／標線等證據。
  單獨補短缺口不能升級，也沒有增加候選。接地量測與分割仍是瓶頸。

圖板與逐件紀錄在 outputs/confirmation_20261116/visual_review.json、各階段 review/。
這些是 agent 目視覆核，不冒充使用者人工 GT；正式與粗略標註均未修改。
8px 是本輪針對可解釋風險提出並驗證的工程門檻，仍需獨立影片檢驗，沒有宣稱完成泛化驗證。

### 可使用的設定與採用邊界

主預設 configs/red_light_rules.json、configs/area_rules.json、configs/double_line_rules.json 都不變。
有收益的兩條路徑提供獨立設定，可單獨或一起傳給既有入口：

```powershell
--red-light-config configs/red_light_confirm_r23.json --area-rule-config configs/area_confirm_crosswalk.json
```

前者只開 R2／R3；後者保留 4au 候選設定並開 confirm_crosswalk＋confirmation_min_footprint_px=8。
停等區、R1、雙線短缺口實驗保留為預設關閉且有測試的選項，不納入建議設定。
confirm_waiting_area 與 confirm_waiting_occupancy 應各自評估；同開時使用深入占用路徑。

### 驗證與輸出

- 結果根目錄：outputs/confirmation_20261116/。
  正式結果是 stage1_red、stage2_window、stage2b、stage3、stage4、stage4b、stage5 各自的 report.json。
  stage1／stage2 是中止或被取代的初版，不拿來做結論。
- 設定關閉的相容性：R2＋R3 在 stage1_red 與 stage2_window 的 174 支輸出逐筆完全相同。
  各項完整基準比較與標註 SHA-256 存 equivalence.json；現存批次輸出比較只排除工具補的頂層 plate。
- 新增測試涵蓋直接／推移觀測、遮擋、重疊誤差帶、慢速爬行、方向、深入占用、3 秒期限、
  微小足跡、燈號換號／反證／缺口、雙線缺失關鍵過渡、評估規則範圍與跨類未標註事件。
- 最終完整測試 314 項通過（274.016 秒），紀錄 tests_final_resolution.log。
- 三支真實 JSONL 以全部規則及兩份選用設定通過入口煙霧測試：ms05 為 line_handoff_direct、
  s01 為 crosswalk_crossed_completely、SOZ019 為 robust_area_temporal_evidence 且 geometry_resolved=true。
  七組完整對照按階段執行；兩份設定一起使用只有上述入口驗證，沒有宣稱完成合併設定的 174 支完整重算。

沒有更換模型或取樣設定，沒有新增推論階段，沒有量測或宣稱 FPS 改善。

## 整合結果（Opus，2026-11-17）

- **重驗方式：** 以獨立腳本與全規則 rule_eval 重驗闖紅燈部分，數字與本報告一致。
- **預設關閉：** 兩組集合與 4av 基準逐位元相同。
- **提交範圍：** 只提交 stage1–5，stage6 斑馬線邊界實驗仍在進行，未提交。
- **細節：** 見 refactor_log「4aw 整合重驗」。
- **回退：** 主預設未變，不傳兩份選用設定即為原行為；整段回退可 revert 該提交。
