"""局部車道與動作備註：方向反例、時間證據、旁路及三個入口一致性。"""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from local_lanes import extract_boundaries, local_lane
from motion_notes import MotionNotes, RED_RULE
from violation_visualization import format_event_summary


def mask_for(i=0, pan=0):
    mask = np.zeros((360, 640), np.uint8)
    for x in (240-pan*i, 400-pan*i):
        cv2.line(mask, (x, 110), (x, 350), 4, 5)
    return mask


def row(i, kind="straight", pan=0):
    t = i/10
    x, y = 320., 320-20*t
    if kind in ("left_turn", "right_turn") and t > 1:
        x += (-1 if kind == "left_turn" else 1)*40*(t-1)
        y = 300-20*min(t-1, .5)
    if kind == "stopped":
        y = 300
    if kind == "toward":
        y = 260+20*t
    if kind == "lane_change":
        x += min(max(t-1, 0)*70, 140)
    x -= pan*i
    return dict(frame=dict(index=i, timestamp_sec=t, width=640, height=360, fps=10),
                observations=dict(vehicles=[dict(object_id="v1", bbox_xyxy=[x-15, y-40, x+15, y], source_frame=i)]),
                postprocessing=dict(ego_motion=dict(valid=True, matrix=[[1, 0, -pan], [0, 1, 0], [0, 0, 1]]),
                                    road_marking_source=dict(direct=True, aligned=True, source_frame=i)))


def event():
    return dict(event_id="clip:red:v1:10", frame=10, timestamp_sec=1., object_id="v1", rule_id=RED_RULE,
                status="suspected", conditions={"red": False}, evidence={"keep": [1, 2]},
                merged_events=[dict(event_id="child")])


def collect(kind="straight", rows=None, delayed=False, masks=True, pan=0, enabled=True):
    rows = rows or [row(i, kind, pan) for i in range(41)]
    original = event()
    with MotionNotes(enabled) as m:
        result = []
        for r in rows:
            i = r["frame"]["index"]
            m.observe(r, mask_for(i, pan) if masks else None)
            result += m.submit([original] if i == 10 and not delayed else [])
        result += m.submit([original] if delayed else [], final=True)
    return result[0]


