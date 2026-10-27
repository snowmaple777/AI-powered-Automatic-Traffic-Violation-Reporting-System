"""現行偵測器與遮罩後處理：車輛（YOLO + ByteTrack）、紅綠燈、道路標線（SegFormer）。"""
from pathlib import Path
import hashlib
import math
import sys

import cv2
import numpy as np
from .runtime import yolo_device, onnx_predictor
from .perception_core import VEHICLE_CLASSES


ROOT = Path(__file__).resolve().parent.parent
THIRD_PARTY = ROOT / "third_party"
if str(THIRD_PARTY) not in sys.path:
    sys.path.insert(0, str(THIRD_PARTY))
ROAD_MODEL_SHA256 = "75ccd13c8efe31a0c1a76f22ddcd3867ebdd606287af3c39ae43b6c589d96b36"  # best56000


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _box_record(box, names):
    xyxy = [round(float(v), 2) for v in box.xyxy[0].cpu().tolist()]
    class_id = int(box.cls[0].item())
    track_id = None if box.id is None else int(box.id[0].item())
    center = [round((xyxy[0] + xyxy[2]) / 2, 2),
              round((xyxy[1] + xyxy[3]) / 2, 2)]
    bottom_center = [center[0], xyxy[3]]
    return {"class_id": class_id, "class_name": names[class_id],
            "confidence": round(float(box.conf[0].item()), 5),
            "bbox_xyxy": xyxy, "center": center,
            "bottom_center": bottom_center, "track_id": track_id}


class VehicleDetector:
    def __init__(self, weights, device="cpu", confidence=0.25, imgsz=640,
                 tracker=ROOT / "configs" / "bytetrack_perception.yaml", classes=None, class_names=None,
                 cuda_conv_search="HEURISTIC"):
        from ultralytics import YOLO
        self.model = YOLO(str(weights), task="detect")
        self.device, self.confidence, self.imgsz = device, confidence, imgsz
        self.device = yolo_device(weights, device)
        self.predictor_class = onnx_predictor(cuda_conv_search)
        self.tracker = str(tracker)
        self.classes = sorted(VEHICLE_CLASSES) if classes is None else classes
        self.class_names = class_names or {}
        self.frame_index = -1
        self.track_first_seen = {}

    def infer(self, frame):
        self.frame_index += 1
        result = self.model.track(
            frame, persist=True, tracker=self.tracker,
            classes=self.classes, conf=self.confidence,
            imgsz=self.imgsz, device=self.device, verbose=False, predictor=self.predictor_class)[0]
        records = [] if result.boxes is None else [
            _box_record(box, result.names) for box in result.boxes]
        for item in records:
            item["class_name"] = self.class_names.get(str(item["class_id"]), item["class_name"])
            track_id = item["track_id"]
            if track_id is None:
                item["object_id"] = None
                item["track_age_frames"] = 0
                continue
            self.track_first_seen.setdefault(track_id, self.frame_index)
            item["object_id"] = f"vehicle-{track_id:06d}"
            item["track_age_frames"] = (
                self.frame_index - self.track_first_seen[track_id] + 1)
        return records


class TrafficLightDetector:
    def __init__(self, weights, device="cpu", confidence=0.12, imgsz=800, class_names=None):
        from ultralytics import YOLO
        self.model = YOLO(str(weights), task="detect")
        self.device, self.confidence, self.imgsz = device, confidence, imgsz
        self.device = yolo_device(weights, device)
        self.class_names = class_names or {}

    def infer(self, frame):
        result = self.model.predict(frame, conf=self.confidence, imgsz=self.imgsz,
                                    device=self.device, verbose=False)[0]
        records = [] if result.boxes is None else [
            _box_record(box, result.names) for box in result.boxes]
        for item in records:
            item["class_name"] = self.class_names.get(str(item["class_id"]), item["class_name"])
        return records


