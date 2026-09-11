# 標線辨識：組員交接包（啟動流程修正版）

## 先看這裡

這包提供程式與模型，不包含已安裝的 Python 環境。**第一次要先執行「安裝環境.cmd」，看到 INSTALLATION COMPLETE 才能執行「開始辨識.cmd」。**

找不到 `.venv\Scripts\python.exe` 表示環境尚未建立成功；搬資料夾或重複貼推論指令不會修復。安裝失敗時應處理第一個錯誤，不要繼續往下執行。

## 1. 第一次使用

1. 完整解壓到預計長期使用的位置（例如 `D:\標線`）。先不要執行影片指令。
2. 雙擊 **安裝環境.cmd**（等同 `install_environment.cmd`）。它會尋找可用的 **64 位元 Python 3.10**，支援 Python Launcher 與常見安裝位置。
3. 若顯示 `no working Python 3.10 64-bit was found`，先安裝 [Python 3.10 Windows installer (64-bit)](https://www.python.org/downloads/release/python-31011/)，保留 Python Launcher 安裝選項，再重開安裝檔。不是下載 embeddable package。
4. 安裝程式會建立本資料夾的 `.venv`，下載並安裝套件，檢查模型，以及執行一次合成圖片的模型推論。第一次需要網路、數 GB 空間，PyTorch 下載可能較久。
5. 必須看到 **INSTALLATION COMPLETE**。任何步驟失敗就會停止；把 `install_logs` 中最後一份 log 傳給負責維護的組員。
6. 雙擊 **開始辨識.cmd** 選影片，或將影片拖到此檔案。結果在 `outputs`。


## 2. 注意與手動測試

- 安裝基準：Windows 64-bit / Python 3.10 / PyTorch 2.1.2 CUDA 12.1 / MMCV 2.1.0。
- GPU 是否相容會在實際運算測試中檢查。未偵測到 CUDA 時程式嘗試 CPU，速度可能很慢；不保證所有硬體皆能使用這套環境。
- `check_environment.py` 檢查匯入、權重雜湊與模型初始化；`smoke_inference.py` 檢查合成圖片前向運算，不代表辨識品質測試。
- 同一台電腦改名或搬動資料夾後，啟動器會先檢查現有環境，通過後自動更新路徑並繼續，不會僅因改名就要求重裝。若環境實際失效，會保留具體錯誤訊息；跨電腦仍應建立自己的環境。
- 分享給其他電腦時排除 `.venv` 與 `environment_ready.json`，對方仍需執行一次安裝環境。

只有在 INSTALLATION COMPLETE 之後，才可用以下手動測試。先把影片放進 inputs，再在本資料夾開啟 PowerShell：

```powershell
& ".\.venv\Scripts\python.exe" video_inference_b2_rcs_diagnostic.py "inputs\ms06.mp4" --max-frames 10 --no-preview
```

完整影片使用「開始辨識.cmd」即可。預覽按 q 結束。`run_video.cmd` 是同一個入口的英文檔名。

## 3. 畫面意義

- 橘色 RGB(255,165,0)：停止線（ID 3）。
- 深綠色：行人穿越道（ID 2）；深紫色：機車停等區（ID 20）。其他類別沿用 RLMD 配色。


## 4. 模型與檔案

- 主要程式：video_inference_b2_rcs_diagnostic.py（沿用舊檔名，內容是橘色正式推論版）。
- 設定：configs/rlmd/segformer_b2_rlmd_rcs_accum4.py。
- 權重：work_dirs/segformer_b2_rlmd_rcs_accum4/best_mIoU_iter_232000.pth。
- configs 與 mmseg 完整保留可用原始碼及設定繼承鏈，含自訂 rlmd_rcs_dataset.py。
- requirements.txt 是推論套件清單；requirements_upstream.txt 是原專案完整清單，包含測試等額外套件，不需另行安裝。
- environment_reference.txt 是從原環境套件 METADATA 讀取的版本參考，不是可攜式虛擬環境，也不是實際 pip freeze 輸出。
- 原 BEST 在 374 張驗證圖的結果：全類別 mIoU 53.34%；停止線 IoU 44.91%、Recall 75.95%、Precision 52.36%。這是原驗證集結果，不保證每支影片相同。

本包不含資料集、私人測試影片、其他 checkpoint、訓練紀錄、虛擬環境。configs 中保留的訓練路徑不會在正常影片推論時讀取。需要重新訓練時應回原完整專案操作。

## 5. 驗證與分享

整理時完成來源檔案核對、Python 語法、設定繼承鏈與模型權重雜湊檢查；未在此整理環境重跑 GPU 推論。請依第 2 節先跑短片。

將整個資料夾壓縮成 ZIP 分享。日後若在此建立 .venv 或產生影片，再次分享前請排除 .venv、inputs 中的私人影片與 outputs 的大型結果。保留 LICENSE、CITATION.cff 及來源说明。

## 6. 來源

- RLMD 官方類別表、資料集來源：https://github.com/stu9113611/RLMD
- MMSegmentation：https://github.com/open-mmlab/mmsegmentation （授權見 LICENSE，原說明見 README_MMSegmentation.md）
- PyTorch 歷史版本安裝：https://pytorch.org/get-started/previous-versions/
- OpenMMLab 官方 Windows Python 3.10 / CUDA 12.1 / Torch 2.1 的 MMCV wheel：https://download.openmmlab.com/mmcv/dist/cu121/torch2.1/index.html

RLMD 引用：Hsiao et al., “RLMD: A Dataset for Road Marking Segmentation”, ICCE-Taiwan 2023, pp. 427–428。原始碼 LICENSE 不等於另行授予 RLMD 資料集或衍生權重的所有使用權利。
