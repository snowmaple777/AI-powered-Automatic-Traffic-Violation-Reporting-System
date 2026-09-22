# 模型替換接口

所有後端繼承 `model_library.PerceptionModel`：

```python
from model_library import PerceptionModel, ModelResult

class MyVehicleModel(PerceptionModel):
    def __init__(self, weights, device="cpu", **options):
        # 在這裡載入自己的引擎；模組 import 時不要載入模型。
        self.weights = weights

    def infer(self, frame, context, upstream):
        # frame 是原圖 uint8 BGR，context 有 index / timestamp_sec。
        # 將實際結果轉成下述標準欄位。
        return ModelResult(records=[])

    def reset(self):
        # 清除新影片不應沿用的追蹤與快取。
        pass

    def close(self):
        # 釋放模型資源。
        pass
```

若存成專案根目錄的 `my_models.py`，設定可寫：

```json
{"version":1,"models":{"vehicles":{
  "backend":"my_models:MyVehicleModel",
  "params":{"weights":"../models/my_vehicle.bin"}
}}}
```

範例僅示範接口，沒有實際偵測功能。自訂模組必須可由 Python import；設定檔應視為可信程式設定。
也可透過 Python 呼叫 `register_backend(name, factory)` 再建立管線。

## 替換內建模型

- 同一引擎：改 `params.weights`；路徑相對 JSON 所在目錄，絕對路徑也可。
- 車輛 YOLO 支援 PT / ONNX；已提供 `../models/vehicle/yolo26s.onnx`。
  `configs/onnx_vehicle.json` 是可直接執行的 ONNX 車輛替換範例。
  固定尺寸 ONNX 的 `imgsz` 需符合模型輸入。`classes` 指定偵測類別 ID，
  `class_names` 可寫成 `{"0":"car","1":"motorcycle"}` 將新模型 ID 對應穩定語意。
- 燈號支援 `class_names`；請對應 `red`、`green`、`yellow`。
- 道路模型需一起修改 `config`、`weights`、`sha256`，必要時指定 `classes`、`palette`。
  雜湊需來自可信來源，載入時會驗證。精簡 MMSegmentation 僅含現有 SegFormer 依賴；
  不同架構應新增後端，自行管理相依套件。
- `enabled:false` 停用階段；`params.device` 可覆蓋該階段的共用裝置。

## 階段契約

| 階段 | records 必要內容 | artifacts |
|---|---|---|
| vehicles | class_id, class_name, confidence, bbox_xyxy, center, bottom_center, track_id, object_id, track_age_frames | 任意非序列化資料 |
| traffic_lights | class_id, class_name, confidence, bbox_xyxy, center, bottom_center, track_id（可 null） | 同上 |
| road_markings | class_id, class_name, pixels, components（area/bbox_xywh/centroid/line_xyxy） | mask、RGB palette 用於疊圖 |
| depth | object_id, track_id, distance_m（可 null）, source_frame | depth_map |
| plates | 偵測框欄位同上，object_id/track_id 關聯車輛，held 表示沿用框 | 任意 |
| ocr | 保留 plates 欄位，增加 raw_text/text/text_confidence/confirmed/ocr_source_frame | 任意 |

座標全部為原影片像素，confidence 為 0–1，records 必須能 JSON 序列化。
無追蹤 ID 時 object_id 為 null，不得將所有無 ID 目標合併成同一快取。
OCR 無有效結果時 text 為 null；不得拿無效原字串充當正規化結果。
車牌正規化為沿用的啟發式規則，不保證涵蓋所有號牌格式。

固定順序：vehicles → traffic_lights → road_markings → depth → plates → ocr。
upstream 是各階段的 ModelResult；未執行／停用的階段提供空結果。
不得修改輸入影像或上游結果。plates 需要 vehicles；ocr 需要 plates；depth 可停用。
可直接替換六種既有任務的模型；新增任務類型需擴充 STAGES、schema 及主程式。

```python
from model_library import FrameContext, ModelPipeline, load_config
with ModelPipeline(load_config("configs/full.json"), device="cpu") as pipeline:
    results = pipeline.infer(frame, FrameContext(index=0, timestamp_sec=0))
    # 開始另一支影片前呼叫 pipeline.reset()
```

模型模組 import 不載入權重。初始化或推論失敗會報錯，不冒充空結果。
車輛 ID 不跨影片保留；大型遮罩及深度陣列只在 artifacts，不寫入 JSONL。
舊 CLI 的 --vehicle-conf 等參數會覆蓋設定；自訂 backend 需支援相應參數，或勿傳該 CLI 選項。
