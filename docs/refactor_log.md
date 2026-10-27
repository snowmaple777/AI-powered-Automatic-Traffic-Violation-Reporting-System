# 分階段重構紀錄

每個階段完成後執行 `驗證階段.cmd <階段名稱>`，確認可以運行後才進行下一階段。
各階段的程式碼以 git tag 保存：`refactor-pre`（重構前）、`refactor-stage1`、`refactor-stage2`、`refactor-stage4b`（階段 3 至 4b）。原本的 `_backup_*` 資料夾內容已逐檔確認與對應 tag 相同，可以刪除；需要舊版程式時以 `git worktree add ..\stage1 refactor-stage1` 取出。

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

## 階段 2：抽出共用核心（不改變行為）

### 驗證方式
- `tools/equivalence_harness.py`：以確定性的假模型輸出（車輛跨越雙線與停止線、騎士與自行車、停在斑馬線上的貨車、紅綠燈切換、一次單幀漏偵測）搭配真實影片畫面，執行完整主流程。與第 1 階段快照 `_backup_stage1/` 比較 JSONL、CSV、規則診斷、事件、標註影片與違規剪輯，9 個輸出檔逐位元一致（只忽略新增的 `schema_version`、`postprocessing.ego_motion`、`postprocessing.double_lines.motion_source`）。不需要 torch，每一步重構後都會執行。
- perception 與 perception_core（騎士融合、車牌配對、車牌追蹤、OCR 包裝層）：6,600 組隨機序列在新舊程式上的輸出逐位元相同。
- 規則重算基準：19 個 JSONL 一致；單元測試 78 項全部通過（含原本需要 torch 的主流程測試）。

### 修改
- **確定性修正**：停止線補償層走訪字串集合，順序隨 Python 雜湊種子改變，同樣輸入每次輸出不同（stage1 與 stage1b 的 ms01 有 16 幀不同）。改為排序走訪，新增 `tests/test_determinism.py`（以三個雜湊種子比較；舊寫法會失敗）。
- **主流程拆分**：`run_pipeline.run` 拆為 `frame_processing.FrameProcessor`（模型輸出 → 觀測紀錄，不載入模型也不寫檔）、`pipeline_outputs`（CSV 與標註畫面），`run` 只負責串接。torch 只在 `--device auto` 時才載入；未安裝時退回 CPU。
- **鏡頭運動獨立**：新增 `road_compensation.EgoMotionTracker`，每幀估計一次並寫入 `postprocessing.ego_motion`；停止線補償器改為接收這份估計（重置條件相同，結果與自行估計一致，見 `tests/test_ego_motion.py`）。下游一律以 `core.motion.road_motion()` 讀取，舊 JSONL 自動退回原位置。
  **刻意的行為改變**：`--no-marking-compensation` 時雙線與斑馬線規則原本 100% 因 `motion_unavailable` 被擋，現在可以正常運作（只有第一幀無前幀可比對）。
- **共用工具 `core/`**：單應矩陣驗證（`core.motion.homography`，原本 4 份）、影格連續性（`core.continuity.is_continuous`，原本 4 份）、`core.geometry.project_points`（`double_lines` 保留轉出口）。
- **類別常數**：COCO 類別編號改用 `perception_core` 的具名常數與 `same_vehicle_family()`（原本 `(0, 1, 3)` 等硬寫數字散落各處）。常數放在 `perception_core`，因為 Paddle worker 以獨立行程載入該檔。
- **命名與死碼**：`model_library/legacy.py` → `detectors.py`（舊路徑保留轉出口）；移除已無呼叫端的 `_fit_line`（移入測試作為參考實作）與重複的 `overlay`。
- **資料契約**：`observation_schema.py` 列出每個欄位的寫入者與讀取方式，新增 `validate_observation()` 與測試（含抽查本機既有輸出）。schema 版本 1.5 → 1.6。
- 驗證腳本新增「與前次逐幀比對」：模型輸出與完整紀錄（忽略預期變更欄位）相同的幀數。

### 未納入本階段
- 騎士配對：`RiderTracker` 對機車已無作用（感知層已融合），但仍處理「人＋自行車」，不能直接移除；需要先把自行車納入感知層的融合，屬於行為改變。
- 紅燈規則的 `_project_line_tracks` 投影判斷條件與 `project_points` 不同（`|w|<1e-8` 與 `w<=1e-6`），保留原樣以免改變行為。
- 事件 ID 格式統一（`rlsl-000001` 與 `來源:規則:...`）會改變輸出，留待之後處理。

