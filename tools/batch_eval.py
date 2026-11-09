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
import uuid
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
                       "configs/marking_compensation.json", "configs/bytetrack_perception.yaml",
                       "eval/ground_truth.json")


def file_identity(path):
    path = Path(path)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"size": path.stat().st_size, "sha256": digest.hexdigest()}


def settings_fingerprint(model_config):
    """設定及實際權重／字典指紋；不只依賴設定中宣告的雜湊。"""
    from model_library import load_config
    path = Path(model_config).resolve()
    name = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.as_posix()
    files = {name: path, **{item: ROOT / item for item in FINGERPRINT_CONFIGS}}
    config = load_config(path)
    for stage, spec in config["models"].items():
        if not spec.get("enabled", True):
            continue
        for key in ("weights", "config", "tracker", "dict_path"):
            if key not in spec.get("params", {}):
                continue
            target = Path(spec["params"][key])
            for item in sorted(target.rglob("*")) if target.is_dir() else [target]:
                if item.is_file():
                    suffix = item.relative_to(target).as_posix() if target.is_dir() else item.name
                    files[f"{stage}.{key}:{suffix}"] = item
    digest = {key: file_identity(value) for key, value in sorted(files.items())}
    return {"model_config": name, "sha256": digest}


def code_version():
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                          text=True, timeout=10, check=True).stdout.strip()
    paths = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "*.py"],
                           cwd=ROOT, capture_output=True, timeout=30, check=True).stdout.decode("utf-8").split("\0")
    sources = {p: file_identity(ROOT / p) if (ROOT / p).is_file() else None for p in sorted(set(paths)) if p}
    return {"commit": head, "source_sha256": hashlib.sha256(
        json.dumps(sources, sort_keys=True).encode("utf-8")).hexdigest()}


