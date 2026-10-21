"""停等區事件併入同一台車的闖紅燈事件。"""
import unittest

from violation_engine import ViolationEngine


class StubRule:
    def __init__(self, rule_id, schedule):
        self.rule_id, self.schedule, self.diagnostics = rule_id, schedule, {}

    def evaluate(self, observation):
        self.diagnostics = {}
        index = observation["frame"]["index"]
        return [dict(event_id=f"{self.rule_id}-{index}-{obj}", rule_id=self.rule_id, object_id=obj,
                     timestamp_sec=index / 10, frame=index) for obj in self.schedule.get(index, [])]

    def finalize(self):
        return []


def run(waiting, red, frames=80, enable_red=True):
    rules = [StubRule("car_motorcycle_waiting_area", waiting)]
    if enable_red:
        rules.append(StubRule("red_light_stop_line_crossing", red))
    engine = ViolationEngine(rules)
    events, diagnostics = [], []
    for i in range(frames):
        events += engine.evaluate({"frame": {"index": i, "timestamp_sec": i / 10}})
        diagnostics.append(rules[0].diagnostics)
    return events + engine.finalize(), diagnostics


class EventMergeTests(unittest.TestCase):
    def test_waiting_area_before_red_light_is_absorbed(self):
        events, _ = run({20: ["v1"]}, {34: ["v1"]})
        self.assertEqual([e["rule_id"] for e in events], ["red_light_stop_line_crossing"])
        self.assertEqual([e["event_id"] for e in events[0]["merged_events"]], ["car_motorcycle_waiting_area-20-v1"])

    def test_same_frame_and_red_light_first(self):
        events, _ = run({20: ["v1"]}, {20: ["v1"]})
        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0]["merged_events"]), 1)
        events, diagnostics = run({25: ["v1"]}, {10: ["v1"]})
        self.assertEqual([e["rule_id"] for e in events], ["red_light_stop_line_crossing"])
        self.assertNotIn("merged_events", events[0])
        self.assertEqual(diagnostics[25]["merged_into"], [dict(event_id="car_motorcycle_waiting_area-25-v1",
                                                               into_event_id="red_light_stop_line_crossing-10-v1")])

    def test_other_vehicle_window_expiry_and_disabled_red_rule_are_not_merged(self):
        events, _ = run({20: ["v2"]}, {22: ["v1"]})
        self.assertEqual(sorted(e["rule_id"] for e in events),
                         ["car_motorcycle_waiting_area", "red_light_stop_line_crossing"])
        waiting = next(e for e in events if e["rule_id"] == "car_motorcycle_waiting_area")
        self.assertAlmostEqual(waiting["released_timestamp_sec"], 5.1)  # 保留 3 秒後輸出
        events, _ = run({10: ["v1"]}, {45: ["v1"]})  # 相隔 3.5 秒，超過窗口
        self.assertEqual(len(events), 2)
        events, _ = run({10: ["v1"]}, {}, frames=15)  # 影片結束時保留中的事件照常輸出
        self.assertEqual([e["rule_id"] for e in events], ["car_motorcycle_waiting_area"])
        self.assertNotIn("released_timestamp_sec", events[0])
        events, _ = run({10: ["v1"]}, {}, enable_red=False)
        self.assertEqual(len(events), 1)
        self.assertNotIn("released_timestamp_sec", events[0])


if __name__ == "__main__":
    unittest.main()
