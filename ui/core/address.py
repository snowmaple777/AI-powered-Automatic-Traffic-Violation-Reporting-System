"""GPS 座標 → 路名／門牌（線上反查，OpenStreetMap Nominatim，不用 API key）。

geo.reverse_geocode 是離線查表，只到「行政區」；這裡再往下查到路名，填進檢舉單的「交通違規地點」。
- 有網路才查得到；查不到（離線、逾時、該點沒有路名）就回傳 None，前端維持只填行政區並提醒手動補。
- Nominatim 的使用規定：每秒最多 1 次、要帶可辨識的 User-Agent。這裡用鎖＋間隔控制，
  並把結果存在 ui/data/address_cache.json（座標取到小數第 5 位，約 1 公尺），同一點不會重複查。
- GPS 本身有幾公尺誤差，路口附近可能抓到隔壁那條路，所以前端會提醒送出前看一眼。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request

from . import geo

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
CACHE_PATH = os.path.join(DATA_DIR, "address_cache.json")
URL = "https://nominatim.openstreetmap.org/reverse"
UA = "MaluSanbao-TrafficReport/1.0 (school project; local demo)"
_lock = threading.Lock()
_last = 0.0
_cache = None


def _load():
    global _cache
    if _cache is None:
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                _cache = json.load(f)
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save():
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_cache, f, ensure_ascii=False)
    os.replace(tmp, CACHE_PATH)


def _fmt(data, lat, lon):
    """Nominatim 回傳 → 「高雄市楠梓區大學南路700號附近」這種台灣寫法。"""
    a = data.get("address") or {}
    off = geo.reverse_geocode(lat, lon)  # 縣市／行政區以離線表為準，名稱跟下拉選單一致
    city = off["city"]
    district = (a.get("suburb") or a.get("city_district") or a.get("town") or a.get("district") or "").strip()
    if not district.endswith(("區", "鄉", "鎮", "市")):
        district = off["district"]
    road = (a.get("road") or a.get("pedestrian") or a.get("residential") or "").strip()
    if not road:
        return None
    no = (a.get("house_number") or "").strip()
    if no and not no.endswith("號"):
        no += "號"
    return {"address": f"{city}{district}{road}{no}附近", "city": city, "district": district,
            "road": road, "house_number": no, "source": "OpenStreetMap"}


def reverse_address(lat: float, lon: float, timeout: float = 6):
    global _last
    key = f"{lat:.5f},{lon:.5f}"
    with _lock:
        cache = _load()
        if key in cache:
            return cache[key]
        wait = 1.05 - (time.time() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.time()
        q = urllib.parse.urlencode({"format": "jsonv2", "lat": f"{lat:.6f}", "lon": f"{lon:.6f}",
                                    "zoom": 18, "addressdetails": 1, "accept-language": "zh-TW"})
        try:
            req = urllib.request.Request(f"{URL}?{q}", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 — 離線／逾時：不快取，下次有網路再查
            return None
        res = _fmt(data, lat, lon) if isinstance(data, dict) and not data.get("error") else None
        cache[key] = res
        try:
            _save()
        except OSError:
            pass
        return res
