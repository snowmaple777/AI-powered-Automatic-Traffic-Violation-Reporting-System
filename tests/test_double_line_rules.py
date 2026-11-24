"""Behavioral sequences: true crossings and adversarial non-crossings."""
import copy
import json
import gzip
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from double_lines import (extract_double_lines, validate_rule_settings, line_quality, attach_double_lines,
                          double_line_geometry_options, double_line_rule_options, validate_geometry_settings)
from observation_schema import build_frame_observation
from violation_engine import create_rules, RuleContext


LANE_CHANGE = [(125, 200), (125, 195), (125, 190), (140, 185), (154, 180),
               (170, 175), (185, 170), (185, 165), (185, 160)]
U_TURN = LANE_CHANGE[:6] + [(185, 185), (185, 190), (185, 195)]
# 以下合成序列依 4ay 以前的側別邊距（4 px＋線帶寬 0.2，約 6 px）設計：例如 (140, 185) 的車底點落在邊距內，
# 用來測狀態機、方向分類與各種中斷。4ay 起預設邊距近乎為零、4az 起方向分類另設取樣邊距；機制測試固定沿用
# 4ay 以前的設定，新預設另見 C3MarginDefaultTests、DirectionMarginTests。
LEGACY_MARGIN = {"side_margin_px": 4.0, "margin_width_ratio": .2, "direction_side_margin_px": 0.0}


def sequence(points, color="yellow", camera_shift=0):
    class_id = 8 if color == "yellow" else 7
    mask = np.zeros((300, 400), dtype=np.uint8)
    mask[20:281, 150:153], mask[20:281, 158:161] = class_id, class_id
    markings = [{"class_id": class_id, "class_name": f"solid double {color}"}]
    template = extract_double_lines(mask, markings)[0]
    records = []
    for index, (x, y) in enumerate(points):
        offset = camera_shift * index
        line = copy.deepcopy(template)
        for part in [line] + line["members"]:
            for key in ("centerline", "left_boundary", "right_boundary"):
                for p in part[key]:
                    p[0] += offset
        line["bbox_xyxy"][0] += offset
        line["bbox_xyxy"][2] += offset
        line.update(source_frame=index, track_id="line-1", observed_frames=index + 5)
        vehicle = dict(object_id="v1", source_frame=index, confidence=.9, class_name="car",
                       bbox_xyxy=[x - 10 + offset, y - 40, x + 10 + offset, y], plate_text="ABC-1234")
        record = build_frame_observation(index, index / 10, 400, 300, 10, [vehicle], [], markings)
        record["observations"]["double_lines"] = [line]
        record["postprocessing"]["road_marking_compensation"]["motion"] = {
            "valid": True, "matrix": [[1, 0, camera_shift], [0, 1, 0], [0, 0, 1]],
            "reprojection_error_px": 0}
        records.append(record)
    return records


def run_records(records, color="yellow", settings=None, legacy_margin=True):
    if legacy_margin:
        settings = {**LEGACY_MARGIN, **(settings or {})}
    rule = create_rules([f"double_{color}_line_crossing"], RuleContext("test", 10, 400, 300),
                        {f"double_{color}_line_crossing": {"settings": settings}})[0]
    events = []
    for record in records:
        events.extend(rule.evaluate(record))
    events.extend(rule.finalize())
    return events


