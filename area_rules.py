"""Road-area extraction, temporal geometry and review-candidate rules.

Estimated ground geometry is explicitly labelled; it is not a measured footprint.
Image-space lane calibration is only supported for an explicitly fixed camera.
"""
import json
import math
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

from core.continuity import is_continuous
from core.geometry import project_points
from core.motion import homography, road_motion
from core.fastcopy import clone


AREA_RULES = {
    "car_motorcycle_waiting_area": ("motorcycle_waiting_area", "紅燈時汽車侵入機車停等區"),
    "crosswalk_stopping": ("crosswalk", "車輛停留於行人穿越道"),
}
DEFAULTS = dict(min_confidence=.5, min_observations=3, intrusion_seconds=.15,
                overlap_ratio=.5, stationary_seconds=1., stationary_radius_px=4.,
                min_intrusion_length_px=2., max_observation_gap_sec=.3,
                max_frame_gap_sec=.2)


def settings_for(overrides=None):
    if overrides is not None and (not isinstance(overrides, dict) or set(overrides) - DEFAULTS.keys()):
        raise ValueError("Unknown area-rule settings")
    settings = {**DEFAULTS, **(overrides or {})}
    for key, value in settings.items():
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be finite and positive")
    if type(settings["min_observations"]) is not int or settings["min_observations"] < 2:
        raise ValueError("min_observations must be an integer >= 2")
    if max(settings["overlap_ratio"], settings["min_confidence"]) > 1:
        raise ValueError("Ratios must be <= 1")
    return settings


def area_rule_options(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.pop("version", None) != 1:
        raise ValueError("Area-rule config requires version=1")
    geometry_options(data.pop("geometry", {}))
    settings = settings_for(data)
    return {name: {"settings": settings} for name in AREA_RULES}


GEOMETRY_DEFAULTS = dict(close_gap_ratio=.025, min_region_pixels=120,
    min_region_side_px=12., min_fill_ratio=.08, max_region_frame_ratio=.45,
    region_ttl_sec=.4, region_match_iou=.25, estimated_quality=.6,
    footprint_height_ratio=.22, footprint_width_ratio=.85,
    duplicate_iou=.85, plate_link_seconds=.3, plate_link_confidence=.9,
    signal_confirm_seconds=.15, fixed_camera=False, lanes=[], ego_polygons=[])


def geometry_options(data=None):
    if data is None:
        data = {}
    if not isinstance(data, dict) or set(data) - GEOMETRY_DEFAULTS.keys():
        raise ValueError("Unknown area geometry settings")
    cfg = clone({**GEOMETRY_DEFAULTS, **(data or {})})
    for key, default in GEOMETRY_DEFAULTS.items():
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            value = cfg[key]
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"geometry.{key} must be finite and positive")
            if ("ratio" in key or "iou" in key or "confidence" in key or "quality" in key) and value > 1:
                raise ValueError(f"geometry.{key} must be <= 1")
    if type(cfg["fixed_camera"]) is not bool:
        raise ValueError("fixed_camera must be boolean")
    if not isinstance(cfg["lanes"], list) or not isinstance(cfg["ego_polygons"], list):
        raise ValueError("lanes and ego_polygons must be arrays")
    if cfg["lanes"] and not cfg["fixed_camera"]:
        raise ValueError("Image-space lanes require fixed_camera=true")
    ids = set()
    for lane in cfg["lanes"]:
        if set(lane) - {"lane_id", "polygon", "signal_roi", "heading"} or not lane.get("lane_id") or lane["lane_id"] in ids:
            raise ValueError("Each lane needs a unique lane_id and known fields")
        ids.add(lane["lane_id"])
        _normalized_polygon(lane["polygon"])
        if "signal_roi" in lane:
            _normalized_polygon(lane["signal_roi"])
        if "heading" in lane:
            heading = np.asarray(lane["heading"], dtype=float)
            if heading.shape != (2,) or not np.isfinite(heading).all() or np.linalg.norm(heading) < 1e-6:
                raise ValueError("heading must be a nonzero image-space direction")
    for points in cfg["ego_polygons"]:
        _normalized_polygon(points)
    return cfg


