"""評估指標：同一時段多台車時，配對優先採用車牌相符的事件。"""
import unittest

from eval.metrics import evaluate


def event(time, plate=None, vehicle_class="car"):
    return {"rule_id": "car_motorcycle_waiting_area", "timestamp_sec": time, "vehicle_class": vehicle_class,
            "evidence": {"vehicle": {"plate_text": plate}}}


class MetricsTests(unittest.TestCase):
    def test_plate_match_wins_over_closer_time(self):
        truth = {"time_tolerance_sec": 1.0, "videos": {"v": {"category": "car_motorcycle_waiting_area", "reviewed": True,
                 "events": [{"rule_id": "car_motorcycle_waiting_area", "start_sec": 0, "end_sec": 18, "plate": "ALY5102"}]}}}
        _, per_rule, plates, correct = evaluate(truth, {"v": [event(9.0), event(1.5, "ALY-5102")]})
        self.assertEqual(per_rule["car_motorcycle_waiting_area"], {"tp": 1, "fp": 1, "fn": 0})
        self.assertEqual((plates, correct), (1, 1))

    def test_without_plates_the_closest_time_is_used(self):
        truth = {"time_tolerance_sec": 1.0, "videos": {"v": {"category": "car_motorcycle_waiting_area", "reviewed": True,
                 "events": [{"rule_id": "car_motorcycle_waiting_area", "start_sec": 4, "end_sec": 6, "plate": None}]}}}
        _, per_rule, _, _ = evaluate(truth, {"v": [event(3.5), event(5.2)]})
        self.assertEqual(per_rule["car_motorcycle_waiting_area"], {"tp": 1, "fp": 1, "fn": 0})


class ExclusionTests(unittest.TestCase):
    def test_excluded_videos_are_not_counted(self):
        truth = {"time_tolerance_sec": 1.0, "videos": {"v": {"category": "crosswalk_stopping", "reviewed": True,
                 "excluded": True, "events": [{"rule_id": "crosswalk_stopping", "start_sec": 0, "end_sec": 1, "plate": None}]}}}
        rows, per_rule, _, _ = evaluate(truth, {"v": []})
        self.assertEqual((rows, per_rule), ([], {}))


if __name__ == "__main__":
    unittest.main()
