"""重構階段驗證：單元測試 → 規則重算比對 → 短片完整辨識與各模型耗時 → 與前次報告比較。

python tools/verify_stage.py --label stage0
結果存於 outputs/verify/<label>/，摘要印在最後，可直接貼回對話。
"""
import argparse
import json
import subprocess
import sys
import time
from collections import Counter
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
VERIFY_DIR = ROOT / "outputs" / "verify"
BASELINE = ROOT / "outputs" / "refactor_baseline" / "replay.json"
# 影片, 最多幀數（0＝整支）。涵蓋紅燈、雙黃線、斑馬線三類規則。
DEFAULT_CLIPS = [("input/闖紅燈/單台車輛違規/ms01.mp4", 0), ("input/違規跨越雙實線/cdy01.mp4", 220),
                 ("input/停止在斑馬線上/soc03.mp4", 0)]


def run_tests():
    started = time.perf_counter()
    proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = [line for line in proc.stderr.splitlines() if line.startswith(("Ran ", "OK", "FAILED"))]
    errors = [line for line in proc.stderr.splitlines() if line.startswith(("ERROR:", "FAIL:"))]
    return {"ok": proc.returncode == 0, "summary": " / ".join(tail), "failures": errors,
            "seconds": round(time.perf_counter() - started, 1)}


def run_replay():
    if not BASELINE.is_file():
        return {"ok": None, "summary": "尚無基準檔，略過"}
    proc = subprocess.run([sys.executable, str(ROOT / "tools/replay_regression.py"), "--compare", str(BASELINE)],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    lines = proc.stdout.strip().splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("規則重算")), len(lines) - 1)
    return {"ok": proc.returncode == 0, "summary": "\n".join(lines[start:start + 40])}


def environment_benchmark():
    """固定運算的耗時，用來判斷速度差異是來自程式還是電腦狀態（電源、降頻、背景負載）。"""
    import numpy as np
    result = {}
    a = np.random.default_rng(0).random((768, 768), dtype=np.float32)
    start = time.perf_counter()
    for _ in range(20):
        a @ a
    result["cpu_ms"] = round((time.perf_counter() - start) * 1000 / 20, 2)
    try:
        import torch
        if torch.cuda.is_available():
            x = torch.randn(4096, 4096, device="cuda", dtype=torch.float16)
            for _ in range(5):
                x @ x
            torch.cuda.synchronize()
            start = time.perf_counter()
            for _ in range(50):
                x @ x
            torch.cuda.synchronize()
            result["gpu_ms"] = round((time.perf_counter() - start) * 1000 / 50, 2)
            result["gpu"] = torch.cuda.get_device_name(0)
    except Exception as exc:  # 基準失敗不影響驗證
        result["gpu_error"] = str(exc)
    return result


