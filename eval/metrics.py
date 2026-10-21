"""以人工標註計算違規偵測指標。

標註檔 eval/ground_truth.json（格式見 eval/README.md）。兩種層級：
  影片層級：videos.<名稱>.category 有填時，該片是否至少輸出一個同規則事件。
  事件層級：reviewed=true 的影片，依規則與時間區間（±容許秒數）一對一配對，
            計算 TP／FP／FN、精確率、召回率；有標車牌時另算車牌完全正確率。
用法：python eval/metrics.py outputs/verify/stage1 [其他資料夾 ...] [--min-status confirmed]
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATUS_RANK = {"suspected": 0, "confirmed": 1}


def load_predictions(folders, min_status):
    """回傳 {影片: [事件]}；有輸出檔但沒有事件的影片也會出現（空串列）。"""
    predictions = defaultdict(list)
    for folder in folders:
        for path in sorted(Path(folder).rglob("*_violations.jsonl")):
            video = path.name.removesuffix("_violations.jsonl")
            predictions[video]
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if STATUS_RANK.get(event.get("status"), -1) >= STATUS_RANK[min_status]:
                    predictions[video].append(event)
    return predictions


def normalize_plate(text):
    return re.sub(r"[^A-Z0-9]", "", str(text or "").upper()) or None


TWO_WHEEL = {"motorcycle", "bicycle", "person"}


def edit_distance(a, b):
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def compatible(gt, event):
    """車牌（雙方都有時，差異字元數不得超過標註車牌長度的一半，容許 OCR 部分誤讀）
    與車種家族（二輪／四輪）不得矛盾。"""
    vehicle = (event.get("evidence") or {}).get("vehicle") or {}
    plate = normalize_plate(vehicle.get("plate_text"))
    expected = normalize_plate(gt.get("plate"))
    if expected and plate and edit_distance(expected, plate) > len(expected) / 2:
        return False
    if gt.get("vehicle") and event.get("vehicle_class"):
        if (gt["vehicle"] in TWO_WHEEL) != (event["vehicle_class"] in TWO_WHEEL):
            return False
    return True


def event_time(event):
    return float(event.get("timestamp_sec", 0.0))


def evaluate(truth, predictions):
    tolerance = float(truth.get("time_tolerance_sec", 1.0))
    per_rule = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    video_rows, plate_total, plate_correct = [], 0, 0
    for video, spec in sorted(truth["videos"].items()):
        if video not in predictions:
            continue  # 這次沒有處理的影片不列入計算
        events = predictions[video]
        category = spec.get("category")
        if category:
            video_rows.append((video, category, any(e.get("rule_id") == category for e in events), len(events)))
        if not spec.get("reviewed"):
            continue
        unmatched = list(events)
        for gt in spec.get("events", []):
            candidates = [e for e in unmatched if e.get("rule_id") == gt["rule_id"] and
                          gt["start_sec"] - tolerance <= event_time(e) <= gt["end_sec"] + tolerance
                          and compatible(gt, e)]
            if not candidates:
                per_rule[gt["rule_id"]]["fn"] += 1
                continue
            middle = (gt["start_sec"] + gt["end_sec"]) / 2
            best = min(candidates, key=lambda e: abs(event_time(e) - middle))
            unmatched.remove(best)
            per_rule[gt["rule_id"]]["tp"] += 1
            if gt.get("plate"):
                plate_total += 1
                predicted = ((best.get("evidence") or {}).get("vehicle") or {}).get("plate_text")
                plate_correct += normalize_plate(predicted) == normalize_plate(gt["plate"])
        for event in unmatched:
            per_rule[event.get("rule_id")]["fp"] += 1
    return video_rows, dict(per_rule), plate_total, plate_correct


def ratio(a, b):
    return f"{a / b:.1%}" if b else "—"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folders", nargs="+", type=Path, help="含 *_violations.jsonl 的輸出資料夾")
    parser.add_argument("--truth", type=Path, default=ROOT / "eval/ground_truth.json")
    parser.add_argument("--min-status", choices=sorted(STATUS_RANK), default="suspected")
    parser.add_argument("--json", type=Path, help="另存指標 JSON")
    args = parser.parse_args(argv)
    truth = json.loads(args.truth.read_text(encoding="utf-8-sig"))
    if truth.get("version") != 1:
        raise SystemExit("ground_truth.json 需要 version=1")
    rows, per_rule, plate_total, plate_correct = evaluate(truth, load_predictions(args.folders, args.min_status))
    present = rows
    print(f"== 影片層級（只計有輸出檔的影片，共 {len(present)} 支）")
    hits = 0
    for video, category, hit, count in present:
        hits += hit
        print(f"  {video:8s} {category:32s} {'命中' if hit else '未命中'}  事件數 {count}")
    print(f"  影片層級召回率：{ratio(hits, len(present))}")
    print("== 事件層級（只計 reviewed=true 的影片）")
    if not per_rule:
        print("  尚無已覆核影片；請在 ground_truth.json 標註事件並設定 reviewed=true")
    for rule, c in sorted(per_rule.items(), key=lambda item: str(item[0])):
        print(f"  {rule}: TP {c['tp']} FP {c['fp']} FN {c['fn']}  精確率 {ratio(c['tp'], c['tp']+c['fp'])}  召回率 {ratio(c['tp'], c['tp']+c['fn'])}")
    print(f"  車牌完全正確：{plate_correct}/{plate_total}（{ratio(plate_correct, plate_total)}）")
    if args.json:
        args.json.write_text(json.dumps({"video_level": present, "event_level": per_rule,
                                         "plate": [plate_correct, plate_total]}, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
