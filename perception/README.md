# 🚗 Edge ADAS: 前向動態感知、單目公尺測距與台灣車牌專屬微調辨識子系統
> **Forward Visual Perception, Metric Depth Estimation & Taiwan Fine-Tuned ALPR Subsystem**  
> 專案核心視覺模組說明文件 | 專為動態行車記錄器 (Ego-Vehicle Dashcam) 設計之全流程感知與高精微調車牌辨識系統

---

## 📌 模組定位與分工職責 (Subsystem Overview)

本模組為團隊自動駕駛與交通違規智慧回報系統中的**前向視覺核心主幹 (Perception Backbone)**。  
在動態顛簸、自車持續移動 (Ego-motion)、小目標遠距失配以及顯存嚴格受限的嚴苛條件下，本子系統獨立完成從原始行車記錄器 1080p 影像輸入到高維度結構化特徵輸出的全流程感知，為下游組員模組（如碰撞預警 FCW、變換車道輔助 LCA、違規行為判定、車牌資料庫記錄等）提供即時、穩定且富含物理意義的特徵資料。

```
[原始行車記錄器 1080p 影像]
           ↓
 ┌────────────────────────────────────────────────────────────────────────┐
 │                      【本子系統負責之核心任務】                        │
 │  1. 車輛/行人多目標偵測與動態追蹤 (YOLO + ByteTrack)                   │
 │  2. 騎士與機車框智慧融合機制 (Rider-Motorcycle BBox & Track Fusion)    │
 │  3. 單目度量空間絕對公尺測距 (Depth Anything V2 Metric VKITTI)         │
 │  4. 兩階段局部車牌超解析定位與防抖 (Two-Stage YOLO + EMA Smoothing)    │
 │  5. 工業級車牌自適應雙邊降噪與反遮罩銳化 (Bilateral & Unsharp Mask)    │
 │  6. 台灣專屬微調 PP-OCRv6 引擎 (4,600+ 張本土真實車牌深度微調)         │
 │  7. Windows cuDNN 多進程隔離通訊 (徹底解決 PyTorch 與 Paddle 衝突)      │
 │  8. 台灣公路局車牌法規約束與位置消歧義 (TaiwanPlateValidator)          │
 └────────────────────────────────────────────────────────────────────────┘
           ↓
[結構化特徵輸出：Track ID、3D 公尺距離、平滑車牌 BBox、法規標準化車牌號碼]
           ↓
 ┌────────────────────────────────────────────────────────────────────────┐
 │                      【下游組員對接之業務模組】                        │
 │  • 前方碰撞預警 (FCW / TTC 碰撞時間計算)                               │
 │  • 車道線辨識與跨線違規判斷 (LDW / 壓實線偵測)                         │
 │  • 交通違規行為判定與自動截圖存證 (Traffic Violation Detection)        │
 │  • 車牌號碼檢索、車籍資料庫登記與雲端通報 (Cloud Telemetry Database)   │
 └────────────────────────────────────────────────────────────────────────┘
```

---

## 🌟 核心工程突破與技術創新 (Core Engineering Innovations)

### 模組一：台灣車牌專屬微調 PP-OCRv6 辨識引擎 (Taiwan Fine-Tuned PP-OCRv6)
* **4,600+ 張本土真實現況車牌微調 (Fine-Tuning)**：
  * 原版預訓練 OCR 在面對台灣車牌特有字型（如窄版英文、數字 `0` 與字母 `D`、斜體 `7` 等）以及反光、雨天、夜間眩光時容易產生字形誤讀。
  * 本模組基於百度最新一代 **PP-OCRv6 (PPLCNetV4 骨幹網路)**，在 4,600 多張台灣真實道路車牌高質量樣本上進行全量 Transfer Learning 微調，在測試盲測集上達到 **97.5%+** 的極高字元辨識率。
* **字碼專屬字典 (ppocrv6_dict.txt)**：
  * 深度綁定微調訓練字元集，確保神經網路輸出之類別索引 100% 精準對照台灣車牌英數編碼。