def summarize_outputs(folder, stem):
    stats = Counter()
    tracks, plates, events = set(), Counter(), []
    detections = folder / f"{stem}_detections.jsonl"
    with detections.open(encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            index = record["frame"]["index"]
            obs = record["observations"]
            stats["frames"] += 1
            vehicles = obs.get("vehicles", [])
            stats["vehicle_boxes"] += len(vehicles)
            stats["fresh_vehicle_boxes"] += sum(v.get("source_frame", index) == index for v in vehicles)
            stats["traffic_light_boxes"] += len(obs.get("traffic_lights", []))
            stats["plate_boxes"] += len(obs.get("plates", []))
            stats["double_lines"] += len(obs.get("double_lines", []))
            stats["traffic_regions"] += len(obs.get("traffic_regions", []))
            stats["motion_valid"] += bool(record.get("postprocessing", {}).get(
                "road_marking_compensation", {}).get("motion", {}).get("valid"))
            for vehicle in vehicles:
                if vehicle.get("object_id"):
                    tracks.add(vehicle["object_id"])
                    if vehicle.get("plate_text"):
                        plates[(vehicle["object_id"], vehicle["plate_text"])] += 1
    violations = folder / f"{stem}_violations.jsonl"
    if violations.is_file():
        for line in violations.read_text(encoding="utf-8").splitlines():
            if line.strip():
                event = json.loads(line)
                events.append({key: event.get(key) for key in ("rule_id", "status", "object_id", "frame")})
    final_plates = {}
    for (object_id, text), _ in sorted(plates.items(), key=lambda item: item[1]):
        final_plates[object_id] = text
    return {**stats, "vehicle_tracks": len(tracks), "plates": dict(sorted(final_plates.items())),
            "events": events}


def run_clip(video, max_frames, out_dir):
    import torch
    import run_pipeline
    from model_library import ModelPipeline

    timings = {}
    sync = torch.cuda.synchronize if torch.cuda.is_available() else (lambda: None)

    class TimedPipeline(ModelPipeline):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            for name, model in self.models.items():
                timings[name] = []

                def measured(frame, context, upstream, infer=model.infer, stage=name):
                    sync()
                    start = time.perf_counter()
                    result = infer(frame, context, upstream)
                    sync()
                    timings[stage].append(time.perf_counter() - start)
                    return result
                model.infer = measured

    original = run_pipeline.ModelPipeline
    run_pipeline.ModelPipeline = TimedPipeline
    argv = [str(ROOT / video), "--output-dir", str(out_dir), "--rules", "all", "--no-violation-clips"]
    if max_frames:
        argv += ["--max-frames", str(max_frames)]
    try:
        started = time.perf_counter()
        with ExitStack() as resources:
            run_pipeline.run(run_pipeline.parse_args(argv), resources)
        wall = time.perf_counter() - started
    finally:
        run_pipeline.ModelPipeline = original
    frames = len(next(iter(timings.values()), []))
    warm = 5 if frames > 15 else 0
    stage_ms = {k: round(1000 * sum(v[warm:]) / max(1, len(v[warm:])), 1) for k, v in timings.items()}
    model_seconds = sum(sum(v) for v in timings.values())
    summary = summarize_outputs(out_dir, Path(video).stem)
    summary.update(wall_seconds=round(wall, 1), wall_fps=round(frames / wall, 2) if wall else None,
                   stage_ms_after_warmup=stage_ms,
                   non_model_ms_per_frame=round(1000 * (wall - model_seconds) / max(1, frames), 1))
    return summary


# 結構重構時預期會改變的欄位；逐幀比對會先移除這些欄位。
EXPECTED_CHANGES = ("schema_version", "postprocessing.ego_motion", "postprocessing.double_lines.motion_source")
MODEL_KEYS = ("vehicles", "traffic_lights", "road_markings_raw", "depth", "plates")


def _drop(item, dotted):
    head, _, rest = dotted.partition(".")
    if isinstance(item, dict) and head in item:
        if rest:
            _drop(item[head], rest)
        else:
            del item[head]


def compare_detections(previous_path, current_path):
    """回傳 {frames, model_same, record_same}：模型輸出與完整紀錄（忽略預期變更欄位）相同的幀數。"""
    if not previous_path.is_file() or not current_path.is_file():
        return None
    result = {"frames": 0, "model_same": 0, "record_same": 0}
    with previous_path.open(encoding="utf-8") as old, current_path.open(encoding="utf-8") as new:
        for a, b in zip(old, new):
            a, b = json.loads(a), json.loads(b)
            result["frames"] += 1
            result["model_same"] += all(a["observations"].get(k) == b["observations"].get(k) for k in MODEL_KEYS)
            for key in EXPECTED_CHANGES:
                _drop(a, key)
                _drop(b, key)
            result["record_same"] += a == b
    return result


def previous_report(label, explicit):
    if explicit:
        path = VERIFY_DIR / explicit / "report.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    reports = [p for p in VERIFY_DIR.glob("*/report.json") if p.parent.name != label and not p.parent.name.startswith("_")]
    if not reports:
        return None
    return json.loads(max(reports, key=lambda p: p.stat().st_mtime).read_text(encoding="utf-8"))


def format_report(report, previous):
    lines = [f"===== 驗證摘要 {report['label']} =====",
             f"單元測試：{'通過' if report['tests']['ok'] else '失敗'}  {report['tests']['summary']}"]
    lines += [f"  {line}" for line in report["tests"]["failures"]]
    replay = report["replay"]
    lines.append(f"規則重算：{ {True: '一致', False: '有差異', None: '略過'}[replay['ok']] }")
    if replay["ok"] is False:
        lines += [f"  {line}" for line in replay["summary"].splitlines()[:30]]
    old_clips = (previous or {}).get("clips", {})
    for key, label in (("environment_before", "環境基準（開始）"), ("environment_after", "環境基準（結束）")):
        env, old_env = report.get(key), (previous or {}).get(key)
        if env:
            text = f"{label}：CPU {env.get('cpu_ms')} ms  GPU {env.get('gpu_ms', '—')} ms"
            if old_env:
                text += f"（前次 CPU {old_env.get('cpu_ms')}  GPU {old_env.get('gpu_ms', '—')}）"
            lines.append(text)
    for name, clip in report["clips"].items():
        if "error" in clip:
            lines.append(f"[{name}] 執行失敗：{clip['error']}")
            continue
        old = old_clips.get(name, {})
        def delta(key):
            return f"（前次 {old[key]}）" if key in old and old[key] != clip.get(key) else ""
        lines.append(f"[{name}] {clip['frames']} 幀  {clip['wall_fps']} FPS{delta('wall_fps')}  "
                     f"非模型 {clip['non_model_ms_per_frame']} ms/幀{delta('non_model_ms_per_frame')}")
        lines.append("  模型 ms/幀：" + "  ".join(f"{k} {v}" for k, v in clip["stage_ms_after_warmup"].items()))
        lines.append(f"  車輛框 {clip['vehicle_boxes']}（新偵測 {clip['fresh_vehicle_boxes']}）{delta('fresh_vehicle_boxes')}  "
                     f"追蹤 {clip['vehicle_tracks']}{delta('vehicle_tracks')}  燈號框 {clip['traffic_light_boxes']}  "
                     f"車牌框 {clip['plate_boxes']}  雙線 {clip['double_lines']}  區域 {clip['traffic_regions']}  "
                     f"運動有效 {clip['motion_valid']}/{clip['frames']}")
        same = clip.get("output_vs_previous")
        if same:
            lines.append(f"  與前次逐幀比對：模型輸出相同 {same['model_same']}/{same['frames']} 幀，"
                         f"完整紀錄相同 {same['record_same']}/{same['frames']} 幀")
        lines.append(f"  車牌：{clip['plates'] or '無'}")
        if old and old.get("plates") != clip["plates"]:
            lines.append(f"  （前次車牌：{old.get('plates') or '無'}）")
        lines.append(f"  事件：{clip['events'] or '無'}")
        if old and old.get("events") != clip["events"]:
            lines.append(f"  （前次事件：{old.get('events') or '無'}）")
    metrics = report.get("metrics")
    if metrics:
        hits = "  ".join(f"{name}:{'命中' if hit else '未命中'}" for name, hit in metrics["video_hits"])
        lines.append(f"評估（對照 eval/ground_truth.json）：{hits}")
        for rule, c in metrics["event_level"].items():
            lines.append(f"  {rule}: TP {c['tp']} FP {c['fp']} FN {c['fn']}")
        lines.append(f"  車牌完全正確 {metrics['plate'][0]}/{metrics['plate'][1]}")
    if previous:
        lines.append(f"比較對象：{previous['label']}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True, help="例如 stage0、stage1")
    parser.add_argument("--compare-to", help="指定比較的前次 label；預設為最近一次")
    parser.add_argument("--skip-pipeline", action="store_true", help="只跑測試與規則重算")
    args = parser.parse_args(argv)
    out = VERIFY_DIR / args.label
    out.mkdir(parents=True, exist_ok=True)
    print("[1/3] 單元測試…", flush=True)
    report = {"label": args.label, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "tests": run_tests()}
    print("[2/3] 規則重算比對…", flush=True)
    report["replay"] = run_replay()
    report["clips"] = {}
    if not args.skip_pipeline:
        report["environment_before"] = environment_benchmark()
        for video, max_frames in DEFAULT_CLIPS:
            name = Path(video).stem
            print(f"[3/3] 完整辨識 {name}…", flush=True)
            try:
                report["clips"][name] = run_clip(video, max_frames, out / name)
            except Exception as exc:  # 繼續其他影片，失敗原因寫入報告
                report["clips"][name] = {"error": f"{type(exc).__name__}: {exc}"}
        report["environment_after"] = environment_benchmark()
    if report["clips"]:
        from eval.metrics import evaluate, load_predictions
        truth = json.loads((ROOT / "eval/ground_truth.json").read_text(encoding="utf-8-sig"))
        rows, per_rule, plate_total, plate_correct = evaluate(truth, load_predictions([out], "suspected"))
        report["metrics"] = {"video_hits": [[r[0], r[2]] for r in rows], "event_level": per_rule,
                             "plate": [plate_correct, plate_total]}
    previous = previous_report(args.label, args.compare_to)
    if previous:
        for name in report["clips"]:
            report["clips"][name]["output_vs_previous"] = compare_detections(
                VERIFY_DIR / previous["label"] / name / f"{name}_detections.jsonl",
                out / name / f"{name}_detections.jsonl")
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    text = format_report(report, previous)
    (out / "summary.txt").write_text(text, encoding="utf-8-sig")
    print("\n" + text)
    print(f"\n摘要已存於 {out / 'summary.txt'}")
    failed = not report["tests"]["ok"] or report["replay"]["ok"] is False or any(
        "error" in clip for clip in report["clips"].values())
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
