# 批次完整性與取樣實驗（2026-11-02）

## 授權、版本與範圍

使用者要求先進行現況檢視建議中的 1（批次完整性）與 2（四組取樣比較）。
基準 48875bc；分支 feature/conservative-marking-schedule。本次不改模型、規則或取樣預設。
工作開始時 git status 乾淨。未提交、推送或合併。

## 完整性修補

- run_pipeline.py：有效 FPS／尺寸；零幀失敗；已知總幀數的 EOF 一致性檢查；max_frames 在解碼前停止並記 partial；檢查 imwrite 回傳值；timings.completion 記終止原因、預期／實際幀數及遮罩數。
- tools/batch_eval.py：done.json v2；實際設定、權重、字典、影片、標註、commit 與 Python 原始碼內容指紋。程式改變不再只警告。
- 同 label 排他鎖；先寫 .attempts，關閉資源後驗證輸出，原子發布影片目錄。失敗保留 failure.json，不發布 done；續跑驗證完整輸出雜湊。
- 批次摘要只計完整且驗證通過的影片；partial 不計入全片指標。舊版 done 不可續用，需新 label。
- tools/compare_sampling.py：同影片集合、同輸入／程式／幀數的比較；事件列表、TP/FP/FN、車牌、迴圈耗時、直接觀測與資料年齡。
- tests/test_batch_integrity.py：14 項測試；操作文件兩份同步新增說明。

介面影響：觀測 schema 與模型／規則介面不變；timings 新增 completion，run 回傳 timings（既有呼叫端可忽略）；done.json 改為 v2，不接受舊格式續跑。rule_eval 使用舊 summarize 模式，仍可分析歷史輸出；批次主入口使用 verified_only。

限制：OpenCV 讀取失敗不直接區分 EOF 與解碼錯誤。本版以總幀數檢查拒絕截斷；中繼資料錯誤的影片也會被保守拒絕。總幀數未知不發布完整結果。此檢查不是影音內容無損證明。
強制終止可能留下 .run.lock；確認沒有執行中的同 label 工作後才能人工移除。直接 run_pipeline 仍會覆寫指定輸出，批次的 staging／發布流程才有本次保護。

## 驗證

本機 Python 3.10.11、torch 2.11.0+cu128、onnxruntime-gpu 1.23.2、OpenCV 4.10.0、numpy 1.26.4、Paddle 3.3.1；RTX 5060 Laptop。
完整性測試 14 項通過；全套 163 項通過（100.096 秒）。原子 rename 在 Windows 沙箱 Temp 被拒，經使用者核准後於沙箱外重跑通過。
最後補充 runtime_devices 診斷與比較器身分檢查後，相關 14 項再次通過。
2026-11-04 最終程式全套 163 項通過（96.241 秒，沙箱外；沙箱內因暫存重新命名限制為 3 errors、1 failure，沒有改碼規避）。
以相同 label／設定／五支影片實測續跑，五支都驗證後略過，未載入 GPU 模型；紀錄 outputs/patches/integrity_resume.txt。
全套測試紀錄 outputs/patches/integrity_final_tests.txt；git diff --check 通過（只有 LF/CRLF 提示）。
GPU 實驗期間禁止更動 Python 程式、設定與權重，避免混合版本。

```powershell
$env:PYTHONIOENCODING='utf-8'
.venv\Scripts\python.exe -m unittest discover -s tests
.venv\Scripts\python.exe tools/batch_eval.py --label sampling_20261102_i2 --model-config configs/default.json --only ms01 ms07 cdy05 cdy07 soc01
.venv\Scripts\python.exe tools/batch_eval.py --label sampling_20261102_i1 --model-config configs/marking_interval_1.json --only ms01 ms07 cdy05 cdy07 soc01
.venv\Scripts\python.exe tools/batch_eval.py --label sampling_20261102_i2_refresh --model-config configs/marking_interval_2_refresh.json --only ms01 ms07 cdy05 cdy07 soc01
.venv\Scripts\python.exe tools/batch_eval.py --label sampling_20261102_i4_refresh --model-config configs/marking_interval_4_refresh.json --only ms01 ms07 cdy05 cdy07 soc01
```