class RoadMarkingSegmenter:
    def __init__(self, config, weights, device="cpu", min_pixels=100, sha256=ROAD_MODEL_SHA256,
                 classes=None, palette=None, inference_scale=None, fp16=False, roi_top=0.0,
                 fast_attention=False, slide=None):
        """slide：{"crop": [高, 寬], "stride": [高, 寬]}，以滑動視窗推論（在 Resize 後的影像上）；
        None 為整張推論（設定檔 test_cfg）。best56000 官方推論使用 crop 1024、stride 683。
        inference_scale：[寬, 高]，取代設定檔 test_pipeline 的 Resize（預設 1920x1080，保持長寬比）。
        fp16：CUDA 上以半精度 autocast 推論。
        roi_top：只分割畫面高度此比例以下的道路區域（以原解析度推論，上方遮罩為背景）。
        fast_attention：注意力層以 need_weights=False 呼叫 PyTorch，使用融合注意力核心，
        不再產生並回傳完全沒用到的注意力權重矩陣（1080p 第一層約 13 萬 x 2 千）。
        以上皆為效能選項，預設維持原行為。"""
        import torch
        from mmseg.apis import init_model
        weights = Path(weights)
        digest = _file_sha256(weights)
        if not sha256 or digest != sha256:
            raise RuntimeError(f"道路標線權重 SHA-256 不符：{digest}")
        # Build first, then load only the state dict ourselves. The checksum
        # gate makes the legacy weights_only=False checkpoint load explicit.
        self.model = init_model(str(config), checkpoint=None, device=device)
        checkpoint = torch.load(weights, map_location="cpu", weights_only=False)
        incompatible = self.model.load_state_dict(checkpoint["state_dict"], strict=False)
        if incompatible.missing_keys or incompatible.unexpected_keys:
            raise RuntimeError(
                "道路模型與設定不相容；missing={} unexpected={}".format(
                    incompatible.missing_keys, incompatible.unexpected_keys))
        self.model.dataset_meta = checkpoint.get("meta", {}).get("dataset_meta", {})
        from road_marking_config import classes as default_classes, palette as default_palette
        self.classes = list(classes or self.model.dataset_meta.get("classes") or default_classes)
        self.palette = palette or self.model.dataset_meta.get("palette") or default_palette
        if len(self.palette) != len(self.classes):
            raise ValueError("Road marking classes and palette must have equal length")
        self.min_pixels = min_pixels
        if inference_scale is not None:
            if (not isinstance(inference_scale, (list, tuple)) or len(inference_scale) != 2
                    or not all(isinstance(v, int) and v >= 64 for v in inference_scale)):
                raise ValueError("inference_scale must be [width, height] integers >= 64")
            resizes = [step for step in self.model.cfg.test_pipeline if step.get("type") == "Resize"]
            if len(resizes) != 1:
                raise ValueError("Road marking config must contain exactly one Resize step")
            resizes[0]["scale"] = tuple(inference_scale)
        self.inference_scale = inference_scale
        self.fp16 = bool(fp16) and str(device).startswith("cuda")
        if not isinstance(roi_top, (int, float)) or not 0 <= roi_top < .9:
            raise ValueError("roi_top must be in [0, 0.9)")
        self.roi_top = float(roi_top)
        self.fast_attention = bool(fast_attention)
        if self.fast_attention:
            enable_fast_attention(self.model)
        self.slide = None
        if slide is not None:
            crop, stride = (tuple(slide.get(k, ())) if isinstance(slide, dict) else () for k in ("crop", "stride"))
            if (len(crop) != 2 or len(stride) != 2 or not all(isinstance(v, int) and v > 0 for v in crop + stride)
                    or any(s > c for s, c in zip(stride, crop))):
                raise ValueError('slide must be {"crop": [h, w], "stride": [h, w]} with 0 < stride <= crop')
            from mmengine.config import ConfigDict
            self.model.test_cfg = ConfigDict(mode="slide", crop_size=crop, stride=stride)
            self.slide = dict(crop=list(crop), stride=list(stride))

    def infer(self, frame):
        import torch
        from mmseg.apis import inference_model
        top = int(frame.shape[0] * self.roi_top)
        source = frame[top:] if top else frame
        if self.fp16:
            with torch.autocast("cuda", dtype=torch.float16):
                result = inference_model(self.model, source)
        else:
            result = inference_model(self.model, source)
        mask = result.pred_sem_seg.data.squeeze().cpu().numpy().astype(np.uint8)
        if top:
            full = np.zeros(frame.shape[:2], dtype=np.uint8)
            full[top:] = mask
            mask = full
        return mask, mask_components(mask, self.classes, self.min_pixels)


def _fast_attention_forward(self, x, hw_shape, identity=None):
    """與 mmseg EfficientMultiheadAttention.forward 相同，只是不要求回傳注意力權重。"""
    from mmseg.models.utils import nchw_to_nlc, nlc_to_nchw
    x_q = x
    if self.sr_ratio > 1:
        x_kv = self.norm(nchw_to_nlc(self.sr(nlc_to_nchw(x, hw_shape))))
    else:
        x_kv = x
    if identity is None:
        identity = x_q
    if self.batch_first:
        x_q, x_kv = x_q.transpose(0, 1), x_kv.transpose(0, 1)
    out = self.attn(query=x_q, key=x_kv, value=x_kv, need_weights=False)[0]
    if self.batch_first:
        out = out.transpose(0, 1)
    return identity + self.dropout_layer(self.proj_drop(out))


