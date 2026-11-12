# 交給 Astra（GPT）：雙線持久診斷拆分（Opus，2026-11-11）

接續 4ah（雙黃線漏報覆核）與 refactor_log「4ah 審查」。這一輪只做診斷，不改任何判定；
曲線延伸與線段記憶由 Opus 另做，兩者會改到同一區塊，請不要動雙線幾何或規則門檻。

## Prompt（直接貼給 GPT）

> 你是 Astra，接續 traffic_ai 開發。先讀 AGENTS.md、docs/ASTRA.md、docs/handoff/TEMPLATE.md、
> docs/handoff/double_yellow_miss_review_4ah.md、outputs/analysis/double_yellow_4ah/report.md，
> 以及 refactor_log 的「4ah 審查」與本文件。
>
> **工作區現況**：分支 feature/conservative-marking-schedule，HEAD 1cc045a。未提交的有：你的 4ad–4ah，以及 Opus 2026-11-10 的
> 清理、tools/make_showcase.py、產生展示成果.cmd、docs/handoff/README.md。全部保留，不要 reset、還原或提交。
> 開始前跑一次完整測試確認全綠。
>
> **任務（編號 4ai）：雙線規則的持久診斷拆分，預設關閉，不改任何事件。**
> 目前 `DoubleLineCrossingRule` 的診斷只有合併的 `unsupported_footprint_or_invalid_box`，而且候選狀態被清除時沒有留下原因，
> 4ah 必須用 Python trace 讀 locals 才拆得出來。請把這些資訊變成正式、可關閉的診斷輸出：
>
> 1. **車底位置失敗分支**：`double_lines.vehicle_line_position` 每個 `return None` 分支各自一個原因碼，至少分出
>    `invalid_bbox`、`outside_near_support`（車底點在可見線段近端之外）、`outside_far_support`（遠端之外）、`endpoint_margin`（離端點太近）。
>    附：失敗的車底參考點座標、使用的線段端點、外插上限與實際需要的外插距離（像素與比例）。原本的合併原因碼保留為彙總，舊讀取端不受影響。
> 2. **候選狀態清除原因**：每次刪除 `self.states[...]` 時記錄 object_id、線段 ID、清除前的 phase、已累積秒數與次數、
>    原因（低信心、無可用線段、觀測間隔過長、運動失效、追蹤換號、重新武裝等，依程式實際分支命名），以及低信心時的實際信心值。
> 3. **原側／另一側證據摘要**：每個候選記錄首次與最後一次看到的 side（-1／0／+1）、各側累積幀數與秒數，讓「整個追蹤只有一側」的情況
>    （cdy02、cdy04）可以直接從診斷看出。
>
> **介面**：新設定（例如 `configs/double_line_rules.json` 的 `detailed_diagnostics`，預設 false），三個入口（run_pipeline、evaluate_violations、
> rule_eval）共用；開啟時寫入既有的逐幀規則診斷檔，不新增事件欄位、不改 schema 版本。關閉時輸出必須與現在逐位元相同。
>
> **驗證**：
> - 開、關兩種設定以 rule_eval 重算 31 支短片與 A5、A1，`_violations.jsonl` 逐位元相同；附逐片比較表。
> - 開啟時，用新診斷重現 4ah 的統計（7 筆漏報標註窗內：219 次線段範圍不足＝近端外 125＋遠端外 94、3 次端點太近；
>   cdy02 第 152、154 幀因低信心 0.285／0.348 清除；cdy04、cdy02 整個 ID 沒有另一側；cdy05 前段原側只有 f159／160），
>   證明不需要 trace 也能得到同樣結論。
> - 新增單元測試涵蓋每個原因碼、開關一致性與舊讀取端相容；跑完整測試。
> - 量測開啟診斷對規則階段耗時與診斷檔大小的影響（A1 整片）。
>
> **限制**：不修改 vehicle_line_position 的判定邏輯、外插距離、門檻、confirmed 條件、GT、long_video_review、third_party；不安裝套件；
> 不推送／合併。交接照 TEMPLATE，refactor_log 新增 4ai；使用說明兩份同步；handoff/README.md 加入本項。
> 完成後停下交接，不要接著做幾何修正或新的疑似類型（cdy06 壓線、錯側占用的語意待使用者決定）。
>
> **之後可接的任務（使用者另行授權才做）**：4aa 標線推論尺度實驗——A5、A3 首分鐘以原解析度與 [1280, 960] 只重跑標線，
> 對照收集組遮罩計算逐類 IoU 與事件差異，報告標線 ms／幀；不改預設。

## 分工

| 項目 | 負責 | 狀態 |
| --- | --- | --- |
| 雙線持久診斷拆分（4ai） | Astra | 本 prompt |
| 彎道延伸＋車輛遮擋前的線段記憶 | Opus | 等 4ai 完成後開始，避免同時改雙線程式 |
| 車框內標線像素排除（cdy06 遮罩污染） | Opus | 之後 |
| 壓線、錯側占用的新疑似類型 | 待使用者決定語意 | — |
| 標線推論尺度（4aa） | Astra | 保留 |
