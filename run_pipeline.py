import argparse
import json
import time
from contextlib import ExitStack
from pathlib import Path

import cv2

from model_library.detectors import ROOT
from model_library import FrameContext, ModelPipeline, load_config
from violation_engine import RuleContext, ViolationEngine, create_rules
from violation_clips import add_clip_arguments, validate_clip_options, export_violation_clips
from road_compensation import CompensationConfig, EgoMotionTracker, RoadMarkingCompensator
from double_lines import double_line_rule_options
from area_rules import AREA_RULES, area_rule_options, area_geometry_options
from frame_processing import FrameProcessor
from pipeline_outputs import DetectionCsv, annotate_frame


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="可替換模型的交通影像辨識")
    parser.add_argument("--area-rule-config", type=Path, default=ROOT / "configs/area_rules.json",
                        help="停等區與行人穿越道候選規則參數")
    parser.add_argument("input", type=Path, help="輸入影片")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs")
    parser.add_argument("--device", default="auto", help="auto、cpu 或 cuda:0")
    parser.add_argument("--model-config", type=Path, default=ROOT / "configs/default.json")
    parser.add_argument("--marking-compensation", action=argparse.BooleanOptionalAction, default=True,
                        help="獨立停止線補償層；--no-marking-compensation 恢復原始標線輸入")
    parser.add_argument("--marking-compensation-config", type=Path,
                        default=ROOT / "configs/marking_compensation.json")
    parser.add_argument("--vehicle-conf", type=float, default=None)
    parser.add_argument("--light-conf", type=float, default=None)
    parser.add_argument("--marking-min-pixels", type=int, default=None)
    parser.add_argument("--tracker", type=Path, default=None,
                        help="追蹤器設定（預設 ByteTrack）")
    parser.add_argument("--max-frames", type=int, default=0)
    parser.add_argument("--save-masks", type=Path, default=None,
                        help="除錯用：將每幀標線遮罩存成 PNG（可離線重播完整後處理）")
    parser.add_argument("--save-video", action="store_true",
                        help="生成疊加模型標記的 MP4；預設只輸出資料")
    parser.add_argument("--rules", "--rule", default="",
                        help="逗號分隔的違規規則；all 啟用所有已註冊規則（含未來新增規則）")
    parser.add_argument("--crossing-direction", choices=["away", "toward"], default="away")
    parser.add_argument("--double-line-config", type=Path, default=ROOT / "configs/double_line_rules.json",
                        help="雙實線跨越規則參數 JSON；辨識與重算共用")
    add_clip_arguments(parser)
    return parser.parse_args(argv)


def main():
    with ExitStack() as resources:
        run(parse_args(), resources)


def resolve_device(requested):
    if requested != "auto":
        return requested
    try:
        import torch  # 只有自動選擇裝置時才需要
    except ImportError:
        return "cpu"
    return "cuda:0" if torch.cuda.is_available() else "cpu"


def build_rule_options(args, rule_names):
    """回傳 (規則參數, 區域幾何設定或 None)。"""
    rule_options = {"red_light_stop_line_crossing": {
        "crossing_direction": getattr(args, "crossing_direction", "away")}}
    area_config = None
    if "all" in rule_names or any(name in AREA_RULES for name in rule_names):
        rule_options.update(area_rule_options(getattr(args, "area_rule_config", ROOT / "configs/area_rules.json")))
        area_config = area_geometry_options(args.area_rule_config)
    if any(name == "all" or name.startswith("double_") for name in rule_names):
        rule_options.update(double_line_rule_options(args.double_line_config))
    return rule_options, area_config


def apply_overrides(config, args):
    for stage, key, value in (("vehicles", "confidence", args.vehicle_conf),
                              ("traffic_lights", "confidence", args.light_conf),
                              ("road_markings", "min_pixels", args.marking_min_pixels),
                              ("vehicles", "tracker", str(args.tracker.resolve()) if args.tracker else None)):
        if value is not None and stage in config["models"]:
            config["models"][stage].setdefault("params", {})[key] = value


