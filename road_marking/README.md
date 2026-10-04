# 台灣道路標線語意分割 · SegFormer-B2

以 MMSegmentation 為基礎，辨識 RLMD 的25類道路標線。預設 **hybrid 推論：75%高斯加權滑窗 logits＋25%整張影像 logits**，先融合再分類。可用任意影片

## 快速開始（Windows）

1. 安裝64位元 Python 3.10，保留 Python Launcher。
2. 依 [模型說明](models/README.md) 把權重放入 `models/`。
3. 執行 `安裝環境.cmd`，等到 `INSTALLATION COMPLETE`。第一次需要網路，會建立本目錄的 `.venv`，安裝 PyTorch 2.1.2 CUDA12.1、MMCV2.1.0及相依套件，執行模型檢查。使用者需有相容NVIDIA驅動；無CUDA則嘗試CPU，速度較慢。
4. 執行 `開始辨識.cmd` 選影片，或把影片拖上去。預設hybrid，結果在 `outputs/時間戳記_hybrid/`。

已有舊版 `.venv` 可先執行 `.venv\Scripts\python.exe check_environment.py` 與 `smoke_inference.py`，不需為了新版模型刪除環境。環境匯入失敗才處理安裝；不要搬移已建立的venv。

```cmd
.venv\Scripts\python.exe infer_video.py "D:\videos\input.mp4"
.venv\Scripts\python.exe infer_video.py "inputs\day.mp4" "inputs\rain.mp4" --no-preview
.venv\Scripts\python.exe infer_video.py "inputs\day.mp4" --mode whole
.venv\Scripts\python.exe infer_video.py "inputs\day.mp4" --mode smooth
.venv\Scripts\python.exe infer_video.py "inputs\day.mp4" --mode uniform
```

`whole`是不切窗；`smooth`是高斯加權滑窗；`uniform`是原始等權滑窗。滑窗512×512、步長341，輸入等比例縮放至1920×1080範圍。預覽按q結束本次工作；`--max-frames 10 --no-preview`可檢查執行。`--checkpoint 路徑`可指定另一個相同25類B2模型。每次輸出新資料夾，包含影片與`run.json`。輸出影片不保留音訊。

## 畫面與模型成績

- 彩色疊圖是原始argmax分類，不補洞。顏色沿用RLMD：斑馬線深綠、停止線藍紫、停等區深紫；舊交接版的橘色停止線已回復RLMD配色。
- CW/SL計數和YES/NO另外使用信心值（0.70/0.60）、下方65% ROI、連通區域（300/150像素）與總面積（1000/500像素）門檻，不改動疊圖。
- 模型是768裁切訓練的第56000次checkpoint。原374張驗證集mIoU **58.66**、停止線IoU **57.04**、斑馬線IoU **77.11**。這些是原等權滑窗成績；尚未量化hybrid在独立雨夜測試集的準確率。
- 可見區域與遮擋處可能誤判，單双線仍可能混淆。偵測到停止線不代表已判定闖紅燈。

## 訓練與程式修正

保留正式768訓練設定、其完整繼承鏈、RCS資料集及訓練入口。資料不隨repo提供。

```text
data/rlmd/images/train/*.jpg
data/rlmd/annotations/train/*.png
data/rlmd/images/val/*.jpg
data/rlmd/annotations/val/*.png
```

影像與標註同名。標註為單通道類別ID 0～24、忽略值255；帶調色盤的P模式PNG可以呈現彩色且保留ID。不要直接把RGB彩色圖當作ID mask。資料來源、授權與切分需自行確認，勿混入獨立測試集。

原訓練：2453張train、374張val，768裁切、FP32、activation checkpointing、累積4次、AdamW主幹3e-5／分割頭9e-5、加權CE＋Dice1.5。原始config保留歷史起始權重路徑。若要從本交接模型再微調，可明確覆寫：

```cmd
.venv\Scripts\python.exe tools/train.py configs/rlmd/segformer_b2_rlmd_expanded_weather_relearn_768.py --work-dir work_dirs/my_run --cfg-options load_from=models/segformer_b2_rlmd_768_best_56000.pth
```

這會啟動新的160000次排程，不是重現原訓練起點。中斷接續同一工作才用`--resume`。驗證保留512/341等權滑窗，與hybrid推論分開，避免混淆訓練與推論成效。

本地MMSeg修正：
1. 加權CE在查詢class_weight之前處理ignore_index，避免補邊255造成CUDA索引越界。
2. `EncoderDecoder.slide_inference`新增可選高斯融合與整張logits混合；未啟用時保留等權模式。
3. RCS稀有類別抽樣實作與回歸檢查位於`mmseg/datasets`及`tests/`。

來源：[MMSegmentation](https://github.com/open-mmlab/mmsegmentation)、[RLMD](https://github.com/stu9113611/RLMD)。保留上游Apache-2.0 LICENSE及CITATION；上游說明見README_MMSegmentation.md。資料集／模型權利需依原始來源確認。
