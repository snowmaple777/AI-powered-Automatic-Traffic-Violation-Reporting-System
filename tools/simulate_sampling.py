"""標線取樣策略離線模擬：以「每幀都直接推論」的輸出（標線 interval 1）模擬其他排程，不需要 GPU 或模型。

非推論幀改用上一個推論幀的遮罩，交由 FrameProcessor 依記錄的鏡頭運動推移（與實際降頻相同的程式路徑），
再以目前的程式重跑完整後處理與規則。用來在跑 GPU 實驗前，先比較不同排程的事件、對齊品質與推論次數。

策略（--policy，可重複；以 + 串接修飾）：
  fixed:N                 與 MarkingModel interval N 相同：index % N == 0 推論
  stationary:N:M          前一幀鏡頭靜止（ego_motion.reason == stationary）時允許遮罩年齡到 M 幀，否則 N 幀
  +refresh_now            本幀鏡頭運動無效時重新推論（= refresh_on_invalid_motion，但不計等待成本）
  +refresh_prev           前一幀鏡頭運動無效時重新推論（不需等待本幀運動估計）

  python tools/simulate_sampling.py --source outputs/eval/sampling_20261102_i1 --out outputs/eval/sim_sampling \\
      --policy fixed:2 --policy fixed:2+refresh_prev --policy stationary:2:4+refresh_prev

限制：只模擬標線排程；車輛、燈號、深度、車牌仍是 interval 1 輸出。推論次數不等於實際時間（推移與等待另計）。
來源必須是每幀直接推論的輸出（road_marking_source.direct 全為 True），否則拒絕執行。
"""
import argparse
import json
import sys
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.real_replay import RecordedEgoMotion, RecordedPipeline, _install_torch_stub  # noqa: E402


class Schedule:
    """依記錄的鏡頭運動決定每幀是否直接推論；只看本幀之前已知的資訊（refresh_now 例外，與現行模式相同）。"""

    def __init__(self, spec, motions):
        parts = spec.split("+")
        base = parts[0].split(":")
        self.spec, self.motions = spec, motions
        self.modifiers = set(parts[1:])
        unknown = self.modifiers - {"refresh_now", "refresh_prev"}
        if unknown:
            raise ValueError(f"未知的修飾：{sorted(unknown)}")
        if base[0] == "fixed" and len(base) == 2:
            self.kind, self.interval, self.still_interval = "fixed", int(base[1]), None
        elif base[0] == "stationary" and len(base) == 3:
            self.kind, self.interval, self.still_interval = "stationary", int(base[1]), int(base[2])
        else:
            raise ValueError(f"無法解析策略：{spec}")
        if self.interval < 1 or (self.still_interval is not None and self.still_interval < self.interval):
            raise ValueError(f"間隔不合理：{spec}")
        self.last = None
        self.stats = {"policy": spec, "frames": 0, "inferences": 0, "reasons": {}}

    def _invalid(self, index):
        motion = self.motions.get(index) or {}
        return not motion.get("valid", False) and motion.get("reason") != "first_frame"

    def due(self, index):
        reason = None
        if self.last is None:
            reason = "startup"
        elif self.kind == "fixed":
            reason = "baseline_due" if index % self.interval == 0 else None
        else:
            previous = self.motions.get(index - 1) or {}
            limit = self.still_interval if previous.get("reason") == "stationary" else self.interval
            reason = "baseline_due" if index - self.last >= limit else None
        if reason is None and "refresh_prev" in self.modifiers and self._invalid(index - 1):
            reason = "previous_motion_invalid"
        if reason is None and "refresh_now" in self.modifiers and self._invalid(index):
            reason = "motion_invalid"
        self.stats["frames"] += 1
        why = reason or "cache"
        self.stats["reasons"][why] = self.stats["reasons"].get(why, 0) + 1
        if reason is not None:
            self.stats["inferences"] += 1
            self.last = index
        return reason is not None


class SimulatedPipeline(RecordedPipeline):
    def __init__(self, records, mask_dir, palette, schedule, classes, min_pixels):
        super().__init__(records, mask_dir, palette)
        self.schedule, self.classes, self.min_pixels = schedule, list(classes), min_pixels
        self.cached = None

    def infer(self, frame, context):
        results = super().infer(frame, context)
        marking = results["road_markings"]
        if self.schedule.due(context.index) or self.cached is None:
            self.cached = (context.index, marking.artifacts.get("mask"), marking.records)
        source, mask, records = self.cached
        artifacts = {"mask": mask, "palette": self.palette, "source_frame": source,
                     "classes": self.classes, "min_pixels": self.min_pixels} if mask is not None else {}
        results["road_markings"] = self.ModelResult(records, artifacts)
        return results


def check_direct(records, name):
    for record in records:
        source = record.get("postprocessing", {}).get("road_marking_source") or {}
        if not source.get("direct", False):
            raise ValueError(f"{name} 第 {record['frame']['index']} 幀不是直接推論，來源必須是標線 interval 1 的輸出")