def _normalized_polygon(points):
    result = polygon(np.asarray(points, dtype=np.float32) * 1000) / 1000
    if np.any(result < 0) or np.any(result > 1):
        raise ValueError("Calibration coordinates must be normalized to [0, 1]")
    return result


def area_geometry_options(path):
    return geometry_options(json.loads(Path(path).read_text(encoding="utf-8-sig")).get("geometry", {}))


def _polygon_iou(a, b):
    a, b = np.asarray(a, np.float32), np.asarray(b, np.float32)
    intersection, _ = cv2.intersectConvexConvex(a, b)
    return float(intersection / max(1., cv2.contourArea(a) + cv2.contourArea(b) - intersection))


def _box_polygon(box):
    x1, y1, x2, y2 = box
    return np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]], np.float32)


def extract_traffic_regions(mask, markings, cfg):
    """Join only nearby semantic fragments, then retain each group's convex hull.

    A hull includes gaps between crosswalk stripes. Sparse/line-like components
    are rejected instead of extending a single boundary into an invented area.
    """
    if mask is None:
        return []
    h, w = mask.shape
    gap = max(1, round(min(h, w) * cfg["close_gap_ratio"]))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (gap * 2 + 1, gap * 2 + 1))
    regions = []
    for marking in markings:
        kind = {"motor waiting zone": "motorcycle_waiting_area"}.get(
            marking["class_name"], marking["class_name"])
        if kind not in {"crosswalk", "motorcycle_waiting_area"}:
            continue
        selected = mask == marking["class_id"]
        rows, cols = np.flatnonzero(selected.any(axis=1)), np.flatnonzero(selected.any(axis=0))
        if not len(rows):
            continue
        # 閉運算只在外擴 2×gap 的範圍內進行：膨脹最多外擴 gap，侵蝕再往外看 gap，
        # 因此與全畫面運算結果相同，但面積小得多。
        pad = 2 * gap + 1
        top, left = max(0, int(rows[0]) - pad), max(0, int(cols[0]) - pad)
        bottom, right = min(h, int(rows[-1]) + pad + 1), min(w, int(cols[-1]) + pad + 1)
        binary = selected[top:bottom, left:right].astype(np.uint8)
        joined = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
        count, labels = cv2.connectedComponents(joined)
        # 一次取出原始語意像素並依連通區塊分組（穩定排序保留逐列順序），
        # 避免對每個區塊在全畫面重做 labels == label 比對。
        all_ys, all_xs = np.nonzero(binary)
        owner = labels[all_ys, all_xs]
        order = np.argsort(owner, kind="stable")
        bounds = np.searchsorted(owner[order], np.arange(count + 1))
        for label in range(1, count):
            members = order[bounds[label]:bounds[label + 1]]
            ys, xs = all_ys[members] + top, all_xs[members] + left
            if len(xs) < cfg["min_region_pixels"]:
                continue
            hull = cv2.convexHull(np.column_stack((xs, ys)).astype(np.float32)).reshape(-1, 2)
            area = cv2.contourArea(hull)
            if (area <= 1 or area > h*w*cfg["max_region_frame_ratio"] or
                    len(xs)/area < cfg["min_fill_ratio"] or
                    min(cv2.minAreaRect(hull)[1]) < cfg["min_region_side_px"]):
                continue
            regions.append(dict(kind=kind, polygon=hull.tolist(), confidence=cfg["estimated_quality"],
                method="semantic_fragment_hull", quality_kind="heuristic_not_probability",
                semantic_pixels=len(xs)))
    # 固定輸出順序（cv2 標號順序會隨裁切範圍改變）：類別內依多邊形最上、最左點排序。
    regions.sort(key=lambda r: (r["kind"], min(p[1] for p in r["polygon"]), min(p[0] for p in r["polygon"])))
    return regions


