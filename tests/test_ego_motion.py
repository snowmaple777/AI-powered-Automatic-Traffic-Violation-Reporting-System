import json
import unittest

import cv2
import numpy as np

from core.motion import homography, road_motion
from road_compensation import CompensationConfig, EgoMotionTracker, RoadMarkingCompensator


def textured_frames(count, shift=(3, 1), size=(360, 640)):
    rng = np.random.default_rng(0)
    base = cv2.GaussianBlur((rng.random((size[0] + 80, size[1] + 80, 3)) * 255).astype(np.uint8), (0, 0), 1.5)
    return [np.ascontiguousarray(base[40 - k * shift[1]:40 - k * shift[1] + size[0],
                                      40 - k * shift[0]:40 - k * shift[0] + size[1]]) for k in range(count)]


def stop_line(y):
    return [{"class_id": 3, "class_name": "stop line", "pixels": 1, "components": [
        {"area": 900, "bbox_xywh": [200, y, 150, 6], "line_xyxy": [200, y + 3, 350, y + 3], "centroid": [275, y + 3]}]}]


class EgoMotionTests(unittest.TestCase):
    def test_compensator_with_shared_motion_matches_internal_estimate(self):
        config = CompensationConfig()
        internal, shared, ego = RoadMarkingCompensator(config), RoadMarkingCompensator(config), EgoMotionTracker(config)
        valid = 0
        for index, frame in enumerate(textured_frames(8)):
            markings = stop_line(250 + index) if index != 5 else []
            kwargs = dict(frame_index=index, timestamp_sec=index / 30, road_markings=markings, vehicles=[])
            motion, _ = ego.update(frame, frame_index=index, timestamp_sec=index / 30, vehicles=[])
            a = internal.process(frame, **kwargs)
            b = shared.process(frame, motion=motion, **kwargs)
            self.assertEqual(json.dumps(a.diagnostics, sort_keys=True), json.dumps(b.diagnostics, sort_keys=True))
            self.assertEqual(json.dumps(a.compensated_markings), json.dumps(b.compensated_markings))
            valid += motion.valid
        self.assertGreater(valid, 4)

    def test_discontinuity_resets_both_in_the_same_way(self):
        ego = EgoMotionTracker(CompensationConfig())
        frames = textured_frames(3)
        ego.update(frames[0], frame_index=0, timestamp_sec=0, vehicles=[])
        motion, reason = ego.update(frames[1], frame_index=5, timestamp_sec=5 / 30, vehicles=[])
        self.assertEqual((motion.valid, motion.reason, reason), (False, "first_frame", "discontinuous_input"))

    def test_old_records_fall_back_to_compensation_motion(self):
        old = {"postprocessing": {"road_marking_compensation": {"motion": {"valid": True, "matrix": np.eye(3).tolist()}}}}
        new = {"postprocessing": {"ego_motion": {"valid": False}, "road_marking_compensation": old["postprocessing"]["road_marking_compensation"]}}
        self.assertTrue(road_motion(old)["valid"])
        self.assertFalse(road_motion(new)["valid"])
        self.assertIsNotNone(homography(road_motion(old)))
        self.assertIsNone(homography({"valid": True, "matrix": [[1, 0], [0, 1]]}))
        self.assertIsNone(homography({"valid": True, "matrix": np.eye(3).tolist(), "reprojection_error_px": 9}, max_error_px=5))


if __name__ == "__main__":
    unittest.main()
