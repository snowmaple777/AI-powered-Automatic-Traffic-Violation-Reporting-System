# 分階段重構紀錄

每個階段完成後執行 `驗證階段.cmd <階段名稱>`，確認可以運行後才進行下一階段。
重構前的程式碼完整備份在 `_backup_pre_refactor/`。

## 驗證工具（階段 0）

| 工具 | 用途 |
| --- | --- |
| `驗證階段.cmd`／`tools/verify_stage.py` | 單元測試 → 規則重算比對 → ms01、cdy01（前 220 幀）、soc03 完整辨識與各模型耗時 → 評估指標，並與前次報告比較 |
| `tools/replay_regression.py` | 以既有 JSONL 重跑全部規則，與 `outputs/refactor_baseline/replay.json` 比對，不需要 torch |
| `eval/metrics.py`、`eval/ground_truth.json` | 影片層級與事件層級指標；標註方式見 `eval/README.md` |

stage0 實測（RTX 5060 Laptop）：0.61–1.54 FPS。每幀耗時以 plates（129–650 ms）與 road_markings（347–548 ms）為主，非模型處理 119–340 ms；vehicles 只需約 8 ms。

## 階段 1：低風險修正

### 紅燈越線規則（violation_engine.py）
- 降頻推論時沿用的上一幀車框（`source_frame` 不是當幀）不再累積線前／線後次數，也不產生事件。原本一次偵測可以湊成兩次觀測，與雙線、區域規則的作法不一致。
- 停止線換線交接的距離門檻由畫面高 1.5% 收緊為 0.8%（新參數 `max_line_handoff_ratio`）。
- 新增條件 `same_stop_line`：曾經換線交接的越線最多只能是「疑似」。stage0 已證實 ms01 的「確認違規」完全來自交接，補償層多輸出一段停止線就會消失。
- 新增 `tests/test_red_light_rule.py`（4 項，在舊程式上全部失敗）。

### 車牌 OCR（model_library/perception_core.py、ppocr_worker.py）
- ONNX 與 Paddle 後端改為共用 `PlateTextRecognizer`：前處理、CTC 解碼、重試策略（原圖 → 增強 → ±10°／±5°）與台灣車牌驗證只有一份實作。原本兩者的處理順序、角度與驗證規則都不同，worker 只檢查長度。
- 信心值改為所選字元的模型原始機率平均，不再對英數字重新正規化。原本模型偏向中文字時信心仍接近 1（ms01 的 60 次 OCR 信心全部約等於 1.0）。
- 車牌追蹤：單次最高信心的讀數若從未被其他讀數支持，且至少有三筆同長度讀數，改採逐字元加權投票的結果（原有但未使用的 `assemble_composite_plate`）。最高讀數有重複支持時，仍維持「嚴格更高信心才換字」。畫面上顯示的原始讀數不變。
- 新增 `tests/test_plate_ocr.py`（6 項）。

### 推論設定
- `vehicles.interval` 由 2 改為 1（三個模型設定檔）。車輛模型每幀只需約 8 ms，降頻會讓車牌裁切與規則使用錯位的車框，ByteTrack 也只拿到一半的幀率。
- `bytetrack_perception.yaml` 的 `track_buffer` 由 30 改為 60，讓遺失追蹤的保留秒數維持原值。

### 遮罩幾何加速（結果與舊算法一致）
- `mask_components`（legacy.py）：主軸由所有區塊的矩一次算出，只處理該類別所在的範圍。合成 1080p 遮罩上 188 → 20 ms。區塊輸出順序改為由上到下、由左到右；唯一的數值差異是剛好 45° 的平手（同一條線，延伸端點不同）。
- `extract_traffic_regions`：閉運算只在外擴 2×間隙的範圍內進行（可證明與全畫面相同），像素一次分組。104 → 46 ms。
- `extract_double_lines`：逐列邊界改為陣列運算，結果逐位元相同。
- 新增 `tests/test_mask_geometry.py`，與逐區塊、全畫面的參考算法比對。

### 規則重算的刻意差異
`outputs/refactor_baseline/stage1_intended_changes.txt`（舊基準保留為 `replay_stage0.json`）：
- ms01：第 86 幀的紅燈「確認違規」消失，因為其線前觀測來自換線交接。
- soc01：疑似紅燈事件由第 45 幀移到第 46 幀，因為第 45 幀是沿用的快取車框。

### 階段 1 補充：Windows 驗證後的修正
stage1 驗證中 cdy01 的雙黃線事件消失。原因是車輛改為每幀推論後，偵測器在第 130 幀漏掉目標車一幀，而雙線規則只要車輛不在當幀清單就立即刪除狀態，沒有使用 `max_observation_gap_sec`。stage0 只是剛好沒在偵測幀遇到漏偵測。區域規則也有相同寫法。
- 雙線規則：車輛短暫漏偵測時，已 armed／越線中的候選保留到 `max_observation_gap_sec`，並標記位置缺口（事件最多為疑似），處理方式與「參考點暫時不可用」相同；尚在建立起點的候選直接放棄。
- 區域規則：車輛短暫漏偵測時保留累積中的狀態，直到 `max_observation_gap_sec`。
- 以 stage1 在 Windows 產生的 cdy01 JSONL 重算：事件恢復（第 162 幀、變換車道、疑似）；19 個既有 JSONL 的重算結果與基準一致。
- 驗證腳本新增環境基準（固定 CPU／GPU 運算耗時，在影片處理前後各量一次）。stage1 連未修改的燈號、深度模型都慢了約 1.8 倍，需要靠它區分程式變慢還是電腦狀態不同。
- 計時註記：stage0 的 vehicles 8 ms 是含快取幀的平均，每次實際推論約 17 ms（見 stage0+）。

### 已知仍未解決（後續階段）
ms01 的真實違規約在 2 秒處，目前仍未偵測到；燈號與車道對應屬於階段 4 的範圍。
