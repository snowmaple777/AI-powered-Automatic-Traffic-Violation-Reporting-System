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


def occluded(item, bottom):
    """在車輛前方（較近）加入一台遮住其底邊的車。"""
    blocker = {"object_id": "vehicle-000099", "class_name": "car", "confidence": .9,
               "bbox_xyxy": [850, bottom - 100, 1150, bottom + 80], "bottom_center": [1000, bottom + 80],
               "track_age_frames": 0, "source_frame": item["frame"]["index"]}
    item["observations"]["vehicles"] = item["observations"]["vehicles"] + [blocker]
    return item


class OccludedContactTests(unittest.TestCase):
    """車框底邊被較近的車遮住時，底邊不是接地點，不能用來判定越線。"""

    def run_rule(self, frames, **options):
        rule = RedLightStopLineCrossingRule(RuleContext("t", 25, 1920, 1080), **options)
        events = []
        for item in frames:
            events += rule.evaluate(item)
        return rule, events + rule.finalize()

    def frames(self):
        before = [frame(i, 760, [LINE_A]) for i in range(10)]
        return before + [occluded(frame(i, 640, [LINE_A]), 640) for i in range(10, 15)]

    def test_skip_mode_ignores_occluded_after_observations(self):
        rule, events = self.run_rule(self.frames())
        self.assertEqual(events, [])
        self.assertIn("contact_point_occluded", rule.diagnostics["vehicles"][0]["blocked_by"])

    def test_suspected_mode_blocks_confirmation(self):
        _, events = self.run_rule(self.frames(), occluded_contact="suspected")
        self.assertEqual([e["status"] for e in events], ["suspected"])
        self.assertFalse(events[0]["conditions"]["contact_point_visible"])

    def test_off_restores_previous_behavior_and_visible_contact_still_confirms(self):
        _, events = self.run_rule(self.frames(), occluded_contact="off")
        self.assertEqual([e["status"] for e in events], ["confirmed"])
        frames = [frame(i, 760, [LINE_A]) for i in range(10)] + [frame(i, 640, [LINE_A]) for i in range(10, 15)]
        _, events = self.run_rule(frames)
        self.assertEqual([e["status"] for e in events], ["confirmed"])
        self.assertTrue(events[0]["conditions"]["contact_point_visible"])

    def test_duplicate_detection_and_far_vehicles_do_not_count_as_occluders(self):
        box = [900, 490, 1100, 640]
        same = {"object_id": "vehicle-000099", "class_name": "truck", "bbox_xyxy": [898, 492, 1102, 650]}
        behind = {"object_id": "vehicle-000098", "class_name": "car", "bbox_xyxy": [850, 300, 1150, 600]}
        person = {"object_id": "vehicle-000097", "class_name": "person", "bbox_xyxy": [850, 540, 1150, 720]}
        self.assertFalse(RedLightStopLineCrossingRule._contact_occluded(box, "vehicle-000001", [same, behind, person]))
        self.assertTrue(RedLightStopLineCrossingRule._contact_occluded(
            box, "vehicle-000001", [{"object_id": "x", "class_name": "bus", "bbox_xyxy": [800, 560, 1000, 720]}]))

    def test_invalid_option_is_rejected(self):
        with self.assertRaises(ValueError):
            RedLightStopLineCrossingRule(RuleContext("t", 25, 1920, 1080), occluded_contact="maybe")


class CrosswalkProxyTests(unittest.TestCase):
    """某台車沒有重疊的真正停止線時，改用斑馬線近端邊緣作代理線（只產生疑似）。"""

    def frames(self):
        far_line = {"area": 2000, "bbox_xywh": [1500, 700, 300, 10], "line_xyxy": [1500, 705, 1800, 705],
                    "centroid": [1650, 705]}  # 別的車道的停止線，不與車框重疊
        frames = [frame(i, 760, [far_line]) for i in range(10)] + [frame(i, 640, [far_line]) for i in range(10, 15)]
        for item in frames:
            item["observations"]["traffic_regions"] = [{"region_id": "cw", "kind": "crosswalk", "predicted": False,
                "polygon": [[850, 680], [1150, 680], [1150, 700], [850, 700]]}]
        return frames

    def run_rule(self, frames, **options):
        rule = RedLightStopLineCrossingRule(RuleContext("t", 25, 1920, 1080), **options)
        events = []
        for item in frames:
            events += rule.evaluate(item)
        return events + rule.finalize()

    def test_per_vehicle_proxy_is_used_when_no_stop_line_overlaps_the_vehicle(self):
        events = self.run_rule(self.frames())
        self.assertEqual([e["status"] for e in events], ["suspected"])
        self.assertEqual(events[0]["evidence"]["stop_line"]["proxy"], "crosswalk_near_edge")
        self.assertFalse(events[0]["conditions"]["stop_line_observed"])
        # 舊行為：畫面上還有其他停止線時不使用代理線。
        self.assertEqual(self.run_rule(self.frames(), crosswalk_proxy_mode="no_stop_line"), [])
        with self.assertRaises(ValueError):
            self.run_rule(self.frames(), crosswalk_proxy_mode="always")

    def test_real_overlapping_stop_line_takes_precedence_over_the_proxy(self):
        frames = self.frames()
        for item in frames:
            item["observations"]["road_markings"][0]["components"].append(dict(LINE_A))
        events = self.run_rule(frames)
        self.assertEqual([e["status"] for e in events], ["confirmed"])
        self.assertIsNone(events[0]["evidence"]["stop_line"].get("proxy"))


