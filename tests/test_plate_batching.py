import sys
import types
import unittest
from unittest.mock import patch

import numpy as np

from model_library.base import FrameContext, ModelResult


class _Tensor(list):
    def cpu(self):
        return self

    def tolist(self):
        return list(self)

    def item(self):
        return self[0]


class _Box:
    def __init__(self, xyxy, conf):
        self.xyxy, self.conf = [_Tensor(xyxy)], [_Tensor([conf])]


class FakeYOLO:
    """依裁切大小產生一個車牌框；記錄每次 predict 的呼叫方式。"""
    calls = []
    callers = []  # 每次呼叫的模型實例（區分 session）
    fail_batch = False

    def __init__(self, *args, **kwargs):
        pass

    def predict(self, source, **kwargs):
        FakeYOLO.calls.append((len(source) if isinstance(source, list) else 1, kwargs.get("rect")))
        FakeYOLO.callers.append(self)
        if isinstance(source, list) and FakeYOLO.fail_batch:
            raise RuntimeError("static batch")
        images = source if isinstance(source, list) else [source]
        return [types.SimpleNamespace(boxes=[_Box([5, 5, 5 + img.shape[1] / 3, 5 + img.shape[1] / 9], .8)])
                for img in images]


def vehicles():
    boxes = [[100, 100, 300, 260], [500, 120, 700, 300], [900, 400, 1000, 520]]
    return [dict(bbox_xyxy=b, class_id=2, confidence=.9, track_id=i + 1, object_id=f"vehicle-{i + 1:06d}")
            for i, b in enumerate(boxes)]


class PlateBatchingTests(unittest.TestCase):
    def run_model(self, **options):
        fake = types.ModuleType("ultralytics")
        fake.YOLO = FakeYOLO
        with patch.dict(sys.modules, {"ultralytics": fake}), \
             patch("model_library.perception.onnx_predictor", return_value=None), \
             patch("model_library.perception.yolo_device", return_value="cpu"):
            from model_library.perception import PlateModel
            model = PlateModel("unused.onnx", road_scan=True, **options)
            frame = np.zeros((720, 1280, 3), np.uint8)
            upstream = {"vehicles": ModelResult(vehicles()), "depth": ModelResult([])}
            FakeYOLO.calls, FakeYOLO.callers = [], []
            return model.infer(frame, FrameContext(0, 0), upstream).records, list(FakeYOLO.calls), model

    def test_batched_crops_match_sequential_candidates(self):
        sequential, calls, _ = self.run_model()
        batched, batch_calls, _ = self.run_model(batch_crops=True)
        self.assertEqual([r["bbox_xyxy"] for r in batched], [r["bbox_xyxy"] for r in sequential])
        self.assertEqual(calls, [(1, None)] * 4)          # 3 張裁切逐張 + 路面掃描
        self.assertEqual(batch_calls, [(3, None), (1, None)])  # 一批 3 張 + 路面掃描

    def test_failed_batch_falls_back_and_stays_sequential(self):
        sequential, _, _ = self.run_model()
        FakeYOLO.fail_batch = True
        try:
            with self.assertWarns(UserWarning):
                records, calls, model = self.run_model(batch_crops=True)
        finally:
            FakeYOLO.fail_batch = False
        self.assertEqual([r["bbox_xyxy"] for r in records], [r["bbox_xyxy"] for r in sequential])
        self.assertFalse(model.batch_crops)
        self.assertEqual(calls, [(3, None)] + [(1, None)] * 4)

    def test_fixed_input_disables_rect_letterbox(self):
        _, calls, _ = self.run_model(fixed_input=True)
        self.assertTrue(all(rect is False for _, rect in calls))


class StableShapeTests(PlateBatchingTests):
    def test_stable_shapes_separates_scan_session(self):
        records, calls, model = self.run_model(stable_shapes=True)
        crops, scan = FakeYOLO.callers[:3], FakeYOLO.callers[3]
        self.assertTrue(all(c is model.model for c in crops))
        self.assertIs(scan, model.scan_model)
        self.assertIsNot(model.model, model.scan_model)
        self.assertEqual(calls, [(1, False)] * 3 + [(1, None)])  # 只有裁切固定方形；掃描維持 rect
        self.assertEqual(len(records), 4)


if __name__ == "__main__":
    unittest.main()