def simulate(folder, out, spec, min_pixels=100):
    _install_torch_stub()
    import run_pipeline
    from road_marking_config import classes, palette
    name = folder.name
    records = [json.loads(line) for line in (folder / f"{name}_detections.jsonl").read_text(encoding="utf-8").splitlines()
               if line.strip()]
    check_direct(records, name)
    motions = {r["frame"]["index"]: r["postprocessing"].get("ego_motion") for r in records}
    schedule = Schedule(spec, motions)
    truth = json.loads((ROOT / "eval/ground_truth.json").read_text(encoding="utf-8-sig"))
    video = ROOT / truth["videos"][name]["file"]
    pipeline = SimulatedPipeline(records, folder / "masks", palette, schedule, classes, min_pixels)
    argv = [str(video), "--output-dir", str(out), "--rules", "all", "--no-violation-clips",
            "--max-frames", str(len(records)), "--device", "cpu"]
    original = run_pipeline.EgoMotionTracker
    run_pipeline.EgoMotionTracker = lambda config: RecordedEgoMotion(records)
    try:
        with ExitStack() as resources:
            run_pipeline.run(run_pipeline.parse_args(argv), resources, pipeline=pipeline)
    finally:
        run_pipeline.EgoMotionTracker = original
    return schedule.stats


def quality(clip):
    unaligned = max_age = direct = 0
    with (clip / f"{clip.name}_detections.jsonl").open(encoding="utf-8") as source:
        for line in source:
            evidence = json.loads(line)["postprocessing"].get("road_marking_source") or {}
            direct += bool(evidence.get("direct"))
            unaligned += not evidence.get("aligned", False)
            max_age = max(max_age, evidence.get("age_frames", 0))
    return {"direct_frames": direct, "unaligned_frames": unaligned, "max_age_frames": max_age}


def label_of(spec):
    return spec.replace(":", "_").replace("+", "__")


def main(argv=None):
    from eval.metrics import evaluate, load_predictions
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, required=True, help="標線 interval 1 的評估資料夾")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--policy", action="append", required=True)
    parser.add_argument("--videos", nargs="*", help="只模擬這些影片（預設全部）")
    parser.add_argument("--min-pixels", type=int, default=100, help="與實際執行的 road_markings min_pixels 相同")
    parser.add_argument("--report-only", action="store_true", help="不重跑，只彙整已完成的模擬")
    args = parser.parse_args(argv)
    folders = sorted(p for p in args.source.iterdir() if (p / f"{p.name}_detections.jsonl").is_file())
    if args.videos:
        folders = [p for p in folders if p.name in set(args.videos)]
    truth = json.loads((ROOT / "eval/ground_truth.json").read_text(encoding="utf-8-sig"))
    report, lines = {"source": str(args.source), "policies": {}}, ["標線取樣策略離線模擬（推論次數只計標線模型）"]
    for spec in args.policy:
        target = args.out / label_of(spec)
        videos, failed = {}, {}
        for folder in folders:
            clip = target / folder.name
            marker = target / f"{folder.name}.sim.json"
            if not marker.is_file() and not args.report_only:
                print(f"模擬 {spec} {folder.name}", flush=True)
                try:
                    stats = simulate(folder, clip, spec, args.min_pixels)
                except RuntimeError as error:  # 例：此平台的 OpenCV 無法解碼該影片
                    failed[folder.name] = str(error)
                    continue
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
            if marker.is_file():
                videos[folder.name] = dict(json.loads(marker.read_text(encoding="utf-8")), **quality(clip))
        predictions = load_predictions([target / name for name in videos], "suspected")
        _, metrics, plates_total, plates_correct = evaluate(truth, predictions)
        for name in videos:
            videos[name]["events"] = [(e["rule_id"], e["status"], round(e["timestamp_sec"], 3), e.get("object_id"))
                                      for e in predictions.get(name, [])]
        report["policies"][spec] = {"videos": videos, "failed": failed, "metrics": metrics,
                                    "plates": [plates_correct, plates_total]}
        frames = sum(v["frames"] for v in videos.values())
        inferences = sum(v["inferences"] for v in videos.values())
        lines.append(f"\n{spec}：{len(videos)} 支 {frames} 幀，標線推論 {inferences} 次（{inferences / max(frames, 1):.1%}）")
        lines.append(f"  TP/FP/FN {metrics}；車牌 {plates_correct}/{plates_total}")
        for name, video in videos.items():
            lines.append(f"  {name}: 推論 {video['inferences']}；未對齊 {video['unaligned_frames']}；"
                         f"最大年齡 {video['max_age_frames']}；原因 {video['reasons']}")
            lines.append(f"    事件 {video['events']}")
        for name, error in failed.items():
            lines.append(f"  {name}: 略過（{error}）")
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.out / "summary.txt").write_text("\n".join(lines), encoding="utf-8-sig")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
