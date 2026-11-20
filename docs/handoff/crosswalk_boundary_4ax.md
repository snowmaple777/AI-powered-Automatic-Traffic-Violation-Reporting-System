# 4ax：斑馬線幾何可靠性與長片對照（2026-11-17）

## 範圍與基準

使用者要求先實作斑馬線改進、比較改動前後，並分析長片是否增加誤報。
起始分支 feature/conservative-marking-schedule，HEAD e3873b3b2379ba95ad3316ed6c299b920feea9ce；
工作目錄包含本聊天 4aw 未提交變更，全部保留。本輪沒有執行提交、推送或合併。
評估期間 Opus 將 4aw 整合為 0aee18c，明確保留本輪 stage6 工作目錄變更；收尾核對評估保存的六份核心程式
SHA-256 與目前檔案全部相同，因此無須因提交編號前進重跑相同程式。

這次是現有區域多邊形的規則層可靠性檢查，沒有重建條紋、修改凸包、鏡頭補償、停止判定或模型。
A1／A5 只有逐幀觀測，沒有完整逐幀標線遮罩，因此不把本輪宣稱為完成條紋邊界重建。
使用同一批觀測，在每支影片內以四個獨立引擎對照，排除 GPU 感知差異。

- 正式集合 31 支：outputs/eval/eval_4aj，eval/ground_truth.json。
- 粗略集合 141 支：outputs/eval/new_20261114，outputs/rough_truth_20261115.json；只評主類別，排除 .attempts。
- 長片 A1／A5：outputs/analysis/long_video_20261108/run_01/phase1，合計 2187.247 秒（36.45 分鐘）。
- **改動前**使用 4aw 選用設定 confirm_crosswalk=true、confirmation_min_footprint_px=8；
  不是把 4aw 確認收益計為這次新增收益。主預設仍不啟用確認，且本輪沒有改主預設。
- 本輪 baseline 與 4aw stage4b/resolved 的 174 支事件 JSON 完全相同。
- 四組均完成全部 174 支，沒有用事件附近片段代替完整長片。

## 實作

area_rules.py 新增兩個選用設定，未指定時行為不變：

1. crosswalk_boundary_guard：沿用 4aw 誤差定義 max(2px, 車寬×3%)，向左右上下擾動接地多邊形，
   每個位置仍須符合原重疊門檻。分別計算原區域與既有遮擋延伸的最小重疊；
   direct_min_overlap 成立記 interior，只有延伸成立記 occlusion_estimated，其餘記 boundary_uncertain 並拒絕累積。
   interior 只是對估計多邊形的位置穩健，不代表真實條紋內部或直接觀測。
   原直接觀測／推移／預測來源與確認檢查仍保留；遮擋延伸不增加直接證據。
2. crosswalk_min_footprint_px：接地多邊形影像座標 x／y 跨度中的較小值須至少達指定像素。
   本輪固定 8px，沿用 4aw 對確認採用的值，擴展到候選資格；不是距離門檻或校準過的可信度。
   不符合時記 crosswalk_footprint_unresolved，不建立或延續本幀候選。

兩者都只作用於斑馬線分支，不改其他規則的判定；共用區域設定仍會出現在停等區事件的 parameters 中。
邊界開關顯式 false 會從參數移除，保持關閉時輸出相容。
新資料只在診斷與事件 evidence.boundary_review／before、after 快照，不修改觀測 schema。
缺口、去重與重新累積仍沿用原流程，因此不能僅憑「收緊」就推論事件必為原集合的子集，必須完整重算。

## 四組結果

以下 TP／FP／FN 是全部候選；confirmed 另列。粗略集合不將跨類未標註事件算成誤報。

| 設定 | 正式 TP／FP／FN | 粗略主類別 TP／FP／FN | 粗略 confirmed TP／FP | 粗略全部事件（含跨類） | 長片事件 |
| --- | --- | --- | --- | --- | --- |
| baseline（4aw） | 1／3／3 | 15／5／17 | 2／0 | 45 | 2 |
| 邊界檢查 | 1／3／3 | 10／3／22 | 3／0 | 31 | 2 |
| 僅 8px 候選檢查 | 1／3／3 | 15／5／17 | 2／0 | 36 | 1 |
| 兩者合用 | 1／3／3 | 10／3／22 | 3／0 | 26 | 1 |

正式集合 confirmed 均為 0；長片均無 confirmed。
邊界檢查雖少兩筆評分 FP、增加一筆確認，卻損失五筆 TP，不採用。合用也不採用。
僅 8px 候選檢查保留所有已標註命中與原兩筆確認，但未增加正確命中或確認，不能稱為召回改善。
主類別 FP 仍為 5；被刪除的九筆全在其他類別影片，不可宣稱「消除九筆誤報」。

## 逐件差異與覆核

### 邊界檢查的退步

消失的五筆 GT 命中：SOZ008 3.300 秒、SOZ013 1.049 秒、SOZ017 9.304 秒、
SOZ026 10.633 秒、SOZ028 3.467 秒。前後圖板的車輛與 GT 相符；它們原先多依賴遮擋延伸或邊緣重疊，
對接地多邊形施加相同位置誤差會截斷有效候選。這不是單純清掉錯誤目標。

