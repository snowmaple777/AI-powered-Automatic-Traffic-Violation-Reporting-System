# 任務：把 autoForm（Python + Playwright 交通檢舉自動填表）移植成 Android App

你現在位於 `autoForm/` 專案根目錄。請先完整閱讀以下檔案再動手，理解現有流程：
- `main.py`（info.txt 解析、input_data 欄位對應、媒體檔搜尋、縣市→driver 路由）
- `app_gui.py`（GUI 流程、依地點自動推薦縣市）
- `drivers/base_driver.py`（run_workflow 範本流程、select_native / select_cascade / select_violation、PostBack 等待、未比對欄位紅框提示）
- `drivers/kaohsiung_driver.py`（第一個要移植的縣市）
- `plugins/human_verification_helper.py`（人工驗證等待邏輯）
- `utils/option_matcher.py`（下拉選單模糊比對、行政區比對、條號處理）＋ `tests/test_option_matcher.py`
- `configs/*.json`（各縣市選擇器、kaohsiung 的 violation_map、violation_catalog.json、option_synonyms.json）

## 目標架構（已決定，不要改成其他方案）
**Kotlin 原生 App + Android WebView + JavaScript 注入**。
不要用 Termux / Chaquopy / Playwright Android / 遠端伺服器——Playwright 無法在手機上獨立執行，個資也不能送到伺服器。

