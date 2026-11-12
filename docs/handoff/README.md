# 交接文件索引（2026-11-10 整理）

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
| 4ad | [windows_rules_4ad.md](windows_rules_4ad.md) | Windows 重算確認 4ab／4ac | 未提交，待 Opus 整合 |
| 4ae | [depth_letterbox_4ae.md](depth_letterbox_4ae.md) | 深度 4:3 補邊修正 | 未提交，待 Opus 審查 |
| 4af | [local_lane_motion_4af.md](local_lane_motion_4af.md) | 局部車道與動作備註（預設關） | 未提交，待 Opus 審查 |
| 4ag | [motion_layers_4ag.md](motion_layers_4ag.md) | 分層動作備註 | 未提交，待 Opus 審查 |

展示成果：`產生展示成果.cmd`／`tools/make_showcase.py`，最新輸出 `outputs/showcase_20261110`（不進 git）。
