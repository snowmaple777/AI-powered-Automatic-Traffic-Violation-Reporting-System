# Traffic AI

交通影片辨識與違規候選分析，整合車輛追蹤、單目車距、車牌定位、台灣微調
PP-OCRv6、紅綠燈與道路標線。文件更新：2026-10-28。

## 開始使用

在此目錄的 PowerShell 執行：

~~~powershell
.\安裝環境.cmd
.\啟動辨識.cmd "input\影片.mp4" --save-video
~~~

default 與 full 均啟用完整六階段；違規判定需另外加上
--rules red_light_stop_line_crossing。

模型權重（`.pt`／`.pth`／`.onnx`）不放在 git 中，需另外放到 `models/` 下對應位置；
檔名、大小與 SHA-256 見 [模型清單.txt](模型清單.txt)，載入時會檢查。目前使用：

| 階段 | 模型 |
| --- | --- |
| 車輛 | YOLO26s（ONNX）＋ ByteTrack |
| 紅綠燈 | YOLO26s 紅／綠／黃／箭頭，推論尺寸 1280（箭頭燈不參與紅綠燈判定） |
| 道路標線 | SegFormer B2 best56000，滑動視窗 1024/683、fp16，每 2 幀推論一次（中間幀依鏡頭運動推移） |
| 車距／車牌／車牌文字 | Depth Anything V2 Small、車牌 YOLO（ONNX）、台灣微調 PP-OCRv6 |

啟用違規規則後，辨識結束自動剪輯每個事件前後各 5 秒，保存在 outputs/<影片名>_violation_clips/。
疑似違規以橘框標示，規則確認違規以紅框標示，附上車輛及違規資訊。
可用 --clip-before／--clip-after 調整秒數；片段目前不含音訊。

雙黃線／雙白線支援完整跨越判定，並由相機運動補償後的軌跡推定變換車道或迴轉。
--rules all 會啟用雙線規則；方向證據不足時保留為完整跨越。所有候選仍需人工覆核。
調整 configs/double_line_rules.json 可微調穩定時間、誤差範圍與行為角度，
影片辨識及 JSONL 重算均支援 --double-line-config，詳見使用說明。

- [完整使用說明](docs/usage.md)：安裝、辨識、裝置、輸出與常見問題。
- [純文字使用說明](使用說明.txt)：供記事本閱讀，與操作文件同步。

## 使用版目錄

| 位置 | 用途 |
| --- | --- |
| 根目錄的啟動／安裝 cmd | 安裝、辨識、選用填表入口 |
| run_pipeline.py | 影片辨識主流程（模型推論與輸出串接） |
| frame_processing.py、pipeline_outputs.py | 逐幀後處理（模型輸出 → 觀測紀錄）、CSV 與標註畫面 |
| observation_schema.py | 觀測紀錄（JSONL）的欄位契約與驗證 |
| evaluate_violations.py、compensate_recording.py | 從既有輸出重算規則或停止線補償 |
| configs/ | 模型設定、ByteTrack 設定、標線補償、雙線與區域規則參數 |
| model_library/、road_compensation/、core/ | 辨識模型、鏡頭運動與停止線補償、共用幾何工具 |
| violation_engine.py、signal_state.py | 違規規則（闖紅燈、雙線）、燈號時間證據、事件合併 |
| double_lines.py | 雙線有限幾何、運動補償追蹤與影片標記 |
| area_rules.py | 停等區／斑馬線區域建立、道路追蹤、車輛接地近似、車道號誌配對與候選規則 |
| eval/ | 人工標註答案（ground_truth.json）與指標計算 |
| tools/ | 全片評估、離線重播、規則重算、效能量測與驗證工具（說明見 docs/refactor_log.md） |
| tests/ | 單元與回歸測試（`python -m unittest discover -s tests`） |
| third_party/ | 道路模型所需的 MMSegmentation 程式及授權 |
| models/ | 現行權重、OCR 字典與模型檔案資訊 |
| input/、outputs/ | 輸入影片與輸出結果 |
| reporting/autoForm/ | 填表 GUI、縣市驅動、案件與個人設定 |
| docs/usage.md、使用說明.txt | 操作說明的 Markdown 與純文字版本 |
| .venv/ | 現有執行環境 |

歷史文件及舊辨識後端已移除；現行測試與效能量測工具保留供維護。
車輛、燈號與標線偵測器位於 model_library/detectors.py（model_library/legacy.py 只保留舊匯入路徑的相容轉出口）。
版本控制資料 .git 保留，不影響一般使用。

預設 OCR 使用 CPU Paddle；ONNX 沒有 CUDA provider 時改用 CPU，詳見 docs/gpu_setup.md。

目前狀態（RTX 5060 Laptop）：1080p 約 1.6 FPS、720p 約 2.8 FPS。31 支評估影片中只有 6 支有事件標註，
準確度數字、各版本的修改與評估結果見 [docs/refactor_log.md](docs/refactor_log.md)。
