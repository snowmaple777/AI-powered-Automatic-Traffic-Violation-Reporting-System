"""4am／4an／4ao：雙線遮擋延伸、線段記憶、起點容忍與壓線（皆預設關閉，使用時事件只能是疑似）。"""
import unittest

import numpy as np

from double_lines import (ORIGIN_OPTION_KEYS, OCCLUSION_OPTION_KEYS, STRADDLE_OPTION_KEYS, _extend_line,
                          validate_rule_settings)
from violation_engine import RuleContext, create_rules
from test_double_line_rules import LANE_CHANGE, run_records, sequence


def trim(records, keep):
    """依車底位置裁掉每幀雙線的一部分：keep(point_y, vehicle_bottom) 為 True 的點才保留。"""
    for record, (_, y) in zip(records, LANE_CHANGE):
        line = record["observations"]["double_lines"][0]
        for part in [line] + line["members"]:
            for key in ("centerline", "left_boundary", "right_boundary"):
                part[key] = [p for p in part[key] if keep(p[1], y)] or part[key][-2:]
    return records


class OcclusionOptionTests(unittest.TestCase):
    def test_defaults_stay_out_of_settings_and_event_parameters(self):
        rule = create_rules(["double_yellow_line_crossing"], RuleContext("test", 10, 400, 300))[0]
        self.assertFalse(set(OCCLUSION_OPTION_KEYS) & set(rule.settings))
        event = run_records(sequence(LANE_CHANGE))[0]
        self.assertFalse(set(OCCLUSION_OPTION_KEYS) & set(event["evidence"]["parameters"]))
        enabled = run_records(sequence(LANE_CHANGE), settings={"curve_extrapolation": True})[0]
        self.assertIs(enabled["evidence"]["parameters"]["curve_extrapolation"], True)
        self.assertEqual(enabled["status"], "confirmed")  # 沒有用到延伸時，開啟選項不改變判定
        for bad in ({"curve_extrapolation": 1}, {"line_memory_seconds": -1}):
            with self.assertRaises(ValueError):
                validate_rule_settings(bad)

    def test_near_endpoint_inside_vehicle_box_is_extended_as_suspected(self):
        # 可見雙線止於車框內（車身遮住往鏡頭方向的延續），預設無法量測；開啟後可延伸，只能是疑似。
        records = trim(sequence(LANE_CHANGE), lambda py, y: py <= y - 4)
        self.assertEqual(run_records(records), [])
        events = run_records(records, settings={"extend_near_endpoint": True})
        self.assertEqual([e["status"] for e in events], ["suspected"])
        self.assertIn("line_observed_at_footprint", events[0]["missing_conditions"])
        # 近端在車框上方很遠（不是被這台車遮住）時不延伸。
        far = trim(sequence(LANE_CHANGE), lambda py, y: py <= y - 60)
        self.assertEqual(run_records(far, settings={"extend_near_endpoint": True}), [])

    def test_curve_extension_follows_curvature_and_falls_back_to_linear(self):
        ys = np.arange(100., 201., 4.)
        xs = 100 + .004 * (ys - 100) ** 2
        line = {key: np.column_stack((xs + offset, ys)).tolist()
                for key, offset in (("centerline", 0), ("left_boundary", -3), ("right_boundary", 3))}
        target, truth = 236., 100 + .004 * 136 ** 2
        curved = _extend_line(line, target, 60, curve=True)
        straight = _extend_line(line, target, 60)
        self.assertTrue(curved["curve_extrapolated"])
        self.assertLess(abs(curved["centerline"][-1][0] - truth), .5)
        self.assertGreater(abs(straight["centerline"][-1][0] - truth), 2)
        self.assertAlmostEqual(curved["left_boundary"][-1][0] + 3, curved["centerline"][-1][0])
        self.assertTrue(np.all(np.diff(np.asarray(curved["centerline"])[:, 1]) > 0))
        # 鋸齒狀（擬合殘差大）時退回直線外插。
        noisy = {key: [[x + (5 if i % 2 else -5), y] for i, (x, y) in enumerate(path)]
                 for key, path in line.items()}
        self.assertNotIn("curve_extrapolated", _extend_line(noisy, target, 60, curve=True))
        self.assertIsNone(_extend_line(line, target, 20, curve=True))  # 超過延伸上限

    def test_line_memory_bridges_brief_line_loss_as_suspected(self):
        records = sequence(LANE_CHANGE)
        records[4]["observations"]["double_lines"] = []
        self.assertEqual(run_records(records), [])
        events = run_records(records, settings={"line_memory_seconds": .5})
        self.assertEqual([e["status"] for e in events], ["suspected"])
        self.assertIn("directly_observed_pair", events[0]["missing_conditions"])
        self.assertEqual(run_records(records, settings={"line_memory_seconds": .05}), [])

    def test_line_memory_hands_off_to_new_track_after_gap(self):
        records = sequence(LANE_CHANGE)
        for record in records[4:6]:
            record["observations"]["double_lines"] = []
        for record in records[6:]:
            record["observations"]["double_lines"][0]["track_id"] = "line-2"
        self.assertEqual(run_records(records), [])
        events = run_records(records, settings={"line_memory_seconds": .5})
        self.assertEqual([e["status"] for e in events], ["suspected"])
        self.assertIn("same_line_track", events[0]["missing_conditions"])

    def test_remembered_line_never_seeds_new_candidates(self):
        # 線只在前兩幀出現；之後車輛才在其一側出現並越過記憶中的位置，不能由記憶建立起點。
        records = sequence(LANE_CHANGE)
        for record in records[:2]:
            record["observations"]["vehicles"] = []
        for record in records[2:]:
            record["observations"]["double_lines"] = []
        self.assertEqual(run_records(records, settings={"line_memory_seconds": 2.0}), [])


