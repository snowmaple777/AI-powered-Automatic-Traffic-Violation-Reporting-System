"""合併後事件的同追蹤車牌補充；原始事件證據永不改寫。"""
from collections import deque
from copy import deepcopy
import math
import re
import sqlite3
import json

DEFAULT_WINDOW_SECONDS = 0.0
DEFAULT_TRACK_GAP_SECONDS = 0.3
DEFAULT_MIN_CONFIDENCE = 0.8
DEFAULT_MAX_CENTER_SHIFT = 0.5
DEFAULT_MIN_IOU = 0.1


def normalize_plate(text):
    return re.sub(r"[^A-Z0-9]", "", str(text or "").upper()) or None


def event_plate_text(event, *, at_event=False):
    """新欄位即使 text=null 也不退回別的車牌；舊事件沿用原證據。"""
    if not at_event and isinstance(event.get("plate"), dict):
        return event["plate"].get("text")
    return ((event.get("evidence") or {}).get("vehicle") or {}).get("plate_text")


def window_seconds(value):
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError("plate_backfill_seconds 必須是有限的非負秒數")
    return value


def add_plate_arguments(parser):
    parser.add_argument("--plate-backfill-seconds", type=window_seconds, default=DEFAULT_WINDOW_SECONDS,
                        help="事件後同追蹤車牌補充視窗；0 停用（預設）")


def _valid_box(box):
    return (isinstance(box, (list, tuple)) and len(box) == 4
            and all(isinstance(x, (int, float)) and math.isfinite(x) for x in box)
            and box[2] > box[0] and box[3] > box[1])


def _jump(a, b):
    if not _valid_box(a) or not _valid_box(b):
        return True
    shift = math.hypot((a[0]+a[2]-b[0]-b[2])/2, (a[1]+a[3]-b[1]-b[3])/2)
    diagonal = math.hypot(a[2]-a[0], a[3]-a[1])
    intersection = max(0, min(a[2], b[2])-max(a[0], b[0])) * max(0, min(a[3], b[3])-max(a[1], b[1]))
    union = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - intersection
    return shift > DEFAULT_MAX_CENTER_SHIFT * diagonal or intersection / union < DEFAULT_MIN_IOU


