"""解析行車記錄器 GPS 軌跡檔：CSV / GPX / NMEA。

統一輸出 [{"t": 秒 或 None, "time": datetime 或 None, "lat": float, "lon": float}, ...]
- t    : 相對影片開頭的秒數（CSV 若給的是秒數欄位）
- time : 絕對時間（GPX / NMEA / CSV ISO 時間）
"""
from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

TW = timezone(timedelta(hours=8))


def _parse_time(s: str):
    s = s.strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TW)
        return dt.astimezone(TW)
    except ValueError:
        return None


def _parse_csv(text: str):
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    header = [h.strip().lower() for h in rows[0]]

    def idx(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return None

    i_lat, i_lon = idx("lat", "latitude", "緯度"), idx("lon", "lng", "longitude", "經度")
    i_t = idx("t", "sec", "seconds", "offset", "秒")
    i_time = idx("time", "timestamp", "datetime", "時間")
    body = rows[1:]
    if i_lat is None or i_lon is None:  # 無標頭：假設 t,lat,lon
        i_t, i_lat, i_lon, i_time, body = 0, 1, 2, None, rows
    pts = []
    for r in body:
        try:
            p = {"lat": float(r[i_lat]), "lon": float(r[i_lon]), "t": None, "time": None}
        except (ValueError, IndexError):
            continue
        if i_t is not None:
            try:
                p["t"] = float(r[i_t])
            except (ValueError, IndexError):
                pass
        if i_time is not None and i_time < len(r):
            p["time"] = _parse_time(r[i_time])
        pts.append(p)
    return pts


def _parse_gpx(text: str):
    root = ET.fromstring(text)
    pts = []
    for el in root.iter():
        if el.tag.endswith("trkpt") or el.tag.endswith("wpt"):
            t = None
            for c in el:
                if c.tag.endswith("time") and c.text:
                    t = _parse_time(c.text)
            pts.append({"lat": float(el.get("lat")), "lon": float(el.get("lon")), "t": None, "time": t})
    return pts


def _nmea_deg(v: str, hemi: str):
    if not v:
        return None
    d = float(v)
    deg = int(d // 100)
    val = deg + (d - deg * 100) / 60
    return -val if hemi in ("S", "W") else val


def _parse_nmea(text: str):
    pts = []
    for line in text.splitlines():
        m = re.match(r"^\$(GP|GN)RMC,([^*]*)", line.strip())
        if not m:
            continue
        f = m.group(2).split(",")
        if len(f) < 9 or f[1] != "A":
            continue
        lat, lon = _nmea_deg(f[2], f[3]), _nmea_deg(f[4], f[5])
        t = None
        try:
            t = datetime.strptime(f[8] + f[0][:6], "%d%m%y%H%M%S").replace(tzinfo=timezone.utc).astimezone(TW)
        except ValueError:
            pass
        if lat is not None and lon is not None:
            pts.append({"lat": lat, "lon": lon, "t": None, "time": t})
    return pts


def parse_track(filename: str, raw: bytes):
    text = raw.decode("utf-8-sig", errors="replace")
    name = filename.lower()
    if name.endswith(".gpx") or text.lstrip().startswith("<"):
        return _parse_gpx(text)
    if name.endswith((".nmea", ".nma", ".log")) or "$GPRMC" in text or "$GNRMC" in text:
        return _parse_nmea(text)
    return _parse_csv(text)


def position_at(track, video_t: float, recorded_at: datetime | None):
    """依影片秒數找出（線性內插）當下座標。"""
    if not track:
        return None
    keyed = []
    if any(p.get("t") is not None for p in track):
        keyed = [(p["t"], p) for p in track if p.get("t") is not None]
    elif recorded_at and any(p.get("time") for p in track):
        keyed = [((p["time"] - recorded_at).total_seconds(), p) for p in track if p.get("time")]
    if not keyed:
        return {"lat": track[0]["lat"], "lon": track[0]["lon"]}
    keyed.sort(key=lambda x: x[0])
    if video_t <= keyed[0][0]:
        p = keyed[0][1]
        return {"lat": p["lat"], "lon": p["lon"]}
    for (t0, a), (t1, b) in zip(keyed, keyed[1:]):
        if t0 <= video_t <= t1:
            r = 0 if t1 == t0 else (video_t - t0) / (t1 - t0)
            return {"lat": a["lat"] + (b["lat"] - a["lat"]) * r, "lon": a["lon"] + (b["lon"] - a["lon"]) * r}
    p = keyed[-1][1]
    return {"lat": p["lat"], "lon": p["lon"]}


def track_to_json(track):
    return [
        {"lat": p["lat"], "lon": p["lon"], "t": p.get("t"),
         "time": p["time"].isoformat() if p.get("time") else None}
        for p in track
    ]


def track_from_json(data):
    return [
        {"lat": p["lat"], "lon": p["lon"], "t": p.get("t"),
         "time": _parse_time(p["time"]) if p.get("time") else None}
        for p in (data or [])
    ]
