"""Perception algorithms adapted to traffic_ai's staged observation contract."""
from copy import deepcopy
from pathlib import Path

import numpy as np

from .base import ModelResult, PerceptionModel
from .legacy import VehicleDetector
from .perception_core import (fuse_rider_and_motorcycles, filter_duplicate_boxes,
                              match_plate_to_vehicle, PlateOCRTracker,
                              TaiwanPlateValidator, ONNXPPOCRv6Recognizer)
from .runtime import yolo_device, onnx_predictor


def box_record(box, **extra):
    x1, y1, x2, y2 = map(float, box[:4])
    center = [(x1+x2)/2, (y1+y2)/2]
    return dict(bbox_xyxy=[x1,y1,x2,y2], center=center,
                bottom_center=[center[0],y2], **extra)


class VehicleModel(PerceptionModel):
    def __init__(self, weights, device="cpu", interval=2, tracker=None, **params):
        if interval < 1:
            raise ValueError("vehicle interval must be positive")
        # Resolve our own tracker so startup never needs package downloads.
        tracker = tracker or Path(__file__).resolve().parent.parent / "configs" / "bytetrack_perception.yaml"
        self.backend = VehicleDetector(weights, device=device, tracker=tracker, **params)
        self.interval = interval
        self.reset()

    def reset(self):
        self.cached = None
        self.source_frame = None
        self.riders, self.seen, self.first_seen = {}, {}, {}
        self.backend.frame_index = -1
        self.backend.track_first_seen.clear()
        for tracker in getattr(getattr(self.backend.model, "predictor", None), "trackers", []):
            tracker.reset()

    def infer(self, frame, context, upstream):
        if self.cached is None or context.index % self.interval == 0:
            raw = self.backend.infer(frame)
            boxes = [r["bbox_xyxy"] + [r["confidence"], r["class_id"], r["track_id"], None] for r in raw]
            for r in raw:
                if r["track_id"] is not None:
                    self.seen[r["track_id"]] = context.index
            self.seen = {k:v for k,v in self.seen.items() if context.index-v <= 90}
            self.riders = {k:v for k,v in self.riders.items() if k in self.seen}
            fused, mappings = fuse_rider_and_motorcycles(boxes, self.riders)
            records = []
            names = {r["class_id"]:r["class_name"] for r in raw}
            names[3] = "motorcycle"
            h,w = frame.shape[:2]
            for b in fused:
                tid = b[6]
                bbox = [float(np.clip(v,0,limit)) for v,limit in zip(b[:4],[w,h,w,h])]
                if tid is not None:
                    self.first_seen.setdefault(tid, context.index)
                records.append(box_record(bbox, class_id=int(b[5]), class_name=names[int(b[5])],
                    confidence=float(b[4]), track_id=tid,
                    object_id=f"vehicle-{tid:06d}" if tid is not None else None,
                    track_age_frames=0 if tid is None else context.index-self.first_seen[tid]+1))
            self.first_seen = {k:v for k,v in self.first_seen.items() if k in self.seen or k in self.riders.values()}
            self.cached = ModelResult(records, {"track_mappings":mappings})
            self.source_frame = context.index
        result = deepcopy(self.cached)  # downstream annotations must not mutate cached detections
        for r in result.records:
            r["source_frame"] = self.source_frame
            r["track_age_frames"] += context.index-self.source_frame if r["track_id"] is not None else 0
        return result

    def close(self):
        self.cached = None
        self.backend = None


