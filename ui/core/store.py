"""資料存放：上傳檔、分析工作（job）、檢舉案件（case）、檢舉人資料。

全部存在 ui/data/ 底下（已列入 .gitignore）。
案件資料夾格式與 autoForm 相同（info.txt + 佐證影片），之後可直接
    python autoForm/main.py --city <縣市代碼> --case-dir ui/data/cases/<案件>
"""
from __future__ import annotations

import json
import os
import random
import shutil
import threading
import time
import traceback
import uuid
import zipfile
from datetime import datetime, timedelta

from . import geo, gps_parser
from .analyzers import ANALYZERS, STAGES, fmt_ts

UI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_DIR = os.path.dirname(UI_DIR)
DATA_DIR = os.path.join(UI_DIR, "data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
JOB_DIR = os.path.join(DATA_DIR, "jobs")
CASE_DIR = os.path.join(DATA_DIR, "cases")
PROFILE_PATH = os.path.join(DATA_DIR, "reporter_profile.json")
DRAFT_DIR = os.path.join(DATA_DIR, "drafts")
AUTOFORM_PROFILE = os.path.join(PROJECT_DIR, "autoForm", "reporter_profile.json")

for d in (UPLOAD_DIR, JOB_DIR, CASE_DIR, DRAFT_DIR):
    os.makedirs(d, exist_ok=True)

_jobs: dict = {}
_lock = threading.Lock()


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def safe_name(name: str) -> str:
    keep = "".join(c for c in os.path.basename(name) if c.isalnum() or c in "._-")
    return keep or "video.mp4"


# ---------------------------------------------------------------------------
# 上傳
# ---------------------------------------------------------------------------
def save_upload(filename: str, stream, length: int) -> dict:
    vid = uuid.uuid4().hex[:10]
    name = f"{vid}_{safe_name(filename)}"
    path = os.path.join(UPLOAD_DIR, name)
    remaining = length
    with open(path, "wb") as f:
        while remaining > 0:
            chunk = stream.read(min(1 << 20, remaining))
            if not chunk:
                break
            f.write(chunk)
            remaining -= len(chunk)
    return {"video_id": name, "size": os.path.getsize(path), "url": f"/files/uploads/{name}"}


# ---------------------------------------------------------------------------
# 分析工作
# ---------------------------------------------------------------------------
def _public_job(job):
    keys = ("id", "status", "created", "finished", "video_name", "video_url", "video_meta", "stages",
            "current_stage", "logs", "violations", "error", "analyzer", "gps", "recorded_at", "preview_version",
            "stats", "found", "started_ts")
    return {k: job.get(k) for k in keys}


def create_job(payload: dict) -> dict:
    video_id = safe_name(payload["video_id"])
    video_path = os.path.join(UPLOAD_DIR, video_id)
    if not os.path.exists(video_path):
        raise FileNotFoundError("找不到上傳的影片，請重新上傳")
    jid = datetime.now().strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:4]
    jdir = os.path.join(JOB_DIR, jid)
    os.makedirs(jdir, exist_ok=True)
    gps = payload.get("gps") or {}
    track = gps_parser.track_from_json(gps.get("track")) if gps.get("track") else []
    if not track and gps.get("lat") is not None:
        track = [{"lat": float(gps["lat"]), "lon": float(gps["lon"]), "t": 0.0, "time": None}]
    job = {
        "id": jid, "dir": jdir, "status": "queued", "created": _now(), "finished": None,
        "video_path": video_path, "video_name": video_id.split("_", 1)[-1],
        "video_url": f"/files/uploads/{video_id}", "video_meta": None,
        "analyzer": payload.get("analyzer", "simulated"),
        "speed": float(payload.get("speed", 1.0)),
        "recorded_at": payload.get("recorded_at"),
        "gps": {"mode": gps.get("mode", "point"), "points": len(track),
                "lat": track[0]["lat"] if track else None, "lon": track[0]["lon"] if track else None},
        "gps_track": track,
        "stages": [{"key": k, "label": l, "progress": 0.0, "state": "pending"} for k, l in STAGES],
        "current_stage": None, "logs": [], "violations": [], "error": None, "preview_version": 0,
        "stats": {}, "found": [], "started_ts": time.time(),
    }
    with _lock:
        _jobs[jid] = job
    _save_job(job)
    threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    return _public_job(job)


