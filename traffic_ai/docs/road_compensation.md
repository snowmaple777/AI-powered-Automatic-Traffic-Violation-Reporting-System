# 停止線補償接口與行為

補償位於模型輸出與違規輸入之間，程式位於 `road_compensation/`。
`model_library/` 與 `violation_engine.py` 的辨識／判斷程式不需改寫。
第一版只處理 `stop line`；其他標線類別原樣通過。

## 資料流與公開接口

```text
模型的原始標線 + 車輛框 + 原影片影格
                     ↓
RoadMarkingCompensator.process(...)
                     ↓
CompensationResult
  raw_markings          原始結果，不更動座標、不補線
  compensated_markings  觀測／預測／品質不足的補償結果
  rule_markings         通過品質門檻的結果，格式與原標線相同
  diagnostics           鏡頭配準品質、失效原因及可判定狀態
                     ↓
attach_compensation(observation, result)
                     ↓
原違規引擎 evaluate(observation)
```

```python
from road_compensation import (
    CompensationConfig, RoadMarkingCompensator, attach_compensation,
)

compensator = RoadMarkingCompensator(
    CompensationConfig.from_json("configs/marking_compensation.json")
)
result = compensator.process(
    frame,                         # uint8 BGR 原圖
    frame_index=frame_index,        # 從 0 開始、連續遞增
    timestamp_sec=timestamp,        # 秒，必須遞增
    road_markings=raw_markings,     # 模型原始 records
    vehicles=vehicle_records,      # 每筆需 bbox_xyxy；包含行人也可
)
rule_input = attach_compensation(observation, result)
events = violation_engine.evaluate(rule_input)

# 新影片開始前：不可沿用上一支影片的停止線記憶。
compensator.reset()
```

所有幾何均使用原影片像素。標線每筆需 `class_id/class_name/pixels/components`；
component 需 `area/bbox_xywh/line_xyxy`，輸出補齊 centroid。
`process` 不修改輸入影像、車輛或標線；adapter 也回傳獨立 observation。
可注入其他 `motion_estimator`，只需提供
`estimate(previous_gray, current_gray, previous_road_mask, current_road_mask) -> MotionEstimate`。
有效矩陣須為前幀→當幀的原圖座標 3×3 單應矩陣。

## JSONL schema 1.2

| 欄位 | 定義 |
|---|---|
| observations.road_markings_raw | 完整模型原始輸出 |
| observations.road_markings_compensated | 含品質不足線段的補償輸出，供診斷、繪圖 |
| observations.road_markings | 僅合格停止線＋其他原始類別；既有引擎直接讀取 |
| postprocessing.road_marking_compensation | enabled、status、motion、expired_tracks、reset_reason、設定值 |

補償 component 增加 `compensation`：

| 欄位 | 定義 |
|---|---|
| id | 補償追蹤 ID；與既有引擎的 marking_id 是不同命名空間 |
| source | observed / predicted_occluded / predicted_missing |
| support_score | 0–1 的時序支持分數；不是經校準的機率，也不是模型 confidence |
| uncertainty_px | 估計的位置不確定性，原圖像素 |
| observed_frames | 該軌跡實際得到觀測的次數，預測幀不增加 |
| last_observed_frame / age_seconds | 最後實際觀測及距今秒數 |
| occlusion_ratio | 有限線段採樣點被車框覆蓋的比例 |
| rule_eligible | 是否通過最低觀測次數、支持分數及不確定性門檻 |

`status=unknown` 表示本層沒有可提供給停止線規則的合格標線，**不等於沒有違規**。
若有部分合格線段則為 usable，不表示整個路口都可判定。
配準失敗時當幀原始觀測仍保留，但重新累積可信度，不把舊線假設成靜止沿用。
未啟用時只有 `enabled:false`，road_markings 保持原始語意，不加入 raw/compensated 副本。

## 重要演算法