四組依序、同一單張 GPU，首支影片有初始化成本；done.model_load_seconds 與 timings.wall_seconds 分開報告。指標以本次 frozen 設定中的 ground_truth 雜湊核對；先前只靠 6 支標註的數字不作對照。

## 實驗結果（2026-11-04 整理）

四組各 5 支完整影片、1,410 幀；20 個 done.json 及全部輸出雜湊通過比較器驗證，程式／輸入／幀數一致。
另核對全部 done 的標註 SHA-256 等於目前 ground_truth，且程式指紋等於本次交付程式。
標籤保留啟動日 sampling_20261102；結果在 2026-11-04 接續整理。沒有重跑或替換既有 baseline_18。

| 設定 | 迴圈秒數 | 相對預設耗時 | 直接標線幀 | 未對齊幀 | 全規則 TP/FP/FN |
| --- | ---: | ---: | ---: | ---: | --- |
| interval 2（預設） | 982.397 | 1.000 | 706 | 311 | 2/2/4 |
| interval 1 | 1406.567 | 1.432 | 1410 | 0 | 2/2/4 |
| interval 2＋refresh | 1211.919 | 1.234 | 1017 | 0 | 2/2/4 |
| interval 4＋refresh | 1182.011 | 1.203 | 823 | 0 | 1/4/5 |

全規則數字為下表加總，不能代替逐規則結果。車牌指標只在命中的有牌事件計算：前三組 1/1，i4＋refresh 為 0/0（沒有命中的有牌事件，不能解讀成 OCR 準確率 0%）。

| 規則 | 預設／i1／i2＋refresh TP/FP/FN | i4＋refresh TP/FP/FN |
| --- | --- | --- |
| 闖紅燈 | 1/1/1 | 0/1/2 |
| 雙黃線 | 0/0/3 | 0/0/3 |
| 停等區 | 0/1/0 | 0/2/0 |
| 斑馬線 | 1/0/0 | 1/1/0 |

影片層級：前三組闖紅燈 1/2、雙黃線 0/2、斑馬線 1/1；i4＋refresh 闖紅燈降至 0/2，其餘不變。
這只是本次 5 支子集，不能與先前 31 支評估直接比較。

| 影片 | i2 秒 | i1 秒 | i2＋refresh 秒 | i4＋refresh 秒 |
| --- | ---: | ---: | ---: | ---: |
| cdy05 | 419.608 | 605.442 | 525.960 | 531.968 |
| cdy07 | 115.605 | 181.560 | 168.742 | 173.803 |
| ms01 | 103.300 | 125.242 | 107.057 | 101.779 |
| ms07 | 89.374 | 118.369 | 108.466 | 110.701 |
| soc01 | 254.510 | 375.954 | 301.694 | 263.760 |

模型載入分別 60.549、26.201、27.196、26.773 秒，不含在表中；每組第一支影片的首次推論暖機仍含在迴圈內。
每組只有一次量測、固定執行順序，沒有隨機交錯或重複實驗；例如 soc01 的背景運動耗時由 145.25 增到 161.76 ms/幀，存在機器狀態波動。
因此不將 ms01 的 1.5% 速度差宣稱為加速，也不拿這次比歷史慢的速度直接判定程式退步。
本次足以否決候選預設，但不足以建立普遍效能結論；素材都是短片，未驗證分鐘／小時級長影片。

實際後端：車輛、深度及車牌 ONNX session 包含 CUDAExecutionProvider（並有 CPU fallback）；標線與紅綠燈實際 predictor 參數在 cuda:0。
紅綠燈上層包裝物件另有 CPU 參數，不能誤認實際 predictor 在 CPU；OCR 使用設定中的 CPU，未對子程序另做裝置診斷。

### 逐片變動與畫面檢查

