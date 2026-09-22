# perception-v2 合併記錄

來源：`perception-v2/AI-powered-Automatic-Traffic-Violation-Reporting-System-feature-perception-v2`。

- 車輛 PT 與 BoT-SORT 保留，新增來源車輛 ONNX 供設定替換。
- 深度前處理、中央 ROI 中位數取樣、車牌裁切與幾何篩選、EMA、短暫框沿用、
  OCR 抽樣及時序投票整合成獨立後端；文字正規化器取自來源程式。
- 舊主程式批次跳幀／插值未搬入：目前逐幀 BoT-SORT，深度可按 interval 快取。
  不沿用來源 README 的 FPS、精度或「0 誤判」宣稱。
- 修正來源 OCR 正規化失敗時輸出原字串的行為：保留 raw_text，text 使用 null；
  失敗辨識也節流，離開畫面的 OCR 快取有期限。
- 同功能道路／燈號模型沿用 traffic_ai 現有權重；原 road_marking 的訓練框架、
  安裝器與診斷 GUI 留在來源，統一推論入口為 run_pipeline.py。
- 填表 GUI、縣市 drivers、案件與 profile 保留到 reporting/autoForm；未執行網頁填表或送出。
- 燈號資料轉換、標籤檢查、推論腳本搬到 tools/traffic_light，修正推論權重路徑。
- 原範例影片不重複複製；input 與來源影片保留原位。
- model_interfaces.py 保留舊匯入相容性，新整合使用 model_library。

新增權重來源、大小、SHA-256 在 `models/migration_manifest.json`。
原專案未刪除或修改，舊 outputs 未覆寫，整合測試使用獨立子資料夾。
