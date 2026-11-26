"""4cb 召回選項：燈號主燈位置（signal_spatial_main）、紅燈沿用時間設定（signal_hold_seconds）、雙線壓線疑似
（straddle_presence_seconds）。三項預設關閉，只產生疑似。"""
import json
import tempfile
import unittest
from pathlib import Path

from double_lines import RULE_DEFAULTS, validate_rule_settings
from signal_state import CombinedSignalState, TemporalSignalState
from test_double_line_rules import run_records, sequence
from violation_engine import red_light_rule_options

W, H = 1000, 500


def light(color, x, y, confidence=.8, size=16):
    return dict(class_name=color, confidence=confidence, bbox_xyxy=[x, y, x + size, y + size])


def run_signal(frames, **kwargs):
    state = CombinedSignalState(30, **kwargs)
    out = None
    for index, lights in enumerate(frames):
        out = state.update(lights, index)
    return out, state


class SpatialMainSignalTests(unittest.TestCase):
    # 主燈（紅）在畫面上方中央，側邊（左緣、較低）有穩定的綠燈：原判定衝突→未知。
    conflict = [[light("red", 492, 140), light("green", 40, 230)] for _ in range(20)]

    def test_default_reports_unknown_on_conflict(self):
        state, tracker = run_signal(self.conflict)
        self.assertIsNone(state)
        self.assertEqual(tracker.evidence["reason"], "conflicting_signal_tracks")

    def test_central_red_is_weak_red(self):
        state, tracker = run_signal(self.conflict, spatial_main=(W, H))
        self.assertEqual((state, tracker.strength), ("red", "weak"))
        self.assertEqual(tracker.evidence["reason"], "spatial_main_signal")
        self.assertEqual(tracker.evidence["conflicting_colors"], ["green", "red"])

    def test_central_green_keeps_original_behaviour(self):
        # 主燈是綠燈、側邊紅燈（行人號誌）：不採用位置判定，結果與未啟用相同。
        frames = [[light("green", 492, 140), light("red", 40, 230)] for _ in range(20)]
        self.assertEqual(run_signal(frames, spatial_main=(W, H))[0], run_signal(frames)[0])

    def test_overrides_selected_green_continuity(self):
        # 先只有綠燈（成為 selected 主燈），之後中央出現紅燈、綠燈仍在：延續會判綠，位置判定改為 weak 紅燈。
        frames = [[light("green", 40, 230)] for _ in range(10)] + self.conflict
        self.assertEqual(run_signal(frames, selected_continuity=True)[0], "green")
        state, tracker = run_signal(frames, selected_continuity=True, spatial_main=(W, H))
        self.assertEqual((state, tracker.strength), ("red", "weak"))

    def test_no_conflict_is_unchanged(self):
        frames = [[light("red", 492, 140)] for _ in range(10)]
        state, tracker = run_signal(frames, spatial_main=(W, H))
        self.assertEqual((state, tracker.strength), ("red", "strong"))
        plain = TemporalSignalState(30)
        for index, lights in enumerate(frames):
            plain.update(lights, index)
        self.assertEqual(plain.evidence["reason"], "temporal_support")


class RedLightConfigTests(unittest.TestCase):
    def options(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "red.json"
            path.write_text(json.dumps({"version": 1, **data}), encoding="utf-8")
            return red_light_rule_options(path)

    def test_spatial_main_and_hold_are_configurable(self):
        self.assertEqual(self.options({"signal_spatial_main": True, "signal_hold_seconds": 5}),
                         {"signal_spatial_main": True, "signal_hold_seconds": 5})
        for bad in ({"signal_spatial_main": 1}, {"signal_hold_seconds": 0}, {"signal_hold_seconds": "3"}):
            with self.assertRaises(ValueError):
                self.options(bad)


def straddling(frames, height=40, color="yellow"):
    """一出現就壓在雙線線帶上（沒有起點側），停留 frames 幀（10 fps）。"""
    records = sequence([(155, 200)] * frames, color)
    for record in records:
        vehicle = record["observations"]["vehicles"][0]
        x1, y1, x2, y2 = vehicle["bbox_xyxy"]
        vehicle["bbox_xyxy"] = [x1, y2 - height, x2, y2]
    return records


class StraddlePresenceTests(unittest.TestCase):
    ON = {"straddle_presence_seconds": 1.0}

    def test_off_by_default(self):
        self.assertEqual(RULE_DEFAULTS["straddle_presence_seconds"], 0.0)
        self.assertEqual(run_records(straddling(20), legacy_margin=False), [])
        with self.assertRaises(ValueError):
            validate_rule_settings({"straddle_presence_seconds": 1.0, "direction_side_margin_px": 0.0})

    def test_vehicle_already_on_the_band_is_suspected_once(self):
        for color in ("yellow", "white"):
            events = run_records(straddling(40, color=color), color, settings=self.ON, legacy_margin=False)
            self.assertEqual(len(events), 1, color)
            event = events[0]
            self.assertEqual((event["status"], event["behavior"]), ("suspected", "line_straddle"))
            self.assertFalse(event["conditions"]["complete_band_crossing"])
            self.assertFalse(event["conditions"]["stable_origin"])
            self.assertEqual(event["timestamp_sec"], 0.0)  # 開始壓線的時間
            self.assertGreaterEqual(event["evidence"]["straddle"]["span_sec"], 1.0)

    def test_short_straddle_and_small_vehicle_are_ignored(self):
        self.assertEqual(run_records(straddling(8), settings=self.ON, legacy_margin=False), [])
        self.assertEqual(run_records(straddling(20, height=15), settings=self.ON, legacy_margin=False), [])

    def test_mostly_off_band_samples_are_ignored(self):
        # 側別來回跳動：線帶與一側交替，線帶樣本不到一半。
        points = [(155, 200) if i % 3 == 0 else (125, 200) for i in range(30)]
        self.assertEqual(run_records(sequence(points), settings=self.ON, legacy_margin=False), [])


if __name__ == "__main__":
    unittest.main()