def attach_area_geometry(record, mask, state, cfg):
    """Enrich the existing observation in place; state is an ordinary per-video dict.

    Raw/rider vehicle identities remain intact for other rules. Only area rules
    consume area_vehicles, where duplicate detections and configured ego areas
    are filtered. Never bridge an ID change using position alone.
    """
    frame, obs = record["frame"], record["observations"]
    index, now = frame["index"], frame["timestamp_sec"]
    size = (frame["width"], frame["height"])
    if not is_continuous(state.get("frame"), frame, .2):
        state.clear()
    state["frame"] = dict(frame)
    post = record.setdefault("postprocessing", {})
    motion = road_motion(record)
    if cfg["fixed_camera"]:
        motion = dict(valid=True, matrix=np.eye(3).tolist(), method="configured_fixed_camera")
    matrix = homography(motion)
    valid_motion = matrix is not None
    old = []
    if valid_motion:
        for region in state.get("regions", []):
            if now-region["last_seen_sec"] <= cfg["region_ttl_sec"]:
                try:
                    item = clone(region)
                    item["polygon"] = polygon(project_points(item["polygon"], matrix)).tolist()
                    old.append(item)
                except (ValueError, cv2.error):
                    pass
    mask_ok = mask is not None and np.asarray(mask).shape == (size[1], size[0])
    current = extract_traffic_regions(mask if mask_ok else None,
        obs.get("road_markings_raw", obs.get("road_markings", [])), cfg)
    used = set()
    for region in current:
        scores = sorted([(_polygon_iou(region["polygon"], item["polygon"]), n)
                         for n, item in enumerate(old) if item["kind"] == region["kind"] and n not in used], reverse=True)
        if scores and scores[0][0] >= cfg["region_match_iou"]:
            match = scores[0][1]
            used.add(match)
            region["region_id"] = old[match]["region_id"]
        else:
            state["sequence"] = state.get("sequence", 0) + 1
            region["region_id"] = f"area-{state['sequence']:06d}"
        region.update(last_seen_sec=now, observed_frame=index, source_frame=index, predicted=False)
    for n, region in enumerate(old):
        if n not in used and not any(r["kind"] == region["kind"] and
                                    _polygon_iou(r["polygon"], region["polygon"]) > .05 for r in current):
            region.update(source_frame=index, predicted=True, method="road_motion_propagated")
            current.append(region)
    lanes = _associate_area_lanes(current, obs, state, cfg, frame)
    state["regions"] = clone(current)
    vehicles, excluded = _prepare_area_vehicles(obs, state, cfg, frame, matrix, valid_motion, lanes)
    obs["traffic_regions"], obs["area_vehicles"] = current, vehicles
    post["area_geometry"] = dict(enabled=True, motion=motion, mask_available=mask_ok,
        observed_regions=sum(not r["predicted"] for r in current),
        predicted_regions=sum(r["predicted"] for r in current), excluded=excluded,
        parameters=cfg)
    return record


def _associate_area_lanes(regions, obs, state, cfg, frame):
    """Associate only explicit lane/traffic-light calibrations, never all red lights."""
    size = (frame["width"], frame["height"])
    index, now = frame["index"], frame["timestamp_sec"]
    lanes = []
    for lane in cfg["lanes"]:
        item = dict(lane, polygon=(np.asarray(lane["polygon"]) * size).astype(np.float32))
        if "signal_roi" in lane:
            item["signal_roi"] = (np.asarray(lane["signal_roi"]) * size).astype(np.float32)
        lanes.append(item)
    signals = state.setdefault("signals", {})
    for lane in lanes:
        candidates = [light for light in obs.get("traffic_lights", [])
            if "signal_roi" in lane and light.get("source_frame", index) == index and
            light.get("confidence", 0) >= .5 and cv2.pointPolygonTest(lane["signal_roi"],
            tuple(map(float, light["center"])), False) >= 0]
        colors = {light["class_name"] for light in candidates}
        signal = signals.get(lane["lane_id"])
        if len(colors) != 1:
            signals.pop(lane["lane_id"], None)
            continue
        color = next(iter(colors))
        if not signal or signal["state"] != color:
            signal = dict(state=color, since=now)
            signals[lane["lane_id"]] = signal
        if now-signal["since"] >= cfg["signal_confirm_seconds"]:
            lane["signal"] = dict(state=color, confidence=min(x["confidence"] for x in candidates),
                                  lane_id=lane["lane_id"], source_frame=index)
    for region in regions:
        center = np.asarray(region["polygon"], np.float32).mean(axis=0)
        matches = [lane for lane in lanes if cv2.pointPolygonTest(lane["polygon"], tuple(map(float, center)), False) >= 0]
        if len(matches) == 1:
            region["lane_id"] = matches[0]["lane_id"]
            region["signal"] = matches[0].get("signal", {})
        else:
            region.pop("lane_id", None)
            region.pop("signal", None)
    return lanes


