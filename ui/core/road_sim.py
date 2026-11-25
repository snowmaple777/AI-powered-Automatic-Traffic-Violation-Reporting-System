"""沿真實道路的模擬 GPS 軌跡（展示用）。

gps_sim.simulate 是在平面上隨機開，不會沿著路，常落在校園、空地裡，反查不到路名。
這裡改成：
- 用 OSRM 公開路線服務（router.project-osrm.org，OpenStreetMap 圖資）在起點附近規劃一段真實路線，
  沿著有路名的路段每秒取一點（時速約 25～40 km/h）。
- 用網頁同一套 Nominatim 反查（address.reverse_address）確認抽查的點都查得到路名，查不到就換一條。
  查過的結果留在 address_cache.json，之後開檢舉單直接命中。
- 需要網路；連不到時丟 Offline，呼叫端自己決定要不要退回 gps_sim.simulate。

回傳格式跟 gps_sim.simulate 相同：[{"t", "time", "lat", "lon"}...]，可直接給 gps_parser / to_gpx 用。
"""
from __future__ import annotations

import json
import math
import random
import ssl
import time
import urllib.request
from datetime import datetime, timedelta
from xml.sax.saxutils import escape

from . import address
from .gps_parser import TW

# 依序嘗試：OpenStreetMap 官網用的 FOSSGIS 主機、OSRM 官方 demo 主機（API 格式相同）
OSRM_HOSTS = ("https://routing.openstreetmap.de/routed-car", "https://router.project-osrm.org")
OSRM_PATH = "/route/v1/driving/{lon1:.6f},{lat1:.6f};{lon2:.6f},{lat2:.6f}?overview=false&steps=true&geometries=geojson"
M_PER_DEG_LAT = 111_320.0
MIN_SPEED = 15 / 3.6          # 路段不夠長時最多放慢到這裡，再不夠就在路段尾端停下來
SKIP_UNNAMED_M = 60           # 夾在兩條路中間、這麼短的無名連接段（匝道口、迴轉道）可以穿過
_last_osrm = 0.0
_ctx = None


def ssl_context():
    """系統根憑證＋certifi（有裝的話）。有些電腦的 Python 根憑證太舊，會把有效的憑證判成過期；
    certifi 是常態更新的根憑證清單，補上它就好，不關閉憑證驗證。"""
    global _ctx
    if _ctx is None:
        _ctx = ssl.create_default_context()
        try:
            import certifi  # type: ignore
            _ctx.load_verify_locations(cafile=certifi.where())
        except Exception:  # noqa: BLE001 — 沒裝 certifi 就只用系統的
            pass
    return _ctx


class Offline(Exception):
    """連不到 OSRM 或 Nominatim。"""


def dist_m(a, b):
    r = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def offset(lat, lon, north_m, east_m):
    return (lat + north_m / M_PER_DEG_LAT,
            lon + east_m / (M_PER_DEG_LAT * math.cos(math.radians(lat))))


def osrm_route(a, b, timeout=15):
    """OSRM 路線的 steps（每秒最多 1 次）；網路有問題丟 Offline。"""
    global _last_osrm
    wait = 1.0 - (time.time() - _last_osrm)
    if wait > 0:
        time.sleep(wait)
    _last_osrm = time.time()
    path = OSRM_PATH.format(lat1=a[0], lon1=a[1], lat2=b[0], lon2=b[1])
    errors, data = [], None
    for host in OSRM_HOSTS:
        try:
            req = urllib.request.Request(host + path, headers={"User-Agent": address.UA})
            with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as r:
                data = json.loads(r.read().decode("utf-8"))
            break
        except Exception as e:  # noqa: BLE001 — 這台不行就換下一台
            errors.append(f"{host}：{e}")
    if data is None:
        raise Offline("連不到 OSRM 路線服務：\n    " + "\n    ".join(errors))
    if data.get("code") != "Ok" or not data.get("routes"):
        return []
    return data["routes"][0]["legs"][0]["steps"]


