"""Model-independent, pluggable traffic violation rule engine."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
import math
import copy
from signal_state import TemporalSignalState
from typing import Iterable


@dataclass
class RuleContext:
    """Configuration shared by rules; contains no model instances."""
    source_id: str
    fps: float
    frame_width: int
    frame_height: int


class ViolationRule(ABC):
    """Implement this interface to add a rule without changing model code."""

    rule_id = "base"

    @abstractmethod
    def evaluate(self, observation: dict) -> list[dict]:
        """Consume one frame observation and return zero or more events."""

    def finalize(self) -> list[dict]:
        """Optionally flush pending events after the last frame."""
        return []


class ViolationEngine:
    def __init__(self, rules: Iterable[ViolationRule]):
        self.rules = list(rules)

    def evaluate(self, observation: dict) -> list[dict]:
        events = []
        for rule in self.rules:
            events.extend(rule.evaluate(observation))
        return events

    def finalize(self) -> list[dict]:
        events = []
        for rule in self.rules:
            events.extend(rule.finalize())
        return events


class RedLightStopLineCrossingRule(ViolationRule):
    """Report suspected and rule-confirmed red-light crossing events.

    Both statuses require review; confirmation refers to configured conditions.
    Signal-to-lane association belongs in later rules/calibration data.
    """

    rule_id = "red_light_stop_line_crossing"

    ALLOWED_VEHICLE_CLASSES = {"car", "motorcycle", "bus", "truck"}

    def __init__(self, context: RuleContext, stable_signal_seconds=0.5,
                 signal_confidence=0.30, line_margin_pixels=8,
                 min_track_frames=10, min_line_area=300,
                 min_line_aspect=2.0, max_line_angle=35.0,
                 min_line_stable_frames=5, crossing_direction="away",
                 max_crossing_gap_seconds=0.32):
        self.context = context
        self.signal_confidence = signal_confidence
        self.line_margin = max(line_margin_pixels,
                               round(context.frame_height * 0.008))
        self.min_track_frames = min_track_frames
        self.min_line_area = min_line_area
        self.min_line_aspect = min_line_aspect
        self.max_line_angle = max_line_angle
        self.min_line_stable_frames = min_line_stable_frames
        self.line_tracks = {}
        self.next_line_id = 1
        self.crossing_states = {}
        self.reported = set()
        self.event_sequence = 0
        self.pending_events = {}
        if crossing_direction not in {"away", "toward"}:
            raise ValueError("crossing_direction must be away or toward")
        self.crossing_direction = crossing_direction
        self.max_crossing_gap = max(1, round(context.fps * max_crossing_gap_seconds))
        self.signal_tracker = TemporalSignalState(context.fps, anchor_confidence=signal_confidence,
                                                  window_seconds=stable_signal_seconds)
        self.diagnostics = {}

    @staticmethod
    def _line_angle(line):
        x1, y1, x2, y2 = line
        angle = abs(math.degrees(math.atan2(y2 - y1, x2 - x1)))
        return min(angle, 180.0 - angle)

    def _project_line_tracks(self, observation):
        """重用補償層提供的鏡頭變換，讓規則內的舊線也位於當幀座標。"""
        meta=observation.get("postprocessing", {}).get("road_marking_compensation", {})
        motion=meta.get("motion", {})
        if meta.get("reset_reason"):
            self.line_tracks.clear()
            self.crossing_states.clear()
        if not motion.get("valid") or motion.get("matrix") is None:
            return
        matrix=motion["matrix"]
        def transform(x,y):
            scale=matrix[2][0]*x+matrix[2][1]*y+matrix[2][2]
            if abs(scale)<1e-8:
                raise ValueError("Invalid homography")
            return [(matrix[0][0]*x+matrix[0][1]*y+matrix[0][2])/scale,
                    (matrix[1][0]*x+matrix[1][1]*y+matrix[1][2])/scale]
        for key,track in list(self.line_tracks.items()):
            try:
                x,y,w,h=track["bbox_xywh"]
                corners=[transform(x,y),transform(x+w,y),transform(x+w,y+h),transform(x,y+h)]
                left=min(p[0] for p in corners); top=min(p[1] for p in corners)
                right=max(p[0] for p in corners); bottom=max(p[1] for p in corners)
                line=track["line_xyxy"]
                track["line_xyxy"]=transform(*line[:2])+transform(*line[2:])
                track["bbox_xywh"]=[left,top,right-left,bottom-top]
                track["centroid"]=transform(*track["centroid"])
                track["angle_degrees"]=self._line_angle(track["line_xyxy"])
            except (ValueError,OverflowError):
                del self.line_tracks[key]

    def _valid_stop_components(self, observations):
        candidates = []
        for marking in observations["road_markings"]:
            if marking["class_name"] != "stop line":
                continue
            for component in marking["components"]:
                line = component.get("line_xyxy")
                compensation = component.get("compensation", {})
                trusted = compensation.get("rule_eligible", False)
                if compensation and not trusted:
                    continue
                minimum_area = min(self.min_line_area, 100) if trusted else self.min_line_area
                if not line or component["area"] < minimum_area:
                    continue
                _, _, width, height = component["bbox_xywh"]
                if width / max(height, 1) < self.min_line_aspect:
                    continue
                angle = self._line_angle(line)
                if angle > self.max_line_angle:
                    continue
                item = dict(component)
                item["angle_degrees"] = round(angle, 2)
                candidates.append(item)
        return candidates

    def _update_stop_lines(self, candidates, frame_index):
        unused = set(self.line_tracks)
        updated = []
        for component in sorted(candidates, key=lambda item: item["area"],
                                reverse=True):
            cx, cy = component["centroid"]
            best_id, best_distance = None, float("inf")
            for line_id in unused:
                previous = self.line_tracks[line_id]
                if frame_index - previous["last_frame"] > self.max_crossing_gap:
                    continue
                distance = math.hypot(cx - previous["centroid"][0],
                                      cy - previous["centroid"][1])
                angle_delta = abs(component["angle_degrees"]
                                  - previous["angle_degrees"])
                old_id = previous.get("compensation", {}).get("id")
                new_id = component.get("compensation", {}).get("id")
                if old_id and old_id == new_id:
                    best_id, best_distance = line_id, -1.0
                    break
                # 片段可沿同一條停止線左右改變；配對以法向位移與區間重疊為依據。
                px, _, pw, _ = previous["bbox_xywh"]
                x, _, width, _ = component["bbox_xywh"]
                overlap = min(px+pw, x+width) - max(px, x)
                normal_distance = abs(cy - self._line_y(previous["line_xyxy"], cx))
                if (normal_distance < best_distance
                        and normal_distance <= self.context.frame_height * 0.025
                        and overlap > 0
                        and angle_delta <= 15.0):
                    best_id, best_distance = line_id, normal_distance
            if best_id is None:
                best_id = self.next_line_id
                self.next_line_id += 1
                stable_frames = 1
            else:
                unused.remove(best_id)
                stable_frames = self.line_tracks[best_id]["stable_frames"] + 1
            tracked = dict(component, marking_id=f"stopline-{best_id:04d}",
                           stable_frames=stable_frames,
                           last_frame=frame_index)
            self.line_tracks[best_id] = tracked
            # 補償層已驗證多幀，不再重複等待 5 幀而錯過短暫可見的停止線。
            if stable_frames >= self.min_line_stable_frames or component.get("compensation", {}).get("rule_eligible", False):
                updated.append(tracked)
        self.line_tracks = {
            line_id: item for line_id, item in self.line_tracks.items()
            if frame_index - item["last_frame"] <= self.max_crossing_gap
        }
        # 同一條線的可見片段可能改變補償 ID。只在舊線暫時消失、區間重疊且近共線時，
        # 將尚未完成的越線狀態交接給新片段；保留原時間，不能延長過期狀態。
        active_ids={item["marking_id"] for item in updated}
        for current in updated:
            candidates=[]
            x,_,width,_=current["bbox_xywh"]
            for old in self.line_tracks.values():
                if old["marking_id"] in active_ids or old["last_frame"]==frame_index:
                    continue
                ox,_,ow,_=old["bbox_xywh"]
                if min(x+width,ox+ow)<=max(x,ox) or abs(current["angle_degrees"]-old["angle_degrees"])>10:
                    continue
                cx=(max(x,ox)+min(x+width,ox+ow))/2
                delta=abs(self._line_y(current["line_xyxy"],cx)-self._line_y(old["line_xyxy"],cx))
                if delta<=self.context.frame_height*.015:
                    candidates.append((delta,old))
            if candidates:
                old=min(candidates,key=lambda pair:pair[0])[1]
                for (vehicle_id,line_id),state in list(self.crossing_states.items()):
                    if line_id!=old["marking_id"] or state["phase"]!="before_line":
                        continue
                    new_key=(vehicle_id,current["marking_id"])
                    if self.crossing_states.get(new_key,{}).get("before_count",0)<state["before_count"]:
                        self.crossing_states[new_key]=dict(state,line_reassociated_from=line_id)
                        del self.crossing_states[(vehicle_id,line_id)]
        return updated

    @staticmethod
    def _covers_x(component, x):
        left, _, width, _ = component["bbox_xywh"]
        padding = max(30.0, width * 0.20)
        return left - padding <= x <= left + width + padding

    @staticmethod
    def _line_y(line, x):
        x1, y1, x2, y2 = line
        if abs(x2 - x1) < 1e-6:
            return (y1 + y2) / 2.0
        ratio = (x - x1) / (x2 - x1)
        return y1 + ratio * (y2 - y1)

    def evaluate(self, observation):
        obs = observation["observations"]
        frame = observation["frame"]
        index = frame["index"]
        signal_state = self.signal_tracker.update(obs["traffic_lights"], index)
        self._project_line_tracks(observation)
        stop_components = self._update_stop_lines(
            self._valid_stop_components(obs), index)
        # 只保留短時間連續軌跡；不能把數秒前的線前位置接成現在的越線。
        self.crossing_states = {key:state for key,state in self.crossing_states.items()
                                if index-state["last_frame"] <= self.max_crossing_gap}
        self.diagnostics = {"frame":index,"signal":dict(self.signal_tracker.evidence),
                            "usable_stop_lines":len(stop_components),"vehicles":[],
                            "skipped_vehicles":[]}
        if signal_state in {"green", "yellow"}:
            self.crossing_states.clear()
            return []
        events = []
        for vehicle in obs.get("rule_vehicles", obs["vehicles"]):
            object_id = vehicle.get("object_id")
            skipped_by = []
            if not object_id:
                skipped_by.append("missing_object_id")
            if vehicle["class_name"] not in self.ALLOWED_VEHICLE_CLASSES:
                skipped_by.append("unsupported_vehicle_class")
            if vehicle.get("track_age_frames", 0) < self.min_track_frames:
                skipped_by.append("insufficient_track_age")
            if skipped_by:
                self.diagnostics["skipped_vehicles"].append({
                    "object_id":object_id, "class_name":vehicle["class_name"],
                    "blocked_by":skipped_by})
                continue
            # Group/person display boxes must never define the vehicle contact proxy.
            group_box = vehicle.get("rider_group", {}).get("vehicle_bbox_xyxy")
            vehicle_box = group_box or vehicle.get("bbox_xyxy")
            if vehicle_box is not None:
                x = (vehicle_box[0] + vehicle_box[2]) / 2
                y = vehicle_box[3]
            else:
                x, y = vehicle["bottom_center"]
                vehicle_box = [x, y, x, y]
            overlapping_lines = 0
            for stop_line in stop_components:
                left, _, width, _ = stop_line["bbox_xywh"]
                overlap_left, overlap_right = max(left, vehicle_box[0]), min(left+width, vehicle_box[2])
                if overlap_right < overlap_left:
                    continue
                overlapping_lines += 1
                # 遮擋時線段可能只露在車身一側：使用車框底邊與有限線段重疊位置。
                # 不再要求中心 x 恰好落在線段內，也不把線無限延伸到其他車道。
                reference_x = min(max(x, overlap_left), overlap_right)
                signed_distance = y - self._line_y(stop_line["line_xyxy"], reference_x)
                oriented = -signed_distance if self.crossing_direction == "away" else signed_distance
                side = -1 if oriented < -self.line_margin else (1 if oriented > self.line_margin else 0)
                state_key = (object_id, stop_line["marking_id"])
                state = self.crossing_states.setdefault(state_key, {
                    "phase":"unrelated", "before_count":0, "after_count":0,
                    "last_frame":index, "red_before":False, "before_frame":None,
                    "red_before_frame":None})
                state["last_frame"] = index
                if side == -1:
                    state["before_count"] += 1
                    state["after_count"] = 0
                    state["before_frame"] = index
                    if signal_state == "red":
                        state["red_before_frame"] = index
                        state["red_before_evidence"] = dict(self.signal_tracker.evidence)
                    state["red_before"] = (state["red_before_frame"] is not None and
                                           index-state["red_before_frame"] <= self.max_crossing_gap)
                    if state["before_count"] >= 2:
                        state["phase"] = "before_line"
                elif side == 0:
                    state["after_count"] = 0
                elif state["phase"] == "before_line":
                    # 可直接線前→線後；不要求攝影機剛好拍到狹窄的跨線區間。
                    state["after_count"] += 1
                else:
                    state["after_count"] = 0
                blocked_by = []
                if state["before_count"] < 2:
                    blocked_by.append("insufficient_before_line_observations")
                if state["after_count"] < 2:
                    blocked_by.append("insufficient_after_line_observations")
                if not state["red_before"]:
                    blocked_by.append("no_recent_red_before_crossing")
                if signal_state != "red":
                    blocked_by.append("signal_not_confirmed_red")
                if (state.get("red_before_evidence", {}).get("signal_id")
                        != self.signal_tracker.evidence.get("signal_id")):
                    blocked_by.append("signal_identity_not_confirmed")
                if object_id in self.reported:
                    blocked_by.append("already_reported")
                self.diagnostics["vehicles"].append({"object_id":object_id,
                    "marking_id":stop_line["marking_id"],"signed_distance_pixels":round(signed_distance,2),
                    "phase":state["phase"],"before_count":state["before_count"],
                    "after_count":state["after_count"],"red_before":state["red_before"],
                    "blocked_by":blocked_by})
                confirmed = (state["after_count"] >= 2 and state["red_before"] and signal_state == "red"
                        and state.get("red_before_evidence", {}).get("signal_id") == self.signal_tracker.evidence.get("signal_id")
                        and object_id not in self.reported)
                suspected = (state["after_count"] >= 2 or
                             (state["after_count"] >= 1 and signal_state == "red"))
                if object_id in self.reported or not (confirmed or suspected):
                    continue
                conditions = {
                    "tracked_vehicle": True,
                    "usable_overlapping_stop_line": True,
                    "before_line_observed": state["before_count"] >= 2,
                    "crossing_observed": state["after_count"] >= 2,
                    "red_before_crossing": state["red_before"],
                    "red_at_crossing": signal_state == "red",
                    "same_signal": bool(state.get("red_before_evidence", {}).get("signal_id")) and
                        state.get("red_before_evidence", {}).get("signal_id") == self.signal_tracker.evidence.get("signal_id"),
                }
                if confirmed:
                    behavior = "red_light_stop_line_crossing"
                    description = "車輛在紅燈期間由停止線前移至線後，符合目前紅燈越線規則。"
                else:
                    behavior = "possible_red_light_stop_line_crossing"
                    description = "車輛已由停止線前移至線後，疑似紅燈越線；尚缺部分越線確認或紅燈時序證據。"
                previous = self.pending_events.get(object_id)
                if previous is None:
                    self.event_sequence += 1
                event = {
                    "event_id": previous["event_id"] if previous else f"rlsl-{self.event_sequence:06d}",
                    "rule_id": self.rule_id,
                    "status": "confirmed" if confirmed else "suspected",
                    "status_label": "確認違規" if confirmed else "疑似違規",
                    "confirmation_scope": "configured_rule_conditions",
                    "behavior": behavior,
                    "description": description,
                    "conditions": conditions,
                    "met_conditions": [key for key,value in conditions.items() if value],
                    "missing_conditions": [key for key,value in conditions.items() if not value],
                    "review_required": True,
                    "source_id": self.context.source_id,
                    "frame": frame["index"],
                    "timestamp_sec": frame["timestamp_sec"],
                    "object_id": object_id,
                    "vehicle_class": vehicle["class_name"],
                    "evidence": {
                        "vehicle": vehicle,
                        "stop_line": stop_line,
                        "signal_state": signal_state or "unknown",
                        "signal_evidence": dict(self.signal_tracker.evidence),
                        "crossing_direction": self.crossing_direction,
                        "before_frame": state["before_frame"],
                        "red_before_frame": state["red_before_frame"],
                        "red_before_evidence": state.get("red_before_evidence"),
                        "line_reassociated_from": state.get("line_reassociated_from"),
                        "reference_point": [reference_x, y],
                        "reference_point_method": "bbox_bottom_overlap",
                        "signed_distance_pixels": round(signed_distance, 2),
                    },
                }
                if confirmed:
                    state["phase"] = "crossed_line"
                    self.reported.add(object_id)
                    self.pending_events.pop(object_id, None)
                    events.append(copy.deepcopy(event))
                elif previous is None or (conditions["crossing_observed"], sum(conditions.values())) > (
                        previous["conditions"]["crossing_observed"], len(previous["met_conditions"])):
                    # Keep the strongest snapshot, without combining unrelated lines/frames.
                    self.pending_events[object_id] = copy.deepcopy(event)
            if not overlapping_lines:
                self.diagnostics["skipped_vehicles"].append({
                    "object_id":object_id, "class_name":vehicle["class_name"],
                    "blocked_by":["no_overlapping_stop_line" if stop_components
                                  else "no_usable_stop_line"]})
        return events

    def finalize(self):
        events = sorted(self.pending_events.values(), key=lambda event: event["frame"])
        self.pending_events.clear()
        return events


RULE_FACTORIES = {
    RedLightStopLineCrossingRule.rule_id: RedLightStopLineCrossingRule,
}


def create_rules(names, context, rule_options=None):
    names = list(names)
    unknown = sorted(set(names) - RULE_FACTORIES.keys() - {"all"})
    if unknown:
        raise ValueError("未知違規規則：" + ", ".join(unknown))
    names = list(RULE_FACTORIES) if "all" in names else list(dict.fromkeys(names))
    options = rule_options or {}
    return [RULE_FACTORIES[name](context, **options.get(name, {})) for name in names]
