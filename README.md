# AI-powered-Automatic-Traffic-Violation-Reporting-System

## 整合版交通影像辨識（2026-09-22）

**合作夥伴快速測試：** [下載含全部模型權重的完整測試包](https://github.com/snowmaple777/AI-powered-Automatic-Traffic-Violation-Reporting-System/releases/tag/traffic-ai-partner-20260922)，
選擇附件 `traffic_ai-partner-20260922.zip`，依 [合作夥伴說明](traffic_ai/PARTNER_README.md) 啟動。
完整包包含 7 個模型權重與校驗清單，不需 Git LFS；仍需 Python 環境及自己的影片。

新增 [traffic_ai](traffic_ai/README.md)，整合車輛、號誌、道路標線與可替換的模型接口，
提供騎士／兩輪車組合追蹤及影片標註、停止線補償、疑似／規則確認的違規 JSONL。
`--rule all` 自動啟用所有已註冊規則。原有各模組仍保留。

```powershell
cd traffic_ai
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# 依 configs/default.json 準備模型權重後執行
.\.venv\Scripts\python.exe run_pipeline.py "影片路徑.mp4" --rule all --save-video
```

程式碼包不含模型權重、輸入影片與個人設定；部署及權重說明請見整合版 README。

身為土生土長的台灣人，我們對於台灣交通系統的混亂是有目共睹的。各類交通違規行為如闖紅燈、亂切車道......等每分每秒都在發生，層出不窮。然而，面對如此龐大的違規量能，我們不可能完全依賴有限的警力與國家資源將其一一繩之以法；但對現代人而言，更不可能花費自己寶貴的時間去操作繁瑣的網路檢舉流程。 因此，我們希望能開發出一款「馬路三寶檢舉輔助系統」，利用影像辨識與系統整合技術，實現從違規影像蒐集、車牌與行為偵測，到自動填報檢舉單的一條龍服務，用戶只需確認檢舉資訊是否正確，即可按按鈕送出，大幅降低民眾參與交通正義的門檻，用科技力量化被動為主動，共同打造更安全、流暢的交通環境。 
