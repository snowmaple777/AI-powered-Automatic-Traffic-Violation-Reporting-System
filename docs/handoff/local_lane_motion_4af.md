# 4af：局部車道與闖紅燈行進動作備註

## 1. 任務與狀態

- 需求：局部車道與運動方向分析，為闖紅燈事件補上直行或轉彎備註。此版不新增模型、違規規則、完整路口拓樸、法定行向或號誌歸屬。
- Astra 開發，交 Opus 審查；狀態：待整合，**不建議切換預設值**。
- 基準 HEAD `1cc045a`，分支 `feature/conservative-marking-schedule`。保留原工作目錄 4ad／4ae 深度修正、設定、測試與文件。
- 開始前快照：`outputs/analysis/local_motion_4af/before`；增量差異：`outputs/patches/4af_local_lane_motion.diff`。本次未提交／推送／合併。

## 2. 變更與責任位置

| 檔案 | 修改後行為 | 性質 |
| --- | --- | --- |
| `local_lanes.py` | 由既有語意遮罩抽取可見邊界、車輛附近走廊與疊圖 | 選用新功能 |
| `motion_notes.py` | 精簡歷史暫存、鏡頭補償軌跡、事件備註；三入口共用 | 選用新功能 |
| `run_pipeline.py` | 使用已處理遮罩；輸出診斷、摘要、局部幾何疊圖 | 接線 |
| `evaluate_violations.py`、`tools/rule_eval.py` | 使用保存的遮罩與相同註記器 | 接線 |
| `violation_visualization.py` | 文字摘要與剪輯資訊卡呈現備註 | 呈現 |
| `tests/test_motion_notes.py` | 方向、反例、來源與三入口一致性 | 13 項測試 |
| schema 文件、兩份使用說明、refactor_log、AGENTS、此交接 | 契約、限制與結果 | 文件 |

幾何與事件生命週期分成兩模組；沿用 core.motion、core.geometry、TrackHistory。接地遮擋沿用紅燈規則 `_contact_occluded`，未複製或修改規則。資料流已接通：模型既有遮罩 → 局部分析 → 合併後事件備註 → JSONL／文字／剪輯；FrameProcessor 原紀錄不被改寫。

## 3. 介面交接

| 欄位／設定 | 生產端 | 消費端與契約 | 舊資料相容 |
| --- | --- | --- | --- |
| `--motion-notes` | 三入口 CLI | bool，預設 false | 關閉旁路，不建庫／sidecar、不新增欄位、不延遲 |
| `--motion-mask-dir` | 離線 CLI | evaluate 指單片 masks；rule_eval 指含 `<影片>/masks` 的根目錄 | 省略取來源旁 masks；缺檔有診斷，損壞／尺寸錯誤報錯 |
| `event.motion_note` | MotionNotes | version=1；maneuver/text/method/reason/estimated/affects_violation/evidence | 舊事件無欄位，呈現不變；不處理 merged_events |
| `*_local_motion.jsonl` | observe | 原圖座標邊界、多邊形、來源、每車接地點及補償位移 | 獨立 sidecar，不是模型觀測或確認證據 |
| `*_motion_notes.txt` | write_motion_summary | 闖紅燈事件文字摘要 | 只在啟用時產生 |

