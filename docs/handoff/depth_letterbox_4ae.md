# 4ae：4:3 深度固定輸入與補邊修復

## 1. 任務與狀態

使用者要求修復預設設定在 4:3 首幀崩潰，改用補邊並比較拉伸／補邊的距離及車牌辨識。
Astra 實作、交 Opus 審查；基準 `1cc045a`，`feature/conservative-marking-schedule`，無推送／合併。
狀態：程式、單元測試、A5／A3 首分鐘實際模型對照及預設完整流程 smoke 完成，待 Opus 審查。4ad 使用獨立歷史快照，不受此修正影響。
增量差異：`outputs/patches/4ae_depth_letterbox.diff`；與 4ad 分開交付，未提交。

## 2. 變更與責任位置

| 檔案 | 原問題 | 修改後 |
| --- | --- | --- |
| model_library/depth.py | 跟隨 ONNX 宣告的動態尺寸，4:3 產生 392×518，內部 1,037／1,401 tokens 不相容；固定輸入路徑會拉伸 | 新增可選 input_shape，維持長寬比縮放與平均色補邊，輸出先裁補邊再還原原圖 |
| configs 下七份含 onnx_depth 的設定 | 沒有記錄該權重真正可用尺寸 | 明列 input_shape=[392,700]，其餘模型／門檻不變 |
| tests/test_depth_letterbox.py | 缺少尺寸與補邊裁切驗證 | 10 項測試，涵蓋形變、裁切、車框座標、快取、輸出降解析度、參數與配置 |
| docs/usage.md、使用說明.txt | 未說明假動態尺寸與自訂設定遷移 | 同步記錄設定欄位與補邊行為 |

七份設定：default、full、onnx_vehicle、activity_boost、marking_interval_1、marking_interval_2_refresh、marking_interval_4_refresh。
沒有新增生產模組、臨時後端或第二套辨識流程；原 DepthModel 直接接回 ModelPipeline。

## 3. 介面交接

- `input_shape` 為可選 [height,width] 整數序列，各值至少 14；與 ONNX 宣告的固定維度矛盾則立即報錯。
- 內附權重雖宣告動態 height／width，實際可用固定輸入為 392×700；明列在其設定，不依檔名猜測、不捕捉推論錯誤後靜默拉伸。
- 自訂舊設定若使用同一權重，須補上 input_shape。真正的動態模型可省略，保留 input_size 決定目標尺寸的路徑。固定維度模型可直接採用其 ONNX 維度。
- 960×720 → 523×392，左右補 88／89 像素；正規化後補 0，相當於 RGB 訓練平均色。深度圖按輸出／輸入尺寸比例裁邊後還原 960×720，不把補邊帶入車框距離。
- 16:9 也維持長寬比；1280×720 → 697×392，左右補 1／2 像素。相較舊式 700×392 拉伸，可能有小幅距離變動，不宣稱舊片感知輸出不變。
- 深度快取 interval、source_frame、reset、原圖座標、distance_m 與 depth_map 格式不變，schema 維持 1.9；規則與 confirmed 條件未改。
- 這是使用者明確授權的預設崩潰修復。距離估計與 OCR 門檻實際通過情況會變，精度仍需真值驗證；不因此降低 22 m／58 px 門檻。

## 4. 可重現環境與實驗方法

Windows、RTX 5060 Laptop、本機 .venv；NumPy 1.26.4、OpenCV 4.10.0、ONNX Runtime GPU 1.23.2、torch 2.11.0+cu128、ultralytics 8.4.146、Paddle 3.3.1。
原始錯誤以 CPUExecutionProvider 重現並保存於 `outputs/analysis/p0_20261110/depth_original_failure.json`。完整版本與深度／車牌／OCR 權重、字典 SHA-256 在 environment.json。
模型與程式指紋、解析後的完整設定、影片指紋存於各案例 request.json。沒有變更權重、第三方程式或安裝套件。

對照使用 A5、A3 各首分鐘 1,800 幀與 phase0 同一區間的車輛觀測；只重跑深度→車牌→OCR，保留車牌追蹤與 OCR 快取的完整時間連續性。
拉伸使用 source_4ac 中原始 DepthModel，加上壓測既用的固定 ONNX 輸入尺寸；補邊使用修復後預設深度設定。
兩分支重新跑車牌偵測（距離也會影響車牌裁切／配對／保留），不是只替已有牌框換距離。模型 GPU 工作串行；OCR 明確使用 CPU。
車輛 track_mappings 未存於 JSONL，兩分支均使用空映射；這是固定觀測的因果對照，不是重新推論所有感知模型的全片準確率測試。
4ad CPU 工作同時執行，故本次 elapsed／stage_seconds 不作獨占硬體效能基準。

