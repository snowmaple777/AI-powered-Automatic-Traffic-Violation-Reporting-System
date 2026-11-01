"""標線降頻：跳過的幀依鏡頭運動推移前一幀的遮罩。"""
import unittest
from unittest.mock import patch

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


def marking_model(interval, **options):
    with patch("model_library.builtin.RoadMarkingSegmenter", return_value=FakeSegmenter()):
        return MarkingModel(interval=interval, **options)


def results_for(marking):
    empty = ModelResult()
    return {"vehicles": empty, "traffic_lights": empty, "road_markings": marking, "depth": empty,
            "plates": empty, "ocr": empty}


class MarkingIntervalTests(unittest.TestCase):
    @staticmethod
    def activity(vehicles=False, lights=False):
        return {"vehicles": ModelResult([{"source_frame": 0}] if vehicles else []),
                "traffic_lights": ModelResult([{"class_name": "red"}] if lights else [])}

    def test_disabled_boost_matches_baseline_despite_activity(self):
        model = marking_model(2)
        sources = [model.infer(None, FrameContext(i, i / 30), self.activity(True, True))
                   .artifacts["source_frame"] for i in range(6)]
        self.assertEqual(sources, [0, 0, 2, 2, 4, 4])

    def test_activity_immediately_refreshes_and_keeps_baseline_anchors(self):
        for stage in ("vehicles", "traffic_lights"):
            model = marking_model(2, activity_boost=True, activity_hold_seconds=0)
            sources = []
            for i in range(6):
                upstream = self.activity()
                if i == 1:
                    upstream[stage] = ModelResult([{"source_frame": 0}])
                sources.append(model.infer(None, FrameContext(i, i / 30), upstream).artifacts["source_frame"])
            self.assertEqual(sources, [0, 1, 2, 2, 4, 4])
            self.assertEqual(model.schedule_diagnostics["extra_inferences"], 1)
            self.assertEqual(model.schedule_diagnostics["inferences"], 4)

    def test_hold_uses_video_seconds_and_reset_clears_state(self):
        model = marking_model(2, activity_boost=True, activity_hold_seconds=.25)
        sources = [model.infer(None, FrameContext(i, i / 10), self.activity(i == 1))
                   .artifacts["source_frame"] for i in range(6)]
        self.assertEqual(sources, [0, 1, 2, 3, 4, 4])
        self.assertEqual(sum(model.schedule_diagnostics["reasons"].values()), 6)
        model.reset()
        self.assertEqual(model.schedule_diagnostics["frames"], 0)
        self.assertIsNone(model.cached)
        self.assertEqual(model.infer(None, FrameContext(0, 0), self.activity()).artifacts["source_frame"], 0)

    def test_discontinuity_refreshes_on_off_cycle_frame(self):
        for index, timestamp, shape in ((3, .1, (20, 20, 3)),
                                       (1, 1., (20, 20, 3)),
                                       (1, 0., (20, 20, 3)),
                                       (1, .1, (30, 20, 3))):
            model = marking_model(2, activity_boost=True, activity_hold_seconds=0)
            model.infer(np.zeros((20, 20, 3)), FrameContext(0, 0), self.activity())
            result = model.infer(np.zeros(shape), FrameContext(index, timestamp), self.activity())
            self.assertEqual(result.artifacts["source_frame"], index)
            self.assertEqual(model.schedule_diagnostics["reasons"]["startup_or_discontinuity"], 2)

    def test_missing_upstream_refreshes_without_mutating_inputs(self):
        model = marking_model(2, activity_boost=True, activity_hold_seconds=0)
        model.infer(None, FrameContext(0, 0), {})
        upstream = {"vehicles": ModelResult()}
        result = model.infer(None, FrameContext(1, .1), upstream)
        self.assertEqual(result.artifacts["source_frame"], 1)
        self.assertEqual(list(upstream), ["vehicles"])
        self.assertEqual(upstream["vehicles"].records, [])

    def test_invalid_schedule_options_fail_before_model_load(self):
        with patch("model_library.builtin.RoadMarkingSegmenter") as backend:
            for options in ({"activity_boost": "true"}, {"refresh_on_invalid_motion": 1}, {"activity_hold_seconds": -1},
                            {"activity_hold_seconds": float("nan")},
                            {"activity_hold_seconds": float("inf")}, {"activity_hold_seconds": True}):
                with self.assertRaises(ValueError):
                    MarkingModel(**options)
            backend.assert_not_called()

    def test_motion_refresh_only_on_skipped_frames_with_invalid_or_unknown_motion(self):
        valid, broken = MotionEstimate(True, np.eye(3), "stationary"), MotionEstimate(False, reason="low_inlier_support")
        motions = {1: broken, 3: valid, 5: None}
        calls = []

        def probe(index):
            def ask():
                calls.append(index)
                return motions[index]
            return ask
        model = marking_model(2, refresh_on_invalid_motion=True)
        sources = [model.infer(None, FrameContext(i, i / 30, motion_probe=probe(i)), {}).artifacts["source_frame"]
                   for i in range(6)]
        self.assertEqual(sources, [0, 1, 2, 2, 4, 5])
        self.assertEqual(calls, [1, 3, 5])  # 固定推論幀不等待鏡頭運動
        stats = model.schedule_diagnostics
        self.assertEqual((stats["inferences"], stats["extra_inferences"]), (5, 2))
        self.assertEqual((stats["reasons"]["motion_invalid"], stats["reasons"]["motion_unknown"]), (1, 1))
        self.assertEqual(stats["mode"], "refresh_on_invalid_motion")
        no_probe = marking_model(2, refresh_on_invalid_motion=True)
        self.assertEqual([no_probe.infer(None, FrameContext(i, i / 30), {}).artifacts["source_frame"]
                          for i in range(3)], [0, 1, 2])
        model.reset()
        self.assertEqual((model.schedule_diagnostics["frames"], model.schedule_diagnostics["motion_wait_seconds"]), (0, 0.0))

    def test_motion_refresh_disabled_never_waits_for_motion(self):
        model = marking_model(2)
        ask = lambda: self.fail("停用時不得等待鏡頭運動")
        sources = [model.infer(None, FrameContext(i, i / 30, motion_probe=ask), {}).artifacts["source_frame"]
                   for i in range(4)]
        self.assertEqual(sources, [0, 0, 2, 2])
        self.assertFalse(model.schedule_enabled)

    def test_boost_flows_through_frame_processor_as_fresh_observation(self):
        model = marking_model(2, activity_boost=True, activity_hold_seconds=0)
        processor = FrameProcessor(30, 200, 120, ["road_markings"])
        for i in range(2):
            marking = model.infer(None, FrameContext(i, i / 30), self.activity(True))
            result = processor.process(None, i, i / 30, results_for(marking))
            source = result.record["postprocessing"]["road_marking_source"]
            self.assertEqual((source["source_frame"], source["direct"], source["age_frames"]), (i, True, 0))
        self.assertEqual(model.backend.calls, 2)

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
                         dict(source_frame=0, direct=False, propagated=True, warped=True, aligned=True,
                              age_frames=1, motion_valid=True))
        self.assertEqual(out[1].road_markings[0]["class_name"], "stop line")
        self.assertEqual(out[0].record["postprocessing"]["road_marking_source"],
                         dict(source_frame=0, direct=True, propagated=False, warped=False, aligned=True,
                              age_frames=0, motion_valid=False))

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