def enable_fast_attention(model):
    """將 SegFormer 的注意力層切換為不回傳權重的版本；回傳替換的層數。"""
    import types
    from mmseg.models.backbones.mit import EfficientMultiheadAttention
    count = 0
    for module in model.modules():
        if isinstance(module, EfficientMultiheadAttention):
            module.forward = types.MethodType(_fast_attention_forward, module)
            count += 1
    if not count:
        raise RuntimeError("找不到 SegFormer 注意力層，無法啟用 fast_attention")
    return count


def mask_components(mask, classes, min_pixels):
    """將語意遮罩轉為每類別的連通區塊紀錄（面積、外框、質心與主軸線）。

    主軸由所有區塊的一階／二階矩一次算出（np.bincount），等同 cv2.fitLine(DIST_L2)，
    避免對每個區塊在全畫面重做 labels == label 比對。
    """
    records = []
    height, width = mask.shape
    for class_id in np.unique(mask):
        class_id = int(class_id)
        if class_id == 0:
            continue
        selected = mask == class_id
        rows, cols = np.flatnonzero(selected.any(axis=1)), np.flatnonzero(selected.any(axis=0))
        top, left = int(rows[0]), int(cols[0])
        # 只在該類別像素的外接範圍內標記；標號順序與全畫面標記相同。
        binary = selected[top:rows[-1] + 1, left:cols[-1] + 1].astype(np.uint8)
        count, labels, stats, centroids = cv2.connectedComponentsWithStats(binary, 8)
        stats[:, cv2.CC_STAT_LEFT] += left
        stats[:, cv2.CC_STAT_TOP] += top
        centroids = centroids + [left, top]
        ys, xs = np.nonzero(labels)
        owner = labels[ys, xs]
        xs, ys = xs.astype(np.float64) + left, ys.astype(np.float64) + top
        n = np.bincount(owner, minlength=count).astype(np.float64)
        safe = np.maximum(n, 1)
        mean_x = np.bincount(owner, xs, count) / safe
        mean_y = np.bincount(owner, ys, count) / safe
        cov_xx = np.bincount(owner, xs * xs, count) / safe - mean_x ** 2
        cov_yy = np.bincount(owner, ys * ys, count) / safe - mean_y ** 2
        cov_xy = np.bincount(owner, xs * ys, count) / safe - mean_x * mean_y
        angle = 0.5 * np.arctan2(2 * cov_xy, cov_xx - cov_yy)
        components = []
        for label in range(1, count):
            area = int(stats[label, cv2.CC_STAT_AREA])
            if area < min_pixels:
                continue
            x, y, w, h = [int(v) for v in stats[label, :4]]
            line_xyxy = None
            if n[label] >= 2:
                line_xyxy = _principal_line(math.cos(angle[label]), math.sin(angle[label]),
                                            mean_x[label], mean_y[label], width, height)
            components.append({"area": area, "bbox_xywh": [x, y, w, h],
                               "centroid": [round(float(centroids[label, 0]), 2),
                                            round(float(centroids[label, 1]), 2)],
                               "line_xyxy": line_xyxy})
        # 固定輸出順序：外框由上到下、由左到右（cv2 標號順序會隨裁切範圍改變）。
        components.sort(key=lambda c: (c["bbox_xywh"][1], c["bbox_xywh"][0], c["area"]))
        if components:
            records.append({"class_id": class_id, "class_name": classes[class_id],
                            "pixels": int(binary.sum()), "components": components})
    return records


def _principal_line(vx, vy, x0, y0, width, height):
    """主軸延伸到畫面邊界，回傳 [x1, y1, x2, y2]（等同 cv2.fitLine(DIST_L2) 主軸延伸至畫面邊界）。"""
    if abs(vx) >= abs(vy):
        x1, x2 = 0.0, float(width - 1)
        slope = vy / vx
        y1, y2 = y0 + (x1 - x0) * slope, y0 + (x2 - x0) * slope
    else:
        y1, y2 = 0.0, float(height - 1)
        slope = vx / vy
        x1, x2 = x0 + (y1 - y0) * slope, x0 + (y2 - y0) * slope
    return [round(float(x1), 2), round(float(y1), 2), round(float(x2), 2), round(float(y2), 2)]


def draw_boxes(frame, records, color, prefix=""):
    for item in records:
        x1, y1, x2, y2 = map(int, item["bbox_xyxy"])
        label = f'{prefix}{item["class_name"]} {item["confidence"]:.2f}'
        if item.get("distance_m") is not None:
            label += f' {item["distance_m"]:.1f}m'
        if item.get("raw_text") or item.get("plate_text"):
            label += ' ' + str(item.get("raw_text") or item.get("plate_text"))
        if item.get("object_id"):
            label += f' [{item["object_id"]}]'
        elif item.get("track_id") is not None:
            label += f' #{item["track_id"]}'
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        cv2.putText(frame, label, (x1, max(18, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2, cv2.LINE_AA)
    return frame
