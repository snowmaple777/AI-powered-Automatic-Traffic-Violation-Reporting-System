"""標線降頻：跳過的幀依鏡頭運動推移前一幀的遮罩。"""
import unittest

import numpy as np

from frame_processing import FrameProcessor
from model_library.base import FrameContext, ModelResult
from model_library.builtin import MarkingModel
from road_compensation import MotionEstimate

CLASSES = ["background", "box junction", "crosswalk", "stop line"]


class FakeSegmenter:
    classes, min_pixels, palette = CLASSES, 10, [[0, 0, 0]] * 4

    def __init__(self):
        self.calls = 0

    def infer(self, frame):
        self.calls += 1
        mask = np.zeros((120, 200), np.uint8)
        mask[60:64, 40:120] = 3
        return mask, [{"class_id": 3, "class_name": "stop line", "pixels": int((mask == 3).sum()), "components": []}]


def marking_model(interval):
    model = MarkingModel.__new__(MarkingModel)
    model.interval, model.backend, model.cached = interval, FakeSegmenter(), None
    return model


def results_for(marking):
    empty = ModelResult()
    return {"vehicles": empty, "traffic_lights": empty, "road_markings": marking, "depth": empty,
            "plates": empty, "ocr": empty}


class MarkingIntervalTests(unittest.TestCase):
    def test_model_infers_only_on_interval_frames(self):
        model = marking_model(2)
        sources = [model.infer(None, FrameContext(i), {}).artifacts["source_frame"] for i in range(5)]
        self.assertEqual(sources, [0, 0, 2, 2, 4])
        self.assertEqual(model.backend.calls, 3)

    def run_frames(self, motions):
        model = marking_model(2)
        processor = FrameProcessor(30, 200, 120, ["road_markings"])
        outputs = []
        for index, motion in enumerate(motions):
            marking = model.infer(None, FrameContext(index), {})
            processor.ego_motion = None
            result = processor.process(None, index, index / 30, results_for(marking),
                                       motion_future=_Done(motion))
            outputs.append(result)
        return outputs

    def test_skipped_frame_mask_is_warped_by_camera_motion(self):
        shift = np.array([[1, 0, 5], [0, 1, 7], [0, 0, 1]], float)  # 前幀→當幀：右 5、下 7 像素
        out = self.run_frames([MotionEstimate(False, reason="first_frame"), MotionEstimate(True, shift, "accepted")])
        fresh, skipped = (o.marking_result.artifacts["mask"] for o in out)
        np.testing.assert_array_equal(skipped[67:71, 45:125], 3)
        self.assertEqual(int((skipped == 3).sum()), int((fresh == 3).sum()))
        self.assertEqual(out[1].record["postprocessing"]["road_marking_source"],
                         dict(source_frame=0, propagated=True, warped=True))
        self.assertEqual(out[1].road_markings[0]["class_name"], "stop line")
        self.assertNotIn("road_marking_source", out[0].record["postprocessing"])

    def test_invalid_motion_reuses_previous_mask_unwarped(self):
        out = self.run_frames([MotionEstimate(False, reason="first_frame"),
                               MotionEstimate(False, reason="poor_spatial_coverage")])
        np.testing.assert_array_equal(out[1].marking_result.artifacts["mask"], out[0].marking_result.artifacts["mask"])
        self.assertFalse(out[1].record["postprocessing"]["road_marking_source"]["warped"])


class _Done:
    def __init__(self, motion):
        self.motion = motion

    def result(self):
        return (self.motion, None), 0.0


if __name__ == "__main__":
    unittest.main()
