import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from double_lines import extract_double_lines, attach_double_lines, finite_line_position
from observation_schema import build_frame_observation
from violation_engine import create_rules, RuleContext, ViolationEngine
from violation_visualization import event_details


MARKINGS = [{"class_id": 7, "class_name": "solid double white", "components": []},
            {"class_id": 8, "class_name": "solid double yellow", "components": []}]


def line_mask(shift=0):
    mask = np.zeros((120, 320), dtype=np.uint8)
    for y in range(25, 105):
        x = 90 + round((y - 60) ** 2 / 300) + shift
        mask[y, x:x + 3] = 8
        mask[y, x + 8:x + 11] = 8
    return mask


def observation(index, motion=None):
    record = build_frame_observation(index, index / 30, 320, 120, 30, [], [], MARKINGS)
    record["postprocessing"]["road_marking_compensation"]["motion"] = motion or {
        "valid": True, "matrix": np.eye(3).tolist()}
    return record


class DoubleLineTests(unittest.TestCase):
    def test_curved_pair_has_finite_mask_boundaries(self):
        mask = line_mask()
        original = mask.copy()
        lines = extract_double_lines(mask, MARKINGS)
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual(line["geometry_status"], "paired")
        self.assertEqual([p[1] for p in (line["centerline"][0], line["centerline"][-1])], [25, 104])
        self.assertGreater(len(set(p[0] for p in line["centerline"])), 2)
        for boundary in ("left_boundary", "right_boundary"):
            for x, y in line[boundary]:
                self.assertEqual(mask[int(y), int(x)], 8)
        np.testing.assert_array_equal(mask, original)
        self.assertFalse(line["rule_eligible"])

    def test_missing_rows_are_not_bridged_and_colors_are_not_paired(self):
        mask = line_mask()
        mask[55:75] = 0
        lines = extract_double_lines(mask, MARKINGS)
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertTrue(line["bbox_xyxy"][3] < 55 or line["bbox_xyxy"][1] >= 75)
        mask = np.zeros_like(mask)
        mask[25:105, 90:93], mask[25:105, 98:101] = 7, 8
        lines = extract_double_lines(mask, MARKINGS)
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line["component_count"] == 1 for line in lines))

    def test_finite_position_has_sign_and_no_end_extension(self):
        line = [[100, 20], [100, 60], [100, 100]]
        self.assertLess(finite_line_position([110, 50], line)["signed_distance_px"], 0)
        self.assertGreater(finite_line_position([90, 50], line)["signed_distance_px"], 0)
        self.assertIsNone(finite_line_position([100, 110], line))
        self.assertIsNone(finite_line_position([100, 10], line))

    def test_camera_motion_preserves_id_and_snapshots(self):
        state = {}
        first = observation(0)
        attach_double_lines(first, line_mask(), state)
        saved = copy.deepcopy(first)
        motion = {"valid": True, "matrix": [[1, 0, 20], [0, 1, 0], [0, 0, 1]]}
        second = observation(1, motion)
        attach_double_lines(second, line_mask(20), state)
        a, b = first["observations"]["double_lines"][0], second["observations"]["double_lines"][0]
        self.assertEqual(a["track_id"], b["track_id"])
        self.assertEqual(b["observed_frames"], 2)
        self.assertEqual(first, saved)
        b["centerline"][0][0] = -100
        self.assertNotEqual(state["tracks"][0]["centerline"][0][0], -100)
        json.dumps(second, allow_nan=False)

    def test_invalid_motion_gap_and_missing_mask_break_identity(self):
        for motion in ({"valid": False}, {"valid": True, "matrix": [[0]]},
                       {"valid": True, "matrix": np.zeros((3, 3)).tolist()}):
            state = {}
            first, second = observation(0), observation(1, motion)
            attach_double_lines(first, line_mask(), state)
            attach_double_lines(second, line_mask(), state)
            self.assertNotEqual(first["observations"]["double_lines"][0]["track_id"],
                                second["observations"]["double_lines"][0]["track_id"])
        state = {}
        for index, mask in ((0, line_mask()), (1, None), (2, line_mask()), (5, line_mask())):
            record = observation(index)
            attach_double_lines(record, mask, state)
            if index == 1:
                self.assertEqual(record["observations"]["double_lines"], [])
            else:
                self.assertEqual(state["tracks"][0]["observed_frames"], 1)
        self.assertEqual(state["next_id"], 3)

    def test_ambiguous_components_do_not_merge(self):
        mask = np.zeros((120, 320), dtype=np.uint8)
        for x in (90, 98, 106):
            mask[25:105, x:x + 3] = 8
        lines = extract_double_lines(mask, MARKINGS)
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(line["geometry_status"] == "provisional" for line in lines))

    def test_ambiguous_track_match_gets_new_identity(self):
        state = {}
        first = observation(0)
        attach_double_lines(first, line_mask(), state)
        duplicate = copy.deepcopy(state["tracks"][0])
        duplicate["track_id"] = "other-track"
        state["tracks"].append(duplicate)
        second = observation(1)
        attach_double_lines(second, line_mask(), state)
        self.assertEqual(second["observations"]["double_lines"][0]["tracking_status"], "new")
        self.assertEqual(second["observations"]["double_lines"][0]["observed_frames"], 1)

    def test_replay_entrypoint_accepts_old_and_new_schema_without_events(self):
        import evaluate_violations
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = [observation(0), observation(1)]
            records[0]["schema_version"] = "1.2"
            records[0]["observations"].pop("double_lines")
            attach_double_lines(records[1], line_mask(), {})
            source, output, diagnostics = root / "input.jsonl", root / "events.jsonl", root / "diagnostics.jsonl"
            source.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
            with patch("sys.argv", ["evaluate_violations.py", str(source), "--output", str(output),
                                    "--rules", "all", "--diagnostics", str(diagnostics)]):
                evaluate_violations.main()
            self.assertEqual(output.read_text(encoding="utf-8"), "")
            details = [json.loads(line) for line in diagnostics.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(details), 2)
            key = "double_yellow_line_crossing"
            self.assertEqual(details[0]["rules"][key]["geometry_status"], "geometry_unavailable")
            self.assertEqual(details[1]["rules"][key]["geometry_status"], "observed")

    def test_legacy_records_and_fresh_vehicle_gate_never_emit_events(self):
        rules = create_rules(["all"], RuleContext("test", 30, 320, 120))
        engine = ViolationEngine(rules)
        record = observation(0)
        record["observations"].pop("double_lines")
        self.assertEqual(engine.evaluate(record), [])
        state = {}
        double_rules = rules[1:]
        for index in range(4):
            record = observation(index)
            record["observations"]["vehicles"] = [
                {"object_id": "v1", "source_frame": index},
                {"object_id": "v2", "source_frame": index - 1}, {"object_id": "v3"}]
            attach_double_lines(record, line_mask(), state)
            for rule in double_rules:
                self.assertEqual(rule.evaluate(record), [])
                self.assertEqual(rule.diagnostics["fresh_vehicle_count"], 1)
                self.assertEqual(rule.diagnostics["status"], "evaluating")
        self.assertEqual(engine.finalize(), [])

    def test_event_contract_requires_explicit_decision_and_retains_evidence(self):
        context = RuleContext("test", 30, 320, 120)
        for color, class_id in (("yellow", 8), ("white", 7)):
            rule = create_rules([f"double_{color}_line_crossing"], context)[0]
            record = observation(0)
            attach_double_lines(record, line_mask(), {})
            line = record["observations"]["double_lines"][0]
            line.update(class_name=f"solid double {color}", class_id=class_id)
            vehicle = {"object_id": "v1", "class_name": "car", "plate_text": "ABC-1234"}
            with self.assertRaises(ValueError):
                rule.build_event(record["frame"], vehicle, line, status="confirmed", conditions={"crossing": False})
            event = rule.build_event(record["frame"], vehicle, line, status="suspected",
                                    conditions={"crossing": False})
            vehicle["plate_text"] = None
            self.assertEqual(event_details(event)["plate"], "ABC-1234")
            self.assertEqual(event["evidence"]["double_line"]["track_id"], line["track_id"])
            json.dumps(event, allow_nan=False)
            event.pop("description")
            self.assertEqual(event_details(event)["reason"], rule.label)


if __name__ == "__main__":
    unittest.main()
