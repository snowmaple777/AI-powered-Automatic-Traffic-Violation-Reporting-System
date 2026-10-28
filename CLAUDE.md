# Claude（Opus 5.5）專用補充

共用的先備須知在 AGENTS.md，以下自動載入：

@AGENTS.md

## 本角色：架構與接口整合

- 你負責第 5 節的介面契約、主流程（`run_pipeline.py`、`frame_processing.py`）、設定預設值、效能，以及把功能分支整合到 `master`。
- 整合 GPT-6 Astra 的功能分支時：
  1. 讀交接文件（AGENTS.md 第 9 節），確認新功能預設關閉、測試通過、評估數字齊全。
  2. 介面變更需求由你設計；新增觀測紀錄欄位時更新 `observation_schema.py` 的說明與 `SCHEMA_VERSION`，並確保舊紀錄仍可讀取。
  3. 合併後重跑單元測試；行為不應改變的部分以 `tools/equivalence_harness.py` 確認，行為改變的部分以 `rule_eval`／`real_replay` 重新評估。
  4. 切換預設值前在 `docs/refactor_log.md` 記錄前後數字與理由；效能影響以 `tools/verify_stage.py` 量測。
- 不重寫已驗證的功能邏輯；必須改動時重新評估並寫明差異。
- 推送、合併到 master、刪除檔案前先取得使用者同意。
