# 4ag：分層動作備註、有效區間與投影品質診斷

## 1. 任務與狀態

- 使用者同意依前述方案降低沒有描述的未知：分層備註、事件附近有效區間、補償品質診斷；另核對片尾資料及避免停車起步時捏造進入方向。
- Astra 開發，交 Opus 審查；狀態：待整合，維持實驗性預設關閉。
- 基準 HEAD `1cc045a`，`feature/conservative-marking-schedule` 原工作目錄，已含未提交 4ad／4ae／4af。全部保留，無 reset／還原。
- 本次前置快照 `outputs/analysis/motion_layers_4ag/before`；增量差異 `outputs/patches/4ag_motion_layers.diff`，須接在 4af 後套用。不提交／推送／合併。續行時發現 Opus 另增展示工具、交接索引及清理紀錄，全部保留；refactor_log 的差異基準另存 `before/refactor_log_with_opus_cleanup.md`，不將 Opus 內容列入本次差異。
- 不改車輛／標線／鏡頭模型、規則、confirmed、GT 或追蹤 ID；不跨 ID 找回失蹤車、不做新的 GPU 推論或自動安裝。

## 2. 變更與責任位置

| 檔案 | 原問題 | 修改後行為 |
| --- | --- | --- |
| `motion_notes.py` | 任一窗口缺口否決整段，未知無描述；投影至最後一幀 | 選涵蓋事件的連續區間、事件幀參考、局部尺度診斷；分四層描述 |
| `tests/test_motion_notes.py` | 4af 只覆蓋二元分類／未知 | 加入 8 項層級、有效區間、失真、起步、不可借 ID／不可混淆鏡頭等測試；修改兩個舊測試以檢查缺口沒有被跨接 |
| `observation_schema.py` | 可選事件備註 v1 說明 | 新增 v2 欄位與向後相容說明，觀測 schema 不變 |
| 使用說明兩份、AGENTS、refactor_log、此文件 | 舊結果及操作說明 | 同步分層語意與驗證限制 |

沒有新增生產模組／類別，擴充既有 MotionNotes 與純函式。三入口及文字／剪輯消費端沿用 4af 接線，讀 text 即可呈現新備註；無平行流程。

## 3. 介面交接

事件 `motion_note.version=2`；觀測 schema 仍 1.9，獨立逐幀 sidecar 語意不變。`--motion-notes` 預設仍 false，沒有新增強制開關。

| 欄位 | 生產端／消費端 | 語意與相容 |
| --- | --- | --- |
| `level` | MotionNotes／報表與呈現 | classified、tentative、observed、unknown；classified 也不是法定確認 |
| `maneuver` | MotionNotes／既有讀取端 | 只寫通過完整分類的動作；較弱描述仍 unknown |
| `tentative_maneuver` | MotionNotes／新報表 | 較短或部分證據的 straight／left_turn／right_turn，否則 null；不覆寫 maneuver |
| `observation` | MotionNotes／診斷 | 畫面座標 dx/dy、水平／垂直方向、實際時間區間；includes_camera_motion=true，不等於道路方向 |
| `quality` | MotionNotes／診斷與覆核 | 原／選定區間、失效原因與幀、前後資料缺少量、投影尺度／異向拉伸、進入方向是否可辨 |
| `evidence` | MotionNotes／覆核 | raw／補償／平滑軌跡與各自樣本幀、事件參考幀，平滑半徑 0.1 秒 |
| `text` | MotionNotes／原文字摘要與剪輯資訊卡 | 最適合呈現的完整中文備註，明示疑似或畫面移動；舊消費端不需改碼 |
| `method` | MotionNotes／診斷 | 沿用兩個補償方法名稱，另加 image_motion_only 表示未有可用補償路徑；若外部讀取端使用封閉 enum，須依 version=2 擴充 |

- 舊事件沒有 v2 欄位仍可讀。Opus 需審查可選事件欄位及分類展示用語；這次不遷移 detections、不改正式規則介面。
- 關閉時完全旁路、不新增欄位／讀遮罩／建庫／等待。啟用仍是事件前 1.5 秒、後 3 秒、原 FIFO／片尾 flush，不增加既有發布等待。
- 不改原事件 status、conditions、ID、時間、evidence、plate、merged_events。沒有把 tentative 對應成違規 suspected，也不讓描述影響 confirmed。
- 新觀測與快取規則保持；中位數平滑只整理現有新樣本，不增加直接證據。局部車道來源與 4af 相同。