def write_json_atomic(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def runtime_devices(pipeline):
    """推論後檢查已建立的 session／實際模型參數裝置，而非只有可用 provider 清單。"""
    import torch
    result, seen = {}, set()
    def visit(node, path, depth):
        if node is None or id(node) in seen or depth > 7:
            return
        seen.add(id(node))
        if callable(getattr(node, "get_providers", None)):
            result[path] = {"providers": node.get_providers()}
        if isinstance(node, torch.nn.Module):
            parameter = next(node.parameters(), None)
            if parameter is not None:
                result[path] = {"device": str(parameter.device)}
        for key in ("backend", "model", "predictor", "session", "scan_model"):
            child = getattr(node, "__dict__", {}).get(key)
            if child is not None:
                visit(child, path + "." + key, depth + 1)
    for name, model in pipeline.models.items():
        visit(model, name, 0)
    return result


def output_inventory(folder):
    return {p.relative_to(folder).as_posix(): file_identity(p) for p in sorted(folder.rglob("*"))
            if p.is_file() and p.name != "done.json"}


def validate_outputs(folder, name, masks_required):
    """只對已關閉的輸出驗證；失敗不得發布完成標記。"""
    from tools.real_replay import read_mask
    timings = json.loads((folder / f"{name}_timings.json").read_text(encoding="utf-8"))
    frames = timings["frames"]
    completion = timings["completion"]
    if frames <= 0 or completion["termination"] not in ("eof", "max_frames"):
        raise ValueError("未完成可驗證的解碼範圍")
    if completion["termination"] == "eof" and frames != completion["expected_frames"]:
        raise ValueError("EOF 幀數與影片中繼資料不符")
    if completion["termination"] == "max_frames" and frames != completion["max_frames"]:
        raise ValueError("局部範圍尚未完成")
    for suffix, key in (("detections", "frame"), ("rule_diagnostics", "frame")):
        count = 0
        with (folder / f"{name}_{suffix}.jsonl").open(encoding="utf-8") as source:
            for index, line in enumerate(source):
                row = json.loads(line)
                actual = row[key]["index"] if suffix == "detections" else row[key]
                if actual != index:
                    raise ValueError(f"{suffix} 幀序不連續")
                count += 1
        if count != frames:
            raise ValueError(f"{suffix} 幀數不符")
    with (folder / f"{name}_violations.jsonl").open(encoding="utf-8") as source:
        for line in source:
            json.loads(line)
    if (folder / f"{name}_detections.csv").stat().st_size == 0:
        raise ValueError("CSV 為空")
    if masks_required:
        masks = sorted((folder / "masks").glob("*.png"))
        if len(masks) != frames or completion["masks_written"] != frames:
            raise ValueError("遮罩數量不符")
        for index, path in enumerate(masks):
            if path.stem != f"{index:06d}" or read_mask(path) is None:
                raise ValueError(f"遮罩不可讀或順序錯誤：{path}")
    return timings


def verify_done(folder, request=None):
    done = json.loads((folder / "done.json").read_text(encoding="utf-8"))
    if done.get("version") != 2 or done.get("status") not in ("complete", "partial"):
        raise ValueError(f"{folder} 缺少新版完整性紀錄；請用新的 label")
    if request is not None and done.get("request") != request:
        raise ValueError(f"{folder} 的程式、設定、影片或處理範圍不同；請用新的 label")
    if done.get("outputs") != output_inventory(folder):
        raise ValueError(f"{folder} 輸出遭修改、遺失或新增，不能略過")
    return done


def summarize(label, verified_only=False):
    from eval.metrics import evaluate, load_predictions
    out = EVAL_DIR / label
    truth = json.loads((ROOT / "eval/ground_truth.json").read_text(encoding="utf-8-sig"))
    folders = [out]
    if verified_only:
        folders = [p for p in sorted(out.iterdir()) if p.is_dir() and not p.name.startswith(".")
                   and verify_done(p)["status"] == "complete"]
    predictions = load_predictions(folders, "suspected")
    rows, per_rule, plate_total, plate_correct = evaluate(truth, predictions)
    _, original_rules, original_total, original_correct = evaluate(truth, predictions, plate_at_event=True)
    lines = [f"===== 全片評估 {label} ====="]
    if verified_only:
        lines.append(f"只計完整且驗證通過的影片：{len(folders)} 支；partial 與 .attempts 不計分。")
    by_category = {}
    for video, category, hit, count in rows:
        by_category.setdefault(category, []).append(hit)
    lines.append("影片層級（該片至少輸出一個同類事件）：" + "  ".join(
        f"{c}: {sum(h)}/{len(h)}" for c, h in by_category.items()))
    for rule, c in sorted(per_rule.items(), key=lambda item: str(item[0])):
        precision = f"{c['tp'] / (c['tp'] + c['fp']):.0%}" if c["tp"] + c["fp"] else "—"
        recall = f"{c['tp'] / (c['tp'] + c['fn']):.0%}" if c["tp"] + c["fn"] else "—"
        lines.append(f"事件層級 {rule}: TP {c['tp']} FP {c['fp']} FN {c['fn']}  精確率 {precision}  召回率 {recall}")
    lines.append(f"事件當下車牌完全正確：{original_correct}/{original_total}；含補充：{plate_correct}/{plate_total}")
    lines.append("逐片（命中與否、事件、目標規則最常見的阻擋原因）：")
    report = {"rows": rows, "event_level": per_rule, "videos": {},
              "truth_identity": file_identity(ROOT / "eval/ground_truth.json"),
              "verified_only": verified_only, "plate": [plate_correct, plate_total],
              "plate_at_event": [original_correct, original_total], "event_level_at_event": original_rules}
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
    if args.max_frames < 0:
        parser.error("--max-frames 不可為負數")
    if not args.label or Path(args.label).name != args.label or args.label in (".", ".."):
        parser.error("--label 必須是單一目錄名稱")
    out = EVAL_DIR / args.label
    out.mkdir(parents=True, exist_ok=True)
    lock = out / ".run.lock"
    try:
        lock_file = lock.open("x", encoding="utf-8")
    except FileExistsError:
        parser.error(f"{lock} 已存在；請先確認沒有同 label 工作，再人工移除殘留鎖")
    try:
        with lock_file:
            lock_file.write(str(time.time()))
            lock_file.flush()
            execute_batch(args, out)
    finally:
        lock.unlink()
    return 0


def execute_batch(args, out):
    if not args.summary_only:
        import run_pipeline
        from model_library import ModelPipeline, load_config
        truth = json.loads((ROOT / "eval/ground_truth.json").read_text(encoding="utf-8-sig"))
        if args.only and set(args.only) - set(truth["videos"]):
            raise ValueError(f"未知影片：{sorted(set(args.only) - set(truth['videos']))}")
        videos = {k: v for k, v in truth["videos"].items() if not args.only or k in args.only}
        fingerprint = settings_fingerprint(args.model_config)
        version = code_version()
        pending = []
        for name, spec in videos.items():
            source = (ROOT / spec["file"]).resolve()
            request = {"settings": fingerprint, "code": version, "input": {
                "path": str(source), **file_identity(source)}, "max_frames": args.max_frames}
            folder = out / name
            if folder.exists():
                verify_done(folder, request)
                print(f"{name} 已驗證完成指定範圍，略過", flush=True)
            else:
                pending.append((name, source, request))
        if not pending:
            print(summarize(args.label, verified_only=True))
            return
        with ExitStack() as shared:
            config = load_config(args.model_config)
            load_started = time.perf_counter()
            pipeline = shared.enter_context(ModelPipeline(config, run_pipeline.resolve_device("auto")))
            load_seconds = time.perf_counter() - load_started
            print(f"模型載入 {load_seconds:.2f} 秒", flush=True)
            started = time.perf_counter()
            for n, (name, source, request) in enumerate(pending, 1):
                folder = out / name
                staging = out / ".attempts" / f"{name}-{uuid.uuid4().hex}"
                staging.mkdir(parents=True)
                print(f"[{n}/{len(pending)}] {name}（已用 {(time.perf_counter() - started) / 60:.1f} 分鐘）", flush=True)
                argv_run = [str(source), "--output-dir", str(staging), "--rules", "all",
                            "--no-violation-clips", "--save-masks", str(staging / "masks"),
                            "--model-config", str(args.model_config)]
                if args.max_frames:
                    argv_run += ["--max-frames", str(args.max_frames)]
                video_started = time.perf_counter()
                try:
                    with ExitStack() as resources:
                        run_pipeline.run(run_pipeline.parse_args(argv_run), resources, pipeline=pipeline)
                    timings = validate_outputs(staging, name, "road_markings" in pipeline.models)
                    # 執行中也可能有人改碼／設定／影片；不能把混合版本發布為完成。
                    if code_version() != version or settings_fingerprint(args.model_config) != fingerprint:
                        raise ValueError("執行中程式或設定／權重變更，結果保留在 .attempts")
                    if file_identity(source) != {k: request["input"][k] for k in ("size", "sha256")}:
                        raise ValueError("執行中輸入影片變更")
                    write_json_atomic(staging / "done.json", {
                        "version": 2, "status": timings["completion"]["status"], "request": request,
                        "completion": timings["completion"], "frames": timings["frames"],
                        "seconds": round(time.perf_counter() - video_started, 3),
                        "model_load_seconds": round(load_seconds, 3),
                        "runtime_devices": runtime_devices(pipeline), "outputs": output_inventory(staging)})
                    staging.rename(folder)
                except BaseException as exc:
                    write_json_atomic(staging / "failure.json", {"error": repr(exc), "request": request})
                    raise
    print("\n" + summarize(args.label, verified_only=True))
    print(f"\n摘要已存於 {out / 'summary.txt'}")


if __name__ == "__main__":
    sys.exit(main())