def _prepare_area_vehicles(obs, state, cfg, frame, matrix, valid_motion, lanes):
    """Filter duplicates/ego detections and estimate explicitly labelled geometry."""
    size = (frame["width"], frame["height"])
    index, now = frame["index"], frame["timestamp_sec"]
    vehicles, excluded = [], []
    raw = obs.get("rule_vehicles", obs.get("vehicles", []))
    for original in sorted(raw, key=lambda v: v.get("confidence", 0), reverse=True):
        if original.get("class_name") not in {"car", "truck", "bus", "motorcycle", "bicycle"}:
            continue
        vehicle = clone(original)
        box = vehicle["bbox_xyxy"]
        if not np.isfinite(box).all() or box[2] <= box[0] or box[3] <= box[1]:
            excluded.append(dict(object_id=vehicle.get("object_id"), reason="invalid_box"))
            continue
        anchor = ((box[0]+box[2])/2, box[3])
        reason = None
        if any(cv2.pointPolygonTest((np.asarray(p)*size).astype(np.float32), anchor, False) >= 0 for p in cfg["ego_polygons"]):
            reason = "configured_ego_area"
        family = "car" if vehicle["class_name"] in {"car", "truck", "bus"} else vehicle["class_name"]
        if any(v["area_class_family"] == family and _polygon_iou(_box_polygon(box),
               _box_polygon(v["bbox_xyxy"])) >= cfg["duplicate_iou"] for v in vehicles):
            reason = "duplicate_box"
        if reason:
            excluded.append(dict(object_id=vehicle.get("object_id"), reason=reason))
            continue
        vehicle["area_class_family"] = family
        vehicle["source_object_id"] = vehicle.get("source_object_id", vehicle.get("object_id"))
        vehicles.append(vehicle)
    history = state.get("vehicles", []) if valid_motion else []
    fresh = [v for v in vehicles if v.get("source_frame") == index]
    for vehicle in fresh:
        text = vehicle.get("plate_text")
        candidates = [v for v in history if text and v.get("plate_text") == text and
            v.get("area_class_family") == vehicle["area_class_family"] and
            min(v.get("text_confidence") or 0, vehicle.get("text_confidence") or 0) >= cfg["plate_link_confidence"] and
            now-v["last_seen_sec"] <= cfg["plate_link_seconds"] and
            _polygon_iou(project_points(_box_polygon(v["bbox_xyxy"]), matrix), _box_polygon(vehicle["bbox_xyxy"])) >= .5]
        # Plate must be unique now and in history, with a close projected box.
        if len(candidates) == 1 and sum(v.get("plate_text") == text for v in vehicles) == 1:
            vehicle["object_id"] = candidates[0]["object_id"]
        x1, y1, x2, y2 = vehicle["bbox_xyxy"]
        if vehicle.get("road_geometry", {}).get("source_frame") == index:
            continue  # Preserve measured or externally calibrated geometry.
        if x1 <= 1 or y1 <= 1 or x2 >= size[0]-1 or y2 >= size[1]-1:
            vehicle["geometry_unavailable_reason"] = "clipped_vehicle"
            continue
        inset = (x2-x1)*(1-cfg["footprint_width_ratio"])/2
        top = y2-(y2-y1)*cfg["footprint_height_ratio"]
        footprint = [[x1+inset, top], [x2-inset, top], [x2-inset/2, y2], [x1+inset/2, y2]]
        geometry = dict(source_frame=index, confidence=cfg["estimated_quality"],
            method="lower_body_trapezoid_estimate", quality_kind="heuristic_not_probability",
            footprint=footprint, estimated=True)
        matches = [lane for lane in lanes if cv2.pointPolygonTest(lane["polygon"], ((x1+x2)/2, y2), False) >= 0]
        if len(matches) == 1:
            lane = matches[0]
            vehicle["lane_id"] = lane["lane_id"]
            if "heading" in lane:
                p = np.asarray(footprint)
                edges = [(p[n], p[(n+1)%4]) for n in range(4)]
                edge = max(edges, key=lambda pair: np.dot((pair[0]+pair[1])/2, lane["heading"]))
                geometry["front_edge"] = np.asarray(edge).tolist()
        vehicle["road_geometry"] = geometry
    # Cached frames retain canonical IDs, but never become fresh measurements.
    aliases = state.setdefault("aliases", {})
    for vehicle in vehicles:
        source_id = vehicle.get("source_object_id")
        if vehicle.get("source_frame") == index:
            aliases[source_id] = (vehicle.get("object_id"), now)
        elif source_id in aliases and now-aliases[source_id][1] <= cfg["plate_link_seconds"]:
            vehicle["object_id"] = aliases[source_id][0]
    state["aliases"] = {k: v for k, v in aliases.items() if now-v[1] <= cfg["plate_link_seconds"]}
    if fresh:
        state["vehicles"] = [dict(v, last_seen_sec=now) for v in fresh]
    elif valid_motion:
        # History boxes must stay in the previous frame's coordinate system.
        state["vehicles"] = [dict(v, bbox_xyxy=[*np.min(project_points(_box_polygon(v["bbox_xyxy"]), matrix), axis=0),
                                               *np.max(project_points(_box_polygon(v["bbox_xyxy"]), matrix), axis=0)])
                             for v in history if now-v["last_seen_sec"] <= cfg["plate_link_seconds"]]
    else:
        state["vehicles"] = []
    return vehicles, excluded


