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


def _track_break(previous, current):
    if current[1]-previous[1] > .3+1e-9:
        return "track_gap"
    a, b = previous[2]["box"], current[2]["box"]
    shift = math.hypot((a[0]+a[2]-b[0]-b[2])/2, (a[1]+a[3]-b[1]-b[3])/2)
    if shift > .5*math.hypot(a[2]-a[0], a[3]-a[1]):
        return "track_jump"
    return None


def event_segment(rows, oid, event_frame, *, compensate):
    """取包含事件的連續片段；不跨越缺幀、遮擋、ID 中斷或無效鏡頭矩陣。

    無效 H 僅切斷到達該幀的連結；可以從該幀開始新的片段，不以單位矩陣補洞。
    raw 片段不要求有效 H，只能描述畫面位移。
    """
    segments, current, previous_vehicle = [], [], None
    issues = []
    for row in rows:
        i, t, body = row
        vehicle = body["vehicles"].get(oid)
        reasons = []
        if current:
            if i != current[-1][0]+1 or t-current[-1][1] > .3+1e-9:
                reasons.append("frame_gap")
            if compensate and body["matrix"] is None:
                reasons.append("motion_gap")
        if vehicle and vehicle.get("contact_occluded"):
            reasons.append("contact_occluded")
        if vehicle and previous_vehicle:
            reason = _track_break(previous_vehicle, (i, t, vehicle))
            if reason:
                reasons.append(reason)
        if reasons:
            issues.extend(dict(frame=i, reason=r) for r in reasons)
            if current:
                segments.append(current)
            current, previous_vehicle = [], None
        if vehicle and vehicle.get("contact_occluded"):
            continue
        current.append(row)
        if vehicle:
            previous_vehicle = (i, t, vehicle)
    if current:
        segments.append(current)
    chosen = next((s for s in segments if any(r[0] == event_frame for r in s)), [])
    seen = [(i, t, body["vehicles"][oid]) for i, t, body in chosen if oid in body["vehicles"]]
    if not any(r[0] == event_frame for r in seen):
        return [], [], issues
    chosen = [r for r in chosen if seen[0][0] <= r[0] <= seen[-1][0]]
    return chosen, seen, issues


def _interval(seen):
    if not seen:
        return None
    return dict(start_frame=seen[0][0], end_frame=seen[-1][0], start_sec=seen[0][1],
                end_sec=seen[-1][1], samples=len(seen))


def _smooth(points, times):
    """固定秒數的局部中位數去除框抖動，不增加新觀測、不外插。"""
    p, t = np.asarray(points, float), np.asarray(times, float)
    return np.asarray([np.median(p[np.abs(t-now) <= .1+1e-9], axis=0) for now in t])


def image_movement(seen, event_time):
    """即使補償失效也可描述『畫面』移動，絕不據此填車輛左／右轉。"""
    post = [r for r in seen if r[1] >= event_time-1e-9]
    if len(post) < 4 or post[-1][1]-post[0][1] < .2-1e-9:
        return dict(kind="unavailable", text="", reason="short_image_track")
    p = _smooth([r[2]["point"] for r in post], [r[1] for r in post])
    width = float(np.median([r[2]["box"][2]-r[2]["box"][0] for r in post]))
    # 時間分箱後再比較，避免高 FPS 小抖動被當作大量反向證據。
    bins = np.floor((np.asarray([r[1] for r in post])-post[0][1])/.2+1e-6).astype(int)
    centers = np.asarray([np.median(p[bins == k], axis=0) for k in sorted(set(bins))])
    delta = p[-1]-p[0]
    def axis_direction(axis, negative, positive):
        if abs(delta[axis]) < max(4., width*.15):
            return "unclear"
        steps = np.diff(centers[:, axis])
        steps = steps[np.abs(steps) >= max(.5, width*.01)]
        if len(steps) and np.mean(steps*delta[axis] > 0) >= .7:
            return negative if delta[axis] < 0 else positive
        return "unclear"
    horizontal = axis_direction(0, "left", "right")
    vertical = axis_direction(1, "up", "down")
    words = {"left": "左", "right": "右", "up": "上", "down": "下", "unclear": ""}
    direction = words[horizontal]+words[vertical]
    if direction:
        text = "越線後在畫面中向"+direction+"方移動（含鏡頭影響，不等於車輛轉向）"
    elif np.max(np.linalg.norm(p-p[0], axis=1)) < max(4., width*.15):
        text = "越線後在畫面中的位移不明顯（不能據此判定車輛靜止）"
    else:
        return dict(kind="unavailable", text="", reason="unstable_image_direction")
    return dict(kind="image_motion", text=text, horizontal=horizontal, vertical=vertical,
                dx_px=round(float(delta[0]), 3), dy_px=round(float(delta[1]), 3),
                includes_camera_motion=True, interval=_interval(post))


