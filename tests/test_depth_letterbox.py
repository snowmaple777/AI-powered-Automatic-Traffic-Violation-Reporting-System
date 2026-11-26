"""深度補邊的幾何、輸出裁切、快取與固定尺寸契約；不載入權重。"""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from model_library.base import FrameContext, ModelResult
from model_library.depth import DepthModel


class DepthLetterboxTests(unittest.TestCase):
    def model(self, output, declared=('batch', 3, 'height', 'width'), **params):
        session = Mock()
        session.get_inputs.return_value = [SimpleNamespace(name='image', shape=list(declared))]
        session.run.return_value = [output[None, None]]
        ort = SimpleNamespace(InferenceSession=Mock(return_value=session))
        with patch.dict('sys.modules', onnxruntime=ort):
            model = DepthModel('unused.onnx', **params)
        return model, session

    def infer(self, model, shape=(720, 960), index=0):
        h, w = shape
        frame = np.full((h, w, 3), 255, np.uint8)
        vehicle = dict(object_id='car', track_id=1, bbox_xyxy=[0, 0, w, h])
        return model.infer(frame, FrameContext(index, index / 30), {'vehicles': ModelResult([vehicle])})

    def test_4_3_uses_fixed_canvas_and_removes_asymmetric_padding(self):
        # 960×720 → 523×392，左右補 88／89；以高值邊框檢查距離不受補邊污染。
        depth = np.full((392, 700), 999., np.float32)
        depth[:, 88:611] = 12.
        model, session = self.model(depth, input_shape=[392, 700])
        result = self.infer(model)
        tensor = session.run.call_args.args[1]['image']
        self.assertEqual(tensor.shape, (1, 3, 392, 700))
        self.assertTrue(np.all(tensor[:, :, :, :88] == 0))
        self.assertTrue(np.all(tensor[:, :, :, 611:] == 0))
        self.assertTrue(np.all(tensor[:, :, :, 88:611] != 0))
        self.assertEqual(result.artifacts['depth_map'].shape, (720, 960))
        np.testing.assert_allclose(result.artifacts['depth_map'], 12.)
        self.assertEqual(result.records[0]['distance_m'], 12.)

    def test_portrait_and_wide_inputs_remove_correct_axis(self):
        for shape, region in [((80, 40), (slice(None), slice(28, 56))),
                              ((40, 120), (slice(14, 42), slice(None)))]:
            with self.subTest(shape=shape):
                depth = np.full((56, 84), 999., np.float32)
                depth[region] = 7.
                model, _ = self.model(depth, input_shape=[56, 84])
                np.testing.assert_allclose(self.infer(model, shape).artifacts['depth_map'], 7.)

    def test_scaled_output_crops_in_output_coordinates(self):
        depth = np.full((28, 42), 999., np.float32)
        depth[:, 14:28] = 9.
        model, _ = self.model(depth, input_shape=[56, 84])
        np.testing.assert_allclose(self.infer(model, (80, 40)).artifacts['depth_map'], 9.)

    def test_16_9_retains_aspect_ratio_and_original_output_size(self):
        model, session = self.model(np.ones((392, 700), np.float32), input_shape=[392, 700])
        result = self.infer(model, (720, 1280))
        tensor = session.run.call_args.args[1]['image']
        self.assertTrue(np.all(tensor[:, :, :, :1] == 0))
        self.assertTrue(np.all(tensor[:, :, :, 698:] == 0))
        self.assertTrue(np.all(tensor[:, :, :, 1:698] != 0))
        self.assertEqual(result.artifacts['depth_map'].shape, (720, 1280))

    def test_vehicle_boxes_stay_in_original_coordinates(self):
        depth = np.full((56, 84), 999., np.float32)
        depth[:, 14:42] = 5.
        depth[:, 42:70] = 20.
        model, _ = self.model(depth, input_shape=[56, 84])
        vehicles = [dict(object_id='left', track_id=1, bbox_xyxy=[0, 0, 35, 80]),
                    dict(object_id='right', track_id=2, bbox_xyxy=[45, 0, 80, 80])]
        result = model.infer(np.zeros((80, 80, 3), np.uint8), FrameContext(0, 0),
                             {'vehicles': ModelResult(vehicles)})
        self.assertEqual([r['distance_m'] for r in result.records], [5., 20.])

    def test_interval_source_frame_reset_and_resolution_change(self):
        model, session = self.model(np.ones((56, 84), np.float32), input_shape=[56, 84], interval=2)
        self.infer(model, index=0)
        cached = self.infer(model, index=1)
        self.assertEqual(session.run.call_count, 1)
        self.assertEqual(cached.records[0]['source_frame'], 0)
        self.assertEqual(self.infer(model, index=2).records[0]['source_frame'], 2)
        self.infer(model, shape=(80, 80), index=3)
        self.assertEqual(session.run.call_count, 3)
        model.reset()
        self.assertIsNone(model.cached)
        self.assertEqual(self.infer(model, index=1).records[0]['source_frame'], 1)

    def test_static_model_shape_without_override(self):
        model, session = self.model(np.ones((56, 84), np.float32), declared=(1, 3, 56, 84))
        self.infer(model)
        self.assertEqual(session.run.call_args.args[1]['image'].shape, (1, 3, 56, 84))

    def test_dynamic_model_without_override_uses_input_size(self):
        model, session = self.model(np.ones((56, 70), np.float32), input_size=56)
        self.infer(model, (80, 100))
        self.assertEqual(session.run.call_args.args[1]['image'].shape, (1, 3, 56, 70))

    def test_invalid_shape_and_static_conflict_fail_early(self):
        for shape in ([0, 700], [392], [392, 700.], [True, 700], '392,700'):
            with self.subTest(shape=shape), self.assertRaises(ValueError):
                self.model(np.ones((2, 2), np.float32), input_shape=shape)
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            self.model(np.ones((2, 2), np.float32), declared=(1, 3, 56, 84), input_shape=[56, 70])

    def test_shipped_depth_configs_declare_actual_export_size(self):
        root = Path(__file__).resolve().parents[1]
        for path in (root / 'configs').glob('*.json'):
            depth = json.loads(path.read_text(encoding='utf-8-sig')).get('models', {}).get('depth', {})
            if depth.get('backend') == 'onnx_depth':
                with self.subTest(config=path.name):
                    self.assertEqual(depth['params']['input_shape'], [392, 700])


if __name__ == '__main__':
    unittest.main()
