# 停止線補償驗證（2026-09-19）

## 結果

- 25 項測試全部通過：既有模型接口測試 9 項，新補償／鏡頭運動／原規則與主程式整合測試 16 項。
- 實際 ms01 既有辨識資料重播 99 幀：96 幀背景配準有效，60 幀存在通過補償品質門檻的
  停止線；產生 319 筆觀測、41 筆短暫漏偵測預測、32 筆遮擋預測。數量為每幀線段筆數，
  不是獨立停止線數，也不代表全部進入違規規則。
- 每一幀 raw 標線與來源 JSONL 完全一致；交給規則的補償 component 均為 rule_eligible。
- 模型→補償→規則完整流程成功處理 8 幀，輸出 JSONL、CSV、MP4 與候選事件檔。
- --no-marking-compensation 旁路成功處理 2 幀，停用時不附加補償標線。
- 固定模型輸出的主程式整合測試另驗證 CSV 補償列、原始資料保存及旁路行為；
  實際 8 幀片段未偵測到停止線，該片段不產生補償 CSV 列屬預期行為。
- 對同一份原始輸出使用現有規則作 A/B：未補償與補償版本都產生 0 筆事件。
  本片段不能證明事件準確率改善；仍需人工事件標註及更多場景。
- 已檢視遮擋補償畫面；預測線以紫色虛線與來源標籤顯示，保持有限線段範圍。

## 隔離確認

修改前後 SHA-256 一致：

| 檔案 | SHA-256 |
|---|---|
| violation_engine.py | F77482B20AC44096EFEDAEF6CD0C0CCF4B61C86F34CA3A615DCC8D5D7C54E574 |
| model_library/legacy.py | D15644FE1B7D72005A7E32FC674FC2E3F2D3CF8345A8D1DE55D01F6D63EF55D3 |
| model_library/pipeline.py | D38403EA729BF73C2A499907EA786BF0F72B4153032165F478D76B130EFCCCF2 |

串接只修改影片主程式與 observation schema，沒有改寫辨識模型或違規狀態機。

## 產物

- outputs/compensation_validation/ms01_detections.jsonl：99 幀原始／補償／規則輸入與診斷。
- outputs/compensation_validation/ms01_detections.mp4：重播檢視影片。
- outputs/compensation_validation/verification.json：機器可讀驗證摘要。
- outputs/compensation_pipeline_smoke：8 幀完整推論產物。
- outputs/compensation_bypass_smoke：2 幀旁路產物。

完整模型測試沿用獲准的沙箱外執行，避免本機 MMSegmentation/YAPF 系統快取權限問題。
補償重播與單元測試不需載入模型，已在沙箱內執行。