def draw_area_geometry(frame, record):
    for region in record["observations"].get("traffic_regions", []):
        points = np.asarray(region["polygon"], np.int32)
        color = (0, 170, 255) if region.get("predicted") else (230, 200, 60)
        cv2.polylines(frame, [points], True, color, 2)
        cv2.putText(frame, region["region_id"], tuple(points[0]), cv2.FONT_HERSHEY_SIMPLEX, .45, color, 1)
    for vehicle in record["observations"].get("area_vehicles", []):
        geometry = vehicle.get("road_geometry", {})
        if geometry.get("source_frame") != record["frame"]["index"]:
            continue
        if "footprint" in geometry:
            cv2.polylines(frame, [np.asarray(geometry["footprint"], np.int32)], True, (80, 220, 100), 1)
        if "front_edge" in geometry:
            cv2.polylines(frame, [np.asarray(geometry["front_edge"], np.int32)], False, (0, 100, 255), 3)
    return frame


def polygon(value):
    points = np.asarray(value, dtype=np.float32)
    if (points.ndim != 2 or points.shape[1] != 2 or len(points) < 3
            or not np.isfinite(points).all() or not cv2.isContourConvex(points)
            or cv2.contourArea(points) <= 1):
        raise ValueError("A nondegenerate convex polygon is required")
    return points


def front_overlap(edge, region):
    """Clip the finite front segment to a convex region, including crossing ends."""
    edge = np.asarray(edge, dtype=float)
    if edge.shape != (2, 2) or not np.isfinite(edge).all():
        raise ValueError("front_edge must contain two finite points")
    direction = edge[1] - edge[0]
    length = float(np.linalg.norm(direction))
    if length <= 0:
        raise ValueError("Degenerate front edge")
    orientation = 1 if cv2.contourArea(region, oriented=True) > 0 else -1
    low, high = 0., 1.
    for a, b in zip(region, np.roll(region, -1, axis=0)):
        side = b - a
        normal = orientation * np.array([-side[1], side[0]])
        offset, slope = np.dot(normal, edge[0] - a), np.dot(normal, direction)
        if abs(slope) < 1e-9:
            if offset <= 0:
                return 0.  # Merely lying on the boundary is not intrusion.
        elif slope > 0:
            low = max(low, -offset / slope)
        else:
            high = min(high, -offset / slope)
    return max(0., high - low) * length