class DoubleLineRuleTests(unittest.TestCase):
    def test_line_handoff_skips_lines_projected_behind_the_horizon(self):
        # 曾讓全片重播當掉：舊線經鏡頭運動投影到地平線後方，project_points 拋出 ValueError。
        rule = create_rules(["double_yellow_line_crossing"], RuleContext("test", 10, 400, 300))[0]
        rule.previous_lines = {"old": np.array([[100., 50.], [120., 250.]])}
        usable = [{"track_id": "new", "centerline": [[101., 50.], [121., 250.]]}]
        behind = np.array([[1., 0, 0], [0, 1., 0], [0, .01, -1.]])  # w = 0.01y - 1 <= 0（y <= 100）
        rule._hand_off_lines(usable, behind)  # 不得拋出例外
        self.assertEqual(set(rule.previous_lines), {"new"})

    def test_brief_unsupported_position_requires_review_and_expires(self):
        original_position = __import__("violation_engine").vehicle_line_position
        records = sequence(LANE_CHANGE)

        def missing_position(vehicle, line, settings, error=0):
            if vehicle["source_frame"] == 4:
                return None
            return original_position(vehicle, line, settings, error)

        with patch("violation_engine.vehicle_line_position", side_effect=missing_position):
            events = run_records(records)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "suspected")
        self.assertFalse(events[0]["conditions"]["uninterrupted_positions"])
        with patch("violation_engine.vehicle_line_position", side_effect=missing_position):
            self.assertEqual(run_records(records, settings={"emit_suspected": False}), [])

        def long_gap(vehicle, line, settings, error=0):
            if 3 <= vehicle["source_frame"] <= 6:
                return None
            return original_position(vehicle, line, settings, error)

        with patch("violation_engine.vehicle_line_position", side_effect=long_gap):
            self.assertEqual(run_records(records), [])

    def test_complete_crossing_both_colors_directions_and_maneuvers(self):
        for color in ("yellow", "white"):
            for path, behavior in ((LANE_CHANGE, "lane_change"), (U_TURN, "u_turn")):
                for reverse in (False, True):
                    points = [(310 - x, y) for x, y in path] if reverse else path
                    with self.subTest(color=color, behavior=behavior, reverse=reverse):
                        events = run_records(sequence(points, color), color)
                        self.assertEqual(len(events), 1)
                        event = events[0]
                        self.assertEqual(event["status"], "confirmed")
                        self.assertEqual(event["behavior"], behavior)
                        self.assertLess(event["frame"], event["confirmation_frame"])
                        self.assertEqual(event["evidence"]["vehicle"]["plate_text"], "ABC-1234")
                        self.assertIsNotNone(event["evidence"]["transition"])
                        json.dumps(event, allow_nan=False)

    def test_camera_motion_does_not_change_behavior(self):
        for path, behavior in ((LANE_CHANGE, "lane_change"), (U_TURN, "u_turn")):
            events = run_records(sequence(path, camera_shift=4))
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["behavior"], behavior)
        self.assertEqual(run_records(sequence([(125, 190)] * 12, camera_shift=7)), [])

    def test_complete_crossing_without_direction_remains_unclassified(self):
        events = run_records(sequence([(x, 190) for x, _ in LANE_CHANGE]))
        self.assertEqual(events[0]["behavior"], "complete_crossing")
        self.assertEqual(events[0]["status"], "confirmed")

    def test_touch_return_start_on_line_and_near_endpoint_do_not_emit(self):
        for points in (LANE_CHANGE[:5] + [(125, 180)] * 5,
                       LANE_CHANGE[4:], [(125, 180), (130, 180)] * 6,
                       [(x, 282) for x, y in LANE_CHANGE]):
            self.assertEqual(run_records(sequence(points)), [])

    def test_missing_transition_can_only_be_suspected(self):
        # Small enough steps to remain physically plausible, but no sampled center in the band.
        path = LANE_CHANGE[:3] + [(145, 185), (180, 180), (185, 175), (185, 170), (185, 165)]
        events = run_records(sequence(path))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "suspected")
        self.assertIn("transition_observed", events[0]["missing_conditions"])
        self.assertEqual(run_records(sequence(path), settings={"emit_suspected": False}), [])

    def test_cached_vehicle_frames_never_add_stability_votes(self):
        records = sequence(LANE_CHANGE)
        for record in records[1:3]:
            record["observations"]["vehicles"][0]["source_frame"] = 0
        self.assertEqual(run_records(records), [])

    def test_identity_motion_gap_and_low_quality_break_candidates(self):
        for mode in ("vehicle_id", "missing_line", "frame_gap", "low_confidence",
                     "box_jump", "pair_missing", "stale_line", "long_missing_vehicle"):
            records = sequence(LANE_CHANGE)
            item = records[4]
            if mode == "line_id":
                for record in records[4:]:
                    record["observations"]["double_lines"][0]["track_id"] = "line-2"
            elif mode == "vehicle_id":
                for record in records[4:]:
                    record["observations"]["vehicles"][0]["object_id"] = "v2"
            elif mode == "motion":
                item["postprocessing"]["road_marking_compensation"]["motion"]["valid"] = False
            elif mode == "missing_line":
                item["observations"]["double_lines"] = []
            elif mode == "frame_gap":
                records.pop(4)
            elif mode == "low_confidence":
                item["observations"]["vehicles"][0]["confidence"] = .1
            elif mode == "box_jump":
                item["observations"]["vehicles"][0]["bbox_xyxy"] = [80, 40, 220, 180]
            elif mode == "pair_missing":
                item["observations"]["double_lines"][0].pop("members")
            elif mode == "stale_line":
                item["observations"]["double_lines"][0]["source_frame"] = 3
            else:  # 超過 max_observation_gap_sec（0.3 秒＝3 幀）的漏偵測
                for record in records[4:8]:
                    record["observations"]["vehicles"] = []
            with self.subTest(mode=mode):
                self.assertEqual(run_records(records), [])

    def test_brief_motion_failure_is_tolerated_as_suspected(self):
        # 鏡頭運動估計短暫失敗：以「鏡頭不動」近似，保留候選但只能是疑似；關閉或超過時限則不輸出。
        def broken(frames):
            records = sequence(LANE_CHANGE)
            for record in records[frames]:
                record["postprocessing"]["road_marking_compensation"]["motion"]["valid"] = False
            return records
        events = run_records(broken(slice(4, 5)))
        self.assertEqual([e["status"] for e in events], ["suspected"])
        self.assertIn("camera_motion_compensated", events[0]["missing_conditions"])
        self.assertEqual(run_records(broken(slice(4, 5)), settings={"allow_motion_gaps": False}), [])
        self.assertEqual(run_records(broken(slice(4, 9)), settings={"max_motion_gap_sec": .2}), [])

    def test_far_endpoint_occluded_by_vehicle_is_extrapolated_as_suspected(self):
        # 雙線遠端被車身遮住（可見線段止於車底下方），沿端點方向外插判斷，事件只能是疑似。
        records = sequence(LANE_CHANGE)
        for record, (x, y) in zip(records, LANE_CHANGE):
            line = record["observations"]["double_lines"][0]
            for part in [line] + line["members"]:
                for key in ("centerline", "left_boundary", "right_boundary"):
                    part[key] = [p for p in part[key] if p[1] >= y + 4] or part[key][-2:]
        events = run_records(records)
        self.assertEqual([e["status"] for e in events], ["suspected"])
        self.assertIn("line_observed_at_footprint", events[0]["missing_conditions"])
        self.assertEqual(run_records(records, settings={"max_extrapolation_ratio": 0}), [])

    def test_brief_missed_detection_keeps_candidate_as_suspected(self):
        # 偵測器漏掉一幀，與「參考點暫時不可用」同樣處理：保留候選但只能是疑似。
        records = sequence(LANE_CHANGE)
        records[4]["observations"]["vehicles"] = []
        events = run_records(records)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "suspected")
        self.assertFalse(events[0]["conditions"]["uninterrupted_positions"])
        # 尚未 armed 時漏偵測：起點證據不足，直接放棄。
        records = sequence(LANE_CHANGE)
        records[1]["observations"]["vehicles"] = []
        self.assertEqual(run_records(records), [])

    def test_event_dedup_and_reverse_crossing_after_rearm(self):
        path = LANE_CHANGE + [(185, 160)] * 15
        self.assertEqual(len(run_records(sequence(path))), 1)
        path += [(170, 160), (154, 160), (140, 160), (125, 160), (125, 155), (125, 150)]
        events = run_records(sequence(path))
        self.assertEqual(len(events), 2)
        self.assertNotEqual(events[0]["event_id"], events[1]["event_id"])

    def test_geometrically_identical_line_id_change_keeps_evidence(self):
        records = sequence(LANE_CHANGE)
        for record in records[4:]:
            record["observations"]["double_lines"][0]["track_id"] = "replacement-id"
        self.assertEqual(len(run_records(records)), 1)

    def test_cdy01_real_recording_and_disabled_semantic_fallback(self):
        fixture = Path(__file__).parent / "fixtures/cdy01_crossing.jsonl.gz"
        records = [json.loads(s) for s in gzip.decompress(fixture.read_bytes()).decode().splitlines()]
        events = run_records(records)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["object_id"], "vehicle-000010")
        self.assertEqual(event["status"], "suspected")
        self.assertTrue(5 <= event["timestamp_sec"] <= 6)
        self.assertTrue(6 <= event["confirmation_timestamp_sec"] <= 6.5)
        self.assertIn("directly_observed_pair", event["missing_conditions"])
        self.assertEqual(run_records(records, settings={"allow_semantic_band": False}), [])
        self.assertEqual(run_records(records, settings={"max_partial_support_seconds": .2}), [])

    def test_internal_polyline_corner_has_finite_position(self):
        from double_lines import finite_line_position
        # Nearest point is an internal vertex, not perpendicular to either segment.
        position = finite_line_position([120, 45], [[100, 20], [90, 45], [100, 70]])
        self.assertIsNotNone(position)
        self.assertGreater(position["endpoint_distance_px"], 0)

    def test_semantic_origin_cannot_be_upgraded_by_a_later_pair(self):
        records = sequence(LANE_CHANGE)
        for record in records[:3]:
            line = record["observations"]["double_lines"][0]
            line["component_count"] = 1
            line["members"] = [{key: copy.deepcopy(line[key]) for key in
                                ("centerline", "left_boundary", "right_boundary")}]
        events = run_records(records)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "suspected")

    def test_timeout_and_end_of_video_do_not_flush_incomplete_events(self):
        path = LANE_CHANGE[:5] + [(154, 180)] * 65 + LANE_CHANGE[5:]
        self.assertEqual(run_records(sequence(path)), [])
        self.assertEqual(run_records(sequence(LANE_CHANGE[:6])), [])

    def test_parameters_change_decision_and_invalid_values_fail(self):
        self.assertEqual(run_records(sequence(LANE_CHANGE), settings={"stable_observations": 6}), [])
        for settings in ({"typo": 1}, {"stable_observations": 1}, {"stable_seconds": float("nan")},
                         {"emit_suspected": 1}, {"min_vehicle_confidence": 2},
                         {"min_uturn_angle_deg": 20}, {"max_box_scale_ratio": 1}):
            with self.assertRaises(ValueError):
                validate_rule_settings(settings)

    def test_invalid_or_single_pair_geometry_cannot_be_eligible(self):
        line = sequence(LANE_CHANGE)[0]["observations"]["double_lines"][0]
        self.assertIsNone(line_quality(line, validate_rule_settings()))
        line["members"][0]["centerline"][3][0] += 100
        self.assertEqual(line_quality(line, validate_rule_settings()), "nonparallel_pair")
        line["component_count"] = 1
        self.assertEqual(line_quality(line, validate_rule_settings()), "unverified_pair")

    def test_real_decision_replay_and_clip_export(self):
        import evaluate_violations
        from violation_clips import export_violation_clips
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "input.jsonl", root / "events.jsonl"
            records = sequence(LANE_CHANGE)
            source.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
            legacy = root / "legacy_margin.json"
            legacy.write_text(json.dumps({"version": 1, **LEGACY_MARGIN}), encoding="utf-8")
            with patch("sys.argv", ["evaluate_violations.py", str(source), "--output", str(output),
                                    "--rules", "double_yellow_line_crossing", "--double-line-config", str(legacy)]):
                evaluate_violations.main()
            events =[json.loads(s) for s in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(events), 1)
            video = root / "input.mp4"
            writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 10, (400, 300))
            self.assertTrue(writer.isOpened())
            for _ in records:
                writer.write(np.zeros((300, 400, 3), dtype=np.uint8))
            writer.release()
            clips = export_violation_clips(video, source, events, root / "clips", .3, .4)
            self.assertEqual(len(clips), 1)
            summary = Path(clips[0]["clip_path"]).with_suffix(".txt").read_text(encoding="utf-8-sig")
            self.assertIn("ABC-1234", summary)
            self.assertIn("變換車道", summary)
            self.assertEqual(clips[0]["event"]["confirmation_frame"], 8)
            config = root / "strict.json"
            config.write_text(json.dumps({"version": 1, "stable_observations": 6}), encoding="utf-8")
            with patch("sys.argv", ["evaluate_violations.py", str(source), "--output", str(output),
                                    "--rules", "double_yellow_line_crossing",
                                    "--double-line-config", str(config)]):
                evaluate_violations.main()
            self.assertEqual(output.read_text(encoding="utf-8"), "")



