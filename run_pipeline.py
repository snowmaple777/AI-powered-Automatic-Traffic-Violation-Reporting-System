import argparse
import csv
import json
import time
from contextlib import ExitStack
from pathlib import Path

import cv2
import torch

from model_library.legacy import ROOT, draw_boxes
from model_library import FrameContext, ModelPipeline, load_config
from model_library.render import overlay_markings
from observation_schema import build_frame_observation
from violation_engine import RuleContext, ViolationEngine, create_rules
from violation_visualization import draw_violation_candidates
from violation_clips import add_clip_arguments, validate_clip_options, export_violation_clips
from road_compensation import CompensationConfig, RoadMarkingCompensator, attach_compensation
from road_compensation.render import draw_compensation
from rider_tracking import RiderTracker, attach_riders, draw_rider_groups
from double_lines import attach_double_lines, draw_double_lines, double_line_rule_options


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="可替換模型的交通影像辨識")
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


def run(args, resources):
    validate_clip_options(args.clip_before, args.clip_after)
    if not args.input.is_file():
        raise SystemExit(f"找不到影片：{args.input}")
    if args.max_frames < 0:
        raise SystemExit("--max-frames 不可為負數")
    config = load_config(args.model_config)
    rule_names = [name.strip() for name in args.rules.split(",") if name.strip()]
    rule_options = {"red_light_stop_line_crossing": {
        "crossing_direction": getattr(args, "crossing_direction", "away")}}
    if any(name == "all" or name.startswith("double_") for name in rule_names):
        rule_options.update(double_line_rule_options(args.double_line_config))
    # 補償層獨立於模型工廠：更換標線模型不需更改此接口；停用時完全旁路。
    compensator = None
    if args.marking_compensation and config["models"].get("road_markings", {}).get("enabled", True) and "road_markings" in config["models"]:
        compensator = RoadMarkingCompensator(CompensationConfig.from_json(args.marking_compensation_config))
    for stage, key, value in (("vehicles", "confidence", args.vehicle_conf),
                              ("traffic_lights", "confidence", args.light_conf),
                              ("road_markings", "min_pixels", args.marking_min_pixels),
                              ("vehicles", "tracker", str(args.tracker.resolve()) if args.tracker else None)):
        if value is not None and stage in config["models"]:
            config["models"][stage].setdefault("params", {})[key] = value
    device = ("cuda:0" if torch.cuda.is_available() else "cpu") if args.device == "auto" else args.device
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = args.input.stem
    video_path = args.output_dir / f"{stem}_annotated.mp4"
    jsonl_path = args.output_dir / f"{stem}_detections.jsonl"
    csv_path = args.output_dir / f"{stem}_detections.csv"

    pipeline = resources.enter_context(ModelPipeline(config, device))
    cap = cv2.VideoCapture(str(args.input))
    resources.callback(cap.release)
    if not cap.isOpened():
        raise SystemExit(f"無法開啟影片：{args.input}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    width, height = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    rider_tracker = RiderTracker(fps)
    double_line_state = {}
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
    csv_file = resources.enter_context(csv_path.open("w", newline="", encoding="utf-8-sig"))
    csv_writer = csv.DictWriter(csv_file, fieldnames=["frame", "timestamp_sec", "model", "class_id",
        "class_name", "confidence", "track_id", "object_id", "track_age_frames",
        "center", "bottom_center", "geometry", "distance_m", "plate_text",
        "raw_text", "text_confidence"])
    csv_writer.writeheader()
    started, frame_index = time.perf_counter(), 0
    window_started = started
    try:
        with jsonl_path.open("w", encoding="utf-8") as jsonl:
            while True:
                ok, frame = cap.read()
                if not ok or (args.max_frames and frame_index >= args.max_frames):
                    break
                timestamp = frame_index / fps
                results = pipeline.infer(frame, FrameContext(frame_index, timestamp))
                vehicles = [dict(item) for item in results["vehicles"].records]
                traffic_lights = results["traffic_lights"].records
                road_markings = results["road_markings"].records
                plates = results["ocr"].records if "ocr" in pipeline.models else results["plates"].records
                distances = {item["object_id"]: item for item in results["depth"].records if item.get("object_id")}
                plate_map = {item["object_id"]: item for item in plates if item.get("object_id")}
                for vehicle in vehicles:
                    object_id = vehicle.get("object_id")
                    vehicle["distance_m"] = distances.get(object_id, {}).get("distance_m")
                    vehicle["plate_text"] = (plate_map.get(object_id, {}).get("text") or
                        results["ocr"].artifacts.get("vehicle_plate_map", {}).get(object_id))
                    vehicle["text_confidence"] = results["ocr"].artifacts.get(
                        "vehicle_plate_confidence_map", {}).get(object_id)
                record = build_frame_observation(
                    frame_index, timestamp, width, height, fps,
                    vehicles, traffic_lights, road_markings,
                    results["depth"].records, plates, list(pipeline.models))
                compensation = None
                if compensator is not None:
                    compensation = compensator.process(
                        frame, frame_index=frame_index, timestamp_sec=timestamp,
                        road_markings=road_markings, vehicles=vehicles)
                    # 原始／補償結果均保存；引擎既有 road_markings 入口只接收通過品質閘門者。
                    record = attach_compensation(record, compensation)
                attach_riders(record, rider_tracker)
                attach_double_lines(record, results["road_markings"].artifacts.get("mask"),
                                    double_line_state)
                jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
                if violations_file:
                    frame_events = violation_engine.evaluate(record)
                    recent_events = [event for event in recent_events if
                                     timestamp-event.get("confirmation_timestamp_sec", event["timestamp_sec"]) <= 1.0]
                    recent_events.extend(frame_events)
                    all_events.extend(frame_events)
                    for event in frame_events:
                        violations_file.write(json.dumps(event, ensure_ascii=False) + "\n")
                    diagnostics_file.write(json.dumps({"frame":frame_index,
                        "rules":{rule.rule_id:getattr(rule,"diagnostics",{}) for rule in violation_engine.rules}},
                        ensure_ascii=False)+"\n")
                for model_name, items in (("vehicle", vehicles), ("traffic_light", traffic_lights), ("plate", plates)):
                    for item in items:
                        csv_writer.writerow({"frame": frame_index, "timestamp_sec": round(timestamp, 3),
                            "model": model_name, "class_id": item["class_id"], "class_name": item["class_name"],
                            "confidence": item["confidence"], "track_id": item["track_id"],
                            "object_id": item.get("object_id", ""),
                            "track_age_frames": item.get("track_age_frames", ""),
                            "center": json.dumps(item["center"]),
                            "bottom_center": json.dumps(item["bottom_center"]),
                            "distance_m": item.get("distance_m"),
                            "plate_text": item.get("text", item.get("plate_text")),
                            "raw_text": item.get("raw_text"),
                            "text_confidence": item.get("text_confidence"),
                            "geometry": json.dumps(item["bbox_xyxy"])})
                for item in road_markings:
                    csv_writer.writerow({"frame": frame_index, "timestamp_sec": round(timestamp, 3),
                        "model": "road_marking", "class_id": item["class_id"], "class_name": item["class_name"],
                        "confidence": "", "track_id": "", "object_id": "", "track_age_frames": "",
                        "center": "", "bottom_center": "", "geometry": json.dumps(item["components"])})
                for line in record["observations"]["double_lines"]:
                    csv_writer.writerow({"frame": frame_index, "timestamp_sec": round(timestamp, 3),
                        "model": "double_line", "class_id": line["class_id"],
                        "class_name": line["class_name"], "track_id": line["track_id"],
                        "track_age_frames": line["observed_frames"], "geometry": json.dumps(line)})
                if compensation is not None:
                    for item in compensation.compensated_markings:
                        if any("compensation" in c for c in item["components"]):
                            csv_writer.writerow({"frame":frame_index,"timestamp_sec":round(timestamp,3),
                                "model":"road_marking_compensated","class_id":item["class_id"],
                                "class_name":item["class_name"],"geometry":json.dumps(item["components"])})
                if writer:
                    annotated = overlay_markings(frame, results["road_markings"])
                    grouped_members = {member for group in record['observations']['rider_groups']
                                       if group['status']=='confirmed'
                                       for member in (group['rider_id'], group['vehicle_id'])}
                    draw_boxes(annotated, [v for v in vehicles if v.get('object_id') not in grouped_members],
                               (255, 180, 0), "vehicle:")
                    draw_boxes(annotated, traffic_lights, (0, 255, 255), "light:")
                    draw_boxes(annotated, plates, (0, 0, 255), "plate:")
                    if compensation is not None:
                        draw_compensation(annotated, compensation)
                    draw_rider_groups(annotated, record['observations']['rider_groups'])
                    draw_double_lines(annotated, record['observations']['double_lines'])
                    draw_violation_candidates(annotated, record['observations']['rule_vehicles'], recent_events, timestamp)
                    writer.write(annotated)
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
