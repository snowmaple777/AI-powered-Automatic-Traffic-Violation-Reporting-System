# 評估資料與指標

`ground_truth.json` 記錄每支影片的人工答案，`metrics.py` 依此計算指標。重構的每個階段都用同一份答案比較，才能判斷改動是否真的變好。

## 標註方式

1. `category` 是該片主要的違規規則 ID。`cdy`、`soc`、`ctm` 開頭的影片已依檔名預填；`ms`、`s`、`mm` 開頭的請自行填入，例如 `red_light_stop_line_crossing`。
2. 逐一核對影片，把每個真實違規填入 `events`：

   ```json
   {"rule_id": "red_light_stop_line_crossing", "start_sec": 3.0, "end_sec": 4.0, "plate": "CAA-8738", "note": "白色貨車"}
   ```

   `start_sec`／`end_sec` 是違規發生的時間區間（例如車頭越過停止線的前後）。`plate` 看不清楚就填 `null`。
3. 整支影片核對完成後設 `"reviewed": true`。沒有違規的影片也要設 true，`events` 保持空陣列，這樣系統在該片的輸出才會被計為假陽性。

規則 ID：`red_light_stop_line_crossing`、`double_yellow_line_crossing`、`double_white_line_crossing`、`car_motorcycle_waiting_area`、`crosswalk_stopping`。

## 執行

```
.venv\Scripts\python.exe eval\metrics.py outputs\某次輸出資料夾 [--min-status confirmed]
```

- 影片層級：有填 `category` 的影片是否至少輸出一個同規則事件。不需要時間標註就能使用。
- 事件層級：只計 `reviewed=true` 的影片，依規則與時間（±`time_tolerance_sec`）配對，計算精確率、召回率與車牌完全正確率。