## 階段 3：效能

stage2 每幀耗時（RTX 5060 Laptop）：plates 150–790 ms、road_markings 320–380 ms、非模型 120–215 ms，其餘模型合計約 60 ms。

### 3a：非模型處理（輸出不變）
以等價性工具在 1080p 影片上做 cProfile，40 幀共 12.2 s，其中 `copy.deepcopy` 佔 4.3 s（35%）。
- `core.fastcopy.clone`：純資料（dict／list／數字／字串）的快速深拷貝，取代逐幀處理中的 `copy.deepcopy`（雙線、區域、補償、規則、車輛快取）。
- 雙線規則的證據快照改為只保存引用（快照物件每幀新建、之後不再修改；產生事件時 `build_event` 會完整拷貝）。原本起點側每幀都深拷貝整條雙線幾何後又覆蓋掉。
- 標線疊圖改為一次查表上色（原本每類別各做一次全畫面比對）；1080p 153 → 66 ms，逐像素一致。只影響 `--save-video`。
- 結果：同一份 profile 中規則評估 4.13 → 1.38 s、逐幀後處理 3.02 → 2.38 s、總計 12.2 → 8.8 s（40 幀，含假模型與影片寫出）。
- 驗證：等價性工具（cdy01、ms01 兩支影片）9 個輸出檔逐位元一致；以 stage2 實際資料重算 cdy01／ms01／soc03，事件（含 96 KB 的雙線證據）與規則診斷逐位元一致；perception 隨機比對一致。

### 3b：模型效能選項（預設不變，待量測後決定）
- 標線分割 `inference_scale`（取代 1920x1080 的 Resize）與 `fp16`（CUDA autocast）。
- 車牌偵測 `fixed_input`（關閉 ultralytics 依長寬比最小填補，固定方形輸入，避免 CUDA 每遇新形狀重新選擇卷積演算法）與 `batch_crops`（同幀所有車輛裁切合成一批；模型不支援批次時自動退回逐張）。新增 `tests/test_plate_batching.py`。
- `效能評估.cmd`（`tools/profile_models.py`）：量測各組合的速度，以及與現行設定的一致程度（標線：重點類別 IoU；車牌：召回率與 IoU）。

### 3a／3b 的 Windows 驗證結果與修正方向
- stage3a：三支影片完整紀錄與 stage2 逐幀相同（正確性通過）。但非模型時間幾乎不變（ms01 213.6 ms、cdy01 118.6 ms、soc03 159.2 ms）：VM 上的改善沒有在 Windows 上出現，代表那邊的非模型瓶頸是別的部分。主流程因此加入分段計時，寫入 `*_timings.json`，並顯示在驗證摘要。
- 第一輪效能評估（`outputs/verify/profile/`）：
  - 標線分割：fp16 只快約 10%（342 → 309 ms），IoU ≥ 0.997；降解析度不可行：1280x720 快約 3 倍，但停止線 IoU 只剩 0.107，雙白線 0.39。已從評估中移除，改測 `roi_top`（只分割下方道路區域，原解析度）。
  - 車牌偵測：fixed_input（780 ms）、batch_crops（917 ms）、兩者合用（1164 ms）都比現行（720 ms）更慢。「CUDA 形狀變化」的假設不成立；輸入像素越多越慢的模式，較像推論實際跑在 CPU 上。評估工具已加入實際 ONNX providers 與每次呼叫的前處理／推論／後處理耗時。兩個選項維持預設關閉。

### 3c：依 Windows 分段計時修正方向
- stage3b 驗證在最後比較步驟當掉：尋找前次報告時誤抓評估工具的 `profile/report.json`（沒有 label）。已改為只採用含 label 與 clips 的驗證報告。輸出檔完整，從檔案讀回：三支影片與 stage3a 逐幀相同。
- Windows 上非模型每幀分段（stage3b）：鏡頭運動 55–88 ms 為最大宗，ms01 的區域幾何 41 ms，其餘（雙線、規則、補償、寫檔、解碼）合計 5–25 ms。因此 3a 的拷貝優化在這批影片上效果有限（只在有雙線狀態時明顯）。
- 鏡頭運動（結果逐幀相同）：逐點遮罩檢查與空間分佈格子改為陣列運算；光度誤差中位數改用 256 格直方圖精確計算（差值為 0–255 的整數），不再排序上百萬個浮點數；前一幀縮圖快取重用。以三支真實影片＋stage3b 車框重跑，輸出逐幀相同，VM 上 33.4 → 28.0 ms。（光流金字塔重用不可行：OpenCV 的 Python 介面不接受預建金字塔。）
- `--save-masks`：將每幀標線遮罩存成 PNG，驗證腳本預設開啟（存檔時間另計、不列入非模型時間）。有了真實遮罩，就能以真實資料離線重播完整後處理，用於剖析區域幾何等需要遮罩的階段。