def _save_job(job):
    save = {k: v for k, v in job.items() if k != "gps_track"}
    save["gps_track"] = gps_parser.track_to_json(job.get("gps_track") or [])
    try:
        _write_json(os.path.join(job["dir"], "job.json"), save)
    except (OSError, TypeError, ValueError):
        traceback.print_exc()


def _run_job(job):
    job["status"] = "running"
    order = [s["key"] for s in job["stages"]]
    last_save = [0.0]

    def report(stage, progress, message, preview=None):
        idx = order.index(stage)
        for i, s in enumerate(job["stages"]):
            if i < idx:
                s["state"], s["progress"] = "done", 1.0
            elif i == idx:
                s["state"], s["progress"] = ("done" if progress >= 1 else "running"), min(1.0, progress)
        job["current_stage"] = stage
        job["logs"].append({"time": datetime.now().strftime("%H:%M:%S"), "stage": stage, "msg": message})
        if preview:
            job["preview_version"] += 1
        if time.time() - last_save[0] > 1.0:  # 分析中途也存檔，重新整理或重開都還看得到進度
            last_save[0] = time.time()
            _save_job(job)

    try:
        analyzer_cls = ANALYZERS.get(job["analyzer"], ANALYZERS["simulated"])
        analyzer = analyzer_cls(speed=job["speed"]) if job["analyzer"] == "simulated" else analyzer_cls()
        events = analyzer.analyze(job, report)
        _enrich(job, events)
        job["violations"] = events
        for s in job["stages"]:
            s["state"], s["progress"] = "done", 1.0
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        job["status"], job["error"] = "error", str(e)
    job["finished"] = _now()
    _save_job(job)


def _parse_recorded_at(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=gps_parser.TW)
        return dt
    except ValueError:
        return None


def _enrich(job, events):
    """為每筆違規補上：發生時間、GPS 位置、縣市行政區。"""
    start = _parse_recorded_at(job.get("recorded_at")) or datetime.now(gps_parser.TW)
    for ev in events:
        when = start + timedelta(seconds=ev["t"])
        ev["datetime"] = when.strftime("%Y-%m-%d %H:%M:%S")
        ev["date"], ev["time"] = when.strftime("%Y-%m-%d"), when.strftime("%H:%M")
        ev["ts_label"] = fmt_ts(ev["t"])
        pos = gps_parser.position_at(job.get("gps_track"), ev["t"], start)
        if pos:
            ev["lat"], ev["lon"] = round(pos["lat"], 6), round(pos["lon"], 6)
            ev["geo"] = geo.reverse_geocode(pos["lat"], pos["lon"])
        else:
            ev["lat"] = ev["lon"] = None
            ev["geo"] = None
        base = f"/files/jobs/{job['id']}/"
        ev["clip_url"] = base + ev["clip"] if ev.get("clip") else None
        ev["snapshot_url"] = base + ev["snapshot"] if ev.get("snapshot") else None


def get_job(jid):
    with _lock:
        job = _jobs.get(jid)
    if job:
        return _public_job(job)
    path = os.path.join(JOB_DIR, safe_name(jid), "job.json")
    data = _read_json(path)
    if not data:
        return None
    if data.get("status") in ("queued", "running"):
        # 記憶體裡沒有、檔案卻顯示進行中 → 伺服器曾經關閉，分析被中斷
        data["status"] = "interrupted"
        data["error"] = "分析過程中伺服器被關閉，請重新分析"
        _write_json(path, data)
    return _public_job(data)


def list_jobs():
    out = []
    for jid in sorted(os.listdir(JOB_DIR), reverse=True):
        j = get_job(jid)
        if j:
            item = {k: j.get(k) for k in ("id", "status", "created", "video_name")}
            item["violations"] = len(j.get("violations") or [])
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# 檢舉案件
# ---------------------------------------------------------------------------
INFO_KEYS = [
    ("違規日期", "date"), ("違規時間", "time"), ("違規行政區", "district"),
    ("交通違規地點", "location"), ("車牌號碼", "plate"), ("違規車種類型", "vehicle_type"),
    ("違規類別", "category"), ("違規事實", "fact"), ("違規事實敘述", "description"),
]


