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


ROAD_KEYS = ("road", "pedestrian", "footway", "cycleway", "path", "service", "residential")
NEG_TTL = 24 * 3600  # 「該點沒有路名」只記一天，之後再查（舊版存的 null 一律視為過期）


def _road_of(data):
    a = data.get("address") or {}
    for k in ROAD_KEYS:
        v = (a.get(k) or "").strip()
        if v:
            return v
    # 點正好落在道路本身（category=highway）時，路名在 name 不在 address.road
    if data.get("category") == "highway" and (data.get("name") or "").strip():
        return data["name"].strip()
    return ""


def _district_of(data):
    """OSM 的行政區（楠梓區、竹北市…）；拿不到回傳空字串。
    離線表只用「最近的區中心點」判斷，區界附近會判錯，所以有 OSM 的就以 OSM 為準。"""
    a = data.get("address") or {}
    d = (a.get("suburb") or a.get("city_district") or a.get("town") or a.get("district") or "").strip()
    return d if d.endswith(("區", "鄉", "鎮", "市")) else ""


def _fmt(data, lat, lon):
    """Nominatim 回傳 → 「高雄市楠梓區大學南路700號附近」這種台灣寫法。"""
    a = data.get("address") or {}
    off = geo.reverse_geocode(lat, lon)  # 縣市名稱以離線表為準，跟下拉選單一致
    city = off["city"]
    district = _district_of(data) or off["district"]
    road = _road_of(data)
    if not road:
        return None
    no = (a.get("house_number") or "").strip()
    if no and not no.endswith("號"):
        no += "號"
    return {"address": f"{city}{district}{road}{no}附近", "city": city, "district": district,
            "road": road, "house_number": no, "source": "OpenStreetMap"}


def _ssl():
    from .road_sim import ssl_context  # 同一份：系統根憑證＋certifi
    return ssl_context()


def _query(lat, lon, zoom, timeout):
    """打一次 Nominatim（遵守每秒 1 次）。網路錯誤往外丟。"""
    global _last
    wait = 1.05 - (time.time() - _last)
    if wait > 0:
        time.sleep(wait)
    _last = time.time()
    q = urllib.parse.urlencode({"format": "jsonv2", "lat": f"{lat:.6f}", "lon": f"{lon:.6f}",
                                "zoom": zoom, "addressdetails": 1, "accept-language": "zh-TW"})
    req = urllib.request.Request(f"{URL}?{q}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl()) as r:
        return json.loads(r.read().decode("utf-8"))


def reverse_address(lat: float, lon: float, timeout: float = 6):
    """成功回傳地址 dict；失敗回傳 {"error": "offline" | "no_road"}。
    no_road 時若 OSM 有給行政區，會一併帶 "district"，讓前端修正離線表猜錯的行政區。"""
    key = f"{lat:.5f},{lon:.5f}"
    with _lock:
        cache = _load()
        hit = cache.get(key)
        if isinstance(hit, dict) and hit.get("address"):
            return hit
        if isinstance(hit, dict) and hit.get("error") and time.time() - hit.get("ts", 0) < NEG_TTL:
            return {k: v for k, v in hit.items() if k != "ts"}
        # zoom 18 是建築物等級，橋下／路口常抓到建物或區域而沒有路名，再退到街道等級
        res, district = None, ""
        for zoom in (18, 17, 16):
            try:
                data = _query(lat, lon, zoom, timeout)
            except Exception:  # noqa: BLE001 — 離線／逾時：不快取，下次有網路再查
                return {"error": "offline"}
            if isinstance(data, dict) and not data.get("error"):
                district = district or _district_of(data)
                res = _fmt(data, lat, lon)
                if res:
                    break
        fail = {"error": "no_road", **({"district": district} if district else {})}
        cache[key] = res if res else {**fail, "ts": time.time()}
        try:
            _save()
        except OSError:
            pass
        return res if res else fail