- i1：ms01 的同一藍色貨車 CAA-8738 闖紅燈 confirmed 由 3.600 改為 3.440 秒；soc01 同一右側灰色休旅車的斑馬線 suspected 由 1.201 改為 1.064 秒。事件車輛、狀態、TP/FP/FN 都相同。
- i2＋refresh：五支影片的事件列表（規則、狀態、時間、ID、車牌）與預設完全相同。消除未對齊遮罩未帶來本子集召回提升。
- i4＋refresh／ms01：藍色貨車 CAA-8738 的 3.600 秒闖紅燈 confirmed 消失，改留下 1.760 秒停等區 suspected；依現有標註計闖紅燈 FN＋1、停等區 FP＋1。原圖確認車輛相同、紅燈越線場景仍存在。第 80–98 幀診斷顯示停止線追蹤／選線變動，越線後的新線 before_count 為 0，另有 stop_line_not_directly_observed；不能只歸因於一個閘門，更不能放寬確認門檻補回。
- i4＋refresh／soc01：新增 3.364 秒中央黑色前車 BFV-3895 的 crosswalk_stopping suspected，並非標註的右側灰色休旅車。依目前標註計 FP＋1；原圖確認對象不同，未修改標註或作法律認定。事件使用推移區域、遮擋延伸，direct_region_observation=false、region_visible_at_footprint=false，累積約 1.03 秒。
- cdy05、cdy07、ms07：四組目標違規皆漏報，沒有新增命中。i4＋refresh 在 cdy05/cdy07 直接推論分別 454/186 次，高於預設 308/106 次；不穩定畫面會反覆觸發重算，無法靠較大 interval 省時。

核對圖：outputs/patches/sampling_review/ms01.jpg、soc01.jpg。
完整比較：outputs/eval/sampling_20261102_comparison.json 與 .txt；原始逐幀診斷、遮罩、done 在各 label 目錄。

## 採用與下一步

採用批次完整性修補；取樣預設維持 interval 2、refresh_on_invalid_motion=false、activity_boost=false。
本輪不擴大到 31 支 GPU 重跑：i1／refresh 沒有增加命中，i4＋refresh 反而降低品質，且沒有總耗時優勢。

後續優先做預設關閉的「只記決策、不跳幀」診斷：記錄場景穩定時間、車輛距離停止線／雙線／斑馬線的時間或距離、運動有效性、遮擋與標線年齡。
先離線檢查能否在 ms01 越線前恢復密集觀測，且不讓 cdy05/cdy07/ms07 的連續運動失效產生虛假的省時計算。
沒有確認可分離的安全區間前，不直接把 interval 4 上線。模型匯出或幾何加速另列獨立實驗，不與取樣修改混測。

### 給接手 agent 的 prompt

先讀 AGENTS.md、docs/ASTRA.md、本文件與 docs/handoff/marking_evidence_gate.md。基準 48875bc 加本文件列出的未提交差異；先核對工作目錄，不覆寫本次修補。使用 sampling_20261102 四組既有輸出，先做離線診斷，不啟動完整 GPU 批次。目標是設計預設關閉的取樣決策診斷，確認穩定且遠離事件的位置是否有安全降頻空間；接近停止線／雙線／斑馬線、遮擋或運動失效時須保留直接觀測。重點反例：ms01 CAA-8738 的 3.44–3.60 秒越線、soc01 BFV-3895 的 3.364 秒新增候選、cdy05/cdy07/ms07 運動失效。不得放寬 confirmed、不得把推移當直接證據、不得更改 ground_truth 或預設。若需變更共用介面，列出生產端、消費端及相容方案交 Opus；交付實際 TP/FP/FN、事件車輛／時間／狀態、分段耗時與下一步是否值得 GPU 驗證。之後必須以未參與設計的影片和真正長片檢驗，不能只對這五支調參。

## 差異交付

完整差異檔 outputs/patches/batch_integrity_sampling.patch，以 48875bc 為基準，包含以下八檔（含未追蹤新檔）：
run_pipeline.py、tools/batch_eval.py、tools/compare_sampling.py、tests/test_batch_integrity.py、docs/usage.md、使用說明.txt、docs/refactor_log.md、本交接文件。
在乾淨且相同基準的 checkout 執行 git apply --check 後才能套用；目前工作目錄已套用，勿重複執行。
input 與 outputs 不納入 Git；比較報告與核對圖留在本機上述路徑，交接到其他機器時需另行攜帶。