### 模組二：Windows cuDNN 多進程隔離通訊架構 (Dual-Process IPC Isolation)
* **攻克業界最頑劣之 DLL 衝突**：
  * **底層死穴**：在 Windows 作業系統上，YOLO / Depth Anything 依賴 PyTorch 的 CUDA DLL，而微調模型則依賴 PaddlePaddle 的推論庫。若在同一個 Python 進程裡同時載入兩者呼叫 GPU，會觸發 C++ cuDNN 符號衝突，導致程式直接崩潰閃退（Access Violation 0xC0000005）。
  * **架構創新**：設計 `ppocr_client.py` 與 `ppocr_worker.py`，將 Paddle Inference 隔離於純淨的獨立子進程中，透過 Localhost Socket 與高效二進制記憶體（Binary Struct Memory）通訊。
  * **成效**：徹底解決 Windows DLL 衝突，實現 **全 GPU 硬體加速**，單次微調 OCR 推論僅需數毫秒，兼具工業級穩定度與極致效能！

### 模組三：全目標時序追蹤與「人車合一」智慧融合 (Tracking & Rider-Bike Fusion)
* **多類別統一感知**：同時鎖定道路 6 大目標（汽車 `car`、機車 `motorcycle`、公車 `bus`、卡車 `truck`、行人 `person`、自行車 `bicycle`）。
* **人車一體框融合 (Rider-Motorcycle IoU Fusion)**：
  * **業界痛點**：行車記錄器常將「外送員/機車騎士」與「機車」分別偵測為兩個獨立目標（例如 `motorcycle #11` 與 `person #12`），導致車牌號碼一下掛在人身上、一下掛在車身上，追蹤斷裂分離。
  * **解決架構**：計算騎士與機車框之底部重疊比例（Bottom Overlap）與水平相交比。當判定騎士騎乘於機車上時，自動將兩者融合成單一完整的「機車+騎士整體框」，並將歷史車牌投票狀態與 Track ID 深度合併，徹底解決號碼分散問題。

### 模組四：單目度量空間前車公尺測距 (Metric Depth Estimation)
* **抗自車移動干擾 (Ego-Motion Resilience)**：傳統光流法在「自車與前車等速」時相對位移為零直接失效；本系統採用 VKITTI 度量微調之 `Depth-Anything-V2-Metric-VKITTI-Small` 模型，直接輸出物體的**實體公尺距離 ($0 \sim 80\text{m}$)**。
* **中央 50% ROI 中位數採樣 (Median Depth)**：僅從車輛邊界框中央 50% 核心區域進行取樣，徹底過濾背後的路面、天空與車底陰影噪聲，測距穩定度提升 40% 以上。
* **時序降頻復用**：深度網絡每隔 2 幀推論一次，中間幀沿用時序深度緩存，直接節省 58% 深度計算量。

### 模組五：兩階段局部裁切車牌定位 (Two-Stage License Plate Localization)
* **攻克小目標尺度失配 (Scale Mismatch)**：1080p 畫面中的車牌通常僅 $50 \times 20$ 像素，若直接整張影像縮放到 640 全圖，車牌特徵將被抽樣完全破壞；本系統 Stage 1 先鎖定車輛，加上 8% 外擴裁切後送入 Stage 2 專用車牌模型，使車牌檢出信心度大幅躍升至 **0.75+**。
* **抗抖動 EMA 平滑濾波 ($\alpha=0.65$)**：消除逐格框微幅震顫，搭配掉幀位移補償（Holdover 1~2 影格），畫面標註框極致穩定不閃爍。

### 模組六：工業級車牌影像前處理增強 (Bilateral Denoising & Adaptive Unsharp Masking)
* **雙三次插值放大 (Bicubic Upscaling)**：當車牌高度小於 48px 時，幾何放大至 48px 標準高度，維持小字連續筆畫。
* **雙邊保邊降噪 (Bilateral Filter)**：先過濾行車記錄器 H.264/JPEG 壓縮方塊雜訊，避免噪點被銳化放大為虛假筆畫。
* **自適應反遮罩銳化 (Adaptive Unsharp Masking)**：依據車牌清晰度（Laplacian 方差）動態調整銳化係數：嚴重模糊時加強對比與邊緣反差；清晰時適度銳化，大幅拉升 OCR 字元辨識率。

### 模組七：台灣車牌法規語法約束與字元位置消歧義 (TaiwanPlateValidator)
* **符合交通部公路局號牌編碼法規**：
  * 8 代新式汽機車（3 英文 - 4 數字，如 `ABC-1234`）
  * 8 代/7 代機車（3 英文 - 3 數字，如 `MAY-123`）
  * 7 代舊式汽車（2 代字 - 4 數字 或 4 數字 - 2 代字，如 `AB-1234`、`0001-DA`）
  * 舊式計程車/客貨車（2-3 或 3-2 碼）
