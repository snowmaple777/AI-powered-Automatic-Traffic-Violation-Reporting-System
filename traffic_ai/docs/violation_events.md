# 違規事件分級

使用 `--rule all` 或 `--rules all` 啟用全部已註冊規則，例如：

```powershell
.\.venv\Scripts\python.exe run_pipeline.py input/ms03.mp4 --rule all --save-video
.\.venv\Scripts\python.exe evaluate_violations.py outputs/ms03_detections.jsonl --rules all
```

`all` 在執行時從 `RULE_FACTORIES` 展開，未來新增並註冊的規則會自動加入。
也可以逗號指定個別規則。重複名稱不重複啟用；`all` 與未知名稱混用仍回報錯誤。

啟用 --rules red_light_stop_line_crossing 時，*_violations.jsonl 記錄兩種狀態：

- suspected／疑似違規：底部參考點有線前→線後觀測，但越線確認或號誌證據未齊。
- confirmed／確認違規：目前設定的紅燈越線條件全部成立。

confirmed 的範圍是 configured_rule_conditions，仍保留 review_required=true：
本規則尚未建立車道與號誌的明確對應，輸出是系統規則判定。

每筆事件提供 behavior、中文 description、conditions、met_conditions、
missing_conditions，以及原始車輛、停止線、號誌與時間證據。
騎乘組合的原始成員 ID 可從 evidence.vehicle.rider_id / source_object_id 取得。

條件代碼：

| 代碼 | 意義 |
| --- | --- |
| tracked_vehicle | 有合格的車輛追蹤 |
| usable_overlapping_stop_line | 有與車框重疊的合格停止線 |
| before_line_observed | 至少兩筆線前觀測 |
| crossing_observed | 已有線前紀錄且至少兩筆線後觀測 |
| red_before_crossing | 線前位置具有近期紅燈證據 |
| red_at_crossing | 當下時序號誌為紅燈 |
| same_signal | 前後號誌身份一致 |

只有紅燈、車框上半部與線重疊、或只觀察到車在線後，都不列為疑似；綠／黃燈會中斷判定。
底部參考點由車輛框底邊計算；騎乘組合使用 vehicle_bbox_xyxy，不使用人物或組合顯示框。
底邊仍為接地位置的代理值，並非輪胎偵測，因此不能把框線相交直接稱為實際壓線。
未匹配停止線的原因仍寫入 rule_diagnostics.jsonl，不能憑紅燈替每輛車推定違規。

同一車輛每次執行至多一筆最終事件。確認事件即時輸出；疑似事件保存最強的
單一證據快照，在正常處理結束的 finalize 階段輸出。若稍後確認，移除待輸出的疑似事件。
JSONL 寫入順序不保證為時間順序，依 timestamp_sec 排序即可。
尚未完成處理或強制終止程序時，疑似事件可能尚未寫出。

舊 status=candidate 已改為 confirmed，下游程式應使用新狀態；
疑似事件描述會明列「未觀察完整過程」等限制，不把已在線後的位置當成已證實闖紅燈。
