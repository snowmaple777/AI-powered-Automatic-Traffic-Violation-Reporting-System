"""資料存放：上傳檔、分析工作（job）、檢舉案件（case）、檢舉人資料。

全部存在 ui/data/ 底下（已列入 .gitignore）。
案件資料夾格式與 autoForm 相同（info.txt + 佐證影片），之後可直接
    python autoForm/main.py --city <縣市代碼> --case-dir ui/data/cases/<案件>
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import random
import shutil
import threading
import time
import traceback
import uuid
import zipfile
from datetime import datetime, timedelta

from . import geo, gps_parser
from .analyzers import ANALYZERS, JobCancelled, fmt_ts
from .remote import RemoteAnalyzer

UI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_DIR = os.path.dirname(UI_DIR)
DATA_DIR = os.path.join(UI_DIR, "data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
JOB_DIR = os.path.join(DATA_DIR, "jobs")
CASE_DIR = os.path.join(DATA_DIR, "cases")
PROFILE_PATH = os.path.join(DATA_DIR, "reporter_profile.json")
DRAFT_DIR = os.path.join(DATA_DIR, "drafts")
# autoForm：master 分支放在 reporting/autoForm，舊的 main 分支放在 autoForm
AUTOFORM_DIR = next((d for d in (os.path.join(PROJECT_DIR, "reporting", "autoForm"),
                                 os.path.join(PROJECT_DIR, "autoForm")) if os.path.isdir(d)),
                    os.path.join(PROJECT_DIR, "reporting", "autoForm"))
AUTOFORM_PROFILE = os.path.join(AUTOFORM_DIR, "reporter_profile.json")
CONFIG_PATH = os.path.join(UI_DIR, "config.json")


def load_config():
    """ui/config.json（可選）＞ 環境變數 ＞ 預設值。推論服務搬上雲端時只要改 inference_url。"""
    cfg = {"inference_url": os.environ.get("INFERENCE_URL", "http://127.0.0.1:8770"),
           "inference_api_key": os.environ.get("INFERENCE_API_KEY", ""),
           # 自動填表要開看得到的瀏覽器，所以永遠是「這台電腦」上的推論服務；
           # 推論搬上雲端後，這兩個仍指向本機。
           "autoform_url": os.environ.get("AUTOFORM_URL", ""),
           "autoform_api_key": os.environ.get("AUTOFORM_API_KEY", "")}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items() if v is not None})
    except (OSError, ValueError):
        pass
    if not cfg["autoform_url"]:
        cfg["autoform_url"] = cfg["inference_url"]
        cfg["autoform_api_key"] = cfg["autoform_api_key"] or cfg["inference_api_key"]
    return cfg


CONFIG = load_config()

PROFILE_DIR = os.path.join(DATA_DIR, "profiles")
ACCOUNTS_PATH = os.path.join(DATA_DIR, "accounts.json")

for d in (UPLOAD_DIR, JOB_DIR, CASE_DIR, DRAFT_DIR, PROFILE_DIR):
    os.makedirs(d, exist_ok=True)


# ---------------------------------------------------------------------------
# 帳號隔離：每筆草稿／分析／案件／上傳都記下 owner（帳號 Email），只有本人看得到。
# server.py 每個請求一開始會呼叫 set_user()，這裡用 thread-local 記住目前是誰。
# 加上帳號隔離之前留下的舊資料沒有 owner，歸給「最早註冊的帳號」。
# ---------------------------------------------------------------------------
_ctx = threading.local()


def set_user(email):
    _ctx.user = (email or "").lower() or None


def current_user():
    return getattr(_ctx, "user", None)


def require_user():
    u = current_user()
    if not u:
        raise PermissionError("請先登入")
    return u


def legacy_owner():
    try:
        with open(ACCOUNTS_PATH, "r", encoding="utf-8") as f:
            accounts = json.load(f)
    except (OSError, ValueError):
        return None
    if not accounts:
        return None
    return min(accounts, key=lambda e: (accounts[e].get("created") or "", e))


def owns(rec) -> bool:
    u = current_user()
    if not u or not rec:
        return False
    owner = rec.get("owner")
    return owner == u if owner else u == legacy_owner()


def _owner_file(upload_name):
    return os.path.join(UPLOAD_DIR, safe_name(upload_name) + ".owner")


def owns_upload(name) -> bool:
    try:
        with open(_owner_file(name), "r", encoding="utf-8") as f:
            return owns({"owner": f.read().strip()})
    except OSError:
        return owns({})


def owns_file(kind, item_id) -> bool:
    """/files/<kind>/<id>/… 的權限檢查。"""
    if kind == "uploads":
        return owns_upload(item_id)
    if kind == "jobs":
        return owns(_read_json(os.path.join(JOB_DIR, safe_name(item_id), "job.json")))
    if kind == "cases":
        return owns(_read_json(os.path.join(CASE_DIR, safe_name(item_id), "case.json")))
    return False

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
    user = require_user()
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
    with open(_owner_file(name), "w", encoding="utf-8") as f:
        f.write(user)
    return {"video_id": name, "size": os.path.getsize(path), "url": f"/files/uploads/{name}"}


# ---------------------------------------------------------------------------
# 分析工作
# ---------------------------------------------------------------------------
def _public_job(job):
    keys = ("id", "status", "created", "finished", "video_name", "video_url", "video_meta", "stages",
            "current_stage", "logs", "violations", "error", "analyzer", "gps", "recorded_at", "preview_version",
            "stats", "found", "started_ts", "options", "fps", "eta_sec", "remote")
    return {k: job.get(k) for k in keys}


def create_job(payload: dict) -> dict:
    video_id = safe_name(payload["video_id"])
    video_path = os.path.join(UPLOAD_DIR, video_id)
    if not os.path.exists(video_path) or not owns_upload(video_id):
        raise FileNotFoundError("找不到上傳的影片，請重新上傳")
    analyzer = payload.get("analyzer", "simulated")
    if analyzer not in ANALYZERS:
        raise ValueError(f"不支援的分析模式：{analyzer}")
    analyzer_cls = ANALYZERS[analyzer]
    opts = payload.get("options") or {}
    jid = datetime.now().strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:4]
    jdir = os.path.join(JOB_DIR, jid)
    os.makedirs(jdir, exist_ok=True)
    gps = payload.get("gps") or {}
    track = gps_parser.track_from_json(gps.get("track")) if gps.get("track") else []
    if not track and gps.get("lat") is not None:
        track = [{"lat": float(gps["lat"]), "lon": float(gps["lon"]), "t": 0.0, "time": None}]
    job = {
        "id": jid, "owner": require_user(), "dir": jdir, "status": "queued", "created": _now(), "finished": None,
        "video_path": video_path, "video_name": video_id.split("_", 1)[-1],
        "video_url": f"/files/uploads/{video_id}", "video_meta": None,
        "analyzer": analyzer,
        "speed": float(payload.get("speed", 1.0)),
        "options": {"max_seconds": float(opts["max_seconds"]) if opts.get("max_seconds") else None,
                    "rules": opts.get("rules") or "all"},
        "cancel": False, "fps": None, "eta_sec": None, "remote": None,
        "recorded_at": payload.get("recorded_at"),
        "gps": {"mode": gps.get("mode", "point"), "points": len(track),
                "lat": track[0]["lat"] if track else None, "lon": track[0]["lon"] if track else None},
        "gps_track": track,
        "stages": [{"key": k, "label": l, "weight": w, "progress": 0.0, "state": "pending"}
                   for k, l, w in analyzer_cls.STAGES],
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

    def report(stage, progress, message=None, preview=None):
        idx = order.index(stage)
        for i, s in enumerate(job["stages"]):
            if i < idx:
                s["state"], s["progress"] = "done", 1.0
            elif i == idx:
                s["state"], s["progress"] = ("done" if progress >= 1 else "running"), min(1.0, progress)
        job["current_stage"] = stage
        if message:
            job["logs"].append({"time": datetime.now().strftime("%H:%M:%S"), "stage": stage, "msg": message})
        if preview:
            job["preview_version"] += 1
        if time.time() - last_save[0] > 1.0:  # 分析中途也存檔，重新整理或重開都還看得到進度
            last_save[0] = time.time()
            _save_job(job)

    try:
        if job["analyzer"] == "remote":
            analyzer = RemoteAnalyzer(CONFIG["inference_url"], CONFIG.get("inference_api_key", ""))
        else:
            analyzer = ANALYZERS["simulated"](speed=job["speed"])
        events = analyzer.analyze(job, report)
        _enrich(job, events)
        job["violations"] = events
        for s in job["stages"]:
            s["state"], s["progress"] = "done", 1.0
        job["status"] = "done"
    except JobCancelled:
        job["status"], job["error"] = "cancelled", "已取消分析"
        job["logs"].append({"time": datetime.now().strftime("%H:%M:%S"),
                            "stage": job.get("current_stage") or "", "msg": "已取消分析"})
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
        return _public_job(job) if owns(job) else None
    path = os.path.join(JOB_DIR, safe_name(jid), "job.json")
    data = _read_json(path)
    if not data or not owns(data):
        return None
    if data.get("status") in ("queued", "running"):
        # 記憶體裡沒有、檔案卻顯示進行中 → 伺服器曾經關閉，分析被中斷
        data["status"] = "interrupted"
        data["error"] = "分析過程中 UI 伺服器被關閉，請重新分析"
        _write_json(path, data)
        remote = data.get("remote") or {}
        if remote.get("job_id"):  # 推論服務那邊可能還在跑，順手取消，避免白白占用 GPU
            threading.Thread(target=_cancel_remote, args=(remote,), daemon=True).start()
    return _public_job(data)


def _cancel_remote(remote):
    try:
        RemoteAnalyzer(remote.get("url") or CONFIG["inference_url"],
                       CONFIG.get("inference_api_key", ""))._req("POST", f"/jobs/{remote['job_id']}/cancel", timeout=5)
    except Exception:  # noqa: BLE001
        pass


def cancel_job(jid):
    with _lock:
        job = _jobs.get(safe_name(jid))
    if not job or not owns(job):
        raise FileNotFoundError("找不到分析紀錄，或分析已結束")
    if job.get("status") in ("queued", "running"):
        job["cancel"] = True
    return _public_job(job)


def inference_health():
    """給前端顯示推論服務狀態。"""
    analyzer = RemoteAnalyzer(CONFIG["inference_url"], CONFIG.get("inference_api_key", ""))
    try:
        h = analyzer.health(timeout=2)
    except Exception as e:  # noqa: BLE001
        return {"online": False, "url": CONFIG["inference_url"], "error": str(e)}
    models = h.get("models") or []
    ready = [m for m in models if m.get("exists") or not m.get("enabled", True)]
    return {"online": True, "url": CONFIG["inference_url"], "gpu": h.get("gpu"), "busy": h.get("busy"),
            "queue_length": h.get("queue_length"), "ffmpeg": h.get("ffmpeg"),
            "models_ready": len(ready), "models_total": len(models),
            "models_missing": [m["path"] for m in models if m not in ready]}


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
    if old and not owns(old):
        old = None
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
        "id": cid, "owner": require_user(), "created": old["created"] if old else _now(), "updated": _now(), "status": "draft", "job_id": job["id"], "violation": ev,
        "fields": fields, "city": city, "city_code": geo.CITY_CODES.get(city, "kaohsiung"),
        "files": sorted(os.listdir(cdir)), "submit": None,
    }
    _write_json(os.path.join(cdir, "case.json"), case)
    return _public_case(case)


def _public_case(case):
    c = dict(case)
    c["base_url"] = f"/files/cases/{case['id']}/"
    rel = os.path.relpath(os.path.join(CASE_DIR, case["id"]), AUTOFORM_DIR).replace("/", "\\")
    c["autoform_cmd"] = (f'cd {os.path.relpath(AUTOFORM_DIR, PROJECT_DIR)} && python main.py '
                         f'--city {case["city_code"]} --case-dir "{rel}"')
    return c


def get_case(cid):
    data = _read_json(os.path.join(CASE_DIR, safe_name(cid), "case.json"))
    return _public_case(data) if data and owns(data) else None


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
    if not case or not owns(case):
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


# ---------------------------------------------------------------------------
# 真的自動填表：交給推論服務執行組員的 autoForm（會在這台電腦開瀏覽器）
# ---------------------------------------------------------------------------
def _autoform_client():
    return RemoteAnalyzer(CONFIG["autoform_url"], CONFIG.get("autoform_api_key", ""))


def _case_or_404(cid):
    case = _read_json(os.path.join(CASE_DIR, safe_name(cid), "case.json"))
    if not case or not owns(case):
        raise FileNotFoundError("找不到案件")
    return case


def start_autofill(cid, payload):
    case = _case_or_404(cid)
    test = bool(payload.get("test", True))
    if case.get("status") == "submitted":
        raise ValueError("這個案件已經送出過了")
    if not test and not payload.get("confirm"):
        raise ValueError("正式送出需要再次確認")
    profile = get_profile()
    if not test and not all(profile.get(k) for k in ("reporter_name", "reporter_id", "reporter_phone")):
        raise ValueError("正式送出前，請先到「檢舉人資料」填好姓名、身分證和電話")
    cdir = os.path.join(CASE_DIR, case["id"])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in os.listdir(cdir):
            if name != "case.json":
                z.write(os.path.join(cdir, name), arcname=f"{case['id']}/{name}")
        # 一定附上這個帳號的檢舉人資料（就算是空的），避免 autoForm 改用別人的預設檔
        z.writestr("reporter_profile.json", json.dumps(profile, ensure_ascii=False))
    data = buf.getvalue()
    client = _autoform_client()
    query = urllib.parse.urlencode({"city": case.get("city_code", "kaohsiung"), "test": "1" if test else "0",
                                    "name": case["id"]})
    req = urllib.request.Request(client.base + "/reports?" + query, data=data, method="POST",
                                 headers=dict(client._headers(), **{"Content-Type": "application/zip"}))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            rep = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error")
        except Exception:  # noqa: BLE001
            msg = str(e)
        raise ValueError(f"無法開始填表：{msg}") from None
    except OSError:
        raise ValueError("連不到推論服務，請先開「啟動推論服務.cmd」") from None
    case["autofill"] = {"report_id": rep["id"], "test": test, "started": _now(), "url": CONFIG["autoform_url"]}
    _write_json(os.path.join(cdir, "case.json"), case)
    return {"case": _public_case(case), "report": rep}


def autofill_status(cid, action=None):
    case = _case_or_404(cid)
    af = case.get("autofill")
    if not af:
        raise FileNotFoundError("這個案件還沒啟動自動填表")
    client = RemoteAnalyzer(af.get("url") or CONFIG["autoform_url"], CONFIG.get("autoform_api_key", ""))
    path = f"/reports/{af['report_id']}" + (f"/{action}" if action else "")
    try:
        rep = client._req("POST" if action else "GET", path, timeout=10)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"取得填表狀態失敗：{e}") from None
    changed = False
    if rep.get("state") in ("done", "error", "cancelled") and af.get("final") != rep["state"]:
        af["final"] = rep["state"]
        af["message"] = rep.get("message", "")
        changed = True
        if rep["state"] == "done" and not af.get("test"):
            case["status"] = "submitted"
            case["submit"] = {"time": _now(), "simulated": False, "via": "autoForm", "receipt": None,
                              "confirmed": bool(rep.get("success")), "message": rep.get("message", "")}
        if rep["state"] == "done" and af.get("test"):
            af["tested"] = _now()
    if changed:
        _write_json(os.path.join(CASE_DIR, case["id"], "case.json"), case)
    return {"case": _public_case(case), "report": rep}


def autofill_health():
    try:
        h = _autoform_client().health(timeout=3)
    except Exception as e:  # noqa: BLE001
        return {"online": False, "available": False, "hint": "連不到推論服務，請先開「啟動推論服務.cmd」", "error": str(e)}
    af = h.get("autoform")
    if af is None:
        return {"online": True, "available": False, "hint": "推論服務版本太舊，請更新 inference_service"}
    return dict(af, online=True)


def zip_case(cid) -> str:
    cdir = os.path.join(CASE_DIR, safe_name(cid))
    if not os.path.isdir(cdir) or not owns(_read_json(os.path.join(cdir, "case.json"))):
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
              "job_id", "ignored", "filter", "selected", "forms", "cases", "mode", "max_seconds")


def _draft_path(did):
    return os.path.join(DRAFT_DIR, safe_name(did) + ".json")


def create_draft(payload=None):
    did = datetime.now().strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:4]
    draft = {"id": did, "owner": require_user(), "created": _now(), "updated": _now(), "step": 1, "max_step": 1,
             "ignored": [], "filter": "all", "forms": {}, "cases": {}}
    for k in DRAFT_KEYS:
        if payload and k in payload:
            draft[k] = payload[k]
    _write_json(_draft_path(did), draft)
    return draft


def get_draft(did):
    d = _read_json(_draft_path(did))
    return d if owns(d) else None


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
    if os.path.exists(path) and get_draft(did):
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
        if c and owns(c) and c.get("status") == "submitted":
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
            if d and d.get("video") and owns(d):  # 連影片都沒上傳的草稿不列出
                out.append(_draft_summary(d))
    out.sort(key=lambda x: x.get("updated") or "", reverse=True)
    return out


# ---------------------------------------------------------------------------
# 檢舉人資料
# ---------------------------------------------------------------------------
PROFILE_KEYS = ["reporter_name", "reporter_id", "reporter_phone", "reporter_email", "reporter_address"]


def _profile_path(user):
    return os.path.join(PROFILE_DIR, hashlib.sha1(user.encode("utf-8")).hexdigest()[:16] + ".json")


def get_profile():
    """每個帳號一份。舊的共用資料（ui/data/reporter_profile.json、autoForm 的設定檔）只給最早註冊的帳號。"""
    user = require_user()
    data = _read_json(_profile_path(user))
    if data is None and user == legacy_owner():
        data = _read_json(PROFILE_PATH) or _read_json(AUTOFORM_PROFILE)
    data = data or {}
    return {k: data.get(k, "") for k in PROFILE_KEYS}


def save_profile(data):
    clean = {k: str(data.get(k, "")).strip() for k in PROFILE_KEYS}
    _write_json(_profile_path(require_user()), clean)
    return clean


# ---------------------------------------------------------------------------
# 系統狀態
# ---------------------------------------------------------------------------