class C3MarginDefaultTests(unittest.TestCase):
    """4ay 預設：側別邊距近乎為零，車底點一離開線帶本身就算該側（配準誤差項仍保留）。"""

    def test_default_margin_values_and_config_agree(self):
        from double_lines import RULE_DEFAULTS
        config = json.loads((Path(__file__).resolve().parents[1] / "configs/double_line_rules.json").read_text(encoding="utf-8"))
        for key, value in (("side_margin_px", .01), ("margin_width_ratio", .001), ("direction_side_margin_px", 4.0),
                           ("direction_margin_width_ratio", .2), ("direction_min_samples", 2)):
            self.assertEqual(RULE_DEFAULTS[key], value)
            self.assertEqual(config[key], value)

    def test_shipped_config_enables_band_dwell_and_endpoint_inference(self):
        # 4bh：預設設定檔啟用 BDE（4bb 停留 6 秒＋4ba 推定 1.5 秒）；程式預設仍為關閉。
        from double_lines import RULE_DEFAULTS, validate_rule_settings
        config = json.loads((Path(__file__).resolve().parents[1] / "configs/double_line_rules.json").read_text(encoding="utf-8"))
        self.assertEqual((config["band_dwell_seconds"], config["endpoint_inference_seconds"]), (6.0, 1.5))
        self.assertEqual((RULE_DEFAULTS["band_dwell_seconds"], RULE_DEFAULTS["endpoint_inference_seconds"]), (0.0, 0.0))
        for removed in ("curve_extrapolation", "extend_near_endpoint", "line_memory_seconds",
                        "max_origin_interruption_sec", "straddle_min_seconds", "confirm_short_position_gap"):
            with self.assertRaises(ValueError):  # 4bh 移除的實驗選項
                validate_rule_settings({removed: RULE_DEFAULTS.get(removed, 0)})

    def test_points_just_outside_band_count_as_side(self):
        # (140, 185)：右側車底點 x=147，離線帶左緣 150 只有 3 px；舊邊距下為 0，新預設下算原側。
        events = run_records(sequence(LANE_CHANGE), legacy_margin=False)
        self.assertEqual([e["status"] for e in events], ["confirmed"])
        # 事件時間（過線幀）不變；對側更早達到穩定，確認幀比舊邊距早一幀（7 對 8）。
        legacy = run_records(sequence(LANE_CHANGE))[0]
        self.assertEqual(events[0]["frame"], legacy["frame"])
        self.assertEqual(events[0]["confirmation_frame"], legacy["confirmation_frame"] - 1)

    def test_lateral_samples_near_band_degrade_maneuver_label_without_direction_margin(self):
        # 4ay 副作用（4az 以方向取樣邊距修正）：停用方向邊距時，靠近線帶的橫移樣本進入方向判斷，
        # 變換車道／迴轉退回 complete_crossing。
        for path in (LANE_CHANGE, U_TURN):
            events = run_records(sequence(path), settings={"direction_side_margin_px": 0.0}, legacy_margin=False)
            self.assertEqual([e["behavior"] for e in events], ["complete_crossing"])

    def test_touch_and_return_still_do_not_emit(self):
        for points in (LANE_CHANGE[:5] + [(125, 180)] * 5, [(125, 180), (130, 180)] * 6):
            self.assertEqual(run_records(sequence(points), legacy_margin=False), [])


