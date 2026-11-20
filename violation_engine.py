"""Model-independent, pluggable traffic violation rule engine."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
import json
import math
from pathlib import Path
import cv2
from core.fastcopy import clone
from functools import partial
import numpy as np
from double_lines import (validate_rule_settings, line_quality, vehicle_line_position,
                          project_points, classify_maneuver, resolve_rule_lines,
                          project_line_geometry, RULE_DEFAULTS as DOUBLE_LINE_DEFAULTS,
                          OCCLUSION_OPTION_KEYS, ORIGIN_OPTION_KEYS, STRADDLE_OPTION_KEYS, ENDPOINT_OPTION_KEYS,
                          BAND_DWELL_OPTION_KEYS)
from signal_state import CombinedSignalState
from core.trajectory import TrackHistory
from core.continuity import is_continuous
from core.motion import homography, road_motion
from area_rules import AreaOccupancyRule
from observation_schema import marking_evidence, marking_geometry_source
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
    """依序執行規則，並把同一台車的停等區事件併入闖紅燈事件。

    闖紅燈的車必然行經停等區，兩者是同一個違規行為，不分開報告。停等區事件可能早於闖紅燈事件
    （ms01：2.0 秒 vs 3.4 秒；ms05：1.4 秒 vs 6.6 秒），所以先保留：至少 merge_window_seconds，
    且這台車仍在畫面中（最後出現後未超過 merge_track_gap_seconds）或闖紅燈規則仍有它的待定事件時繼續保留。
    期間同一台車出現闖紅燈事件時，整筆停等區事件放進該事件的 merged_events，不再單獨輸出。
    闖紅燈事件在前時，同一支影片中之後的停等區事件直接略過，記錄在停等區規則當幀的 diagnostics["merged_into"]。
    未啟用闖紅燈規則時不保留、不合併。
    """
    MERGE_INTO = {"car_motorcycle_waiting_area": "red_light_stop_line_crossing"}

    def __init__(self, rules: Iterable[ViolationRule], merge_window_seconds=3.0, merge_track_gap_seconds=1.0):
        self.rules = list(rules)
        enabled = {rule.rule_id for rule in self.rules}
        self.merge = {source: target for source, target in self.MERGE_INTO.items()
                      if source in enabled and target in enabled}
        self.merge_window = merge_window_seconds
        self.merge_track_gap = merge_track_gap_seconds
        self.pending = []      # (保留起始時間, 停等區事件)
        self.reported = {}     # (目標規則, object_id) -> (時間, event_id)，整支影片有效
        self.last_seen = {}    # object_id -> 最後出現的時間

    def evaluate(self, observation: dict) -> list[dict]:
        events = []
        for rule in self.rules:
            events.extend(rule.evaluate(observation))
        now = observation["frame"]["timestamp_sec"]
        items = observation.get("observations", {})
        for group in ("vehicles", "rule_vehicles", "area_vehicles"):
            for vehicle in items.get(group) or []:
                if vehicle.get("object_id"):
                    self.last_seen[vehicle["object_id"]] = now
        return self._merge(events, now)

    def _still_tracked(self, event, now):
        """保留中的停等區事件是否還可能等到同一台車的闖紅燈事件。"""
        identity = event.get("object_id")
        seen = self.last_seen.get(identity)
        if seen is not None and now - seen <= self.merge_track_gap:
            return True
        target = next((r for r in self.rules if r.rule_id == self.merge[event["rule_id"]]), None)
        return identity in getattr(target, "pending_events", {})

    def finalize(self) -> list[dict]:
        events = []
        for rule in self.rules:
            events.extend(rule.finalize())
        return self._merge(events, None)

    def _merge(self, events, now):
        if not self.merge:
            return events
        targets = set(self.merge.values())
        output = []
        # 同一幀內先處理停等區事件（進入保留），同幀的闖紅燈事件才能吸收它。
        for event in sorted(events, key=lambda e: e["rule_id"] not in self.merge):
            rule_id, identity = event["rule_id"], event.get("object_id")
            at = now if now is not None else event["timestamp_sec"]
            if rule_id in targets and identity:
                absorbed = [e for _, e in self.pending if self.merge[e["rule_id"]] == rule_id and e.get("object_id") == identity]
                if absorbed:
                    self.pending = [(t, e) for t, e in self.pending if not any(e is a for a in absorbed)]
                    event.setdefault("merged_events", []).extend(absorbed)
                self.reported[(rule_id, identity)] = (at, event.get("event_id"))
                output.append(event)
            elif rule_id in self.merge and identity:
                earlier = self.reported.get((self.merge[rule_id], identity))
                if earlier:
                    rule = next(r for r in self.rules if r.rule_id == rule_id)
                    getattr(rule, "diagnostics", {}).setdefault("merged_into", []).append(
                        dict(event_id=event.get("event_id"), into_event_id=earlier[1]))
                    continue
                self.pending.append((at, event))
            else:
                output.append(event)
        due = [now is None or (now - t > self.merge_window and not self._still_tracked(e, now))
               for t, e in self.pending]
        released = [e for (_, e), go in zip(self.pending, due) if go]
        self.pending = [item for item, go in zip(self.pending, due) if not go]
        for event in released:
            if now is not None:
                event["released_timestamp_sec"] = now  # 延後輸出；標註影片依此時間顯示
            output.append(event)
        if now is None:
            self.reported.clear()
            self.last_seen.clear()
        return output


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
                 max_crossing_gap_seconds=0.32, max_line_handoff_ratio=0.008,
                 weak_signal_evidence=True, signal_hold_seconds=3.0, crosswalk_proxy=True,
                 require_red_for_suspected=False, require_direct_marking_evidence=True,
                 green_veto_seconds=0.5, crosswalk_proxy_mode="per_vehicle",
                 signal_selected_continuity=True, occluded_contact="skip",
                 proxy_requires_red=True, proxy_exclude_lateral=True,
                 confirm_line_handoff=False, confirm_crosswalk_proxy=False,
                 confirm_after_motion_gap=False, deferred_confirmation_seconds=3.0,
                 confirm_signal_episode=False):
        """weak_signal_evidence／signal_hold_seconds／crosswalk_proxy 只讓事件成為「疑似」：
        「確認」仍要求強燈號證據（與先前相同）且觀測到真正的停止線。
        require_red_for_suspected：疑似事件也必須在越線前後有紅燈證據（任何強度）。
        require_direct_marking_evidence：「確認」的停止線必須由模型直接觀測累積（標線降頻時的推移遮罩
        不計入次數）。補償層的線看 compensation.direct_rule_eligible；未啟用補償時看規則自己的直接觀測幀數。
        green_veto_seconds：越線前後這段時間內觀測到綠燈（任何強度）時不輸出疑似事件——綠燈之後至少隔著
        黃燈才會轉紅，紅燈結束則直接轉綠，因此越線前後 0.5 秒內出現綠燈，與「紅燈越線」矛盾。疑似事件在
        影片結束才輸出，期間若出現綠燈或車輛退回停止線前，待定的疑似事件即取消。0 為停用。「確認」不受影響。
        crosswalk_proxy_mode：斑馬線代理線的使用時機。"per_vehicle"（預設）每幀都產生代理線、獨立追蹤，
        某台車沒有重疊的真正停止線時就改用代理線；"no_stop_line" 為舊行為（整個畫面沒有停止線候選時才用）。
        代理線的事件一律只能是疑似（stop_line_observed=false）。
        signal_selected_continuity：燈號顏色衝突時沿用先前的主燈號，但只在主燈號是綠／黃燈時（見
        signal_state.TemporalSignalState）；紅轉綠後主綠燈與側邊紅燈並存時不再判成未知。只會減少事件。
        occluded_contact：車框底邊（接地點代理）被較近的車輛遮住時的處理。"skip"（預設）：該幀不算線前／線後
        觀測（同快取框）；"suspected"：照常累積但不得確認（條件 contact_point_visible）；"off"：舊行為。
        遮擋判定：另一台車（car／motorcycle／bus／truck，非同一 ID、非重複偵測）底邊比本車低、上緣高於本車底邊，
        且與本車水平重疊至少本車寬的 30%。
        proxy_requires_red／proxy_exclude_lateral：斑馬線代理線的疑似事件必須在越線前後有紅燈證據（任何強度），
        且車輛最近 2 秒不是橫向移動（水平位移 ≥ 車寬一半且 ≥ 底邊垂直位移 2 倍，多為橫越或轉彎的車）。
        4av 延後確認（預設全部關閉＝行為與輸出不變；設定檔 configs/red_light_rules.json）：疑似事件本來就保留到
        片尾才輸出，期間車輛繼續被追蹤；以下選項讓「首次越線後 deferred_confirmation_seconds 秒內」補齊的直接觀測
        也能把同一次越線升級為確認（事件沿用原 event_id，另記 confirmation_basis 與越線起點）。每條替代路徑仍要求
        越線前紅燈、確認當下強紅燈、同一燈號、接地點可見等原條件，只取代下列單一阻擋條件；同一次確認
        最多使用一條替代路徑（例如交接＋運動失效兩項同時缺少時仍是疑似）：
        confirm_line_handoff（R2）：停止線交接（line_reassociated_from）不再阻擋確認，條件是交接前的線曾達到確認所需的
        直接觀測（被車身遮住前）、確認當下的線有直接觀測（交接本身已要求近共線、間距 ≤ max_line_handoff_ratio 畫面高）。
        confirm_crosswalk_proxy（R3）：斑馬線代理線的越線可以確認，條件是車輛接地點有 2 次新觀測越過同一條
        直接觀測（非預測、marking_source=direct）斑馬線的遠端邊緣——整台車通過斑馬線，必然已越過其前方的停止線；
        車框中心須在斑馬線中段（兩端尖角的邊緣擬合不可靠），且最近 2 秒（運動補償後）沿越線方向前進至少半個
        斑馬線深度、不是橫向移動（proxy_exclude_lateral），排除斑馬線多邊形抖動與橫越、路邊的車。
        confirm_after_motion_gap（R4）：鏡頭運動短暫失效不再阻擋確認，條件是最後一次線前觀測與最近連續 2 次線後
        觀測都不在失效幀，且停止線在最後一個失效幀之後重新被直接觀測（線的位置已重新定錨）。"""
        self.context = context
        self.signal_confidence = signal_confidence
        self.line_margin = max(line_margin_pixels,
                               round(context.frame_height * 0.008))
        self.min_track_frames = min_track_frames
        self.min_line_area = min_line_area
        self.min_line_aspect = min_line_aspect
        self.max_line_angle = max_line_angle
        self.min_line_stable_frames = min_line_stable_frames
        # 換線交接只允許幾乎重合的線段；交接後的事件不能成為「確認」。
        self.max_line_handoff_ratio = max_line_handoff_ratio
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
        self.signal_tracker = CombinedSignalState(context.fps, anchor_confidence=signal_confidence,
                                                  window_seconds=stable_signal_seconds,
                                                  weak_evidence=weak_signal_evidence,
                                                  hold_seconds=signal_hold_seconds,
                                                  selected_continuity=signal_selected_continuity)
        if occluded_contact not in ("skip", "suspected", "off"):
            raise ValueError("occluded_contact must be skip, suspected or off")
        self.occluded_contact = occluded_contact
        self.proxy_requires_red = proxy_requires_red
        self.proxy_exclude_lateral = proxy_exclude_lateral
        self.history = TrackHistory(seconds=2.0)
        self.crosswalk_proxy = crosswalk_proxy
        if crosswalk_proxy_mode not in ("per_vehicle", "no_stop_line"):
            raise ValueError("crosswalk_proxy_mode must be per_vehicle or no_stop_line")
        self.crosswalk_proxy_mode = crosswalk_proxy_mode
        self.require_red_for_suspected = require_red_for_suspected
        self.require_direct_marking_evidence = require_direct_marking_evidence
        self.green_veto_frames = max(0, round(context.fps * green_veto_seconds))
        self.confirm_line_handoff = bool(confirm_line_handoff)
        self._direct_lines = set()  # R2：曾達到確認直接觀測門檻的停止線 marking_id
        self.confirm_crosswalk_proxy = bool(confirm_crosswalk_proxy)
        self.confirm_after_motion_gap = bool(confirm_after_motion_gap)
        self.confirm_signal_episode = bool(confirm_signal_episode)
        self._signal_episode = None
        self._signal_episode_sequence = 0
        self._episode_previous_frame = None
        if (not isinstance(deferred_confirmation_seconds, (int, float)) or isinstance(deferred_confirmation_seconds, bool)
                or not math.isfinite(deferred_confirmation_seconds) or deferred_confirmation_seconds <= 0):
            raise ValueError("deferred_confirmation_seconds must be finite and positive")
        self.deferred_confirmation_frames = max(1, round(context.fps * deferred_confirmation_seconds))
        self.deferred_confirmation = (self.confirm_line_handoff or self.confirm_crosswalk_proxy
                                      or self.confirm_after_motion_gap or self.confirm_signal_episode)
        self.last_green_frame = None
        self._marking_evidence = marking_evidence({})
        self.diagnostics = {}

    def _update_signal_episode(self, frame, lights, strong_state):
        """R1 保守試驗：同一空間位置、單一直接紅燈軌跡的短暫換號。

        不把 weak／held 當成直接證據；任何綠黃偵測、顏色衝突或輸入不連續即切斷。
        不在無車道關聯時以多數決跨燈接續；保留原始 signal_id 供覆核。
        """
        index = frame['index']
        evidence = self.signal_tracker.evidence
        previous = self._signal_episode
        continuous = is_continuous(self._episode_previous_frame, frame, .2)
        self._episode_previous_frame = dict(frame)
        contrary = any(l.get('class_name') in {'green', 'yellow'} and l.get('confidence', 0) >= .18 for l in lights)
        if (not continuous or contrary or evidence.get('reason') == 'conflicting_signal_tracks'
                or previous and index - previous['last'] > max(1, round(self.context.fps * .2))):
            previous = self._signal_episode = None
        if strong_state != 'red' or evidence.get('last_observed_frame') != index or contrary:
            return
        identity = evidence.get('signal_id')
        tracks = self.signal_tracker.strong.tracks
        track = tracks.get(identity)
        direct = [t for t in tracks.values() if t['last'] == index]
        if track is None or track.get('center') is None or len(direct) != 1:
            self._signal_episode = None
            return
        center, size = track['center'], track['size']
        compatible = (previous and math.dist(center, previous['center']) <= max(12., min(size, previous['size']))
                      and .5 <= size / previous['size'] <= 2.)
        if not compatible:
            self._signal_episode_sequence += 1
            previous = dict(id=f'red-episode-{self._signal_episode_sequence}', since=index)
        self._signal_episode = dict(previous, last=index, center=center, size=size)
        evidence['red_episode_id'] = previous['id']
        evidence['red_episode_since_frame'] = previous['since']

    @staticmethod
    def _line_angle(line):
        x1, y1, x2, y2 = line
        angle = abs(math.degrees(math.atan2(y2 - y1, x2 - x1)))
        return min(angle, 180.0 - angle)

    def _project_line_tracks(self, observation):
        """重用補償層提供的鏡頭變換，讓規則內的舊線也位於當幀座標。"""
        meta=observation.get("postprocessing", {}).get("road_marking_compensation", {})
        motion=road_motion(observation)
        # 補償層容忍鏡頭運動短暫失效（allow_motion_gaps）時不重設；此期間的越線只支持疑似。
        self._motion_gap = bool(meta.get("motion_gap"))
        if meta.get("reset_reason"):
            self.line_tracks.clear()
            self.crossing_states.clear()
        elif self._motion_gap:
            # 只有補償層的真正停止線容忍短暫失效；斑馬線代理線（備援）沿用舊行為，失效即清除。
            proxies={track["marking_id"] for track in self.line_tracks.values() if track.get("proxy")}
            self.line_tracks={key:track for key,track in self.line_tracks.items() if not track.get("proxy")}
            self.crossing_states={key:state for key,state in self.crossing_states.items() if key[1] not in proxies}
        if not motion.get("valid") or motion.get("matrix") is None:
            return
        matrix=motion["matrix"]
        def transform(x,y):
            scale=matrix[2][0]*x+matrix[2][1]*y+matrix[2][2]
            if abs(scale)<1e-8:
                raise ValueError("Invalid homography")
            return [(matrix[0][0]*x+matrix[0][1]*y+matrix[0][2])/scale,
                    (matrix[1][0]*x+matrix[1][1]*y+matrix[1][2])/scale]
        self.history.project(lambda x, y: tuple(transform(x, y)))
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

    def _crosswalk_proxy_lines(self, observations):
        """沒有可用停止線時，以直接觀測到的斑馬線近端邊緣作為代理線（只支持「疑似」）。

        台灣路口停止線位於斑馬線前方，越過斑馬線近端必然已越過停止線，因此是保守的代理。
        近端：行進方向 away 取斑馬線多邊形下緣，toward 取上緣。
        """
        lines = []
        for region in observations.get("traffic_regions", []):
            if region.get("kind") != "crosswalk" or region.get("predicted"):
                continue
            hull = np.asarray(region["polygon"], dtype=np.float32)
            if len(hull) < 3:
                continue
            left, right = int(np.argmin(hull[:, 0])), int(np.argmax(hull[:, 0]))
            n = len(hull)
            chain_a = [hull[(left + k) % n] for k in range((right - left) % n + 1)]
            chain_b = [hull[(right + k) % n] for k in range((left - right) % n + 1)]
            pick = max if self.crossing_direction == "away" else min
            near = pick((chain_a, chain_b), key=lambda c: float(np.mean([p[1] for p in c])))
            line = self._fit_edge(near)
            if line is None:
                continue
            x1, _, x2, _ = line
            angle = self._line_angle(line)
            if angle > self.max_line_angle:
                continue
            top, bottom = min(line[1], line[3]), max(line[1], line[3])
            item = {"area": int(round((x2 - x1) * max(1.0, bottom - top))),
                    "bbox_xywh": [x1, top, x2 - x1, max(1.0, bottom - top)],
                    "centroid": [(x1 + x2) / 2, (line[1] + line[3]) / 2], "line_xyxy": line,
                    "angle_degrees": round(angle, 2), "proxy": "crosswalk_near_edge",
                    "region_id": region.get("region_id")}
            if self.confirm_crosswalk_proxy:
                # R3：遠端邊緣（另一條鏈）與斑馬線本身是否為模型直接觀測；只在啟用時加入，關閉時輸出不變。
                far = self._fit_edge(chain_b if near is chain_a else chain_a)
                item["far_line_xyxy"] = far
                item["region_direct"] = region.get("marking_source", "direct") == "direct"
            lines.append(item)
        return lines

    @staticmethod
    def _fit_edge(chain):
        """以直線擬合斑馬線多邊形的一側邊緣；邊緣太短或近垂直時回傳 None。"""
        chain = np.asarray(chain)
        if len(chain) < 2 or np.ptp(chain[:, 0]) < 10:
            return None
        vx, vy, x0, y0 = cv2.fitLine(chain.reshape(-1, 1, 2), cv2.DIST_L2, 0, .01, .01).ravel()
        if abs(vx) < 1e-6:
            return None
        x1, x2 = float(chain[:, 0].min()), float(chain[:, 0].max())
        return [x1, float(y0 + (x1 - x0) * vy / vx), x2, float(y0 + (x2 - x0) * vy / vx)]

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
                item["direct_observation"] = bool(self._marking_evidence["direct"])
                candidates.append(item)
        if self.crosswalk_proxy and self.crosswalk_proxy_mode == "per_vehicle":
            candidates += self._crosswalk_proxy_lines(observations)
        elif not candidates and self.crosswalk_proxy:
            candidates = self._crosswalk_proxy_lines(observations)
        return candidates

    def _update_stop_lines(self, candidates, frame_index):
        unused = set(self.line_tracks)
        updated = []
        for component in sorted(candidates, key=lambda item: item["area"],
                                reverse=True):
            cx, cy = component["centroid"]
            best_id, best_distance = None, float("inf")
            for line_id in sorted(unused):
                previous = self.line_tracks[line_id]
                if bool(previous.get("proxy")) != bool(component.get("proxy")):
                    continue  # 真正的停止線與斑馬線代理線分開追蹤
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
            direct = bool(component.get("direct_observation", False))
            if best_id is None:
                best_id = self.next_line_id
                self.next_line_id += 1
                stable_frames = 1
                direct_frames, last_direct = int(direct), (frame_index if direct else None)
            else:
                unused.remove(best_id)
                old = self.line_tracks[best_id]
                stable_frames = old["stable_frames"] + 1
                direct_frames = old.get("direct_frames", 0) + direct
                last_direct = frame_index if direct else old.get("last_direct_frame")
            tracked = dict(component, marking_id=f"stopline-{best_id:04d}",
                           stable_frames=stable_frames,
                           last_frame=frame_index, direct_frames=direct_frames,
                           last_direct_frame=last_direct)
            self.line_tracks[best_id] = tracked
            # 補償層已驗證多幀，不再重複等待 5 幀而錯過短暫可見的停止線。
            if stable_frames >= self.min_line_stable_frames or component.get("compensation", {}).get("rule_eligible", False):
                updated.append(tracked)
        self.line_tracks = {
            line_id: item for line_id, item in self.line_tracks.items()
            if frame_index - item["last_frame"] <= self.max_crossing_gap
        }
        if self.confirm_line_handoff:
            # R2：記錄曾達到「確認」直接觀測門檻的線。交接多半發生在車輛壓上舊線、舊線被車身遮住時，
            # 交接當下舊線已不是直接觀測，因此改看它被遮住之前是否曾經成立。
            self._direct_lines.update(item["marking_id"] for item in updated
                                      if self._stop_line_direct_evidence(item, frame_index))
        # 同一條線的可見片段可能改變補償 ID。只在舊線暫時消失、區間重疊且近共線時，
        # 將尚未完成的越線狀態交接給新片段；保留原時間，不能延長過期狀態。
        active_ids={item["marking_id"] for item in updated}
        for current in updated:
            candidates=[]
            x,_,width,_=current["bbox_xywh"]
            for old in self.line_tracks.values():
                if bool(old.get("proxy")) != bool(current.get("proxy")):
                    continue
                if old["marking_id"] in active_ids or old["last_frame"]==frame_index:
                    continue
                ox,_,ow,_=old["bbox_xywh"]
                if min(x+width,ox+ow)<=max(x,ox) or abs(current["angle_degrees"]-old["angle_degrees"])>10:
                    continue
                cx=(max(x,ox)+min(x+width,ox+ow))/2
                delta=abs(self._line_y(current["line_xyxy"],cx)-self._line_y(old["line_xyxy"],cx))
                if delta<=self.context.frame_height*self.max_line_handoff_ratio:
                    candidates.append((delta,old))
            if candidates:
                old=min(candidates,key=lambda pair:pair[0])[1]
                for (vehicle_id,line_id),state in list(self.crossing_states.items()):
                    if line_id!=old["marking_id"] or state["phase"]!="before_line":
                        continue
                    new_key=(vehicle_id,current["marking_id"])
                    if self.crossing_states.get(new_key,{}).get("before_count",0)<state["before_count"]:
                        self.crossing_states[new_key]=dict(state,line_reassociated_from=line_id)
                        if self.confirm_line_handoff:
                            # R2：交接前的線也必須曾有足以確認的直接觀測；多次交接時全部都要成立。
                            self.crossing_states[new_key]["handoff_direct"] = bool(
                                state.get("handoff_direct", True) and old["marking_id"] in self._direct_lines)
                        del self.crossing_states[(vehicle_id,line_id)]
        return updated

    def _update_deferred_state(self, state, side, gap_now, index, stop_line, box, reference_x, y, contact_occluded):
        """4av：替代確認路徑的逐幀狀態（只在啟用延後確認時維護，關閉時狀態與輸出不變）。"""
        if side == -1:
            state["before_in_gap"] = gap_now
        if state["after_count"] == 0:
            state.update(first_after_frame=None, after_clean=0, far_count=0)
            return
        if state["after_count"] == 1:
            state["first_after_frame"] = index
        # R4：最近連續的線後觀測都不在運動失效幀。
        state["after_clean"] = 0 if gap_now else state.get("after_clean", 0) + 1
        # R3：接地點越過同一條直接觀測斑馬線的遠端邊緣（新觀測、接地點未被遮住）。
        far = stop_line.get("far_line_xyxy")
        if not (self.confirm_crosswalk_proxy and stop_line.get("proxy") and far):
            state["far_count"] = 0
            return
        if not stop_line.get("region_direct") or contact_occluded:
            # 標線降頻時斑馬線隔幀才是直接觀測；推移幀與接地點被遮住的幀不是證據，不計入也不歸零。
            return
        if not self._inside_crosswalk_span(box, stop_line):
            # 斑馬線兩端的尖角處近、遠端邊緣收斂，擬合不可靠（路邊、橫越的車）：不算證據並歸零。
            state["far_count"] = 0
            return
        x = min(max(reference_x, min(far[0], far[2])), max(far[0], far[2]))
        distance = y - self._line_y(far, x)
        oriented = -distance if self.crossing_direction == "away" else distance
        state["far_count"] = state.get("far_count", 0) + 1 if oriented > self.line_margin else 0

    @staticmethod
    def _inside_crosswalk_span(box, stop_line, inset=0.1):
        """R3：車框中心 x 位於斑馬線近端與遠端邊緣的中段（兩端各內縮 inset 比例）。"""
        cx = (box[0] + box[2]) / 2
        for line in (stop_line["line_xyxy"], stop_line["far_line_xyxy"]):
            left, right = min(line[0], line[2]), max(line[0], line[2])
            margin = (right - left) * inset
            if not left + margin <= cx <= right - margin:
                return False
        return True

    def _crossed_crosswalk_forward(self, object_id, now, stop_line, box):
        """R3：車輛最近 2 秒（鏡頭運動補償後）沿越線方向前進至少半個斑馬線深度，且不是橫向移動。
        只看相對斑馬線的位置時，斑馬線多邊形本身的抖動也可能造成「通過遠端」。"""
        moved = self.history.displacement(object_id, now, 2.0)
        if not moved:
            return False
        cx = (box[0] + box[2]) / 2
        depth = abs(self._line_y(stop_line["line_xyxy"], cx) - self._line_y(stop_line["far_line_xyxy"], cx))
        forward = -moved["dy"] if self.crossing_direction == "away" else moved["dy"]
        lateral = self.proxy_exclude_lateral and self.history.lateral(object_id, now, 2.0)
        return forward >= 0.5 * depth and not lateral

    @staticmethod
    def _clean_after_gap(state, stop_line):
        """R4：運動失效後的越線是否只由有效幀支持，且停止線在最後一個失效幀之後重新被直接觀測。"""
        if state.get("after_clean", 0) < 2 or state.get("before_in_gap", True):
            return False
        compensation = stop_line.get("compensation") or {}
        last_direct = (compensation.get("last_direct_observed_frame") if compensation
                       else stop_line.get("last_direct_frame"))
        last_gap = state.get("last_gap_frame")
        return last_gap is not None and last_direct is not None and last_direct > last_gap

    def _stop_line_direct_evidence(self, stop_line, index):
        """停止線是否有足以支持「確認」的直接觀測（推移遮罩不計入）。"""
        if stop_line.get("proxy"):
            return False
        compensation = stop_line.get("compensation")
        if compensation:
            if "direct_rule_eligible" in compensation:
                return bool(compensation["direct_rule_eligible"])
            return bool(compensation.get("rule_eligible"))  # 1.8 以前的紀錄沒有直接觀測欄位：沿用原判定
        last = stop_line.get("last_direct_frame")
        return bool(stop_line.get("direct_frames", 0) >= self.min_line_stable_frames and last is not None
                    and index - last <= self.max_crossing_gap and self._marking_evidence["aligned"])

    @staticmethod
    def _covers_x(component, x):
        left, _, width, _ = component["bbox_xywh"]
        padding = max(30.0, width * 0.20)
        return left - padding <= x <= left + width + padding

    OCCLUDER_CLASSES = {"car", "motorcycle", "bus", "truck"}

    @classmethod
    def _contact_occluded(cls, box, object_id, vehicles):
        """車框底邊是否被較近的車輛遮住（此時底邊不是接地點，不能用來判定越線）。"""
        x1, y1, x2, y2 = box
        width, height = x2 - x1, y2 - y1
        if width <= 0 or height <= 0:
            return False
        margin = max(4.0, height * 0.05)
        for other in vehicles:
            if other.get("object_id") == object_id or other.get("class_name") not in cls.OCCLUDER_CLASSES:
                continue
            ox1, oy1, ox2, oy2 = other.get("bbox_xyxy") or (0, 0, 0, 0)
            if not (oy2 >= y2 + margin and oy1 < y2):
                continue
            overlap = min(x2, ox2) - max(x1, ox1)
            if overlap < 0.3 * width:
                continue
            inter = overlap * max(0.0, min(y2, oy2) - max(y1, oy1))
            union = width * height + (ox2 - ox1) * (oy2 - oy1) - inter
            if union > 0 and inter / union >= 0.7:
                continue  # 幾乎重合：同一台車的重複偵測，不是遮擋
            return True
        return False

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
        self._marking_evidence = marking_evidence(observation)
        # 只有強證據沿用原規則的所有判斷（含綠／黃燈清除狀態與「確認」）；弱證據與延續只支持疑似。
        strong_state = signal_state if self.signal_tracker.strength == "strong" else None
        if self.confirm_signal_episode:
            self._update_signal_episode(frame, obs['traffic_lights'], strong_state)
        self._project_line_tracks(observation)
        self.history.update(frame["timestamp_sec"], index, obs.get("rule_vehicles", obs["vehicles"]),
                            lambda v: v.get("rider_group", {}).get("vehicle_bbox_xyxy") or v.get("bbox_xyxy"))
        stop_components = self._update_stop_lines(
            self._valid_stop_components(obs), index)
        # 只保留短時間連續軌跡；不能把數秒前的線前位置接成現在的越線。
        self.crossing_states = {key:state for key,state in self.crossing_states.items()
                                if index-state["last_frame"] <= self.max_crossing_gap}
        self.diagnostics = {"frame":index,"signal":dict(self.signal_tracker.evidence),
                            "usable_stop_lines":len(stop_components),"vehicles":[],
                            "skipped_vehicles":[]}
        if signal_state == "green" and self.green_veto_frames:
            self.last_green_frame = index
            # 越線後不久就出現綠燈：取消這段期間的待定疑似事件。
            for object_id, pending in list(self.pending_events.items()):
                if index - pending["frame"] <= self.green_veto_frames:
                    del self.pending_events[object_id]
                    self.diagnostics.setdefault("cancelled_pending", []).append(
                        dict(event_id=pending["event_id"], object_id=object_id, reason="green_after_crossing"))
        if strong_state in {"green", "yellow"}:
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
            contact_occluded = (self.occluded_contact != "off" and
                                self._contact_occluded(vehicle_box, object_id, obs["vehicles"]))
            overlapping_lines = 0
            overlapping = [line for line in stop_components
                           if min(line["bbox_xywh"][0] + line["bbox_xywh"][2], vehicle_box[2])
                           >= max(line["bbox_xywh"][0], vehicle_box[0])]
            if any(not line.get("proxy") for line in overlapping):
                # 有重疊的真正停止線時不使用斑馬線代理線（代理線只是備援）。
                overlapping = [line for line in overlapping if not line.get("proxy")]
            for stop_line in overlapping:
                left, _, width, _ = stop_line["bbox_xywh"]
                overlap_left, overlap_right = max(left, vehicle_box[0]), min(left+width, vehicle_box[2])
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
                gap_now = bool(getattr(self, "_motion_gap", False) or stop_line.get("compensation", {}).get("motion_gap"))
                if gap_now:
                    state["motion_gap"] = True
                    if self.confirm_after_motion_gap:
                        state["last_gap_frame"] = index
                if vehicle.get("source_frame", index) != index:
                    # 降頻推論時沿用的上一幀車框不是新觀測：只讓狀態不過期，
                    # 不累積線前／線後次數，也不產生事件。
                    self.diagnostics["vehicles"].append({"object_id":object_id,
                        "marking_id":stop_line["marking_id"],"signed_distance_pixels":round(signed_distance,2),
                        "phase":state["phase"],"before_count":state["before_count"],
                        "after_count":state["after_count"],"red_before":state["red_before"],
                        "blocked_by":["cached_detection"]})
                    continue
                if contact_occluded and self.occluded_contact == "skip":
                    # 接地點被較近的車遮住：底邊位置不可信，不算新的線前／線後觀測（同快取框）。
                    self.diagnostics["vehicles"].append({"object_id":object_id,
                        "marking_id":stop_line["marking_id"],"signed_distance_pixels":round(signed_distance,2),
                        "phase":state["phase"],"before_count":state["before_count"],
                        "after_count":state["after_count"],"red_before":state["red_before"],
                        "blocked_by":["contact_point_occluded"]})
                    continue
                if contact_occluded:
                    state["contact_occluded"] = True
                if side == -1:
                    pending = self.pending_events.get(object_id)
                    if pending and pending["evidence"]["stop_line"].get("marking_id") == stop_line["marking_id"]:
                        # 越線後又退回停止線前（多半是誤判或在線上來回的偵測）：取消待定的疑似事件。
                        del self.pending_events[object_id]
                        self.diagnostics.setdefault("cancelled_pending", []).append(
                            dict(event_id=pending["event_id"], object_id=object_id, reason="returned_behind_line"))
                    state["before_count"] += 1
                    state["after_count"] = 0
                    state["before_frame"] = index
                    if strong_state == "red":
                        state["red_before_frame"] = index
                        state["red_before_evidence"] = dict(self.signal_tracker.evidence)
                    if signal_state == "red":
                        state["any_red_before_frame"] = index
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
                if self.deferred_confirmation:
                    self._update_deferred_state(state, side, gap_now, index, stop_line, vehicle_box,
                                                reference_x, y, contact_occluded)
                blocked_by = []
                if state["before_count"] < 2:
                    blocked_by.append("insufficient_before_line_observations")
                if state["after_count"] < 2:
                    blocked_by.append("insufficient_after_line_observations")
                if not state["red_before"]:
                    blocked_by.append("no_recent_red_before_crossing")
                if strong_state != "red":
                    blocked_by.append("signal_not_confirmed_red")
                if stop_line.get("proxy"):
                    blocked_by.append("proxy_stop_line")
                if (state.get("red_before_evidence", {}).get("signal_id")
                        != self.signal_tracker.evidence.get("signal_id")):
                    blocked_by.append("signal_identity_not_confirmed")
                if state.get("line_reassociated_from"):
                    blocked_by.append("stop_line_reassociated")
                if state.get("motion_gap"):
                    blocked_by.append("camera_motion_gap")
                if state.get("contact_occluded"):
                    blocked_by.append("contact_point_occluded")
                direct_line = self._stop_line_direct_evidence(stop_line, index)
                if self.require_direct_marking_evidence and not direct_line and not stop_line.get("proxy"):
                    blocked_by.append("stop_line_not_directly_observed")
                if object_id in self.reported:
                    blocked_by.append("already_reported")
                self.diagnostics["vehicles"].append({"object_id":object_id,
                    "marking_id":stop_line["marking_id"],"signed_distance_pixels":round(signed_distance,2),
                    "phase":state["phase"],"before_count":state["before_count"],
                    "after_count":state["after_count"],"red_before":state["red_before"],
                    "blocked_by":blocked_by})
                # 停止線條件：真正的停止線＋直接觀測，且沒有交接（R2 允許兩端都直接觀測的交接）；
                # 或（R3）代理線但車輛已完整通過直接觀測的斑馬線。運動失效（R4）另判。
                basis = []
                line_ok = not stop_line.get("proxy") and (direct_line or not self.require_direct_marking_evidence)
                if line_ok and state.get("line_reassociated_from"):
                    line_ok = bool(self.confirm_line_handoff and direct_line and state.get("handoff_direct"))
                    if line_ok:
                        basis.append("line_handoff_direct")
                proxy_ok = bool(self.confirm_crosswalk_proxy and stop_line.get("proxy") and state.get("far_count", 0) >= 2
                                and self._crossed_crosswalk_forward(object_id, frame["timestamp_sec"], stop_line, vehicle_box))
                if proxy_ok:
                    basis.append("crosswalk_crossed_completely")
                motion_ok = not state.get("motion_gap")
                if not motion_ok and self.confirm_after_motion_gap and self._clean_after_gap(state, stop_line):
                    motion_ok = True
                    basis.append("clean_crossing_after_motion_gap")
                same_signal = (state.get("red_before_evidence", {}).get("signal_id") == self.signal_tracker.evidence.get("signal_id"))
                episode_ok = bool(self.confirm_signal_episode and
                    state.get("red_before_evidence", {}).get("red_episode_id") and
                    state["red_before_evidence"]["red_episode_id"] == self.signal_tracker.evidence.get("red_episode_id") and
                    state.get("before_frame") is not None and
                    index - state["before_frame"] <= self.max_crossing_gap)
                if not same_signal and episode_ok:
                    basis.append("same_direct_red_episode")
                # 替代路徑只在首次線後觀測起 deferred_confirmation_seconds 內有效（同一次越線），
                # 且每次確認最多替代一個阻擋條件（兩項同時替代的證據疊加風險，例如看板誤偵測在鏡頭轉動時「越線」）。
                in_window = not basis or (len(basis) == 1 and state.get("first_after_frame") is not None and
                                          index - state["first_after_frame"] <= self.deferred_confirmation_frames)
                confirmed = (state["after_count"] >= 2 and state["red_before"] and strong_state == "red"
                        and (same_signal or episode_ok)
                        and (line_ok or proxy_ok)
                        and motion_ok and in_window
                        and not state.get("contact_occluded")
                        and object_id not in self.reported)
                any_red = signal_state == "red" or (
                    state.get("any_red_before_frame") is not None
                    and index - state["any_red_before_frame"] <= self.max_crossing_gap)
                suspected = (state["after_count"] >= 2 or
                             (state["after_count"] >= 1 and signal_state == "red"))
                if self.require_red_for_suspected and not any_red:
                    suspected = False
                if stop_line.get("proxy") and suspected:
                    if self.proxy_requires_red and not any_red:
                        suspected = False
                        blocked_by.append("proxy_without_red")
                    elif self.proxy_exclude_lateral and self.history.lateral(object_id, frame["timestamp_sec"], 2.0):
                        suspected = False
                        blocked_by.append("lateral_motion")
                green_nearby = (self.last_green_frame is not None and self.green_veto_frames
                                and index - self.last_green_frame <= self.green_veto_frames)
                if green_nearby:
                    suspected = False
                    blocked_by.append("green_near_crossing")
                if object_id in self.reported or not (confirmed or suspected):
                    continue
                conditions = {
                    "tracked_vehicle": True,
                    "usable_overlapping_stop_line": True,
                    "before_line_observed": state["before_count"] >= 2,
                    "crossing_observed": state["after_count"] >= 2,
                    "red_before_crossing": state["red_before"],
                    "red_at_crossing": strong_state == "red",
                    "strong_signal_evidence": strong_state == "red",
                    "stop_line_observed": not stop_line.get("proxy"),
                    "same_signal": bool(state.get("red_before_evidence", {}).get("signal_id")) and
                        state.get("red_before_evidence", {}).get("signal_id") == self.signal_tracker.evidence.get("signal_id"),
                    "same_stop_line": not state.get("line_reassociated_from"),
                    "camera_motion_compensated": not state.get("motion_gap", False),
                    "contact_point_visible": not state.get("contact_occluded", False),
                }
                if self.require_direct_marking_evidence:
                    conditions["stop_line_direct_evidence"] = direct_line
                if self.confirm_line_handoff and state.get("line_reassociated_from"):
                    conditions["stop_line_handoff_direct"] = bool(direct_line and state.get("handoff_direct"))
                if self.confirm_crosswalk_proxy and stop_line.get("proxy"):
                    conditions["crosswalk_crossed_completely"] = state.get("far_count", 0) >= 2
                if self.confirm_after_motion_gap and state.get("motion_gap"):
                    conditions["clean_crossing_after_motion_gap"] = self._clean_after_gap(state, stop_line)
                if self.confirm_signal_episode:
                    conditions["same_direct_red_episode"] = episode_ok
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
                        "signal_strength": self.signal_tracker.strength,
                        "signal_evidence": dict(self.signal_tracker.evidence),
                        "crossing_direction": self.crossing_direction,
                        "before_frame": state["before_frame"],
                        "red_before_frame": state["red_before_frame"],
                        "red_before_evidence": state.get("red_before_evidence"),
                        "line_reassociated_from": state.get("line_reassociated_from"),
                        "reference_point": [reference_x, y],
                        "reference_point_method": "bbox_bottom_overlap",
                        "signed_distance_pixels": round(signed_distance, 2),
                        "marking_evidence": dict(self._marking_evidence),
                    },
                }
                if self.deferred_confirmation:
                    # 延後確認：記錄確認依據與首次越線幀；standard＝原本的確認條件全部成立。
                    event["confirmation_basis"] = (basis or ["standard"]) if confirmed else []
                    event["evidence"]["first_after_line_frame"] = state.get("first_after_frame")
                if confirmed:
                    state["phase"] = "crossed_line"
                    self.reported.add(object_id)
                    self.pending_events.pop(object_id, None)
                    events.append(clone(event))
                elif previous is None or (conditions["crossing_observed"], sum(conditions.values())) > (
                        previous["conditions"]["crossing_observed"], len(previous["met_conditions"])):
                    # Keep the strongest snapshot, without combining unrelated lines/frames.
                    self.pending_events[object_id] = clone(event)
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


# 4ba 起點與終點推定：另一側樣本彼此最多相距的秒數（與試算相同）。
ENDPOINT_CLUSTER_SECONDS = 1.0


class DoubleLineCrossingRule(ViolationRule):
    """Complete footprint-proxy crossings, with conservative maneuver inference."""

    def __init__(self, context, *, color, settings=None):
        if color not in ("yellow", "white"):
            raise ValueError("Double-line color must be yellow or white")
        self.context = context
        self.rule_id = f"double_{color}_line_crossing"
        self.class_name = f"solid double {color}"
        self.label = "跨越雙黃線" if color == "yellow" else "跨越雙白線"
        self.diagnostics = {}
        self.settings = validate_rule_settings(settings)
        # 診斷控制不進入原 settings／事件 evidence.parameters，停用時序列化保持原樣。
        self.detailed_diagnostics = self.settings.pop("detailed_diagnostics")
        # 4am 選項維持預設（關閉）時不留在 settings，事件參數與診斷與先前逐位元相同。
        for key in (OCCLUSION_OPTION_KEYS + ORIGIN_OPTION_KEYS + STRADDLE_OPTION_KEYS + ENDPOINT_OPTION_KEYS
                    + BAND_DWELL_OPTION_KEYS + ("confirm_short_position_gap",)):
            if self.settings[key] == DOUBLE_LINE_DEFAULTS[key]:
                del self.settings[key]
        self.straddle_seconds = self.settings.get("straddle_min_seconds", 0.0)
        self.straddle_merge = self.settings.get("straddle_merge_seconds", DOUBLE_LINE_DEFAULTS["straddle_merge_seconds"])
        self.pending_straddles = {}  # object_id -> 待合併的壓線事件（4ao）
        # 4ba 起點與終點推定：停用時不保留車框歷史，也不多做任何投影。
        self.endpoint_seconds = self.settings.get("endpoint_inference_seconds", 0.0)
        self.endpoint_min_height = self.settings.get("endpoint_min_box_height_ratio",
                                                     DOUBLE_LINE_DEFAULTS["endpoint_min_box_height_ratio"]) * context.frame_height
        self.endpoint_history = {}  # object_id -> 經運動補償帶到目前座標的較早車框
        self.endpoint_last = {}     # object_id -> 同車最後一筆雙線事件（原規則或推定）的確認時間
        # 4bb 壓線停留跨越：停用時不保留側別序列。
        self.dwell_seconds = self.settings.get("band_dwell_seconds", 0.0)
        option = lambda key: self.settings.get(key, DOUBLE_LINE_DEFAULTS[key])
        self.dwell_min_band = option("band_dwell_min_observations")
        self.dwell_min_line = option("band_dwell_min_line_ratio") * context.frame_height
        self.dwell_min_height = option("band_dwell_min_box_height_ratio") * context.frame_height
        self.dwell_series = {}   # object_id -> 逐幀側別樣本（每幀一個）
        self.dwell_last = {}     # object_id -> 同車最後一筆雙線事件的確認時間
        self.dwell_pending = []
        self._current_frame = None
        self.memory_seconds = self.settings.get("line_memory_seconds", 0.0)
        self.remembered_lines = {}
        self._side_history = {} if self.detailed_diagnostics else None
        self.final_diagnostics = None
        self.states = {}
        self.line_memory = {}
        self.previous_frame = None
        self.motion_gap_started = None
        self.previous_lines = {}
        self._frame_geometry = "direct"

    def evaluate(self, observation):
        frame = observation["frame"]
        self._current_frame = frame
        released = self._release_straddles(frame) if self.pending_straddles else []
        items = observation.get("observations", {})
        # 舊紀錄的雙線沒有 geometry_source 時，以本幀標線來源推定（見 marking_evidence 的退回規則）。
        self._frame_geometry = marking_geometry_source(marking_evidence(observation))
        lines = [line for line in items.get("double_lines", [])
                 if line.get("class_name") == self.class_name]
        vehicles = items.get("rule_vehicles", items.get("vehicles", []))
        fresh = [vehicle for vehicle in vehicles
                 if vehicle.get("source_frame") == frame["index"]]
        self.diagnostics = {
            "status": "evaluating", "enabled": True,
            "geometry_status": "observed" if lines else "geometry_unavailable",
            "line_track_ids": [line.get("track_id") for line in lines],
            "vehicle_count": len(vehicles), "fresh_vehicle_count": len(fresh),
            "cached_or_unknown_vehicle_count": len(vehicles) - len(fresh),
            "blocked_lines": [], "skipped_vehicles": [], "reset_reason": None,
            "settings": self.settings,
        }
        if self.detailed_diagnostics:
            self._diagnostic_frame = dict(frame)
            self.diagnostics["details"] = {"version": 1, "position_failures": [],
                "positions": [], "state_changes": [], "candidates": []}
            # 只保留短期量測段；每個量測均寫檔，可離線按 object_id 彙總整片，記憶體不隨片長增加。
            for key, summary in list(self._side_history.items()):
                if key not in self.states and (frame["timestamp_sec"]-summary["last_timestamp_sec"] > self.settings["max_observation_gap_sec"]
                        or frame["index"] <= summary["last_frame"]):
                    del self._side_history[key]
        motion = road_motion(observation)
        reset = None
        if not is_continuous(self.previous_frame, frame, self.settings["max_frame_gap_sec"]):
            reset = "discontinuous_input"
        self.previous_frame = dict(frame)
        matrix = homography(motion, self.settings["max_motion_error_px"])
        motion_gap = False
        if matrix is None:
            # 鏡頭運動短暫失敗：在 max_motion_gap_sec 內以「鏡頭不動」近似並加大側向容許，
            # 候選保留但標記為未經運動補償（事件最多為疑似）；超過時限才清除。
            now = frame["timestamp_sec"]
            if self.motion_gap_started is None:
                self.motion_gap_started = now
            if (self.settings["allow_motion_gaps"] and not reset
                    and now - self.motion_gap_started <= self.settings["max_motion_gap_sec"]):
                matrix, motion_gap = np.eye(3), True
                motion = dict(motion or {}, reprojection_error_px=self.settings["max_motion_error_px"])
        else:
            self.motion_gap_started = None
        if matrix is None:
            self._diagnose_all_states("motion_unavailable")
            self.states.clear()
            self.endpoint_history.clear()
            self.line_memory.clear()
            self.remembered_lines.clear()
            self.diagnostics.update(status="blocked", reset_reason="motion_unavailable")
            return released
        error = float(motion.get("reprojection_error_px", 0))  # 已由 homography() 驗證
        if reset:
            self._diagnose_all_states("discontinuous_input")
            self.states.clear()
            self.endpoint_history.clear()
            self.dwell_series.clear()
            self.line_memory.clear()
            self.remembered_lines.clear()
            self.diagnostics["reset_reason"] = reset
        lines = resolve_rule_lines(lines, self.line_memory, matrix, frame, self.settings, vehicles)
        usable = []
        for line in lines:
            reason = line_quality(line, self.settings)
            if line.get("source_frame") != frame["index"] or not line.get("track_id"):
                reason = "stale_or_untracked_line"
            if reason:
                self.diagnostics["blocked_lines"].append({"track_id": line.get("track_id"), "reason": reason})
            else:
                usable.append(line)
        if self.memory_seconds > 0:
            usable = usable + self._remember_lines(usable, matrix, frame)
        else:
            self._hand_off_lines(usable, matrix)
        valid_ids = {line["track_id"] for line in usable}
        vehicle_ids = {v.get("object_id") for v in vehicles}
        for key, state in list(self.states.items()):
            if (key[1] not in valid_ids or
                    frame["timestamp_sec"] - state["last_time"] > self.settings["max_observation_gap_sec"]):
                self._diagnose_state(key, "line_unavailable" if key[1] not in valid_ids else "observation_gap")
                del self.states[key]
                continue
            if key[0] not in vehicle_ids:
                # 偵測器短暫漏掉車輛，與「參考點暫時不可用」同樣處理：已 armed／越線中的
                # 候選保留到 max_observation_gap_sec，但標記缺口（事件最多為疑似）；
                # 尚在建立起點的候選直接放棄。不更新 last_time，也不補造位置。
                if state["phase"] in ("armed", "crossing"):
                    state["position_gap"] = True
                    state.update(after_path=[], destination_since=None, destination_count=0)
                elif state["phase"] != "reported":
                    self._diagnose_state(key, "vehicle_missing")
                    del self.states[key]
                    continue
            try:
                # Bring old ground samples into the current road coordinate system.
                # Evidence snapshots remain in their original image coordinates.
                for path in (state["before_path"], state["after_path"]):
                    if path:
                        projected = project_points([s["point"] for s in path], matrix)
                        for sample, point in zip(path, projected):
                            sample["point"] = point
                state["last_point"] = project_points([state["last_point"]], matrix)[0]
                x1, y1, x2, y2 = state["last_box"]
                box = np.asarray(project_points([[x1, y1], [x2, y2]], matrix))
                state["last_box"] = [*box.min(axis=0), *box.max(axis=0)]
            except ValueError:
                self._diagnose_state(key, "projection_invalid")
                del self.states[key]
        if self.endpoint_history:
            self._project_endpoint_history(matrix, error, motion_gap)
        if motion_gap:
            self.diagnostics["motion_gap"] = True
            for state in self.states.values():
                state["motion_gap"] = True
        events = self._evaluate_crossing(frame, fresh, usable, error, motion_gap)
        self.diagnostics["states"] = [{"object_id": key[0], "line_id": key[1], "phase": s["phase"]}
                                      for key, s in self.states.items()]
        if self.detailed_diagnostics:
            self.diagnostics["details"]["candidates"] = [self._state_summary(key) for key in self.states]
        return released + events if released else events

    def _state_summary(self, key):
        state = self.states[key]
        now = self._diagnostic_frame["timestamp_sec"]
        return dict(object_id=key[0], line_id=key[1], phase=state["phase"], origin=state["origin"],
                    elapsed_seconds=max(0., now-state["origin_since"]),
                    before_count=len(state["before_path"]), after_count=len(state["after_path"]),
                    destination_count=state["destination_count"],
                    destination_seconds=max(0., now-state["destination_since"]) if state["destination_since"] is not None else 0.,
                    since_last_observation_seconds=max(0., now-state.get("last_time", now)),
                    sides=clone(self._side_history.get(key)))

    def _diagnose_state(self, key, reason, *, action="deleted", **extra):
        # 候選的每次結束都經過這裡（刪除前呼叫），壓線在此判定（4ao）。
        if self.straddle_seconds > 0 and key in self.states:
            self._end_straddle(key, reason)
        if self.detailed_diagnostics and key in self.states:
            self.diagnostics["details"]["state_changes"].append(
                dict(self._state_summary(key), reason=reason, action=action, **extra))

    def _diagnose_all_states(self, reason):
        if self.detailed_diagnostics or self.straddle_seconds > 0:
            for key in self.states:
                self._diagnose_state(key, reason)

    def _diagnose_position(self, key, frame, position):
        if not self.detailed_diagnostics:
            return
        side, now, index = position["side"], frame["timestamp_sec"], frame["index"]
        summary = self._side_history.get(key)
        if (summary is None or now-summary["last_timestamp_sec"] > self.settings["max_observation_gap_sec"]
                or index <= summary["last_frame"]):
            summary = dict(first_side=side, last_side=side, first_frame=index, last_frame=index,
                           first_timestamp_sec=now, last_timestamp_sec=now,
                           counts={"-1": 0, "0": 0, "1": 0}, seconds={"-1": 0., "0": 0., "1": 0.})
            self._side_history[key] = summary
        # 每側秒數只累加連續幀、相同 side 的間隔，不把缺口／不穩定位置或快取當成證據。
        if (index == summary["last_frame"]+1 and side == summary["last_side"]
                and 0 < now-summary["last_timestamp_sec"] <= self.settings["max_observation_gap_sec"]):
            summary["seconds"][str(side)] += now-summary["last_timestamp_sec"]
        summary["counts"][str(side)] += 1
        summary.update(last_side=side, last_frame=index, last_timestamp_sec=now)
        self.diagnostics["details"]["positions"].append(dict(object_id=key[0], line_id=key[1],
            side=side, point=position["point"], extrapolated=position.get("extrapolated", False), sides=clone(summary)))

    def _hand_off_lines(self, usable, matrix):
        """雙線追蹤編號中斷（同一條線換了新編號）時，把車輛候選轉移到新編號。

        上一幀的線經運動補償後，若與本幀一條「新出現」的線在重疊的影像列上平均水平距離
        不超過 max_line_handoff_px，視為同一條實體線。轉移後標記 line_handoff，事件最多為疑似。
        """
        limit = self.settings["max_line_handoff_px"]
        current = {line["track_id"]: np.asarray(line["centerline"], dtype=float) for line in usable}
        previous, self.previous_lines = self.previous_lines, current
        if limit <= 0 or not previous:
            return
        # 依上一幀的線序走訪（不用 set），line_handoffs 診斷的順序才不受字串雜湊種子影響。
        lost = [track for track in previous if track not in current]
        new = [track for track in current if track not in previous]
        if not lost or not new:
            return
        mapping = {}
        for old in lost:
            try:
                path = np.asarray(project_points(previous[old], matrix), dtype=float)
            except ValueError:
                continue  # 投影越過地平線等不穩定情況：這條舊線不參與交接
            if not np.isfinite(path).all():
                continue
            best = None
            for track in new:
                target = current[track]
                low = max(path[:, 1].min(), target[:, 1].min())
                high = min(path[:, 1].max(), target[:, 1].max())
                if high - low < 20 or np.any(np.diff(path[:, 1]) <= 0) or np.any(np.diff(target[:, 1]) <= 0):
                    continue
                rows = np.linspace(low, high, 8)
                distance = float(np.mean(np.abs(np.interp(rows, path[:, 1], path[:, 0]) -
                                                np.interp(rows, target[:, 1], target[:, 0]))))
                if distance <= limit and (best is None or distance < best[0]):
                    best = (distance, track)
            if best:
                mapping[old] = best[1]
        for (object_id, track), state in list(self.states.items()):
            if track in mapping and (object_id, mapping[track]) not in self.states:
                self._diagnose_state((object_id, track), "line_handoff", action="transferred", new_line_id=mapping[track])
                if self.detailed_diagnostics and (object_id, track) in self._side_history:
                    self._side_history[(object_id, mapping[track])] = self._side_history.pop((object_id, track))
                state["line_handoff"] = True
                self.states[(object_id, mapping[track])] = self.states.pop((object_id, track))
        if mapping:
            self.diagnostics["line_handoffs"] = mapping

    @staticmethod
    def _row_distance(path, target):
        """兩條線在共同影像列（至少 20 列）上的平均水平距離；沒有足夠重疊時回傳 None。"""
        low, high = max(path[:, 1].min(), target[:, 1].min()), min(path[:, 1].max(), target[:, 1].max())
        if high - low < 20 or np.any(np.diff(path[:, 1]) <= 0) or np.any(np.diff(target[:, 1]) <= 0):
            return None
        rows = np.linspace(low, high, 8)
        return float(np.mean(np.abs(np.interp(rows, path[:, 1], path[:, 0]) -
                                    np.interp(rows, target[:, 1], target[:, 0]))))

    def _remember_lines(self, usable, matrix, frame):
        """線段記憶（line_memory_seconds > 0 時取代逐幀交接）。

        每條可用雙線保存最後一次的幾何，之後每幀以鏡頭運動帶到目前影像座標。線消失期間：
        - 新出現的線與記憶幾何重疊（max_line_handoff_px 內）時，把候選轉移過去（line_handoff）；
        - 仍有候選的線以記憶幾何繼續量測（partial_support、remembered），不建立新候選；
        超過 line_memory_seconds 或投影失敗即丟棄。回傳本幀要補上的記憶線。
        """
        now, limit = frame["timestamp_sec"], self.settings["max_line_handoff_px"]
        current = {line["track_id"] for line in usable}
        kept = {}
        for track, entry in self.remembered_lines.items():
            if track in current or now - entry["last_seen"] > self.memory_seconds:
                continue
            try:
                kept[track] = dict(entry, line=project_line_geometry(entry["line"], matrix))
            except (ValueError, KeyError):
                continue
        new = [line for line in usable if line["track_id"] not in self.remembered_lines]
        mapping = {}
        if limit > 0:
            for track, entry in kept.items():
                path = np.asarray(entry["line"]["centerline"], dtype=float)
                best = None
                for line in new:
                    distance = self._row_distance(path, np.asarray(line["centerline"], dtype=float))
                    if distance is not None and distance <= limit and (best is None or distance < best[0]):
                        best = (distance, line["track_id"])
                if best:
                    mapping[track] = best[1]
        for (object_id, track), state in list(self.states.items()):
            if track in mapping and (object_id, mapping[track]) not in self.states:
                self._diagnose_state((object_id, track), "line_handoff", action="transferred", new_line_id=mapping[track])
                if self.detailed_diagnostics and (object_id, track) in self._side_history:
                    self._side_history[(object_id, mapping[track])] = self._side_history.pop((object_id, track))
                state["line_handoff"] = True
                self.states[(object_id, mapping[track])] = self.states.pop((object_id, track))
        if mapping:
            self.diagnostics["line_handoffs"] = mapping
        for track in mapping:
            kept.pop(track)
        waiting = {key[1] for key in self.states}
        extra = [dict(entry["line"], source_frame=frame["index"], partial_support=True, remembered=True,
                      geometry_source="remembered", remembered_seconds=now - entry["last_seen"])
                 for track, entry in kept.items() if track in waiting]
        self.remembered_lines = kept
        self.remembered_lines.update({line["track_id"]: {"line": clone(line), "last_seen": now} for line in usable})
        self.diagnostics["remembered_lines"] = [line["track_id"] for line in extra]
        return extra

    def _origin_interruption(self, state, now):
        """起點容忍（4an）：本次中斷仍在 max_origin_interruption_sec 內則標記並回傳 True。"""
        limit = self.settings.get("max_origin_interruption_sec", 0.0)
        if limit <= 0:
            return False
        since = state.get("origin_interrupted_since")
        if since is None:
            state["origin_interrupted_since"] = since = now
        if now - since > limit:
            return False
        state["origin_interrupted"] = True
        return True

    # 候選結束原因中可以成立壓線的：退回原側，或車仍在線帶內時追蹤結束。車框跳動與投影失敗
    # （很可能換了目標或幾何無效）不算。
    STRADDLE_END_REASONS = ("returned_to_origin", "observation_gap", "line_unavailable", "crossing_timeout",
                            "low_confidence", "invalid_vehicle", "motion_unavailable", "discontinuous_input",
                            "finalize")

    def _end_straddle(self, key, reason):
        """4ao 壓線（C1）：armed 後進入線帶的候選結束時，停留達門檻則暫存壓線事件。"""
        state = self.states[key]
        if state["phase"] != "crossing" or reason not in self.STRADDLE_END_REASONS:
            return
        cfg, entry = self.settings, state["transition"] or state["crossing"]
        if state["band_first"] is None:
            return
        seconds = state["band_last"] - state["band_first"]
        if seconds < self.straddle_seconds or state["band_count"] < cfg["stable_observations"]:
            return
        episode = dict(entry_frame=entry["frame"]["index"], entry_timestamp_sec=entry["frame"]["timestamp_sec"],
                       end_frame=state["band_last_frame"], end_timestamp_sec=state["band_last"],
                       band_seconds=seconds, band_observations=state["band_count"], end_reason=reason)
        pending = self.pending_straddles.get(key[0])
        if pending:  # 同一車的連續壓線（含不同雙線編號）合併為一筆，等待時間由最後一次起算
            pending["event"]["evidence"]["straddle"]["episodes"].append(episode)
            pending["end"] = max(pending["end"], state["band_last"])
            return
        conditions = dict(stable_origin=not state.get("origin_interrupted", False),
                          stable_destination=False, complete_band_crossing=False,
                          continuous_tracks=True, geometry_verified=True,
                          uninterrupted_positions=not state.get("position_gap", False),
                          directly_observed_pair=not state.get("inferred_geometry", False),
                          transition_observed=state["transition"] is not None,
                          line_observed_at_footprint=not state.get("extrapolated", False),
                          same_line_track=not state.get("line_handoff", False),
                          camera_motion_compensated=not state.get("motion_gap", False))
        if cfg["require_direct_marking_evidence"]:
            conditions["direct_marking_evidence"] = self._direct_marking_evidence(state, entry["double_line"])
        if not cfg["emit_suspected"]:
            return
        evidence = {"before": state["before"], "transition": state["transition"], "after": None,
                    "direction": None, "reference_point_method": "bbox_inset_bottom_edge_proxy",
                    "parameters": cfg, "maneuver": "line_straddle", "straddle": {"episodes": [episode]}}
        event = self.build_event(entry["frame"], entry["vehicle"], entry["double_line"],
                                 status="suspected", conditions=conditions, evidence=evidence)
        event["behavior"] = "line_straddle"
        event["description"] = (self.label + "（壓線）：車框內縮底邊代理由一側進入雙線帶後"
                                + ("退回原側" if reason == "returned_to_origin" else "仍在線帶內即結束追蹤")
                                + "，未完整跨越，待覆核。")
        self.pending_straddles[key[0]] = {"event": event, "end": state["band_last"]}

    def _release_straddles(self, frame=None):
        """等待合併期滿（frame=None 表示片尾）的壓線事件依暫存順序輸出。"""
        released = []
        for key, pending in list(self.pending_straddles.items()):
            if frame is None or frame["timestamp_sec"] - pending["end"] >= self.straddle_merge:
                event = pending["event"]
                last = frame or self._current_frame
                if last is not None:
                    event.update(confirmation_frame=last["index"], confirmation_timestamp_sec=last["timestamp_sec"])
                released.append(event)
                del self.pending_straddles[key]
        return released

    def _direct_marking_evidence(self, state, line):
        """確認所需的直接觀測：推移（標線降頻）只對齊幾何，不能湊足觀測次數。

        - 線本身至少 min_line_observations 次模型直接觀測（舊紀錄退回 observed_frames）；
        - 越線前、越線後的樣本各至少一個落在直接觀測的幾何上；
        - 越線前後與線帶內的樣本都不能落在未對齊的推移幾何上（鏡頭運動失效時沿用的遮罩）。
        """
        count = line.get("direct_observed_frames", line.get("observed_frames", 0))
        before = [s.get("geometry", "direct") for s in state["before_path"]]
        after = [s.get("geometry", "direct") for s in state["after_path"]]
        transition = state.get("transition")
        middle = [transition["double_line"].get("geometry_source", "direct")] if transition else []
        return bool(count >= self.settings["min_line_observations"] and "direct" in before
                    and "direct" in after and "stale" not in before + after + middle)

    @staticmethod
    def _snapshot(frame, vehicle, line, position):
        """只保存引用：這些物件每幀新建、之後不再修改；產生事件時 build_event 會完整拷貝證據。

        起點側的快照每幀都會被覆蓋，若每次都深拷貝整條雙線幾何，會佔掉規則大半時間。
        """
        return {"frame": frame, "vehicle": vehicle, "double_line": line,
                "reference_point": position["point"], "side": position["side"]}

    def _evaluate_crossing(self, frame, vehicles, lines, motion_error=0, motion_gap=False):
        events = []
        now, cfg = frame["timestamp_sec"], self.settings
        armed_now = [] if self.endpoint_seconds > 0 else None  # 4ba：本幀剛建立穩定起點的候選
        self.dwell_pending = []
        for vehicle in vehicles:
            object_id = vehicle.get("object_id")
            if (not object_id or vehicle.get("class_name") not in
                    {"car", "truck", "bus", "motorcycle", "bicycle"} or
                    not isinstance(vehicle.get("confidence"), (int, float)) or
                    not math.isfinite(vehicle["confidence"]) or vehicle["confidence"] < cfg["min_vehicle_confidence"]):
                confidence = vehicle.get("confidence")
                tolerable = (object_id and vehicle.get("class_name") in {"car", "truck", "bus", "motorcycle", "bicycle"}
                             and isinstance(confidence, (int, float)) and math.isfinite(confidence)
                             and confidence >= cfg.get("origin_interruption_min_confidence", DOUBLE_LINE_DEFAULTS["origin_interruption_min_confidence"]))
                for key in [key for key in self.states if key[0] == object_id]:
                    if (tolerable and self.states[key]["phase"] in ("seeding", "armed")
                            and self._origin_interruption(self.states[key], now)):
                        continue  # 4an：建立起點時的短暫低信心不刪除候選，也不計入穩定次數
                    reason = ("low_confidence" if isinstance(confidence, (int, float))
                              and math.isfinite(confidence) and confidence < cfg["min_vehicle_confidence"] else "invalid_vehicle")
                    self._diagnose_state(key, reason, confidence=confidence if isinstance(confidence, (int, float)) and math.isfinite(confidence) else None)
                    del self.states[key]
                self.diagnostics["skipped_vehicles"].append({"object_id": object_id, "reason": "invalid_vehicle"})
                continue
            for line in lines:
                key = (object_id, line["track_id"])
                if line.get("remembered") and key not in self.states:
                    continue  # 記憶幾何只延續既有候選，不建立新的起點
                if self.detailed_diagnostics:
                    detail = {}
                    position = vehicle_line_position(vehicle, line, cfg, motion_error, diagnostics=detail)
                    if position is None:
                        self.diagnostics["details"]["position_failures"].append(
                            dict(object_id=object_id, line_id=line["track_id"], **detail))
                else:
                    position = vehicle_line_position(vehicle, line, cfg, motion_error)
                if position is None:
                    state = self.states.get(key)
                    # The observation-gap timeout still expires this state.
                    # Never refresh last_time or invent a missing ground sample.
                    if state and state["phase"] in ("armed", "crossing"):
                        state["position_gap"] = True
                        state.update(after_path=[], destination_since=None, destination_count=0)
                    else:
                        self._diagnose_state(key, "position_unavailable")
                        self.states.pop(key, None)
                    self.diagnostics["skipped_vehicles"].append({
                        "object_id": object_id, "line_id": line["track_id"],
                        "reason": "unsupported_footprint_or_invalid_box"})
                    continue
                side, point, box = position["side"], position["point"], position["box"]
                self._diagnose_position(key, frame, position)
                if self.dwell_seconds > 0:
                    self._band_dwell_sample(frame, vehicle, line, position)
                state = self.states.get(key)
                if state:
                    if cfg.get("confirm_short_position_gap") and state.get("position_gap"):
                        state["max_position_interval"] = max(state.get("max_position_interval", 0.), now - state["last_time"])
                    old = state["last_box"]
                    sizes = [box[2] - box[0], box[3] - box[1]]
                    old_sizes = [old[2] - old[0], old[3] - old[1]]
                    ratios = [max(a / max(b, 1e-6), b / a) for a, b in zip(sizes, old_sizes)]
                    if (max(ratios) > cfg["max_box_scale_ratio"] or
                            np.linalg.norm(np.asarray(point) - state["last_point"]) >
                            cfg["max_step_box_ratio"] * max(sizes)):
                        self._diagnose_state(key, "box_jump")
                        self.states.pop(key)
                        self.diagnostics["skipped_vehicles"].append({"object_id": object_id, "reason": "box_jump"})
                        state = None
                sample = {"time": now, "point": list(point),
                          "geometry": line.get("geometry_source", self._frame_geometry)}
                if "direction_side" in position:  # 4az：此樣本離線帶夠遠，可用於方向分類
                    sample["direction_clear"] = side != 0 and position["direction_side"] == side
                if state is None:
                    if side == 0:
                        continue  # Starting on a line cannot establish an origin side.
                    state = dict(phase="seeding", origin=side, origin_since=now,
                                 before_path=[], after_path=[], before=None, crossing=None,
                                 transition=None, destination_since=None, destination_count=0)
                    self.states[key] = state
                state.update(last_time=now, last_point=list(point), last_box=list(box))
                if position.get("extrapolated"):
                    state["extrapolated"] = True
                if motion_gap:
                    state["motion_gap"] = True
                if state["phase"] == "reported":
                    if side != state["destination"]:
                        state["rearm_since"] = now
                    elif now - state["rearm_since"] >= cfg["rearm_seconds"]:
                        self._diagnose_state(key, "rearmed", action="reset")
                        state.update(phase="seeding", origin=side, origin_since=now,
                                     before_path=[], after_path=[], crossing=None, transition=None, position_gap=False,
                                     origin_interrupted=False, origin_interrupted_since=None)
                    else:
                        continue
                    if state["phase"] == "reported":
                        continue
                if state["phase"] == "crossing" and now - state["crossing"]["frame"]["timestamp_sec"] > cfg["max_crossing_seconds"]:
                    self._diagnose_state(key, "crossing_timeout")
                    del self.states[key]
                    continue
                if side == state["origin"]:
                    # A touch-and-return cancels the candidate without emitting it.
                    if state["phase"] == "crossing":
                        self._diagnose_state(key, "returned_to_origin", action="reset")
                        state.update(phase="seeding", origin_since=now, before_path=[],
                                     after_path=[], crossing=None, transition=None, position_gap=False)
                    state["origin_interrupted_since"] = None
                    path = state["before_path"]
                    path.append(sample)
                    path[:] = [s for s in path if now - s["time"] <= max(cfg["heading_window_sec"], cfg["stable_seconds"])]
                    state["before"] = self._snapshot(frame, vehicle, line, position)
                    if len(path) >= cfg["stable_observations"] and now - state["origin_since"] >= cfg["stable_seconds"]:
                        if armed_now is not None and state["phase"] == "seeding" and not line.get("remembered"):
                            armed_now.append((key, vehicle, line, position["tangent"]))
                        state["phase"] = "armed"
                    continue
                if state["phase"] == "seeding":
                    if side == 0 and self._origin_interruption(state, now):
                        continue  # 4an：短暫落在線帶邊緣，不刪除起點也不計入穩定次數
                    self._diagnose_state(key, "origin_not_stable")
                    del self.states[key]
                    continue
                if state["phase"] == "armed":
                    state.update(phase="crossing", crossing=self._snapshot(frame, vehicle, line, position),
                                 after_path=[], destination_since=None, destination_count=0, transition=None,
                                 inferred_geometry=(state["before"]["double_line"].get("component_count") != 2 or
                                                    state["before"]["double_line"].get("partial_support", False)),
                                 band_count=0, band_first=None)
                if position.get("band_points"):  # 4ao 壓線只計車底點進入線帶本身（不含邊距）的觀測
                    if state["band_first"] is None:
                        state["band_first"] = now
                    state.update(band_last=now, band_last_frame=frame["index"], band_count=state["band_count"] + 1)
                state["inferred_geometry"] = state.get("inferred_geometry", False) or line.get("partial_support", False) or line.get("component_count") != 2
                if position["center_in_band"]:
                    state["transition"] = self._snapshot(frame, vehicle, line, position)
                if side == 0:
                    state.update(after_path=[], destination_since=None, destination_count=0)
                    continue
                if state["destination_since"] is None:
                    state["destination_since"] = now
                state["destination_count"] += 1
                state["after_path"].append(sample)
                if (state["destination_count"] < cfg["stable_observations"] or
                        now - state["destination_since"] < cfg["stable_seconds"]):
                    continue
                conditions = dict(stable_origin=not state.get("origin_interrupted", False),
                                  stable_destination=True, complete_band_crossing=True,
                                  continuous_tracks=True, geometry_verified=True,
                                  uninterrupted_positions=not state.get("position_gap", False),
                                  directly_observed_pair=not state.get("inferred_geometry", False),
                                  transition_observed=state["transition"] is not None,
                                  line_observed_at_footprint=not state.get("extrapolated", False),
                                  same_line_track=not state.get("line_handoff", False),
                                  camera_motion_compensated=not state.get("motion_gap", False))
                if cfg["require_direct_marking_evidence"]:
                    conditions["direct_marking_evidence"] = self._direct_marking_evidence(state, line)
                status = "confirmed" if all(conditions.values()) else "suspected"
                gap_basis = bool(cfg.get("confirm_short_position_gap") and not conditions["uninterrupted_positions"]
                    and 0 < state.get("max_position_interval", float('inf')) <= .2 + 1e-9
                    and all(v for k, v in conditions.items() if k != "uninterrupted_positions"))
                maneuver, direction = classify_maneuver(state["before_path"], state["after_path"], position["tangent"], cfg)
                crossing = state["transition"] or state["crossing"]
                evidence = {"before": state["before"], "transition": state["transition"],
                            "after": self._snapshot(frame, vehicle, line, position),
                            "direction": direction, "reference_point_method": "bbox_inset_bottom_edge_proxy",
                            "parameters": cfg, "maneuver": maneuver}
                if status == "confirmed" or gap_basis or cfg["emit_suspected"]:
                    event = self.build_event(crossing["frame"], crossing["vehicle"], crossing["double_line"],
                                             status=status, conditions=conditions, evidence=evidence)
                    if gap_basis:
                        # 不改寫缺口事實；此路徑只替代 uninterrupted_positions，其餘條件全部成立。
                        status = "confirmed"
                        event.update(status=status, status_label="確認違規", confirmation_basis=["short_position_gap"])
                        event["evidence"]["max_position_interval_sec"] = state["max_position_interval"]
                    event.update(behavior=maneuver, confirmation_frame=frame["index"], confirmation_timestamp_sec=now)
                    if vehicle.get("plate_text"):
                        event["evidence"]["vehicle"]["plate_text"] = vehicle["plate_text"]
                    if object_id in self.pending_straddles:  # 4ao：同一車先前的壓線併入這次完整跨越
                        event["evidence"]["merged_straddles"] = self.pending_straddles.pop(object_id)["event"]["evidence"]["straddle"]["episodes"]
                    action = {"lane_change": "，軌跡符合變換車道", "u_turn": "，軌跡符合迴轉",
                              "complete_crossing": "，行為方向未能分類"}[maneuver]
                    event["description"] = self.label + "：車框內縮底邊代理由雙線帶一側完整移至另一側" + action + (
                        "；跨線過程有觀測支持。" if status == "confirmed" else
                        "；短暫缺少有效車輛參考點，待覆核。" if not conditions["uninterrupted_positions"] else
                        "；雙線語意或遮擋補償證據待覆核。" if not conditions["directly_observed_pair"] else
                        "；車輛處的雙線被遮擋，以可見線段延伸判斷，待覆核。" if not conditions["line_observed_at_footprint"] else
                        "；期間雙線追蹤編號中斷，依位置銜接，待覆核。" if not conditions["same_line_track"] else
                        "；期間鏡頭運動估計短暫失敗，未經運動補償，待覆核。" if not conditions["camera_motion_compensated"] else
                        "；越線前後缺少直接觀測的雙線（只有推移估計），待覆核。" if not conditions.get("direct_marking_evidence", True) else
                        "；缺少線帶內觀測，待覆核。")
                    events.append(event)
                state.update(phase="reported", destination=side, rearm_since=now)
        if self.dwell_seconds > 0:
            reported = {event["object_id"] for event in events}
            for event in events:
                self.dwell_last[event["object_id"]] = now
            # 原規則同一幀已報這台車時不重複輸出（原規則在同一幀稍後才處理到這台車的情形）。
            events += [event for event in self.dwell_pending if event["object_id"] not in reported]
            self.dwell_pending = []
        if armed_now is not None:
            events += self._infer_endpoints(frame, vehicles, events, armed_now, motion_error)
        return events

    def _band_dwell_sample(self, frame, vehicle, line, position):
        """4bb：記錄這台車本幀相對同幀雙線的側別；同一幀有多條線時保留較明確的一個（方向樣本＞線帶＞其他）。"""
        center = np.asarray(line["centerline"], dtype=float)
        if float(np.linalg.norm(np.diff(center, axis=0), axis=1).sum()) < self.dwell_min_line:
            return  # 碎片線上的量測不採用
        now, side = frame["timestamp_sec"], position["side"]
        sample = {"time": now, "index": frame["index"], "side": side, "frame": frame, "vehicle": vehicle, "line": line,
                  "clear": side != 0 and position.get("direction_side") == side,
                  "band": side == 0 and position.get("band_points", 0) > 0,
                  "height": position["box"][3] - position["box"][1], "extrapolated": position.get("extrapolated", False)}
        series = self.dwell_series.setdefault(vehicle["object_id"], [])
        if series and series[-1]["index"] == frame["index"]:
            if (sample["clear"], sample["band"]) <= (series[-1]["clear"], series[-1]["band"]):
                return
            series.pop()
        series.append(sample)
        horizon = self.dwell_seconds + ENDPOINT_CLUSTER_SECONDS + 1.
        while series and now - series[0]["time"] > horizon:
            series.pop(0)
        event = self._band_dwell_check(vehicle["object_id"], series, frame)
        if event is not None:
            self.dwell_pending.append(event)

    def _band_dwell_check(self, object_id, series, frame):
        """4bb 壓線停留跨越：起點側方向樣本 → 線帶內觀測 → 終點側穩定（見 double_lines.RULE_DEFAULTS 說明）。"""
        now, cfg = frame["timestamp_sec"], self.settings
        needed = cfg["direction_min_samples"]
        floor = self.dwell_last.get(object_id, -math.inf)
        if now - floor <= self.dwell_seconds:
            return None
        samples = [s for s in series if s["time"] > floor]
        if not samples or samples[-1]["side"] == 0:
            return None
        destination = samples[-1]["side"]
        start = len(samples)
        while start and samples[start - 1]["side"] == destination:
            start -= 1
        tail, head = samples[start:], samples[:start]
        if (len(tail) < cfg["stable_observations"] or tail[-1]["time"] - tail[0]["time"] < cfg["stable_seconds"] - 1e-9
                or sum(s["clear"] for s in tail) < needed or tail[-1]["height"] < self.dwell_min_height):
            return None
        # 起點側方向樣本必須全在第一個線帶樣本之前：起點→線帶→終點的順序要乾淨；
        # 線帶與起點側交替（例如車停在線的遠端尖點附近、側別來回跳動）不視為跨越的起點。
        first_band = next((i for i, s in enumerate(head) if s["band"]), len(head))
        origin = [i for i, s in enumerate(head[:first_band]) if s["side"] == -destination and s["clear"]]
        if not origin:
            return None
        newest = head[origin[-1]]
        cluster = [head[i] for i in origin if newest["time"] - head[i]["time"] <= ENDPOINT_CLUSTER_SECONDS]
        band = [s for s in head[first_band:] if s["band"]]
        first_clear = next(s for s in tail if s["clear"])
        if (len(cluster) < needed or len(band) < self.dwell_min_band
                or first_clear["time"] - newest["time"] > self.dwell_seconds + 1e-9):
            return None
        if any(key[0] == object_id and state["phase"] == "crossing" and state["origin"] == -destination
               for key, state in self.states.items()):
            return None  # 原規則對這台車有同方向跨越中的候選，交給原規則
        used = cluster + band + tail
        line_ids = sorted({s["line"].get("track_id") for s in used}, key=str)
        conditions = dict(
            stable_origin=False, stable_destination=True, complete_band_crossing=True, continuous_tracks=True,
            geometry_verified=True, uninterrupted_positions=False,
            directly_observed_pair=all(s["line"].get("component_count") == 2 and not s["line"].get("partial_support", False)
                                       for s in used),
            transition_observed=True, line_observed_at_footprint=not any(s["extrapolated"] for s in used),
            same_line_track=len(line_ids) == 1, camera_motion_compensated=False)
        crossing = band[0]
        snapshot = lambda s: {"frame": s["frame"], "vehicle": s["vehicle"], "double_line": s["line"],
                              "side": s["side"]}
        evidence = {"before": snapshot(newest), "transition": snapshot(crossing), "after": snapshot(tail[-1]),
                    "direction": {"reason": "band_dwell_not_classified"},
                    "reference_point_method": "bbox_inset_bottom_edge_proxy", "parameters": cfg,
                    "maneuver": "complete_crossing",
                    "band_dwell": {"origin_frames": [cluster[0]["index"], newest["index"]],
                                   "band_observations": len(band),
                                   "dwell_sec": round(first_clear["time"] - newest["time"], 3),
                                   "line_track_ids": line_ids}}
        event = self.build_event(crossing["frame"], crossing["vehicle"], crossing["line"], status="suspected",
                                 conditions=conditions, evidence=evidence)
        event.update(behavior="complete_crossing", confirmation_frame=frame["index"], confirmation_timestamp_sec=now)
        vehicle = tail[-1]["vehicle"]
        if vehicle.get("plate_text"):
            event["evidence"]["vehicle"]["plate_text"] = vehicle["plate_text"]
        event["description"] = (self.label + "：車底代理點先在雙線一側，之後壓在線帶上一段時間，再於另一側穩定；"
                                "起點不夠穩定或過程中斷，以逐幀側別判斷，待覆核。")
        self.dwell_last[object_id] = now
        return event

    def _project_endpoint_history(self, matrix, error, motion_gap):
        """4ba：把保留的車框帶到目前路面座標；累積誤差以每幀配準誤差的平方和開根號估計。"""
        for object_id, samples in list(self.endpoint_history.items()):
            corners = [corner for s in samples for corner in
                       ([s["box"][0], s["box"][1]], [s["box"][2], s["box"][1]],
                        [s["box"][2], s["box"][3]], [s["box"][0], s["box"][3]])]
            try:
                projected = np.asarray(project_points(corners, matrix), dtype=float).reshape(-1, 4, 2)
            except ValueError:
                del self.endpoint_history[object_id]
                continue
            for sample, box in zip(samples, projected):
                sample["box"] = [*box.min(axis=0).tolist(), *box.max(axis=0).tolist()]
                sample["error2"] += error * error
                sample["motion_gap"] = sample["motion_gap"] or motion_gap

    def _infer_endpoints(self, frame, vehicles, events, armed_now, motion_error):
        """4ba 起點與終點推定（方案二）：剛建立穩定起點的候選，回頭找另一側的較早樣本。

        另一側：以目前這條線重新量測較早車框，至少 direction_min_samples 個落在方向邊距（4az）外、
        彼此相距 ENDPOINT_CLUSTER_SECONDS 內；終點側：候選 before_path 中至少同樣數量的方向邊距外樣本；
        兩段間隔不超過 endpoint_inference_seconds，中間可缺車框或量不到位置。兩端都量得到位置代表都落在
        同一條線的可見列範圍內，而線為 y 單調，起訖連線必定穿過該段線帶，不另做相交檢查。
        同一車在上一筆雙線事件（原規則或推定）之後的樣本才使用，事件後 window 秒內不再推定，避免重複。
        """
        now, cfg = frame["timestamp_sec"], self.settings
        window = self.endpoint_seconds + ENDPOINT_CLUSTER_SECONDS + max(cfg["heading_window_sec"], cfg["stable_seconds"])
        needed = cfg["direction_min_samples"]
        for event in events:
            self.endpoint_last[event["object_id"]] = now
        for object_id, samples in list(self.endpoint_history.items()):
            while samples and now - samples[0]["time"] > window:
                samples.pop(0)
            if not samples:
                del self.endpoint_history[object_id]
        inferred = []
        for key, vehicle, line, tangent in armed_now:
            object_id, state = key[0], self.states.get(key)
            floor = self.endpoint_last.get(object_id, -math.inf)
            if state is None or state["phase"] != "armed" or now - floor <= window or not cfg["emit_suspected"]:
                continue
            destination = [s for s in state["before_path"] if s.get("direction_clear")]
            box = state["last_box"]  # 本幀車框（遠方小車防護）
            if len(destination) < needed or box[3] - box[1] < self.endpoint_min_height:
                continue
            t_destination = min(s["time"] for s in destination)
            samples = [s for s in self.endpoint_history.get(object_id, ()) if floor < s["time"] < state["origin_since"]]
            origin, clear = -state["origin"], []
            for index, sample in enumerate(samples):
                position = vehicle_line_position({"bbox_xyxy": sample["box"]}, line, cfg,
                                                 motion_error + math.sqrt(sample["error2"]))
                if position is not None and position["side"] == origin and position.get("direction_side") == origin:
                    clear.append((index, position))
            if not clear:
                continue
            newest_index = clear[-1][0]
            newest = samples[newest_index]
            cluster = [(samples[i], p) for i, p in clear
                       if newest["time"] - samples[i]["time"] <= ENDPOINT_CLUSTER_SECONDS]
            gap = t_destination - newest["time"]
            if len(cluster) < needed or gap > self.endpoint_seconds + 1e-9:
                continue
            after = samples[newest_index + 1] if newest_index + 1 < len(samples) else newest
            origin_path = [{"time": s["time"], "point": p["point"], "direction_clear": True} for s, p in cluster]
            maneuver, direction = classify_maneuver(origin_path, destination, tangent, cfg)
            conditions = dict(
                stable_origin=False, stable_destination=True, complete_band_crossing=False,
                continuous_tracks=True, geometry_verified=True, uninterrupted_positions=False,
                directly_observed_pair=line.get("component_count") == 2 and not line.get("partial_support", False),
                transition_observed=False,
                line_observed_at_footprint=not (state.get("extrapolated") or any(p.get("extrapolated") for _, p in cluster)),
                same_line_track=not state.get("line_handoff", False),
                camera_motion_compensated=not (state.get("motion_gap") or any(s["motion_gap"] for s, _ in cluster)))
            evidence = {"before": None, "transition": None, "after": state["before"], "direction": direction,
                        "reference_point_method": "bbox_inset_bottom_edge_proxy", "parameters": cfg, "maneuver": maneuver,
                        "endpoint_inference": {
                            "origin_frames": [cluster[0][0]["frame"]["index"], newest["frame"]["index"]],
                            "origin_samples": len(cluster), "gap_sec": round(gap, 3),
                            "max_accumulated_motion_error_px": round(max(math.sqrt(s["error2"]) for s, _ in cluster), 3)}}
            event = self.build_event(after["frame"], after["vehicle"], line, status="suspected",
                                     conditions=conditions, evidence=evidence)
            event.update(behavior=maneuver, confirmation_frame=frame["index"], confirmation_timestamp_sec=now)
            if vehicle.get("plate_text"):
                event["evidence"]["vehicle"]["plate_text"] = vehicle["plate_text"]
            event["description"] = (self.label + "：車輛先在雙線一側、之後已在另一側穩定，中間缺少連續的車輛參考點，"
                                    "以起點與終點推定跨越，待覆核。")
            inferred.append(event)
            self.endpoint_last[object_id] = now
        stamp = dict(frame)
        for vehicle in vehicles:
            confidence, box = vehicle.get("confidence"), vehicle.get("bbox_xyxy")
            if (vehicle.get("object_id") and vehicle.get("class_name") in {"car", "truck", "bus", "motorcycle", "bicycle"}
                    and isinstance(confidence, (int, float)) and math.isfinite(confidence)
                    and confidence >= cfg["min_vehicle_confidence"] and isinstance(box, (list, tuple)) and len(box) == 4):
                self.endpoint_history.setdefault(vehicle["object_id"], []).append(
                    {"time": now, "frame": stamp, "vehicle": vehicle, "box": [float(b) for b in box],
                     "error2": 0., "motion_gap": False})
        return inferred

    def finalize(self):
        # Incomplete approaches never become events merely because the video ends
        # (4ao 壓線開啟時，片尾仍在線帶內且停留達門檻的候選例外，只能是疑似).
        if self.straddle_seconds > 0 and not self.detailed_diagnostics:
            for key in list(self.states):
                self._end_straddle(key, "finalize")
        if self.detailed_diagnostics and hasattr(self, "_diagnostic_frame"):
            self.diagnostics["details"]["state_changes"] = []
            self._diagnose_all_states("finalize")
            self.final_diagnostics = dict(frame=self._diagnostic_frame["index"],
                state_changes=self.diagnostics["details"]["state_changes"])
            self._side_history.clear()
        self.states.clear()
        self.line_memory.clear()
        self.remembered_lines.clear()
        self.endpoint_history.clear()
        self.endpoint_last.clear()
        self.dwell_series.clear()
        self.dwell_last.clear()
        self.previous_frame = None
        released = self._release_straddles() if self.pending_straddles else []
        self._current_frame = None
        return released

    def build_event(self, frame, vehicle, line, *, status, conditions, evidence=None):
        """Serialize an explicit decision with immutable evidence snapshots."""
        if status not in ("suspected", "confirmed") or not conditions or any(
                type(value) is not bool for value in conditions.values()):
            raise ValueError("Explicit status and boolean conditions are required")
        if status == "confirmed" and not all(conditions.values()):
            raise ValueError("Confirmed events require all supplied conditions")
        if line.get("class_name") != self.class_name:
            raise ValueError("Line class does not match the rule")
        return clone({
            "event_id": f"{self.context.source_id}:{self.rule_id}:{vehicle['object_id']}:"
                        f"{line['track_id']}:{frame['index']}",
            "rule_id": self.rule_id, "source_id": self.context.source_id,
            "status": status, "status_label": "確認違規" if status == "confirmed" else "疑似違規",
            "confirmation_scope": "configured_rule_conditions", "review_required": True,
            "behavior": self.rule_id,
            "description": self.label + ("，符合設定的跨線條件。" if status == "confirmed"
                                          else "，跨線證據尚待覆核。"),
            "frame": frame["index"], "timestamp_sec": frame["timestamp_sec"],
            "object_id": vehicle["object_id"], "vehicle_class": vehicle.get("class_name"),
            "conditions": conditions,
            "met_conditions": [key for key, value in conditions.items() if value],
            "missing_conditions": [key for key, value in conditions.items() if not value],
            "evidence": {**(evidence or {}), "vehicle": vehicle, "double_line": line},
        })


RULE_FACTORIES = {
    RedLightStopLineCrossingRule.rule_id: RedLightStopLineCrossingRule,
    "double_yellow_line_crossing": partial(DoubleLineCrossingRule, color="yellow"),
    "double_white_line_crossing": partial(DoubleLineCrossingRule, color="white"),
    "car_motorcycle_waiting_area": partial(AreaOccupancyRule, rule_id="car_motorcycle_waiting_area"),
    "crosswalk_stopping": partial(AreaOccupancyRule, rule_id="crosswalk_stopping"),
}


RED_LIGHT_CONFIG_OPTIONS = {"confirm_line_handoff": bool, "confirm_crosswalk_proxy": bool,
                            "confirm_after_motion_gap": bool, "deferred_confirmation_seconds": (int, float),
                            "confirm_signal_episode": bool}


def red_light_rule_options(path):
    """讀取闖紅燈規則設定（configs/red_light_rules.json，4av）。

    只回傳檔案中出現的選項，其餘沿用 RedLightStopLineCrossingRule 的建構子預設值；
    crossing_direction 等由各入口的命令列參數決定，不放在這個檔案。
    """
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError(f"{path}: version 必須是 1")
    unknown = sorted(key for key in data if key != "version" and not key.startswith("_")
                     and key not in RED_LIGHT_CONFIG_OPTIONS)
    if unknown:
        raise ValueError(f"{path}: 未知的闖紅燈設定：{', '.join(unknown)}")
    options = {}
    for key, kind in RED_LIGHT_CONFIG_OPTIONS.items():
        if key not in data:
            continue
        value = data[key]
        if kind is bool and type(value) is not bool:
            raise ValueError(f"{path}: {key} 必須是 true／false")
        if kind is not bool and (type(value) not in kind or not math.isfinite(value) or value <= 0):
            raise ValueError(f"{path}: {key} 必須是正數")
        options[key] = value
    return options


def create_rules(names, context, rule_options=None):
    names = list(names)
    unknown = sorted(set(names) - RULE_FACTORIES.keys() - {"all"})
    if unknown:
        raise ValueError("未知違規規則：" + ", ".join(unknown))
    names = list(RULE_FACTORIES) if "all" in names else list(dict.fromkeys(names))
    options = rule_options or {}
    return [RULE_FACTORIES[name](context, **options.get(name, {})) for name in names]