class AreaOccupancyRule:
    """One shared state machine; no geometry estimators or model objects here."""

    def __init__(self, context, *, rule_id, settings=None):
        self.context, self.rule_id = context, rule_id
        self.kind, self.label = AREA_RULES[rule_id]
        self.settings = settings_for(settings)
        self.states, self.previous_frame, self.diagnostics = {}, None, {}

    def evaluate(self, observation):
        frame, items, cfg = observation["frame"], observation.get("observations", {}), self.settings
        index, now = frame["index"], frame["timestamp_sec"]
        self.diagnostics = dict(status="evaluating", blocked=[], states=[], measurements=[])
        if not is_continuous(self.previous_frame, frame, cfg["max_frame_gap_sec"]):
            self.states.clear()
            self.diagnostics["reset_reason"] = "discontinuous_input"
        self.previous_frame = dict(frame)
        regions = [r for r in items.get("traffic_regions", []) if r.get("kind") == self.kind]
        if not regions:
            self.states.clear()
            self.diagnostics.update(status="blocked", reason="region_geometry_unavailable")
            return []
        matrix = None
        if self.kind == "crosswalk":
            post = observation.get("postprocessing", {})
            motion = post.get("area_geometry", {}).get("motion", road_motion(observation))
            try:
                matrix = homography(motion)
                if matrix is None:
                    raise ValueError("motion_unavailable")
                for state in self.states.values():
                    state["anchor"] = project_points([state["anchor"]], matrix)[0]
            except (TypeError, ValueError):
                self.states.clear()
                self.diagnostics.update(status="blocked", reason="motion_unavailable")
                return []
        events, retained = [], set()
        vehicles = items.get("area_vehicles", items.get("rule_vehicles", items.get("vehicles", [])))
        region_ids = Counter(r.get("region_id") for r in regions)
        vehicle_ids = Counter(v.get("object_id") for v in vehicles)
        for region in regions:
            for vehicle in vehicles:
                identity, region_id = vehicle.get("object_id"), region.get("region_id")
                key = (identity, region_id)
                try:
                    if not identity or not region_id:
                        raise ValueError("missing_identity")
                    if vehicle_ids[identity] != 1 or region_ids[region_id] != 1:
                        raise ValueError("ambiguous_identity")
                    allowed = {"car", "truck", "bus"} if self.kind == "motorcycle_waiting_area" else {"car", "truck", "bus", "motorcycle", "bicycle"}
                    if vehicle.get("class_name") not in allowed:
                        continue
                    if region.get("source_frame") != index:
                        raise ValueError("stale_region")
                    area = polygon(region.get("polygon"))
                    for item in (region, vehicle):
                        confidence = item.get("confidence", 0)
                        if not isinstance(confidence, (float, int)) or not math.isfinite(confidence) or not cfg["min_confidence"] <= confidence <= 1:
                            raise ValueError("low_confidence")
                    state = self.states.get(key)
                    if state and now - state["last_time"] > cfg["max_observation_gap_sec"]:
                        state = None
                    if self.kind == "motorcycle_waiting_area":
                        signal = region.get("signal", {})
                        if (not region.get("lane_id") or vehicle.get("lane_id") != region["lane_id"] or
                                signal.get("lane_id") != region["lane_id"] or
                                signal.get("source_frame") != index or signal.get("state") != "red" or
                                not cfg["min_confidence"] <= float(signal.get("confidence", 0)) <= 1):
                            raise ValueError("associated_red_signal_unavailable")
                    if vehicle.get("source_frame") != index:
                        if state:
                            retained.add(key)
                        continue  # Cached detections cannot add evidence or trigger events.
                    geometry = vehicle.get("road_geometry", {})
                    if geometry.get("source_frame") != index or not geometry.get("method"):
                        raise ValueError("vehicle_geometry_unavailable")
                    confidence = geometry.get("confidence", 0)
                    if not isinstance(confidence, (float, int)) or not math.isfinite(confidence) or not cfg["min_confidence"] <= confidence <= 1:
                        raise ValueError("low_geometry_confidence")
                    if self.kind == "motorcycle_waiting_area":
                        if "front_edge" not in geometry:
                            raise ValueError("front_direction_unavailable")
                        measured = front_overlap(geometry.get("front_edge"), area)
                        self.diagnostics["measurements"].append(dict(object_id=identity, region_id=region_id,
                            measurement=measured, threshold=cfg["min_intrusion_length_px"], name="front_overlap_px"))
                        if measured < cfg["min_intrusion_length_px"]:
                            continue
                        point, duration = None, cfg["intrusion_seconds"]
                    else:
                        footprint = polygon(geometry.get("footprint"))
                        intersection, _ = cv2.intersectConvexConvex(footprint, area)
                        measured = float(intersection / cv2.contourArea(footprint))
                        self.diagnostics["measurements"].append(dict(object_id=identity, region_id=region_id,
                            measurement=measured, threshold=cfg["overlap_ratio"], name="footprint_overlap_ratio"))
                        if measured + 1e-6 < cfg["overlap_ratio"]:
                            continue
                        point, duration = footprint.mean(axis=0).tolist(), cfg["stationary_seconds"]
                        if state and np.linalg.norm(np.asarray(point) - state["anchor"]) > cfg["stationary_radius_px"]:
                            state = None
                    snapshot = clone(dict(frame=frame, vehicle=vehicle, region=region, measurement=measured))
                    if state is None:
                        state = dict(since=now, count=0, anchor=point, before=snapshot, reported=False)
                    state.update(last_time=now, count=state["count"] + 1)
                    self.states[key] = state
                    retained.add(key)
                    if state["reported"] or state["count"] < cfg["min_observations"] or now - state["since"] + 1e-6 < duration:
                        continue
                    event = dict(event_id=f"{self.context.source_id}:{self.rule_id}:{identity}:{region_id}:{index}",
                                 rule_id=self.rule_id, source_id=self.context.source_id, status="suspected",
                                 status_label="疑似違規", review_required=True, behavior=self.rule_id,
                                 confirmation_scope="configured_candidate_conditions",
                                 description=self.label + "；符合設定門檻，待人工覆核。",
                                 frame=index, timestamp_sec=now, object_id=identity, vehicle_class=vehicle["class_name"],
                                 conditions={"fresh_geometry": True, "temporal_support": True},
                                 evidence=dict(vehicle=vehicle, region=region, before=state["before"], after=snapshot,
                                               measurement=measured, duration_sec=now-state["since"], parameters=cfg,
                                               measurement_name="front_overlap_px" if point is None else "footprint_overlap_ratio"))
                    event["conditions"]["estimated_vehicle_geometry"] = bool(geometry.get("estimated"))
                    event["conditions"]["predicted_region"] = bool(region.get("predicted"))
                    if geometry.get("estimated"):
                        event["description"] = self.label + "；依車輛接地近似符合設定門檻，待人工覆核。"
                    events.append(clone(event))
                    state["reported"] = True
                except (ValueError, TypeError, KeyError, cv2.error) as exc:
                    self.diagnostics["blocked"].append(dict(object_id=identity, region_id=region_id, reason=str(exc)))
        # 偵測器短暫漏掉車輛時保留累積中的狀態，直到 max_observation_gap_sec；
        # 車輛仍在但已離開區域或條件不符者照舊清除。
        self.states = {key: state for key, state in self.states.items() if key in retained or (
            key[0] not in vehicle_ids and now - state["last_time"] <= cfg["max_observation_gap_sec"])}
        self.diagnostics["states"] = [dict(object_id=k[0], region_id=k[1], count=s["count"], reported=s["reported"])
                                      for k, s in self.states.items()]
        return events

    def finalize(self):
        self.states.clear()
        self.previous_frame = None
        return []