- 觀測 schema 保持 1.9，可選事件欄位的文件已補入 observation_schema。原 status、conditions、ID、frame、timestamp、evidence、plate、去重完全保留。
- 只用白／黃單實、雙實、虛線，遮罩最寬 640 px；逐連通塊取列中心線，不補遮擋、不跨端點外插。短片段、單側、明顯曲線不形成走廊。走廊**不保證是法定車道或可行駛區**。
- 明確 direct 幀才抽取；中間幀用有效路面 H 推移，最多 0.5 秒，來源仍是舊直接觀測。缺來源／對齊失效不建立車道，快取不增加證據。
- 只收同 ID 且 source_frame=index 的新車框；騎士群組取 vehicle_bbox_xyxy。事件幀須有新框。相鄰新觀測間隔 <=0.3 秒；框跳動、接地遮擋、鏡頭或幀缺口回 unknown。
- 固定事件前 1.5／後 3 秒；至少 9 個新觀測，前 0.4 秒、後 1 秒。將軌跡投影到同一參考幀；角度是影像角度，**不是世界 yaw**。
- 直行檢查整段共線且持續前進，或前後方向一致且側移小；轉彎需尾段方向穩定、偏轉 60–125 度、足夠側移。有局部車道時要求進入方向一致；無車道時採僅軌跡推估，文字明示。
- method=`local_lane_and_ego_compensated_track`／`ego_compensated_track_only`；所有結果 estimated=true、affects_violation=false。未把備註用於規則。
- 接在 PlateBackfill 後，FIFO 等事件後 3 秒資料；後續非紅燈事件也可能等前一筆。引擎或車牌補充原有延遲另計。片尾 flush，資料不足仍回 unknown，原事件時間不改。
- SQLite 保存整片精簡歷史以支援引擎延後釋出，頁面快取 2 MiB，磁碟隨片長增加，結束刪除；不是整個程序 RAM 2 MiB。
- 即時總覽事件卡先顯示原事件；最終 JSONL／文字／匯出剪輯有備註。`--save-video` 另疊局部邊界、走廊與位移箭頭，不回寫先前影格。
- Opus 決定：可選欄位／sidecar 整合、是否將接地遮擋函式升為公開共用介面、將來是否納入觀測 schema。此版無舊資料遷移需求。

## 4. 可重現環境