class OriginInterruptionTests(unittest.TestCase):
    TOLERANT = {"max_origin_interruption_sec": .1}
    TAIL = LANE_CHANGE[4:]  # 線帶內→另一側

    @staticmethod
    def statuses(events):
        return [(e["status"], e["missing_conditions"]) for e in events]

    def test_defaults_stay_out_of_settings(self):
        rule = create_rules(["double_yellow_line_crossing"], RuleContext("test", 10, 400, 300))[0]
        self.assertFalse(set(ORIGIN_OPTION_KEYS) & set(rule.settings))
        for bad in ({"max_origin_interruption_sec": -.1}, {"origin_interruption_min_confidence": 1.5}):
            with self.assertRaises(ValueError):
                validate_rule_settings(bad)

    def test_brief_band_edge_during_seeding_is_tolerated_as_suspected(self):
        points = [(125, 200), (152, 196), (125, 192), (125, 190), (140, 185)] + self.TAIL
        self.assertEqual(run_records(sequence(points)), [])
        self.assertEqual(self.statuses(run_records(sequence(points), settings=self.TOLERANT)),
                         [("suspected", ["stable_origin"])])
        # 連續中斷超過時限仍刪除候選。
        longer = [(125, 200), (152, 197), (152, 195), (152, 193), (125, 192), (125, 190), (140, 185)] + self.TAIL
        self.assertEqual(run_records(sequence(longer), settings=self.TOLERANT), [])

    def test_brief_low_confidence_during_seeding_respects_floor(self):
        points = [(125, 200), (125, 197), (125, 193), (125, 190), (140, 185)] + self.TAIL

        def records(confidence):
            items = sequence(points)
            items[1]["observations"]["vehicles"][0]["confidence"] = confidence
            return items

        self.assertEqual(run_records(records(.3)), [])
        self.assertEqual(self.statuses(run_records(records(.3), settings=self.TOLERANT)),
                         [("suspected", ["stable_origin"])])
        self.assertEqual(run_records(records(.2), settings=self.TOLERANT), [])
        self.assertEqual(run_records(records(.3), settings={**self.TOLERANT, "origin_interruption_min_confidence": .32}), [])

    def test_uninterrupted_crossing_stays_confirmed_with_tolerance_enabled(self):
        events = run_records(sequence(LANE_CHANGE), settings=self.TOLERANT)
        self.assertEqual([e["status"] for e in events], ["confirmed"])


