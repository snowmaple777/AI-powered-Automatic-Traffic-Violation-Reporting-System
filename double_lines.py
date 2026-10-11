"""Finite double-line geometry and conservative frame-to-frame association.

Plain dictionaries are the persisted contract and the in-memory tracker state.
No crossing decisions or inferred/occluded line extensions belong in this layer.
"""
import copy
import json
import math
from pathlib import Path

import cv2
import numpy as np


DOUBLE_LINE_CLASSES = {"solid double yellow", "solid double white"}

# All rule thresholds are overridable by configs/double_line_rules.json.
RULE_DEFAULTS = dict(
    min_line_observations=3, min_line_length_px=30.0, min_pair_gap_px=2.0,
    max_pair_angle_deg=20.0, max_pair_gap_ratio=4.0, endpoint_margin_px=6.0,
    side_margin_px=4.0, margin_width_ratio=.2, max_motion_error_px=5.0,
    min_vehicle_confidence=.35, stable_observations=3, stable_seconds=.15,
    max_observation_gap_sec=.3, max_frame_gap_sec=.2, max_crossing_seconds=6.0,
    max_box_scale_ratio=1.7, max_step_box_ratio=1.0, rearm_seconds=1.0,
    heading_window_sec=.6, min_heading_displacement_px=8.0,
    heading_consistency=.8, max_lane_change_angle_deg=35.0,
    min_uturn_angle_deg=140.0, min_longitudinal_alignment=.7, emit_suspected=True,
    allow_semantic_band=True, max_partial_support_seconds=3.0,
    min_fragment_length_px=45.0, fragment_match_distance_px=12.0, max_occluded_gap_sec=.5,
    footprint_inset_ratio=.15)


def validate_rule_settings(overrides=None):
    overrides = {} if overrides is None else overrides
    if not isinstance(overrides, dict) or set(overrides) - RULE_DEFAULTS.keys():
        raise ValueError("Unknown double-line rule settings")
    settings = {**RULE_DEFAULTS, **overrides}
    for key, value in settings.items():
        if key in ("emit_suspected", "allow_semantic_band"):
            if type(value) is not bool:
                raise ValueError(f"{key} must be boolean")
        elif type(value) not in (int, float) or not math.isfinite(value) or (value < 0 if key == "footprint_inset_ratio" else value <= 0):
            raise ValueError(f"{key} must be finite and positive")
    for key in ("min_line_observations", "stable_observations"):
        if type(settings[key]) is not int or settings[key] < 2:
            raise ValueError(f"{key} must be an integer >= 2")
    for key in ("min_vehicle_confidence", "heading_consistency", "min_longitudinal_alignment"):
        if settings[key] > 1:
            raise ValueError(f"{key} must be <= 1")
    if not 0 < settings["max_lane_change_angle_deg"] < settings["min_uturn_angle_deg"] <= 180:
        raise ValueError("Lane-change and U-turn angle ranges must not overlap")
    if settings["max_pair_angle_deg"] >= 90 or settings["max_pair_gap_ratio"] < 1 or settings["max_box_scale_ratio"] <= 1:
        raise ValueError("Invalid geometry or box stability limits")
    if settings["stable_seconds"] >= settings["max_crossing_seconds"]:
        raise ValueError("Stable duration must be shorter than crossing timeout")
    if settings["footprint_inset_ratio"] >= .5:
        raise ValueError("footprint_inset_ratio must be less than .5")
    return settings