class PlateModel(PerceptionModel):
    def __init__(self, weights, device="cpu", confidence=.30, imgsz=320,
                 padding=.08, min_vehicle_size=50, max_distance=22.,
                 smooth_alpha=.65, hold_frames=2, road_scan=True, cuda_conv_search="HEURISTIC"):
        from ultralytics import YOLO
        if not 0 <= smooth_alpha <= 1 or hold_frames < 0:
            raise ValueError("Invalid plate smoothing options")
        self.model = YOLO(str(weights), task="detect")
        self.device = yolo_device(weights, device)
        self.predictor_class = onnx_predictor(cuda_conv_search)
        self.confidence, self.imgsz = confidence, imgsz
        self.padding, self.min_size, self.max_distance = padding, min_vehicle_size, max_distance
        self.alpha, self.hold_frames, self.road_scan = smooth_alpha, hold_frames, road_scan
        self.reset()

    def reset(self):
        self.history = {}

    def _detect(self, crop, left, top, size):
        result = self.model.predict(crop, conf=self.confidence, imgsz=size,
                                    device=self.device, verbose=False, predictor=self.predictor_class)[0]
        boxes = []
        for b in ([] if result.boxes is None else result.boxes):
            x1,y1,x2,y2 = b.xyxy[0].cpu().tolist()
            pw,ph = x2-x1,y2-y1
            if .95 <= pw/max(ph,1) <= 4.5 and pw >= 16 and ph >= 8:
                boxes.append([left+x1,top+y1,left+x2,top+y2,float(b.conf[0].item()),0])
        return boxes

    def infer(self, frame, context, upstream):
        h,w = frame.shape[:2]
        distances = {r["object_id"]:r["distance_m"] for r in upstream["depth"].records if r.get("object_id")}
        vehicles = []
        for r in upstream["vehicles"].records:
            if r["class_id"] not in (0,2,3,5,7):
                continue
            b = r["bbox_xyxy"]
            if r["class_id"] == 0 and (b[3] <= h*.4 or b[2]-b[0] < 35 or b[3]-b[1] < 60):
                continue
            vehicles.append(b+[r["confidence"],r["class_id"],r["track_id"],distances.get(r.get("object_id"))])
        candidates = []
        for v in vehicles:
            x1,y1,x2,y2 = v[:4]
            vw,vh = x2-x1,y2-y1
            if min(vw,vh) < self.min_size or (v[7] is not None and v[7] > self.max_distance):
                continue
            if v[5] == 0:
                px,py,pd = max(vw*.65,100),vh*.1,vh*.35
            elif v[5] == 3:
                px,py,pd = max(vw*.35,60),vh*.1,max(vh*.25,40)
            else:
                px,py,pd = vw*self.padding,vh*self.padding,vh*self.padding
            left,top,right,bottom = max(0,int(x1-px)),max(0,int(y1-py)),min(w,int(x2+px)),min(h,int(y2+pd))
            if right-left >= 10 and bottom-top >= 10:
                candidates.extend(self._detect(frame[top:bottom,left:right],left,top,self.imgsz))
        if self.road_scan:
            top = int(h*.35)
            candidates.extend(self._detect(frame[top:,:],0,top,640))
        records, matched = [], set()
        for b in filter_duplicate_boxes(candidates,.45):
            v = match_plate_to_vehicle(b,vehicles)
            tid = v[6] if v is not None else None
            distance = v[7] if v is not None else None
            if distance is not None and distance > self.max_distance:
                continue
            if tid is not None and tid in matched:
                continue
            prev = self.history.get(tid) if tid is not None else None
            bbox = b[:4]
            if prev and context.index-prev["frame"] == 1:
                dx,dy = v[0]-prev["vehicle_box"][0],v[1]-prev["vehicle_box"][1]
                predicted = np.array(prev["box"])+[dx,dy,dx,dy]
                if abs((bbox[0]+bbox[2]-predicted[0]-predicted[2])/2) < (v[2]-v[0])*.4:
                    bbox = (self.alpha*np.array(bbox)+(1-self.alpha)*predicted).tolist()
            if tid is not None:
                matched.add(tid)
                self.history[tid] = dict(box=bbox,vehicle_box=v[:4],confidence=b[4],lost=0,frame=context.index)
            records.append(self._record(bbox,b[4],v,False))
        for v in vehicles:
            tid = v[6]
            prev = self.history.get(tid)
            if tid in matched or not prev or context.index-prev["frame"] != 1 or prev["lost"] >= self.hold_frames:
                continue
            if v[7] is not None and v[7] > self.max_distance:
                continue
            dx,dy = v[0]-prev["vehicle_box"][0],v[1]-prev["vehicle_box"][1]
            bbox = (np.array(prev["box"])+[dx,dy,dx,dy]).tolist()
            bbox = [float(np.clip(x,0,lim)) for x,lim in zip(bbox,[w,h,w,h])]
            if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                continue
            prev.update(box=bbox,vehicle_box=v[:4],confidence=prev["confidence"]*.9,lost=prev["lost"]+1,frame=context.index)
            records.append(self._record(bbox,prev["confidence"],v,True))
        self.history = {k:v for k,v in self.history.items() if v["frame"] == context.index}
        # Remove holdover boxes overlapping a current detection.
        indexed = [r["bbox_xyxy"]+[r["confidence"],i] for i,r in enumerate(records)]
        return ModelResult([records[b[5]] for b in filter_duplicate_boxes(indexed,.4)])

    @staticmethod
    def _record(bbox, confidence, vehicle, held):
        tid = vehicle[6] if vehicle is not None else None
        return box_record(bbox,class_id=0,class_name="license_plate",confidence=confidence,
            track_id=tid,object_id=f"vehicle-{tid:06d}" if tid is not None else None,
            vehicle_class_id=vehicle[5] if vehicle is not None else None,
            distance_m=vehicle[7] if vehicle is not None else None,held=held)

    def close(self):
        self.model = None
        self.reset()