### 3d：第二輪模型評估與下一步
- stage3c：三支影片與 stage3a 逐幀相同；鏡頭運動 Windows 上約快 10%（ms01 87.9 → 77.8 ms）。
- 標線分割：只分割道路區域（roi_top）不可行，停止線 IoU 0.374（roi 0.3）／0.267（roi 0.4），模型依賴整張畫面的上下文。拆解：前處理 4 ms、模型前向 294 ms、轉回 CPU 2.6 ms，瓶頸在 GPU。fp16 只快約 5%，不像是運算吞吐量的瓶頸。
  發現 mmseg `EfficientMultiheadAttention.forward` 呼叫 PyTorch 注意力時沒有 `need_weights=False`，PyTorch 因此走慢速路徑，實際產生並回傳完全沒用到的注意力權重矩陣（1080p 第一層約 12.96 萬 x 2,040），也無法使用融合注意力核心。mmseg 自己的 `legacy_forward` 有傳此參數。新增 `fast_attention` 選項（載入後替換該層 forward，不修改 third_party），列入評估。
- 車牌偵測：ONNX 確實在 CUDA 上執行，但 320 裁切每次推論 331 ms，640 路面掃描只要 78 ms；同一模型在 CPU 上做 320 推論約 47 ms。新增 `tools/onnx_bench.py`，直接以 onnxruntime 量測固定形狀、現行「320、320、640」交替與 rect 多形狀三種模式，搭配三種 cuDNN 搜尋設定，排除 ultralytics 的影響。
- 區域幾何（以 stage3c 真實遮罩重播）：VM 上每幀 24 ms，其中形態學閉運算 15 ms（55x55 橢圓核）。找不到結果相同的替代算法（距離轉換在邊界與光柵化橢圓不同；只做膨脹會改變分組），且只在大面積斑馬線影片中明顯，暫不處理。

### 3e：依量測結果設定新預設
第三輪評估（`outputs/verify/profile/`）：
- 標線分割：fast_attention fp32 為 232 ms（IoU ≥ 0.999）；**fast_attention + fp16 為 145 ms**（原始 322 ms，快 2.2 倍），像素一致率 1.0000，停止線 IoU 0.996，其餘重點類別 ≥ 0.999。單獨 fp16 沒有效果（328 ms）：瓶頸是慢速注意力路徑，不是運算精度。
- ONNX 微基準（`onnx_bench.json`）：車牌模型輸入形狀固定時每次約 10 ms（320）／9–13 ms（640x256）；現行「320、320、640」交替中位數 73 ms、最大 559 ms；rect 多形狀輪流中位數 225–251 ms。HEURISTIC 與 EXHAUSTIVE 差異不大，DEFAULT 全面更慢（約 140 ms 起）。結論：onnxruntime CUDA 每次輸入形狀改變都有大量額外成本，與搜尋模式無關。這也解釋了 fixed_input 為何沒效：裁切固定後，同一個 session 仍在 320 與 640 之間交替。
- 新選項 `plates.stable_shapes`：裁切固定 320 方形（同 fixed_input），路面掃描改用第二個 ONNX session（rect 縮放，形狀由畫面尺寸決定、整支影片不變），讓每個 session 只看到單一形狀。新增測試確認兩個 session 分開、只有裁切關閉 rect。
- 預設改為 `road_markings.fast_attention=true`、`road_markings.fp16=true`、`plates.stable_shapes=true`（三個模型設定檔）。裁切的結果與 fixed_input 相同：相對原始逐張的車牌框召回 0.991、平均 IoU 0.988；路面掃描結果不變。

### stage3e 驗證結果
| 影片 | FPS（stage3c → 3e） | 標線分割 ms | 車牌偵測 ms |
| --- | --- | --- | --- |
| ms01（1080p） | 0.61 → 1.85 | 383 → 175 | 890 → 39 |
| cdy01（720p） | 1.46 → 2.91 | 330 → 134 | 164 → 27 |
| soc03 | 0.60 → 2.02 | 615 → 133 | 822 → 59 |