對應關係：
| Python 版 | Android 版 |
|---|---|
| Playwright Chromium | App 內 WebView |
| page.fill / check / select_option | `evaluateJavascript()` 注入 JS：設值後 dispatch `input`、`change`（必要時 `blur`）事件 |
| configs/*.json | **不要複製**；Gradle `sourceSets.main.assets.srcDirs` 加入 `../configs`（或建置前 copy task），維持單一來源 |
| drivers/*_driver.py | `assets/drivers/<city>.js`（每縣市一支）＋ Kotlin 端步驟狀態機 |
| base_driver.py 共用工具 | `assets/drivers/_common.js`（fill、check、selectNative、selectCascade、waitFor、highlight、readOptions…） |
| expect_navigation / ASP.NET PostBack | `WebViewClient.onPageFinished` + 網址/DOM 判斷目前步驟，再注入下一步 |
| set_input_files 上傳 | 攔截 `WebChromeClient.onShowFileChooser`，自動回傳該案件下一個待上傳檔案的 content Uri（JS 端 click file input 觸發） |
| HumanVerificationHelperPlugin | 使用者直接在 WebView 中輸入驗證碼；App 顯示提示橫幅，偵測網址變化/DOM 狀態/使用者按「我已完成驗證」後繼續 |
| option_matcher.py | 移植成 Kotlin（`OptionMatcher.kt`），並把 `tests/test_option_matcher.py` 的案例改寫成 JUnit 測試，全部要通過 |
| reporter_profile.json | App 設定頁，存 EncryptedSharedPreferences（身分證字號等個資） |
| info.txt 案件資料夾 | 兩種輸入：(a) 用 SAF 選資料夾讀 info.txt + jpg/png/mp4；(b) App 內表單手動填寫 + 從相簿挑照片/影片 |

## 專案規格
- 位置：`autoForm/android/`（Android Studio 專案，Gradle Kotlin DSL）
- Kotlin、minSdk 26、targetSdk 最新穩定版、UI 用 Jetpack Compose（WebView 用 AndroidView 包）
- 套件名：`tw.nuk.autoform`
- 不需要後端、不需要網路權限以外的敏感權限

### 畫面
1. **個資設定**：姓名、身分證字號、電話、Email、通訊地址（對應 main.py 的 reporter_* 欄位）
2. **案件**：載入資料夾或手動填寫；欄位同 info.txt（違規日期、違規時間、違規行政區、交通違規地點、車牌號碼、違規車種類型、違規類別、違規事實、違規事實敘述）；顯示媒體縮圖；違規事實提供下拉（來源：kaohsiung.json violation_map + violation_catalog.json 的名稱）
3. **縣市選擇**：依地點/行政區自動推薦（照 app_gui.py 邏輯），可手動改
4. **填表頁**：全螢幕 WebView + 上方狀態列（目前步驟、log、未比對欄位提示、「我已完成驗證」、「重新注入本步驟」按鈕）

### WebView 必要設定
- `javaScriptEnabled`、`domStorageEnabled`、允許 file chooser
- **User-Agent 改成桌面版 Chrome**（否則部分網站給手機版排版，config 選擇器會失效）；`useWideViewPort` + `loadWithOverviewMode` 讓桌面版頁面可縮放
- 花蓮 config 的 start_url 是 `http://`：要用 network_security_config 只對該網域允許 cleartext
- 部分 config（如 taipei.json）沒有 start_url：改用 `檢舉網站.txt` 的網址
- 用 `addJavascriptInterface` 提供 `AutoForm.log()`、`AutoForm.stepDone(name, json)`、`AutoForm.reportUnmatched(json)`、`AutoForm.requestFile()` 給 JS 回報
- 每次 onPageFinished 重新注入 `_common.js` + 縣市 js（頁面重載後全域變數會消失），狀態只存 Kotlin 端
- SPA 網站（台北、台中網址含 `#/`）不一定觸發 onPageFinished：用 JS `MutationObserver`/輪詢 waitFor(selector) 判斷

### 縣市 driver 介面（JS）
每支 `<city>.js` 匯出：
```js
window.AutoFormDriver = {
  detectStep(url, document) -> "start" | "fill" | "verify" | "done" | ...,
  steps: { start(ctx){...}, fill(ctx){...}, upload(ctx){...}, submit(ctx){...} }
}
```
`ctx` 由 Kotlin 注入：input_data（同 main.py 的鍵名）、config JSON 內容、媒體檔數量、目前上傳到第幾個。
流程順序與 `BaseDriver.run_workflow` 相同，包含 `verification_timing`（before_fill / before_submit / pre_verification）。

## 執行順序（請照順序，每階段都要能編譯執行再進下一步）
1. 建 Android 專案骨架、assets 連結 configs、三個畫面、個資加密儲存、info.txt 解析（照 main.py parse_info_txt）
2. 移植 OptionMatcher.kt + JUnit 測試全綠
3. WebView 填表頁 + `_common.js` + JS bridge
4. **只做高雄市**（`kaohsiung.js`），完整跑通：Statement.aspx 勾 4 個同意 → 我要檢舉 → Mail.aspx 填個資（性別依身分證第二碼）→ 兩個行政區下拉 → 日期時間＋滑桿 → 車牌拆兩欄 → 車種 → violation_map 大類/小類連動（大類是 AutoPostBack，要等小類選項更新）→ 地點、敘述 → 逐檔上傳（每次上傳都會 PostBack，最多 3 個）→ 標紅未比對欄位 → 等使用者輸入驗證碼 → 送出 → 判斷成功（離開 Mail.aspx 且非 DisplayCaptcha）
   - 注意：每次 PostBack 頁面重載後 JS 狀態與紅框會消失（伺服器會回填已送出的欄位值），進度要存在 Kotlin 端，紅框要重新標
5. 高雄穩定後，依同模式逐一移植其他 19 個縣市（每個縣市對照其 `drivers/*_driver.py` 和 config）
6. 金門、連江只能寄 Email：提供用 `Intent.ACTION_SEND` 帶入內容與附件的備案

## 規則
- **絕對不要自動按最後送出前跳過人工驗證**；驗證碼、Email OTP 一律由使用者手動完成
- 不要自動繞過 CAPTCHA
- 測試時可以填表到驗證步驟為止，**不要真的送出檢舉**，除非我明確說要
- 個資不得寫入 log 檔或上傳任何地方
- 每完成一個階段，告訴我怎麼在手機上測、哪裡可能還不穩

完成第 1～4 階段後先停下來回報，等我實機測完高雄再繼續第 5 階段。