def run(args, resources, pipeline=None):
    """pipeline：可傳入已載入的 ModelPipeline 供多支影片共用（批次評估）；會先 reset 追蹤狀態。"""
    validate_clip_options(args.clip_before, args.clip_after)
    if not args.input.is_file():
        raise SystemExit(f"找不到影片：{args.input}")
    if args.max_frames < 0:
        raise SystemExit("--max-frames 不可為負數")
    config = load_config(args.model_config)
    rule_names = [name.strip() for name in args.rules.split(",") if name.strip()]
    rule_options, area_config = build_rule_options(args, rule_names)
    # 補償層獨立於模型工廠：更換標線模型不需更改此接口；停用時完全旁路。
    # 鏡頭運動是獨立階段：停用停止線補償時仍估計，雙線與區域規則照常運作。
    motion_config = CompensationConfig.from_json(args.marking_compensation_config)
    compensator = None
    if args.marking_compensation and config["models"].get("road_markings", {}).get("enabled", True) and "road_markings" in config["models"]:
        compensator = RoadMarkingCompensator(motion_config)
    apply_overrides(config, args)
    device = resolve_device(args.device)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = args.input.stem
    video_path = args.output_dir / f"{stem}_annotated.mp4"
    jsonl_path = args.output_dir / f"{stem}_detections.jsonl"
    csv_path = args.output_dir / f"{stem}_detections.csv"

    if pipeline is None:
        pipeline = resources.enter_context(ModelPipeline(config, device))
    else:
        pipeline.reset()
    cap = cv2.VideoCapture(str(args.input))
    resources.callback(cap.release)
    if not cap.isOpened():
        raise SystemExit(f"無法開啟影片：{args.input}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    width, height = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    processor = FrameProcessor(fps, width, height, list(pipeline.models), compensator, area_config,
                               EgoMotionTracker(motion_config))
    resources.callback(processor.close)
    pending_motion = {}

    def start_motion_after_vehicles(stage, partial):
        # 鏡頭運動只需要畫面與車框（road_mask 只讀 bbox_xyxy），車輛偵測完成即可並行估計。
        if stage == "vehicles":
            pending_motion["future"] = processor.start_motion(
                pending_motion["frame"], pending_motion["index"], pending_motion["time"],
                partial["vehicles"].records)
    pipeline.stage_callback = start_motion_after_vehicles
    writer = None
    if args.save_video:
        writer = cv2.VideoWriter(str(video_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
        resources.callback(writer.release)
        if not writer.isOpened():
            raise SystemExit(f"無法建立輸出影片：{video_path}")
    context = RuleContext(args.input.stem, fps, width, height)
    violation_engine = ViolationEngine(create_rules(rule_names, context, rule_options))
    violations_path = args.output_dir / f"{stem}_violations.jsonl"
    violations_file = resources.enter_context(violations_path.open("w", encoding="utf-8")) if rule_names else None
    diagnostics_path = args.output_dir / f"{stem}_rule_diagnostics.jsonl"
    diagnostics_file = resources.enter_context(diagnostics_path.open("w", encoding="utf-8")) if rule_names else None
    recent_events, all_events = [], []
    detection_csv = DetectionCsv(resources.enter_context(csv_path.open("w", newline="", encoding="utf-8-sig")))
    timer = processor.timer  # 主迴圈與逐幀後處理共用，輸出到 *_timings.json
    started, frame_index = time.perf_counter(), 0
    window_started = started
    try:
        with jsonl_path.open("w", encoding="utf-8") as jsonl:
            while True:
                with timer.measure("decode"):
                    ok, frame = cap.read()
                if not ok or (args.max_frames and frame_index >= args.max_frames):
                    break
                timestamp = frame_index / fps
                pending_motion.update(frame=frame, index=frame_index, time=timestamp, future=None)
                with timer.measure("models"):
                    results = pipeline.infer(frame, FrameContext(frame_index, timestamp))
                result = processor.process(frame, frame_index, timestamp, results,
                                           motion_future=pending_motion["future"])
                if args.save_masks is not None:
                    with timer.measure("save_masks"):
                        # 存處理後的遮罩：標線降頻時為依鏡頭運動推移到當幀的遮罩，離線重播才會一致。
                        mask = result.marking_result.artifacts.get("mask")
                        if mask is not None:
                            args.save_masks.mkdir(parents=True, exist_ok=True)
                            cv2.imwrite(str(args.save_masks / f"{frame_index:06d}.png"), mask)
                record = result.record
                with timer.measure("write_jsonl"):
                    jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
                rules_started = time.perf_counter()
                if violations_file:
                    frame_events = violation_engine.evaluate(record)
                    recent_events = [event for event in recent_events if
                                     timestamp-event.get("released_timestamp_sec", event.get(
                                         "confirmation_timestamp_sec", event["timestamp_sec"])) <= 1.0]
                    recent_events.extend(frame_events)
                    all_events.extend(frame_events)
                    for event in frame_events:
                        violations_file.write(json.dumps(event, ensure_ascii=False) + "\n")
                    diagnostics_file.write(json.dumps({"frame":frame_index,
                        "rules":{rule.rule_id:getattr(rule,"diagnostics",{}) for rule in violation_engine.rules}},
                        ensure_ascii=False)+"\n")
                timer.seconds["rules"] = timer.seconds.get("rules", 0.0) + time.perf_counter() - rules_started
                with timer.measure("write_csv"):
                    detection_csv.write(frame_index, timestamp, result)
                if writer:
                    with timer.measure("annotate_video"):
                        writer.write(annotate_frame(frame, result, recent_events, timestamp, area_config is not None))
                frame_index += 1
                if frame_index % 10 == 0:
                    now = time.perf_counter()
                    rate = frame_index / (now - started)
                    recent_rate = 10 / (now - window_started)
                    print(f"已處理 {frame_index} 幀，累計 {rate:.2f} FPS（含首次暖機），"
                          f"最近 10 幀 {recent_rate:.2f} FPS", flush=True)
                    window_started = now
    finally:
        if violations_file:
            for event in violation_engine.finalize():
                all_events.append(event)
                violations_file.write(json.dumps(event, ensure_ascii=False) + "\n")
            violations_file.flush()
    timings = {"frames": frame_index, "wall_seconds": round(time.perf_counter() - started, 3),
               "ms_per_frame": timer.summary(frame_index)}
    (args.output_dir / f"{stem}_timings.json").write_text(json.dumps(timings, indent=2), encoding="utf-8")
    if rule_names and args.violation_clips:
        print("正在剪輯違規片段…", flush=True)
        clips = export_violation_clips(args.input, jsonl_path, all_events,
                                      args.output_dir / f"{stem}_violation_clips",
                                      args.clip_before, args.clip_after)
        print(f"已輸出 {len(clips)} 個違規片段")
    if writer:
        print(f"完成：{video_path}")
    print(f"JSONL：{jsonl_path}")
    print(f"CSV：{csv_path}")
    if rule_names:
        print(f"違規候選事件：{violations_path}")
        print(f"逐幀規則診斷：{diagnostics_path}")


if __name__ == "__main__":
    main()
