"""以真實資料離線重播：用已存的 JSONL（模型輸出）＋逐幀遮罩＋原影片，以目前的程式重跑
完整後處理與違規規則。不需要 torch 或模型權重，用於驗證規則與後處理的修改。

鏡頭運動預設採用 JSONL 記錄的估計值（--recompute-motion 才重新估計）：不同平台的 OpenCV／
影片解碼會讓估計結果不同，採用記錄值才能逐位元重現原機輸出。

  python tools/real_replay.py outputs/eval/baseline/ms01 --out /tmp/replay/ms01
  python tools/real_replay.py outputs/eval/baseline --all --out /tmp/replay   # 整個評估資料夾
"""
import argparse
import json
import sys
import types
from contextlib import ExitStack
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_torch_stub():
    try:
        import torch  # noqa: F401
    except ImportError:
        torch = types.ModuleType("torch")
        torch.cuda = types.SimpleNamespace(is_available=lambda: False, synchronize=lambda: None)
        sys.modules["torch"] = torch


class RecordedPipeline:
    """依 JSONL 與遮罩還原每幀模型輸出（ModelPipeline 介面）。"""
    stage_callback = None

    def __init__(self, records, mask_dir, palette):
        from model_library.base import ModelResult
        from model_library.registry import STAGES
        self.ModelResult, self.stages = ModelResult, STAGES
        self.records, self.mask_dir, self.palette = records, Path(mask_dir), palette
        self.models = {name: None for name in records[0]["enabled_models"]} if records else {}

    def reset(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def infer(self, frame, context):
        R = self.ModelResult
        record = self.records[context.index]
        if record["frame"]["index"] != context.index:
            raise ValueError("JSONL 與影片幀序不一致")
        obs = record["observations"]
        mask_path = self.mask_dir / f"{context.index:06d}.png"
        mask = cv2.imread(str(mask_path), cv2.IMREAD_UNCHANGED) if mask_path.is_file() else None
        vehicles = [dict(v) for v in obs["vehicles"]]
        results = {name: R() for name in self.stages}
        results["vehicles"] = R(vehicles, {"track_mappings": {}})
        results["traffic_lights"] = R(obs["traffic_lights"])
        results["road_markings"] = R(obs.get("road_markings_raw", obs["road_markings"]),
                                     {"mask": mask, "palette": self.palette} if mask is not None else {})
        results["depth"] = R(obs.get("depth", []))
        results["plates"] = R(obs.get("plates", []))
        results["ocr"] = R(obs.get("plates", []), {
            "vehicle_plate_map": {v["object_id"]: v["plate_text"] for v in vehicles
                                  if v.get("object_id") and v.get("plate_text")},
            "vehicle_plate_confidence_map": {v["object_id"]: v["text_confidence"] for v in vehicles
                                             if v.get("object_id") and v.get("text_confidence") is not None}})
        if self.stage_callback is not None:
            self.stage_callback("vehicles", results)
        return results


class RecordedEgoMotion:
    """EgoMotionTracker 介面：依幀序回傳 JSONL 記錄的鏡頭運動。"""
    def __init__(self, records):
        self.by_index = {r["frame"]["index"]: r["postprocessing"].get("ego_motion") for r in records}

    def reset(self):
        pass

    def update(self, frame, *, frame_index, timestamp_sec, vehicles):
        import numpy as np
        from road_compensation import MotionEstimate
        data = self.by_index.get(frame_index)
        if data is None:
            raise ValueError(f"第 {frame_index} 幀沒有記錄的鏡頭運動（舊版輸出請加 --recompute-motion）")
        matrix = np.asarray(data["matrix"], dtype=float) if data.get("matrix") is not None else None
        return MotionEstimate(data["valid"], matrix, data["reason"], data["inliers"], data["inlier_ratio"],
                              data["reprojection_error_px"], data["photometric_error"]), None


def replay(folder, out, video=None, extra=(), recompute_motion=False):
    """folder：含 <名稱>_detections.jsonl 與 masks/ 的輸出資料夾。回傳輸出資料夾。"""
    _install_torch_stub()
    import run_pipeline
    from road_marking_config import palette
    folder = Path(folder)
    name = folder.name
    jsonl = folder / f"{name}_detections.jsonl"
    records = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    if video is None:
        truth = json.loads((ROOT / "eval/ground_truth.json").read_text(encoding="utf-8-sig"))
        video = ROOT / truth["videos"][name]["file"]
    pipeline = RecordedPipeline(records, folder / "masks", palette)
    argv = [str(video), "--output-dir", str(out), "--rules", "all", "--no-violation-clips",
            "--max-frames", str(len(records)), "--device", "cpu", *extra]
    original = run_pipeline.EgoMotionTracker
    if not recompute_motion:
        run_pipeline.EgoMotionTracker = lambda config: RecordedEgoMotion(records)
    try:
        with ExitStack() as resources:
            run_pipeline.run(run_pipeline.parse_args(argv), resources, pipeline=pipeline)
    finally:
        run_pipeline.EgoMotionTracker = original
    return Path(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path, help="單支影片的輸出資料夾，或搭配 --all 的評估資料夾")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--video", type=Path)
    parser.add_argument("--recompute-motion", action="store_true", help="重新估計鏡頭運動，不用記錄值")
    parser.add_argument("extra", nargs="*", help="額外傳給 run_pipeline 的參數（放在 -- 之後）")
    args = parser.parse_args(argv)
    folders = sorted(p for p in args.source.iterdir() if (p / f"{p.name}_detections.jsonl").is_file()) \
        if args.all else [args.source]
    for folder in folders:
        target = args.out / folder.name if args.all else args.out
        print(f"重播 {folder.name} → {target}", flush=True)
        replay(folder, target, args.video, args.extra, args.recompute_motion)
    return 0


if __name__ == "__main__":
    sys.exit(main())
