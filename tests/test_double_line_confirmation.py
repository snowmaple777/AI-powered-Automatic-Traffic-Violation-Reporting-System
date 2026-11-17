"""完整跨越的短量測缺口；保留線帶直接觀測與原側／另一側等必要條件。"""
import unittest
from unittest.mock import patch
import violation_engine
from test_double_line_rules import LANE_CHANGE, run_records, sequence


class ShortGapTests(unittest.TestCase):
    def run_gap(self, missing, **settings):
        original = violation_engine.vehicle_line_position
        def position(vehicle, line, cfg, error=0):
            return None if vehicle['source_frame'] in missing else original(vehicle, line, cfg, error)
        with patch('violation_engine.vehicle_line_position', side_effect=position):
            return run_records(sequence(LANE_CHANGE), settings=settings)

    def test_short_gap_with_observed_transition_confirms(self):
        # 第 5 幀缺失，中心進入線帶的第 4 幀仍有直接觀測。
        self.assertEqual(self.run_gap({5})[0]['status'], 'suspected')
        e = self.run_gap({5}, confirm_short_position_gap=True, emit_suspected=False)[0]
        self.assertEqual(e['status'], 'confirmed')
        self.assertEqual(e['confirmation_basis'], ['short_position_gap'])
        self.assertFalse(e['conditions']['uninterrupted_positions'])

    def test_missing_transition_or_long_gap_cannot_confirm(self):
        for missing in ({4}, {4, 5}, {3, 4, 5, 6}):
            self.assertFalse(any(e['status'] == 'confirmed' for e in self.run_gap(missing, confirm_short_position_gap=True)))


if __name__ == '__main__':
    unittest.main()
