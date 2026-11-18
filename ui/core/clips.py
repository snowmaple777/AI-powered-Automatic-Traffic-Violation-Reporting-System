"""影片來源：已切好的片段庫、雲端連結匯入、長片待切分收件匣。

每個帳號各自一份，彼此看不到：
- 片段庫：<clips_dir>/<帳號資料夾>/ ，影片切段程式把某個帳號的長片切好後放這裡，
  可以有子資料夾（一個子資料夾＝一支長片切出來的所有片段）。
- 長片收件匣：<片段庫>/_待切分/ ，從介面上傳的長片先放這裡，等切段程式處理；
  底線開頭的資料夾不會出現在片段清單。
- <帳號資料夾> 是 Email 換掉特殊字元，例如 di@gmail.com → di_at_gmail.com。
路徑可在 ui/config.json 用 "clips_dir"（預設 ui/clips）改，相對路徑以專案根目錄為準。
"""
from __future__ import annotations

import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import geo, gps_parser, store

VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".m4v", ".ts", ".webm")
MAX_IMPORT_BYTES = 4 * 1024 ** 3  # 雲端匯入上限 4 GB
INBOX_NAME = "_待切分"
GPS_EXTS = (".gpx", ".nmea", ".nma", ".csv", ".log")


def _resolve(path):
    path = os.path.expanduser(path)
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(store.PROJECT_DIR, path))


def clips_root():
    cfg = store.CONFIG.get("clips_dir") or os.environ.get("CLIPS_DIR")
    return _resolve(cfg) if cfg else os.path.join(os.path.dirname(store.DATA_DIR), "clips")  # ui/clips


def user_folder(email):
    return re.sub(r"[^\w.-]", "_", email.lower().replace("@", "_at_"))


def user_dir():
    """目前登入帳號的片段庫（沒有就建立）。"""
    d = os.path.join(clips_root(), user_folder(store.require_user()))
    os.makedirs(d, exist_ok=True)
    return d


def long_inbox():
    return os.path.join(user_dir(), INBOX_NAME)


def _sources():
    """這個帳號看得到的片段來源：(前綴, 資料夾, 顯示名稱)。"""
    return [("my", user_dir(), "")]


def sidecar_gps(video_path):
    """影片旁邊同名的軌跡檔（seg_001.mp4 → seg_001.gpx / .nmea / .csv）。"""
    stem = os.path.splitext(video_path)[0]
    for ext in GPS_EXTS:
        for cand in (stem + ext, stem + ext.upper()):
            if os.path.isfile(cand):
                return cand
    return None


def _is_video(name):
    return name.lower().endswith(VIDEO_EXTS)


def _stamp(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# 片段庫（每個帳號自己的）
# ---------------------------------------------------------------------------
def list_clips() -> dict:
    clips = []
    for prefix, root, label in _sources():
        for cur, dirs, files in os.walk(root):
            rel_dir = os.path.relpath(cur, root)
            depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
            dirs[:] = sorted(d for d in dirs if not d.startswith(("_", ".")) and depth < 2)
            for f in sorted(files):
                if not _is_video(f):
                    continue
                full = os.path.join(cur, f)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                st = os.stat(full)
                sub = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
                group = "／".join(x for x in (label, sub) if x)
                cid = f"{prefix}/{rel}"
                gps = sidecar_gps(full)
                clips.append({
                    "id": cid, "name": f, "group": group, "gps": os.path.basename(gps) if gps else None,
                    "size": st.st_size, "mtime": int(st.st_mtime * 1000), "modified": _stamp(st.st_mtime),
                    "url": "/files/clips/" + quote(cid),
                })
    groups = sorted({c["group"] for c in clips})
    return {"dir": user_dir(), "exists": True, "clips": clips, "groups": groups}


def clip_path(cid: str) -> str:
    """片段 id → 實際路徑；只能拿到自己看得到的片段。"""
    cid = unquote(cid or "").replace("\\", "/").lstrip("/")
    prefix, _, rel = cid.partition("/")
    root = next((r for p, r, _ in _sources() if p == prefix), None)
    if not root:
        raise FileNotFoundError("找不到這段片段")
    full = os.path.normpath(os.path.join(root, *[p for p in rel.split("/") if p not in ("", ".", "..")]))
    if not full.startswith(root + os.sep) or not _is_video(full) or not os.path.isfile(full):
        raise FileNotFoundError("找不到這段片段")
    return full


def use_clip(cid: str) -> dict:
    """把片段複製一份到 uploads，之後的分析流程跟一般上傳完全一樣。"""
    full = clip_path(cid)
    with open(full, "rb") as f:
        res = store.save_upload(os.path.basename(full), f, os.path.getsize(full))
    st = os.stat(full)
    res.update({"name": os.path.basename(full), "source": "clips", "clip_id": cid,
                "lastModified": int(st.st_mtime * 1000)})
    side = sidecar_gps(full)
    if side:  # 有同名軌跡檔就一起帶給前端
        with open(side, "rb") as f:
            raw = f.read()
        try:
            track = gps_parser.parse_track(side, raw)
        except Exception:  # noqa: BLE001 — 軌跡檔壞掉就當沒有
            track = []
        if track:
            res["gps"] = {"file": os.path.basename(side), "count": len(track),
                          "track": gps_parser.track_to_json(track),
                          "geo": geo.reverse_geocode(track[0]["lat"], track[0]["lon"]),
                          "simulated": b"simulated track" in raw[:4096]}
    return res


# ---------------------------------------------------------------------------
# 雲端連結匯入（Google Drive／Dropbox／OneDrive／直接的影片網址）
# ---------------------------------------------------------------------------
def _drive_id(u):
    m = re.search(r"/file/d/([\w-]{10,})", u.path) or re.search(r"/d/([\w-]{10,})", u.path)
    if m:
        return m.group(1)
    return (parse_qs(u.query).get("id") or [None])[0]


def normalize_url(url: str):
    url = (url or "").strip()
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ValueError("請貼上 http(s) 開頭的分享連結")
    host = u.netloc.lower()
    if "drive.google.com" in host or "docs.google.com" in host or "drive.usercontent.google.com" in host:
        fid = _drive_id(u)
        if not fid:
            raise ValueError("看不懂這個 Google Drive 連結，請用「共用 → 複製連結」的網址")
        return f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t", "Google Drive"
    if "dropbox.com" in host:
        q = re.sub(r"([?&])dl=0", r"\1dl=1", url)
        return (q if "dl=1" in q else q + ("&" if "?" in q else "?") + "dl=1"), "Dropbox"
    if "1drv.ms" in host or "onedrive.live.com" in host:
        return url + ("&" if "?" in url else "?") + "download=1", "OneDrive"
    return url, "網址"


def _filename_from(resp, fallback):
    cd = resp.headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd, re.I) or re.search(r'filename="?([^";]+)"?', cd, re.I)
    name = unquote(m.group(1)) if m else os.path.basename(urlparse(resp.geturl()).path) or fallback
    if not _is_video(name):
        name = os.path.splitext(name)[0] + ".mp4"
    return name


