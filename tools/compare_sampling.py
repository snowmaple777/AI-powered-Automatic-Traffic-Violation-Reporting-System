"""彙整完整性驗證過的取樣實驗；拒絕用不同影片集合比較速度。"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.batch_eval import verify_done, file_identity
from eval.metrics import evaluate, load_predictions
from plate_backfill import event_plate_text


def collect(folder):
    rows = {}
    for clip in sorted(folder.iterdir()):
        if not clip.is_dir() or clip.name.startswith("."):
            continue
        done = verify_done(clip)
        if done["status"] != "complete":
            raise ValueError(f"局部影片不能比較：{clip}")
        timing = json.loads((clip / f"{clip.name}_timings.json").read_text(encoding="utf-8"))
        direct = unaligned = age_total = max_age = run = max_run = 0
        with (clip / f"{clip.name}_detections.jsonl").open(encoding="utf-8") as source:
            for line in source:
                evidence = json.loads(line)["postprocessing"].get("road_marking_source", {})
                direct += bool(evidence.get("direct"))
                unaligned += not evidence.get("aligned", False)
                age = evidence.get("age_frames", 0)
                age_total += age
                max_age = max(max_age, age)
                run = 0 if evidence.get("direct") else run + 1
                max_run = max(max_run, run)
        events = load_predictions([clip], "suspected")[clip.name]
        rows[clip.name] = {
            "input_identity": done["request"]["input"], "code": done["request"]["code"],
            "frames": timing["frames"], "loop_seconds": timing["wall_seconds"],
            "published_seconds": done["seconds"], "model_load_seconds": done["model_load_seconds"],
            "ms_per_frame": timing["ms_per_frame"], "schedule": timing.get("road_marking_schedule"),
            "direct_frames": direct, "unaligned_frames": unaligned,
            "mean_age_frames": age_total / timing["frames"], "max_age_frames": max_age,
            "max_propagated_run": max_run,
            "events": [{"rule": e["rule_id"], "status": e["status"], "time": e["timestamp_sec"],
                        "object_id": e.get("object_id"), "plate": event_plate_text(e)}
                       for e in events]}
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folders", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    truth = json.loads((ROOT / "eval/ground_truth.json").read_text(encoding="utf-8-sig"))
    groups = {}
    expected = None
    for folder in args.folders:
        videos = collect(folder)
        if not videos or (expected is not None and set(videos) != expected):
            raise ValueError("影片集合不同或沒有完整結果，不可比較")
        expected = set(videos)
        if groups:
            baseline_videos = next(iter(groups.values()))["videos"]
            if any(videos[n][k] != baseline_videos[n][k] for n in videos
                   for k in ("frames", "input_identity", "code")):
                raise ValueError("影片、程式或實際幀數不同，不可直接比較")
        predictions = load_predictions([folder / n for n in sorted(videos)], "suspected")
        _, metrics, total, correct = evaluate(truth, predictions)
        _, original_metrics, original_total, original_correct = evaluate(truth, predictions, plate_at_event=True)
        groups[folder.name] = {"videos": videos, "metrics": metrics, "plates": [correct, total],
                             "loop_seconds": sum(v["loop_seconds"] for v in videos.values()),
                             "frames": sum(v["frames"] for v in videos.values()),
                             "plates_at_event": [original_correct, original_total],
                             "metrics_at_event": original_metrics}
    report = {"truth": file_identity(ROOT / "eval/ground_truth.json"), "groups": groups}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["取樣實驗比較（只計實際辨識迴圈；不含模型載入、發布雜湊驗證）"]
    baseline = next(iter(groups.values()))
    for label, group in groups.items():
        lines.append(f"\n{label}: {group['frames']} 幀 / {group['loop_seconds']:.3f} 秒，"
                     f"相對第一組 {baseline['loop_seconds']/group['loop_seconds']:.3f} 倍")
        lines.append(f"TP/FP/FN：{group['metrics']}；事件當下車牌 {group['plates_at_event']}；含補充 {group['plates']}")
        for name, video in group["videos"].items():
            changed = video["events"] != baseline["videos"][name]["events"]
            lines.append(f"  {name}: {video['loop_seconds']:.3f}s；直接 {video['direct_frames']}；"
                         f"未對齊 {video['unaligned_frames']}；最大年齡 {video['max_age_frames']} 幀；事件變動 {changed}")
            if changed:
                lines.append(f"    基準 {baseline['videos'][name]['events']}")
                lines.append(f"    本組 {video['events']}")
    args.output.with_suffix(".txt").write_text("\n".join(lines), encoding="utf-8-sig")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