class StraddleTests(unittest.TestCase):
    ON = {"straddle_min_seconds": .25}
    ORIGIN = [(125, 200), (125, 197), (125, 194)]
    BAND = [(140, 191), (154, 188), (154, 185), (154, 182), (154, 179)]  # 第一幀只在邊距；線帶內 0.3 秒、4 次
    BACK = [(125, 176), (125, 173), (125, 170), (125, 167)]
    CROSS = [(140, 164), (154, 161), (170, 158), (185, 155), (185, 152), (185, 149)]

    def test_defaults_stay_out_of_settings_and_emit_nothing(self):
        rule = create_rules(["double_yellow_line_crossing"], RuleContext("test", 10, 400, 300))[0]
        self.assertFalse(set(STRADDLE_OPTION_KEYS) & set(rule.settings))
        self.assertEqual(run_records(sequence(self.ORIGIN + self.BAND + self.BACK)), [])
        for bad in ({"straddle_min_seconds": -1}, {"straddle_merge_seconds": float("nan")}):
            with self.assertRaises(ValueError):
                validate_rule_settings(bad)

    def test_touch_and_return_is_reported_as_suspected_straddle(self):
        events = run_records(sequence(self.ORIGIN + self.BAND + self.BACK), settings=self.ON)
        self.assertEqual([(e["status"], e["behavior"]) for e in events], [("suspected", "line_straddle")])
        event = events[0]
        self.assertFalse(event["conditions"]["complete_band_crossing"])
        self.assertIn("stable_destination", event["missing_conditions"])
        self.assertEqual(event["evidence"]["maneuver"], "line_straddle")
        [episode] = event["evidence"]["straddle"]["episodes"]
        self.assertEqual((episode["end_reason"], episode["band_observations"]), ("returned_to_origin", 4))
        self.assertAlmostEqual(episode["band_seconds"], .3)
        self.assertEqual(event["frame"], 7)  # 與跨越事件相同：最後一次車底中心在線帶內的幀
        # 停留不足門檻時不輸出。
        self.assertEqual(run_records(sequence(self.ORIGIN + self.BAND + self.BACK),
                                     settings={"straddle_min_seconds": .5}), [])

    def test_touching_only_the_band_margin_is_not_a_straddle(self):
        margin = [(141, 191), (141, 188), (141, 185), (141, 182), (141, 179)]  # 車底右點只到邊距
        self.assertEqual(run_records(sequence(self.ORIGIN + margin + self.BACK), settings=self.ON), [])

    def test_vehicle_first_seen_on_line_is_not_a_straddle(self):
        points = [(154, 200), (154, 197), (154, 194), (154, 191)] + self.BACK
        self.assertEqual(run_records(sequence(points), settings=self.ON), [])

    def test_track_ending_inside_band_counts_with_or_without_detailed_diagnostics(self):
        for detailed in (False, True):
            events = run_records(sequence(self.ORIGIN + self.BAND),
                                 settings={**self.ON, "detailed_diagnostics": detailed})
            self.assertEqual([e["evidence"]["straddle"]["episodes"][0]["end_reason"] for e in events],
                             ["finalize"])

    def test_later_full_crossing_absorbs_pending_straddle(self):
        points = self.ORIGIN + self.BAND + self.BACK + self.CROSS
        events = run_records(sequence(points), settings=self.ON)
        self.assertEqual([e["evidence"]["maneuver"] for e in events], ["complete_crossing"])
        self.assertEqual(len(events[0]["evidence"]["merged_straddles"]), 1)
        # 等待期過後才完成跨越：分別輸出，壓線在前。
        events = run_records(sequence(points), settings={**self.ON, "straddle_merge_seconds": .2})
        self.assertEqual([e["behavior"] for e in events], ["line_straddle", "complete_crossing"])
        self.assertNotIn("merged_straddles", events[1]["evidence"])

    def test_repeated_straddles_merge_into_one_event(self):
        events = run_records(sequence(self.ORIGIN + self.BAND + self.BACK + self.BAND[:1] + [
            (154, 164), (154, 161), (154, 158), (154, 155)] + self.BACK), settings=self.ON)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0]["evidence"]["straddle"]["episodes"]), 2)

    def test_clean_crossing_is_unchanged_with_straddle_enabled(self):
        events = run_records(sequence(LANE_CHANGE), settings=self.ON)
        self.assertEqual([e["status"] for e in events], ["confirmed"])
        self.assertNotIn("merged_straddles", events[0]["evidence"])


if __name__ == "__main__":
    unittest.main()