class _Capped:
    """讀到上限就停，避免無限大的回應塞爆硬碟。"""

    def __init__(self, f, limit):
        self.f, self.left = f, limit

    def read(self, n):
        if self.left <= 0:
            return b""
        b = self.f.read(min(n, self.left))
        self.left -= len(b)
        return b


def import_url(url: str) -> dict:
    store.require_user()
    dl, provider = normalize_url(url)
    req = urllib.request.Request(dl, headers={"User-Agent": "Mozilla/5.0 TrafficReportUI"})
    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        raise ValueError(f"{provider} 回應 {e.code}，請確認連結已設為「知道連結的任何人都能檢視」") from e
    except (urllib.error.URLError, OSError) as e:
        raise ValueError(f"連不到 {provider}：{getattr(e, 'reason', e)}") from e
    with resp:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if ctype.startswith("text/html"):
            raise ValueError(f"{provider} 回傳的是網頁不是影片檔。請把分享權限設為「知道連結的任何人」，"
                             "或確認連結指向的是影片檔")
        length = int(resp.headers.get("Content-Length") or 0)
        if length > MAX_IMPORT_BYTES:
            raise ValueError("檔案超過 4 GB，請先用切段程式切成短片")
        name = _filename_from(resp, "cloud_video.mp4")
        res = store.save_upload(name, _Capped(resp, MAX_IMPORT_BYTES), length or MAX_IMPORT_BYTES)
    if res["size"] == 0:
        raise ValueError("下載到的檔案是空的")
    res.update({"name": name, "source": "cloud", "provider": provider})
    return res


# ---------------------------------------------------------------------------
# 長片收件匣（給切段程式處理）
# ---------------------------------------------------------------------------
def save_long(filename: str, stream, length: int) -> dict:
    store.require_user()
    inbox = long_inbox()
    os.makedirs(inbox, exist_ok=True)
    base = store.safe_name(filename)
    stem, ext = os.path.splitext(base)
    path, i = os.path.join(inbox, base), 1
    while os.path.exists(path):
        path = os.path.join(inbox, f"{stem}_{i}{ext}")
        i += 1
    remaining = length
    with open(path, "wb") as f:
        while remaining > 0:
            chunk = stream.read(min(1 << 20, remaining))
            if not chunk:
                break
            f.write(chunk)
            remaining -= len(chunk)
    return {"name": os.path.basename(path), "size": os.path.getsize(path), "inbox": inbox}


def list_longs() -> dict:
    inbox = long_inbox()
    items = []
    if os.path.isdir(inbox):
        for f in sorted(os.listdir(inbox)):
            full = os.path.join(inbox, f)
            if os.path.isfile(full) and _is_video(f):
                st = os.stat(full)
                items.append({"name": f, "size": st.st_size, "modified": _stamp(st.st_mtime)})
    return {"inbox": inbox, "items": items}

