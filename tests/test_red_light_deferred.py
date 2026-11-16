"""4av 闖紅燈延後確認：R2 停止線交接、R3 斑馬線完整通過、R4 鏡頭運動失效後的乾淨越線（預設關閉）。"""
import json
import tempfile
import unittest
from pathlib import Path

from violation_engine import RedLightStopLineCrossingRule, RuleContext, red_light_rule_options
try:  # unittest discover -s tests 時測試資料夾在 sys.path 上；從專案根目錄以模組名執行時則需 tests. 前綴
    from test_red_light_rule import LINE_A, frame, tilted_line
except ModuleNotFoundError:
    from tests.test_red_light_rule import LINE_A, frame, tilted_line

ROOT = Path(__file__).resolve().parents[1]
# 菱形斑馬線：近端（下緣）擬合在 y≈666.7、遠端（上緣）在 y≈633.3，兩端點唯一，邊緣乾淨。
CROSSWALK = [[800, 650], [1000, 600], [1200, 650], [1000, 700]]


def run(frames, **options):
    rule = RedLightStopLineCrossingRule(RuleContext("t", 25, 1920, 1080), **options)
    live = []
    for item in frames:
        live += rule.evaluate(item)
    return live, live + rule.finalize()


class LineHandoffTests(unittest.TestCase):
    def frames(self, old_direct=True):
        old = dict(LINE_A) if old_direct else dict(LINE_A, compensation={
            "id": "comp-a", "rule_eligible": True, "direct_rule_eligible": False})
        frames = [frame(i, 760, [old]) for i in range(10)] + [frame(10, 760, [tilted_line(2)])]
        return frames + [frame(i, 640, [tilted_line(2)]) for i in range(11, 15)]

    def test_handoff_between_directly_observed_lines_can_confirm_when_enabled(self):
        _, events = run(self.frames())
        self.assertEqual([e["status"] for e in events], ["suspected"])
        self.assertNotIn("confirmation_basis", events[0])
        self.assertNotIn("stop_line_handoff_direct", events[0]["conditions"])
        live, events = run(self.frames(), confirm_line_handoff=True)
        self.assertEqual([e["status"] for e in live], ["confirmed"])
        self.assertEqual(len(events), 1)
        self.assertEqual(live[0]["confirmation_basis"], ["line_handoff_direct"])
        self.assertFalse(live[0]["conditions"]["same_stop_line"])
        self.assertTrue(live[0]["conditions"]["stop_line_handoff_direct"])
        self.assertEqual(live[0]["evidence"]["first_after_line_frame"], 11)

    def test_handoff_from_a_line_without_direct_observation_stays_suspected(self):
        _, events = run(self.frames(old_direct=False), confirm_line_handoff=True)
        self.assertEqual([e["status"] for e in events], ["suspected"])
        self.assertFalse(events[0]["conditions"]["stop_line_handoff_direct"])
        self.assertEqual(events[0]["confirmation_basis"], [])

    def test_plain_crossing_reports_standard_basis(self):
        frames = [frame(i, 760, [LINE_A]) for i in range(10)] + [frame(i, 640, [LINE_A]) for i in range(10, 15)]
        live, _ = run(frames, confirm_line_handoff=True)
        self.assertEqual(live[0]["confirmation_basis"], ["standard"])
        self.assertNotIn("stop_line_handoff_direct", live[0]["conditions"])


