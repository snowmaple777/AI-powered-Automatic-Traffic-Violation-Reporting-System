"""重構等價性檢查：以確定性的假模型輸出驅動完整主流程，逐位元比較兩份程式的所有輸出。

模型推論以固定劇本取代（車輛跨越雙線與停止線、騎士與自行車、停在斑馬線上的貨車、
紅綠燈切換、一次單幀漏偵測），影像使用真實影片，所以鏡頭運動估計、補償、雙線、
區域、規則、CSV、標註影片與違規剪輯都會實際執行。不需要 torch 或模型權重。

  git worktree add ../stage1 refactor-stage1      # 以 tag 取出舊版程式
  python tools/equivalence_harness.py run --code ../stage1 --out /tmp/a
  python tools/equivalence_harness.py run --code . --out /tmp/b
  python tools/equivalence_harness.py compare /tmp/a /tmp/b
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VIDEO = ROOT / "input" / "違規跨越雙實線" / "cdy01.mp4"


def install_torch_stub():
    try:
        import torch  # noqa: F401
    except ImportError:
        torch = types.ModuleType("torch")
        torch.cuda = types.SimpleNamespace(is_available=lambda: False, synchronize=lambda: None)
        sys.modules["torch"] = torch


def scenario(index, width, height, classes, mask_components, ModelResult, stages):
    import cv2
    import numpy as np
    w, h, i = width, height, index
    mask = np.zeros((h, w), np.uint8)
    cid = classes.index
    x_top, x_bottom = int(.50 * w + .3 * i), int(.42 * w + .3 * i)
    for offset in (0, 14):
        cv2.line(mask, (x_top + offset, int(.42 * h)), (x_bottom + offset, h - 1), cid("solid double yellow"), 5)
    stop_y = min(h - 20, int(.60 * h + 1.5 * i))
    cv2.rectangle(mask, (int(.15 * w), stop_y), (int(.38 * w), stop_y + 8), cid("stop line"), -1)
    for k in range(6):
        x = int(.60 * w) + k * 28
        cv2.rectangle(mask, (x, int(.82 * h)), (x + 14, int(.92 * h)), cid("crosswalk"), -1)
    cv2.rectangle(mask, (int(.62 * w), int(.64 * h)), (int(.80 * w), int(.72 * h)), cid("motor waiting zone"), -1)

    def vehicle(tid, cls_id, name, box, conf=.85):
        x1, y1, x2, y2 = [float(v) for v in box]
        return dict(bbox_xyxy=[x1, y1, x2, y2], center=[(x1 + x2) / 2, (y1 + y2) / 2],
                    bottom_center=[(x1 + x2) / 2, y2], class_id=cls_id, class_name=name, confidence=conf,
                    track_id=tid, object_id=f"vehicle-{tid:06d}", track_age_frames=i + 1, source_frame=i)

    progress = min(1.0, i / 60)
    cx, bottom = .22 * w + .30 * w * progress, .80 * h - .25 * h * progress
    vehicles = []
    if i != 20:  # 單幀漏偵測
        vehicles.append(vehicle(1, 2, "car", [cx - 70, bottom - 90, cx + 70, bottom]))
    vehicles.append(vehicle(2, 3, "motorcycle", [.66 * w, .60 * h, .70 * w, .70 * h]))
    vehicles.append(vehicle(3, 0, "person", [.10 * w + i, .50 * h, .13 * w + i, .66 * h], .7))
    vehicles.append(vehicle(4, 1, "bicycle", [.095 * w + i, .58 * h, .135 * w + i, .70 * h], .7))
    vehicles.append(vehicle(5, 7, "truck", [.58 * w, .70 * h, .78 * w, .90 * h], .9))
    color = "green" if 60 <= i < 66 else "red"
    lights = [dict(bbox_xyxy=[.47 * w, .05 * h, .49 * w, .12 * h], center=[.48 * w, .085 * h],
                   bottom_center=[.48 * w, .12 * h], class_id={"red": 0, "green": 1}[color], class_name=color,
                   confidence=.82, track_id=None)] if i >= 3 else []
    depth = [dict(object_id=v["object_id"], track_id=v["track_id"], distance_m=10. + v["track_id"],
                  source_frame=i) for v in vehicles]
    plates = []
    for v in vehicles:
        if v["track_id"] in (1, 5):
            x1, y1, x2, y2 = v["bbox_xyxy"]
            plates.append(dict(bbox_xyxy=[x1 + 20, y2 - 25, x1 + 80, y2 - 8], center=[x1 + 50, y2 - 16.5],
                               bottom_center=[x1 + 50, y2 - 8], class_id=0, class_name="license_plate",
                               confidence=.7, track_id=v["track_id"], object_id=v["object_id"],
                               vehicle_class_id=v["class_id"], distance_m=10. + v["track_id"], held=False))
    texts = {"vehicle-000001": "ABC-1234", "vehicle-000005": "KLM-5678"}
    ocr = [dict(p, raw_text=texts[p["object_id"]].replace("-", ""), text=texts[p["object_id"]],
                text_confidence=.9, confirmed=i > 10, ocr_source_frame=i) for p in plates]
    results = {name: ModelResult() for name in stages}
    results["vehicles"] = ModelResult(vehicles, {"track_mappings": {}})
    results["traffic_lights"] = ModelResult(lights)
    results["road_markings"] = ModelResult(mask_components(mask, classes, 100),
                                           {"mask": mask, "palette": [[(k * 37) % 256, (k * 91) % 256, (k * 53) % 256]
                                                                      for k in range(len(classes))]})
    results["depth"] = ModelResult(depth)
    results["plates"] = ModelResult(plates)
    results["ocr"] = ModelResult(ocr, {"vehicle_plate_map": {k: v for k, v in texts.items()
                                                               if any(p["object_id"] == k for p in plates)},
                                       "vehicle_plate_confidence_map": {p["object_id"]: .9 for p in plates}})
    return results


def run(code, out, video, frames, extra):
    code = Path(code).resolve()
    sys.path.insert(0, str(code))
    os.chdir(code)
    install_torch_stub()
    from contextlib import ExitStack
    import run_pipeline
    from model_library.base import ModelResult
    from model_library.legacy import mask_components
    from model_library.registry import STAGES
    from road_marking_config import classes

    class FakePipeline:
        def __init__(self, config, device="cpu"):
            self.models = {name: None for name in STAGES}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        stage_callback = None

        def infer(self, frame, context):
            h, w = frame.shape[:2]
            results = scenario(context.index, w, h, list(classes), mask_components, ModelResult, STAGES)
            if self.stage_callback is not None:  # 新版主流程：車輛階段後並行估計鏡頭運動
                self.stage_callback("vehicles", results)
            return results

    run_pipeline.ModelPipeline = FakePipeline
    argv = [str(video), "--output-dir", str(out), "--model-config", str(ROOT / "configs/default.json"),
            "--rules", "all", "--max-frames", str(frames), "--save-video", "--device", "cpu"] + extra
    with ExitStack() as resources:
        run_pipeline.run(run_pipeline.parse_args(argv), resources)


IGNORED_KEYS = []  # 以 --ignore 指定預期新增／變更的欄位（點分路徑），比對時移除


def _drop(item, dotted):
    head, _, rest = dotted.partition(".")
    if isinstance(item, dict) and head in item:
        if rest:
            _drop(item[head], rest)
        else:
            del item[head]


def digest_file(path):
    if path.suffix == ".mp4":
        import cv2
        cap, digest, count = cv2.VideoCapture(str(path)), hashlib.sha256(), 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            digest.update(frame.tobytes())
            count += 1
        cap.release()
        return f"{count} frames {digest.hexdigest()[:16]}"
    data = path.read_bytes()
    if path.suffix == ".jsonl" and IGNORED_KEYS:
        lines = []
        for line in data.decode("utf-8").splitlines():
            item = json.loads(line)
            for key in IGNORED_KEYS:
                _drop(item, key)
            lines.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
        data = "\n".join(lines).encode("utf-8")
    if path.suffix == ".json" and any(part.endswith("_clips") for part in path.parts):
        def strip(item):
            if isinstance(item, dict):
                return {k: strip(v) for k, v in item.items() if k not in ("clip_path", "source_video")}
            if isinstance(item, list):
                return [strip(v) for v in item]
            return item
        data = json.dumps(strip(json.loads(data)), sort_keys=True).encode()
    return hashlib.sha256(data).hexdigest()[:16]


def collect(folder):
    folder = Path(folder)
    files = {}
    for path in sorted(folder.rglob("*")):
        if path.is_file() and not path.name.endswith("_timings.json"):  # 計時每次不同
            rel = path.relative_to(folder).as_posix()
            parts = rel.split("/")
            parts = ["run_*" if p.startswith("run_") else p for p in parts]
            files["/".join(parts)] = digest_file(path)
    return files


def compare(a, b):
    left, right = collect(a), collect(b)
    differences = [f"{k}: {left.get(k)} != {right.get(k)}" for k in sorted(set(left) | set(right))
                   if left.get(k) != right.get(k)]
    for key in sorted(left):
        print(f"  {'≠' if key in {d.split(':')[0] for d in differences} else '='} {key}  {left[key]}")
    if differences:
        print("輸出不同：\n" + "\n".join(differences))
        return 1
    print(f"全部 {len(left)} 個輸出檔逐位元一致")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run")
    r.add_argument("--code", type=Path, default=ROOT)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--video", type=Path, default=DEFAULT_VIDEO)
    r.add_argument("--frames", type=int, default=90)
    r.add_argument("extra", nargs="*", help="額外傳給 run_pipeline 的參數（放在 -- 之後）")
    c = sub.add_parser("compare")
    c.add_argument("a", type=Path)
    c.add_argument("b", type=Path)
    c.add_argument("--ignore", action="append", default=[], help="比對時移除的欄位，例如 postprocessing.ego_motion")
    s = sub.add_parser("check", help="以子行程分別執行兩份程式並比較")
    s.add_argument("--reference", type=Path, required=True)
    s.add_argument("--code", type=Path, default=ROOT)
    s.add_argument("--work", type=Path, required=True)
    s.add_argument("--frames", type=int, default=90)
    s.add_argument("--video", type=Path, default=DEFAULT_VIDEO)
    s.add_argument("--ignore", action="append", default=[])
    s.add_argument("extra", nargs="*")
    args = parser.parse_args()
    if args.command == "run":
        run(args.code, args.out.resolve(), args.video.resolve(), args.frames, args.extra)
        return 0
    IGNORED_KEYS.extend(getattr(args, "ignore", []))
    if args.command == "compare":
        return compare(args.a, args.b)
    env = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
    for name, code in (("reference", args.reference), ("candidate", args.code)):
        out = (args.work / name).resolve()
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "run", "--code", str(code.resolve()),
                        "--out", str(out), "--frames", str(args.frames),
                        "--video", str(args.video.resolve()), "--", *args.extra],
                       env=env, check=True, stdout=subprocess.DEVNULL)
    return compare(args.work / "reference", args.work / "candidate")


if __name__ == "__main__":
    sys.exit(main())
