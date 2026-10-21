"""模型效能選項評估：量測各組合的速度，以及與現行設定輸出的一致程度。

標線分割：推論解析度（inference_scale）與半精度（fp16）。以現行設定的遮罩為基準，
          計算重點類別的 IoU（交集／聯集，跨幀加總）。
車牌偵測：固定輸入尺寸（fixed_input）與裁切合批（batch_crops）。以最近一次驗證的
          車輛偵測結果為上游輸入，比較車牌框與現行設定的召回率與 IoU。
結果存於 outputs/verify/profile/，摘要印在最後。用法：python tools/profile_models.py
"""
import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.verify_stage import DEFAULT_CLIPS, VERIFY_DIR  # noqa: E402

KEY_CLASSES = ("stop line", "solid double yellow", "solid double white", "crosswalk", "motor waiting zone")
# (名稱, 推論解析度, fp16, roi_top, fast_attention)。前兩輪已排除：降解析度（停止線 IoU 約 0.1）、
# 只分割道路區域（停止線 IoU 0.27–0.37，模型依賴整張畫面的上下文）。
SEG_VARIANTS = [("原始 fp32", None, False, 0.0, False), ("fast_attention fp32", None, False, 0.0, True),
                ("fast_attention fp16", None, True, 0.0, True)]
# 第一項為比較基準（原始逐張、rect 縮放、單一 session）。batch_crops／fixed_input 已確認較慢（形狀仍會切換）。
PLATE_VARIANTS = [("原始逐張", {}), ("stable_shapes", {"stable_shapes": True})]
PLATE_OPTION_KEYS = ("batch_crops", "fixed_input", "stable_shapes")
SEG_OPTION_KEYS = ("fast_attention", "fp16", "roi_top", "inference_scale")


def synchronizer():
    try:
        import torch
        if torch.cuda.is_available():
            return torch.cuda.synchronize
    except ImportError:
        pass
    return lambda: None


def read_frames(video, indices):
    wanted, frames = set(indices), {}
    cap = cv2.VideoCapture(str(video))
    index = 0
    while wanted - frames.keys():
        ok, frame = cap.read()
        if not ok:
            break
        if index in wanted:
            frames[index] = frame
        index += 1
    cap.release()
    return frames


def latest_records(name):
    paths = sorted(VERIFY_DIR.glob(f"*/{name}/{name}_detections.jsonl"), key=lambda p: p.stat().st_mtime)
    paths = [p for p in paths if not p.parts[-3].startswith("_")]
    if not paths:
        return None, {}
    with paths[-1].open(encoding="utf-8") as source:
        return paths[-1].parts[-3], {r["frame"]["index"]: r for r in map(json.loads, source)}


def timed(sync, function, *args):
    sync()
    start = time.perf_counter()
    result = function(*args)
    sync()
    return result, time.perf_counter() - start


def profile_segmentation(device, clips, per_clip, sync, segmenter_class, params):
    segmenter = segmenter_class(device=device, **{k: v for k, v in params.items() if k not in SEG_OPTION_KEYS})
    resize = next(step for step in segmenter.model.cfg.test_pipeline if step.get("type") == "Resize")
    original = resize["scale"]
    frames = {name: read_frames(ROOT / video, range(0, per_clip * 4, 4)) for name, (video, _) in clips.items()}
    class_ids = {name: segmenter.classes.index(name) for name in KEY_CLASSES if name in segmenter.classes}
    baseline, rows = {}, []
    from model_library.detectors import enable_fast_attention
    attention_forwards = {}
    for label, scale, fp16, roi_top, fast in SEG_VARIANTS:
        if fp16 and not str(device).startswith("cuda"):
            continue
        resize["scale"] = tuple(scale) if scale else original
        segmenter.fp16 = fp16
        segmenter.roi_top = roi_top
        if fast and not attention_forwards:
            attention_forwards = {m: m.__dict__.get("forward") for m in segmenter.model.modules()}
            enable_fast_attention(segmenter.model)
        warm = next(iter(next(iter(frames.values())).values()))
        segmenter.infer(warm)
        segmenter.infer(warm)
        times, inter, union, agree, total = [], {k: 0 for k in class_ids}, {k: 0 for k in class_ids}, 0, 0
        for clip, items in frames.items():
            for index, frame in items.items():
                (mask, _), seconds = timed(sync, segmenter.infer, frame)
                times.append(seconds)
                key = (clip, index)
                if label == SEG_VARIANTS[0][0]:
                    baseline[key] = mask
                    continue
                base = baseline[key]
                agree += int(np.count_nonzero(base == mask))
                total += base.size
                for name, cid in class_ids.items():
                    a, b = base == cid, mask == cid
                    inter[name] += int(np.count_nonzero(a & b))
                    union[name] += int(np.count_nonzero(a | b))
        row = {"variant": label, "ms_mean": round(1000 * statistics.mean(times), 1),
               "ms_median": round(1000 * statistics.median(times), 1), "frames": len(times)}
        if label != SEG_VARIANTS[0][0]:
            row["pixel_agreement"] = round(agree / max(1, total), 5)
            row["iou"] = {name: (round(inter[name] / union[name], 3) if union[name] else None) for name in class_ids}
        if label == SEG_VARIANTS[0][0]:
            row["breakdown_ms"] = segmentation_breakdown(segmenter, list(frames.values())[0], sync)
        rows.append(row)
        print(f"  標線 {label}: {row['ms_mean']} ms/幀", flush=True)
    resize["scale"] = original
    segmenter.fp16, segmenter.roi_top = False, 0.0
    return rows