class CrosswalkCompletionTests(unittest.TestCase):
    def frames(self, bottoms, marking_source=None):
        frames = []
        for i, bottom in enumerate(bottoms):
            item = frame(i, bottom, [])
            region = {"region_id": "cw", "kind": "crosswalk", "predicted": False, "polygon": CROSSWALK}
            if marking_source:
                region["marking_source"] = marking_source
            item["observations"]["traffic_regions"] = [region]
            frames.append(item)
        return frames

    def test_vehicle_past_the_far_edge_confirms_only_when_enabled(self):
        bottoms = [760] * 10 + [610] * 5
        _, events = run(self.frames(bottoms))
        self.assertEqual([e["status"] for e in events], ["suspected"])
        self.assertNotIn("far_line_xyxy", events[0]["evidence"]["stop_line"])
        live, events = run(self.frames(bottoms), confirm_crosswalk_proxy=True)
        self.assertEqual([e["status"] for e in live], ["confirmed"])
        self.assertEqual(len(events), 1)
        event = live[0]
        self.assertEqual(event["confirmation_basis"], ["crosswalk_crossed_completely"])
        self.assertFalse(event["conditions"]["stop_line_observed"])
        self.assertTrue(event["conditions"]["crosswalk_crossed_completely"])
        self.assertEqual(event["evidence"]["stop_line"]["proxy"], "crosswalk_near_edge")

    def test_vehicle_stopping_inside_the_crosswalk_stays_suspected(self):
        _, events = run(self.frames([760] * 10 + [650] * 5), confirm_crosswalk_proxy=True)
        self.assertEqual([e["status"] for e in events], ["suspected"])
        self.assertFalse(events[0]["conditions"]["crosswalk_crossed_completely"])

    def test_propagated_crosswalk_does_not_confirm(self):
        _, events = run(self.frames([760] * 10 + [610] * 5, marking_source="propagated"),
                        confirm_crosswalk_proxy=True)
        self.assertEqual([e["status"] for e in events], ["suspected"])

    def test_alternating_direct_and_propagated_frames_still_count_direct_ones(self):
        # 標線每 2 幀推論：推移幀不計入也不歸零，兩次直接觀測越過遠端即可確認。
        frames = self.frames([760] * 10 + [610] * 6)
        for item in frames[10:]:
            if item["frame"]["index"] % 2:
                item["observations"]["traffic_regions"][0]["marking_source"] = "propagated"
        live, _ = run(frames, confirm_crosswalk_proxy=True)
        self.assertEqual([e["status"] for e in live], ["confirmed"])
        self.assertEqual(live[0]["frame"], 12)

    def test_vehicle_at_the_crosswalk_tip_does_not_confirm(self):
        frames = self.frames([760] * 10 + [610] * 5)
        for item in frames:  # 車框中心移到 x=815，落在近、遠端邊緣（800–1200）左側 10% 內
            vehicle = item["observations"]["vehicles"][0]
            bottom = vehicle["bbox_xyxy"][3]
            vehicle["bbox_xyxy"] = [790, bottom - 150, 840, bottom]
            vehicle["bottom_center"] = [815, bottom]
        _, events = run(frames, confirm_crosswalk_proxy=True)
        self.assertTrue(all(e["status"] == "suspected" for e in events))

    def test_crosswalk_moving_under_a_still_vehicle_does_not_confirm(self):
        # 車不動（底邊 700，在近端前），斑馬線多邊形每幀往下移 15px（同一條代理線軌跡）：
        # 相對位置先越過近端、再「通過遠端」，但車本身沒有前進。
        frames = self.frames([700] * 21)
        for item in frames:
            shift = max(0, item["frame"]["index"] - 7) * 15
            item["observations"]["traffic_regions"][0]["polygon"] = [[x, y + shift] for x, y in CROSSWALK]
        _, events = run(frames, confirm_crosswalk_proxy=True)
        self.assertEqual([e["status"] for e in events], ["suspected"])
        self.assertTrue(events[0]["conditions"]["crosswalk_crossed_completely"])

    def test_far_edge_must_be_reached_within_the_deferred_window(self):
        bottoms = [760] * 10 + [650] * 10 + [610] * 5
        live, _ = run(self.frames(bottoms), confirm_crosswalk_proxy=True)
        self.assertEqual([e["status"] for e in live], ["confirmed"])
        self.assertEqual(live[0]["evidence"]["first_after_line_frame"], 10)
        _, events = run(self.frames(bottoms), confirm_crosswalk_proxy=True, deferred_confirmation_seconds=0.2)
        self.assertEqual([e["status"] for e in events], ["suspected"])