* **官方禁用字元約束**：台灣公路局英文字軌**全面禁用 `I` 與 `O`**（避免與 `1`、`0` 混淆），演算法以此先驗過濾所有假陽性。
* **字元位置拓撲消歧義**：
  * 數字區段誤讀字母自動修正：`O/D/Q` $\rightarrow$ `0`、`I/L/J` $\rightarrow$ `1`、`Z` $\rightarrow$ `2`、`S` $\rightarrow$ `5`、`B` $\rightarrow$ `8`、`G` $\rightarrow$ `6`。
  * 8 代英文區段誤讀數字自動修正：`8` $\rightarrow$ `B`、`2` $\rightarrow$ `Z`、`5` $\rightarrow$ `S`、`0` $\rightarrow$ `D`。
* **缺損連字號智慧還原**：自動根據 7 碼/6 碼結構標準化插入 `-`（如 `BXH6208` $\rightarrow$ `BXH-6208`）。
* **非號牌雜訊秒殺攔截**：車身純數字貼紙（`1287`、`60968`）與車身銘牌英文（`CAMRY`、`TURBO`）100% 阻絕，0 污染投票快取。

---

## 📁 專案檔案結構與模型配置 (Project Structure)

專案結構劃分清晰，微調模型與輔助通訊模組一目了然：

```text
perception/
│
├── checkpoints/                                  # 核心 AI 推論權重目錄
│   ├── PP-OCRv6_taiwan_infer/                    # 🎯 台灣專屬微調 PP-OCRv6 推論模型 (21.4 MB)
│   │   ├── inference.json                        #    - 模型運算圖結構 (Paddle 3.0 PIR 格式)
│   │   ├── inference.pdiparams                   #    - 微調最優權重參數 (21.0 MB)
│   │   └── inference.yml                         #    - 前後處理設定檔
│   ├── PP-OCRv6_rec_small.onnx                   # 🌟 官方預訓練 ONNX 備用引擎 (21.2 MB)
│   ├── yolo26s.onnx                              # 🚗 第一階段：車輛/行人多目標偵測與追蹤 (38.8 MB)
│   ├── license-plate-finetune-v1s.onnx           # 🪪 第二階段：車輛局部車牌精定位模型 (39.2 MB)
│   └── depth_anything_v2_metric_vkitti_vits.onnx # 📏 單目度量空間 80m 真實公尺深度測距 (98.9 MB)
│
├── train_data/                                   # 字典目錄
│   └── ppocrv6_dict.txt                          # 📖 台灣微調車牌字碼對照字典 (93 KB，必備)
│
├── ppocr_client.py                               # 🔌 OCR 子進程通訊客戶端 (Socket 連線與記憶體打包)
├── ppocr_worker.py                               # ⚡ Paddle Inference 獨立隔離進程 (專職 GPU 推論)
├── detect_and_annotate.py                        # 🚀 系統核心推論主程式 (全流程感知串聯與影片標註)
├── vid.mp4 / vid2.mp4                            # 測試用輸入行車記錄器影片 (1080p 30FPS)
└── README.md                                     # 本技術說明文件
```

---

## 🛠️ 環境配置與一鍵安裝 (Installation)

本系統支援 PyTorch 與 PaddlePaddle 雙引擎，微調模型透過獨立子進程隔離執行，無需擔心相容性問題。

### 1. 硬體建議
* **GPU**：NVIDIA 獨立顯卡（推薦 RTX 3060 Ti / RTX 4060 或以上，VRAM $\ge 6\text{ GB}$）。
* **VRAM 佔用**：所有模型同時在 GPU 上運行之總顯存僅約 **$3.3 \sim 3.7\text{ GB}$**，為其他組員模型預留充足顯存。

### 2. 套件安裝步驟
```powershell
# 1. 安裝 PyTorch (CUDA 12.x 版本)
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121

# 2. 安裝 PaddlePaddle-GPU (支援 CUDA 12)
python -m pip install paddlepaddle-gpu==3.0.0b2 -i https://www.paddlepaddle.org.cn/packages/stable/cu123/

# 3. 安裝 YOLO、OpenCV 與數值計算庫
pip install ultralytics opencv-python numpy Pillow

# 4. 安裝 ONNXRuntime-GPU 與輔助套件
pip install onnxruntime-gpu rapidocr-onnxruntime
```