與 stage3c 以實際輸出比對（遮罩以 `--save-masks` 存檔）：車輛、燈號、深度三支影片逐幀相同；標線遮罩像素一致 ≥ 0.99997，重點類別 IoU 0.990–1.000；車牌框 ms01 60/60（IoU 1.000）、cdy01 90/90（0.975）、soc03 37/38（0.976）；規則使用的停止線元件數、雙線數相同，區域數 ms01 481 → 482；三支影片的事件（規則、狀態、幀、車輛）與車牌文字皆相同。

### 3f：鏡頭運動與 GPU 推論並行
加速後非模型處理約占每幀四成，其中鏡頭運動 59–76 ms 最大。它只依賴畫面與車框，因此 `ModelPipeline` 新增 `stage_callback`：車輛偵測完成後立即在背景執行緒（單一工作者、逐幀依序）估計鏡頭運動，與標線、深度、車牌的 GPU 推論重疊；OpenCV 運算會釋放 GIL。輸入相同，輸出逐位元相同（等價性工具兩支影片驗證）。計時改為 `ego_motion_parallel`（背景耗時）與 `ego_motion_wait`（仍需等待的時間）。

### 第 3 階段總結（stage3f）
| 影片 | stage0 FPS | stage3f FPS | 倍數 |
| --- | --- | --- | --- |
| ms01（1080p） | 0.61 | 2.24 | 3.7× |
| cdy01（720p） | 1.54 | 3.50 | 2.3× |
| soc03 | 0.86 | 2.25 | 2.6× |

stage3f 與 stage3e 的輸出三支影片逐幀相同；鏡頭運動等待時間 0.01 ms（完全被 GPU 推論覆蓋）。目前每幀最大項：標線分割 133–167 ms、鏡頭運動（背景）63–77 ms、ms01 區域幾何 42 ms。

## 階段 4 準備：全片評估
`全片評估.cmd`（`tools/batch_eval.py`）：以現行設定辨識 `eval/ground_truth.json` 的 31 支影片（9,461 幀，約 1 小時），模型只載入一次，可中斷後續跑。輸出 `outputs/eval/<label>/`：每片 JSONL、規則診斷、逐幀遮罩，以及摘要（影片層級命中、事件層級精確率／召回率、未命中影片的目標規則阻擋原因統計）。有了全部影片的 JSONL 與遮罩，規則層的修改可以離線重播驗證，不需重跑模型。

### 離線重播工具與鏡頭運動穩定性
- `tools/real_replay.py`：以 JSONL（模型輸出）＋逐幀遮罩＋原影片重跑完整後處理與規則，不需要 torch。鏡頭運動預設採用 JSONL 記錄值（`--recompute-motion` 才重新估計）。以 stage3f 三支影片驗證：事件與規則診斷和 Windows 原機輸出相同；剩餘差異只有浮點數末位（OpenCV float32 投影約 1e-5、numpy 矩陣運算約 1e-13）與換行字元。
- **發現（精度問題，列入階段 4）**：在 VM 重新估計 ms01 的鏡頭運動，與 Windows 結果的投影差中位數 11.8 px、最大 88 px；兩邊的每幀縮放都在 0.92–1.09 間跳動（停在紅燈前，真實運動應接近不動）。影格順序已確認對齊，不是解碼問題。估計結果被雜訊主導卻仍通過現有品質閘門；雙線、停止線追蹤、區域都依賴它。

## 階段 4：精度（只改規則與後處理，不重新訓練）
原則：放寬的條件只產生 suspected；confirmed 的條件不變。評估以全片評估的 JSONL 離線重算（`tools/rule_eval.py`，約 50 秒；baseline 重算與 Windows 輸出 0 差異）。`eval/metrics.py` 的配對除時間外，再檢查車牌（編輯距離 ≤ 標註車牌長度一半）與車種（兩輪／四輪不衝突）。

### 4a：闖紅燈
根因（baseline 4/13）：燈號模型對清楚可見的燈號信心偏低（中位數 0.15–0.25）；s01、s02 沒有停止線（有斑馬線）；停止線補償因鏡頭運動失敗而很少累積到 3 次觀測。
- `signal_state.CombinedSignalState`：強證據（原燈號追蹤，不變）＋弱證據（信心 0.12 起、1 秒窗口、平均信心 ≥ 0.15）＋紅燈保持（強證據只因觀測不足而失效、且畫面無綠／黃燈時，沿用紅燈最多 3 秒）。
- 沒有停止線時以斑馬線近端邊緣作代理停止線；快取的車輛偵測不再參與判定。
- 新增條件 `strong_signal_evidence`、`stop_line_observed`、`same_stop_line`，任一不成立即為 suspected。
- 結果（_rules_v1）：影片層級 4/13 → 7/13；事件層級 TP 4 FP 2 FN 3 → TP 5 FP 2 FN 2（精確率 71%、召回率 71%）；confirmed 事件不變。