### 分析與保守界線

1. 先找包含事件新車框的同 ID 連續片段；缺幀、0.3 秒以上新框間隔、跳框、接地遮擋切段。事件本身無新框／被遮擋直接未知。
2. 補償片段另外以無效 H 切段。無效 H 的當幀可以作為新片段起點，但不跨越它的入邊，不補單位矩陣。剔除較早失效前的資料，不等於忽略失效。
3. 以事件幀為參考，前段正向／後段反向組合 H；檢查接地點局部 Jacobian。尺度須介於 0.2–5、最大異向拉伸比 <=6，否則只保留畫面描述。這些是實驗拒絕閘門，不能證明通過就幾何正確。
4. `quality.classification_eligible` **只表示通過投影閘門**，不保證觀測長度、位移或進入方向足夠；最終須看 level。
5. 完整分類沿用 4af classify_path 門檻，輸入改為事件座標與固定秒數的中位數平滑。事件後因缺口被截短，即使片段可分類也降為 tentative；進入前幾乎無位移不捏造其方向。
6. 短區間疑似直行須前 >=0.2 秒、後 >=0.25 秒、新樣本至少前 3 後 4、路徑近直線且不折返。疑似轉彎另需可辨進入方向、後 >=0.8 秒、尾段穩定、45–125 度偏轉與至少半車寬側移。全部只寫 tentative_maneuver；保留換道／彎道歧義。
7. 未能分類時，至少 4 個事件後新框、0.2 秒的連續資料才嘗試畫面方向；按 0.2 秒分箱，主要方向步進至少 70% 一致。文字必須說「含鏡頭影響，不等於車輛轉向」。小位移只能說畫面位移不明顯，不能說車輛靜止。

## 4. 可重現環境