SOZ024 的銀色 L8-1760 與 GT 相符，畫面支持停在條紋上。原 4.079 秒疑似改在 8.634 秒發布，
9.178 秒確認：邊界檢查使候選重新累積，確認錨點與期限也跟著改變。這是既有事件延後升級，不是新找到一台車。
另一個大幅延後是 SOZ023，同一物件原 6.406／19.052 秒事件變成 52.219 秒事件。
工具用同物件、三秒窗口比較時會將這些列為 removed／added；不得直接把 added 當成新的違規車輛。

### 僅 8px 候選檢查

沒有新增、時間變動或狀態變動事件；移除：

| 影片 | 秒數 | 覆核限制 |
| --- | --- | --- |
| CIM015 | 19.267、27.000 | 遠處小車，無法確認與斑馬線的實際占用關係 |
| CIM019 | 14.448、20.420 | 路口另一側車輛，部分前後畫面可見移動；車道／區域歸屬未確認 |
| CIM027 | 23.467 | 區域外框延伸至遠處車輛，疑似範圍過鬆 |
| cy005 | 21.207 | 遠處小車且接近片尾，後續畫面不足 |
| cy020 | 2.500 | 遠處路口車輛，方向與占用關係未確認 |
| vr006 | 1.736 | 路口右側白車，前後畫面有移動，疑似把通行判成停止 |
| vr021 | 7.800 | 遠處路邊車，未見位於前景斑馬線，疑似誤報 |
| A1 | 985.751 | 左側遠處橫向車輛，與前景斑馬線歸屬不明；不是既有人工標記 FP |

這些是 agent 圖板覆核，保持未標註／未確認，不新增人工 GT。
部分被排除的遠處車可能是真實違規，因此只提供「減少低解析度候選」的選用設定，不保證召回無損。
圖板中央事件幀畫出當時多邊形；其他幀沒有套用過時的框。黑色圖格表示超出可讀片尾。

## 長片是否增加誤報

| 長片 | 改動前斑馬線事件 | 僅 8px 後 | 已知人工誤報（前→後） |
| --- | --- | --- | --- |
| A1 | 1，985.751 秒 | 0 | 0→0；刪除的事件原未獲人工定論 |
| A5 | 1，156.533 秒 | 1，同事件同時間 | 1→1 |
| 合計 | 2（3.29 筆／小時） | 1（1.65 筆／小時） | 1→1 |

選用方案在完整 A1／A5 中沒有新增事件、重複事件或 confirmed；A5 保留事件除設定參數外逐欄相同。
因此**在這兩支長片與目前觀測下，沒有新增誤報**。下降的是候選數，已知人工誤報沒有減少。
A5 的橫向通行／停止判定問題尚未解決；像素與邊界檢查都不能解決它。
邊界單獨使用也沒有新增長片事件，但 A1 原事件延後到 987.653 秒；合用移除 A1、保留 A5。
本輪只跑斑馬線規則，表內不是所有違規規則的總事件數。
長片只有部分人工覆核，無完整負例／召回標註，不能外推到所有長片或聲稱完整誤報率改善。

## 採用與重現

主預設 configs/area_rules.json 與既有 configs/area_confirm_crosswalk.json 均未改。
新增選用 configs/area_crosswalk_resolution_guard.json：4aw 確認設定＋crosswalk_min_footprint_px=8，
不開 crosswalk_boundary_guard。適合優先減少低解析度候選的使用情境；要保留遠處候選則沿用原設定。

```powershell
.venv\Scripts\python.exe tools/confirmation_eval.py eval/confirmation_experiments/stage6_crosswalk_boundary.json --out outputs/crosswalk_boundary_retest --workers 4
.venv\Scripts\python.exe evaluate_violations.py <detections.jsonl> --rules all --area-rule-config configs/area_crosswalk_resolution_guard.json --output <violations.jsonl>
```

需要前述未進 git 的觀測與粗略標註；評估工具保存展開參數與程式指紋。
沒有對這一輪新增條件掃描參數或依影片 ID 設例外；8px 與誤差帶沿用 4aw，但仍是在既有資料上的驗證，
不是獨立保留集的泛化測試。8px 以原始影像座標計算，不做解析度正規化。

## 驗證與附件

- 完整測試 319 項通過（228.594 秒），含新增五項邊界、遮擋、解析度、非斑馬線隔離、關閉相容性與設定驗證。
- 174 支 baseline 與 4aw 輸出完全相同；正式／粗略 GT、長片人工覆核、主設定及 4aw 選用設定 SHA-256 均不變。
- 真實全規則入口煙霧測試：SOZ019 保留 confirmed，vr021 排除原斑馬線疑義事件；結果見 smoke_*.jsonl。
- 根目錄 outputs/crosswalk_boundary_20261117/：eval/report.json、eval/manifest.json、comparison.json、
  visual_review.json、verification.json、review/、tests.log、eval.log 及修改前 area_rules_before.py。
- 只有規則改動，沒有新推論階段；未量測或宣稱 FPS 改善。

下一階段若要降低 A5 已知誤報或追回上述五筆，應分別研究停止／方向判定及實際條紋支援，
不能把本輪拒絕的全候選邊界檢查直接開為預設。
