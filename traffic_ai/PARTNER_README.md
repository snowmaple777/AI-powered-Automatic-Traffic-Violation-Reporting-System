# 合作夥伴成果測試包

請下載 GitHub Release 附件 **traffic_ai-partner-20260922.zip**；
GitHub 自動產生的 Source code ZIP 不包含這組權重。
附件包含三模型辨識、完整六階段與 ONNX 車輛設定使用的 7 個實際權重檔。
不需要另找權重，無需 Git LFS。仍需 Python 3.10、套件環境與自己的測試影片。

在解壓後的 traffic_ai 目錄開啟 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-perception.txt
.\.venv\Scripts\python.exe tools/verify_models.py
.\.venv\Scripts\python.exe run_pipeline.py "影片路徑.mp4" --rule all --save-video
```

也可以將影片拖到「啟動合作夥伴測試.cmd」，會檢查權重並開啟全部規則及影片輸出。
預設使用車輛、紅綠燈與道路標線模型。若需車牌、OCR、深度估算：

```powershell
.\.venv\Scripts\python.exe run_pipeline.py "影片路徑.mp4" --model-config configs/full.json --rule all --save-video
```

outputs 內會產生標註 MP4、辨識 JSONL/CSV、違規 JSONL 及規則診斷。
違規檔區分 suspected 與 confirmed；confirmed 代表設定規則成立，保留人工覆核標記。
未符合條件時違規檔可為空。疑似事件在正常結束時寫出，請勿強制關閉。

`models/weights_manifest.json` 記錄每個權重的 SHA-256 與大小。
`reference_environment.json` 記錄製作環境版本，供比對；它不是跨硬體通用的安裝鎖檔。
使用同一影片、權重、程式與設定可比較結果，但 CPU/GPU、套件版本可能造成差異。
此包不包含 Python、CUDA、個人資料或測試影片。
