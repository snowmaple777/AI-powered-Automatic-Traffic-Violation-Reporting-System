# 合併驗證（2026-09-19）

- 9 項單元測試通過：動態後端替換、關閉／重置、初始化失敗清理、設定相對路徑、
  缺少權重及相依階段、錯誤回傳型態、深度快取／ROI、OCR 投票／節流／無效文字、
  車牌座標／沿用框期限／對象篩選及輸出 schema。
- 45 個合併相關 Python 檔通過語法檢查；四個複製的 ONNX 權重通過 SHA-256 核對。
- 完整六階段：ms01 前 6 幀，PyTorch 使用自動裝置選擇（本機 CUDA），ONNX 使用 CPU。
  成功輸出 JSONL、CSV、6 幀標記影片及違規候選檔；確認有實際車牌框與 OCR 文字。
  深度快取來源依序為 0、0、2、2、4、4。
- ONNX 替換：只用 `configs/onnx_vehicle.json` 改車輛權重，CPU 執行 ms01 前 2 幀，
  原三模型流程成功輸出 JSONL 與 CSV。
- 新 schema 輸出可交給原 evaluate_violations.py 重算。
- 測試輸出：outputs/merge_full_verified、outputs/merge_onnx_verified；機器可讀摘要：
  outputs/merge_verification.json。既有 outputs 檔未覆寫。

限制：短片冒煙測試證明接口及推論路徑可運作，不代表完整影片的辨識準確率或效能評測。
填表工具僅合併與語法檢查，未操作外部網站。

本機虛擬環境使用 include-system-site-packages；pip check 顯示繼承環境中的
typer/rich、openxlab/filelock、openxlab/setuptools 版本衝突。未為這些既有工具調整
全域套件；上述推論及單元測試均通過。MMSegmentation 的 YAPF 快取在受限沙箱會卡住，
實際模型測試採已獲准的沙箱外本機執行。一般終端機執行不受該沙箱限制。