### 4b：跨越雙黃線
- 規則：鏡頭運動短暫失敗（≤ 1 秒）以「不動」近似並保留候選；雙線遠端被車身遮住時沿端點外插；垂足越過端點時改用同列量測（透視下斜線常見）；雙線追蹤換編號時依位置轉移候選。新增條件 `camera_motion_compensated`、`line_observed_at_footprint`、`same_line_track`，放寬的情況只能是 suspected。
- 後處理（`attach_double_lines`）：鏡頭運動失敗時不重設追蹤，以未補償位置（2.5% 畫面寬）配對；運動「有效」但配不上時也嘗試同樣的後備，標記 `tracking_status: matched_without_motion`。以 `tools/real_replay.py` 重播：雙線追蹤編號數 cdy05 241 → 16、cdy04 → 12、cdy06 231 → 153。
- 結果：cdy 仍為 1/7，其他類別事件不變。逐片檢查影片後，剩下的漏報主要是觀測不到跨越過程，不是規則門檻：
  - cdy02、cdy04：跨越發生在遠處消失點附近，車框只有 12–25 px，線帶與車框無法分辨；之後車輛已在另一側。
  - cdy05：廂型車從路邊出來時首次觀測就已壓在雙線上，起始側從未被觀測到；同時車身占滿畫面使鏡頭運動失敗約 2 秒。
  - cdy03、cdy06：彎道上的雙線分割不穩定（unstable_line／short_line），跨越當下沒有可用的雙線。
  - cdy07：AV1 編碼，VM 無法解碼，未逐片檢查。
  試過「首次觀測即在線上、之後穩定離開」的疑似判定：遠處車輛多半落在邊界容許帶內，無法與對向車道貼線行駛的車分開，會產生誤報，已撤回。
- 下一步需要感知層：雙線分割在彎道的穩定性、遠處小車的接地位置、以及鏡頭運動估計本身（每幀縮放抖動 2–6%）。

### 4a／4b 期間的規則實驗（`outputs/eval/_rules_*`、`_replay_v3`）
這些是 2026-10-21 17:24–17:49 以 `tools/rule_eval.py`／`tools/real_replay.py` 跑的中間版本。當時沒有保存每個版本的程式或參數，以下是事後從輸出（摘要、事件、阻擋原因）比對整理；標「推斷」的項目無法從輸出確認。

| 標籤 | 來源 | 內容 | 結果 | 處置 |
| --- | --- | --- | --- | --- |
| `_rules_base` | baseline JSONL | 以當時規則重算 | 與 baseline 相同 | 確認重算工具 0 差異 |
| `_rules_v1` | baseline | 4a 闖紅燈 | 紅燈 4/13 → 7/13，TP 5 FP 2 FN 2 | 採用 |
| `_rules_v2` | baseline | 闖紅燈變體。mm01、s01、s05 的阻擋原因出現 `proxy_stop_line`，推斷是把代理停止線當成硬性阻擋，而不是降為 suspected | 紅燈 7/13 → 4/13，TP 3 FN 4；s01、s05、mm01 漏報 | 撤回，維持 v1 的「代理停止線只能 suspected」 |
| `_rules_v3` | baseline | v1 紅燈 ＋ 4b 雙線規則（仍是舊的雙線後處理） | 紅燈同 v1；雙線 1/7。cdy02、cdy04 阻擋原因轉為 `phase:armed`（候選保留得較久） | 規則部分採用 |
| `_replay_v3` | 逐幀遮罩重播 | 4b 雙線後處理（`attach_double_lines` 不重設追蹤）重跑 cdy01–06（cdy07 在 VM 無法解碼） | 雙線 1/6，事件不變 | 作為 v3b／v3c 的來源 |
| `_rules_v3b` | `_replay_v3` | 雙線規則變體。cdy05 的 `motion_unavailable` 102 次（v3c 為 223），推斷是運動失敗的容忍時間較長 | 事件不變 | 未採用 |
| `_rules_v3c` | `_replay_v3` | 雙線規則，阻擋統計與最終 stage4 完全相同 | 事件不變 | 採用（即 stage4 設定） |
| `_rules_v3c_40`／`_60`／`_80` | `_replay_v3` | 某一參數取 40／60／80 的掃描，參數名稱未保存；80 與 v3c 相同，40／60 在 cdy02、cdy04、cdy05 多出 `phase:armed`／`seeding` 次數 | 事件不變 | 未採用 |

