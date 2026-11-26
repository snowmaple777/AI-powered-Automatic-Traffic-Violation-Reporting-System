# Claude（Opus 5.5）專用補充

共用的先備須知在 AGENTS.md。支援檔案引用的 Claude 工具可由下列語法載入；其他工具請自行開啟閱讀：

@AGENTS.md

## 本角色：架構與接口整合

- 你負責第 5 節的介面契約、主流程（`run_pipeline.py`、`frame_processing.py`）、設定預設值、效能，以及把功能分支整合到 `master`。
- 整合 GPT-6 Astra 的功能分支時：
  1. 讀交接文件（AGENTS.md 第 9 節），確認新功能預設關閉、測試通過、評估數字齊全。
  2. 介面變更需求由你設計；新增觀測紀錄欄位時更新 `observation_schema.py` 的說明與 `SCHEMA_VERSION`，並確保舊紀錄仍可讀取。
  3. 合併後重跑單元測試；行為不應改變的部分以 `tools/equivalence_harness.py` 確認，行為改變的部分以 `rule_eval`／`real_replay` 重新評估。
  4. 切換預設值前在 `docs/refactor_log.md` 記錄前後數字與理由；效能影響以 `tools/verify_stage.py` 量測。
- 不重寫已驗證的功能邏輯；必須改動時重新評估並寫明差異。
- 角色分工不是推送或發布授權；依使用者當次授權處理，既有明確授權不需重問。保留其他人的修改與輸入／輸出資料。

## 整合前後檢查

1. 讀 [交接範本](docs/handoff/TEMPLATE.md) 對應的功能交付，核對基準 commit、實際 diff、設定、權重識別與測試輸出。
2. 區分「功能行為改變」與「等價結構調整」。分開驗證，避免將事件差異歸因給無關重構。
3. 檢查線上辨識與離線重播是否都走相容資料流；不要只接 `run_pipeline.py` 而漏掉 `FrameProcessor`、規則重算、摘要或事件剪輯。
4. 檢查新舊 JSONL、停用選項、快取／推移來源、跨影片 reset、執行緒關閉、事件 finalize 與延後釋放的時間。
5. 若只整理程式，保留閾值、事件條件、預設開關及權重。若必須改動，明列差異並用同一評估集合比較。
6. 更新共用契約與使用說明；回填交接文件的整合結果、尚未解決問題、是否採用及回退方法。

模型版本升級應在 `model_library` 的後端與設定處消化，向上仍回傳相容的 `ModelResult`。
先核對前處理、類別順序、輸出張量、NMS、追蹤輸入及實際推論 provider，再考慮更動共用介面。
勿為了整合建立第二套 observation／event 物件階層；現有 dict 契約能表達時優先沿用。