class DirectionMarginTests(unittest.TestCase):
    """4az：方向分類只用離線帶夠遠的樣本；只改 behavior／direction，不改事件成立、時間與狀態。"""
    ON = {"direction_side_margin_px": 4.0, "direction_margin_width_ratio": .2}

    def test_restores_maneuver_labels_under_small_side_margin(self):
        # 10 fps 合成序列每步 5 px，確認時對側只有 2 個夠遠的樣本（位移 5 px），故此測試把位移門檻降到 4 px；
        # 真實 29 fps 影片對側約有 6 個樣本，預設 8 px 即可（見 refactor_log 4az）。
        coarse = {**self.ON, "min_heading_displacement_px": 4.0}
        for path, behavior in ((LANE_CHANGE, "lane_change"), (U_TURN, "u_turn")):
            for shift in (0, 4):
                plain = run_records(sequence(path, camera_shift=shift), legacy_margin=False,
                                    settings={"min_heading_displacement_px": 4.0, "direction_side_margin_px": 0.0})
                fixed = run_records(sequence(path, camera_shift=shift), settings=coarse, legacy_margin=False)
                self.assertEqual([e["behavior"] for e in fixed], [behavior])
                for key in ("event_id", "status", "frame", "confirmation_frame", "timestamp_sec", "conditions"):
                    self.assertEqual(fixed[0][key], plain[0][key])
                self.assertEqual(fixed[0]["evidence"]["parameters"]["direction_side_margin_px"], 4.0)

    def test_lateral_only_crossing_stays_unclassified(self):
        events = run_records(sequence([(x, 190) for x, _ in LANE_CHANGE]), settings=self.ON, legacy_margin=False)
        self.assertEqual([(e["behavior"], e["status"]) for e in events], [("complete_crossing", "confirmed")])

    def test_too_few_clear_samples_fall_back_to_unclassified(self):
        events = run_records(sequence(LANE_CHANGE), settings={**self.ON, "direction_min_samples": 4},
                             legacy_margin=False)
        self.assertEqual([e["behavior"] for e in events], ["complete_crossing"])

    def test_enabled_by_default_and_zero_disables(self):
        default = run_records(sequence(LANE_CHANGE), legacy_margin=False)[0]
        self.assertEqual(default["evidence"]["parameters"]["direction_side_margin_px"], 4.0)
        off = run_records(sequence(LANE_CHANGE), settings={"direction_side_margin_px": 0.0}, legacy_margin=False)[0]
        self.assertEqual(off["evidence"]["parameters"]["direction_side_margin_px"], 0.0)
        for key in ("event_id", "status", "frame", "confirmation_frame", "conditions"):
            self.assertEqual(default[key], off[key])

    def test_invalid_values_fail(self):
        for settings in ({"direction_side_margin_px": -1}, {"direction_margin_width_ratio": -.1},
                         {"direction_min_samples": 1}, {"direction_min_samples": 2.5}):
            with self.assertRaises(ValueError):
                validate_rule_settings(settings)



