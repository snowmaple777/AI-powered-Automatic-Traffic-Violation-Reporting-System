import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from model_library import FrameContext, ModelPipeline, ModelResult, PerceptionModel, load_config
from model_library.registry import BACKENDS
from model_library.ocr import OCRModel
from model_library.depth import DepthModel
from model_library.plates import PlateModel
from observation_schema import build_frame_observation


class FakeModel(PerceptionModel):
    def __init__(self, device="cpu", **kwargs):
        self.closed = False
        self.reset_count = 0

    def infer(self, frame, context, upstream):
        return ModelResult([{"frame":context.index}])

    def reset(self):
        self.reset_count += 1

    def close(self):
        self.closed = True


class ModelTests(unittest.TestCase):
    def test_backend_replacement_and_lifecycle(self):
        config = {"models":{"vehicles":{"backend":"test_models:FakeModel"}}}
        with ModelPipeline(config) as pipeline:
            model = pipeline.models["vehicles"]
            result = pipeline.infer(None, FrameContext(7))
            self.assertEqual(result["vehicles"].records, [{"frame":7}])
            self.assertEqual(result["plates"].records, [])
            self.assertEqual(model.reset_count, 1)
        self.assertTrue(model.closed)

    def test_failed_initialization_closes_previous_models(self):
        first = FakeModel()
        with patch.dict(BACKENDS, {"fake":lambda **kw:first}):
            with self.assertRaises(ValueError):
                ModelPipeline({"models":{"vehicles":{"backend":"fake"},
                                         "depth":{"backend":"unknown"}}})
        self.assertTrue(first.closed)

    def test_paths_relative_to_config_and_disabled_missing_weights(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "weights.bin").write_bytes(b"test")
            config = {"version":1,"models":{
                "vehicles":{"backend":"fake","params":{"weights":"weights.bin"}},
                "depth":{"enabled":False,"params":{"weights":"absent"}}}}
            path = directory / "config.json"
            path.write_text(json.dumps(config))
            loaded = load_config(path)
            self.assertEqual(Path(loaded["models"]["vehicles"]["params"]["weights"]), directory / "weights.bin")
            config["models"]["ocr"] = {"backend":"rapidocr"}
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, "requires plates"):
                load_config(path)

    def test_missing_weights_fails_early(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"config.json"
            path.write_text(json.dumps({"version":1,"models":{"vehicles":{
                "backend":"fake","params":{"weights":"missing.pt"}}}}))
            with self.assertRaises(FileNotFoundError):
                load_config(path)

    def test_wrong_backend_return_type(self):
        with patch.object(FakeModel, "infer", return_value=[]):
            with ModelPipeline({"models":{"vehicles":{"backend":"test_models:FakeModel"}}}) as pipeline:
                with self.assertRaises(TypeError):
                    pipeline.infer(None, FrameContext(0))

    def make_ocr(self, text):
        model = OCRModel.__new__(OCRModel)
        model.interval, model.min_width, model.confidence = 4, 40, .5
        model.taiwan_filter, model.cache_ttl = True, 90
        model.reset()
        calls = []
        def infer(*args, **kwargs):
            calls.append(1)
            return [[text, .95]], None
        model.model = infer
        return model, calls

    def test_ocr_voting_throttle_invalid_text_and_reset(self):
        frame = np.zeros((100,200,3),np.uint8)
        upstream = {"plates":ModelResult([{"object_id":"vehicle-1","bbox_xyxy":[10,10,100,40]}])}
        model,calls = self.make_ocr("BXH6208")
        for i in range(5):
            records = model.infer(frame,FrameContext(i),upstream).records
        self.assertEqual(len(calls),2)
        self.assertEqual(records[0]["text"],"BXH-6208")
        self.assertTrue(records[0]["confirmed"])
        model.reset()
        self.assertEqual(model.history,{})
        model,calls = self.make_ocr("CAMRY")
        result = model.infer(frame,FrameContext(0),upstream)
        self.assertIsNone(result.records[0]["text"])
        self.assertEqual(result.records[0]["raw_text"],"CAMRY")
        model.infer(frame,FrameContext(1),upstream)
        self.assertEqual(len(calls),1)

    def test_depth_cache_and_original_frame_roi(self):
        class Input:
            name = "image"
            shape = [1,3,28,28]
        class Session:
            calls = 0
            def run(self, outputs, inputs):
                self.calls += 1
                self.tensor_shape = inputs["image"].shape
                return [np.full((1,28,28),12.,np.float32)]
        model = DepthModel.__new__(DepthModel)
        model.session,model.input = Session(),Input()
        model.input_size,model.interval = 392,2
        model.reset()
        frame = np.zeros((100,200,3),np.uint8)
        upstream = {"vehicles":ModelResult([{"object_id":"v1","bbox_xyxy":[0,0,80,80]}])}
        model.infer(frame,FrameContext(0),upstream)
        record = model.infer(frame,FrameContext(1),upstream).records[0]
        self.assertEqual(record["distance_m"],12.)
        self.assertEqual(record["source_frame"],0)
        self.assertEqual(model.session.calls,1)
        self.assertEqual(model.session.tensor_shape,(1,3,28,28))
        model.reset()
        self.assertIsNone(model.cached)

    def test_schema_preserves_rule_inputs(self):
        record = build_frame_observation(0,0,10,10,30,[],[],[])
        self.assertEqual(record["schema_version"],"1.2")
        for key in ("vehicles","traffic_lights","road_markings","depth","plates"):
            self.assertEqual(record["observations"][key],[])
        json.dumps(record, allow_nan=False)

    def test_plate_coordinates_holdover_expiry_and_person_filter(self):
        from types import SimpleNamespace
        class Coordinates:
            def cpu(self):
                return self
            def tolist(self):
                return [20.,30.,80.,50.]
        box = SimpleNamespace(xyxy=[Coordinates()],conf=[np.float32(.9)])
        class Detector:
            calls = 0
            def predict(self, *args, **kwargs):
                self.calls += 1
                return [SimpleNamespace(boxes=[box] if self.calls == 1 else [])]
        model = PlateModel.__new__(PlateModel)
        model.model = Detector()
        model.device,model.confidence,model.imgsz = "cpu",.3,320
        model.padding,model.min_size,model.max_distance = 0.,50,25.
        model.vehicle_classes = {"car"}
        model.alpha,model.hold_frames = .65,2
        model.reset()
        frame = np.zeros((300,300,3),np.uint8)
        upstream = {"depth":ModelResult(), "vehicles":ModelResult([
            {"object_id":"v1","class_name":"car","bbox_xyxy":[100,100,200,200],"track_id":1},
            {"object_id":"p1","class_name":"person","bbox_xyxy":[0,0,100,100],"track_id":2}])}
        first = model.infer(frame,FrameContext(0),upstream).records
        self.assertEqual(first[0]["bbox_xyxy"],[120.,130.,180.,150.])
        self.assertEqual(model.model.calls,1)
        upstream["vehicles"].records[0]["bbox_xyxy"] = [110,100,210,200]
        held = model.infer(frame,FrameContext(1),upstream).records
        self.assertTrue(held[0]["held"])
        self.assertEqual(held[0]["bbox_xyxy"],[130.,130.,190.,150.])
        self.assertEqual(len(model.infer(frame,FrameContext(2),upstream).records),1)
        self.assertEqual(model.infer(frame,FrameContext(3),upstream).records,[])
        model.reset()
        self.assertEqual(model.history,{})


if __name__ == "__main__":
    unittest.main()
