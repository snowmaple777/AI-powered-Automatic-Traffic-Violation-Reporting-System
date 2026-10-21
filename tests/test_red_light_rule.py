import math
import unittest

from violation_engine import RedLightStopLineCrossingRule, RuleContext

LINE_A = {"area": 2000, "bbox_xywh": [800, 700, 400, 10], "line_xyxy": [800, 705, 1200, 705],
          "centroid": [1000, 705]}
_SLOPE = math.tan(math.radians(8))


def tilted_line(offset):
    """與 LINE_A 在 x=1075 處相距 offset 像素、傾斜 8 度且不會被一般配對吸收的線段。"""
    y = lambda x: 705 + offset + _SLOPE * (x - 1075)
    return {"area": 2000, "bbox_xywh": [950, round(y(950)), 800, round(y(1750) - y(950))],
            "line_xyxy": [950, y(950), 1750, y(1750)], "centroid": [1350, y(1350)],
            "compensation": {"id": "comp-b", "rule_eligible": True}}


def frame(index, bottom, lines, source_frame=None):
    vehicle = {"object_id": "vehicle-000001", "class_name": "car", "confidence": .9,
               "bbox_xyxy": [900, bottom - 150, 1100, bottom], "bottom_center": [1000, bottom],
               "track_age_frames": index + 20,
               "source_frame": index if source_frame is None else source_frame}
    return {"frame": {"index": index, "timestamp_sec": index / 25, "width": 1920, "height": 1080, "fps": 25},
            "observations": {"vehicles": [vehicle], "rule_vehicles": [vehicle],
                             "traffic_lights": [{"class_name": "red", "confidence": .9,
                                                 "bbox_xyxy": [100, 100, 120, 140]}],
                             "road_markings": [{"class_name": "stop line", "components": lines}]},
            "postprocessing": {}}


class RedLightRuleTests(unittest.TestCase):
    def run_rule(self, frames):
        rule = RedLightStopLineCrossingRule(RuleContext("t", 25, 1920, 1080))
        events = []
        for item in frames:
            events += rule.evaluate(item)
        return rule, events, events + rule.finalize()

    def test_fresh_crossing_is_confirmed_once(self):
        frames = [frame(i, 760, [LINE_A]) for i in range(10)]
        frames += [frame(i, 640, [LINE_A]) for i in range(10, 15)]
        _, live, everything = self.run_rule(frames)
        self.assertEqual([e["status"] for e in live], ["confirmed"])
        self.assertEqual(live[0]["frame"], 11)
        self.assertTrue(live[0]["conditions"]["same_stop_line"])
        self.assertEqual(len(everything), 1)

    def test_cached_box_does_not_count_as_second_after_observation(self):
        frames = [frame(i, 760, [LINE_A]) for i in range(10)]
        frames += [frame(10, 640, [LINE_A]), frame(11, 640, [LINE_A], source_frame=10)]
        rule, live, everything = self.run_rule(frames)
        self.assertEqual(live, [])  # 舊版會在第 11 幀以快取框湊滿兩次線後觀測而確認
        self.assertEqual([e["status"] for e in everything], ["suspected"])
        self.assertIn("cached_detection", rule.diagnostics["vehicles"][0]["blocked_by"])

    def test_reassociated_line_can_only_be_suspected(self):
        frames = [frame(i, 760, [LINE_A]) for i in range(10)]
        frames += [frame(10, 760, [tilted_line(2)])]
        frames += [frame(i, 640, [tilted_line(2)]) for i in range(11, 15)]
        _, live, everything = self.run_rule(frames)
        self.assertEqual(live, [])
        self.assertEqual(len(everything), 1)
        event = everything[0]
        self.assertEqual(event["status"], "suspected")
        self.assertFalse(event["conditions"]["same_stop_line"])
        self.assertIsNotNone(event["evidence"]["line_reassociated_from"])

    def test_distant_line_is_not_reassociated(self):
        # 12px 在舊門檻（1.5% 畫面高 = 16px）內會交接，新門檻（0.8% ≈ 8.6px）拒絕。
        frames = [frame(i, 760, [LINE_A]) for i in range(10)]
        frames += [frame(10, 760, [tilted_line(12)])]
        frames += [frame(i, 640, [tilted_line(12)]) for i in range(11, 15)]
        _, _, everything = self.run_rule(frames)
        self.assertTrue(all(e["evidence"]["line_reassociated_from"] is None for e in everything))
        self.assertTrue(all(not e["conditions"]["before_line_observed"] for e in everything))


if __name__ == "__main__":
    unittest.main()
