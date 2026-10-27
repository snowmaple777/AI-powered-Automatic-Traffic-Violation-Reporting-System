# 道路標線影片推論｜best56000

這個分支直接包含目前版本需要的程式、原始模型權重、固定 374 張驗證影像與標註。**除了環境，不需要再向其他人索取檔案。不是 Release 或整包壓縮檔交付。**

## 1. 取得這個分支

```powershell
git clone --single-branch --branch feature/road-marking https://github.com/snowmaple777/AI-powered-Automatic-Traffic-Violation-Reporting-System.git
Set-Location -LiteralPath '.\AI-powered-Automatic-Traffic-Violation-Reporting-System\road_marking'
```

也可使用 GitHub 的 Download ZIP 後開啟其中的 `road_marking` 資料夾。
原始權重約 106 MB，超過 GitHub 一般單檔限制，故以 **Git LFS 存在同一分支的原檔名／路徑下**。
若 Git clone 或 Download ZIP 只取得 LFS 指標，安裝程式會自動從這個分支取回原權重並核對 SHA256，不需另外找下載頁或安裝 Git LFS。

## 2. 安裝環境（不執行驗證）

需求：Windows 64 位元、NVIDIA CUDA GPU／相容驅動、Python 3.10（含 Python Launcher）與安裝時網路。其他系統尚未驗證。

在 `road_marking` 資料夾的 PowerShell 執行：

```powershell
.\install.cmd
```

自動建立本目錄 `.venv`、安裝固定 PyTorch／MMCV／MMEngine、準備套件內附 FFmpeg、檢查分支中的模型與資料 SHA256，並實際匯入 custom imports／visualization／tokenizer。
FFmpeg 不需另外安裝或設定 PATH。安裝腳本**不會推論、跑 val 或訓練**。
看到 `ENVIRONMENT READY` 即表示環境與必要檔案備妥。

## 3. 自行執行驗證

```powershell
.\verify.cmd
```

此步驟由使用者主動執行，在本機完整評估兩組各 374 張 val；512/341 須重現 58.66、1024/683 須重現 59.94（±0.1）。
原本的 SHA256、strict=True、run identity 與 fingerprint 閘門保留；不提供跳過或拿別台電腦的報告通關。

## 4. 跑影片

```powershell
.\run_video.cmd 'D:\videos\your_video.mp4'
```

將路徑換成自己的影片。也可將單支影片拖到 `run_video.cmd`。
結果位於新的 `work_dirs/video_時間戳記/`，終端機會印出完整位置；不覆寫舊結果。

指定輸出位置、只產生 CSV：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Run-Video.ps1 `
  -Video 'D:\videos\your_video.mp4' -Output 'D:\results\new_result' -NoOverlay
```

可加 `-MaxFrames 20` 僅處理前 20 幀，正式完整處理不要加。

## 分支內必要檔案

| 路徑 | 用途 |
|---|---|
| `tools/road_marking_*.py`、`tools/verify_infer_pipeline.py` | 推論核心、疊色與驗證 |
| `configs/` | 原 config 完整繼承鏈、推論設定與獨立疊色 palette |
| `mmseg/` | 此模型需要的客製 MMSeg，含 tokenizer 詞彙檔 |
| `work_dirs/segformer_b2_rlmd_expanded_weather_relearn_768/best_mIoU_iter_56000.pth` | 原始完整權重，Git LFS |
| `data/rlmd_kaggle_upload/images/val/` | 全部 374 張驗證影像 |
| `data/rlmd_kaggle_upload/annotations/val/` | 配對的 374 張 mask |
| `data/rlmd_kaggle_upload/splits/val.txt` | 固定驗證清單 |
| `release/runtime_manifest.json` | 上述必要資產的逐檔 SHA256（此資料夾只放清單，無需下載 Release） |

模型 SHA256：`75ccd13c8efe31a0c1a76f22ddcd3867ebdd606287af3c39ae43b6c589d96b36`。
不含 `.venv`、訓練資料、舊模型、其他實驗、個人輸入影片或預先產生的驗證報告。

## 固定設定與輸出

純 slide 1024/683、FP32、EMA α=0.7（GPU fp16 機率）、15 幀事件投票、面積門檻 0.05%；前 14 幀投票留空。
高對比疊色讀獨立 YAML，不改模型 metainfo。

輸出：`overlay.mp4`（H.264，無音訊）、`pixel_counts.csv`、`presence_timeline.csv`、`events.csv`、完成狀態與執行指紋。

既有完整 val mIoU：512/341 **58.65745**，1024/683 **59.93841**。影片沒有 GT，平滑只量過閃爍，不能宣稱準確率提升。
詳細限制、色卡及影格資料夾操作見 [使用手冊](road_marking_inference/README.md)。

固定環境：Python 3.10、torch 2.1.2+cu121、torchvision 0.16.2+cu121、mmcv 2.1.0、mmengine 0.10.7。
環境依賴由安裝程式連網取得。LICENSE 與 THIRD_PARTY_NOTICES.md 保留上游及資產說明。
