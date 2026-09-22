# 交通影像辨識：可替換模型與 perception-v2 整合

2026-09-22 整合版支援 `--rule all`（`--rules all` 同義）自動啟用所有已註冊規則。
已加入[騎士與兩輪車組合追蹤](docs/rider_tracking.md)、影片組合標註及
[疑似／確認違規事件](docs/violation_events.md)。越線以車輛底邊及前後軌跡判斷，
不因車框上半部與停止線重疊或單獨在線後就回報疑似。

GitHub 程式碼包不含影片、模型權重、虛擬環境或檢舉人個資。
執行前請依 `configs/default.json` 或 `configs/full.json` 將模型權重放入 `models/`；
模型來源與校驗資訊見 `docs/upstream/`、`models/migration_manifest.json`。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run_pipeline.py "影片路徑.mp4" --rule all --save-video
```

已新增獨立停止線補償層：鏡頭運動對齊、時序追蹤、遮擋記憶與品質閘門。
主程式預設啟用，可用 `--no-marking-compensation` 旁路。辨識模型保持獨立。
接口、設定及離線重播見 [停止線補償說明](docs/road_compensation.md)。

2026-09-20 已修正前方藍色貨車漏判：號誌時序證據、駛離鏡頭的越線方向、遮擋後線段交接。
使用 `--rules red_light_stop_line_crossing` 啟用事件判斷；詳見 [修正與回歸結果](docs/red_light_fix.md)。

已加入深度估算、車牌定位、OCR 與獨立填表工具。模型實作位於 `model_library/`，
權重位於 `models/`，透過 JSON 設定檔替換模型。原三模型指令仍可使用。

```powershell
# 在 traffic_ai 內執行；完整功能的額外依賴
.\.venv\Scripts\python.exe -m pip install -r requirements-perception.txt

# 六階段推論
.\.venv\Scripts\python.exe run_pipeline.py input/ms01.mp4 --model-config configs/full.json --max-frames 10 --save-video --output-dir outputs/full

# 接口與快取測試
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

也可將影片拖曳到 `啟動完整辨識.cmd`。`configs/default.json` 保留三模型，
`configs/full.json` 啟用全部六階段。替換同架構模型只需修改 `params.weights`；
不同引擎可實作 `PerceptionModel` 子類，設定 `backend` 為 `模組:類別`。
所有權重與設定路徑相對於 JSON 所在目錄；詳見 [模型接口](docs/model_interface.md)。

完整相依清單安裝 CPU 版 ONNX Runtime；ONNX 階段在無 CUDA provider 時提示並使用 CPU。
PyTorch 模型仍依 `--device` 選擇裝置。若需 ONNX GPU 加速，需另外選擇相容的
`onnxruntime-gpu`，不要同時安裝 CPU/GPU 兩個套件。

JSONL schema 為 `1.2`，保留原欄位並新增 `observations.depth`、`observations.plates`
與 `enabled_models`。距離、車牌利用 `object_id` 關聯車輛；CSV 新增距離與文字欄位。
車牌輸出有原始 `raw_text`、正規化 `text`、`text_confidence`、`confirmed`、`held`。
`confirmed` 只代表時序投票達門檻；無有效文字時 `text` 為 null。
深度是模型估計公尺值，`source_frame` 表示快取來源影格，並非相機實測標定結果。

填表 GUI、縣市 driver、案件範例與本機 profile 已搬到 `reporting/autoForm/`，
可用 `啟動填表.cmd` 或以下指令獨立啟動。推論不會自動填表或送出案件，日期、地點、
違規敘述與證據仍需使用者確認及提供。profile 與案件已加入 `.gitignore`。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-reporting.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe reporting/autoForm/main.py
```

來源與合併範圍見 [合併說明](docs/migration.md)。以下為原三模型操作說明。

同一影格依序經過三個模型：車輛 YOLO、道路標線 SegFormer、交通燈 YOLO。模型固定輸出逐幀 JSONL 與方便試算表分析的 CSV；疊加標記影片由 `--save-video` 決定是否生成。

車輛使用 BoT-SORT 追蹤，並以 sparse optical flow 執行全域相機運動補償，適合行車紀錄器等移動鏡頭。車輛 YOLO 權重沒有修改。每段影片中的車輛會獲得獨立的 `track_id` 與 `object_id`（例如 `vehicle-000017`）。

## 模型接口

`model_interfaces.py` 提供 `VehicleDetector.infer(frame)`、`RoadMarkingSegmenter.infer(frame)` 與 `TrafficLightDetector.infer(frame)`。主流程位於 `run_pipeline.py`。

## 安裝與執行

道路標線實體權重已補齊並驗證：大小 125,892,250 bytes，SHA-256 `5c8da1343ab2848b480db84431dfdceb27a9e58c1139e7aaeed3bb0d8c2379df`。程式初始化時會驗證雜湊，避免載入錯誤或不明 checkpoint。

建議 Python 3.10、NVIDIA GPU。此 SegFormer 推論不使用 MMCV 的自訂 CUDA 算子；內附 MMSegmentation registry 已縮減為 SegFormer 必要模組，因此相依清單採用 `mmcv-lite`。

BoT-SORT 需要 `lap>=0.5.12`，已列在相依清單中；若環境尚未安裝，追蹤器無法啟動。

```powershell
python -m pip install -r requirements.txt
python run_pipeline.py "輸入影片.mp4" --device auto
```

同時生成標記影片並啟用示範違規規則：

```powershell
python run_pipeline.py "輸入影片.mp4" --save-video --rules red_light_stop_line_crossing
```

短片測試：

```powershell
python run_pipeline.py "輸入影片.mp4" --max-frames 10
```

固定輸出位於 `outputs/`：`*_detections.jsonl`、`*_detections.csv`。指定 `--save-video` 才生成 `*_annotated.mp4`；指定 `--rules` 才生成 `*_violations.jsonl`。

色彩疊圖是標線模型輸出；藍色框為車輛／道路使用者，黃色框為燈號。JSONL 每行對應一個影格，座標均以原影片像素表示。

車輛紀錄額外包含 `object_id`、`track_age_frames`、`center` 與 `bottom_center`。`bottom_center` 可供後續判斷車輛是否跨越停止線。追蹤編號在每次啟動程式處理新影片時重新建立，不保證跨不同影片保持相同。

## 模型與違規規則分離

`observation_schema.py` 定義模型與規則間的資料契約；每幀包含 `frame` metadata，以及 `observations.vehicles`、`observations.traffic_lights`、`observations.road_markings`。`violation_engine.py` 不載入任何模型，新規則只需繼承 `ViolationRule` 並註冊至 `RULE_FACTORIES`。

模型跑完後，可以修改規則並直接重算，不必再次執行三個模型：

```powershell
python evaluate_violations.py "outputs/影片_detections.jsonl" --rules red_light_stop_line_crossing
```

內建規則只產生 `candidate` 且設定 `review_required: true`。它用連續紅燈、追蹤車輛底部中心及同幀停止線主軸判斷疑似紅燈越線；號誌與車道配對、攝影機標定及法律認定仍應由後續規則補充。

## 精簡內容

原三模型與精簡 MMSegmentation 核心保留；本次新增四個 ONNX 權重、可替換接口、填表工具及燈號資料工具。完整合併取捨見合併說明。