class OCRModel(PerceptionModel):
    def __init__(self, weights, device="cpu", dict_path=None, interval=4, min_width=58,
                 confidence=.5, max_distance=22., taiwan_filter=True, cache_ttl=90):
        if interval < 1 or cache_ttl < 1:
            raise ValueError("OCR interval and cache_ttl must be positive")
        if Path(weights).is_dir():
            if not dict_path:
                raise ValueError("Paddle PP-OCRv6 requires dict_path")
            from .ppocr_client import PaddleInferWorkerRecognizer
            self.model = PaddleInferWorkerRecognizer(weights,dict_path,device)
        else:
            self.model = ONNXPPOCRv6Recognizer(str(weights),device)
        self.options = dict(interval=interval,min_width=min_width,conf_thresh=confidence,
                            max_dist=max_distance,enable_taiwan_filter=taiwan_filter)
        self.cache_ttl = cache_ttl
        self.reset()

    def reset(self):
        self.tracker = PlateOCRTracker(**self.options)
        self.seen, self.attempts = {}, {}

    def infer(self, frame, context, upstream):
        self.seen = {k:v for k,v in self.seen.items() if context.index-v <= self.cache_ttl}
        self.attempts = {k:v for k,v in self.attempts.items() if k in self.seen}
        self.tracker.records = {k:v for k,v in self.tracker.records.items() if k in self.seen}
        for src,dst in upstream["vehicles"].artifacts.get("track_mappings",{}).items():
            self.tracker.merge_tracks(src,dst)
            if src in self.seen:
                self.seen[dst] = max(self.seen.get(dst, -1), self.seen.pop(src))
            if src in self.attempts:
                self.attempts[dst] = max(self.attempts.get(dst, -1), self.attempts.pop(src))
        # Cache lifetime follows the vehicle, even when its plate is hidden.
        for vehicle in upstream["vehicles"].records:
            tid, cls = vehicle.get("track_id"), vehicle.get("class_id")
            if tid is None:
                continue
            self.seen[tid] = context.index
            old = self.tracker.records.get(tid)
            if old and old.get("veh_cls") is not None and cls is not None:
                if (old["veh_cls"] in (0, 1, 3)) != (cls in (0, 1, 3)):
                    self.tracker.records.pop(tid)
                    self.attempts.pop(tid, None)
        output = []
        h,w = frame.shape[:2]
        for plate in upstream["plates"].records:
            tid = plate.get("track_id")
            cls = plate.get("vehicle_class_id")
            if tid is not None:
                self.seen[tid] = context.index
                old = self.tracker.records.get(tid)
                if old and old.get("veh_cls") is not None and cls is not None and ((old["veh_cls"] in (0,1,3)) != (cls in (0,1,3))):
                    self.tracker.records.pop(tid)
                    self.attempts.pop(tid,None)
            x1,y1,x2,y2 = plate["bbox_xyxy"]
            raw,score = None,0.
            last = self.attempts.get(tid,-10000) if tid is not None else -10000
            if not plate.get("held") and context.index-last >= self.options["interval"] and self.tracker.should_infer(tid,context.index,x2-x1,plate.get("distance_m")):
                dx,dy = (x2-x1)*.05,(y2-y1)*.08
                roi = frame[max(0,int(y1-dy)):min(h,int(y2+dy)),max(0,int(x1-dx)):min(w,int(x2+dx))]
                if tid is not None:
                    self.attempts[tid] = context.index
                if roi.shape[0] >= 8 and roi.shape[1] >= 16:
                    raw,score = self.model.predict(roi)
                    self.tracker.update(tid,context.index,raw,score,veh_cls=cls,plate_w=x2-x1)
            if tid is not None:
                raw,score = self.tracker.get_plate_raw(tid,cls)
                text,_ = self.tracker.get_plate(tid,cls)
            else:
                text = raw if score >= self.options["conf_thresh"] else None
            if self.options["enable_taiwan_filter"]:
                text = TaiwanPlateValidator.validate_and_normalize(text)
            output.append({**plate,"raw_text":raw,"text":text,"text_confidence":float(score),
                "confirmed":self.tracker.records.get(tid,{}).get("confirmed",False),
                "ocr_source_frame":self.attempts.get(tid) if tid is not None else (context.index if raw else None)})
        vehicle_plates, vehicle_scores = {}, {}
        for vehicle in upstream["vehicles"].records:
            tid = vehicle.get("track_id")
            text, score = self.tracker.get_plate(tid, vehicle.get("class_id"))
            if self.options["enable_taiwan_filter"]:
                text = TaiwanPlateValidator.validate_and_normalize(text)
            if text and vehicle.get("object_id"):
                vehicle_plates[vehicle["object_id"]] = text
                vehicle_scores[vehicle["object_id"]] = float(score)
        return ModelResult(output, {"vehicle_plate_map": vehicle_plates,
                                    "vehicle_plate_confidence_map": vehicle_scores})

    def close(self):
        if hasattr(self.model,"close"):
            self.model.close()
        self.model = None
        self.reset()