def segmentation_breakdown(segmenter, frames, sync):
    """現行設定下：mmseg 前處理（CPU）、模型前向與後處理（GPU）、遮罩轉回 CPU 各自的耗時。"""
    try:
        import torch
        from mmseg.apis.utils import _preprare_data
    except ImportError:
        return None
    parts = {"prepare": [], "forward": [], "to_cpu": []}
    for frame in list(frames.values())[:8]:
        sync()
        t0 = time.perf_counter()
        data, _ = _preprare_data(frame, segmenter.model)
        sync()
        t1 = time.perf_counter()
        with torch.no_grad():
            result = segmenter.model.test_step(data)[0]
        sync()
        t2 = time.perf_counter()
        result.pred_sem_seg.data.squeeze().cpu().numpy()
        t3 = time.perf_counter()
        for key, value in zip(parts, (t1 - t0, t2 - t1, t3 - t2)):
            parts[key].append(value)
    return {key: round(1000 * statistics.mean(values), 1) for key, values in parts.items()}


def _iou(a, b):
    ix = max(0., min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0., min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    return inter / max(1e-9, (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - inter)


def profile_plates(device, clips, per_clip, sync, plate_class, params, ModelResult, FrameContext):
    sources = {}
    for name, (video, _) in clips.items():
        label, records = latest_records(name)
        if not records:
            continue
        indices = sorted(records)[:per_clip]
        sources[name] = (label, indices, records, read_frames(ROOT / video, indices))
    baseline, rows = {}, []
    for label, options in PLATE_VARIANTS:
        base = {k: v for k, v in params.items() if k not in PLATE_OPTION_KEYS}  # 由變體決定，不沿用設定檔
        model = plate_class(device=device, **base, **options)
        calls = []
        if label == PLATE_VARIANTS[0][0] and hasattr(getattr(model, "model", None), "predict"):
            original_predict = model.model.predict

            def recording_predict(source, *args, **kwargs):
                results = original_predict(source, *args, **kwargs)
                calls.append({"images": len(source) if isinstance(source, list) else 1,
                              "imgsz": kwargs.get("imgsz"),
                              "speed": [getattr(r, "speed", None) for r in results]})
                return results
            model.model.predict = recording_predict
        times, matched, base_count, extra, ious = [], 0, 0, 0, []
        for clip, (_, indices, records, frames) in sources.items():
            model.reset()
            for index in indices:
                record = records[index]
                upstream = {"vehicles": ModelResult(record["observations"]["vehicles"]),
                            "depth": ModelResult(record["observations"].get("depth", []))}
                context = FrameContext(index, record["frame"]["timestamp_sec"])
                result, seconds = timed(sync, model.infer, frames[index], context, upstream)
                times.append(seconds)
                boxes = [p["bbox_xyxy"] for p in result.records]
                if label == PLATE_VARIANTS[0][0]:
                    baseline[(clip, index)] = boxes
                    continue
                base = list(baseline[(clip, index)])
                base_count += len(base)
                for box in boxes:
                    best = max(base, key=lambda b: _iou(b, box), default=None)
                    if best is not None and _iou(best, box) >= .5:
                        matched += 1
                        ious.append(_iou(best, box))
                        base.remove(best)
                    else:
                        extra += 1
        warm = 5 if len(times) > 10 else 0
        row = {"variant": label, "ms_mean": round(1000 * statistics.mean(times[warm:]), 1),
               "ms_median": round(1000 * statistics.median(times[warm:]), 1), "frames": len(times),
               "batch_active": getattr(model, "batch_crops", None)}
        if label != PLATE_VARIANTS[0][0]:
            row.update(recall=round(matched / max(1, base_count), 4), extra_boxes=extra, baseline_boxes=base_count,
                       mean_iou=round(statistics.mean(ious), 4) if ious else None)
        if calls:
            row["diagnostics"] = plate_diagnostics(model, calls, len(times))
        rows.append(row)
        print(f"  車牌 {label}: {row['ms_mean']} ms/幀", flush=True)
        model.close()
    return rows, {name: value[0] for name, value in sources.items()}


def plate_diagnostics(model, calls, frames):
    """實際執行裝置與每次 ultralytics 呼叫的前處理／推論／後處理耗時。"""
    info = {"device_argument": str(getattr(model, "device", None)), "calls_per_frame": round(len(calls) / max(1, frames), 2)}
    try:
        backend = model.model.predictor.model
        session = getattr(getattr(backend, "backend", backend), "session", None)
        info["onnx_providers"] = session.get_providers() if session is not None else None
    except Exception as exc:
        info["onnx_providers"] = f"unavailable: {exc}"
    try:
        import onnxruntime
        info["onnxruntime_available"] = onnxruntime.get_available_providers()
    except ImportError:
        pass
    for size in sorted({c["imgsz"] for c in calls}, key=str):
        speeds = [s for c in calls if c["imgsz"] == size for s in c["speed"] if s]
        if speeds:
            info[f"imgsz_{size}"] = {"calls": sum(c["imgsz"] == size for c in calls),
                                     **{k: round(statistics.mean(s[k] for s in speeds if s.get(k) is not None), 1)
                                        for k in ("preprocess", "inference", "postprocess")}}
    return info


def format_summary(report):
    lines = [f"===== 模型效能評估（裝置 {report['device']}）====="]
    lines.append("標線分割（每幀 ms；IoU 為與現行設定遮罩相比）")
    for row in report["segmentation"]:
        text = f"  {row['variant']:<20} 平均 {row['ms_mean']:>7}  中位 {row['ms_median']:>7}"
        if "iou" in row:
            text += f"  像素一致 {row['pixel_agreement']:.4f}  IoU " + "  ".join(
                f"{k}:{v}" for k, v in row["iou"].items())
        lines.append(text)
    base = report["segmentation"][0] if report["segmentation"] else {}
    if base.get("breakdown_ms"):
        lines.append(f"  現行設定拆解 ms：{base['breakdown_ms']}")
    lines.append(f"車牌偵測（上游車輛來自 {report.get('plate_sources')}）")
    for row in report["plates"]:
        text = f"  {row['variant']:<24} 平均 {row['ms_mean']:>7}  中位 {row['ms_median']:>7}"
        if "recall" in row:
            text += (f"  召回 {row['recall']}  多出 {row['extra_boxes']}／基準 {row['baseline_boxes']}"
                     f"  平均IoU {row['mean_iou']}  批次啟用 {row['batch_active']}")
        lines.append(text)
        if row.get("diagnostics"):
            lines.append(f"  診斷：{json.dumps(row['diagnostics'], ensure_ascii=False)}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--seg-frames", type=int, default=20, help="每支影片抽樣幀數（每 4 幀取 1）")
    parser.add_argument("--plate-frames", type=int, default=40, help="每支影片連續幀數")
    parser.add_argument("--skip-segmentation", action="store_true")
    parser.add_argument("--skip-plates", action="store_true")
    args = parser.parse_args(argv)
    from run_pipeline import resolve_device
    from model_library import FrameContext, ModelResult, load_config
    from model_library.detectors import RoadMarkingSegmenter
    from model_library.perception import PlateModel
    device = resolve_device(args.device)
    config = load_config(ROOT / "configs/default.json")
    clips = {Path(video).stem: (video, frames) for video, frames in DEFAULT_CLIPS}
    sync = synchronizer()
    report = {"device": device, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "segmentation": [], "plates": []}
    if not args.skip_segmentation:
        print("[1/2] 標線分割…", flush=True)
        report["segmentation"] = profile_segmentation(device, clips, args.seg_frames, sync, RoadMarkingSegmenter,
                                                      dict(config["models"]["road_markings"]["params"]))
    if not args.skip_plates:
        print("[2/2] 車牌偵測…", flush=True)
        report["plates"], report["plate_sources"] = profile_plates(
            device, clips, args.plate_frames, sync, PlateModel, dict(config["models"]["plates"]["params"]),
            ModelResult, FrameContext)
    out = VERIFY_DIR / "profile"
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    text = format_summary(report)
    (out / "summary.txt").write_text(text, encoding="utf-8-sig")
    print("\n" + text + f"\n\n摘要已存於 {out / 'summary.txt'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
