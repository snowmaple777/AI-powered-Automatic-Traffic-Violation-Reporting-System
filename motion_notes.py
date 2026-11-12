"""選用的局部車道／闖紅燈動作備註；不改動規則或原事件欄位。"""
from collections import deque
from copy import deepcopy
import json
import math
from pathlib import Path
import sqlite3

import numpy as np

from core.geometry import project_points
from core.motion import homography, road_motion
from core.trajectory import TrackHistory
from local_lanes import LocalLanes, local_lane
from observation_schema import marking_evidence
from violation_engine import RedLightStopLineCrossingRule


RED_RULE = "red_light_stop_line_crossing"
LABELS = {"straight": "直行", "left_turn": "左轉", "right_turn": "右轉", "unknown": "無法判定"}
LOOK_BEFORE = 1.5
LOOK_AFTER = 3.0


def add_motion_arguments(parser, *, offline=False):
    parser.add_argument("--motion-notes", action="store_true",
                        help="實驗：局部車道與闖紅燈動作備註（預設關）；最多觀察事件後 3 秒")
    if offline:
        parser.add_argument("--motion-mask-dir", type=Path,
                            help="逐幀標線遮罩資料夾；批次則是含各影片/masks 的根目錄，預設使用來源旁 masks")


def read_motion_mask(record, directory):
    """只讀模型直接觀測；其餘幀由同一個 LocalLanes 推移，線上／離線一致。"""
    if not marking_evidence(record)["direct"]:
        return None
    from tools.real_replay import read_mask
    return read_mask(Path(directory) / f'{record["frame"]["index"]:06d}.png')


def write_motion_summary(path, events):
    from violation_visualization import format_event_summary
    Path(path).write_text("\n".join(format_event_summary(e) for e in events if e.get("motion_note")), encoding="utf-8")


def _box(vehicle):
    box = vehicle.get("rider_group", {}).get("vehicle_bbox_xyxy") or vehicle.get("bbox_xyxy")
    if (not isinstance(box, (list, tuple)) or len(box) != 4
            or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in box)
            or box[2] <= box[0] or box[3] <= box[1]):
        return None
    return box


def _angle(a, b):
    return math.degrees(math.atan2(a[0]*b[1] - a[1]*b[0], float(np.dot(a, b))))