def create_case(payload: dict) -> dict:
    job = get_job(payload["job_id"])
    if not job:
        raise FileNotFoundError("找不到分析紀錄")
    ev = next((v for v in job["violations"] if v["id"] == payload["violation_id"]), None)
    if not ev:
        raise FileNotFoundError("找不到違規事件")
    fields = payload.get("fields") or {}
    city = fields.get("city") or (ev.get("geo") or {}).get("city", "")
    old = _read_json(os.path.join(CASE_DIR, safe_name(payload.get("case_id") or "-"), "case.json"))
    if old and old.get("status") == "submitted":
        return _public_case(old)  # 已送出的案件不再修改
    cid = old["id"] if old else "case_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + ev["id"]
    cdir = os.path.join(CASE_DIR, cid)
    os.makedirs(cdir, exist_ok=True)
    jdir = os.path.join(JOB_DIR, job["id"])
    for key in ("clip", "snapshot"):
        if ev.get(key):
            src = os.path.join(jdir, ev[key])
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(cdir, "evidence" + os.path.splitext(ev[key])[1]))
    with open(os.path.join(cdir, "info.txt"), "w", encoding="utf-8") as f:
        for zh, k in INFO_KEYS:
            f.write(f"{zh}: {str(fields.get(k, '')).strip()}\n")
    case = {
        "id": cid, "created": old["created"] if old else _now(), "updated": _now(), "status": "draft", "job_id": job["id"], "violation": ev,
        "fields": fields, "city": city, "city_code": geo.CITY_CODES.get(city, "kaohsiung"),
        "files": sorted(os.listdir(cdir)), "submit": None,
    }
    _write_json(os.path.join(cdir, "case.json"), case)
    return _public_case(case)


def _public_case(case):
    c = dict(case)
    c["base_url"] = f"/files/cases/{case['id']}/"
    c["autoform_cmd"] = (f'cd autoForm && python main.py --city {case["city_code"]} '
                         f'--case-dir "..\\ui\\data\\cases\\{case["id"]}"')
    return c


def get_case(cid):
    data = _read_json(os.path.join(CASE_DIR, safe_name(cid), "case.json"))
    return _public_case(data) if data else None


def list_cases():
    out = []
    for cid in sorted(os.listdir(CASE_DIR), reverse=True):
        c = get_case(cid)
        if c:
            out.append(c)
    return out


def submit_case(cid, payload):
    """模擬 autoForm 送出：產生受理編號並記錄時間。"""
    case = _read_json(os.path.join(CASE_DIR, safe_name(cid), "case.json"))
    if not case:
        raise FileNotFoundError("找不到案件")
    time.sleep(0.5)
    case["status"] = "submitted"
    case["submit"] = {
        "time": _now(), "simulated": True,
        "receipt": f"SIM{datetime.now().strftime('%Y%m%d')}{random.randint(10000, 99999)}",
        "verify": payload.get("verify", "captcha"),
    }
    _write_json(os.path.join(CASE_DIR, case["id"], "case.json"), case)
    return _public_case(case)


def zip_case(cid) -> str:
    cdir = os.path.join(CASE_DIR, safe_name(cid))
    if not os.path.isdir(cdir):
        raise FileNotFoundError("找不到案件")
    path = os.path.join(DATA_DIR, f"{safe_name(cid)}.zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in os.listdir(cdir):
            if name != "case.json":
                z.write(os.path.join(cdir, name), arcname=f"{safe_name(cid)}/{name}")
    return path


# ---------------------------------------------------------------------------
# 草稿（每一次檢舉流程的進度，自動儲存）
# ---------------------------------------------------------------------------
DRAFT_KEYS = ("step", "max_step", "video", "gps", "gps_tab", "preset_index", "recorded_at", "speed",
              "job_id", "ignored", "filter", "selected", "forms", "cases")


def _draft_path(did):
    return os.path.join(DRAFT_DIR, safe_name(did) + ".json")


def create_draft(payload=None):
    did = datetime.now().strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:4]
    draft = {"id": did, "created": _now(), "updated": _now(), "step": 1, "max_step": 1,
             "ignored": [], "filter": "all", "forms": {}, "cases": {}}
    for k in DRAFT_KEYS:
        if payload and k in payload:
            draft[k] = payload[k]
    _write_json(_draft_path(did), draft)
    return draft