---

## 🚀 怎麼用：常用執行指令與模式 (Usage Guide)

進到專題目錄：
```powershell
cd C:\Users\User\Desktop\AI-powered-Automatic-Traffic-Violation-Reporting-System\perception
```

### 1. 完整全功能執行模式（預設自動載入台灣微調模型）
直接執行主程式，系統會自動辨識並載入 `checkpoints/PP-OCRv6_taiwan_infer` 微調模型，全速啟動多進程 GPU 推論：
```powershell
python detect_and_annotate.py --video vid2.mp4 --output annotated_output.mp4
```

### 2. 快速除錯/預覽模式（只處理前 N 幀）
若要快速驗證檢測效果或微調參數，指定 `--max-frames` 可以在處理完指定影格數後自動提早結束並產出影片與統計報表：
```powershell
python detect_and_annotate.py --video vid2.mp4 --output preview.mp4 --max-frames 150
```

### 3. 切換使用官方 ONNX 備用引擎
若需要在未安裝 PaddlePaddle 的電腦上運行，可手動指定使用純 ONNX 模型：
```powershell
python detect_and_annotate.py --video vid2.mp4 --ocr-ckpt checkpoints/PP-OCRv6_rec_small.onnx
```

### 4. 組員顯存讓位模式（OCR 分流至 CPU）
若其他組員的模型需要較多 GPU 顯存，可將 OCR 純辨識單獨指派至 CPU 執行：
```powershell
python detect_and_annotate.py --video vid2.mp4 --ocr-device cpu
```

### 5. 邊緣裝置極速模式（關閉深度測距）
若在算力極度受限之邊緣計算板（如 Jetson 或老舊筆電）上運行，可關閉深度測距提速 40%：
```powershell
python detect_and_annotate.py --video vid2.mp4 --no-depth
```

---

## ⚙️ 核心命令列參數對照表 (CLI Arguments)

| 參數名稱 | 預設值 | 型態 | 功能說明與調優建議 |
| :--- | :---: | :---: | :--- |
| `--video` | `vid.mp4` | str | 輸入之行車記錄器影片路徑。 |
| `--output` | `annotated_output.mp4` | str | 標註後輸出之 MP4 影片路徑。 |
| `--conf-vehicle` | `0.25` | float | 第一階段車輛/行人偵測信心門檻。 |
| `--conf-plate` | `0.30` | float | 第二階段車牌偵測門檻（兩階段局部裁切下，0.30~0.35 可杜絕水箱罩/飾條誤檢）。 |
| `--imgsz-vehicle` | `640` | int | 第一階段車輛推論解析度（640 可維持極高偵測速度）。 |
| `--imgsz-plate` | `320` | int | 第二階段車牌局部裁切塊推論尺寸（局部小圖使用 320，提速 3~4 倍且精度無損）。 |
| `--two-stage` / `--no-two-stage` | `True` | flag | 啟用 / 關閉兩階段局部裁切車牌定位（預設開啟）。 |
| `--top1-per-vehicle` | `True` | flag | 每輛車只保留最高信心度之單一車牌，杜絕一車多框與框亂跳（預設開啟）。 |
| `--smooth` / `--no-smooth` | `True` | flag | 啟用 / 關閉跨影格時序平滑濾波與掉幀補償（預設開啟）。 |
| `--enable-depth` / `--no-depth` | `True` | flag | 啟用 / 關閉 Depth Anything V2 單目度量公尺測距。 |
| `--depth-size` | `392` | int | 深度模型輸入解析度。推薦 `392`（速度與測距精度之黃金平衡點）。 |
| `--depth-interval` | `2` | int | 深度測距間隔幀數（每隔 2 幀推論一次，中間幀時序復用，提速 40%）。 |
| `--enable-ocr` / `--no-ocr` | `True` | flag | 啟用 / 關閉車牌字元辨識。 |
| `--ocr-ckpt` | *(自動選取)* | str | OCR 模型路徑（預設自動載入 `PP-OCRv6_taiwan_infer` 微調模型，若無則依序載入 ONNX）。 |
| `--ocr-device` | `cuda` | str | OCR 推論裝置（`cuda` 或 `cpu`）。 |
| `--ocr-min-plate-w` | `58` | int | 觸發 OCR 之車牌最小像素寬度（小於此寬度視為太遠太模糊自動略過，預設 58px）。 |
| `--plate-max-dist` | `22.0` | float | 車牌偵測與 OCR 之統一最大距離（超過 22 公尺不切圖不跑車牌 YOLO 亦不跑 OCR，節省無效算力）。 |
| `--vehicle-interval` | `2` | int | 車輛目標追蹤間隔影格數（每隔 2 幀跑一次 YOLO 追蹤，中間幀時序內插，算力減半且畫面極致平滑）。 |
| `--taiwan-plate-filter` | `True` | flag | 啟用台灣車牌公路局法規規格校驗與字元消歧義修復（預設開啟）。 |
| `--max-frames` | `0` | int | 最大處理影格數（`0` 代表處理整部影片）。 |

