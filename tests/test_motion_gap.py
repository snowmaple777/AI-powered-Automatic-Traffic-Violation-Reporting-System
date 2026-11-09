"""停止線補償層：鏡頭運動短暫失效時以「不動」近似保留軌跡（allow_motion_gaps），期間越線只支持疑似。"""
import unittest

import numpy as np

from road_compensation import CompensationConfig, MotionEstimate, RoadMarkingCompensator
from violation_engine import RedLightStopLineCrossingRule, RuleContext
try:  # unittest discover -s tests 時測試資料夾在 sys.path 上；從專案根目錄以模組名執行時則需 tests. 前綴
    from test_red_light_rule import LINE_A, frame as red_frame
except ModuleNotFoundError:
    from tests.test_red_light_rule import LINE_A, frame as red_frame

STILL = MotionEstimate(True, np.eye(3), "stationary")
BROKEN = MotionEstimate(False, reason="poor_spatial_coverage")


def stop_line(y=253):
    return [{"class_id": 3, "class_name": "stop line", "pixels": 1, "components": [
        {"area": 900, "bbox_xywh": [200, y - 3, 150, 6], "line_xyxy": [200, y, 350, y], "centroid": [275, y]}]}]


class CompensatorMotionGapTests(unittest.TestCase):
    def run_frames(self, motions, observed=None, **config):
        compensator = RoadMarkingCompensator(CompensationConfig(**config))
        frame = np.zeros((360, 640, 3), np.uint8)
        out = []
        for index, motion in enumerate(motions):
            seen = observed is None or observed[index]
            result = compensator.process(frame, frame_index=index, timestamp_sec=index / 30,
                                         road_markings=stop_line() if seen else [], vehicles=[],
                                         motion=motion if index else None)
            components = [c for m in result.compensated_markings for c in m["components"]]
            out.append((result.diagnostics, components[0]["compensation"] if components else None))
        return out

    def test_default_still_resets_on_any_motion_failure(self):
        out = self.run_frames([None, STILL, STILL, BROKEN, STILL])
        self.assertEqual(out[3][0]["reset_reason"], "poor_spatial_coverage")
        self.assertFalse(out[3][0]["motion_gap"])
        self.assertEqual(out[3][1]["observed_frames"], 1)  # 舊行為：軌跡重來
        self.assertFalse(out[3][1]["rule_eligible"])

    def test_short_gap_keeps_the_observed_track_but_marks_it(self):
        motions = [None, STILL, STILL] + [BROKEN] * 4 + [STILL, STILL]
        out = self.run_frames(motions, allow_motion_gaps=True, max_motion_gap_seconds=.2)
        gap = out[3:7]
        self.assertTrue(all(d["motion_gap"] and d["reset_reason"] is None for d, _ in gap))
        self.assertEqual([c["observed_frames"] for _, c in out], list(range(1, 10)))
        self.assertTrue(all(c["motion_gap"] and c["rule_eligible"] and c["direct_rule_eligible"] for _, c in gap))
        self.assertNotIn("motion_gap", out[7][1])
        self.assertEqual(out[6][0]["motion_gap_seconds"], round(6 / 30 - 2 / 30, 5))

    def test_gap_longer_than_limit_resets(self):
        motions = [None, STILL, STILL] + [BROKEN] * 8
        out = self.run_frames(motions, allow_motion_gaps=True, max_motion_gap_seconds=.2)
        self.assertTrue(out[8][0]["motion_gap"])
        self.assertEqual(out[9][0]["reset_reason"], "poor_spatial_coverage")  # 9/30-2/30 > 0.2
        self.assertEqual(out[9][1]["observed_frames"], 1)

    def test_unobserved_line_during_gap_is_kept_but_not_given_to_rules(self):
        motions = [None, STILL, STILL, BROKEN, STILL]
        observed = [True, True, True, False, True]
        out = self.run_frames(motions, observed, allow_motion_gaps=True, max_occluded_seconds=.5,
                              max_missing_seconds=.1)
        predicted = out[3][1]
        self.assertEqual(predicted["source"], "predicted_missing")
        self.assertFalse(predicted["rule_eligible"] or predicted["direct_rule_eligible"])
        self.assertEqual(out[4][1]["observed_frames"], 4)  # 失效後同一條軌跡繼續累積

    def test_config_rejects_non_boolean_switch(self):
        with self.assertRaises(ValueError):
            CompensationConfig(allow_motion_gaps=1)
        with self.assertRaises(ValueError):
            CompensationConfig(max_motion_gap_seconds=0)


class RedLightMotionGapTests(unittest.TestCase):
    def crossing(self, gap_frames):
        line = dict(LINE_A, compensation={"id": "c1", "rule_eligible": True, "direct_rule_eligible": True})
        frames = [red_frame(i, 760, [line]) for i in range(10)] + [red_frame(i, 640, [line]) for i in range(10, 15)]
        for item in frames:
            gap = item["frame"]["index"] in gap_frames
            item["postprocessing"]["road_marking_compensation"] = {"reset_reason": None, "motion_gap": gap}
        return frames

    def run_rule(self, frames):
        rule = RedLightStopLineCrossingRule(RuleContext("t", 25, 1920, 1080))
        events = []
        for item in frames:
            events += rule.evaluate(item)
        return rule, events + rule.finalize()

    def test_crossing_during_motion_gap_is_only_suspected(self):
        rule, events = self.run_rule(self.crossing({9, 10, 11}))
        self.assertEqual([e["status"] for e in events], ["suspected"])
        self.assertFalse(events[0]["conditions"]["camera_motion_compensated"])
        self.assertIn("camera_motion_gap", rule.diagnostics["vehicles"][0]["blocked_by"])

    def test_motion_gap_still_clears_crosswalk_proxy_states(self):
        rule = RedLightStopLineCrossingRule(RuleContext("t", 25, 1920, 1080))
        rule.line_tracks = {1: dict(LINE_A, marking_id="stopline-0001", proxy="crosswalk_near_edge", last_frame=0,
                                    stable_frames=5, angle_degrees=0),
                            2: dict(LINE_A, marking_id="stopline-0002", last_frame=0, stable_frames=5, angle_degrees=0)}
        rule.crossing_states = {("v1", "stopline-0001"): {"phase": "before_line"},
                                ("v1", "stopline-0002"): {"phase": "before_line"}}
        rule._project_line_tracks({"postprocessing": {"road_marking_compensation": {"motion_gap": True}}})
        self.assertEqual(list(rule.line_tracks), [2])
        self.assertEqual(list(rule.crossing_states), [("v1", "stopline-0002")])

    def test_without_gap_the_same_crossing_is_confirmed(self):
        _, events = self.run_rule(self.crossing(set()))
        self.assertEqual([e["status"] for e in events], ["confirmed"])
        self.assertTrue(events[0]["conditions"]["camera_motion_compensated"])


if __name__ == "__main__":
    unittest.main()
