# 推論服務（inference_service）

把組員的 `run_pipeline.py`（車輛、車牌、號誌、標線模型＋違規規則）和 `reporting/autoForm`（自動填表）包成一個 HTTP 服務，
讓網頁 UI 透過 API 呼叫。**組員的程式一行都沒有改。**

```
瀏覽器 ──> UI 網站（ui/server.py, :8765） ──HTTP API──> 推論服務（本資料夾, :8770） ──> run_pipeline.py
```

現在兩個都跑在你的電腦上；之後搬上雲端，只要把推論服務放到雲端 GPU 主機，
再把 UI 設定裡的網址改成那台主機即可（見最下方）。

## 啟動

先完成 `下載模型.cmd` 和 `安裝環境.cmd`，然後：

1. 雙擊專題資料夾裡的 **`啟動推論服務.cmd`**（這個視窗要一直開著）
2. 雙擊 **`ui\啟動介面.cmd`**，在網頁「分析方式」選 **真實模型**

啟動畫面會列出模型檔是否齊全、有沒有 ffmpeg。GPU 狀態可以在 UI 的分析方式卡片看到。

第一次啟動時，如果找不到 ffmpeg，`啟動推論服務.cmd` 會在 `.venv` 裡安裝 `imageio-ffmpeg`（約 30 MB），
用來把違規片段轉成瀏覽器能播放的 H.264。不想裝也可以，UI 會改播原影片的那一段。

## 它做了什麼

收到影片後：

1. 在背景執行
   `.venv\Scripts\python.exe run_pipeline.py <影片> --output-dir <工作資料夾>\outputs --rules all`
2. 讀它印出的「已處理 N 幀…FPS」算進度、剩餘時間
3. 一邊讀它逐幀寫出的 `*_detections.jsonl`，把偵測框畫在畫面上當作**即時預覽**，並統計追蹤目標數、車牌數
4. 一邊讀 `*_violations.jsonl`，有違規就即時回報
5. 跑完後整理結果：違規片段轉 H.264、擷取違規當下截圖、整理成 `result.json`

一次只跑一支影片（GPU 只有一張），其他的排隊。保留最近 20 個工作，更舊的自動刪除。
檔案都在 `inference_service\data\jobs\<工作編號>\`（已列入 .gitignore）。

## API

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/health` | 服務狀態、模型檔是否齊全、GPU、ffmpeg、是否忙碌 |
| GET | `/jobs` | 工作列表 |
| POST | `/jobs?filename=&rules=all&max_seconds=&clip_before=5&clip_after=5&device=auto` | body 直接放影片內容，回傳工作 |
| GET | `/jobs/<id>` | 進度：`state`、`stage`、`frames_done/frames_total`、`fps`、`eta_sec`、`stats`、`found`（已發現違規）、`log` |
| GET | `/jobs/<id>/preview.jpg` | 即時畫面（畫上模型偵測框） |
| GET | `/jobs/<id>/result` | 最終結果：違規事件（規則、確認／疑似、車牌、判定條件、片段與截圖檔名） |
| GET | `/jobs/<id>/files/<檔名>` | 片段 mp4、截圖 jpg |
| POST | `/jobs/<id>/cancel` | 取消（會連 run_pipeline.py 一起結束） |
| DELETE | `/jobs/<id>` | 刪除工作與檔案 |

`state`：`queued` → `running` → `done`／`error`／`cancelled`／`interrupted`
`stage`：`queued` → `loading`（載入模型）→ `analyzing`（逐幀辨識）→ `clipping`（剪片）→ `packaging`（整理）

## 自動填表（autoForm）

推論服務也負責執行組員的 `reporting/autoForm`：UI 第 5 步按「開啟瀏覽器自動填表」後，
推論服務會在**這台電腦**開一個看得到的瀏覽器，把案件填進該縣市警察局的檢舉網站。

- **測試模式（預設）**：填好表單、上傳佐證，但不做送出前的驗證、**不按送出鈕**，停在送出前讓你檢查。
  做法是 `autoform_runner.py` 在執行時把各縣市 driver 的 `submit()` 換掉，組員的程式不用改。
- **正式送出**：要先在 UI 勾選確認，而且檢舉人資料（姓名、身分證、電話）要填好。
- 驗證碼／Email 認證要你在跳出的瀏覽器裡自己完成，autoForm 偵測到後會自動繼續。
- 隨時可以按「取消」：會關閉瀏覽器，還沒按送出的話什麼都不會送出去。
- 一次只能開一個填表視窗。檢舉人資料（含身分證）只在填表期間暫存在 `data/reports/<編號>/`，結束就刪除。

第一次執行 `啟動推論服務.cmd` 時，會在 `.venv` 裝 playwright 並下載 Chromium（約 150 MB）。

| 方法 | 路徑 | 說明 |
|---|---|---|
| POST | `/reports?city=kaohsiung&test=1&name=` | body 放案件資料夾的 zip（`info.txt`、佐證檔，可附 `reporter_profile.json`） |
| GET | `/reports`、`/reports/<id>` | 狀態：`starting` → `filling` → `waiting_verification` → `submitting` → `done`／`error`／`cancelled`；`browser_open` 表示瀏覽器還開著 |
| POST | `/reports/<id>/continue` | 送一個 Enter 給 autoForm（「按 Enter 繼續」的驗證模式才需要） |
| POST | `/reports/<id>/cancel` | 取消並關閉瀏覽器 |

`/health` 的 `autoform` 欄位會說明能不能用（有沒有 playwright、瀏覽器）。

> 填表要開看得到的瀏覽器、要人輸入驗證碼，所以**只能在使用者自己的電腦上跑**。
> 之後辨識搬到雲端時，在自己電腦另外開一個推論服務負責填表，並在 `ui/config.json` 設 `autoform_url` 指向它（預設與 `inference_url` 相同）。

## 參數

```powershell
.venv\Scripts\python.exe -m inference_service.server --help
```

| 參數 | 預設 | 說明 |
|---|---|---|
| `--host` | `127.0.0.1` | 雲端部署時改成 `0.0.0.0` |
| `--port` | `8770` | |
| `--pipeline-root` | 專題資料夾 | `run_pipeline.py` 所在位置 |
| `--python` | `.venv` 的 Python | 執行 `run_pipeline.py` 用的 Python |
| `--autoform-python` | 同 `--python` | 執行 autoForm 的 Python |
| `--api-key` | 空（不檢查） | 設定後每個請求都要帶 `X-API-Key`，**放到雲端一定要設** |

其他沒列出的參數會原封不動傳給 `run_pipeline.py`，例如 `--model-config configs\full.json`。

## 之後搬上雲端

1. 在雲端 GPU 主機上 clone 專案、放好模型、裝好 `.venv`
2. 啟動：`python -m inference_service.server --host 0.0.0.0 --api-key <自己設一組密鑰>`
3. UI 這邊建立 `ui\config.json`（參考 `ui\config.example.json`）：
   ```json
   { "inference_url": "https://你的雲端主機:8770", "inference_api_key": "同一組密鑰" }
   ```

UI 和 API 都不用改。雲端版還需要考慮 HTTPS、上傳大檔、帳號權限等，到時再處理。