def double_line_rule_options(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.pop("version", None) != 1:
        raise ValueError("Double-line rule config requires version=1")
    settings = validate_rule_settings(data)
    return {f"double_{color}_line_crossing": {"settings": settings}
            for color in ("yellow", "white")}


def _distance(first, second):
    """Compare curves only over shared visible y support; infinity means no match."""
    a, b = np.asarray(first, dtype=float), np.asarray(second, dtype=float)
    a, b = a[np.argsort(a[:, 1])], b[np.argsort(b[:, 1])]
    low, high = max(a[0, 1], b[0, 1]), min(a[-1, 1], b[-1, 1])
    if high - low < max(4, .5 * min(np.ptp(a[:, 1]), np.ptp(b[:, 1]))):
        return float("inf")
    ys = np.linspace(low, high, 12)
    return float(np.max(np.abs(np.interp(ys, a[:, 1], a[:, 0]) -
                               np.interp(ys, b[:, 1], b[:, 0]))))


def extract_double_lines(mask, markings, min_area=40):
    """Extract row envelopes from the current segmentation, never from fitLine.

    Nearby, mutually unique components of the same semantic double-line class
    may form a pair. Unpaired components stay provisional; this is not proof
    that two individual painted stripes were visible.
    """
    if mask is None:
        return []
    mask = np.asarray(mask)
    if mask.ndim != 2:
        raise ValueError("Double-line mask must be a 2D class-id array")
    output = []
    for marking in markings:
        if marking.get("class_name") not in DOUBLE_LINE_CLASSES:
            continue
        count, labels, stats, _ = cv2.connectedComponentsWithStats(
            (mask == marking["class_id"]).astype(np.uint8), 8)
        strips = []
        for label in range(1, count):
            x, y, width, height, area = map(int, stats[label])
            if area < min_area or height < 8:
                continue
            rows = []
            for row in range(y, y + height):
                xs = np.flatnonzero(labels[row, x:x + width] == label) + x
                if len(xs):
                    rows.append([float(xs[0]), float(xs[-1]), row])
            # Very broad/branching components are not usable line geometry.
            if max(right - left for left, right, _ in rows) > mask.shape[1] * .08:
                continue
            strips.append(rows)
        curves = [[[.5 * (left + right), y] for left, right, y in rows]
                  for rows in strips]
        neighbors = {i: [j for j in range(len(strips)) if j != i and
                        _distance(curves[i], curves[j]) <= mask.shape[1] * .035]
                     for i in range(len(strips))}
        used = set()
        for i, rows in enumerate(strips):
            if i in used:
                continue
            members = [i]
            if len(neighbors[i]) == 1:
                j = neighbors[i][0]
                if neighbors[j] == [i] and j not in used:
                    members.append(j)
            used.update(members)
            # Paired geometry is limited to the common visible interval.
            start = max(strips[j][0][2] for j in members)
            end = min(strips[j][-1][2] for j in members)
            envelopes = []
            for row in range(start, end + 1):
                values = [strips[j][row - strips[j][0][2]] for j in members]
                envelopes.append([min(v[0] for v in values), max(v[1] for v in values), row])
            step = max(1, mask.shape[0] // 180)
            sampled = envelopes[::step]
            if sampled[-1] != envelopes[-1]:
                sampled.append(envelopes[-1])
            output.append({"class_id": int(marking["class_id"]),
                           "class_name": marking["class_name"],
                           "centerline": [[(l + r) / 2, y] for l, r, y in sampled],
                           "left_boundary": [[l, y] for l, _, y in sampled],
                           "right_boundary": [[r, y] for _, r, y in sampled],
                           "bbox_xyxy": [min(v[0] for v in envelopes), start,
                                         max(v[1] for v in envelopes), end],
                           "component_count": len(members),
                           "members": [
                               {"centerline": [[(l + r) / 2, y] for l, r, y in strips[j]],
                                "left_boundary": [[l, y] for l, _, y in strips[j]],
                                "right_boundary": [[r, y] for _, r, y in strips[j]]}
                               for j in members],
                           "geometry_status": "paired" if len(members) == 2 else "provisional",
                           "source": "segmentation_mask", "rule_eligible": False})
    return output


def finite_line_position(point, centerline):
    """Return signed pixel distance to a visible segment, or None past its ends.

    Sign follows stored point order (top to bottom). This geometric measurement
    alone does not establish vehicle side, tire contact, or a crossing event.
    """
    points = np.asarray(centerline, dtype=float)
    point = np.asarray(point, dtype=float)
    if len(points) < 2:
        return None
    starts, vectors = points[:-1], np.diff(points, axis=0)
    lengths2 = np.sum(vectors * vectors, axis=1)
    fractions = np.sum((point - starts) * vectors, axis=1) / np.maximum(lengths2, 1e-9)
    candidates = np.flatnonzero(lengths2 > 0)
    if not len(candidates):
        return None
    clamped = np.clip(fractions, 0, 1)
    offsets = point - (starts + clamped[:, None] * vectors)
    index = min(candidates, key=lambda i: np.linalg.norm(offsets[i]))
    if (index == 0 and fractions[index] < 0) or (index == len(vectors) - 1 and fractions[index] > 1):
        return None
    signed = (vectors[index, 0] * offsets[index, 1] -
              vectors[index, 1] * offsets[index, 0]) / np.sqrt(lengths2[index])
    lengths = np.sqrt(lengths2)
    progress = float(lengths[:index].sum() + clamped[index] * lengths[index])
    return {"signed_distance_px": float(signed), "segment_index": int(index),
            "projection": (point - offsets[index]).tolist(),
            "tangent": (vectors[index] / lengths[index]).tolist(),
            "endpoint_distance_px": min(progress, float(lengths.sum()) - progress)}


def line_quality(line, settings):
    """Recheck persisted geometry so replay thresholds apply to old observations."""
    semantic = line.get("component_count") == 1 and settings["allow_semantic_band"]
    if line.get("source") != "segmentation_mask" or (line.get("component_count") != 2 and not semantic):
        return "unverified_pair"
    if line.get("observed_frames", 0) < settings["min_line_observations"]:
        return "unstable_line"
    try:
        center = np.asarray(line["centerline"], dtype=float)
        members = line["members"]
        if len(members) != line["component_count"]:
            return "unverified_pair"
        paths = [center] + [np.asarray(line[key], dtype=float) for key in ("left_boundary", "right_boundary")]
        paths += [np.asarray(member[key], dtype=float) for member in members
                  for key in ("centerline", "left_boundary", "right_boundary")]
        if any(p.ndim != 2 or p.shape[1] != 2 or len(p) < 2 or not np.isfinite(p).all()
               or not np.all(np.diff(p[:, 1]) > 0) for p in paths):
            return "invalid_geometry"
        if np.linalg.norm(np.diff(center, axis=0), axis=1).sum() < settings["min_line_length_px"]:
            return "short_line"
        if any(p[0, 1] > center[0, 1] or p[-1, 1] < center[-1, 1] for p in paths):
            return "unsupported_geometry"
        if semantic:
            return None  # Semantic double-line band; cannot confirm two physical stripes.
        # Include all stored vertices: a sparse fixed grid can miss a local kink.
        ys = np.unique(np.concatenate([p[:, 1] for p in paths]))
        ys = ys[(ys >= center[0, 1]) & (ys <= center[-1, 1])]
        a, b = (np.asarray(m["centerline"], dtype=float) for m in members)
        ax, bx = np.interp(ys, a[:, 1], a[:, 0]), np.interp(ys, b[:, 1], b[:, 0])
        if ax.mean() > bx.mean():
            ax, bx, members = bx, ax, members[::-1]
        right = np.asarray(members[0]["right_boundary"], dtype=float)
        left = np.asarray(members[1]["left_boundary"], dtype=float)
        gap = np.interp(ys, left[:, 1], left[:, 0]) - np.interp(ys, right[:, 1], right[:, 0])
        if gap.min() < settings["min_pair_gap_px"] or gap.max() / gap.min() > settings["max_pair_gap_ratio"]:
            return "invalid_pair_gap"
        angles = np.abs(np.arctan(np.diff(ax) / np.diff(ys)) - np.arctan(np.diff(bx) / np.diff(ys)))
        if np.degrees(angles).max() > settings["max_pair_angle_deg"]:
            return "nonparallel_pair"
    except (KeyError, ValueError, TypeError, IndexError):
        return "invalid_geometry"
    return None


def resolve_rule_lines(lines, memory, matrix, frame, settings, vehicles=()):
    """Maintain finite observed support through matching collinear fragments.

    Never extend an anchor beyond its previously observed ends. Partial support
    expires even when fragments remain visible; events using it are suspected.
    Complete occlusion is bounded by a separate short timeout and requires
    overlap with a detected vehicle. Motion failure clears memory in the rule.
    """
    now = frame["timestamp_sec"]
    current = [copy.deepcopy(line) for line in lines if line.get("source_frame") == frame["index"]]
    anchors = []
    for old in memory.values():
        anchor = copy.deepcopy(old)
        try:
            for part in [anchor] + anchor.get("members", []):
                for key in ("centerline", "left_boundary", "right_boundary"):
                    part[key] = project_points(part[key], matrix)
                    path = np.asarray(part[key])
                    if np.any(np.diff(path[:, 1]) < -.5):
                        raise ValueError("Non-monotone projection")
                    # Pixel-quantized edges can swap adjacent rows by a fraction
                    # of a pixel after homography; canonicalize that ordering.
                    _, unique = np.unique(path[:, 1], return_index=True)
                    part[key] = path[unique].tolist()
            # Perspective rotation moves boundary endpoints to different rows.
            # Trim all paths to shared support instead of extending endpoints.
            parts = [anchor] + anchor.get("members", [])
            paths = [np.asarray(part[key]) for part in parts
                     for key in ("centerline", "left_boundary", "right_boundary")]
            low, high = max(p[0, 1] for p in paths), min(p[-1, 1] for p in paths)
            if high <= low:
                raise ValueError("No common projected support")
            for part in parts:
                for key in ("centerline", "left_boundary", "right_boundary"):
                    p = np.asarray(part[key])
                    ys = np.unique(np.r_[low, p[(p[:, 1] > low) & (p[:, 1] < high), 1], high])
                    part[key] = np.column_stack((np.interp(ys, p[:, 1], p[:, 0]), ys)).tolist()
            anchors.append(anchor)
        except (ValueError, KeyError):
            continue
    candidates = []
    for line in current:
        points = np.asarray(line["centerline"])
        length = float(np.linalg.norm(np.diff(points, axis=0), axis=1).sum())
        matches = []
        if length >= settings["min_fragment_length_px"]:
            for index, anchor in enumerate(anchors):
                if anchor["class_name"] != line["class_name"]:
                    continue
                samples = points[np.linspace(0, len(points) - 1, min(len(points), 20)).astype(int)]
                distances = [finite_line_position(p, anchor["centerline"]) for p in samples]
                valid = [d for d in distances if d is not None]
                if (len(valid) >= .8 * len(samples) and
                        np.quantile([abs(d["signed_distance_px"]) for d in valid], .8) <= settings["fragment_match_distance_px"]):
                    matches.append(index)
        if len(matches) > 1:
            ranked = sorted(matches, key=lambda j: np.linalg.norm(
                np.diff(np.asarray(anchors[j]["centerline"]), axis=0), axis=1).sum(), reverse=True)
            lengths = [np.linalg.norm(np.diff(np.asarray(anchors[j]["centerline"]), axis=0), axis=1).sum()
                       for j in ranked]
            if lengths[0] > 1.5 * lengths[1]:
                matches = ranked[:1]  # Prefer the full finite anchor over its own short fragments.
        candidates.append((line, length, matches))
    output, consumed = [], set()
    for index, anchor in enumerate(anchors):
        matches = [(i, line, length) for i, (line, length, ids) in enumerate(candidates) if ids == [index]]
        if not matches:
            points = np.asarray(anchor["centerline"])
            covered = np.zeros(len(points), dtype=bool)
            for vehicle in vehicles:
                box = vehicle.get("bbox_xyxy", [])
                if len(box) == 4:
                    x1, y1, x2, y2 = box
                    covered |= ((points[:, 0] >= x1) & (points[:, 0] <= x2) &
                                (points[:, 1] >= y1) & (points[:, 1] <= y2))
            if (np.linalg.norm(np.diff(points, axis=0), axis=1).sum() >= settings["min_fragment_length_px"]
                    and covered.mean() >= .2 and now - anchor.get("last_visible_time", -1e9) <= settings["max_occluded_gap_sec"]
                    and now - anchor["support_since"] <= settings["max_partial_support_seconds"]):
                anchor.update(partial_support=True, source_frame=frame["index"], matched_fragment_ids=[])
                output.append(anchor)
            continue
        i, best, length = max(matches, key=lambda item: item[2])
        old_length = np.linalg.norm(np.diff(np.asarray(anchor["centerline"]), axis=0), axis=1).sum()
        if length >= .75 * old_length:
            result = copy.deepcopy(best)
            result["support_since"] = now
        elif now - anchor["support_since"] <= settings["max_partial_support_seconds"]:
            result = anchor
            result["partial_support"] = True
            # Correct accumulated road-registration drift using visible fragments.
            # Only normal displacement is corrected; observed finite ends are not extended.
            offsets = []
            for _, fragment, _ in matches:
                for point in fragment["centerline"][::max(1, len(fragment["centerline"]) // 20)]:
                    position = finite_line_position(point, anchor["centerline"])
                    if position and abs(position["signed_distance_px"]) <= settings["fragment_match_distance_px"]:
                        offsets.append(np.asarray(point) - position["projection"])
            if offsets:
                correction = np.median(offsets, axis=0)
                for part in [result] + result.get("members", []):
                    for key in ("centerline", "left_boundary", "right_boundary"):
                        part[key] = (np.asarray(part[key]) + correction).tolist()
        else:
            continue
        result.update(track_id=anchor["track_id"], observed_frames=anchor["observed_frames"] + 1,
                      source_frame=frame["index"], last_visible_time=now,
                      matched_fragment_ids=[m[1]["track_id"] for m in matches])
        # Record the last fully observed geometry separately from projection time.
        result.setdefault("geometry_source_frame", best["source_frame"])
        if not result.get("partial_support"):
            result["geometry_source_frame"] = frame["index"]
        output.append(result)
        consumed.update(m[0] for m in matches)
    for i, (line, _, _) in enumerate(candidates):
        if i not in consumed:
            # Use an independent identity after an expired partial anchor.
            line.update(track_id=f"{line['track_id']}@{frame['index']}", support_since=now,
                        geometry_source_frame=frame["index"], last_visible_time=now)
            output.append(line)
    memory.clear()
    for line in output:
        points = np.asarray(line["left_boundary"] + line["right_boundary"])
        line["bbox_xyxy"] = [*points.min(axis=0).tolist(), *points.max(axis=0).tolist()]
    memory.update({line["track_id"]: copy.deepcopy(line) for line in output})
    return output


def vehicle_line_position(vehicle, line, settings, motion_error=0):
    """Inset bottom-edge proxy must clear the entire band; not tire localization."""
    try:
        box = np.asarray(vehicle.get("bbox_xyxy", []), dtype=float)
    except (TypeError, ValueError):
        return None
    if box.shape != (4,) or not np.isfinite(box).all() or box[2] <= box[0] or box[3] <= box[1]:
        return None
    x1, y1, x2, y2 = box
    inset = (x2 - x1) * settings["footprint_inset_ratio"]
    points = [[x1 + inset, y2], [(x1 + x2) / 2, y2], [x2 - inset, y2]]
    sides, measurements = [], []
    for point in points:
        position = finite_line_position(point, line["centerline"])
        if position is None or position["endpoint_distance_px"] < settings["endpoint_margin_px"]:
            return None
        foot = np.asarray(position["projection"])
        tangent = np.asarray(position["tangent"])
        normal = np.array([-tangent[1], tangent[0]])
        limits = []
        for key in ("left_boundary", "right_boundary"):
            path = np.asarray(line[key], dtype=float)
            boundary = [np.interp(foot[1], path[:, 1], path[:, 0]), foot[1]]
            limits.append(float(np.dot(np.asarray(boundary) - foot, normal)))
        low, high = sorted(limits)
        margin = settings["side_margin_px"] + (high - low) * settings["margin_width_ratio"] + motion_error
        distance = position["signed_distance_px"]
        sides.append(-1 if distance < low - margin else 1 if distance > high + margin else 0)
        measurements.append({**position, "in_band": low <= distance <= high})
    return {"side": sides[0] if sides[0] == sides[1] == sides[2] else 0,
            "point": points[1], "center_in_band": measurements[1]["in_band"],
            "tangent": measurements[1]["tangent"], "box": box.tolist()}


def project_points(points, matrix):
    homogeneous = np.column_stack((points, np.ones(len(points)))) @ matrix.T
    if not np.isfinite(homogeneous).all() or np.any(homogeneous[:, 2] <= 1e-6):
        raise ValueError("Unstable ground projection")
    return (homogeneous[:, :2] / homogeneous[:, 2:]).tolist()


def classify_maneuver(before, after, tangent, settings):
    """Infer maneuver from camera-compensated ground trajectories, not box heading."""
    vectors = []
    for samples in (before, after):
        if len(samples) < settings["stable_observations"]:
            return "complete_crossing", {"reason": "insufficient_direction_samples"}
        points = np.asarray([s["point"] for s in samples], dtype=float)
        vector = points[-1] - points[0]
        distance = np.linalg.norm(vector)
        travel = np.linalg.norm(np.diff(points, axis=0), axis=1).sum()
        if distance < settings["min_heading_displacement_px"] or distance / max(travel, 1e-6) < settings["heading_consistency"]:
            return "complete_crossing", {"reason": "unstable_or_small_displacement"}
        vectors.append(vector / distance)
    angle = float(np.degrees(np.arccos(np.clip(np.dot(*vectors), -1, 1))))
    longitudinal = [float(np.dot(vector, tangent)) for vector in vectors]
    aligned = min(map(abs, longitudinal)) >= settings["min_longitudinal_alignment"]
    maneuver = "complete_crossing"
    if aligned and longitudinal[0] * longitudinal[1] < 0 and angle >= settings["min_uturn_angle_deg"]:
        maneuver = "u_turn"
    elif aligned and longitudinal[0] * longitudinal[1] > 0 and angle <= settings["max_lane_change_angle_deg"]:
        maneuver = "lane_change"
    return maneuver, {"method": "camera_compensated_ground_trajectory", "angle_degrees": angle,
                      "longitudinal_directions": longitudinal, "inferred": True}


def attach_double_lines(record, mask, state):
    """Mutate the frame record and a caller-owned dict; retain observed tracks only.

    Motion maps previous-frame coordinates into the current frame. Missing or
    ambiguous matches get new IDs; IDs are never reused within a video.
    """
    frame = record["frame"]
    observations = record["observations"]
    diagnostics = record.setdefault("postprocessing", {})
    motion = diagnostics.get("road_marking_compensation", {}).get("motion", {})
    reset_reason = None
    matrix = None
    if state.get("frame") is None:
        reset_reason = "first_frame"
    elif (frame["index"] != state["frame"] + 1 or
          not 0 < frame["timestamp_sec"] - state["time"] <= .2 or
          state["size"] != [frame["width"], frame["height"]]):
        reset_reason = "discontinuous_input"
    elif not motion.get("valid"):
        reset_reason = "motion_unavailable"
    else:
        try:
            matrix = np.asarray(motion.get("matrix"), dtype=float)
            if (matrix.shape != (3, 3) or not np.isfinite(matrix).all() or
                    abs(np.linalg.det(matrix)) < 1e-9):
                raise ValueError("Invalid homography")
        except (ValueError, TypeError):
            matrix, reset_reason = None, "invalid_motion"
    if mask is not None and np.asarray(mask).shape != (frame["height"], frame["width"]):
        mask, reset_reason = None, "mask_shape_mismatch"
    lines = extract_double_lines(mask, observations.get("road_markings_raw",
                                                          observations.get("road_markings", [])))
    previous = [] if reset_reason else state.get("tracks", [])
    projected = []
    for track in previous:
        points = np.asarray(track["centerline"], dtype=float)
        homogeneous = np.column_stack((points, np.ones(len(points)))) @ matrix.T
        if np.any(np.abs(homogeneous[:, 2]) < 1e-6):
            continue
        points = homogeneous[:, :2] / homogeneous[:, 2:]
        if np.isfinite(points).all() and np.all(np.diff(points[:, 1]) > 0):
            projected.append((track, points))
    matches = [[j for j, (track, points) in enumerate(projected)
                if track["class_name"] == line["class_name"] and
                track["component_count"] == line["component_count"] and
                _distance(points, line["centerline"]) <= max(3, frame["width"] * .01)]
               for line in lines]
    for line, choices in zip(lines, matches):
        unique = len(choices) == 1 and sum(choices[0] in others for others in matches) == 1
        if unique:
            old = projected[choices[0]][0]
            line.update(track_id=old["track_id"], observed_frames=old["observed_frames"] + 1)
        else:
            state["next_id"] = state.get("next_id", 0) + 1
            line.update(track_id=f"double-line-{state['next_id']}", observed_frames=1)
        line.update(source_frame=frame["index"], tracking_status="matched" if unique else "new")
        line["rule_eligible"] = line_quality(line, RULE_DEFAULTS) is None
    observations["double_lines"] = lines
    diagnostics["double_lines"] = {
        "version": 1, "status": "observed" if lines else
        ("geometry_unavailable" if mask is None else "no_double_lines"),
        "reset_reason": reset_reason, "observed_count": len(lines),
        "eligibility": "evaluated_by_rule", "motion_source": "road_marking_compensation"}
    state.update(frame=frame["index"], time=frame["timestamp_sec"],
                 size=[frame["width"], frame["height"]], tracks=copy.deepcopy(lines))


def draw_double_lines(frame, lines):
    for line in lines:
        color = (0, 220, 255) if line["class_name"] == "solid double yellow" else (255, 255, 255)
        points = np.rint(line["centerline"]).astype(np.int32)
        cv2.polylines(frame, [points], False, color, 2)
        cv2.putText(frame, line["track_id"], tuple(points[-1]),
                    cv2.FONT_HERSHEY_SIMPLEX, .45, color, 1, cv2.LINE_AA)
    return frame
