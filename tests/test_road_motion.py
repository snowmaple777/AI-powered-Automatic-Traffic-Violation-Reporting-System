"""Background registration must ignore the stationary dashcam foreground."""
import unittest

import cv2
import numpy as np

from road_compensation.contracts import CompensationConfig
from road_compensation.motion import RoadMotionEstimator, road_mask


class RoadMotionTests(unittest.TestCase):
    def test_masked_dashboard_preserves_measured_background_translation(self):
        rng = np.random.default_rng(71)
        background = rng.integers(0, 256, (360, 640), dtype=np.uint8)
        background = cv2.GaussianBlur(background, (5, 5), 0)
        moved = cv2.warpAffine(background, np.float32([[1, 0, 4], [0, 1, 2]]), (640, 360))
        # A stationary camera hood must not force the road transform to identity.
        hood = rng.integers(0, 256, (100, 640), dtype=np.uint8)
        background[260:] = hood
        moved[260:] = hood
        cfg = CompensationConfig(roi_top=.3, roi_bottom=.72)
        mask = road_mask(background.shape, [], cfg.roi_top, cfg.roi_bottom)
        result = RoadMotionEstimator(cfg).estimate(background, moved, mask, mask)
        self.assertTrue(result.valid, result.reason)
        samples = np.float32([[[200, 150], [400, 220]]])
        actual = cv2.perspectiveTransform(samples, result.matrix)
        np.testing.assert_allclose(actual, samples + [4, 2], atol=.7)
        self.assertFalse(mask[260:].any())

    def test_cut_and_featureless_background_do_not_create_motion(self):
        cfg = CompensationConfig(roi_top=.3, roi_bottom=.73)
        estimator = RoadMotionEstimator(cfg)
        rng = np.random.default_rng(17)
        first = rng.integers(0, 256, (360, 640), dtype=np.uint8)
        cut = rng.integers(0, 256, (360, 640), dtype=np.uint8)
        mask = road_mask(first.shape, [], cfg.roi_top, cfg.roi_bottom)
        self.assertFalse(estimator.estimate(first, cut, mask, mask).valid)
        self.assertFalse(estimator.estimate(first * 0, first * 0, mask, mask).valid)

    def test_invalid_motion_region_and_feature_budget_fail_early(self):
        for values in ({"roi_bottom": .3}, {"roi_bottom": 1.1},
                       {"roi_bottom": float("nan")}, {"motion_max_corners": 5},
                       {"motion_max_corners": 100.5}, {"motion_min_distance": 0}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                CompensationConfig(**values)