def gap_sequence(left=3, missing=5, right=5, y=200, color="yellow"):
    """車輛在左側出現 left 幀、偵測器漏掉 missing 幀（原規則候選逾時刪除），之後在右側出現 right 幀。"""
    points = [(125, y)] * left + [(155, y)] * missing + [(185, y)] * right
    records = sequence(points, color)
    for record in records[left:left + missing]:
        record["observations"]["vehicles"] = []
        record["observations"].pop("rule_vehicles", None)
    return records


class EndpointInferenceTests(unittest.TestCase):
    """4ba 起點與終點推定：預設關閉；只補原規則因中段缺車框而漏掉的跨越，且只能是疑似。"""
    ON = {"endpoint_inference_seconds": 1.5}

    def test_disabled_by_default_and_not_in_parameters(self):
        from double_lines import ENDPOINT_OPTION_KEYS, RULE_DEFAULTS
        self.assertEqual(RULE_DEFAULTS["endpoint_inference_seconds"], 0.0)
        self.assertEqual(run_records(gap_sequence(), legacy_margin=False), [])
        default = run_records(sequence(LANE_CHANGE), legacy_margin=False)[0]
        self.assertFalse(set(ENDPOINT_OPTION_KEYS) & set(default["evidence"]["parameters"]))
        rule = create_rules(["double_yellow_line_crossing"], RuleContext("test", 10, 400, 300))[0]
        self.assertFalse(set(ENDPOINT_OPTION_KEYS) & set(rule.settings))

    def test_gap_crossing_becomes_suspected_endpoint_event(self):
        for color in ("yellow", "white"):
            for reverse in (False, True):
                records = gap_sequence(color=color)
                if reverse:  # 右到左：左右對調
                    records = gap_sequence(color=color)
                    for record in records:
                        for vehicle in record["observations"]["vehicles"]:
                            x1, y1, x2, y2 = vehicle["bbox_xyxy"]
                            vehicle["bbox_xyxy"] = [310 - x2, y1, 310 - x1, y2]
                with self.subTest(color=color, reverse=reverse):
                    events = run_records(records, color, settings=self.ON, legacy_margin=False)
                    self.assertEqual(len(events), 1)
                    event = events[0]
                    self.assertEqual(event["status"], "suspected")
                    self.assertFalse(event["conditions"]["stable_origin"])
                    self.assertFalse(event["conditions"]["complete_band_crossing"])
                    self.assertTrue(event["conditions"]["stable_destination"])
                    info = event["evidence"]["endpoint_inference"]
                    self.assertEqual(info["origin_frames"], [0, 2])
                    self.assertAlmostEqual(info["gap_sec"], .6)
                    self.assertEqual(event["frame"], 2)
                    self.assertEqual(event["confirmation_frame"], 10)
                    self.assertEqual(event["evidence"]["vehicle"]["plate_text"], "ABC-1234")
                    self.assertEqual(event["evidence"]["parameters"]["endpoint_inference_seconds"], 1.5)
                    json.dumps(event, allow_nan=False)

    def test_gap_longer_than_limit_or_suspected_disabled_emits_nothing(self):
        self.assertEqual(run_records(gap_sequence(), settings={"endpoint_inference_seconds": .5}, legacy_margin=False), [])
        self.assertEqual(run_records(gap_sequence(), settings={**self.ON, "emit_suspected": False}, legacy_margin=False), [])

    def test_same_side_reappearance_and_too_few_origin_samples_emit_nothing(self):
        same = gap_sequence()
        for record in same[8:]:
            for vehicle in record["observations"]["vehicles"]:
                vehicle["bbox_xyxy"][0] -= 60
                vehicle["bbox_xyxy"][2] -= 60
        self.assertEqual(run_records(same, settings=self.ON, legacy_margin=False), [])
        # 只有 1 個另一側樣本（少於 direction_min_samples），原規則也不會建立起點。
        self.assertEqual(run_records(gap_sequence(left=1, missing=7), settings=self.ON, legacy_margin=False), [])

    def test_observed_crossings_are_unchanged_and_not_duplicated(self):
        for path in (LANE_CHANGE, U_TURN):
            for shift in (0, 4):
                off = run_records(sequence(path, camera_shift=shift), legacy_margin=False)
                on = run_records(sequence(path, camera_shift=shift), settings=self.ON, legacy_margin=False)
                self.assertEqual(len(on), 1)
                for key in ("event_id", "status", "frame", "confirmation_frame", "behavior", "conditions"):
                    self.assertEqual(on[0][key], off[0][key])
        for points in (LANE_CHANGE[:5] + [(125, 180)] * 5, [(125, 180), (130, 180)] * 6):
            self.assertEqual(run_records(sequence(points), settings=self.ON, legacy_margin=False), [])

    def test_camera_motion_is_compensated_in_history(self):
        events = run_records(gap_sequence(), settings=self.ON, legacy_margin=False)
        shifted = gap_sequence()
        for index, record in enumerate(shifted):
            offset = 3 * index
            record["postprocessing"]["road_marking_compensation"]["motion"]["matrix"] = [[1, 0, 3], [0, 1, 0], [0, 0, 1]]
            for line in record["observations"]["double_lines"]:
                for part in [line] + line["members"]:
                    for key in ("centerline", "left_boundary", "right_boundary"):
                        for p in part[key]:
                            p[0] += offset
            for vehicle in record["observations"]["vehicles"]:
                vehicle["bbox_xyxy"][0] += offset
                vehicle["bbox_xyxy"][2] += offset
        moved = run_records(shifted, settings=self.ON, legacy_margin=False)
        self.assertEqual([(e["frame"], e["status"]) for e in moved], [(e["frame"], e["status"]) for e in events])
        self.assertTrue(moved[0]["conditions"]["camera_motion_compensated"])

    def test_small_vehicle_guard(self):
        # 畫面高 300、車框高 40（13%）：預設下限 7% 通過；下限 20% 時擋掉；0 不檢查。
        self.assertEqual(len(run_records(gap_sequence(), settings=self.ON, legacy_margin=False)), 1)
        self.assertEqual(run_records(gap_sequence(), settings={**self.ON, "endpoint_min_box_height_ratio": .2},
                                     legacy_margin=False), [])
        loose = run_records(gap_sequence(), settings={**self.ON, "endpoint_min_box_height_ratio": 0.0}, legacy_margin=False)
        self.assertEqual([e["status"] for e in loose], ["suspected"])

    def test_invalid_values_fail(self):
        for settings in ({"endpoint_inference_seconds": -1}, {"endpoint_min_box_height_ratio": float("nan")}, {"endpoint_min_box_height_ratio": 1.0},
                         {"endpoint_inference_seconds": 1.0, "direction_side_margin_px": 0.0}):
            with self.assertRaises(ValueError):
                validate_rule_settings(settings)