class MotionGapRecoveryTests(unittest.TestCase):
    def frames(self, gap_frames, reobserved=True):
        frames, last_direct = [], 0
        for i in range(15):
            if reobserved or not gap_frames or i < min(gap_frames):
                last_direct = i
            line = dict(LINE_A, compensation={"id": "c1", "rule_eligible": True, "direct_rule_eligible": True,
                                              "last_direct_observed_frame": last_direct})
            item = frame(i, 760 if i < 10 else 640, [line])
            item["postprocessing"]["road_marking_compensation"] = {"reset_reason": None,
                                                                   "motion_gap": i in gap_frames}
            frames.append(item)
        return frames

    def test_gap_before_the_crossing_is_tolerated_when_enabled(self):
        _, events = run(self.frames({5, 6}))
        self.assertEqual([e["status"] for e in events], ["suspected"])
        live, _ = run(self.frames({5, 6}), confirm_after_motion_gap=True)
        self.assertEqual([e["status"] for e in live], ["confirmed"])
        self.assertEqual(live[0]["confirmation_basis"], ["clean_crossing_after_motion_gap"])
        self.assertFalse(live[0]["conditions"]["camera_motion_compensated"])
        self.assertTrue(live[0]["conditions"]["clean_crossing_after_motion_gap"])

    def test_gap_at_the_last_before_observation_stays_suspected(self):
        _, events = run(self.frames({9, 10, 11}), confirm_after_motion_gap=True)
        self.assertEqual([e["status"] for e in events], ["suspected"])
        self.assertFalse(events[0]["conditions"]["clean_crossing_after_motion_gap"])

    def test_line_must_be_directly_observed_after_the_gap(self):
        _, events = run(self.frames({5, 6}, reobserved=False), confirm_after_motion_gap=True)
        self.assertEqual([e["status"] for e in events], ["suspected"])


class SingleBasisTests(unittest.TestCase):
    def test_handoff_and_motion_gap_together_stay_suspected(self):
        frames = []
        for i in range(15):
            if i < 10:
                line = dict(LINE_A)
            else:
                line = tilted_line(2)
                line["compensation"] = dict(line["compensation"], last_direct_observed_frame=i)
            item = frame(i, 760 if i < 10 else 640, [line])
            item["postprocessing"]["road_marking_compensation"] = {"reset_reason": None, "motion_gap": i in (5, 6)}
            frames.append(item)
        _, events = run(frames, confirm_line_handoff=True, confirm_after_motion_gap=True)
        self.assertEqual([e["status"] for e in events], ["suspected"])
        conditions = events[0]["conditions"]
        self.assertTrue(conditions["stop_line_handoff_direct"] and conditions["clean_crossing_after_motion_gap"])


class ConfigTests(unittest.TestCase):
    def test_shipped_config_keeps_previous_behavior(self):
        options = red_light_rule_options(ROOT / "configs/red_light_rules.json")
        self.assertFalse(any(options[key] for key in ("confirm_line_handoff", "confirm_crosswalk_proxy",
                                                       "confirm_after_motion_gap")))
        frames = [frame(i, 760, [LINE_A]) for i in range(10)] + [frame(i, 640, [LINE_A]) for i in range(10, 15)]
        live, _ = run(frames, **options)
        self.assertEqual([e["status"] for e in live], ["confirmed"])
        self.assertNotIn("confirmation_basis", live[0])
        self.assertNotIn("first_after_line_frame", live[0]["evidence"])

    def test_invalid_values_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "red.json"
            for data in ({"version": 1, "confirm_line_handoff": 1}, {"version": 1, "deferred_confirmation_seconds": 0},
                         {"version": 1, "unknown_option": True}, {"version": 2}):
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(ValueError):
                    red_light_rule_options(path)
        with self.assertRaises(ValueError):
            RedLightStopLineCrossingRule(RuleContext("t", 25, 1920, 1080), deferred_confirmation_seconds=float("nan"))


if __name__ == "__main__":
    unittest.main()
