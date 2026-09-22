"""Vehicle-crop plate detection, geometry filtering, EMA and short holdover."""
import numpy as np
from .base import ModelResult, PerceptionModel
from .runtime import yolo_device


class PlateModel(PerceptionModel):
    def __init__(self, weights, device="cpu", confidence=.30, imgsz=320,
                 padding=.08, min_vehicle_size=50, max_distance=25.,
                 vehicle_classes=None, smooth_alpha=.65, hold_frames=2):
        from ultralytics import YOLO
        self.model = YOLO(str(weights), task="detect")
        self.device, self.confidence, self.imgsz = device, confidence, imgsz
        self.device = yolo_device(weights, device)
        self.padding, self.min_size, self.max_distance = padding, min_vehicle_size, max_distance
        self.vehicle_classes = set(vehicle_classes or ["car", "motorcycle", "bus", "truck"])
        if not 0 <= smooth_alpha <= 1 or hold_frames < 0:
            raise ValueError("Invalid plate smoothing options")
        self.alpha, self.hold_frames = smooth_alpha, hold_frames
        self.reset()

    def reset(self):
        self.history = {}

    def close(self):
        self.model = None
        self.reset()

    def infer(self, frame, context, upstream):
        h, w = frame.shape[:2]
        distances = {r["object_id"]: r["distance_m"] for r in upstream["depth"].records if r["object_id"] is not None}
        records, active = [], set()
        for vehicle in upstream["vehicles"].records:
            key = vehicle.get("object_id")
            distance = distances.get(key)
            if vehicle["class_name"] not in self.vehicle_classes or (distance is not None and distance > self.max_distance):
                continue
            x1, y1, x2, y2 = vehicle["bbox_xyxy"]
            vw, vh = x2 - x1, y2 - y1
            if min(vw, vh) < self.min_size:
                continue
            left, top = max(0, int(x1 - vw * self.padding)), max(0, int(y1 - vh * self.padding))
            right, bottom = min(w, int(x2 + vw * self.padding)), min(h, int(y2 + vh * self.padding))
            if right <= left or bottom <= top:
                continue
            if key is not None:
                active.add(key)
            result = self.model.predict(frame[top:bottom, left:right], conf=self.confidence,
                                        imgsz=self.imgsz, device=self.device, verbose=False)[0]
            candidates = []
            for box in ([] if result.boxes is None else result.boxes):
                px1, py1, px2, py2 = box.xyxy[0].cpu().tolist()
                pw, ph = px2 - px1, py2 - py1
                if 1.3 <= pw / max(ph, 1) <= 4.2 and .005 <= pw * ph / (vw * vh) <= .25:
                    candidates.append((float(box.conf[0].item()), [left+px1, top+py1, left+px2, top+py2]))
            previous = self.history.get(key)
            held = False
            if candidates:
                confidence, bbox = max(candidates, key=lambda c: c[0])
                if previous and context.index - previous["frame"] == 1:
                    dx, dy = x1 - previous["vehicle_box"][0], y1 - previous["vehicle_box"][1]
                    predicted = np.array(previous["box"]) + [dx, dy, dx, dy]
                    if abs((bbox[0]+bbox[2]-predicted[0]-predicted[2])/2) < vw*.4:
                        bbox = (self.alpha*np.array(bbox)+(1-self.alpha)*predicted).tolist()
                lost = 0
            elif previous and context.index - previous["frame"] == 1 and previous["lost"] < self.hold_frames:
                dx, dy = x1 - previous["vehicle_box"][0], y1 - previous["vehicle_box"][1]
                bbox = (np.array(previous["box"]) + [dx, dy, dx, dy]).tolist()
                confidence, lost, held = previous["confidence"]*.9, previous["lost"]+1, True
            else:
                continue
            bbox = [max(0., min(float(limit), v)) for v, limit in zip(bbox, [w, h, w, h])]
            if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                continue
            if key is not None:
                self.history[key] = dict(box=bbox, vehicle_box=[x1,y1,x2,y2], confidence=confidence, lost=lost, frame=context.index)
            center = [(bbox[0]+bbox[2])/2, (bbox[1]+bbox[3])/2]
            records.append(dict(class_id=0, class_name="license_plate", confidence=round(confidence,5),
                                bbox_xyxy=bbox, center=center, bottom_center=[center[0],bbox[3]],
                                track_id=vehicle.get("track_id"), object_id=key, distance_m=distance,
                                held=held))
        self.history = {k:v for k,v in self.history.items() if k in active and context.index-v["frame"] <= self.hold_frames}
        kept = []
        for item in sorted(records, key=lambda r:r["confidence"], reverse=True):
            a = item["bbox_xyxy"]
            duplicate = False
            for other in kept:
                b = other["bbox_xyxy"]
                inter = max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
                union = (a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-inter
                if union and inter/union > .45:
                    duplicate = True
                    break
            if not duplicate:
                kept.append(item)
        return ModelResult(kept)