def dwell_sequence(origin=2, band=6, destination=5, color="yellow"):
    """起點只出現 origin 幀（原規則建立不了穩定起點），之後壓在線帶 band 幀，再到另一側 destination 幀。"""
    return sequence([(125, 200)] * origin + [(155, 200)] * band + [(185, 200)] * destination, color)


def mirror(records):
    for record in records:
        for vehicle in record["observations"]["vehicles"]:
            x1, y1, x2, y2 = vehicle["bbox_xyxy"]
            vehicle["bbox_xyxy"] = [310 - x2, y1, 310 - x1, y2]
    return records


class BandDwellTests(unittest.TestCase):
    """4bb 壓線停留跨越：預設關閉；起點短暫、壓線停留後到另一側時補疑似事件，不依賴原規則狀態機。"""
    ON = {"band_dwell_seconds": 6.0}

    def test_disabled_by_default_and_not_in_parameters(self):
        from double_lines import BAND_DWELL_OPTION_KEYS, RULE_DEFAULTS
        self.assertEqual(RULE_DEFAULTS["band_dwell_seconds"], 0.0)
        self.assertEqual(run_records(dwell_sequence(), legacy_margin=False), [])
        default = run_records(sequence(LANE_CHANGE), legacy_margin=False)[0]
        self.assertFalse(set(BAND_DWELL_OPTION_KEYS) & set(default["evidence"]["parameters"]))
        rule = create_rules(["double_yellow_line_crossing"], RuleContext("test", 10, 400, 300))[0]
        self.assertFalse(set(BAND_DWELL_OPTION_KEYS) & set(rule.settings))

    def test_brief_origin_band_dwell_becomes_suspected_event(self):
        for color in ("yellow", "white"):
            for reverse in (False, True):
                records = dwell_sequence(color=color)
                if reverse:
                    mirror(records)
                with self.subTest(color=color, reverse=reverse):
                    events = run_records(records, color, settings=self.ON, legacy_margin=False)
                    self.assertEqual(len(events), 1)
                    event = events[0]
                    self.assertEqual(event["status"], "suspected")
                    self.assertFalse(event["conditions"]["stable_origin"])
                    self.assertTrue(event["conditions"]["complete_band_crossing"])
                    info = event["evidence"]["band_dwell"]
                    self.assertEqual(info["origin_frames"], [0, 1])
                    self.assertEqual(info["band_observations"], 6)
                    self.assertAlmostEqual(info["dwell_sec"], .7)
                    self.assertEqual((event["frame"], event["confirmation_frame"]), (2, 10))
                    self.assertEqual(event["evidence"]["vehicle"]["plate_text"], "ABC-1234")
                    self.assertEqual(event["evidence"]["parameters"]["band_dwell_seconds"], 6.0)
                    json.dumps(event, allow_nan=False)

    def test_thresholds_block(self):
        cases = ((dwell_sequence(band=2), {}), (dwell_sequence(), {"band_dwell_seconds": .5}),
                 (dwell_sequence(), {"band_dwell_min_line_ratio": .9}),  # 線長 260 px < 0.9 × 300
                 (dwell_sequence(), {"band_dwell_min_box_height_ratio": .2}),  # 車框高 40 px < 0.2 × 300
                 (dwell_sequence(origin=1, band=7), {}),  # 起點只有 1 個方向樣本
                 (dwell_sequence(), {"emit_suspected": True, "band_dwell_min_observations": 7}))
        for records, extra in cases:
            with self.subTest(extra=extra):
                self.assertEqual(run_records(records, settings={**self.ON, **extra}, legacy_margin=False), [])
        loose = run_records(dwell_sequence(band=2), settings={**self.ON, "band_dwell_min_observations": 1}, legacy_margin=False)
        self.assertEqual([e["status"] for e in loose], ["suspected"])

    def test_origin_must_precede_first_band_sample(self):
        # 先壓在線帶、之後才短暫出現起點側（側別來回跳動）再到另一側：起點不在第一個線帶樣本之前，不判定
        records = sequence([(155, 200)] * 4 + [(125, 200)] * 2 + [(155, 200)] * 4 + [(185, 200)] * 5)
        self.assertEqual(run_records(records, settings=self.ON, legacy_margin=False), [])

    def test_survives_motion_failure_and_line_track_change(self):
        records = dwell_sequence(band=14)
        for record in records[2:16]:  # 壓線期間鏡頭運動失效 1.4 秒（超過 max_motion_gap_sec，原規則清除候選）
            record["postprocessing"]["road_marking_compensation"]["motion"] = {"valid": False}
        for record in records[9:]:  # 雙線追蹤換編號
            for line in record["observations"]["double_lines"]:
                line["track_id"] = "line-2"
        events = run_records(records, settings=self.ON, legacy_margin=False)
        self.assertEqual([e["status"] for e in events], ["suspected"])
        self.assertFalse(events[0]["conditions"]["same_line_track"])
        self.assertEqual(len(events[0]["evidence"]["band_dwell"]["line_track_ids"]), 2)

    def test_observed_crossings_unchanged_and_not_duplicated(self):
        for path in (LANE_CHANGE, U_TURN):
            for shift in (0, 4):
                off = run_records(sequence(path, camera_shift=shift), legacy_margin=False)
                on = run_records(sequence(path, camera_shift=shift), settings=self.ON, legacy_margin=False)
                self.assertEqual(len(on), 1)
                for key in ("event_id", "status", "frame", "confirmation_frame", "behavior", "conditions"):
                    self.assertEqual(on[0][key], off[0][key])
        for points in (LANE_CHANGE[:5] + [(125, 180)] * 5, [(125, 180), (130, 180)] * 6,
                       [(125, 200)] * 2 + [(155, 200)] * 6 + [(125, 200)] * 5):
            self.assertEqual(run_records(sequence(points), settings=self.ON, legacy_margin=False), [])

    def test_combined_with_endpoint_inference_does_not_duplicate(self):
        events = run_records(dwell_sequence(), settings={**self.ON, "endpoint_inference_seconds": 1.5}, legacy_margin=False)
        self.assertEqual(len(events), 1)

    def test_invalid_values_fail(self):
        for settings in ({"band_dwell_seconds": -1}, {"band_dwell_min_observations": 0},
                         {"band_dwell_min_observations": 2.5}, {"band_dwell_min_line_ratio": 1.0},
                         {"band_dwell_min_box_height_ratio": float("nan")},
                         {"band_dwell_seconds": 6.0, "direction_side_margin_px": 0.0}):
            with self.assertRaises(ValueError):
                validate_rule_settings(settings)


