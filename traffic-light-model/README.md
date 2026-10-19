# 紅綠燈偵測：YOLO26s

2026-10-20 更新。權重為本次 Kaggle 訓練的 best26.pt，發布時沿用 `models/traffic_light_best.pt` 檔名，方便既有呼叫端使用。舊版 YOLO11n 的數據不能當成本模型的指標。

## 類別與介面

0 red、1 green、2 yellow、**3 arrow**。原本只接受三類的下游程式需要明確忽略或處理 arrow，不可把所有其他類別都當 yellow。

本版採單尺度原圖推論：imgsz=1280、conf=0.15、IoU=0.7、max_det=300、FP32、nms=True。這不是舊研究中的 area_downup / 多尺度 B-H30 流程。本權重使用 NMS 分支，不應宣稱本次推論為 NMS-free。

## 安裝與推論

Python 3.10，安裝 `python -m pip install -r requirements.txt`。GPU 另需相容的 CUDA PyTorch。以下指令在本資料夾執行；不會啟動訓練。

```powershell
python predict.py --source "影片完整路徑.mp4" --device 0 --conf 0.15 --output "D:/traffic_output"
```

沒有 GPU 改 `--device cpu`。輸出標框影像/影片與 YOLO txt，分數保持原始值。預設模型相對腳本定位，輸出會轉成絕對路徑。

## 位置追蹤預覽

需要 FFmpeg / ffprobe 放在 PATH，或用 `--ffmpeg`、`--ffprobe` 指定執行檔。此程式已獨立，不依賴研究資料夾。

```powershell
python track_preview.py --source "影片完整路徑.mp4" --device 0 --output "D:/traffic_output/track_v1"
```

先檢查可加 `--dry-run`；短測可加 `--max-frames 10`。輸出目錄必須尚未存在，避免覆寫。

- `comparison.mp4`：左原始 conf>=0.15，右位置追蹤＋EMA 平滑。
- `frames.jsonl`：原始框、類別、信心、追蹤 ID、平滑框、時間戳。
- `summary.json`：模型 SHA256、參數、處理幀數、耗時。
- `encode.log`：影片編碼日誌。

追蹤不依顏色關聯；新軌跡需要 0.15，既有軌跡可由 0.05 接續。漏抓最多保留 0.2 秒，灰色虛線 unknown，信心為 null。只追 red/green/yellow，arrow 不追蹤。參數集中在 `track_preview.py` 的 SETTINGS。沒有顏色投票、不加分；短暫低分偵測仍顯示當幀模型類別。

影片輸出無音軌，以平均時間長度產生固定幀率；JSONL 保留來源時間戳。長影片逐幀解碼，不保留全部像素於記憶體，但 ffprobe 會預讀所有時間戳。

## 已知限制與測試

ms03 的 117 幀曾完成 CPU 左右比較；遠燈判 yellow 的問題仍存在。追蹤可以保留短暫消失的框，但不能救回從未被偵測的燈，也不代表準確率提高。尚未提供此 YOLO26s 的正式 test / 28 幀比較指標；不能沿用先前 YOLO11s 的分數。須人工複核，不能單憑框與顏色判定交通違規。

模型使用的資料來源包含自標台灣圖、Roboflow 台灣資料、BDD、TN-TLD 與負樣本。Roboflow 來源曾有無具名作者及 Taiwan Wanderer 浮水印的授權疑慮，需另確認使用條件；本包不包含原圖、標註、測試影片或資料集。Ultralytics 與上游資料的授權仍適用，詳見各來源條款。

模型檔案雜湊與類別見 `models/metadata.json`。本包沒有自動訓練或自動下載權重步驟。

本機可能保留舊 `predict_stable.py`、`improve.py` 等研究工具；本次發布的入口是 `predict.py` 與 `track_preview.py`，請勿用舊工具解讀這版的追蹤行為或啟動訓練。
