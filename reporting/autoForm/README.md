# 全台交通違規檢舉自動化填表系統

![Python Version](https://img.shields.io/badge/python-3.8%2B-blue)
![Playwright](https://img.shields.io/badge/playwright-1.48%2B-green)

基於 **Python + Playwright + Tkinter** 開發的全台灣各縣市交通違規檢舉自動化填表系統。

---

## 🌟 核心特點

- 🏙️ **基本全縣市支援**：支援全台灣 **20 個縣市**的警察局交通違規檢舉網站。
---

## 🗺️ 支援縣市一覽表 (Supported Cities)

| 縣市 | 代碼 (`--city`) | 縣市 | 代碼 (`--city`) |
| :--- | :--- | :--- | :--- |
| **台北市** | `taipei` | **新北市** | `new_taipei` |
| **基隆市** | `keelung` | **桃園市** | `taoyuan` |
| **新竹市** | `hsinchu_city` | **新竹縣** | `hsinchu_county` |
| **苗栗縣** | `miaoli` | **台中市** | `taichung` |
| **彰化縣** | `changhua` | **南投縣** | `nantou` |
| **雲林縣** | `yunlin` | **嘉義市** | `chiayi_city` |
| **嘉義縣** | `chiayi` | **台南市** | `tainan` |
| **高雄市** | `kaohsiung` | **屏東縣** | `pingtung` |
| **宜蘭縣** | `yilan` | **花蓮縣** | `hualien` |
| **台東縣** | `taitung` | **澎湖縣** | `penghu` |

---

## 📦 環境安裝 (Installation)

### 1. 複製專案 (Clone Repository)
```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git
cd 自動填表單第二版
```

### 2. 安裝 Python 依賴套件與 Playwright 瀏覽器核心
```powershell
pip install playwright
python -m playwright install chromium
```

---

## 🚀 使用說明 (Usage)

### 方法一：啟動 GUI 圖形介面（推薦）
在終端機直接執行：
```powershell
python main.py
```
*或：*
```powershell
python app_gui.py
```

#### GUI 操作步驟：
1. 初次使用請點選 **「修改資料」** 建立您的檢舉人基本資料（姓名、身分證號、電話、信箱、通訊地址）。
2. 點選 **「瀏覽案件...」** 選擇包含違規資訊的案件資料夾。
3. 系統將會根據違規地點自動推薦目標申報縣市。
4. 點選 **「啟動自動填表」**，系統將開啓瀏覽器並自動填妥所有資料。
5. 於瀏覽器中完成人機驗證（輸入驗證碼或完成信箱認證）後，點擊提交。

---

### 方法二：命令列模式
適用於自動化腳本或伺服器環境：

```powershell
python main.py --city <縣市代碼> --case-dir <案件資料夾路徑>
```

#### 執行範例：
* **台北市**：`python main.py --city taipei --case-dir cases/case_taipei`
* **新北市**：`python main.py --city new_taipei --case-dir cases/case_new_taipei`
* **苗栗縣**：`python main.py --city miaoli --case-dir cases/case_miaoli`

---

## 📁 案件資料夾與 `info.txt` 規範 (Case Folder Specification)

每個檢舉案件需獨立放置於一個資料夾中，資料夾內部需包含：
1. **`info.txt`**：文字案件說明檔（UTF-8 編碼）
2. **佐證媒體**：照片 (`.jpg`, `.png`) 或影片 (`.mp4`)

### `info.txt` 檔案範例內容：
```text
違規日期: 2026-08-02
違規時間: 10:15
違規行政區: 信義區
交通違規地點: 臺北市信義區市府路1號之5
車牌號碼: ABC-1234
違規車種類型: 汽車
違規事實: 汽車於人行道、行人穿越道臨時停車(但機車及騎樓不在此限)
違規事實敘述: 該車輛在人行道上臨時停車，阻礙行人通行。
```

---

## 📂 專案架構 (Project Structure)

```text
自動填表單第二版/
├── main.py                   # 系統主入口與 CLI 命令行路由
├── app_gui.py                # Tkinter GUI 圖形化控制台
├── setup_profile.py          # 檢舉人 Profile 設定介面
├── configs/                  # 20+ 縣市 JSON 控制項選擇器設定檔
│   ├── taipei.json
│   ├── new_taipei.json
│   ├── miaoli.json
│   └── ...
├── drivers/                  # 20+ 縣市專屬驅動策略 (BaseDriver 範本模式)
│   ├── base_driver.py
│   ├── taipei_driver.py
│   ├── new_taipei_driver.py
│   ├── miaoli_driver.py
│   └── ...
├── plugins/                  # 人機協作驗證輔助插件
│   ├── base_plugin.py
│   └── human_verification_helper.py
├── utils/                    # 網路攔截器與連線工具
│   └── network_interceptor.py
├── cases/                    # 測試案件範例目錄
├── .gitignore                # 敏感資料與暫存檔排除
└── README.md                 # 專案說明文件
```
---