1. 前後幀道路 ROI 排除車輛框，使用光流前後一致性與 RANSAC 單應矩陣估計背景運動。
   檢查內點比例、空間分布、重投影誤差、變換幅度與配準後亮度差；低紋理、嚴重遮擋或
   切鏡導致失敗時清除歷史，不使用 identity matrix 假裝配準成功。
2. 將模型全畫面延伸主軸裁回 component 支撐範圍，再合併同類、近共線且距離有限的片段。
   大間隙還必須有車框遮擋線索；不將短線無限制延伸到整個車道／畫面。
3. 把前幀停止線投影到當幀後配對，再平滑觀測；保持當幀的觀測 x 範圍。
4. 已確認軌跡漏偵測時才沿用歷史：車框遮擋可保留較久，沒有遮擋依據則很快到期。
   分數按距離最後觀測的時間指數衰減，不確定性隨配準／預測累積。
5. 低分或高不確定性線段留在 compensated 供檢視，不進入 rule_markings。

`configs/marking_compensation.json` 可調整，主要預設：

| 設定 | 預設 |
|---|---:|
| min_observations | 3 次實際觀測 |
| max_occluded_seconds | 0.5 秒 |
| max_missing_seconds | 0.1 秒 |
| confidence_half_life | 0.35 秒 |
| min_rule_support | 0.65 |
| max_uncertainty_ratio | 影像高度的 1% |
| smoothing_alpha | 0.65，當幀權重 |

0.5 秒是保留上限，**不是保證 0.5 秒內都能判定**；品質可能更早低於門檻。

## 使用、旁路與重播

```powershell
# 主流程預設啟用補償
.\.venv\Scripts\python.exe run_pipeline.py input/ms01.mp4 --save-video --rules red_light_stop_line_crossing

# 旁路：恢復原始模型輸出直交規則
.\.venv\Scripts\python.exe run_pipeline.py input/ms01.mp4 --no-marking-compensation --output-dir outputs/raw_comparison

# 使用自己的門檻設定
.\.venv\Scripts\python.exe run_pipeline.py input/ms01.mp4 --marking-compensation-config configs/marking_compensation.json

# 有影片和原 JSONL 時，不用重跑模型
.\.venv\Scripts\python.exe compensate_recording.py input/ms01.mp4 outputs/ms01_detections.jsonl --output outputs/compensated/ms01_detections.jsonl --save-video
.\.venv\Scripts\python.exe evaluate_violations.py outputs/compensated/ms01_detections.jsonl
```

重播要求原影片與 JSONL 從第 0 幀對齊，檢查索引、尺寸、FPS 及時間戳。
相同尺寸／FPS 的不同影片無法僅靠舊 JSONL 識別，呼叫端須提供正確影片。
重播已補償資料時自動使用其中的 raw 欄位，避免重複補償。
標記影片：青色實線=觀測、紫色虛線=預測、灰色=品質不足。
CSV 的 road_marking 保留原始資料，另加 road_marking_compensated 列及完整 metadata。

## 邊界與驗證方式

單應矩陣假設道路近似平面；車框不是精確遮擋分割；無法修正持續的停止線類別誤判。
補償 ID 只用於補償，既有引擎仍使用自己的標線配對、訊號配對與越線狀態機。
2026-09-20 已另行修正规則的跳幀跨越、間斷期限、方向及片段交接，見 red_light_fix.md。
車輛接地位置誤差以及红綠燈和車道對應問題仍存在。品質合格的短期預測可參與候選判斷，來源會保留在事件
stop_line.compensation 中；事件依然要求人工複核。

比較時應在同一份模型原始輸出上做有／無補償 A/B，人工標記停止線位置與事件，再計算
誤報、漏報、未知比例及位置誤差。補償線段變多或 usable 幀變多不代表準確率已提高。

本次實測記錄與未變更檔案雜湊見 [驗證記錄](compensation_validation.md)。
