# 4ad：Windows CPU 重算 4ab／4ac

## 1. 任務與狀態

依使用者 P0 要求，重算 Claude 的兩個規則版本並逐筆比對。Astra 驗證、交 Opus 審查；不修改規則。
工作區起始乾淨，HEAD `1cc045a`，分支 `feature/conservative-marking-schedule`。
4ab 使用 commit `cced518`，4ac 使用 `1b3c707`；以 git archive 解出獨立快照，沒有切換或還原工作區。
狀態：兩版本各 33 支全部重算完成，待 Opus 審查。增量差異 `outputs/patches/4ad_windows_rules.diff`（文件與執行附件，不含 4ae 生產程式）。

## 2. 變更與責任位置

- 生產程式、規則、預設、schema 均無變更。
- `outputs/analysis/p0_20261110/replay_rules.py`：針對兩份快照呼叫各自的 `tools.rule_eval.run_rules`；固定 plate-backfill=0，以單次不中斷的執行驗證 Opus 分段重算／續跑結果。沒有讀取 pickle checkpoint。
- `summarize_rules.py`：彙整逐片逐事件比較，嚴格比較只排除頂層 plate，不對浮點數套容差或刪除 evidence。
- 附件只負責執行與比對，沒有複製規則實作。每支影片保存完整前後事件、SHA-256、每個不同欄位及前後值。

## 3. 介面交接

無模型／觀測／事件介面變更。所有相同、不同的判定均保留原數值；「判定欄位相同」與「完整欄位相同」分開報。
判定欄位包括 event_id、rule_id、status、frame、timestamp_sec、object_id、完整 conditions。
無快取證據、confirmed、finalize、去重或規則門檻變更。另一個 P0 深度修復獨立記為 4ae，未進入本次規則快照。

## 4. 可重現環境

Windows、本機 `.venv`。來源為 `outputs/eval/baseline_18` 的 31 支短片，及 `outputs/analysis/long_video_20261108/run_01/phase1/{A5,A1}` 全片 JSONL。
比對 Claude 的 `opus_variants/short_new`、`A5_new`、`A1_new`（4ab）與 `short_fix34b`、`A5_fix34b`、`A1_fix34b`（4ac）。
沒有重新解碼影片、重跑模型或重估鏡頭運動。工作程序設定 CUDA_VISIBLE_DEVICES 為空，完成時記錄是否曾載入 torch／onnxruntime。
來源與參考檔 SHA-256 各存在 comparison.json，原始碼 archive SHA-256 與 GT 指紋在 baseline_manifest.json。

```powershell
# 專案根目錄；輸出存在時會拒絕覆寫，重跑應使用新的獨立附件目錄。
$env:PYTHONIOENCODING='utf-8'
# 只在全新附件目錄執行一次，建立兩版快照；本次目錄已存在，不需再跑。
.venv\Scripts\python.exe outputs\analysis\p0_20261110\prepare_sources.py
.venv\Scripts\python.exe -u outputs\analysis\p0_20261110\replay_rules.py 4ab
.venv\Scripts\python.exe -u outputs\analysis\p0_20261110\replay_rules.py 4ac
.venv\Scripts\python.exe outputs\analysis\p0_20261110\summarize_rules.py
```

## 5. 驗證與結果

修改前完整單元測試 209 項全部通過（91.650 秒，正常 Windows 環境）。本項未改可執行生產程式。
兩版本各重算 31 支短片及 A5／A1 全片，共 66 個影片案例，84 筆事件。**所有案例的事件 ID、規則、狀態、幀、秒數、車輛與全部 conditions 相同**。
兩程序均未載入 torch／onnxruntime，只有 CPU 規則重算；完整逐片表在 `outputs/analysis/p0_20261110/rules_report.md`／`.json`。

| 版本／集合 | Claude 事件 | Windows 事件 | 完整欄位相同影片 | 判定欄位相同影片 |
| --- | ---: | ---: | ---: | ---: |
| 4ab 短片 | 25 | 25 | 26/31 | 31/31 |
| 4ab A5（18,174 幀） | 4 | 4 | 0/1 | 1/1 |
| 4ab A1（47,396 幀） | 24 | 24 | 0/1 | 1/1 |
| 4ac 短片 | 23 | 23 | 27/31 | 31/31 |
| 4ac A5（18,174 幀） | 1 | 1 | 1/1 | 1/1 |
| 4ac A1（47,396 幀） | 7 | 7 | 0/1 | 1/1 |

所有不相同的欄位都是 evidence 內的數值，最大絕對差 0.00005109033918415662。沒有偷偷排除這些欄位，也未將容差比較寫成通過。
4ab 差異片：cdy01、ctm01、s01、s02、soc01、A5、A1；4ac 差異片：cdy01、ctm01、s01、s02、A1。
主要是停止線座標及雙線幾何數值；跨平台數值差異的套件／運算層原因尚未完全歸因，不能寫成「跨平台逐位元相同」。
長片 4ab A5／A1 分別有 5／33 個不同數值欄位，最大差 0.00001481／0.00004150；4ac A1 有 13 個，最大差 0.00002896。

短片 GT（TP／FP／FN）：闖紅燈 4ab=11/3/4、4ac=11/1/4；兩版本停等區 5/1/2、斑馬線 1/3/3、雙黃線 1/0/7，車牌皆 6/15，重現 Claude 結果。
長片全部為 suspected，沒有 confirmed；無完整違規真值，不報召回。
各次 A5／A1 重算秒數：4ab 891.5／1,708.7，4ac 1,208.8／3,449.8；有其他 CPU 工作並行，不作獨占效能比較。

## 6. 整合決策與後續

- 不為消除浮點差異而改寫生產證據或引入隱藏容差；若需跨平台位元一致性，另案規範幾何計算與序列化精度，交 Opus 決定。
- 本項無行為回退需求。原工作區及兩版參考輸出皆保留；outputs 不提交。
- 本次可接受「規則判定重現」，嚴格完整事件相等仍有上述數值差異，交 Opus 評估是否需要另案處理。
- 4aa 仍保留給尺度實驗；本次另一項 P0 深度補邊見 `depth_letterbox_4ae.md`。
- 未修改 GT 或 long_video_review，不推送／合併，不替使用者判定剩餘違規事件。

接手 prompt：讀本交接與 rules_report，先看判定欄位，再看所有完整 evidence 差異。以各片 comparison.json 定位，不只比較事件數或指標；不得把數值近似當成已通過嚴格相等。
