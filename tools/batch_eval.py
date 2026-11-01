"""全片評估：以現行設定辨識 eval/ground_truth.json 列出的所有影片，計算指標並統計漏判原因。

模型只載入一次；每支影片輸出到 outputs/eval/<label>/<影片>/（含逐幀遮罩，供離線重播後處理與規則）。
已完成的影片會略過（可中斷後續跑）。用法：python tools/batch_eval.py --label baseline
比較不同模型設定時，每個設定使用不同的 label：
  python tools/batch_eval.py --label marking_i4 --model-config configs/marking_interval_4_refresh.json
done.json 記錄設定指紋（模型與規則設定檔的 SHA-256）；同一 label 下設定不同時拒絕續跑，避免混用結果。
"""
import argparse
import hashlib
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
EVAL_DIR = ROOT / "outputs" / "eval"
AREA_RULES = ("crosswalk_stopping", "car_motorcycle_waiting_area")


def diagnose(folder, video, category):
    """統計該片目標規則的阻擋原因（逐幀診斷），回傳最常見的幾項。"""
    path = folder / f"{video}_rule_diagnostics.jsonl"
    if not path.is_file() or not category:
        return {}
    reasons, signal, frames, stop_lines = Counter(), Counter(), 0, 0
    with path.open(encoding="utf-8") as source:
        for line in source:
            rules = json.loads(line)["rules"]
            frames += 1
            if category == "red_light_stop_line_crossing":
                d = rules.get(category, {})
                signal[d.get("signal", {}).get("state", "unknown")] += 1
                stop_lines += bool(d.get("usable_stop_lines"))
                for item in d.get("vehicles", []) + d.get("skipped_vehicles", []):
                    reasons.update(item.get("blocked_by", []))
            elif category in AREA_RULES:
                d = rules.get(category, {})
                if d.get("status") == "blocked":
                    reasons[d.get("reason", "blocked")] += 1
                reasons.update(item.get("reason") for item in d.get("blocked", []))
            else:
                d = rules.get(category, {})
                if d.get("status") == "blocked":
                    reasons[d.get("reset_reason", "blocked")] += 1
                reasons.update(f"line:{item.get('reason')}" for item in d.get("blocked_lines", []))
                reasons.update(f"vehicle:{item.get('reason')}" for item in d.get("skipped_vehicles", []))
                reasons.update(f"phase:{item.get('phase')}" for item in d.get("states", []))
    result = {"frames": frames, "top_reasons": reasons.most_common(5)}
    if category == "red_light_stop_line_crossing":
        result.update(signal=dict(signal), frames_with_stop_line=stop_lines)
    return result


FINGERPRINT_CONFIGS = ("configs/double_line_rules.json", "configs/area_rules.json",
                       "configs/marking_compensation.json")


def settings_fingerprint(model_config):
    """影響輸出的設定檔內容雜湊；程式版本另記（commit 與是否有未提交修改），只作參考不參與比對。"""
    path = Path(model_config).resolve()
    name = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.as_posix()
    files = {name: path, **{item: ROOT / item for item in FINGERPRINT_CONFIGS}}
    digest = {key: hashlib.sha256(value.read_bytes()).hexdigest() for key, value in sorted(files.items())}
    return {"model_config": name, "sha256": digest}


