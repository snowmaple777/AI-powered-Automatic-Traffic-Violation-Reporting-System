"""4am：雙線曲線延伸、近端延伸與線段記憶（皆預設關閉，使用時事件只能是疑似）。"""
import unittest

import numpy as np

from double_lines import OCCLUSION_OPTION_KEYS, _extend_line, validate_rule_settings
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


if __name__ == "__main__":
    unittest.main()