class MotionNotesTests(unittest.TestCase):
    def test_straight_left_right_and_camera_pan(self):
        for kind in ("straight", "left_turn", "right_turn"):
            for pan in (0, 3):
                with self.subTest(kind=kind, pan=pan):
                    note = collect(kind, pan=pan)["motion_note"]
                    self.assertEqual(note["maneuver"], kind, note)

    def test_stopped_toward_and_lane_change_are_not_turns(self):
        for kind in ("stopped", "toward", "lane_change"):
            with self.subTest(kind=kind):
                self.assertEqual(collect(kind)["motion_note"]["maneuver"], "unknown")

    def test_original_fields_input_and_delayed_release(self):
        before = event()
        after = collect()
        self.assertEqual({k: v for k, v in after.items() if k != "motion_note"}, before)
        self.assertEqual(collect(delayed=True), after)
        self.assertEqual(before, event())
        self.assertNotIn("motion_note", after["merged_events"][0])

    def test_disabled_is_exact_bypass_without_database(self):
        e = event()
        with MotionNotes() as m:
            self.assertIsNone(m.db)
            m.observe({})
            self.assertEqual(m.submit([e]), [e])
            self.assertEqual(m.submit([], final=True), [])
        self.assertEqual(collect(enabled=False), event())

    def test_cached_event_is_not_fresh_and_id_switch_cannot_be_borrowed(self):
        rows = [row(i) for i in range(41)]
        rows[10]["observations"]["vehicles"][0]["source_frame"] = 9
        self.assertEqual(collect(rows=rows)["motion_note"]["reason"], "no_fresh_event_vehicle")
        for r in rows[11:]:
            r["observations"]["vehicles"][0]["object_id"] = "other"
        rows[10] = row(10)
        self.assertEqual(collect(rows=rows)["motion_note"]["maneuver"], "unknown")

    def test_occluded_contact_cannot_support_direction(self):
        rows = [row(i) for i in range(41)]
        rows[20]["observations"]["vehicles"].append(dict(object_id="near", class_name="car",
            bbox_xyxy=[290, 270, 360, 350], source_frame=20))
        self.assertEqual(collect(rows=rows)["motion_note"]["reason"], "contact_occluded")

    def test_repeated_frames_rejected_and_missing_mask_is_not_hidden(self):
        with MotionNotes(True) as m:
            m.observe(row(0))
            self.assertEqual(m.diagnostic["geometry_source"], "missing_mask")
            with self.assertRaises(ValueError):
                m.observe(row(0))

    def test_track_gap_jump_motion_failure_and_frame_gap(self):
        for failure in ("track_gap", "track_jump", "motion_gap", "frame_gap"):
            rows = [row(i) for i in range(41)]
            if failure == "track_gap":
                for r in rows[20:25]:
                    r["observations"]["vehicles"] = []
            elif failure == "track_jump":
                rows[20]["observations"]["vehicles"][0]["bbox_xyxy"] = [10, 10, 40, 50]
            elif failure == "motion_gap":
                rows[20]["postprocessing"]["ego_motion"]["valid"] = False
            else:
                del rows[20]
            with self.subTest(failure=failure):
                note = collect(rows=rows)["motion_note"]
                self.assertEqual(note["maneuver"], "unknown")
                self.assertIn(note["reason"], (failure, "motion_gap_or_frame_gap"))

    def test_no_lane_fallback_is_explicit_and_short_eof_is_unknown(self):
        note = collect(masks=False)["motion_note"]
        self.assertEqual(note["maneuver"], "straight")
        self.assertEqual(note["method"], "ego_compensated_track_only")
        self.assertFalse(note["evidence"]["lane_support"])
        self.assertEqual(collect(rows=[row(i) for i in range(18)])["motion_note"]["maneuver"], "unknown")
        rows = [row(i) for i in range(41)]
        for r in rows:
            del r["postprocessing"]["road_marking_source"]
        self.assertFalse(collect(rows=rows)["motion_note"]["evidence"]["lane_support"])
        for kind in ("left_turn", "right_turn"):
            self.assertEqual(collect(kind, masks=False)["motion_note"]["maneuver"], kind)

    def test_cached_markings_are_propagated_not_reobserved(self):
        with MotionNotes(True) as m:
            m.observe(row(0), mask_for())
            r = row(1)
            r["postprocessing"]["road_marking_source"].update(direct=False, source_frame=0)
            m.observe(r, np.zeros((360, 640), np.uint8))
            self.assertEqual(m.diagnostic["geometry_source"], "propagated")
            self.assertEqual(m.diagnostic["source_frame"], 0)
            self.assertFalse(m.diagnostic["direct"])
            self.assertTrue(m.diagnostic["boundaries"])
            r = row(2)
            r["postprocessing"]["road_marking_source"].update(direct=False, source_frame=0, aligned=False)
            m.observe(r)
            self.assertFalse(m.diagnostic["boundaries"])

    def test_stopline_is_not_lane_and_one_side_is_unknown(self):
        mask = mask_for()
        mask[mask == 4] = 3
        self.assertEqual(extract_boundaries(mask, 640, 360), [])
        mask = mask_for(); mask[:, 390:] = 0
        boundaries = extract_boundaries(mask, 640, 360)
        self.assertIsNone(local_lane(boundaries, [305, 260, 335, 300], 640, 360))
        with self.assertRaises(ValueError):
            extract_boundaries(np.zeros((10, 10), np.uint8), 640, 360)

    def test_fifo_and_summary(self):
        with MotionNotes(True) as m:
            e, other = event(), dict(event(), rule_id="crosswalk_stopping", event_id="other")
            for i in range(41):
                m.observe(row(i), mask_for())
                output = m.submit([e, other] if i == 10 else [])
                if i < 40:
                    self.assertEqual(output, [])
            self.assertEqual([e["event_id"] for e in output], ["clip:red:v1:10", "other"])
            self.assertNotIn("motion_note", output[1])
            self.assertIn("直行（車道與軌跡推估", format_event_summary(output[0]))

    def test_online_and_two_offline_entries_match(self):
        import run_pipeline
        import evaluate_violations
        from frame_processing import FrameProcessor
        from model_library.base import ModelResult
        from model_library.registry import STAGES
        from tools.rule_eval import run_rules

        def factory(*args, **kwargs):
            return Mock(rules=[], evaluate=lambda r: [event()] if r["frame"]["index"] == 10 else [], finalize=lambda: [])
        def inject(self, image, index, timestamp, results, **kwargs):
            return Mock(record=row(index), marking_result=ModelResult([], {"mask": mask_for()}))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/"clip.mp4"; source.write_bytes(b"fake")
            config = root/"config.json"; config.write_text('{"version":1,"models":{}}')
            cap = Mock(); cap.isOpened.return_value = True
            cap.get.side_effect = lambda k: {cv2.CAP_PROP_FPS: 10, cv2.CAP_PROP_FRAME_WIDTH: 640,
                cv2.CAP_PROP_FRAME_HEIGHT: 360, cv2.CAP_PROP_FRAME_COUNT: 41}.get(k, 0)
            cap.read.side_effect = [(True, np.zeros((360, 640, 3), np.uint8))]*41+[(False, None)]
            pipeline = Mock(models={}, infer=Mock(return_value={s: ModelResult() for s in STAGES}))
            args = run_pipeline.parse_args([str(source), "--model-config", str(config), "--output-dir", str(root/"online"),
                "--device", "cpu", "--rules", "all", "--no-violation-clips", "--motion-notes", "--save-masks", str(root/"online/masks")])
            with patch("run_pipeline.cv2.VideoCapture", return_value=cap), patch.object(FrameProcessor, "process", inject), \
                 patch("run_pipeline.ViolationEngine", side_effect=factory), patch("run_pipeline.DetectionCsv"), ExitStack() as resources:
                run_pipeline.run(args, resources, pipeline=pipeline)
            jsonl = root/"online/clip_detections.jsonl"
            with patch("tools.rule_eval.ViolationEngine", side_effect=factory):
                result = run_rules(jsonl, root/"rules", "clip", motion_notes=True)
            with patch("evaluate_violations.ViolationEngine", side_effect=factory), patch("sys.argv", ["evaluate_violations.py",
                str(jsonl), "--output", str(root/"evaluated.jsonl"), "--rules", "all", "--no-violation-clips", "--motion-notes"]):
                evaluate_violations.main()
            online = [json.loads(l) for l in (root/"online/clip_violations.jsonl").read_text(encoding="utf-8").splitlines()]
            evaluated = [json.loads(l) for l in (root/"evaluated.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(online, result)
            self.assertEqual(evaluated, result)
            self.assertEqual(result[0]["motion_note"]["maneuver"], "straight")


if __name__ == "__main__":
    unittest.main()