```powershell
$env:PYTHONIOENCODING='utf-8'
.venv\Scripts\python.exe -m unittest discover -s tests -p test_depth_letterbox.py
.venv\Scripts\python.exe -m unittest discover -s tests
.venv\Scripts\python.exe -u outputs\analysis\p0_20261110\compare_depth.py
.venv\Scripts\python.exe outputs\analysis\p0_20261110\summarize_depth.py
# 預設完整流程，前三幀；不使用壓測專用深度後端。
$video = (Get-Content outputs\analysis\long_video_20261108\run_01\phase0\A5\request.json -Raw -Encoding UTF8 | ConvertFrom-Json).video
.venv\Scripts\python.exe run_pipeline.py $video --model-config configs/default.json --device cuda:0 --max-frames 3 --rules all --no-violation-clips --output-dir outputs/analysis/p0_20261110/default_smoke
```

輸出存在時比較附件會拒絕覆寫，重跑須換獨立目錄。首輪 OCR 子程序在沙箱中啟動失敗，保留於 depth_startup_failed_01／depth.log；正常 Windows 環境重跑，未依錯誤訊息安裝套件。

## 5. 驗證與結果

- 修改前完整測試 209 項通過（91.650 秒），新增 10 項相關測試通過（0.127 秒），修改後 **219 項全部通過（111.989 秒）**。
- 原預設在 A5 首幀實際重現 `/Add` 1,037 by 1,401 崩潰。
- 修復後用預設設定內的深度模型於 CPU 跑 A5、A3（4:3）、A9_cut（16:9）首幀，深度圖尺寸正確、100% 有限且正值；詳見 depth_first_frame_cpu.json。
- 預設完整模型流程（未注入壓測後端）成功處理 A5 前 3 幀，max_frames 正常收尾，完整性驗證通過。這是啟動／接線驗證，不當作全片穩定性或準確率數據。
- 四個首分鐘案例各 1,800 幀完整完成；深度與車牌 session 實際為 CUDAExecutionProvider 優先，OCR 子程序明確 disable_gpu。
- A5 2,202 筆、A3 8,899 筆拉伸深度紀錄，與原歷史輸出全部相同。初版附加核對曾以 object_id 字典覆蓋同幀多台未分配 ID 的車，產生一筆假差異；已改按幀＋順序＋ID 比對，無差異，主拉伸／補邊比較原本就依序比對，未受影響。

| 影片／前處理 | 距離中位數 m | 有效距離比例 | 逐幀牌框筆數（含保留） | OCR 次數 | 有車輛歸屬的讀牌幀 |
| --- | ---: | ---: | ---: | ---: | ---: |
| A5 拉伸 | 33.463 | 100% | 276 | 0 | 0 |
| A5 補邊 | 29.840 | 100% | 276 | 0 | 0 |
| A3 拉伸 | 33.572 | 100% | 658 | 106 | 0 |
| A3 補邊 | 34.835 | 100% | 437 | 106 | 0 |

- 同車同幀配對距離差（補邊－拉伸）的中位數：A5 +0.323 m、A3 +1.210 m，與上表整體分布中位數的差不是同一統計。完整 p05／p95／極值存在 depth_report.json。
- 22 m 內外分類：A5 66 車輛幀由外到內、4 由內到外；A3 71 由外到內、286 由內到外。這不代表都通過車牌大小、類別或追蹤等其他閘門。
- A3 牌框總數減少 221 筆，232 幀的牌框數不同，未新增任何有車輛歸屬的車牌讀數。兩組 106 次 OCR 的幀、原字串、信心分數完全相同，105 個無車輛歸屬文字輸出幀；首末 OCR 裁切抽查是時間浮水印，不能算車輛車牌改善。
- 對照資料、逐幀深度與牌框、所有實際 OCR 嘗試及裁切在 `outputs/analysis/p0_20261110/depth/`；摘要 `depth_report.md`／`.json`。
- 距離與車牌缺完整真值，不能以讀數增加或中位數變動宣稱準確率提高。4:3 崩潰修復成立；距離精度及對其他影片召回的影響仍需另驗。

## 6. 整合決策、回退與下一步

建議 Opus 審查固定尺寸設定、平均色補邊以及輸出裁切座標，再整合。若撤回，僅回退本次 depth.py、七份設定、專屬測試與文件；不要回退 4ab／4ac 規則修正。
後續模型評估應使用新 label；舊 JSONL 是舊前處理資料，不能混入新感知基準。沒有重跑所有 31 支短片 GPU 感知，故不宣稱其事件輸出不變。
4:3 預設崩潰已定位並修復，不藉此切換標線尺度、降低 OCR 門檻或放寬 confirmed。4aa 尺度實驗仍獨立處理。
本次沒有新增違規真值、沒有提交 input／outputs、沒有推送／合併。

接手 prompt：先讀本交接、各案例 request/result、逐幀 observations、ocr_attempts 與 ocr_crops，再評估距離門檻變動。不可把浮水印 OCR 當成車輛車牌改善；先區分有車輛歸屬與未配對字串。若需判斷距離精度，另收已知距離真值，而非選擇讀牌較多的前處理當答案。