class RecallDetailTests(unittest.TestCase):
    """4bj 召回細節：同車事件合併、4bb 事件時間、跨越中低信心容忍；預設關閉且不進入事件參數。"""

    @staticmethod
    def round_trip(gap=True):
        records = sequence([(125, 200)] * 2 + [(155, 200)] * 6 + [(185, 200)] * 8 + [(155, 200)] * 6 + [(125, 200)] * 8)
        if gap:  # 回程跨越中漏偵測一幀：原規則的回程事件成為疑似
            records[18]["observations"]["vehicles"] = []
            records[18]["observations"].pop("rule_vehicles", None)
        return records

    def test_disabled_by_default_and_not_in_parameters(self):
        from double_lines import RECALL_OPTION_KEYS, RULE_DEFAULTS
        self.assertEqual([RULE_DEFAULTS[k] for k in RECALL_OPTION_KEYS], [0.0, False])
        default = run_records(sequence(LANE_CHANGE), legacy_margin=False)[0]
        self.assertFalse(set(RECALL_OPTION_KEYS) & set(default["evidence"]["parameters"]))
        rule = create_rules(["double_yellow_line_crossing"], RuleContext("test", 10, 400, 300))[0]
        self.assertFalse(set(RECALL_OPTION_KEYS) & set(rule.settings))

    def test_same_vehicle_suspected_events_are_merged(self):
        on = {"band_dwell_seconds": 1.0}
        events = run_records(self.round_trip(), settings=on, legacy_margin=False)
        self.assertEqual([(e["status"], e["frame"]) for e in events], [("suspected", 2), ("suspected", 21)])
        merged = run_records(self.round_trip(), settings={**on, "same_vehicle_merge_seconds": 10.0}, legacy_margin=False)
        self.assertEqual([e["frame"] for e in merged], [2])
        short = run_records(self.round_trip(), settings={**on, "same_vehicle_merge_seconds": 1.0}, legacy_margin=False)
        self.assertEqual(len(short), 2)
        # 確認事件不受合併影響
        confirmed = run_records(self.round_trip(gap=False), settings={**on, "same_vehicle_merge_seconds": 10.0},
                                legacy_margin=False)
        self.assertEqual([e["status"] for e in confirmed], ["suspected", "confirmed"])

    def test_band_dwell_event_time_can_use_last_band_sample(self):
        on = {"band_dwell_seconds": 6.0}
        first = run_records(dwell_sequence(), settings=on, legacy_margin=False)[0]
        last = run_records(dwell_sequence(), settings={**on, "band_dwell_last_band_time": True}, legacy_margin=False)[0]
        self.assertEqual((first["frame"], last["frame"]), (2, 7))
        self.assertEqual(first["confirmation_frame"], last["confirmation_frame"])
        self.assertEqual(first["evidence"]["band_dwell"], last["evidence"]["band_dwell"])

    def test_invalid_values_fail(self):
        for settings in ({"same_vehicle_merge_seconds": -1}, {"same_vehicle_merge_seconds": float("nan")},
                         {"band_dwell_last_band_time": 1}, {"band_dwell_recheck_on_drop": True},
                         {"crossing_low_confidence_floor": .25}):
            with self.assertRaises(ValueError):
                validate_rule_settings(settings)