def named_polyline(steps):
    """從第一個有路名的路段開始接成折線 [(lat, lon, 路名)...]；遇到較長的無名路段就停。"""
    line, started, last_name = [], False, ""
    for st in steps:
        name = (st.get("name") or "").strip()
        coords = (st.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        if not name:
            if not started:
                continue
            if float(st.get("distance") or 1e9) > SKIP_UNNAMED_M:
                break
            name = last_name
        started, last_name = True, name
        for lon, lat in coords:
            if line and abs(line[-1][0] - lat) < 1e-9 and abs(line[-1][1] - lon) < 1e-9:
                continue
            line.append((lat, lon, name))
    return line


def sample(line, n, speed):
    """沿折線每秒走 speed 公尺取 n 點；不夠長就放慢，最慢 MIN_SPEED，再不夠就停在尾端。"""
    cum = [0.0]
    for p, q in zip(line, line[1:]):
        cum.append(cum[-1] + dist_m(p, q))
    if n > 1 and cum[-1] < speed * (n - 1):
        speed = max(MIN_SPEED, cum[-1] / (n - 1))
    out, j = [], 0
    for i in range(n):
        d = min(speed * i, cum[-1])
        while j < len(line) - 2 and cum[j + 1] < d:
            j += 1
        seg = cum[j + 1] - cum[j]
        r = 0.0 if seg <= 0 else (d - cum[j]) / seg
        p, q = line[j], line[j + 1]
        out.append((p[0] + (q[0] - p[0]) * r, p[1] + (q[1] - p[1]) * r, p[2] if r < 0.5 else q[2]))
    return out


def road_at(lat, lon):
    """Nominatim 反查到的路名；沒有回傳 None；連不到丟 Offline。"""
    res = address.reverse_address(lat, lon)
    if res.get("error") == "offline":
        raise Offline("連不到 Nominatim（OpenStreetMap 反查）")
    return res.get("road") if res.get("address") else None


def simulate(lat, lon, start: datetime, duration: float, seed=None, n=None, spread=300.0,
             check="ends", tries=8, log=None):
    """沿真實道路產生軌跡。

    lat/lon：起點附近（會在 spread 公尺內錯開，OSRM 會貼到最近的路上）。
    n：點數（每秒一點）；沒給就依 duration 算。
    check："ends" 抽查起點／中點／終點、"start" 只查起點（網頁用，比較快）。
    回傳 (track, roads)；試了 tries 次都不行回傳 (None, None)；沒網路丟 Offline。
    """
    rng = random.Random(str(seed) if seed is not None else None)
    if start.tzinfo is None:
        start = start.replace(tzinfo=TW)
    if n is None:
        n = max(2, int(math.ceil(duration)) + 1)
    for attempt in range(1, tries + 1):
        r, ang = spread * math.sqrt(rng.random()), rng.uniform(0, 2 * math.pi)
        a = offset(lat, lon, r * math.cos(ang), r * math.sin(ang))
        speed = rng.uniform(25, 40) / 3.6
        far = min(max(speed * n * 0.9, 700.0), 15000.0) * rng.uniform(1.0, 1.5)
        ang2 = rng.uniform(0, 2 * math.pi)
        b = offset(a[0], a[1], far * math.cos(ang2), far * math.sin(ang2))
        line = named_polyline(osrm_route(a, b))
        if len(line) < 2:
            if log:
                log(f"    第 {attempt} 次：這段路線沒有具名道路，換一條")
            continue
        pts = sample(line, n, speed)
        idx = [0] if check == "start" else sorted({0, (n - 1) // 2, n - 1})
        roads = [road_at(pts[i][0], pts[i][1]) for i in idx]
        if not all(roads):
            if log:
                log(f"    第 {attempt} 次：有點反查不到路名（{roads}），換一條")
            continue
        track = [{"t": float(i), "time": start + timedelta(seconds=i), "lat": round(p[0], 7), "lon": round(p[1], 7)}
                 for i, p in enumerate(pts)]
        names = list(dict.fromkeys([p[2] for p in pts] + roads))
        return track, names
    return None, None


def to_gpx(track, name, roads):
    """跟 gps_sim.to_gpx 一樣的格式，多標註經過的路名；desc 仍含 simulated，網頁會顯示「模擬軌跡」。"""
    from datetime import timezone
    rows = []
    for p in track:
        ts = p["time"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if p.get("time") else ""
        rows.append(f'      <trkpt lat="{p["lat"]:.7f}" lon="{p["lon"]:.7f}">' + (f"<time>{ts}</time>" if ts else "") + "</trkpt>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<gpx version="1.1" creator="馬路三寶 模擬軌跡（展示用，沿真實道路）" xmlns="http://www.topografix.com/GPX/1/1">\n'
            f'  <!-- 沿 OpenStreetMap 道路產生（OSRM），經過：{escape("、".join(roads))}。位置不是影片實際拍攝地點。 -->\n'
            f'  <trk><name>{escape(name)}</name><desc>simulated track for demo (road-following, OpenStreetMap/OSRM)</desc>\n'
            '    <trkseg>\n' + "\n".join(rows) + "\n    </trkseg>\n  </trk>\n</gpx>\n")
