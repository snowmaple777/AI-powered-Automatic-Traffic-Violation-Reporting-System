# 紅綠燈影像辨識模型

使用 YOLO11n 訓練出的影像模型，用以辨識：

- 🔴 Red
- 🟢 Green
- 🟡 Yellow

紅燈與綠燈的辨識效果較佳，但黃燈由於資料較少，辨識能力相對較弱。

模型使用 BDD100K 資料集進行訓練

---

## 使用來源

### 模型

[YOLO11n - Ultralytics](https://docs.ultralytics.com/models/yolo11/)

### 資料集

來源：

[BDD100K - Kaggle](https://www.kaggle.com/datasets/alvaromalfaro/bdd100k)

---

## 模型訓練設定

| 設定 | 數值 |
|---|---|
| Model | YOLO11n |
| Image Size | 800 |
| Batch Size | 8 |
| Epochs | 30 |
| Classes | Red / Green / Yellow |
| Ultralytics | 8.4.146 |

訓練資料中的有效 Traffic Light Annotation：

| Class | 數量 |
|---|---:|
| Red | 52,762 |
| Green | 90,872 |
| Yellow | 3,928 |
| **Total** | **147,562** |

---

## 模型結果

BDD100K Validation Set：

| Class | Precision | Recall | mAP50 | mAP50-95 |
|---|---:|---:|---:|---:|
| All | 0.677 | 0.498 | 0.537 | 0.201 |
| Red | 0.653 | 0.621 | 0.613 | 0.221 |
| Green | 0.697 | 0.724 | 0.704 | 0.256 |
| Yellow | 0.683 | 0.148 | 0.293 | 0.127 |

---
## 安裝

建議使用 Python 3.10。

安裝 Ultralytics：

```bash
pip install ultralytics==8.4.146
```

若要使用 NVIDIA GPU，需另外確認安裝的 PyTorch 版本支援 CUDA。

---

# 使用

模型權重位於：

```text
models/traffic_light_best.pt
```

## 圖片辨識

假設為 `test.jpg`：

```bash
yolo predict model="models/traffic_light_best.pt" source="test.jpg" imgsz=800 conf=0.1 save=True
```
---

## 影片辨識

假設為 `test.mp4`：

```bash
yolo predict model="models/traffic_light_best.pt" source="test.mp4" imgsz=800 conf=0.1 save=True
```
結果會輸出到：


```text
runs/detect/predict/
```
---

## 使用 NVIDIA GPU

如果已安裝支援 CUDA 的 PyTorch，可以指定：

```bash
device=0
```

完整指令例如：

```bash
yolo predict model="models/traffic_light_best.pt" source="test.mp4" imgsz=800 conf=0.15 device=0 save=True
```

其中：

```text
device=0
```

代表使用第一張 NVIDIA GPU。

---

## Confidence Threshold

建議使用：

```text
0.1 ~ 0.20
```

開始測試。

---

## 類別

目前模型有三個類別：

```text
0 = red
1 = green
2 = yellow
```

---

## 專案用途

本模型預計作為闖紅燈辨識以及後續其他檢舉事項判斷的一部分。

此模型僅負責：

看紅綠燈