def compensated_path(rows, seen, event_frame):
    """以事件幀為共同參考，縮短投影鏈；局部縮放／異向拉伸只作拒絕與診斷。

    不把矩陣 invertible 當成道路幾何可靠。檢查每個接地點的局部 Jacobian，
    避免越過投影地平線或極端放大後還產生方向結論。
    """
    pos = next(k for k, r in enumerate(rows) if r[0] == event_frame)
    transforms = {event_frame: np.eye(3)}
    for k in range(pos-1, -1, -1):
        transforms[rows[k][0]] = transforms[rows[k+1][0]] @ np.asarray(rows[k+1][2]["matrix"])
    for k in range(pos+1, len(rows)):
        inv = np.linalg.inv(np.asarray(rows[k][2]["matrix"]))
        transforms[rows[k][0]] = transforms[rows[k-1][0]] @ inv
    points, scales, anisotropy = [], [], []
    for i, _, v in seen:
        matrix = transforms[i]
        if abs(matrix[2, 2]) > 1e-9:
            matrix = matrix/matrix[2, 2]
        transforms[i] = matrix
        x, y = v["point"]
        sample = np.asarray(project_points([[x, y], [x+1, y], [x, y+1]], matrix))
        singular = np.linalg.svd(np.column_stack((sample[1]-sample[0], sample[2]-sample[0])), compute_uv=False)
        if singular[-1] <= 1e-9:
            raise ValueError("projection_scale_invalid")
        scales.extend(singular.tolist())
        anisotropy.append(float(singular[0]/singular[-1]))
        points.append(sample[0].tolist())
    quality = dict(reference_frame=event_frame, min_scale=min(scales), max_scale=max(scales),
                   max_anisotropy=max(anisotropy), projection_valid=True)
    quality["classification_eligible"] = .2 <= min(scales) and max(scales) <= 5 and max(anisotropy) <= 6
    return points, transforms, quality