def code_version():
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "--no-optional-locks", "status", "--porcelain", "--untracked-files=no"],
                               cwd=ROOT, capture_output=True, text=True, timeout=30).stdout.strip() != ""
        return {"commit": head or None, "dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None}


def summarize(label):
    from eval.metrics import evaluate, load_predictions
    out = EVAL_DIR / label
    truth = json.loads((ROOT / "eval/ground_truth.json").read_text(encoding="utf-8-sig"))
    rows, per_rule, plate_total, plate_correct = evaluate(truth, load_predictions([out], "suspected"))
    lines = [f"===== 全片評估 {label} ====="]
    by_category = {}
    for video, category, hit, count in rows:
        by_category.setdefault(category, []).append(hit)
    lines.append("影片層級（該片至少輸出一個同類事件）：" + "  ".join(
        f"{c}: {sum(h)}/{len(h)}" for c, h in by_category.items()))
    for rule, c in sorted(per_rule.items(), key=lambda item: str(item[0])):
        precision = f"{c['tp'] / (c['tp'] + c['fp']):.0%}" if c["tp"] + c["fp"] else "—"
        recall = f"{c['tp'] / (c['tp'] + c['fn']):.0%}" if c["tp"] + c["fn"] else "—"
        lines.append(f"事件層級 {rule}: TP {c['tp']} FP {c['fp']} FN {c['fn']}  精確率 {precision}  召回率 {recall}")
    lines.append(f"車牌完全正確：{plate_correct}/{plate_total}")
    lines.append("逐片（命中與否、事件、目標規則最常見的阻擋原因）：")
    report = {"rows": rows, "event_level": per_rule, "videos": {}}
    for video, category, hit, count in rows:
        folder = out / video
        events = [json.loads(l) for l in (folder / f"{video}_violations.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        summary = [(e["rule_id"], e["status"], e["timestamp_sec"]) for e in events]
        diagnosis = diagnose(folder, video, category)
        report["videos"][video] = {"hit": hit, "events": summary, "diagnosis": diagnosis}
        lines.append(f"  {video:6s} {'命中' if hit else '未中'}  事件 {summary or '無'}")
        if not hit and diagnosis:
            extra = ""
            if "signal" in diagnosis:
                extra = f"  燈號 {diagnosis['signal']}  有停止線幀 {diagnosis['frames_with_stop_line']}/{diagnosis['frames']}"
            lines.append(f"         阻擋：{diagnosis['top_reasons']}{extra}")
    text = "\n".join(lines)
    (out / "summary.txt").write_text(text, encoding="utf-8-sig")
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return text


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--only", nargs="*", help="只跑指定影片（例如 ms01 cdy02）")
    parser.add_argument("--max-frames", type=int, default=0)
    parser.add_argument("--summary-only", action="store_true", help="只重新產生摘要")
    parser.add_argument("--model-config", type=Path, default=ROOT / "configs/default.json",
                        help="模型設定檔（不同設定請用不同 --label）")
    args = parser.parse_args(argv)
    out = EVAL_DIR / args.label
    if not args.summary_only:
        import run_pipeline
        from model_library import ModelPipeline, load_config
        truth = json.loads((ROOT / "eval/ground_truth.json").read_text(encoding="utf-8-sig"))
        videos = {k: v for k, v in truth["videos"].items() if not args.only or k in args.only}
        fingerprint = settings_fingerprint(args.model_config)
        version = code_version()
        with ExitStack() as shared:
            config = load_config(args.model_config)
            pipeline = shared.enter_context(ModelPipeline(config, run_pipeline.resolve_device("auto")))
            started = time.perf_counter()
            for n, (name, spec) in enumerate(videos.items(), 1):
                folder = out / name
                done = folder / "done.json"
                if done.is_file():
                    previous = json.loads(done.read_text(encoding="utf-8"))
                    if previous.get("settings") != fingerprint:
                        raise SystemExit(f"{folder} 的 done.json 與本次設定不同（或沒有設定指紋），"
                                         f"請改用新的 --label，不要混用不同設定的結果。")
                    if previous.get("code") != version:
                        print(f"[{n}/{len(videos)}] {name} 已完成（注意：當時的程式版本 {previous.get('code')} "
                              f"與目前 {version} 不同）", flush=True)
                    else:
                        print(f"[{n}/{len(videos)}] {name} 已完成，略過", flush=True)
                    continue
                print(f"[{n}/{len(videos)}] {name}（已用 {(time.perf_counter() - started) / 60:.1f} 分鐘）", flush=True)
                argv_run = [str(ROOT / spec["file"]), "--output-dir", str(folder), "--rules", "all",
                            "--no-violation-clips", "--save-masks", str(folder / "masks"),
                            "--model-config", str(args.model_config)]
                if args.max_frames:
                    argv_run += ["--max-frames", str(args.max_frames)]
                video_started = time.perf_counter()
                with ExitStack() as resources:
                    run_pipeline.run(run_pipeline.parse_args(argv_run), resources, pipeline=pipeline)
                done.write_text(json.dumps({"seconds": round(time.perf_counter() - video_started, 1),
                                            "settings": fingerprint, "code": version},
                                           ensure_ascii=False), encoding="utf-8")
    print("\n" + summarize(args.label))
    print(f"\n摘要已存於 {out / 'summary.txt'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