結論：v3 系列的所有雙線變體輸出的事件完全相同（只有 cdy01 5.4 秒的疑似），與 4b 的判斷一致，瓶頸在感知層而不是規則門檻。
改進：`tools/rule_eval.py` 改為在輸出資料夾寫入 `rule_options.json`（實際使用的規則參數、來源資料夾、git commit 與是否有未提交修改），之後的實驗都可以追溯。

### 4c：鏡頭運動的靜止判定與抖動閘門
量測（Windows，以全片評估 31 支影片的原影片＋記錄車框重新估計，9,411 幀）：
- 「ms01 停在紅燈前」的前提不成立：ms01 背景光流中位數 4.9 px，鏡頭是在移動的；Windows 上 ms01 每幀縮放偏差最大 4.3%，沒有重現階段 3 VM 上的 0.92–1.09 跳動。VM 與 Windows 的差異應來自解碼／OpenCV，仍未解釋。
- 真正靜止的幀（背景光流中位數 < 0.5 px）很多（ms03、ms05、s02、ctm05 幾乎整片）。這些幀原本仍擬合完整單應矩陣，道路中心被推移中位數 0.05 px、p99 1.9 px、最大 5 px（未要求空間分佈時最大 8.4 px）。
- 移動幀中，道路中心位移與背景光流 p90 的比值 p99 為 1.09，單應矩陣與實際光流一致。

修改（`road_compensation/motion.py`）：
- 靜止判定：可靠光流的中位數 ≤ 0.5 px、至少 `min_inlier_ratio` 的點 ≤ `stationary_flow_px`（1.0 px，原圖像素），且這些點通過與一般估計相同的空間分佈檢查時，直接回傳單位矩陣（`reason: stationary`）。仍做亮度檢查。空間分佈檢查是必要的：沒有它時，行車中只有消失點附近有特徵的幀（cdy05 等）會被誤判為靜止，有效率從 0.35 跳到 0.76。
- 抖動閘門：道路區域中心經單應矩陣的位移超過 `max_flow_extrapolation`（2.0）× 光流 p90（至少 `stationary_flow_px`）時拒絕（`reason: inconsistent_with_flow`）。全片只觸發 1 幀，作為安全網。
- 新增測試：帶感測雜訊的靜止畫面回傳精確單位矩陣；1.5 px 的真實平移不被判為靜止；特徵只在單一角落時不判為靜止。

結果（`outputs/eval/_motion_gate2`，以 `tools/real_replay.py --recompute-motion` 重播）：3,117 幀判為靜止、1 幀被抖動閘門拒絕，各片有效率與 stage4 相同（±0.01）。全部影片的事件、影片層級與事件層級指標與 stage4 完全相同；阻擋原因只有 cdy03、ctm01、ctm03、ctm05 的次數有小幅變動。
結論：在目前這批影片上，鏡頭運動雜訊不是雙線／區域漏報的主因，靜止幀的雜訊本來就很小。這次修改保證靜止時補償結果精確不動、移除少數數 px 的推移，但不會改變偵測結果。雙線漏報仍以 4b 列出的感知層問題為主。

### 4d：機車停等區規則改用畫面燈號
根因：ctm 六支都是行車紀錄器，`configs/area_rules.json` 沒有車道標定，所以每一組「車 × 區域」都卡在逐車道紅燈（`associated_red_signal_unavailable`）。即使換掉燈號來源，下一步也會卡在 `front_direction_unavailable`：車頭方向只來自標定車道的 heading。
修改（`area_rules.py`，原則同 4a：放寬只產生 suspected，這條規則本來就全部是 suspected）：
- 燈號：每幀以 `CombinedSignalState` 追蹤整個畫面的燈號（與闖紅燈相同的強／弱／延續）。車道能配對時沿用原本的逐車道判定；車道已配對但該車道不是紅燈時不退回。配對不到車道才使用畫面燈號。新阻擋原因 `lane_unmatched_no_red_signal`、`lane_unmatched_signal_not_red`。
- 車頭方向：配對不到車道時，取接地梯形的遠端邊作為車頭（行車紀錄器看到的多半是車尾）。貼著畫面左右邊緣 `side_margin_ratio`（2%）的車不使用此假設；ctm06、ms01 都有側面並行車因此誤報。
- 去重：同一台車在 `rearm_seconds`（2 秒）內持續侵入只報告一次。區域追蹤換號、量測單幀低於門檻都曾讓同一台車重複報告，ctm02 的一台車原本有 5 次。
- 事件 conditions 新增 `lane_matched`、`strong_signal_evidence`、`front_edge_from_lane`，evidence 新增 `signal`。
- 測試：無標定時使用畫面燈號（強／弱／綠燈／無燈號）、畫面邊緣的車、已配對車道不被畫面燈號覆蓋、換號與閃爍時只報告一次。

