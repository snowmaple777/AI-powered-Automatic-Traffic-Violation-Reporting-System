"""4ca 斑馬線違停模式（crosswalk_parking_mode）：不看燈號、不做橫移排除、累積靜止；只產生疑似，預設關閉。"""
import unittest

from area_rules import settings_for
from test_area_rules import evaluate, records

PARKING = {"crosswalk_parking_mode": True, "stationary_window_seconds": .5}


def green(rows):
    for row in rows:
        row["observations"]["traffic_lights"] = [dict(class_name="green", confidence=.9, bbox_xyxy=[10, 10, 20, 30])]
    return rows


def shift(row, dx):
    vehicle = row["observations"]["vehicles"][0]
    x = vehicle["bbox_xyxy"][0] + dx
    vehicle["bbox_xyxy"] = [x, 20, x + 20, 70]
    vehicle["road_geometry"]["footprint"] = [[x, 50], [x + 20, 50], [x + 20, 70], [x, 70]]


def jittery():
    """停在斑馬線上 3 秒，但第 1.0、2.0 秒各有一幀被推移誤差拉開 6 px（半徑 4 px）：每段連續靜止都不滿 1 秒。"""
    rows = records(count=30)
    for i in (10, 20):
        shift(rows[i], 6)
    return rows


class CrosswalkParkingModeTests(unittest.TestCase):
    def test_off_by_default_and_validated(self):
        self.assertNotIn("crosswalk_parking_mode", settings_for())
        self.assertNotIn("crosswalk_parking_mode", settings_for({"crosswalk_parking_mode": False}))
        with self.assertRaises(ValueError):
            settings_for({"crosswalk_parking_mode": "yes"})
        with self.assertRaises(ValueError):
            settings_for({"crosswalk_parking_mode": True})  # 需要 stationary_window_seconds
        # 未啟用時事件與原規則相同（沒有新條件）
        self.assertNotIn("parking_mode_candidate", evaluate(records(), {"stationary_window_seconds": .5})[0]["conditions"])

    def test_reports_parked_car_during_green_as_suspected(self):
        self.assertEqual(evaluate(green(records(count=25)), {"stationary_window_seconds": .5}), [])
        events = evaluate(green(records(count=25)), PARKING)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["status"], "suspected")
        self.assertTrue(event["conditions"]["parking_mode_candidate"])
        self.assertFalse(event["conditions"]["signal_checked"])
        self.assertAlmostEqual(event["evidence"]["parking_stationary_sec"], 1.0)
        self.assertEqual(event["evidence"]["measurement_name"], "footprint_overlap_ratio")

    def test_lateral_arrival_is_not_excluded(self):
        rows = records(count=30)
        for i, row in enumerate(rows):
            shift(row, min(0, -100 + 10 * i))  # 前 10 幀由左側橫向駛入，之後停在斑馬線上
        self.assertEqual(evaluate(rows, {"stationary_window_seconds": .5}), [])
        self.assertEqual(len(evaluate(rows, PARKING)), 1)

    def test_stillness_accumulates_across_brief_jitter(self):
        self.assertEqual(evaluate(jittery(), {"stationary_window_seconds": .5}), [])
        events = evaluate(jittery(), PARKING)
        self.assertEqual(len(events), 1)
        self.assertAlmostEqual(events[0]["timestamp_sec"], 2.6)
        self.assertAlmostEqual(events[0]["evidence"]["parking_stationary_sec"], 1.0)

    def test_moving_vehicle_never_accumulates(self):
        self.assertEqual(evaluate(records(count=30, moving=3), PARKING), [])
        self.assertEqual(evaluate(green(records(count=30, moving=3)), PARKING), [])

    def test_no_duplicate_when_original_path_reports_first(self):
        events = evaluate(records(count=40), PARKING)
        self.assertEqual(len(events), 1)
        self.assertNotIn("parking_mode_candidate", events[0]["conditions"])  # 原路徑 1.0 秒先成立

    def test_original_path_wins_within_the_hold(self):
        # 綠燈只在前 0.5 秒：違停 1.5 秒先成立、保留到 4.5 秒；原路徑在綠燈 3 秒後（約 3.5 秒）報告同一台車，
        # 以原路徑為準（它之後還可能被確認），違停事件丟棄。
        rows = records(count=60)
        green(rows[:5])
        events = evaluate(rows, PARKING)
        self.assertEqual(len(events), 1)
        self.assertNotIn("parking_mode_candidate", events[0]["conditions"])
        self.assertGreater(events[0]["timestamp_sec"], 3.4)

    def test_parking_events_are_never_confirmed(self):
        settings = dict(PARKING, confirm_crosswalk=True)
        events = evaluate(green(records(count=40)), settings)
        self.assertEqual([e["status"] for e in events], ["suspected"])
        self.assertTrue(events[0]["conditions"]["parking_mode_candidate"])


if __name__ == "__main__":
    unittest.main()
