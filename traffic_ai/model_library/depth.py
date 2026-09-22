"""Depth Anything V2 ONNX preprocessing migrated from perception-v2."""
import warnings
import cv2
import numpy as np
from .base import ModelResult, PerceptionModel


class DepthModel(PerceptionModel):
    def __init__(self, weights, device="cpu", input_size=392, interval=2):
        import onnxruntime as ort
        if interval < 1 or input_size < 14:
            raise ValueError("depth interval >= 1 and input_size >= 14 required")
        providers = ["CPUExecutionProvider"]
        if str(device).startswith("cuda"):
            if "CUDAExecutionProvider" in ort.get_available_providers():
                device_id = int(str(device).split(":")[1]) if ":" in str(device) else 0
                providers.insert(0, ("CUDAExecutionProvider", {"device_id": device_id}))
            else:
                warnings.warn("ONNX Runtime CUDA unavailable; depth runs on CPU")
        self.session = ort.InferenceSession(str(weights), providers=providers)
        self.input = self.session.get_inputs()[0]
        self.input_size, self.interval = input_size, interval
        self.reset()

    def reset(self):
        self.cached = None
        self.source_frame = None

    def infer(self, frame, context, upstream):
        h, w = frame.shape[:2]
        if self.cached is None or self.cached.shape != (h, w) or context.index % self.interval == 0:
            scale = self.input_size / min(h, w)
            th, tw = [max(14, round(v * scale / 14) * 14) for v in (h, w)]
            if isinstance(self.input.shape[2], int):
                th = self.input.shape[2]
            if isinstance(self.input.shape[3], int):
                tw = self.input.shape[3]
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32) / 255
            rgb = cv2.resize(rgb, (tw, th), interpolation=cv2.INTER_CUBIC)
            rgb = (rgb - np.array([.485, .456, .406], np.float32)) / np.array([.229, .224, .225], np.float32)
            tensor = np.ascontiguousarray(rgb.transpose(2, 0, 1)[None])
            output = self.session.run(None, {self.input.name: tensor})[0].squeeze()
            if output.ndim != 2:
                raise ValueError(f"Expected 2D metric depth map, got {output.shape}")
            self.cached = cv2.resize(output, (w, h), interpolation=cv2.INTER_LINEAR)
            self.source_frame = context.index
        records = []
        for vehicle in upstream["vehicles"].records:
            x1, y1, x2, y2 = vehicle["bbox_xyxy"]
            dx, dy = (x2 - x1) / 4, (y2 - y1) / 4
            left, right = max(0, min(w, int(x1 + dx))), max(0, min(w, int(x2 - dx)))
            top, bottom = max(0, min(h, int(y1 + dy))), max(0, min(h, int(y2 - dy)))
            roi = self.cached[top:bottom, left:right]
            values = roi[np.isfinite(roi) & (roi > 0)]
            records.append({"object_id": vehicle.get("object_id"), "track_id": vehicle.get("track_id"),
                            "distance_m": round(float(np.median(values)), 3) if values.size else None,
                            "source_frame": self.source_frame})
        return ModelResult(records, {"depth_map": self.cached})

    def close(self):
        self.session = None
        self.reset()