class PlateBackfill:
    """線上／離線共用；磁碟暫存精簡觀測，支援 ViolationEngine 延後至片尾輸出的事件。

    停用時不建資料庫、不延遲事件；啟用時按原引擎輸出順序發布，不處理 merged_events 子事件。
    保留完整影片的精簡索引是因為引擎可能無限期保留事件；SQLite 快取限 2 MiB，close 自動刪除暫存庫。
    """
    def __init__(self, seconds=DEFAULT_WINDOW_SECONDS):
        self.seconds = window_seconds(seconds)
        self.pending = deque()
        self.now = None
        self.index = None
        self.db = None
        if self.seconds:
            self.db = sqlite3.connect("")
            self.db.execute("PRAGMA cache_size=-2048")
            self.db.execute("CREATE TABLE vehicles (i INTEGER, t REAL, oid TEXT, fresh INTEGER, norm TEXT, body TEXT)")
            self.db.execute("CREATE INDEX track_time ON vehicles (oid,t)")
            self.db.execute("CREATE INDEX plate_time ON vehicles (norm,t)")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def update(self, observation, events=()):
        frame = observation["frame"]
        now, index = frame["timestamp_sec"], frame["index"]
        if self.seconds and (self.now is not None and (now <= self.now or index <= self.index)):
            raise ValueError("車牌補充需要嚴格遞增的幀序與時間；新影片請建立新實例")
        self.now, self.index = now, index
        if self.db is not None:
            # 規則車輛優先，原始車輛補上未涵蓋的 ID；不把兩個視圖當不同的觀測。
            items, selected = observation.get("observations", {}), {}
            for group in ("rule_vehicles", "vehicles"):
                for vehicle in items.get(group, []):
                    oid = vehicle.get("object_id")
                    if oid and oid not in selected:
                        selected[oid] = {k: vehicle.get(k) for k in
                            ("bbox_xyxy", "source_frame", "plate_text", "text_confidence")}
            self.db.executemany("INSERT INTO vehicles VALUES (?,?,?,?,?,?)", [
                (index, now, oid, int(v["source_frame"] == index), normalize_plate(v["plate_text"]),
                 json.dumps(v)) for oid, v in selected.items()])
        self.pending.extend(deepcopy(list(events)))
        result = []
        while self.pending:
            event = self.pending[0]
            gap = self._expired_gap(event)
            if self.seconds and now < event["timestamp_sec"] + self.seconds - 1e-9 and not gap:
                break
            result.append(self._finish(self.pending.popleft(), interrupted=gap))
        return result

    def _expired_gap(self, event):
        if not self.seconds or self.now is None:
            return False
        start = event["timestamp_sec"]
        # 先到視窗終點者正常完成；較晚的缺席不追溯否決已完成視窗。
        if self.now >= start + self.seconds - 1e-9:
            return False
        last = self.db.execute("SELECT MAX(t) FROM vehicles WHERE oid=? AND fresh=1 AND t>=? AND t<=?",
                               (event.get("object_id"), start, self.now)).fetchone()[0]
        return self.now - (last if last is not None else start) > DEFAULT_TRACK_GAP_SECONDS + 1e-9

    def finalize(self, events=()):
        self.pending.extend(deepcopy(list(events)))
        result = [self._finish(e, interrupted=self._expired_gap(e)) for e in self.pending]
        self.pending.clear()
        return result

    def _finish(self, event, interrupted=False):
        original = event_plate_text(event, at_event=True)
        vehicle = ((event.get("evidence") or {}).get("vehicle") or {})
        start, oid = event["timestamp_sec"], event.get("object_id")
        plate = dict(text=original or None, source="event_frame" if original else "none", object_id=oid,
                     observed_frame=event["frame"] if original else None,
                     observed_timestamp_sec=start if original else None, delay_seconds=0.0 if original else None,
                     text_confidence=vehicle.get("text_confidence") if original else None,
                     window_seconds=self.seconds, conflict=False, rejected=[])
        event["plate"] = plate
        if not self.seconds:
            return event
        end = start + self.seconds
        rows = self.db.execute("SELECT i,t,fresh,body FROM vehicles WHERE oid=? AND t>? AND t<=? ORDER BY t,i",
                               (oid, start, end + 1e-9)).fetchall()
        # 延後至片尾才到達的事件，也重現當時會觸發的提前中止。
        fresh_times = [now for _, now, fresh, _ in rows if fresh]
        horizon = min(end, self.now if self.now is not None else start)
        timeline = [start] + fresh_times + [horizon]
        interrupted = interrupted or any(b-a > DEFAULT_TRACK_GAP_SECONDS + 1e-9
                                         for a, b in zip(timeline, timeline[1:]))
        last_time, last_box, broken = start, vehicle.get("bbox_xyxy"), None
        accepted = None
        candidate_conflict = False
        for index, now, fresh, body in rows:
            v = json.loads(body)
            if not fresh:
                continue  # 快取框不是候選，也不能修補追蹤的時間間隔。
            if now - last_time > DEFAULT_TRACK_GAP_SECONDS + 1e-9:
                broken = broken or "track_gap"
            elif _jump(last_box, v.get("bbox_xyxy")):
                broken = broken or "track_jump"
            last_time, last_box = now, v.get("bbox_xyxy")
            text = v.get("plate_text")
            if not normalize_plate(text):
                continue
            reason = None
            if original and normalize_plate(text) != normalize_plate(original):
                plate["conflict"] = True
                reason = "plate_conflict"
            elif not original and (candidate_conflict or (accepted and normalize_plate(text) != normalize_plate(accepted["text"]))):
                if accepted:
                    plate["rejected"].append(dict(text=accepted["text"], frame=accepted["observed_frame"], reason="plate_conflict"))
                accepted, candidate_conflict = None, True
                reason = "plate_conflict"
            elif broken or interrupted:
                reason = broken or "track_gap"
            elif self.db.execute("SELECT 1 FROM vehicles WHERE norm=? AND oid<>? AND t>=? AND t<=? LIMIT 1",
                                 (normalize_plate(text), oid, start, end + 1e-9)).fetchone():
                reason = "shared_with_other_track"
            elif (not isinstance(v.get("text_confidence"), (int, float))
                  or not math.isfinite(v["text_confidence"])
                  or not DEFAULT_MIN_CONFIDENCE <= v["text_confidence"] <= 1):
                reason = "low_confidence"
            if reason:
                plate["rejected"].append(dict(text=text, frame=index, reason=reason))
            elif accepted is None and not original:
                accepted = dict(text=text, source="same_track_after_event", observed_frame=index,
                                observed_timestamp_sec=now, delay_seconds=round(now-start, 9),
                                text_confidence=v["text_confidence"])
        if accepted:
            plate.update(accepted)
        # 只記已知的第一筆視窗外讀數；發布後不為診斷再延遲或改寫事件。
        outside = self.db.execute("SELECT i,body FROM vehicles WHERE oid=? AND t>? AND fresh=1 AND norm IS NOT NULL ORDER BY t,i LIMIT 1",
                                  (oid, end + 1e-9)).fetchone()
        if outside:
            plate["rejected"].append(dict(text=json.loads(outside[1])["plate_text"], frame=outside[0], reason="after_window"))
        return event
