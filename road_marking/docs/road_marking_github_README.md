# Road marking inference — best56000

台灣道路標線（25 類）的影片推論工具，使用 SegFormer B2。輸入影片，輸出標線疊色影片、每幀像素數與標線出現的時間區間。

## 需求

- Windows、NVIDIA GPU（不會自動改用 CPU）
- Python 3.10、PyTorch 2.1.2+cu121、MMCV 2.1.0、MMEngine 0.10.7（已測環境，其他系統未驗證）
- FFmpeg（需支援 libx264，bin 目錄要在 PATH）。只輸出 CSV、不產生疊色影片時不需要

## 目前這台電腦直接跑影片

**這段可整段貼到新開的 PowerShell，無論目前在哪個目錄。** 它會切回已備妥權重、
驗證資料與驗證報告的原專案，使用既有環境，不需要在待上傳資料夾重新安裝。
請勿只複製最後一條命令；`$py` 等變數只在目前的 PowerShell 工作階段有效。

```powershell
& {
    Set-Location -LiteralPath 'D:\hw_code\畢專\闖紅燈\mmsegmentation' -ErrorAction Stop
    $py = 'D:\road_marking\.venv\Scripts\python.exe'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:MPLBACKEND = 'Agg'
    $video = 'D:\hw_code\畢專\闖紅燈\多台車時單台車闖紅燈\ms04.mp4'
    $out = 'work_dirs\video_' + (Get-Date -Format 'yyyyMMdd_HHmmss_fff')
    if (-not (Test-Path -LiteralPath $py -PathType Leaf)) { throw '找不到 Python' }
    if (-not (Test-Path -LiteralPath $video -PathType Leaf)) { throw '找不到輸入影片' }
    & $py -u tools/road_marking_infer.py --settings configs/road_marking_infer.yaml `
      --input $video --out $out --overlay
    if ($LASTEXITCODE -ne 0) { throw '推論失敗，請查看上方完整錯誤' }
    if (-not (Test-Path -LiteralPath (Join-Path $out 'overlay.mp4'))) { throw '缺少輸出影片' }
    Invoke-Item -LiteralPath $out
}
```

以下步驟適用於**新電腦或獨立部署待上傳目錄**。這份原始碼包不含 `.venv`、權重、
資料集與驗證報告，不能解壓後只執行推論那一行。

## 新電腦安裝

```powershell
py -3.10 -m venv .venv
$py = '.\.venv\Scripts\python.exe'
& $py -m pip install torch==2.1.2 torchvision==0.16.2 --index-url https://download.pytorch.org/whl/cu121
& $py -m pip install --only-binary=:all: mmcv==2.1.0 -f https://download.openmmlab.com/mmcv/dist/cu121/torch2.1/index.html
& $py -m pip install -r requirements-inference.txt
& $py -m pip install --no-deps -e .
if ($LASTEXITCODE -ne 0) { throw '安裝失敗，請檢查完整錯誤' }
```

若找不到對應的 MMCV wheel，請停止並回報，不要讓 pip 自行編譯。

## 需要準備的檔案

1. **模型權重**：放在 `work_dirs/segformer_b2_rlmd_expanded_weather_relearn_768/best_mIoU_iter_56000.pth`
   SHA256：`75ccd13c8efe31a0c1a76f22ddcd3867ebdd606287af3c39ae43b6c589d96b36`
   不能換成其他或精簡過的權重。
2. **驗證集（374 張）**：`data/rlmd_kaggle_upload/` 下的 `images/val/`、`annotations/val/`、`splits/val.txt`
   `val.txt` SHA256：`618aeb35daaf724a919e3ff9e446cc1482d94fb9ffa21675d7e28e14a82ca2de`
   必須是原始的同一批影像與標註。
3. **你的影片**：在下面指令中改成實際路徑即可。

## 使用方式

先切換到本目錄。第一次使用、或換電腦與環境後，要先跑驗證（兩組完整 val，耗時較久），通過才能推論。

```powershell
$py = '.\.venv\Scripts\python.exe'
$env:PYTHONIOENCODING = 'utf-8'
$env:MPLBACKEND = 'Agg'
& $py -u tools/verify_infer_pipeline.py --settings configs/road_marking_infer.yaml
if ($LASTEXITCODE -ne 0) { throw '驗證未通過，停止推論' }
```

完成安裝、備妥上述私有檔案並通過驗證後，推論請**整段貼上**。
先切換到這份獨立 repository 根目錄，把 `$video` 改成實際影片；本段自行設定 Python，
不依賴前一個程式區塊留下的變數。FFmpeg 必須已加入 PATH。

```powershell
& {
    $py = Join-Path (Get-Location).Path '.venv\Scripts\python.exe'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:MPLBACKEND = 'Agg'
    $video = 'D:\videos\your_video.mp4'
    $out = 'work_dirs\video_' + (Get-Date -Format 'yyyyMMdd_HHmmss_fff')
    foreach ($required in @($py, $video, 'configs/road_marking_infer.yaml',
        'work_dirs/segformer_b2_rlmd_expanded_weather_relearn_768/best_mIoU_iter_56000.pth',
        'work_dirs/road_marking_verify/verification.json')) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "缺少 $required；請先完成安裝、私有檔案準備與驗證"
        }
    }
    & $py -u tools/road_marking_infer.py --settings configs/road_marking_infer.yaml `
      --input $video --out $out --overlay
    if ($LASTEXITCODE -ne 0) { throw '推論失敗，請查看上方完整錯誤' }
    if (-not (Test-Path -LiteralPath (Join-Path $out 'overlay.mp4'))) { throw '缺少輸出影片' }
    Invoke-Item -LiteralPath $out
}
```

- 輸出目錄必須是新目錄。
- 輸入若是影格資料夾，要加 `--fps` 指定真實 FPS。
- 去掉 `--overlay` 只會輸出 CSV 與事件檔，速度較快。

## 輸出

| 檔案 | 內容 |
|---|---|
| `overlay.mp4` | 疊色影片（圖例只列該幀出現的類別） |
| `pixel_counts.csv` | 每幀每類的像素數 |
| `presence_timeline.csv` | 每類是否出現（原始與投票後） |
| `events.csv` | 每類連續出現的起訖時間 |

## 推論設定（固定）

- 滑動視窗 1024/683，FP32
- 時間平滑：EMA alpha=0.7（延遲約 1～3 幀，為估計值）
- 出現判定：類別像素超過畫面 0.05%，15 幀窗口過半才算出現（前 14 幀留空，投票另有延遲）
- 疊色顏色由獨立的 YAML 設定，與模型無關；24 色色卡見[使用手冊](road_marking_inference/README.md)
