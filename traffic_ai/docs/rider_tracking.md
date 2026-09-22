# 騎士與兩輪車組合追蹤

主流程預設在模型及標線補償之後執行關聯，不需要另裝模型。
person 與 motorcycle / bicycle 以水平重疊、上下位置配對；雙方必須為
無歧義的最佳配對，至少三次觀測後才標記 confirmed。
人物或車輛其中一個原始 ID 連續、位置及尺寸相容時可承接另一個 ID 的切換。
超過 0.3 秒沒有匹配即過期；兩邊 ID 都變更則不猜測身份。

影片綠框表示已確認的組合，橘框表示待確認。框涵蓋騎士及車輛，標籤含
rider-group ID、R（騎士原始 ID）、V（車輛原始 ID），圓點是車框底邊中心。
這仍是接地點代理值，不是輪胎定位。確認後隱藏成員重複框，避免標籤堆疊。
沒有當幀車輛觀測或配對不明時，不繪製虛構組合，也不推估接地點。

JSONL 保留 observations.vehicles，另新增 rider_groups 與 rule_vehicles。
rule_vehicles 中已确认的兩輪車使用組合 ID，保存 source_object_id、rider_id；
違規引擎優先使用這份輸入，幾何仍採車輛框。原本不支援自行車的規則沒有擴張。
CSV 仍為原始模型資料，組合資訊請讀 JSONL。
組合確認不代表違規確認，停止線與紅燈規則仍需分別成立。

正常啟動加 --save-video 就會標示組合。重用既有辨識結果、不重跑模型：

```powershell
.\.venv\Scripts\python.exe tools/replay_riders.py input/ms03.mp4 outputs/ms03_detections.jsonl --output-dir outputs/rider_groups
```

重播影片只標示騎乘組合，用於清楚檢查關聯；正式主流程另保留號誌與標線標註。
ms03 的 rider vehicle-000001 在組合 rider-group-000004 下，已觀察到
機車成員從 vehicle-000066 換成 vehicle-000112 時仍維持相同組合 ID。
這是啟發式關聯而非訓練過的騎乘辨識器；密集遮擋仍可能漏配或錯配。