def get_draft(did):
    return _read_json(_draft_path(did))


def update_draft(did, patch):
    with _lock:
        draft = get_draft(did)
        if not draft:
            raise FileNotFoundError("找不到草稿")
        for k in DRAFT_KEYS:
            if k in patch:
                draft[k] = patch[k]
        draft["updated"] = _now()
        _write_json(_draft_path(did), draft)
    return {"id": did, "updated": draft["updated"]}


def delete_draft(did):
    path = _draft_path(did)
    if os.path.exists(path):
        os.remove(path)
    return {"deleted": did}


def _draft_summary(d):
    job = get_job(d["job_id"]) if d.get("job_id") else None
    vios = (job or {}).get("violations") or []
    ignored = set(d.get("ignored") or [])
    case_ids = d.get("cases") or {}
    submitted = 0
    for vid, cid in case_ids.items():
        c = _read_json(os.path.join(CASE_DIR, safe_name(cid), "case.json"))
        if c and c.get("status") == "submitted":
            submitted += 1
    pending = len([v for v in vios if v["id"] not in ignored]) - submitted
    finished = bool(job and job.get("status") == "done" and pending <= 0)
    return {
        "id": d["id"], "created": d.get("created"), "updated": d.get("updated"), "step": d.get("step", 1),
        "video_name": (d.get("video") or {}).get("name"), "job_status": (job or {}).get("status"),
        "violations": len(vios), "ignored": len(ignored), "submitted": submitted,
        "pending": max(0, pending), "finished": finished,
        "thumb": next((v.get("snapshot_url") for v in vios if v.get("snapshot_url")), None),
        "place": ((d.get("gps") or {}).get("geo") or {}).get("address_hint"),
    }


def list_drafts():
    out = []
    for name in sorted(os.listdir(DRAFT_DIR), reverse=True):
        if name.endswith(".json"):
            d = _read_json(os.path.join(DRAFT_DIR, name))
            if d and d.get("video"):  # 連影片都沒上傳的草稿不列出
                out.append(_draft_summary(d))
    out.sort(key=lambda x: x.get("updated") or "", reverse=True)
    return out


# ---------------------------------------------------------------------------
# 檢舉人資料
# ---------------------------------------------------------------------------
PROFILE_KEYS = ["reporter_name", "reporter_id", "reporter_phone", "reporter_email", "reporter_address"]


def get_profile():
    data = _read_json(PROFILE_PATH) or _read_json(AUTOFORM_PROFILE) or {}
    return {k: data.get(k, "") for k in PROFILE_KEYS}


def save_profile(data):
    clean = {k: str(data.get(k, "")).strip() for k in PROFILE_KEYS}
    _write_json(PROFILE_PATH, clean)
    return clean


# ---------------------------------------------------------------------------
# 系統狀態
# ---------------------------------------------------------------------------
def system_status():
    from .analyzers import cv2
    ck = os.path.join(PROJECT_DIR, "perception", "checkpoints")
    models = []
    for name in ("yolo26s.onnx", "license-plate-finetune-v1s.onnx",
                 "depth_anything_v2_metric_vkitti_vits.onnx", "PP-OCRv6_taiwan_infer"):
        models.append({"name": name, "found": os.path.exists(os.path.join(ck, name))})
    return {
        "analyzer": "simulated",
        "opencv": getattr(cv2, "__version__", None) if cv2 else None,
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "models": models,
        "modules": {
            "perception": os.path.isdir(os.path.join(PROJECT_DIR, "perception")),
            "traffic_ai": os.path.isdir(os.path.join(PROJECT_DIR, "traffic_ai")),
            "road_marking": os.path.isdir(os.path.join(PROJECT_DIR, "road_marking")),
            "traffic_light": os.path.isdir(os.path.join(PROJECT_DIR, "traffic-light-model")),
            "autoForm": os.path.isdir(os.path.join(PROJECT_DIR, "autoForm")),
        },
        "data_dir": DATA_DIR,
    }