- Windows／既有 .venv／純 CPU；無模型 provider 載入、權重與模型設定不變。
- `outputs/eval/baseline_18`：31 支、9,411 幀，保存的完整原分析範圍與 masks。GT 30 支計分、34 事件（soc04 排除），沒有修改 GT。
- 對照：4af `outputs/analysis/local_motion_4af/final`；實驗：`outputs/eval/motion_layers_4ag`，以正式 rule_eval 重新執行完整規則與備註。
- 報表／日誌／抽查圖／腳本：`outputs/analysis/motion_layers_4ag`。`before/protected_sha256.json` 保存 GT、覆核資料、既有設定／深度／局部車道指紋。

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p test_motion_notes.py
.venv\Scripts\python.exe -m unittest discover -s tests
.venv\Scripts\python.exe tools/rule_eval.py --source outputs/eval/baseline_18 --label motion_layers_4ag --motion-notes
.venv\Scripts\python.exe outputs/analysis/motion_layers_4ag/summarize.py
.venv\Scripts\python.exe outputs/analysis/motion_layers_4ag/benchmark_events.py
```

## 5. 驗證與結果

- 本次修改前相關 13 項通過；修改後相關 **21 項通過**，完整 **240 項通過（一般 Windows 74.787 秒）**。新增 8 項；三入口合成一致性測試包含在內。
- 兩個舊測試原先要求「任何遮擋／缺口整筆未知」，更新為檢查失效原因仍記錄，取用樣本不跨失效處、完整 maneuver 不被短片段提升。其餘原測試保留。
- 整片逐事件比較與最終計數見 `report.json`／`report.md`；只排除 motion_note，保留完整原事件欄位（非只比指標）。最終結果如下。
- 全部 12 筆紅燈事件皆產生四張有幀號、時間、目標框的抽查圖。這是 agent 目視抽查，**不是使用者確認的動作 GT**。
- ms03：機車向左彎出的跡象支持疑似左轉；ms04：短片尾僅支持觀察區間疑似直行。**ms02 1.040 秒的右轉候選仍有歧義，可能是錯誤動作推測，不計成正確右轉成果**。
- ms05 畫面向左上移動但道路行進仍似直行：保留畫面描述，不報左轉。s04 投影最大尺度約 5.56，拒絕道路動作推測，不放寬以追求分類。
- 已核對原影片：ms01 99 幀、ms04 172 幀、s02 181 幀，全部已分析到片尾，沒有尚可補算的影格。詳細清單 `tail_inventory.json`。不對不存在的片尾做外插。
- 低位移的進入方向不可辨時，保留後續畫面移動描述；此版沒有車頭模型，不能可靠判斷是否停車起步或倒車。
- 未執行 GPU 重推論、新長片或完整串流效能測試；動作分類精確率／召回率仍缺獨立人工標註，不能以無描述未知下降宣稱辨識準確率提高。

### 最終量化（2026-11-11 完成交接）

31 支、9,411 幀全部完成；診斷行數逐片等於來源幀數。23 筆事件只排除 motion_note 後逐筆完全相同，report.json 包含逐片來源 SHA-256 及每筆前後事件雜湊。對照是同 Windows 的 4af 最終結果，不是 baseline_18 原始舊規則事件；rule_eval 控制台顯示與原始來源不同，並非本次備註改變事件。

| 紅燈備註層級 | 4af | 4ag |
| --- | ---: | ---: |
| 完整分類 classified | 2 | 2 |
| 疑似動作 tentative | 0 | 3 |
| 僅畫面描述 observed | 0 | 6 |
| 無足夠描述 unknown | 10 | 1 |

正式 maneuver 仍為 straight 2、unknown 10，沒有新增完整左右轉分類。疑似為 ms02 1.040 秒右轉、ms03 2.169 秒左轉、ms04 5.233 秒直行；ms02 未確認，不能計為正確右轉。mm01 2.800 秒仍未知。全部 12 筆描述與抽查限制在 report.md；上述時間以事件 JSONL 為準。

TP/FP/FN 與 4af 相同：紅燈 11/1/4、停等區 5/1/2、斑馬線 1/3/3、雙黃線 1/0/7；車牌 6/15。這些是原違規評估，不是動作分類準確率。

CPU 每事件 SQLite 查詢＋備註計算（一次暖機、各 15 次）：

| 影片 | 4af 平均 ms/事件 | 4ag 平均 ms/事件 |
| --- | ---: | ---: |
| ctm01 | 5.45 | 81.04 |
| ms03 | 21.01 | 54.70 |
| ms04 | 3.90 | 27.43 |
| ms05 | 30.82 | 85.69 |
| s01 | 18.53 | 67.67 |
| s04 | 9.08 | 55.19 |

完整原始統計見 event_benchmark.json。排除遮罩解碼、模型、規則、影片、sidecar 與逐幀 observe；非每幀成本、非全流程 FPS。此版更慢，增加約 24–76 ms/事件；不宣稱加速或即時。逐幀 observe 未改，4af 歷史逐幀量測不能當成此版端到端效能。

## 6. 整合決策與回退

- 維持實驗／預設關閉。呈現改善可供覆核，但沒有足夠證據啟用正式自動動作分類。
- 關閉 --motion-notes 回到原事件；如需回退至 4af，可用本次增量差異反向套用，保留 4ad／4ae／4af，不 reset 工作區。
- 文件同步：使用說明兩份、schema 文件、refactor_log、AGENTS。未提交／推送／合併，也不修改 outputs／input 的版本控制設定。
- Opus 後續 prompt：

> 讀 AGENTS、ASTRA、4af／4ag 交接，保留三批未提交修改。審查 motion_note v2 的 level 與 tentative_maneuver，舊讀取端不得把疑似動作當正式 maneuver；預設繼續關閉。先人工覆核 report.md 的 12 筆，特別是 ms02 1.040 秒右轉候選，列為未確認／可能錯誤，不拿它當成功案例。補獨立左右轉／換道／彎道與停車起步標註，不改原違規 GT。分別報 classified、tentative、observed、unknown 以及錯誤／未確認描述數。ms01／ms04／s02 原片已到尾，後續須新素材而非補算空資料；mm01 不跨 ID 借車；s04 先查相機補償尺度失真，不調高閘門來湊分類。任何規則／schema／預設變更另交接決定。
