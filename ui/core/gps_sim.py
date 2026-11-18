"""模擬 GPS 軌跡（展示用）。

我們手上的測試影片沒有 GPS，這裡依「起點座標＋開始時間＋影片長度」產生一條像市區開車的軌跡：
每秒一點、時速約 20～45 km/h、方向慢慢偏、偶爾在路口轉彎或停等紅燈，再加一點點 GPS 雜訊。
同一個 seed（例如影片檔名）每次產生的軌跡都一樣，展示時不會每次跑出不同地點。

產生的軌跡跟真的 GPS 檔格式相同，可以直接給 gps_parser / position_at 用，也能匯出成 GPX。
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

from .gps_parser import TW

M_PER_DEG_LAT = 111_320.0


def simulate(lat: float, lon: float, start: datetime, duration: float, seed=None) -> list[dict]:
    """回傳 [{"t", "time", "lat", "lon"}...]，t 是相對影片開頭的秒數。"""
    rng = random.Random(str(seed) if seed is not None else None)
    if start.tzinfo is None:
        start = start.replace(tzinfo=TW)
    n = max(2, int(math.ceil(duration)) + 1)
    heading = rng.choice([0, 90, 180, 270]) + rng.uniform(-12, 12)  # 市區道路大多是棋盤狀
    speed = rng.uniform(25, 40) / 3.6                                # m/s
    target = speed
    turn_left = 0.0           # 正在轉彎還要轉多少度
    stop_left = 0             # 停等紅燈還剩幾秒
    next_event = rng.uniform(15, 40)
    x = y = 0.0               # 相對起點的公尺（東、北）
    pts = []
    for i in range(n):
        if i:
            if i >= next_event and not turn_left and not stop_left:
                if rng.random() < 0.6:
                    turn_left = rng.choice([-90, 90])
                    target = rng.uniform(12, 20) / 3.6       # 轉彎減速
                else:
                    stop_left = rng.randint(5, 15)           # 停紅燈
                next_event = i + rng.uniform(20, 45)
            if stop_left:
                target = 0
                stop_left -= 1
                if not stop_left:
                    target = rng.uniform(25, 45) / 3.6
            if turn_left:
                step = math.copysign(min(abs(turn_left), 30), turn_left)
                heading += step
                turn_left -= step
                if not turn_left:
                    target = rng.uniform(25, 45) / 3.6
            else:
                heading += rng.uniform(-1.5, 1.5)
            speed += (target - speed) * 0.35                 # 加減速平順一點
            x += speed * math.sin(math.radians(heading))
            y += speed * math.cos(math.radians(heading))
        noise = 1.5 if speed > 0.5 else 0.6                  # GPS 雜訊（公尺）
        ny, nx = y + rng.gauss(0, noise), x + rng.gauss(0, noise)
        plat = lat + ny / M_PER_DEG_LAT
        plon = lon + nx / (M_PER_DEG_LAT * math.cos(math.radians(lat)))
        pts.append({"t": float(i), "time": start + timedelta(seconds=i),
                    "lat": round(plat, 7), "lon": round(plon, 7)})
    return pts


def to_gpx(track: list[dict], name: str = "simulated") -> str:
    """匯出成 GPX 1.1（時間用 UTC）。"""
    rows = []
    for p in track:
        ts = p["time"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if p.get("time") else ""
        rows.append(f'      <trkpt lat="{p["lat"]:.7f}" lon="{p["lon"]:.7f}">' + (f"<time>{ts}</time>" if ts else "") + "</trkpt>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<gpx version="1.1" creator="馬路三寶 模擬軌跡（展示用）" xmlns="http://www.topografix.com/GPX/1/1">\n'
            f'  <trk><name>{escape(name)}</name><desc>simulated track for demo</desc>\n    <trkseg>\n'
            + "\n".join(rows) + "\n    </trkseg>\n  </trk>\n</gpx>\n")


def parse_local(s: str | None) -> datetime:
    """前端 datetime-local 的值（台灣時間）→ datetime；沒給就用現在。"""
    if s:
        try:
            dt = datetime.fromisoformat(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=TW)
        except ValueError:
            pass
    return datetime.now(TW).replace(microsecond=0)
