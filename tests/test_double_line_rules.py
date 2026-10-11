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

from double_lines import extract_double_lines, validate_rule_settings, line_quality
from observation_schema import build_frame_observation
from violation_engine import create_rules, RuleContext


LANE_CHANGE = [(125, 200), (125, 195), (125, 190), (140, 185), (154, 180),
               (170, 175), (185, 170), (185, 165), (185, 160)]
U_TURN = LANE_CHANGE[:6] + [(185, 185), (185, 190), (185, 195)]


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


def run_records(records, color="yellow", settings=None):
    rule = create_rules([f"double_{color}_line_crossing"], RuleContext("test", 10, 400, 300),
                        {f"double_{color}_line_crossing": {"settings": settings}})[0]
    events = []
    for record in records:
        events.extend(rule.evaluate(record))
    events.extend(rule.finalize())
    return events


class DoubleLineRuleTests(unittest.TestCase):
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
        for mode in ("vehicle_id", "motion", "missing_line", "frame_gap", "low_confidence",
                     "box_jump", "pair_missing", "stale_line", "missing_vehicle"):
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
            else:
                item["observations"]["vehicles"] = []
            with self.subTest(mode=mode):
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
            with patch("sys.argv", ["evaluate_violations.py", str(source), "--output", str(output),
                                    "--rules", "double_yellow_line_crossing"]):
                evaluate_violations.main()
            events = [json.loads(s) for s in output.read_text(encoding="utf-8").splitlines()]
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


if __name__ == "__main__":
    unittest.main()