class ProxyRestrictionTests(unittest.TestCase):
    """斑馬線代理線的疑似事件需要紅燈證據，且不能是橫向移動的車（2026-11-09 長片誤報修正）。"""

    def frames(self, lights=True, shift=0):
        frames = []
        for i in range(15):
            item = frame(i, 760 if i < 10 else 640, [])
            vehicle = item["observations"]["vehicles"][0]
            x = 900 + shift * i
            vehicle["bbox_xyxy"] = [x, vehicle["bbox_xyxy"][1], x + 200, vehicle["bbox_xyxy"][3]]
            vehicle["bottom_center"] = [x + 100, vehicle["bottom_center"][1]]
            if not lights:
                item["observations"]["traffic_lights"] = []
            item["observations"]["traffic_regions"] = [{"region_id": "cw", "kind": "crosswalk", "predicted": False,
                "polygon": [[0, 680], [1920, 680], [1920, 700], [0, 700]]}]
            frames.append(item)
        return frames

    def run_rule(self, frames, **options):
        rule = RedLightStopLineCrossingRule(RuleContext("t", 25, 1920, 1080), **options)
        events = []
        for item in frames:
            events += rule.evaluate(item)
        return rule, events + rule.finalize()

    def test_proxy_with_red_and_forward_motion_is_suspected(self):
        _, events = self.run_rule(self.frames())
        self.assertEqual([e["status"] for e in events], ["suspected"])
        self.assertEqual(events[0]["evidence"]["stop_line"]["proxy"], "crosswalk_near_edge")

    def test_proxy_without_any_red_is_not_reported(self):
        rule, events = self.run_rule(self.frames(lights=False))
        self.assertEqual(events, [])
        self.assertIn("proxy_without_red", rule.diagnostics["vehicles"][0]["blocked_by"])
        _, legacy = self.run_rule(self.frames(lights=False), proxy_requires_red=False)
        self.assertEqual([e["status"] for e in legacy], ["suspected"])

    def test_laterally_moving_vehicle_is_not_reported_on_a_proxy(self):
        rule, events = self.run_rule(self.frames(shift=30))
        self.assertEqual(events, [])
        self.assertIn("lateral_motion", rule.diagnostics["vehicles"][0]["blocked_by"])
        _, legacy = self.run_rule(self.frames(shift=30), proxy_exclude_lateral=False)
        self.assertEqual([e["status"] for e in legacy], ["suspected"])


class SuspectedVetoTests(unittest.TestCase):
    """疑似事件：越線前後 0.5 秒內出現綠燈、或越線後退回停止線前，都不輸出。"""

    @staticmethod
    def lights(frames, color, indices):
        for item in frames:
            if item["frame"]["index"] in indices:
                item["observations"]["traffic_lights"] = [
                    {"class_name": color, "confidence": .9, "bbox_xyxy": [100, 100, 120, 140]}]
        return frames

    def run_rule(self, frames, **options):
        rule = RedLightStopLineCrossingRule(RuleContext("t", 25, 1920, 1080), **options)
        events = []
        for item in frames:
            events += rule.evaluate(item)
        return rule, events + rule.finalize()

    def unknown_signal_crossing(self, after=(10, 15)):
        # 燈號全程無法判定（沒有燈號框）：只能產生疑似事件。
        frames = [frame(i, 760, [LINE_A]) for i in range(10)] + [frame(i, 640, [LINE_A]) for i in range(*after)]
        for item in frames:
            item["observations"]["traffic_lights"] = []
        return frames

    def test_baseline_unknown_signal_crossing_is_suspected(self):
        _, events = self.run_rule(self.unknown_signal_crossing())
        self.assertEqual([e["status"] for e in events], ["suspected"])

    def test_green_shortly_after_crossing_cancels_pending_suspected(self):
        frames = self.unknown_signal_crossing((10, 40))
        rule, events = self.run_rule(self.lights(frames, "green", range(20, 40)))  # 25 fps：約 0.4 秒後轉綠
        self.assertEqual(events, [])
        _, kept = self.run_rule(self.lights(self.unknown_signal_crossing((10, 40)), "green", range(30, 40)),
                                green_veto_seconds=0.5)  # 0.8 秒後才轉綠：保留
        self.assertEqual([e["status"] for e in kept], ["suspected"])
        _, disabled = self.run_rule(self.lights(self.unknown_signal_crossing((10, 40)), "green", range(20, 40)),
                                    green_veto_seconds=0)
        self.assertEqual([e["status"] for e in disabled], ["suspected"])

    def test_green_just_before_crossing_blocks_suspected(self):
        # 弱綠燈（信心 0.2）不會清除越線狀態，停用否決時會產生疑似；啟用時越線前 0.5 秒內有綠燈即不輸出。
        frames = self.unknown_signal_crossing()
        for item in frames:
            if item["frame"]["index"] < 9:
                item["observations"]["traffic_lights"] = [
                    {"class_name": "green", "confidence": .2, "bbox_xyxy": [100, 100, 120, 140]}]
        self.assertEqual([e["status"] for e in self.run_rule(frames, green_veto_seconds=0)[1]], ["suspected"])
        self.assertEqual(self.run_rule(frames)[1], [])

    def test_returning_behind_the_line_cancels_pending_suspected(self):
        frames = self.unknown_signal_crossing() + [frame(i, 760, [LINE_A]) for i in range(15, 20)]
        for item in frames:
            item["observations"]["traffic_lights"] = []
        rule, events = self.run_rule(frames)
        self.assertEqual(events, [])

    def test_confirmed_event_is_not_affected_by_later_green(self):
        frames = [frame(i, 760, [LINE_A]) for i in range(10)] + [frame(i, 640, [LINE_A]) for i in range(10, 30)]
        _, events = self.run_rule(self.lights(frames, "green", range(13, 30)))
        self.assertEqual([e["status"] for e in events], ["confirmed"])


if __name__ == "__main__":
    unittest.main()
