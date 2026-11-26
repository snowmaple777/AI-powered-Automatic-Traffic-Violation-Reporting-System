"""車輛影像軌跡：橫向移動判定與鏡頭運動補償。"""
import unittest

from core.trajectory import TrackHistory


def vehicle(x, bottom, index, identity="v1", source=None):
    return {"object_id": identity, "bbox_xyxy": [x, bottom - 50, x + 40, bottom],
            "source_frame": index if source is None else source}


class TrackHistoryTests(unittest.TestCase):
    def test_lateral_needs_large_horizontal_motion_relative_to_vertical(self):
        h = TrackHistory(3.0)
        for i in range(10):
            h.update(i * .1, i, [vehicle(100 + 5 * i, 300, i)])
        self.assertTrue(h.lateral("v1", .9, 1.0))       # dx 45 ≥ 20 且垂直不動
        forward = TrackHistory(3.0)
        for i in range(10):
            forward.update(i * .1, i, [vehicle(100 + 5 * i, 300 - 5 * i, i)])
        self.assertFalse(forward.lateral("v1", .9, 1.0))  # dx 45 < 2 × dy 45

    def test_cached_boxes_and_old_points_are_ignored(self):
        h = TrackHistory(1.0)
        h.update(0, 0, [vehicle(100, 300, 0)])
        h.update(.1, 1, [vehicle(500, 300, 1, source=0)])   # 快取框不是新觀測
        self.assertIsNone(h.displacement("v1", .1, 1.0))
        h.update(2.0, 20, [vehicle(110, 300, 20)])
        self.assertIsNone(h.displacement("v1", 2.0, 1.0))  # 1 秒前的點已過期

    def test_projection_removes_camera_pan(self):
        h = TrackHistory(3.0)
        for i in range(10):
            # 鏡頭每幀右移 5 px：車在影像中左移 5 px，補償後實際不動
            h.project(lambda x, y: (x - 5, y))
            h.update(i * .1, i, [vehicle(100 - 5 * i, 300, i)])
        self.assertAlmostEqual(h.displacement("v1", .9, 1.0)["dx"], 0)
        self.assertFalse(h.lateral("v1", .9, 1.0))


if __name__ == "__main__":
    unittest.main()