class TrimWideRowsTests(unittest.TestCase):
    """4bg 過寬列截除：預設維持整個元件丟棄；啟用時保留最長一段合格列。"""
    MARKINGS = [{"class_id": 8, "class_name": "solid double yellow"}]

    @staticmethod
    def flared_mask(narrow=(20, 201), wide=(201, 281)):
        # 畫面寬 400（過寬門檻 32 px）：上段 11 px 寬，下段 81 px 寬，兩段相連成同一個元件
        mask = np.zeros((300, 400), dtype=np.uint8)
        mask[narrow[0]:narrow[1], 150:161] = 8
        mask[wide[0]:wide[1], 120:201] = 8
        return mask

    def test_default_drops_the_whole_wide_component(self):
        self.assertEqual(extract_double_lines(self.flared_mask(), self.MARKINGS), [])

    def test_enabled_keeps_the_narrow_rows_only(self):
        lines = extract_double_lines(self.flared_mask(), self.MARKINGS, trim_wide_rows=True)
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual(line["bbox_xyxy"], [150.0, 20, 160.0, 200])
        self.assertEqual((line["component_count"], line["geometry_status"]), (1, "provisional"))
        self.assertTrue(all(r[0] - l[0] <= 32 for l, r in zip(line["left_boundary"], line["right_boundary"])))

    def test_narrow_components_are_unchanged(self):
        mask = np.zeros((300, 400), dtype=np.uint8)
        mask[20:281, 150:153], mask[20:281, 158:161] = 8, 8
        self.assertEqual(extract_double_lines(mask, self.MARKINGS, trim_wide_rows=True),
                         extract_double_lines(mask, self.MARKINGS))

    def test_short_remaining_block_is_still_dropped(self):
        mask = self.flared_mask(narrow=(20, 27), wide=(27, 120))
        self.assertEqual(extract_double_lines(mask, self.MARKINGS, trim_wide_rows=True), [])

    def test_equal_blocks_keep_the_lower_one(self):
        mask = np.zeros((300, 400), dtype=np.uint8)
        mask[20:60, 150:161] = mask[100:140, 150:161] = 8
        mask[60:100, 120:201] = 8
        lines = extract_double_lines(mask, self.MARKINGS, trim_wide_rows=True)
        self.assertEqual(lines[0]["bbox_xyxy"][1::2], [100, 139])

    def test_attach_uses_the_geometry_option(self):
        def attach(geometry):
            record = build_frame_observation(0, 0, 400, 300, 10, [], [], self.MARKINGS)
            attach_double_lines(record, self.flared_mask(), {}, geometry)
            return record["observations"]["double_lines"]
        self.assertEqual(attach(None), [])
        self.assertEqual(attach(validate_geometry_settings()), [])
        self.assertEqual(len(attach(validate_geometry_settings({"trim_wide_rows": True}))), 1)

    def test_config_geometry_is_read_separately_from_rule_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rules.json"
            path.write_text(json.dumps({"version": 1, "geometry": {"trim_wide_rows": True}}), encoding="utf-8")
            self.assertEqual(double_line_geometry_options(path), {"trim_wide_rows": True})
            settings = double_line_rule_options(path)["double_yellow_line_crossing"]["settings"]
            self.assertEqual(settings, validate_rule_settings())
            path.write_text(json.dumps({"version": 1}), encoding="utf-8")
            self.assertEqual(double_line_geometry_options(path), {"trim_wide_rows": False})
            for geometry in ({"trim_wide_rows": 1}, {"wide_row_ratio": .1}, [True]):
                path.write_text(json.dumps({"version": 1, "geometry": geometry}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    double_line_geometry_options(path)
                with self.assertRaises(ValueError):
                    double_line_rule_options(path)

    def test_default_config_keeps_the_option_off(self):
        path = Path(__file__).resolve().parents[1] / "configs/double_line_rules.json"
        self.assertEqual(double_line_geometry_options(path), {"trim_wide_rows": False})


if __name__ == "__main__":
    unittest.main()
