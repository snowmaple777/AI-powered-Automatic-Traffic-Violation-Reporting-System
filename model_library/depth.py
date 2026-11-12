"""Depth Anything V2 ONNX：維持長寬比補邊，裁回原圖後擷取車輛距離。"""
import warnings
import cv2
import numpy as np
from .base import ModelResult, PerceptionModel
from .runtime import prepare_onnx_cuda


class DepthModel(PerceptionModel):
    def __init__(self, weights, device="cpu", input_size=392, interval=2, cuda_conv_search="HEURISTIC",
                 input_shape=None):
        import onnxruntime as ort
        if interval < 1 or input_size < 14:
            raise ValueError("depth interval >= 1 and input_size >= 14 required")
        if cuda_conv_search not in {"HEURISTIC", "EXHAUSTIVE", "DEFAULT"}:
            raise ValueError("Invalid CUDA convolution search mode")
        # 某些匯出檔宣告動態 H/W，內部位置編碼卻固定；由權重所屬設定明確指定 H/W。
        if input_shape is not None and (not isinstance(input_shape, (list, tuple)) or len(input_shape) != 2
                or any(type(v) is not int or v < 14 for v in input_shape)):
            raise ValueError("depth input_shape must be [height, width], integers >= 14")
        providers = ["CPUExecutionProvider"]
        if str(device).startswith("cuda"):
            if "CUDAExecutionProvider" in prepare_onnx_cuda():
                device_id = int(str(device).split(":")[1]) if ":" in str(device) else 0
                providers.insert(0, ("CUDAExecutionProvider", {"device_id": device_id,
                                     "cudnn_conv_algo_search": cuda_conv_search}))
            else:
                warnings.warn("ONNX Runtime CUDA unavailable; depth runs on CPU")
        self.session = ort.InferenceSession(str(weights), providers=providers)
        self.input = self.session.get_inputs()[0]
        self.input_shape = tuple(input_shape) if input_shape is not None else None
        if self.input_shape is not None:
            for requested, declared in zip(self.input_shape, self.input.shape[2:4]):
                if isinstance(declared, int) and requested != declared:
                    raise ValueError("depth input_shape conflicts with the ONNX fixed input shape")
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
            if self.input_shape is not None:
                th, tw = self.input_shape
            scale = min(th / h, tw / w)
            nh, nw = min(th, max(1, round(h * scale))), min(tw, max(1, round(w * scale)))
            top, left = (th - nh) // 2, (tw - nw) // 2
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32) / 255
            rgb = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_CUBIC)
            rgb = (rgb - np.array([.485, .456, .406], np.float32)) / np.array([.229, .224, .225], np.float32)
            # 正規化後補 0，相當於以 RGB 訓練平均色補邊，不把黑邊當作影像內容。
            padded = np.zeros((th, tw, 3), dtype=np.float32)
            padded[top:top + nh, left:left + nw] = rgb
            tensor = np.ascontiguousarray(padded.transpose(2, 0, 1)[None])
            output = self.session.run(None, {self.input.name: tensor})[0].squeeze()
            if output.ndim != 2:
                raise ValueError(f"Expected 2D metric depth map, got {output.shape}")
            # 模型輸出的深度解析度不一定等於輸入；先按比例裁掉補邊，再還原原圖座標。
            oh, ow = output.shape
            y1, y2 = round(top * oh / th), round((top + nh) * oh / th)
            x1, x2 = round(left * ow / tw), round((left + nw) * ow / tw)
            if y2 <= y1 or x2 <= x1:
                raise ValueError("Depth output is too small to remove letterbox padding")
            self.cached = cv2.resize(output[y1:y2, x1:x2], (w, h), interpolation=cv2.INTER_LINEAR)
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