結果（`outputs/eval/_waiting_v2`，`tools/rule_eval.py` 以 stage4 重算）：
- 影片層級停等區 0/6 → 3/6（ctm02、ctm05、ctm06）。其他規則的事件與指標完全不變。
- 逐張檢查事件畫面：ctm02 8.4 秒（黑色休旅車停在停等區，紅燈）與 ctm06 7.8 秒（紅色轎車由左側駛入停等區）看起來是真實違規；ctm05 12.1 秒（紅色轎車）可能是；ctm05 1.5 秒是遠處很小的車框，存疑。ctm 影片還沒有事件標註，無法計算精確率。
- ms01 多一個事件（2.0 秒，闖紅燈的貨車行經停等區），在事件層級算成 1 個誤報。已決定併入闖紅燈事件（見下）。
- ctm01、ctm03、ctm04 仍未命中，主要阻擋是 `region_geometry_unavailable` 與 `low_confidence`（區域或車輛信心 < 0.5），屬於區域分割與信心門檻，不在本次範圍。

停等區事件併入闖紅燈事件（`ViolationEngine`）：
- 規範：闖紅燈的車必然行經停等區，同一台車的停等區事件不單獨報告，整筆放進闖紅燈事件的 `merged_events`。
- 停等區事件可能早於闖紅燈事件（ms01：2.0 秒 vs 3.4 秒），所以先保留 `merge_window_seconds`（3 秒）再輸出。期間同一台車出現闖紅燈事件就併入；沒有就照常輸出，並加上 `released_timestamp_sec`（標註影片依此時間顯示）。闖紅燈事件在前時，窗口內的停等區事件直接略過，記在停等區規則當幀診斷的 `merged_into`。未啟用闖紅燈規則時不保留、不合併。
- 結果（`outputs/eval/_merge_v1`）：ms01 的停等區誤報消失，併入 3.4 秒的確認闖紅燈事件。ctm 仍為 3/6，其他規則不變。ctm06 同一台車的闖紅燈事件晚 5.5 秒，超過窗口，兩者都保留。
- 測試 `tests/test_event_merge.py`：停等區在前／同幀／闖紅燈在前、不同車、超過窗口、影片結束時清空、未啟用闖紅燈規則。

### 4e：更換紅綠燈模型
- 新模型：`models/traffic_light/traffic_light_best.pt` 改為 YOLO26s（2026-10-13 訓練，SHA256 `3ff0b2e2…62cb`，20.5 MB），類別 red／green／yellow／arrow，訓練尺寸 1280，因此三個模型設定檔加上 `imgsz: 1280`。舊模型（YOLO11n、3 類、800，SHA256 `57b19ed8…c760`）已被覆蓋。
- 箭頭燈：`arrow` 保留在觀測中，但不參與紅／綠／黃判定（`signal_state.SIGNAL_COLORS`、`signal_lights()`；停等區的逐車道燈號同樣過濾）。原本的追蹤器會把箭頭當成另一種顏色，紅燈旁的箭頭燈會造成「顏色衝突」而判成未知。新增 `tests/test_signal_state.py`。
- 評估方式：以新舊模型逐幀重跑 31 支評估影片的燈號，替換 stage4 JSONL 的 `traffic_lights` 後以 `tools/rule_eval.py` 重算（`outputs/eval/_light_old_rules`、`_light_new_rules`）。舊模型的對照組與 stage4 記錄 9,411 幀逐幀相同。
- 結果：
  - 紅燈信心明顯提高：中位數 0.23 → 0.35，≥ 0.30（強證據門檻）的比例 31% → 56%。
  - 闖紅燈事件層級 TP 5 FP 2 FN 2 → TP 5 FP 1 FN 2（精確率 71% → 83%）；影片層級 7/13 → 6/13（s05 未中，該片尚未標註事件）。
  - ms04 由疑似變為確認；ms03 失去確認事件（只剩疑似）。soc02 的疑似闖紅燈誤報消失。停等區 3/6 不變。
