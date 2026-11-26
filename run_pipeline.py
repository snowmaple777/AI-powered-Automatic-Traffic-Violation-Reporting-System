import argparse
import json
import math
import time
from contextlib import ExitStack
from pathlib import Path

import cv2

from model_library.detectors import ROOT
from model_library import FrameContext, ModelPipeline, load_config
from violation_engine import RuleContext, ViolationEngine, create_rules, red_light_rule_options
from violation_clips import add_clip_arguments, validate_clip_options, export_violation_clips
from road_compensation import CompensationConfig, EgoMotionTracker, RoadMarkingCompensator
from double_lines import double_line_geometry_options, double_line_rule_options, write_final_double_line_diagnostics
from area_rules import AREA_RULES, area_rule_options, area_geometry_options
from frame_processing import FrameProcessor
from pipeline_outputs import DetectionCsv, annotate_frame
from plate_backfill import PlateBackfill, add_plate_arguments, CLI_WINDOW_SECONDS
from motion_notes import MotionNotes, add_motion_arguments, write_motion_summary
from local_lanes import draw_local_motion
from stream_source import SegmentedCapture, SegmentedFrames, StreamProcess, input_source, is_stream_url, stream_name
from stream_triage import HOLD_SECONDS, TRIAGE_SKIP_STAGES, DeepWindows, held_events
from core.timing import StageTimer
from rule_profiles import add_profile_argument, apply_profile


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="可替換模型的交通影像辨識")
    parser.add_argument("--area-rule-config", type=Path, default=ROOT / "configs/area_rules.json",
                        help="停等區與行人穿越道候選規則參數")
    parser.add_argument("--red-light-config", type=Path, default=ROOT / "configs/red_light_rules.json",
                        help="闖紅燈規則設定（延後確認選項，4av）")
    parser.add_argument("input", type=input_source,
                        help="輸入影片，或串流網址（rtsp://、http(s)://、udp:// 等；以串流模式處理）")
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
    parser.add_argument("--allow-short-decode", action="store_true",
                        help="可解碼幀數少於中繼資料時不中止，以實際解碼範圍完成並記錄為 short_eof（預設關閉）")
    parser.add_argument("--save-masks", type=Path, default=None,
                        help="除錯用：將每幀標線遮罩存成 PNG（可離線重播完整後處理）")
    parser.add_argument("--save-video", action="store_true",
                        help="生成疊加模型標記的 MP4；預設只輸出資料")
    parser.add_argument("--rules", "--rule", default="",
                        help="逗號分隔的違規規則；all 啟用所有已註冊規則（含未來新增規則）")
    parser.add_argument("--crossing-direction", choices=["away", "toward"], default="away")
    parser.add_argument("--double-line-config", type=Path, default=ROOT / "configs/double_line_rules.json",
                        help="雙實線跨越規則參數 JSON；辨識與重算共用。"
                             "其中的 geometry 子物件是雙線幾何選項，不論是否啟用雙線規則都會套用")
    stream = parser.add_argument_group("串流模式（4bq）：邊收邊以固定幀率錄成分段檔，分析與剪片都讀分段")
    stream.add_argument("--stream-input", action="store_true",
                        help="本機影片也用串流模式處理（可變幀率影片、或測試串流流程）；網址輸入自動啟用")
    stream.add_argument("--stream-fps", type=float, default=None, help="固定幀率；預設沿用來源標稱幀率")
    stream.add_argument("--stream-segment-seconds", type=float, default=2.0, help="每個分段的秒數，預設 2")
    stream.add_argument("--stream-codec", choices=["FFV1", "MJPG", "mp4v"], default="FFV1",
                        help="分段編碼：FFV1（預設，無損，分析結果與直接讀檔相同，檔案大）；MJPG／mp4v 較小但有損，會改變偵測結果")
    stream.add_argument("--stream-max-seconds", type=float, default=0.0,
                        help="收錄幾秒後停止（0＝直到來源結束；直播來源可用 Ctrl+C 結束）")
    stream.add_argument("--stream-name", default=None, help="串流的輸出檔名；預設由網址產生")
    stream.add_argument("--stream-stop-file", type=Path, default=None,
                        help="這個檔案出現就停止收錄，已收錄的部分照常分析完（推論服務的「停止串流」）")
    stream.add_argument("--stream-triage-fps", "--triage-fps", dest="stream_triage_fps", type=float, default=0.0,
                        help="降頻篩選（4bs；4bu 起檔案輸入也可用，長片切段）：以約這個幀率分析找候選事件，"
                             "事件前後時段再以原幀率深度分析；0＝停用（每幀分析，預設）。建議 10～15（低於 6 時規則幾乎不出事件）。"
                             "需要 --rules")
    stream.add_argument("--stream-triage-before", type=float, default=8.0, help="深度分析從觸發前幾秒開始，預設 8")
    stream.add_argument("--stream-triage-after", type=float, default=6.0, help="深度分析到觸發後幾秒，預設 6")
    stream.add_argument("--stream-triage-max-before", type=float, default=20.0,
                        help="觸發車輛較早出現時，深度分析從它首次出現的前 1 秒開始，但最多從觸發前幾秒開始，預設 20（4bt）")
    stream.add_argument("--stream-triage-all-models", action="store_true",
                        help="篩選也跑深度、車牌、OCR 模型（預設略過；這些模型不影響規則觸發，4bt）")
    add_clip_arguments(parser)
    add_plate_arguments(parser)
    add_motion_arguments(parser)
    add_profile_argument(parser)
    return apply_profile(parser.parse_args(argv))


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
        "crossing_direction": getattr(args, "crossing_direction", "away"),
        **red_light_rule_options(getattr(args, "red_light_config", ROOT / "configs/red_light_rules.json"))}}
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
    plate_backfill = resources.enter_context(PlateBackfill(getattr(args, "plate_backfill_seconds", CLI_WINDOW_SECONDS)))
    streaming = is_stream_url(args.input) or getattr(args, "stream_input", False)
    if not is_stream_url(args.input) and not Path(args.input).is_file():
        raise SystemExit(f"找不到影片：{args.input}")
    if args.max_frames < 0:
        raise SystemExit("--max-frames 不可為負數")
    triage_target = getattr(args, "stream_triage_fps", 0.0) or 0.0
    if not math.isfinite(triage_target) or triage_target < 0:
        raise SystemExit("--stream-triage-fps 不可為負數")
    if triage_target:
        if not args.rules.strip():
            raise SystemExit("--stream-triage-fps 需要 --rules（以規則的候選事件決定深度分析的時段）")
        if args.save_video or args.save_masks is not None or getattr(args, "motion_notes", False):
            raise SystemExit("--stream-triage-fps 不支援 --save-video、--save-masks、--motion-notes")
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
    stem = getattr(args, "stream_name", None) or stream_name(args.input)
    motion_notes = resources.enter_context(MotionNotes(getattr(args, "motion_notes", False),
        args.output_dir / f"{stem}_local_motion.jsonl"))
    video_path = args.output_dir / f"{stem}_annotated.mp4"
    jsonl_path = args.output_dir / f"{stem}_detections.jsonl"
    csv_path = args.output_dir / f"{stem}_detections.csv"

    if pipeline is None:
        pipeline = resources.enter_context(ModelPipeline(config, device))
    else:
        pipeline.reset()
    recorder = None
    clip_source = args.input
    if streaming:
        # 串流：獨立程序收錄成固定幀率分段，分析依序讀分段；總幀數未知，時間軸仍是幀號 ÷ fps。
        recorder = StreamProcess(str(args.input), args.output_dir / f"{stem}_stream", fps=args.stream_fps,
                                  segment_seconds=args.stream_segment_seconds, codec=args.stream_codec,
                                  max_seconds=args.stream_max_seconds,
                                  stop_files=[getattr(args, "stream_stop_file", None)])
        recorder.start()
        resources.callback(recorder.stop)
        try:
            recorder.wait_ready()
        except RuntimeError as e:
            raise SystemExit(str(e)) from None
        cap = SegmentedFrames(recorder)
        resources.callback(cap.release)
        fps, width, height = recorder.fps, recorder.width, recorder.height
        expected_frames = None
        clip_source = recorder.folder
        print(f"串流收錄：{recorder.folder}（{width}x{height}，固定 {fps:g} FPS，來源 {recorder.source_fps:g} FPS）",
              flush=True)
    else:
        cap = cv2.VideoCapture(str(args.input))
        resources.callback(cap.release)
        if not cap.isOpened():
            raise SystemExit(f"無法開啟影片：{args.input}")
        fps = cap.get(cv2.CAP_PROP_FPS)
        width, height = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if not math.isfinite(fps) or fps <= 0 or width <= 0 or height <= 0:
            raise ValueError("影片 FPS 或尺寸無效，無法建立可靠時間軸")
        reported_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        expected_frames = round(reported_frames) if math.isfinite(reported_frames) and reported_frames > 0 else None
    if triage_target:
        stride = round(fps / triage_target)  # 每 stride 幀取 1 幀；整數間隔讓篩選的時間軸固定
        if stride >= 2:
            return _run_triage(args, resources, pipeline, recorder, cap, stem, rule_names, rule_options, area_config,
                               motion_config, compensator is not None, plate_backfill.seconds, stride,
                               (fps, width, height), expected_frames, clip_source)
        print(f"{'串流' if streaming else '影片'}只有 {fps:g} FPS，不到篩選幀率 {triage_target:g} 的兩倍，不降頻、每幀分析",
              flush=True)
    processor = FrameProcessor(fps, width, height, list(pipeline.models), compensator, area_config,
                               EgoMotionTracker(motion_config),
                               double_line_geometry_options(args.double_line_config))
    resources.callback(processor.close)
    pending_motion = {}

    def start_motion_after_vehicles(stage, partial):
        # 鏡頭運動只需要畫面與車框（road_mask 只讀 bbox_xyxy），車輛偵測完成即可並行估計。
        if stage == "vehicles":
            pending_motion["future"] = processor.start_motion(
                pending_motion["frame"], pending_motion["index"], pending_motion["time"],
                partial["vehicles"].records)
    pipeline.stage_callback = start_motion_after_vehicles

    def probe_motion():
        # 讓模型（標線 refresh_on_invalid_motion）查詢本幀鏡頭運動：阻塞到背景估計完成，不讀未完成的結果。
        future = pending_motion.get("future")
        if future is None:
            return None
        (motion, _), _ = future.result()
        return motion
    writer = None
    if args.save_video:
        writer = cv2.VideoWriter(str(video_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
        resources.callback(writer.release)
        if not writer.isOpened():
            raise SystemExit(f"無法建立輸出影片：{video_path}")
    context = RuleContext(stem, fps, width, height)
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
    termination = None
    masks_written = 0
    try:
        with jsonl_path.open("w", encoding="utf-8") as jsonl:
            while True:
                if args.max_frames and frame_index >= args.max_frames:
                    termination = "max_frames"
                    break
                with timer.measure("decode"):
                    ok, frame = cap.read()
                if not ok:
                    if frame_index == 0:
                        raise RuntimeError("影片未成功解碼任何影格")
                    if recorder is not None:
                        termination = "stream_end"
                        break
                    if expected_frames is not None and frame_index != expected_frames:
                        # 只容許「少於」中繼資料；多讀到的幀仍視為中繼資料錯誤。
                        if not (getattr(args, "allow_short_decode", False) and frame_index < expected_frames):
                            raise RuntimeError(f"影片可能截斷或影格中繼資料不符：讀到 {frame_index} / {expected_frames} 幀")
                        termination = "short_eof"
                        break
                    termination = "eof" if expected_frames is not None else "unverified_eof"
                    break
                timestamp = frame_index / fps
                pending_motion.update(frame=frame, index=frame_index, time=timestamp, future=None)
                with timer.measure("models"):
                    results = pipeline.infer(frame, FrameContext(frame_index, timestamp, motion_probe=probe_motion))
                result = processor.process(frame, frame_index, timestamp, results,
                                           motion_future=pending_motion["future"])
                if args.save_masks is not None:
                    with timer.measure("save_masks"):
                        # 存處理後的遮罩：標線降頻時為依鏡頭運動推移到當幀的遮罩，離線重播才會一致。
                        mask = result.marking_result.artifacts.get("mask")
                        if mask is not None:
                            args.save_masks.mkdir(parents=True, exist_ok=True)
                            mask_path = args.save_masks / f"{frame_index:06d}.png"
                            ok, buf = cv2.imencode(".png", mask)  # 非 ASCII 路徑：先編碼再寫檔
                            if not ok:
                                raise OSError(f"遮罩寫入失敗：{mask_path}")
                            mask_path.write_bytes(buf.tobytes())
                            masks_written += 1
                record = result.record
                if motion_notes.enabled:
                    with timer.measure("local_motion"):
                        motion_notes.observe(record, result.marking_result.artifacts.get("mask"))
                with timer.measure("write_jsonl"):
                    jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
                rules_started = time.perf_counter()
                if violations_file:
                    frame_events = violation_engine.evaluate(record)
                    recent_events = [event for event in recent_events if
                                     timestamp-event.get("released_timestamp_sec", event.get(
                                         "confirmation_timestamp_sec", event["timestamp_sec"])) <= 1.0]
                    recent_events.extend(frame_events)
                    completed_events = motion_notes.submit(plate_backfill.update(record, frame_events))
                    all_events.extend(completed_events)
                    for event in completed_events:
                        violations_file.write(json.dumps(event, ensure_ascii=False) + "\n")
                    diagnostics_file.write(json.dumps({"frame":frame_index,
                        "rules":{rule.rule_id:getattr(rule,"diagnostics",{}) for rule in violation_engine.rules}},
                        ensure_ascii=False)+"\n")
                timer.seconds["rules"] = timer.seconds.get("rules", 0.0) + time.perf_counter() - rules_started
                with timer.measure("write_csv"):
                    detection_csv.write(frame_index, timestamp, result)
                if writer:
                    with timer.measure("annotate_video"):
                        annotated = annotate_frame(frame, result, recent_events, timestamp, area_config is not None)
                        if motion_notes.enabled:
                            draw_local_motion(annotated, motion_notes.diagnostic)
                        writer.write(annotated)
                frame_index += 1
                if frame_index % 10 == 0:
                    now = time.perf_counter()
                    rate = frame_index / (now - started)
                    recent_rate = 10 / (now - window_started)
                    print(f"已處理 {frame_index} 幀，累計 {rate:.2f} FPS（含首次暖機），"
                          f"最近 10 幀 {recent_rate:.2f} FPS", flush=True)
                    window_started = now
    except KeyboardInterrupt:
        # 串流（直播）以 Ctrl+C 結束：停在目前影格，照常收尾、寫計時與剪片；影片檔仍照原本中止。
        if recorder is None:
            raise
        termination = "interrupted"
        print(f"已中止串流分析（第 {frame_index} 幀），整理已分析的部分…", flush=True)
    finally:
        if violations_file:
            for event in motion_notes.submit(plate_backfill.finalize(violation_engine.finalize()), final=True):
                all_events.append(event)
                violations_file.write(json.dumps(event, ensure_ascii=False) + "\n")
            violations_file.flush()
            write_final_double_line_diagnostics(diagnostics_file, violation_engine.rules)
    if motion_notes.enabled:
        write_motion_summary(args.output_dir / f"{stem}_motion_notes.txt", all_events)
    timings = {"frames": frame_index, "wall_seconds": round(time.perf_counter() - started, 3),
               "ms_per_frame": timer.summary(frame_index),
               "plate_backfill_seconds": plate_backfill.seconds,
               "completion": {"status": "complete" if termination in ("eof", "short_eof")
                              else "stream" if termination == "stream_end" else "partial",
                              "termination": termination, "expected_frames": expected_frames,
                              "max_frames": args.max_frames, "masks_written": masks_written}}
    if recorder is not None:
        recorder.stop()  # --max-frames 提前結束分析時也停止收錄，統計才是最終值
        timings["stream"] = recorder.stats()
    if motion_notes.enabled:
        timings["motion_notes"] = {"enabled": True, "look_after_seconds": 3.0}
    marking_model = pipeline.models.get("road_markings")
    if getattr(marking_model, "schedule_enabled", False):
        timings["road_marking_schedule"] = dict(
            marking_model.schedule_diagnostics,
            motion_wait_seconds=round(marking_model.schedule_diagnostics["motion_wait_seconds"], 3))
    (args.output_dir / f"{stem}_timings.json").write_text(json.dumps(timings, indent=2), encoding="utf-8")
    if rule_names and args.violation_clips:
        print("正在剪輯違規片段…", flush=True)
        clips = export_violation_clips(clip_source, jsonl_path, all_events,
                                      args.output_dir / f"{stem}_violation_clips",
                                      args.clip_before, args.clip_after, raw=getattr(args, "clip_raw", False))
        print(f"已輸出 {len(clips)} 個違規片段")
    if writer:
        print(f"完成：{video_path}")
    print(f"JSONL：{jsonl_path}")
    print(f"CSV：{csv_path}")
    if rule_names:
        print(f"違規候選事件：{violations_path}")
        print(f"逐幀規則診斷：{diagnostics_path}")
    return timings


class _FrameAnalyzer:
    """一組逐幀分析狀態（後處理、規則、鏡頭運動並行），模型共用。降頻篩選與每段深度分析各建一組（4bs）。
    與 run() 主迴圈的逐幀步驟相同：車輛偵測後在背景估計鏡頭運動，標線模型可查詢本幀運動。"""

    def __init__(self, pipeline, fps, width, height, stem, rule_names, rule_options, area_config, motion_config,
                 double_line_config, use_compensator, timer, skip=()):
        self.pipeline, self.skip = pipeline, tuple(skip)
        self.processor = FrameProcessor(fps, width, height, list(pipeline.models),
                                        RoadMarkingCompensator(motion_config) if use_compensator else None,
                                        area_config, EgoMotionTracker(motion_config),
                                        double_line_geometry_options(double_line_config))
        self.processor.timer = self.timer = timer
        self.engine = ViolationEngine(create_rules(rule_names, RuleContext(stem, fps, width, height), rule_options))
        self.pending = {}

    def _after_stage(self, stage, partial):
        if stage == "vehicles":
            p = self.pending
            p["future"] = self.processor.start_motion(p["frame"], p["index"], p["time"], partial["vehicles"].records)

    def _probe(self):
        future = self.pending.get("future")
        if future is None:
            return None
        (motion, _), _ = future.result()
        return motion

    def step(self, frame, index, timestamp):
        self.pending.update(frame=frame, index=index, time=timestamp, future=None)
        self.pipeline.stage_callback = self._after_stage
        with self.timer.measure("models"):
            context = FrameContext(index, timestamp, motion_probe=self._probe)
            results = (self.pipeline.infer(frame, context, skip=self.skip) if self.skip
                       else self.pipeline.infer(frame, context))
        result = self.processor.process(frame, index, timestamp, results, motion_future=self.pending["future"])
        started = time.perf_counter()
        events = self.engine.evaluate(result.record)
        self.timer.seconds["rules"] = self.timer.seconds.get("rules", 0.0) + time.perf_counter() - started
        return result, events

    def close(self):
        self.processor.close()


class _ForwardReader:
    """檔案輸入的深度分析（4bu）：另開一個讀取器，只往前讀、以 grab 略過不需要的幀，不依賴編碼器的定位精度。
    深度分析的時段依序且不重疊，所以整支影片最多多解碼一次。"""

    def __init__(self, path):
        self.cap, self.position = cv2.VideoCapture(str(path)), 0
        if not self.cap.isOpened():
            raise RuntimeError(f"無法開啟影片：{path}")

    def seek(self, index):
        if index < self.position:
            raise RuntimeError(f"深度分析時段倒退：{index} < {self.position}")
        while self.position < index:
            if not self.cap.grab():
                return False
            self.position += 1
        return True

    def read(self):
        ok, frame = self.cap.read()
        if ok:
            self.position += 1
        return ok, frame

    def release(self):
        self.cap.release()


def _run_triage(args, resources, pipeline, recorder, cap, stem, rule_names, rule_options, area_config,
                motion_config, use_compensator, backfill_seconds, stride, video_info, expected_frames, clip_source):
    """降頻篩選（4bs，見 stream_triage.py；4bu 起也用於檔案輸入＝長片切段）。輸出格式與一般模式相同，但逐幀紀錄、
    事件與剪片只涵蓋深度分析的時段；篩選本身的逐幀紀錄與候選另存 *_triage_detections.jsonl、*_triage_violations.jsonl
    （幀號是取樣序號，stream_frame 是原幀號）。串流從收錄分段重讀深度分析的時段，檔案另開一個只往前讀的讀取器。"""
    fps, width, height = video_info
    forward = None if recorder is not None else _ForwardReader(args.input)
    if forward is not None:
        resources.callback(forward.release)
    grab = getattr(cap, "grab", None)  # 不分析的幀只 grab 不轉成影像（分段讀取器沒有 grab，照常 read）
    triage_fps = fps / stride
    out = args.output_dir
    jsonl_path, csv_path = out / f"{stem}_detections.jsonl", out / f"{stem}_detections.csv"
    violations_path, diagnostics_path = out / f"{stem}_violations.jsonl", out / f"{stem}_rule_diagnostics.jsonl"
    triage_jsonl_path, triage_events_path = out / f"{stem}_triage_detections.jsonl", out / f"{stem}_triage_violations.jsonl"
    jsonl = resources.enter_context(jsonl_path.open("w", encoding="utf-8"))
    violations_file = resources.enter_context(violations_path.open("w", encoding="utf-8"))
    diagnostics_file = resources.enter_context(diagnostics_path.open("w", encoding="utf-8"))
    triage_jsonl = resources.enter_context(triage_jsonl_path.open("w", encoding="utf-8"))
    triage_events = resources.enter_context(triage_events_path.open("w", encoding="utf-8"))
    detection_csv = DetectionCsv(resources.enter_context(csv_path.open("w", newline="", encoding="utf-8-sig")))
    triage_timer, deep_timer = StageTimer(), StageTimer()
    common = (width, height, stem, rule_names, rule_options, area_config, motion_config, args.double_line_config,
              use_compensator)
    skip = () if args.stream_triage_all_models else tuple(s for s in TRIAGE_SKIP_STAGES if s in pipeline.models)
    triage = _FrameAnalyzer(pipeline, triage_fps, *common, triage_timer, skip=skip)
    resources.callback(lambda: triage.close())  # 深度分析後會換新的一組，關閉當時的那組
    windows = DeepWindows(fps, args.stream_triage_before, args.stream_triage_after, HOLD_SECONDS,
                          args.stream_triage_max_before)
    all_events, event_ids = [], set()
    first_seen, triggered = {}, set()  # 篩選中各車首次出現的時間；已觸發過的 (rule_id, event_id)
    print(f"降頻篩選：每 {stride} 幀分析 1 幀（{triage_fps:g} FPS），候選事件前 {args.stream_triage_before:g} 秒"
          f"（觸發車輛較早出現時從它出現前 1 秒，最多 {windows.max_before:g} 秒）到後 {args.stream_triage_after:g} 秒"
          f"以原幀率深度分析" + (f"；篩選略過 {'、'.join(skip)}" if skip else ""), flush=True)

    def emit(events, window_no):
        for event in events:
            if event["event_id"] in event_ids:  # 各段的規則狀態是新的，流水號可能重複
                event["event_id"] = f"{event['event_id']}-w{window_no}"
            event_ids.add(event["event_id"])
            all_events.append(event)
            violations_file.write(json.dumps(event, ensure_ascii=False) + "\n")
        violations_file.flush()

    def deep(start, end):
        window_no = len(windows.completed) + 1
        print(f"深度分析第 {window_no} 段：{start / fps:.1f}–{(end + 1) / fps:.1f} 秒（第 {start}–{end} 幀）", flush=True)
        pipeline.reset()  # 每段從乾淨的追蹤與快取開始，與 4bo 的片段重播相同
        analyzer = _FrameAnalyzer(pipeline, fps, *common, deep_timer)
        backfill = PlateBackfill(backfill_seconds)
        source = SegmentedCapture(recorder.folder) if forward is None else forward
        last = start - 1
        try:
            if not (source.set(cv2.CAP_PROP_POS_FRAMES, start) if forward is None else forward.seek(start)):
                raise RuntimeError(f"無法定位影格：{start}")
            for index in range(start, end + 1):
                with deep_timer.measure("decode"):
                    ok, frame = source.read()
                if not ok:
                    raise RuntimeError(f"深度分析讀取失敗：第 {index} 幀")
                timestamp = index / fps
                result, events = analyzer.step(frame, index, timestamp)
                with deep_timer.measure("write_jsonl"):
                    jsonl.write(json.dumps(result.record, ensure_ascii=False) + "\n")
                diagnostics_file.write(json.dumps({"frame": index, "rules": {
                    rule.rule_id: getattr(rule, "diagnostics", {}) for rule in analyzer.engine.rules}},
                    ensure_ascii=False) + "\n")
                with deep_timer.measure("write_csv"):
                    detection_csv.write(index, timestamp, result)
                emit(backfill.update(result.record, events), window_no)
                last = index
            emit(backfill.finalize(analyzer.engine.finalize()), window_no)
            write_final_double_line_diagnostics(diagnostics_file, analyzer.engine.rules)
            jsonl.flush()
        finally:
            if forward is None:
                source.release()
            analyzer.close()
            backfill.close()
            pipeline.reset()  # 篩選的追蹤從新的編號重新開始
            first_seen.clear()
            if last >= start:  # 中途 Ctrl+C 時，已寫出的部分也算分析過，之後的時段不重疊（剪片要求幀號遞增）
                windows.finish(start, last)

    def trigger(events, held=()):
        # 每筆候選只觸發一次；保留中的候選（held）在出現當下就觸發，不必等規則輸出（闖紅燈疑似事件要到片尾）
        wrote = False
        for kind, items in (("event", events), ("held", held)):
            for event in items:
                key = (event.get("rule_id"), event.get("event_id"))
                if key in triggered:
                    continue
                triggered.add(key)
                seen = first_seen.get(event.get("object_id"))
                triage_events.write(json.dumps(dict(event, stream_frame=round(event["timestamp_sec"] * fps),
                                                    triage_trigger=kind, triage_first_seen_sec=seen),
                                               ensure_ascii=False) + "\n")
                windows.add(event["timestamp_sec"], seen)
                wrote = True
        if wrote:
            triage_events.flush()

    started, position, termination = time.perf_counter(), 0, None
    window_started, triage_frames = started, 0
    try:
        while True:
            if args.max_frames and position >= args.max_frames:
                termination = "max_frames"
                break
            with triage_timer.measure("decode"):
                if position % stride == 0 or grab is None:
                    ok, frame = cap.read()
                else:
                    ok, frame = grab(), None
            if not ok:
                if position == 0:
                    raise RuntimeError("影片未成功解碼任何影格")
                if recorder is not None:
                    termination = "stream_end"
                elif expected_frames is not None and position != expected_frames:
                    # 與一般模式相同：只容許「少於」中繼資料（--allow-short-decode）
                    if not (getattr(args, "allow_short_decode", False) and position < expected_frames):
                        raise RuntimeError(f"影片可能截斷或影格中繼資料不符：讀到 {position} / {expected_frames} 幀")
                    termination = "short_eof"
                else:
                    termination = "eof" if expected_frames is not None else "unverified_eof"
                break
            if position % stride == 0:
                result, events = triage.step(frame, triage_frames, position / fps)
                record = result.record
                record["frame"]["stream_frame"] = position
                for vehicle in record.get("observations", {}).get("vehicles") or []:
                    if vehicle.get("object_id"):
                        first_seen.setdefault(vehicle["object_id"], position / fps)
                triage_jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
                trigger(events, held_events(triage.engine))
                triage_frames += 1
                if triage_frames % 10 == 0:
                    now = time.perf_counter()
                    # 推論服務以「已處理 N 幀」計算已分析到串流的哪裡：N 是原幀號，不是取樣數
                    print(f"已處理 {position + 1} 幀，累計 {(position + 1) / (now - started):.2f} FPS（含首次暖機），"
                          f"最近 10 幀 {10 * stride / (now - window_started):.2f} FPS", flush=True)
                    window_started = now
            position += 1
            due = windows.due(position)
            for start, end in due:
                deep(start, end)
                window_started = time.perf_counter()
            if due:
                # 深度分析後追蹤已 reset、車輛編號重新開始，篩選的規則狀態也換新（舊編號的狀態會套到別台車）；
                # 保留中的候選在出現時已觸發過
                triage.close()
                triage = _FrameAnalyzer(pipeline, triage_fps, *common, triage_timer, skip=skip)
    except KeyboardInterrupt:
        termination = "interrupted"
        print(f"已中止分析（第 {position} 幀），深度分析已觸發的時段…", flush=True)
    finally:
        triage_jsonl.flush()
    trigger(triage.engine.finalize())
    for start, end in windows.due(position, final=True):
        deep(start, end)
    if recorder is not None:
        recorder.stop()
    deep_frames = windows.frames()
    timings = {"frames": position, "wall_seconds": round(time.perf_counter() - started, 3),
               "ms_per_frame": deep_timer.summary(deep_frames),
               "plate_backfill_seconds": backfill_seconds,
               "completion": {"status": "complete" if termination in ("eof", "short_eof")
                              else "stream" if termination == "stream_end" else "partial",
                              "termination": termination, "expected_frames": expected_frames,
                              "max_frames": args.max_frames, "masks_written": 0},
               "triage": {"stride": stride, "fps": triage_fps, "frames": triage_frames,
                          "ms_per_frame": triage_timer.summary(triage_frames),
                          "triggers": windows.triggers, "skipped_triggers": windows.skipped,
                          "before_seconds": args.stream_triage_before, "after_seconds": args.stream_triage_after,
                          "max_before_seconds": windows.max_before, "skipped_stages": list(skip),
                          "windows_sec": [[round(a / fps, 3), round((b + 1) / fps, 3)] for a, b in windows.completed],
                          "deep_frames": deep_frames, "deep_share": round(deep_frames / max(1, position), 4)}}
    if recorder is not None:
        timings["stream"] = recorder.stats()
    (out / f"{stem}_timings.json").write_text(json.dumps(timings, indent=2), encoding="utf-8")
    print(f"降頻篩選完成：{position} 幀中深度分析 {deep_frames} 幀（{len(windows.completed)} 段），事件 {len(all_events)} 筆",
          flush=True)
    if args.violation_clips and all_events:
        print("正在剪輯違規片段…", flush=True)
        clips = export_violation_clips(clip_source, jsonl_path, all_events, out / f"{stem}_violation_clips",
                                      args.clip_before, args.clip_after, raw=getattr(args, "clip_raw", False))
        print(f"已輸出 {len(clips)} 個違規片段")
    print(f"JSONL：{jsonl_path}")
    print(f"CSV：{csv_path}")
    print(f"違規候選事件：{violations_path}")
    print(f"逐幀規則診斷：{diagnostics_path}")
    return timings


if __name__ == "__main__":
    main()
