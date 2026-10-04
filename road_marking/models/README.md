# 模型權重（不納入 Git）

把 `segformer_b2_rlmd_768_best_56000.pth` 放在這裡。
SHA-256：`75ccd13c8efe31a0c1a76f22ddcd3867ebdd606287af3c39ae43b6c589d96b36`

這份本機交接包已放入權重；GitHub 原始碼 ZIP 不含權重。
發布者請將它另附到 GitHub Release，並在 Release 說明放入本檔名及 SHA-256；目前尚未建立公開下載網址。
不要把 .pth 加入一般 Git commit（本檔約 106 MB，超過 GitHub 一般單檔限制）。

25 類 RLMD、SegFormer-B2、768 裁切訓練的第 56000 次 checkpoint。
374 張原驗證集：mIoU 58.66、停止線 IoU 57.04、斑馬線 IoU 77.11。
這些是 512/341 等權滑窗的驗證結果，不是 hybrid 的量化成績，也不是雨夜測試成績。