- Windows、既有 .venv、純 CPU；不載入模型、不換權重、不安裝套件。來源 `outputs/eval/baseline_18`：31 片、9,411 幀，完整原分析範圍，遮罩與原感知結果不變。
- GT 不變：31 片中 soc04 排除計分，30 片、34 個事件。規則參考是 4ad Windows `outputs/analysis/p0_20261110/4ac/<影片>` 的 23 筆事件。
- 附件根目錄 `outputs/analysis/local_motion_4af`：`replay.py` 的 `replay_v1` 是初版完整規則開／關比對；僅用於原事件等價，不是最終動作結果。
- 最終 `verify_final.py` → `final` 整片重播共用註記器。`final/report.json` 含逐片來源 SHA-256、逐事件比對雜湊及所有備註；每片另有完整 sidecar。
- 正式 rule_eval CLI 額外重算 ctm01、s01、ms05、cdy02；結果與 final 逐筆核對。沒有重新推論模型，因此不屬新模型準確率驗證。

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests
.venv\Scripts\python.exe tools/rule_eval.py --source outputs/eval/baseline_18 --label motion_notes_4af_check --only ctm01 s01 ms05 cdy02 --motion-notes
.venv\Scripts\python.exe evaluate_violations.py outputs/eval/baseline_18/s01/s01_detections.jsonl --output outputs/s01_motion.jsonl --rules all --no-violation-clips --motion-notes
# 原 run_pipeline 命令後加 --motion-notes；需要局部幾何總覽時另加 --save-video
```

## 5. 驗證與結果

- 修改前 219 項通過（一般 Windows 32.157 秒）；修改後 **232 項通過（131.914 秒）**，新增 13 項。後測有其他 CPU 重播並行，耗時不比較性能。
- 沙箱基準 3 errors／1 failure 與暫存檔重新命名／發布有關；一般 Windows 全綠。後測曾因自動核准額度失敗未啟動，續行後正常完成。日誌均保存。
- 合成案例涵蓋直行、左右轉、鏡頭平移、停止、反向、換道、ID 切換、快取框、跳框、中斷、片尾、單側／停止線非車道、遮擋、缺來源、FIFO、三入口一致性。
- **31 片、23 事件：排除唯一新增 motion_note 後全部欄位逐筆相等**，含 evidence 與 plate；逐事件雜湊在 final/report.json，非只比較指標。

| 指標 | 關閉 | 啟用 | 限制 |
| --- | --- | --- | --- |
| 闖紅燈 TP/FP/FN | 11/1/4 | 11/1/4 | 原違規 GT，非動作標註 |
| 停等區 | 5/1/2 | 5/1/2 | 同上 |
| 斑馬線 | 1/3/3 | 1/3/3 | 同上 |
| 雙黃線 | 1/0/7 | 1/0/7 | 同上 |
| 車牌正確 | 6/15 | 6/15 | backfill=0 |
| 紅燈動作 | 無欄位 | 直行 2、未知 10、左／右轉 0 | 12 事件，不能宣稱動作準確率 |
| 有邊界的幀 | 未啟用 | 5,244/9,411 | 不等於形成車道 |
| 兩側走廊支援的車輛觀測 | 未啟用 | 73/43,975，約 0.166% | 新車框觀測，非獨立車輛數或精確率 |

| 影片／秒數 | 最終動作 | 原因 |
| --- | --- | --- |
| ctm01 13.367 | 直行，僅軌跡推估 | 共線軌跡；抽看 12.367、13.367、14.367、15.867 秒相符 |
| s01 0.767 | 直行，僅軌跡推估 | 共線軌跡；抽看 0、0.767、1.767、3.267 秒相符 |
| mm01 2.800、2.967；ms04 5.233；s04 14.633 | 未知 | 鏡頭／影格缺口，4 筆 |
| ms01 3.600；s02 5.685 | 未知 | 事件後同 ID 僅約 0.32／0.30 秒，2 筆 |
| ms02 1.040；ms03 2.169；ms05 6.624 | 未知 | 換道／彎道／未完成轉彎歧義，3 筆 |
| ms02 1.080 | 未知 | 位移不足，1 筆 |

以上原事件狀態與時間皆不變。所有 12 筆紅燈事件均無兩側車道支援。**左右轉只有合成測試，尚無真實轉彎正例驗證**。
已知漏描述：ms05 抽看白車直行，但補償接地軌跡有較大彎折，仍回 unknown，未對該片放寬。
視覺抽查 cdy02：路邊停車區也可能形成兩側走廊，不代表可行駛車道；不能用本版作規則排除。
完整 GPU、串流延遲與長片容量測試未執行。模組 CPU 量測另附於下方，不代表完整流程 FPS。

## 6. 整合決策與回退

- 維持實驗、預設關閉；關閉旗標即可回原事件輸出。舊讀取端可忽略新增欄位。沒有更換權重，無模型回退需求。
- GT／long_video_review／third_party 未修改。已同步使用說明兩份、schema 文件、AGENTS 與 refactor_log。未獲推送／合併授權，本機交付。
- 下一步應補真實動作覆核與車道診斷，不能以降低 unknown 比例單獨宣稱改善。

給 Opus 的接續 prompt：

> 讀 AGENTS、ASTRA、此交接與 refactor_log 4af，保留 4ad／4ae 修改。審查 motion_note 與 sidecar 契約，維持預設關閉、confirmed 與 GT 不變。先建立獨立動作覆核清單，涵蓋直行、左右轉、換道、彎道、橫向穿越、停車起步，記錄前後時間與同 ID 連續性，不只挑成功樣本。以 final/report.json 的 12 筆起步，特別分析 ms05 補償後彎折；補真實轉彎正例與保留驗證片段後，才驗證影像角度門檻。先核查 73 個走廊候選中的停車區／道路邊緣，再評估邊界跨幀追蹤、虛線連接、遮擋補充；推測不算新直接證據。任何 schema、規則語意或預設變更另行交接決定。

### 量測與入口補記

- 正式 rule_eval 的 ctm01、s01、ms05、cdy02 與 final 的事件檔逐位元相同（共 3 事件），詳 entry_check.json。
- 單獨 CPU 模組測量（無其他本次測試／重播並行）：

| 影片 | 幀數 | observe 平均／P95 ms | PNG 解碼平均 ms | SQLite／sidecar bytes |
| --- | ---: | ---: | ---: | ---: |
| cdy02 | 218 | 4.90／9.69 | 1.85 | 585728／877175 |
| s01 | 226 | 4.46／17.12 | 1.83 | 303104／233750 |
| ms05 | 301 | 17.68／25.85 | 5.20 | 1032192／627712 |

observe 包含邊界抽取／推移、每車幾何、SQLite 與 sidecar 寫入，不含遮罩 PNG 解碼、規則與模型。
s01／ms05 的事件結尾分類耗時 15.73／26.46 ms。不是模型完整流程 FPS，也沒有測直播影格延遲。
效能仍可改進，尤其每個歷史點的單應投影；此版先保留共用 TrackHistory，不改既有規則軌跡實作。
基準腳本 benchmark.py、原始數字 benchmark.json。無長片 DB 容量測試，不外推到小時級保證。
