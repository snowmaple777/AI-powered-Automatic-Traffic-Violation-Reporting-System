import copy
import unittest

import numpy as np

from area_rules import crosswalk_boundary_evidence, settings_for
from test_area_rules import records, evaluate


class CrosswalkBoundaryTests(unittest.TestCase):
    def test_boundary_rejects_unstable_overlap_but_preserves_interior(self):
        rows = records(count=25)
        for row in rows:
            row['observations']['traffic_regions'][0]['polygon'] = [[55,40],[100,40],[100,90],[55,90]]
        self.assertTrue(evaluate(rows))  # 原足跡恰有一半在區域中。
        self.assertFalse(evaluate(rows, {'crosswalk_boundary_guard': True, 'occlusion_gap_ratio': 0}))
        self.assertTrue(evaluate(records(count=25), {'crosswalk_boundary_guard': True}))

    def test_occlusion_is_separate_from_interior(self):
        area = np.asarray([[0,40],[50,40],[50,80],[0,80]], np.float32)
        footprint = np.asarray([[48,55],[65,55],[65,70],[48,70]], np.float32)
        result = crosswalk_boundary_evidence(area, footprint, [45,20,70,75], .25, .5)
        self.assertEqual(result['support'], 'occlusion_estimated')
        self.assertLess(result['direct_min_overlap'], .5)
        self.assertGreaterEqual(result['estimated_min_overlap'], .5)
        self.assertEqual(crosswalk_boundary_evidence(area, footprint, [45,20,70,75], 0, .5)['support'],
                         'boundary_uncertain')

    def test_resolution_guard_does_not_affect_waiting_area(self):
        self.assertFalse(evaluate(records(), {'crosswalk_min_footprint_px': 21}))
        self.assertTrue(evaluate(records(), {'crosswalk_min_footprint_px': 8}))
        rows = records('motorcycle_waiting_area')
        before = evaluate(rows)
        after = evaluate(rows, {'crosswalk_min_footprint_px': 100, 'crosswalk_boundary_guard': True})
        self.assertEqual([(e['object_id'], e['timestamp_sec']) for e in before],
                         [(e['object_id'], e['timestamp_sec']) for e in after])

    def test_disabled_is_exact_and_observations_unchanged(self):
        rows = records(count=25)
        original = copy.deepcopy(rows)
        self.assertEqual(evaluate(rows), evaluate(rows, {'crosswalk_boundary_guard': False}))
        evaluate(rows, {'crosswalk_boundary_guard': True})
        self.assertEqual(rows, original)

    def test_invalid_options(self):
        for value in (0, -1, True, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                settings_for({'crosswalk_min_footprint_px': value})
        with self.assertRaises(ValueError):
            settings_for({'crosswalk_boundary_guard': 1})


if __name__ == '__main__':
    unittest.main()
