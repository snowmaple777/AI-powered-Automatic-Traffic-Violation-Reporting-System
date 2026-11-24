"""斑馬線候選解析度門檻（4ax crosswalk_min_footprint_px；4bh 起預設設定檔啟用 8px）。"""
import copy
import unittest

from area_rules import settings_for
from test_area_rules import records, evaluate


class CrosswalkResolutionTests(unittest.TestCase):
    def test_resolution_guard_does_not_affect_waiting_area(self):
        self.assertFalse(evaluate(records(), {'crosswalk_min_footprint_px': 21}))
        self.assertTrue(evaluate(records(), {'crosswalk_min_footprint_px': 8}))
        rows = records('motorcycle_waiting_area')
        before = evaluate(rows)
        after = evaluate(rows, {'crosswalk_min_footprint_px': 100})
        self.assertEqual([(e['object_id'], e['timestamp_sec']) for e in before],
                         [(e['object_id'], e['timestamp_sec']) for e in after])

    def test_observations_unchanged(self):
        rows = records(count=25)
        original = copy.deepcopy(rows)
        evaluate(rows, {'crosswalk_min_footprint_px': 8})
        self.assertEqual(rows, original)

    def test_invalid_options(self):
        for value in (0, -1, True, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                settings_for({'crosswalk_min_footprint_px': value})
        for removed in ('crosswalk_boundary_guard', 'tolerate_overlap_dips'):  # 4bh 移除的實驗選項
            with self.assertRaises(ValueError):
                settings_for({removed: True})


if __name__ == '__main__':
    unittest.main()