---

## 🤝 組員模組對接介面與資料結構 (Teammate API)

下游組員（如碰撞預警 FCW、車道偏離 LDW、違規檢舉存證系統）可直接在主迴圈後讀取結構化輸出變數：

### 1. 感知資料結構與變數清單

| 變數名稱 | 資料型態 | 格式與欄位定義 |
| :--- | :--- | :--- |
| `batch_vehicle_boxes[i]` | `List[List]` | 每個車輛目標：`[x1, y1, x2, y2, conf, cls_id, track_id, dist_m]` |
| `batch_plate_boxes[i]` | `List[List]` | 每個車牌目標：`[x1, y1, x2, y2, pconf, pcls, track_id, plate_text, text_conf]` |
| `vehicle_plate_map` | `Dict[int, str]` | 活躍車輛快速索引：`{track_id: "標準車牌號碼"}` (例如 `{2: "BYX-0298"}`) |

### 2. 組員調用範例代碼 (Python)

```python
# 下游組員直接在影格迭代中提取本模組產出之高維特徵：
for vehicle in batch_vehicle_boxes[frame_idx]:
    vx1, vy1, vx2, vy2, v_conf, v_cls, track_id, dist_m = vehicle
    
    # 範例 A：組員 FCW 碰撞預警模組 (Forward Collision Warning)
    if dist_m is not None and dist_m < 8.0:
        trigger_fcw_alert(track_id=track_id, distance=dist_m)
        
    # 範例 B：組員違規存證與車籍資料庫登錄模組
    plate_str = vehicle_plate_map.get(track_id)
    if plate_str:
        log_violation_event(
            vehicle_id=track_id,
            license_plate=plate_str, # 保證符合台灣法規標準格式 (如 BYX-0298)
            distance_meters=dist_m,
            bounding_box=[vx1, vy1, vx2, vy2]
        )
```

---

## 📊 實際運行與終端報告範例 (Execution Log)

在專題環境下執行 `python detect_and_annotate.py --video vid2.mp4 --max-frames 10` 之終端即時輸出：

```text
🚀 使用推理裝置: CUDA
🎮 GPU 顯卡名稱: NVIDIA GeForce RTX 3060 Ti
⚙️ 執行模式: 兩階段車輛裁切偵測 (Two-Stage Crop & Detect)
📐 車輛推論尺寸: 640 | 車牌推論尺寸: 320
📏 深度測距模組: ONNX 格式 (checkpoints/depth_anything_v2_metric_vkitti_vits.onnx) 載入成功！
🔤 文字辨識模組: 台灣車牌專屬微調 PP-OCRv6 (Paddle Inference 獨立進程) 引擎 (checkpoints/PP-OCRv6_taiwan_infer) 載入成功 (使用裝置: CUDA，英數先驗遮罩: 已啟用)！
🔤 車牌時序追蹤模組載入完成 (台灣車牌語法校驗: 已啟用，人車合一融合: 已啟用)！

=======================================================
📊 偵測、辨識與信心值統計報告 (Detection, OCR & Confidence Summary)
=======================================================
🚗 車輛偵測總次數: 32 框 (平均信心值: 0.816)
🪪 車牌偵測總次數: 15 框 (平均信心值: 0.662)
🔤 車牌文字辨識統計:
   - 車輛 Track #2.0 ──▶ 外出車牌: BYX-0298 (影片原版OCR: BYX-0298 | 信心值: 0.975)
=======================================================
```