def classify_path(points, times, event_time, lane, vehicle_width):
    """已補償至同一影像座標的接地軌跡；角度是影像估計，不是世界 yaw。

    至少觀察越線後 1 秒；轉彎需出發方向對齊車道、持續偏轉與離開原走廊。
    變換車道後回到平行、低速、反向、只有單一橫移向量均不硬判轉彎。
    """
    p, t = np.asarray(points, float), np.asarray(times, float)
    evidence = dict(samples=len(p), observed_before_seconds=float(event_time-t[0]),
                    observed_after_seconds=float(t[-1]-event_time))
    if len(p) < 9 or event_time-t[0] < .4 or t[-1]-event_time < 1.:
        return "unknown", "insufficient_pre_or_post_track", evidence
    pre = p[t <= event_time]
    if len(pre) < 3:
        return "unknown", "insufficient_pre_track", evidence
    # 直行先看整段軌跡的共線性，而非短尾段角度：遠處框抖動會放大角度。
    # 地面單應投影保留直線；不把影像角度當世界角度。需足夠位移及越線後前進。
    centered = p-p.mean(0)
    _, _, axes = np.linalg.svd(centered, full_matrices=False)
    principal = axes[0]
    if np.dot(principal, p[-1]-p[0]) < 0:
        principal = -principal
    progress = centered @ principal
    residual = np.abs(centered @ np.array([-principal[1], principal[0]]))
    span = float(progress[-1]-progress[0])
    fit_error = float(np.percentile(residual, 95))
    event_pos = int(np.flatnonzero(t <= event_time)[-1])
    aligned = True
    if lane:
        lane_axis = np.subtract(lane["axis"][1], lane["axis"][0])
        aligned = abs(_angle(lane_axis, principal)) <= 25
    # 反覆折返及中途換道後回正不符合整段共線；微小晃動容忍度隨車框尺度限制。
    monotonic = np.min(progress-np.maximum.accumulate(progress)) >= -vehicle_width*.15
    evidence.update(straight_fit_error_px=round(fit_error, 3), travel_px=round(span, 3))
    if (aligned and monotonic and span >= max(8., vehicle_width*.6)
            and progress[-1]-progress[event_pos] >= vehicle_width*.3
            and fit_error <= min(vehicle_width*.1, span*.06)):
        return "straight", "collinear_compensated_path", evidence
    # 首尾小組平均減少框抖動；尾段需持續 0.8 秒且兩半方向一致。
    incoming = pre[-min(3, len(pre)//2):].mean(0) - pre[:min(3, len(pre)//2)].mean(0)
    tail = p[t >= t[-1] - .8 - 1e-6]
    if len(tail) < 5:
        return "unknown", "insufficient_tail", evidence
    mid = len(tail)//2
    a, b = tail[mid]-tail[0], tail[-1]-tail[mid]
    outgoing = tail[-1]-tail[0]
    threshold = max(4., vehicle_width * .15)
    if min(np.linalg.norm(incoming), np.linalg.norm(outgoing)) < threshold:
        return "unknown", "low_motion", evidence
    if min(np.linalg.norm(a), np.linalg.norm(b)) < threshold * .35 or abs(_angle(a, b)) > 25:
        return "unknown", "unstable_tail", evidence
    axis = np.asarray(lane["axis"][1])-lane["axis"][0] if lane else incoming.copy()
    axis /= max(float(np.linalg.norm(axis)), 1e-9)
    incoming_angle, turn = _angle(axis, incoming), _angle(incoming, outgoing)
    # 相對初始走廊的橫向偏移；方向以已知向遠方的進入軌跡為準。
    normal = np.array([-axis[1], axis[0]])
    unit = lane["width_px"] if lane else vehicle_width
    lateral = (p-pre[-1]) @ normal / unit
    offset, excursion = float(lateral[-1]), float(np.max(np.abs(lateral)))
    evidence.update(incoming_angle_deg=round(incoming_angle, 3), turn_angle_deg=round(turn, 3),
                    lateral_offset=round(offset, 3), max_lateral_excursion=round(excursion, 3),
                    lateral_unit="lane_width" if lane else "vehicle_width", angle_space="ego_compensated_image",
                    lane_support=bool(lane))
    if abs(incoming_angle) > 25:
        return "unknown", "entry_not_aligned_or_toward_camera", evidence
    if abs(turn) <= 20 and excursion <= (.3 if lane else .4):
        return "straight", "aligned_entry_and_exit", evidence
    if 60 <= abs(turn) <= 125 and abs(offset) >= (.65 if lane else 1.5) and turn * offset > 0:
        return ("left_turn" if turn < 0 else "right_turn"), "sustained_heading_change", evidence
    return "unknown", "lane_change_curve_or_incomplete_turn", evidence


class MotionNotes:
    """三個入口共用的註記器。SQLite 保存精簡歷史以支援引擎片尾才釋出的事件。

    關閉時不建庫、不延遲、不改事件。開啟時維持 FIFO，非紅燈事件亦可能等待前面的紅燈備註。
    不處理 merged_events，不把推移標線計成直接證據；觀測 schema 不變，診斷寫獨立 sidecar。
    """
    def __init__(self, enabled=False, diagnostics_path=None):
        self.enabled = enabled
        self.db = None
        self.stream = None
        self.pending = deque()
        self.now = self.index = None
        self.lanes = LocalLanes()
        self.history = TrackHistory(.6)
        self.diagnostic = {}
        if enabled:
            self.db = sqlite3.connect("")
            self.db.execute("PRAGMA cache_size=-2048")
            self.db.execute("CREATE TABLE frames (i INTEGER PRIMARY KEY, t REAL, body TEXT)")
            self.db.execute("CREATE INDEX times ON frames(t)")
            if diagnostics_path:
                self.stream = Path(diagnostics_path).open("w", encoding="utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.stream:
            self.stream.close()
        if self.db:
            self.db.close()

    def observe(self, record, mask=None):
        if not self.enabled:
            return
        f = record["frame"]
        now, index = f["timestamp_sec"], f["index"]
        if self.now is not None and (now <= self.now or index <= self.index):
            raise ValueError("動作備註需要嚴格遞增的幀與時間；新影片請建立新實例")
        continuous = self.index is not None and index == self.index + 1 and now-self.now <= .3
        matrix = homography(road_motion(record), max_error_px=5.)
        self.now, self.index = now, index
        lane_info = self.lanes.update(record, mask, matrix, continuous)
        if continuous and matrix is not None:
            self.history.project(lambda x, y: project_points([[x, y]], matrix)[0])
        else:
            self.history.tracks.clear()
        obs = record.get("observations", {})
        vehicles = obs.get("rule_vehicles", obs.get("vehicles", []))
        self.history.update(now, index, [v for v in vehicles if v.get("source_frame") == index], _box)
        fresh = {}
        for v in vehicles:
            oid, box = v.get("object_id"), _box(v)
            if not oid or box is None or v.get("source_frame") != index:
                continue
            # 沿用紅燈規則同一接地遮擋檢查，避免另寫一套近似判準。
            occluded = RedLightStopLineCrossingRule._contact_occluded(box, oid, vehicles)
            lane = None if occluded else local_lane(self.lanes.boundaries, box, f["width"], f["height"])
            if lane:
                lane.update(source_frame=lane_info["source_frame"], geometry_source=lane_info["geometry_source"])
            point = [(box[0]+box[2])/2, box[3]]
            d = self.history.displacement(oid, now, .6)
            fresh[oid] = dict(object_id=oid, box=box, point=point, lane=lane,
                              contact_occluded=occluded,
                              motion_vector=([[point[0]-d["dx"], point[1]-d["dy"]], point] if d and not occluded else None))
        self.diagnostic = dict(frame=index, timestamp_sec=now, **lane_info, vehicles=list(fresh.values()))
        if self.stream:
            self.stream.write(json.dumps(self.diagnostic, ensure_ascii=False)+"\n")
        body = dict(matrix=matrix.tolist() if matrix is not None and continuous else None, vehicles=fresh)
        self.db.execute("INSERT INTO frames VALUES (?,?,?)", (index, now, json.dumps(body)))

    def submit(self, events=(), *, final=False):
        if not self.enabled:
            return list(events)
        self.pending.extend(deepcopy(list(events)))
        output = []
        while self.pending:
            e = self.pending[0]
            if (not final and e.get("rule_id") == RED_RULE
                    and self.now < e["timestamp_sec"] + LOOK_AFTER - 1e-9):
                break
            self.pending.popleft()
            if e.get("rule_id") == RED_RULE:
                e["motion_note"] = self._note(e)
            output.append(e)
        return output

    def _note(self, event):
        time, oid = event["timestamp_sec"], event.get("object_id")
        note = dict(version=1, maneuver="unknown", text="行進動作：無法判定", reason="missing_track",
                    method="local_lane_and_ego_compensated_track", estimated=True,
                    window_before_seconds=LOOK_BEFORE, window_after_seconds=LOOK_AFTER,
                    affects_violation=False, evidence={})
        def fail(reason):
            note["reason"] = reason
            explanations = {"no_fresh_event_vehicle": "事件當下沒有新車框", "track_gap": "追蹤中斷",
                            "track_jump": "車框跳動", "motion_gap_or_frame_gap": "鏡頭運動或影格不連續",
                            "contact_occluded": "接地位置被遮擋"}
            note["text"] = "行進動作：無法判定（" + explanations.get(reason, reason) + "）"
            return note
        rows = [(i, t, json.loads(body)) for i, t, body in self.db.execute(
            "SELECT i,t,body FROM frames WHERE t>=? AND t<=? ORDER BY i",
            (time-LOOK_BEFORE-1e-9, time+LOOK_AFTER+1e-9))]
        seen = [(i, t, body["vehicles"][oid]) for i, t, body in rows if oid in body["vehicles"]]
        if not seen:
            return note
        at_event = [r for r in seen if r[0] == event["frame"]]
        if not at_event:
            return fail("no_fresh_event_vehicle")
        if any(v.get("contact_occluded") for _, _, v in seen):
            return fail("contact_occluded")
        anchor = next((r for r in reversed(seen) if time-.5 <= r[1] <= time and r[2]["lane"]), None)
        note["evidence"].update(lane_frame=anchor[0] if anchor else None, lane_support=anchor is not None)
        if anchor is None:
            note["method"] = "ego_compensated_track_only"
        # 不跨越 ID 的中斷，事件窗口中有跳框也不以其他車替補。
        for previous, current in zip(seen, seen[1:]):
            if current[1]-previous[1] > .3+1e-9:
                return fail("track_gap")
            a, b = previous[2]["box"], current[2]["box"]
            shift = math.hypot((a[0]+a[2]-b[0]-b[2])/2, (a[1]+a[3]-b[1]-b[3])/2)
            if shift > .5*math.hypot(a[2]-a[0], a[3]-a[1]):
                return fail("track_jump")
        # 裁到目標可見區間：離開畫面後的鏡頭失效不否定已觀察的完整動作。
        rows = [r for r in rows if seen[0][0] <= r[0] <= seen[-1][0]]
        transforms, cumulative = {}, np.eye(3)
        for pos in range(len(rows)-1, -1, -1):
            i, _, body = rows[pos]
            transforms[i] = cumulative.copy()
            if pos:
                if i != rows[pos-1][0]+1 or body["matrix"] is None:
                    return fail("motion_gap_or_frame_gap")
                cumulative = cumulative @ np.asarray(body["matrix"])
        try:
            points = [project_points([v["point"]], transforms[i])[0] for i, _, v in seen]
            lane = deepcopy(anchor[2]["lane"]) if anchor else None
            if lane:
                lane["polygon"] = project_points(lane["polygon"], transforms[anchor[0]])
                lane["axis"] = project_points(lane["axis"], transforms[anchor[0]])
                lane["width_px"] = float(np.linalg.norm(np.subtract(lane["polygon"][1], lane["polygon"][0])))
            box = at_event[0][2]["box"]
            endpoints = project_points([[box[0], box[3]], [box[2], box[3]]], transforms[event["frame"]])
            width = float(np.linalg.norm(np.subtract(*endpoints)))
            if (lane is not None and lane["width_px"] < 5) or width < 2:
                return fail("projection_scale_invalid")
            maneuver, reason, evidence = classify_path(points, [r[1] for r in seen], time, lane, width)
        except (ValueError, OverflowError, FloatingPointError):
            return fail("projection_invalid")
        note.update(maneuver=maneuver, reason=reason,
                    text="行進動作：" + LABELS[maneuver] + (("（車道與軌跡推估，待覆核）" if lane else
                                                          "（僅軌跡推估，待覆核）") if maneuver != "unknown" else ""))
        note["evidence"].update(evidence, local_lane=lane, reference_frame=seen[-1][0],
                                sample_frames=[r[0] for r in seen], trajectory=points)
        return note
