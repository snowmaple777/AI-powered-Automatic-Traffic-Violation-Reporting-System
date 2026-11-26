# 交接文件索引（2026-11-17 更新）

依時間排列；「狀態」以 refactor_log 為準。新交接請複製 `TEMPLATE.md`。

| 編號 | 文件 | 內容 | 狀態 |
| --- | --- | --- | --- |
| 4l | [conservative_marking_schedule.md](conservative_marking_schedule.md)、[marking_evidence_gate.md](marking_evidence_gate.md) | 標線證據閘門與保守排程 | 已提交（48875bc） |
| 4t | [batch_integrity_sampling_eval.md](batch_integrity_sampling_eval.md) | 批次完整性、取樣實驗 | 已提交（cced518） |
| 4u | [recognition_failure_analysis.md](recognition_failure_analysis.md) | 辨識失敗原因盤點 | 已提交 |
| 4v | [failure_fix_and_dynamic_sampling_review.md](failure_fix_and_dynamic_sampling_review.md)、[plate_backfill_and_diagnostics_prompt.md](plate_backfill_and_diagnostics_prompt.md) | 失效改善評估、車牌補充契約與 GPT prompt | 已提交 |
| 4w | [plate_backfill.md](plate_backfill.md) | 同追蹤車牌補充（預設關） | 已提交 |
| 4x–4z | [long_video_stress_preflight.md](long_video_stress_preflight.md)、[long_video_test_plan.md](long_video_test_plan.md)、[long_video_phase0_phase1.md](long_video_phase0_phase1.md)、[long_video_retest_4z.md](long_video_retest_4z.md) | 長片盤點、測試方針、階段 0／1、覆核與修正紀錄 | 已提交 |
| 4ab–4ac | 見 refactor_log 與 long_video_retest_4z.md 第 7 節 | 長片誤報修正 1–4 | 已提交（cced518、1b3c707） |
| — | [next_development_plan.md](next_development_plan.md) | 後續開發方案 | 已提交（1cc045a） |
| 4ad | [windows_rules_4ad.md](windows_rules_4ad.md) | Windows 重算確認 4ab／4ac | 已整合（4aj，feature/conservative-marking-schedule） |
| 4ae | [depth_letterbox_4ae.md](depth_letterbox_4ae.md) | 深度 4:3 補邊修正 | 已整合（4aj，feature/conservative-marking-schedule） |
| 4af | [local_lane_motion_4af.md](local_lane_motion_4af.md) | 局部車道與動作備註（預設關） | 已整合（4aj，feature/conservative-marking-schedule） |
| 4ag | [motion_layers_4ag.md](motion_layers_4ag.md) | 分層動作備註 | 已整合（4aj，feature/conservative-marking-schedule） |
| 4ah | [double_yellow_miss_review_4ah.md](double_yellow_miss_review_4ah.md) | 雙黃線漏報逐片覆核與修正順序 | 已整合（4aj，feature/conservative-marking-schedule） |
| 4ai | [double_line_diagnostics_4ai.md](double_line_diagnostics_4ai.md)、[原始 prompt](double_line_diagnostics_prompt.md) | 雙線持久診斷拆分（預設關）；33 片事件逐位元不變 | 已整合（4aj，feature/conservative-marking-schedule） |
| 4aw | [confirmation_rules_4aw.md](confirmation_rules_4aw.md) | 七組確認規則對照、逐件誤報檢查與選用設定 | 已整合；R2＋R3、斑馬線確認、停等區佔用確認於 4bh 納入預設，其餘實驗於 4bh 移除 |
| 4ax | [crosswalk_boundary_4ax.md](crosswalk_boundary_4ax.md) | 斑馬線四組對照、召回退步與長片誤報檢查 | 已提交（32db27a）；8px 候選門檻於 4bh 納入預設，邊界檢查於 4bh 移除 |
| 4bh | 見 refactor_log 4bh | 專案整理：正式＋新影片合併評估，採用最佳組合為預設、移除落選實驗選項 | feature/conservative-marking-schedule |
| 4bi | [recall_improvement_4bi.md](recall_improvement_4bi.md) | 4bh 預設下 125 筆漏報逐筆歸因與各規則召回方案、風險、預期收益 | 只分析、未改程式 |

展示成果：`產生展示成果.cmd`／`tools/make_showcase.py`，最新輸出 `outputs/showcase_20261110`（不進 git）。