def tentative_path(points, times, event_time, width):
    """只產生額外疑似欄位，不放寬 maneuver 的正式分類門檻。"""
    p, t = np.asarray(points, float), np.asarray(times, float)
    pre, post = p[t <= event_time+1e-9], p[t >= event_time-1e-9]
    if len(pre) < 3 or len(post) < 4 or event_time-t[0] < .2 or t[-1]-event_time < .25:
        return None
    incoming = np.median(pre[-3:], axis=0)-np.median(pre[:3], axis=0)
    # 短片尾僅描述目前已看到的直線路徑，不能保證後來不轉彎。
    centered = p-p.mean(0)
    _, _, axes = np.linalg.svd(centered, full_matrices=False)
    direction = axes[0]
    if np.dot(p[-1]-p[0], direction) < 0:
        direction = -direction
    progress = centered @ direction
    span = float(progress[-1]-progress[0])
    residual = np.abs(centered @ np.array([-direction[1], direction[0]]))
    if (span >= max(8., width*.5) and np.linalg.norm(post[-1]-post[0]) >= width*.2
            and np.min(progress-np.maximum.accumulate(progress)) >= -width*.15
            and np.percentile(residual, 95) <= min(width*.12, span*.08)):
        return dict(maneuver="straight", reason="limited_linear_path",
                    text="觀察區間疑似直行（後續動作未定，待覆核）")
    # 起步前的微小抖動不能當進入方向來推斷左／右轉。
    if np.linalg.norm(incoming) < max(6., width*.4) or t[-1]-event_time < .8:
        return None
    tail = p[t >= t[-1]-.8-1e-9]
    if len(tail) < 5:
        return None
    mid = len(tail)//2
    a, b = tail[mid]-tail[0], tail[-1]-tail[mid]
    if min(np.linalg.norm(a), np.linalg.norm(b)) < max(2., width*.1) or abs(_angle(a, b)) > 25:
        return None
    turn = _angle(incoming, tail[-1]-tail[0])
    normal = np.array([-incoming[1], incoming[0]])/np.linalg.norm(incoming)
    offset = float(np.dot(p[-1]-pre[-1], normal)/width)
    if 45 <= abs(turn) <= 125 and abs(offset) >= .5 and turn*offset > 0:
        maneuver = "left_turn" if turn < 0 else "right_turn"
        return dict(maneuver=maneuver, reason="partial_turn_evidence", angle_deg=round(turn, 3),
                    text="疑似"+LABELS[maneuver]+"（尚不能排除變換車道或彎道，待覆核）")
    return None


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
        note = dict(version=2, maneuver="unknown", level="unknown", tentative_maneuver=None,
                    text="行進動作：無法判定", reason="missing_track",
                    method="image_motion_only", estimated=True,
                    window_before_seconds=LOOK_BEFORE, window_after_seconds=LOOK_AFTER,
                    affects_violation=False, evidence={}, quality={}, observation={})
        rows = [(i, t, json.loads(body)) for i, t, body in self.db.execute(
            "SELECT i,t,body FROM frames WHERE t>=? AND t<=? ORDER BY i",
            (time-LOOK_BEFORE-1e-9, time+LOOK_AFTER+1e-9))]
        event_row = next((r for r in rows if r[0] == event["frame"]), None)
        vehicle = event_row[2]["vehicles"].get(oid) if event_row else None
        if vehicle is None:
            note["reason"] = "no_fresh_event_vehicle"
            note["text"] = "行進動作：無法判定（事件當下沒有新車框）"
            return note
        if vehicle.get("contact_occluded"):
            note["reason"] = "contact_occluded"
            note["text"] = "行進動作：無法判定（事件接地位置被遮擋）"
            return note
        raw_rows, raw_seen, raw_issues = event_segment(rows, oid, event["frame"], compensate=False)
        selected, seen, issues = event_segment(raw_rows, oid, event["frame"], compensate=True)
        quality = dict(raw_interval=_interval(raw_seen), selected_interval=_interval(seen),
                       issues=raw_issues+issues, projection_valid=False, classification_eligible=False,
                       unavailable_motion_frames=[i for i, _, body in raw_rows if body["matrix"] is None],
                       missing_before_seconds=max(0., LOOK_BEFORE-(time-rows[0][1])),
                       missing_after_seconds=max(0., LOOK_AFTER-(rows[-1][1]-time)))
        note["quality"] = quality
        note["observation"] = image_movement(raw_seen, time)
        note["evidence"]["raw_sample_frames"] = [r[0] for r in raw_seen]
        note["evidence"]["raw_trajectory"] = [r[2]["point"] for r in raw_seen]
        lane = None
        candidate = None
        if seen:
            times = [r[1] for r in seen]
            quality.update(selected_before_seconds=time-times[0], selected_after_seconds=times[-1]-time,
                           trimmed_before=seen[0][0] != raw_seen[0][0],
                           trimmed_after=seen[-1][0] != raw_seen[-1][0])
            anchor = next((r for r in reversed(seen) if time-.5 <= r[1] <= time and r[2]["lane"]), None)
            note["evidence"].update(lane_frame=anchor[0] if anchor else None, lane_support=anchor is not None,
                                    sample_frames=[r[0] for r in seen], reference_frame=event["frame"])
            try:
                points, transforms, projection = compensated_path(selected, seen, event["frame"])
                quality.update(projection)
                note["evidence"]["trajectory"] = points
                if not quality["classification_eligible"]:
                    note["reason"] = "projection_distortion"
                else:
                    lane = deepcopy(anchor[2]["lane"]) if anchor else None
                    if lane:
                        lane["polygon"] = project_points(lane["polygon"], transforms[anchor[0]])
                        lane["axis"] = project_points(lane["axis"], transforms[anchor[0]])
                        lane["width_px"] = float(np.linalg.norm(np.subtract(lane["polygon"][1], lane["polygon"][0])))
                        if lane["width_px"] < 5:
                            lane = None
                    width = vehicle["box"][2]-vehicle["box"][0]  # 參考幀即事件幀，不必另投影車寬。
                    smooth = _smooth(points, times)
                    maneuver, reason, evidence = classify_path(smooth, times, time, lane, width)
                    pre = smooth[np.asarray(times) <= time+1e-9]
                    entry_span = float(np.max(np.linalg.norm(pre-pre[0], axis=1)))
                    quality["entry_direction_available"] = entry_span >= max(4., width*.15)
                    if maneuver != "unknown" and not quality["entry_direction_available"]:
                        maneuver, reason = "unknown", "entry_direction_unavailable"
                    note["method"] = "local_lane_and_ego_compensated_track" if lane else "ego_compensated_track_only"
                    note["reason"] = reason
                    note["evidence"].update(evidence, smoothed_trajectory=smooth.tolist(), local_lane=lane,
                                            smoothing_radius_seconds=.1)
                    # 未跨缺口，但事件後被缺口截短時，不能把短片段結果說成完整動作。
                    future_cut = quality["trimmed_after"] or any(
                        q["frame"] > event["frame"] for q in raw_issues)
                    if maneuver != "unknown" and not future_cut:
                        note.update(maneuver=maneuver, level="classified",
                                    text="行進動作："+LABELS[maneuver]+("（車道與軌跡推估，待覆核）" if lane else "（僅軌跡推估，待覆核）"))
                    else:
                        if quality["entry_direction_available"]:
                            candidate = tentative_path(smooth, times, time, width)
                        if maneuver != "unknown" and future_cut:
                            candidate = dict(maneuver=maneuver, reason="truncated_valid_track",
                                             text="有效片段疑似"+LABELS[maneuver]+"（後續追蹤中斷，待覆核）")
            except (ValueError, OverflowError, FloatingPointError, np.linalg.LinAlgError):
                note["reason"] = "projection_invalid"
                quality.update(projection_valid=False, classification_eligible=False)
        if note["level"] != "classified":
            if candidate:
                note.update(level="tentative", tentative_maneuver=candidate["maneuver"], text=candidate["text"])
                note["evidence"]["tentative"] = candidate
            elif note["observation"].get("kind") == "image_motion":
                note.update(level="observed", text="行進動作未定；"+note["observation"]["text"])
            else:
                note["text"] = "行進動作：無法判定（有效軌跡不足或不穩定）"
        return note
