# Traffic AI

交通影片辨識與違規候選分析，整合車輛追蹤、單目車距、車牌定位、台灣微調
PP-OCRv6、紅綠燈與道路標線。文件更新：2026-10-11。

## 開始使用

在此目錄的 PowerShell 執行：

~~~powershell
.\安裝環境.cmd
.\啟動辨識.cmd "input\影片.mp4" --save-video
~~~

default 與 full 均啟用完整六階段；違規判定需另外加上
--rules red_light_stop_line_crossing。模型與字典皆在專案內，不依賴旁邊的 perception 目錄。

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
| run_pipeline.py | 影片辨識主流程 |
| evaluate_violations.py、compensate_recording.py | 從既有輸出重算規則或停止線補償 |
| configs/ | 模型設定、ByteTrack 設定、標線補償參數 |
| model_library/、road_compensation/ | 執行所需的辨識與後處理程式 |
| double_lines.py | 雙線有限幾何、運動補償追蹤與影片標記 |
| tests/ | 車牌持續顯示、事件剪輯與雙線框架的回歸測試 |
| third_party/ | 道路模型所需的 MMSegmentation 程式及授權 |
| models/ | 現行權重、OCR 字典與模型檔案資訊 |
| input/、outputs/ | 輸入影片與輸出結果 |
| reporting/autoForm/ | 填表 GUI、縣市驅動、案件與個人設定 |
| docs/usage.md、使用說明.txt | 操作說明的 Markdown 與純文字版本 |
| .venv/ | 現有執行環境 |

歷史文件及舊辨識後端已移除；現行測試與效能量測工具保留供維護。
model_library/legacy.py 雖保留原檔名，仍包含現行車輛、燈號與標線所需的底層實作。
版本控制資料 .git 保留，不影響一般使用。

預設 OCR 使用 CPU Paddle；ONNX 沒有 CUDA provider 時改用 CPU。
目前完成短片段流程驗證與三個 ONNX 模型的 CUDA 執行檢查，詳見 docs/gpu_setup.md。
尚未完成整段影片準確率、GPU 穩態吞吐量與 OCR GPU 評測。