- 已知問題：黃燈偵測 198 → 2,558 次。soc02 多為真正的黃燈（夜間黃閃），但也有誤判：ms02 信心 0.69 的「黃燈」是橘色招牌。ms03 失去確認事件的原因見 4f（原先寫成「紅／黃跳動」是錯的）。
- 速度：RTX 5060 Laptop 上每幀 20.7 → 26.8 ms，相對整體每幀約 400 ms 影響不大。

### 4f：黃燈需持續才中斷紅燈
- 量測（`_light_new`，以實際追蹤器的配對統計同一燈的顏色序列）：同一燈「紅→黃→紅」的黃燈段多為 1–5 幀（ctm01、ctm04、ctm06、soc01、soc05、s04），真正的黃燈段為數十幀（ctm02 綠→黃→紅 88 幀）。
- 修改（`signal_state.py`）：
  - `TemporalSignalState(yellow_confirm_seconds=…)`：同一燈由紅轉黃時，黃燈先暫存，不寫入歷史、不觸發轉燈；紅燈證據照常延續。連續觀測達門檻才切斷紅燈記憶，暫存的黃燈成為歷史並觸發 `observed_signal_change`。中間出現紅燈即清除暫存。預設 0 為原行為。
  - `CombinedSignalState(yellow_confirm_seconds=0.2)`：強、弱追蹤器都使用 0.2 秒（30 fps 為 6 幀）。紅燈延續也一樣：連續 0.2 秒都有黃燈才取消延續（綠燈仍立即取消）。
  - 測試：短暫黃燈維持紅燈、持續黃燈中斷紅燈且歷史只剩黃燈、預設值維持立即中斷、延續不被單幀黃燈取消。
- 結果（`outputs/eval/_yellow_v1`，以新燈號模型的 `_light_new` 重算）：9,411 幀中 276 幀（2.9%）的燈號狀態改變。主要是短暫黃燈不再中斷紅燈（黃／未知 → 紅），以及紅燈延續不被取消。副作用是紅燈延續較久，與真正的綠燈同時存在時判為未知（綠 → 未知 75 幀）。全部影片的事件與指標都沒有改變。
- **ms03 沒有被這次修改影響（0 幀改變）**：它的問題不是同一燈的紅黃跳動。路口遠端一個很小的號誌（畫面 822, 785）在整段影片中穩定被讀成黃燈（信心 0.32–0.41），與近處的紅燈各自成為穩定的追蹤，追蹤器判定 `conflicting_signal_tracks`（未知），所以沒有強證據的紅燈。要救回 ms03 的確認事件，需要處理「多個燈號顏色衝突」的判定，例如依信心或燈號大小決定主燈號，或排除遠處的小燈號。

### 4g：穩定燈號顏色衝突時採用主燈
- 修改（`signal_state.py`）：多個穩定燈顏色衝突時，若某燈對所有異色穩定燈的平均信心 ≥ 1.5 倍，或框邊長 ≥ 2 倍，就採用它的顏色（`reason: dominant_signal_track`，evidence 記錄被覆蓋的顏色 `overridden`）。沒有明顯主燈時仍為 `conflicting_signal_tracks`。
- 強證據追蹤器以主燈判定時，strength 標為 `dominant`：屬於較弱的證據，只支持「疑似」，不能讓事件成為「確認」（確認仍只接受 `strong`）。弱證據追蹤器以主燈判定時維持 `weak`。
- 測試：信心佔優、框大小佔優、沒有明顯主燈維持未知、停用時維持原行為。
- 結果（`outputs/eval/_dominant_v1`，新燈號模型）：
  - 9,411 幀中原本 1,506 幀為顏色衝突，其中 1,353 幀改由主燈判定（`dominant` 1,098、`weak` 255），剩 153 幀仍為未知。
  - ms03 的目標事件（vehicle-000029，1.935 秒）仍為「疑似」：主燈判定被刻意設為較弱證據，不能確認。另外恢復了 stage4 曾有的 vehicle-000073 疑似事件（同在 1.935 秒），事件層級把它算成誤報。
  - 闖紅燈事件層級 TP 5 FP 1 → TP 5 FP 2（精確率 83% → 71%）；影片層級 6/13 不變；其他規則不變（ctm02 停等區事件 8.8 → 8.4 秒）。
  - 注意：主燈判定也常讓綠燈蓋過紅燈（cdy01 133 幀、soc01 120 幀為「綠燈主導、覆蓋紅燈」）。
- 結論：在目前的標註下，這次修改沒有改善指標。若要讓 ms03 回到「確認」，需要允許主燈判定支持確認（至少在只覆蓋黃燈時），但這會放寬確認條件。
