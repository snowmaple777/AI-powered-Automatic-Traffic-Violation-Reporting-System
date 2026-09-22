from pathlib import Path
import hashlib
import sys

import cv2
import numpy as np
from .runtime import yolo_device


ROOT = Path(__file__).resolve().parent.parent
THIRD_PARTY = ROOT / "third_party"
if str(THIRD_PARTY) not in sys.path:
    sys.path.insert(0, str(THIRD_PARTY))
VEHICLE_CLASSES = {0, 1, 2, 3, 5, 7}
ROAD_MODEL_SHA256 = "5c8da1343ab2848b480db84431dfdceb27a9e58c1139e7aaeed3bb0d8c2379df"


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
                 tracker=ROOT / "botsort_dashcam.yaml", classes=None, class_names=None):
        from ultralytics import YOLO
        self.model = YOLO(str(weights), task="detect")
        self.device, self.confidence, self.imgsz = device, confidence, imgsz
        self.device = yolo_device(weights, device)
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
            imgsz=self.imgsz, device=self.device, verbose=False)[0]
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
                 classes=None, palette=None):
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

    def infer(self, frame):
        from mmseg.apis import inference_model
        result = inference_model(self.model, frame)
        mask = result.pred_sem_seg.data.squeeze().cpu().numpy().astype(np.uint8)
        records = []
        for class_id in np.unique(mask):
            class_id = int(class_id)
            if class_id == 0:
                continue
            binary = (mask == class_id).astype(np.uint8)
            count, labels, stats, centroids = cv2.connectedComponentsWithStats(binary, 8)
            components = []
            for label in range(1, count):
                area = int(stats[label, cv2.CC_STAT_AREA])
                if area < self.min_pixels:
                    continue
                x, y, w, h = [int(v) for v in stats[label, :4]]
                ys, xs = np.where(labels == label)
                line_xyxy = self._fit_line(xs, ys, mask.shape[1], mask.shape[0])
                components.append({"area": area, "bbox_xywh": [x, y, w, h],
                                   "centroid": [round(float(centroids[label, 0]), 2),
                                                round(float(centroids[label, 1]), 2)],
                                   "line_xyxy": line_xyxy})
            if components:
                records.append({"class_id": class_id,
                                "class_name": self.classes[class_id],
                                "pixels": int(binary.sum()),
                                "components": components})
        return mask, records

    @staticmethod
    def _fit_line(xs, ys, width, height):
        """Represent a marking component's principal axis in image pixels."""
        if len(xs) < 2:
            return None
        points = np.column_stack((xs, ys)).astype(np.float32)
        vx, vy, x0, y0 = [float(value) for value in
                          cv2.fitLine(points, cv2.DIST_L2, 0, 0.01, 0.01)]
        if abs(vx) >= abs(vy):
            x1, x2 = 0.0, float(width - 1)
            y1 = y0 + (x1 - x0) * vy / max(abs(vx), 1e-6) * (1 if vx >= 0 else -1)
            y2 = y0 + (x2 - x0) * vy / max(abs(vx), 1e-6) * (1 if vx >= 0 else -1)
        else:
            y1, y2 = 0.0, float(height - 1)
            x1 = x0 + (y1 - y0) * vx / max(abs(vy), 1e-6) * (1 if vy >= 0 else -1)
            x2 = x0 + (y2 - y0) * vx / max(abs(vy), 1e-6) * (1 if vy >= 0 else -1)
        return [round(x1, 2), round(y1, 2), round(x2, 2), round(y2, 2)]

    def overlay(self, frame, mask, alpha=0.35):
        colors = np.zeros_like(frame)
        for class_id, rgb in enumerate(self.palette):
            colors[mask == class_id] = rgb[::-1]
        blended = cv2.addWeighted(frame, 1.0 - alpha, colors, alpha, 0)
        output = frame.copy()
        output[mask != 0] = blended[mask != 0]
        return output


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
