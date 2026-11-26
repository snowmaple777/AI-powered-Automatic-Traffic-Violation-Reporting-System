"""長片自動切段（4bu）：上傳到 <片段庫>/_待切分/ 的長片，交給推論服務以降頻篩選粗略掃過，
疑似違規的前後（前 8 秒、後 6 秒）剪成原始短片（不畫標註，之後要再分析），存到 <片段庫>/<長片檔名>/，
就會出現在「已切好的片段」。切完的長片移到 <片段庫>/_已切分/（底線開頭，不出現在片段清單）。

- 切段紀錄：<片段庫>/_切段紀錄/<長片檔名>.json（狀態、進度、剩餘時間、正在逐幀確認的時段、片段數）。
- 一次只切一支（推論服務也只有一張 GPU），其餘排隊；介面伺服器重開時，排隊中的會接著切，切到一半的標成中斷、可重試。
- 需要真實模型（推論服務）；沒有開推論服務時標成失敗並說明原因。
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import time
import traceback
from datetime import datetime

from . import clips, store
from .analyzers import VIOLATION_TYPES
from .remote import RULE_TYPES, RemoteAnalyzer

RECORD_DIR = "_切段紀錄"
DONE_DIR = "_已切分"
TRIAGE_FPS = 10              # 降頻篩選幀率（4bs：每秒 10 幀，確認事件不變）
CLIP_BEFORE, CLIP_AFTER = 8.0, 6.0  # 4bo：疑似事件前 8 後 6 秒的片段可重現整片的事件
RULE_PROFILE = "high_recall"  # 4by：切段只是粗篩、片段之後會再人工看，用高召回規則組合（漏報較少、誤報較多）
ACTIVE = ("queued", "uploading", "cutting", "saving")
POLL_SECONDS = 1.5
REPLACE_RETRIES = 30  # 切段紀錄被別的程式開著時，最多等約 3 秒

_lock = threading.RLock()
_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
_cancel: set = set()
_worker = None


class _Cancelled(Exception):
    pass


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _user_dir(email):
    d = os.path.join(clips.clips_root(), clips.user_folder(email))
    os.makedirs(d, exist_ok=True)
    return d


def _record_path(email, name):
    return os.path.join(_user_dir(email), RECORD_DIR, name + ".json")


def _read(email, name):
    # 與寫入同一把鎖：Windows 上檔案被讀取時 os.replace 會被拒（WinError 5）
    with _lock:
        try:
            with open(_record_path(email, name), "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None


def _write(email, rec):
    path = _record_path(email, rec["name"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    # 別的程式（防毒、索引、使用者開著看）短暫開著紀錄檔時，Windows 會拒絕取代（WinError 5）：稍等重試，
    # 不要因為一次進度寫不進去就讓整支長片切段失敗。
    for attempt in range(REPLACE_RETRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == REPLACE_RETRIES - 1:
                raise
            time.sleep(0.1)


def _update(email, name, **fields):
    with _lock:
        rec = _read(email, name) or {"name": name}
        rec.update(fields)
        _write(email, rec)
        return rec


def _unique(path):
    stem, ext = os.path.splitext(path)
    out, i = path, 2
    while os.path.exists(out):
        out, i = f"{stem}_{i}{ext}", i + 1
    return out


def _safe_name(name):
    base = os.path.basename(name or "")
    if not base or base.startswith(".") or base != name:
        raise FileNotFoundError("找不到這支長片")
    return base


# ---------------------------------------------------------------------------
# 給 HTTP 請求用（目前登入的帳號）
# ---------------------------------------------------------------------------
def enqueue(name):
    """排進切段佇列（上傳完成自動呼叫；失敗、取消或舊的長片可再按「開始切段」）。"""
    email = store.require_user()
    name = _safe_name(name)
    path = os.path.join(clips.long_inbox(), name)
    if not os.path.isfile(path):
        raise FileNotFoundError("找不到這支長片")
    st = os.stat(path)
    with _lock:
        rec = _read(email, name)
        if rec and rec.get("state") in ACTIVE:
            return rec
        _cancel.discard((email, name))
        rec = _update(email, name, owner=email, size=st.st_size, modified=clips._stamp(st.st_mtime),  # noqa: SLF001
                      state="queued", progress=0.0, upload=None, eta_sec=None, deep=None,
                      windows=0, found=0, clips=0, folder=None, error=None, created=_now(), started=None,
                      finished=None, remote=None)
    _queue.put((email, name))
    _ensure_worker()
    return rec


def cancel(name):
    email = store.require_user()
    name = _safe_name(name)
    with _lock:
        rec = _read(email, name)
        if not rec or rec.get("state") not in ACTIVE:
            return rec
        _cancel.add((email, name))
        if rec.get("state") == "queued":
            return _update(email, name, state="cancelled", error="已取消", finished=_now())
    return rec


def list_longs():
    """待切分的長片＋所有切段紀錄（切完的長片已移到 _已切分）。進行中的排前面。"""
    email = store.require_user()
    inbox = clips.long_inbox()
    items = {}
    if os.path.isdir(inbox):
        for f in sorted(os.listdir(inbox)):
            full = os.path.join(inbox, f)
            if os.path.isfile(full) and clips._is_video(f):  # noqa: SLF001
                st = os.stat(full)
                items[f] = {"name": f, "size": st.st_size, "modified": clips._stamp(st.st_mtime),  # noqa: SLF001
                            "state": "waiting"}
    rec_dir = os.path.join(_user_dir(email), RECORD_DIR)
    if os.path.isdir(rec_dir):
        for f in sorted(os.listdir(rec_dir)):
            if not f.endswith(".json"):
                continue
            rec = _read(email, f[:-5])
            if not rec:
                continue
            item = items.setdefault(rec["name"], {"name": rec["name"], "size": rec.get("size"),
                                                  "modified": rec.get("modified")})
            item.update({k: v for k, v in rec.items() if k not in ("remote",)})
    order = {"cutting": 0, "saving": 0, "uploading": 0, "queued": 1, "waiting": 2, "error": 3, "cancelled": 3}
    out = sorted(items.values(), key=lambda x: (order.get(x.get("state"), 4), x.get("created") or x.get("modified") or ""))
    return {"inbox": inbox, "items": out, "active": any(x.get("state") in ACTIVE for x in out)}


def resume():
    """介面伺服器啟動時：排隊中的接著切；切到一半的標成中斷（推論服務的工作可能還在跑，重試會重新上傳）。"""
    root = clips.clips_root()
    if not os.path.isdir(root):
        return
    requeued = False
    for folder in sorted(os.listdir(root)):
        rec_dir = os.path.join(root, folder, RECORD_DIR)
        if not os.path.isdir(rec_dir):
            continue
        for f in sorted(os.listdir(rec_dir)):
            path = os.path.join(rec_dir, f)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    rec = json.load(fh)
            except (OSError, ValueError):
                continue
            email = rec.get("owner")
            if not email or rec.get("state") not in ACTIVE:
                continue
            if rec["state"] == "queued":
                _queue.put((email, rec["name"]))
                requeued = True
            else:
                _update(email, rec["name"], state="error", error="切段途中介面伺服器被關閉，請重試", finished=_now())
    if requeued:
        _ensure_worker()


# ---------------------------------------------------------------------------
# 背景切段
# ---------------------------------------------------------------------------
def _ensure_worker():
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work, daemon=True, name="longcut")
            _worker.start()


def _work():
    while True:
        email, name = _queue.get()
        rec = _read(email, name)
        if not rec or rec.get("state") != "queued" or (email, name) in _cancel:
            continue
        try:
            _cut(email, name)
        except _Cancelled:
            _update(email, name, state="cancelled", error="已取消", finished=_now(), deep=None)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            _update(email, name, state="error", error=str(e), finished=_now(), deep=None)
        finally:
            _cancel.discard((email, name))


def _check(email, name):
    if (email, name) in _cancel:
        raise _Cancelled()


def _cut(email, name):
    user_dir = _user_dir(email)
    path = os.path.join(user_dir, clips.INBOX_NAME, name)
    if not os.path.isfile(path):
        raise RuntimeError("找不到這支長片（可能已被移走）")
    st = os.stat(path)
    _update(email, name, owner=email, size=st.st_size, modified=clips._stamp(st.st_mtime))  # noqa: SLF001
    client = RemoteAnalyzer(store.CONFIG["inference_url"], store.CONFIG.get("inference_api_key", ""))
    try:
        health = client.health(timeout=4)
    except Exception:  # noqa: BLE001
        raise RuntimeError(f"連不到推論服務（{client.base}）。請先執行「啟動推論服務.cmd」再重試。") from None
    missing = [m["path"] for m in health.get("models", []) if m.get("enabled", True) and not m.get("exists")]
    if missing:
        raise RuntimeError("推論服務缺少模型檔：" + "、".join(missing) + "。請先執行「下載模型.cmd」。")
    _update(email, name, state="uploading", upload=0.0, started=_now())
    last = [0.0]

    def on_up(frac):
        _check(email, name)
        if frac - last[0] >= 0.02 or frac >= 1:
            last[0] = frac
            _update(email, name, upload=round(frac, 3))

    query = {"filename": name, "rules": "all", "triage": f"{TRIAGE_FPS:g}", "clip_raw": "1",
             "clip_before": f"{CLIP_BEFORE:g}", "clip_after": f"{CLIP_AFTER:g}", "profile": RULE_PROFILE}
    remote = client._upload(path, query, on_up)  # noqa: SLF001
    rid = remote["id"]
    _update(email, name, state="cutting", upload=1.0, progress=0.0, remote={"url": client.base, "job_id": rid})
    try:
        while True:
            _check(email, name)
            job = client._req("GET", f"/jobs/{rid}", timeout=10)  # noqa: SLF001
            state, stage = job.get("state"), job.get("stage")
            if state == "done":
                break
            if state in ("error", "cancelled", "interrupted"):
                raise RuntimeError(job.get("error") or f"推論服務工作 {state}")
            done, total = job.get("frames_done") or 0, job.get("frames_total") or 0
            triage = job.get("triage") or {}
            saving = stage in ("clipping", "packaging", "done")
            _update(email, name, state="saving" if saving else "cutting",
                    progress=1.0 if saving else (min(0.99, done / total) if total else 0.0),
                    eta_sec=None if saving else job.get("eta_sec"), deep=triage.get("deep"),
                    windows=triage.get("windows") or 0, found=len(job.get("found") or []),
                    queued=state == "queued")
            time.sleep(POLL_SECONDS)
    except Exception:
        # 取消，或這邊出錯（寫不進紀錄、連線中斷）：一併取消推論服務的工作，不讓它在背景繼續佔用 GPU
        try:
            client._req("POST", f"/jobs/{rid}/cancel")  # noqa: SLF001
        except Exception:  # noqa: BLE001
            pass
        raise

    _update(email, name, state="saving", progress=1.0, eta_sec=None, deep=None)
    result = client._req("GET", f"/jobs/{rid}/result", timeout=30)  # noqa: SLF001
    events = [ev for ev in result.get("events") or [] if ev.get("clip")]
    stem = os.path.splitext(name)[0]
    folder = _unique(os.path.join(user_dir, stem)) if events else None
    saved = 0
    for i, ev in enumerate(sorted(events, key=lambda e: e.get("timestamp_sec") or 0), 1):
        _check(email, name)
        os.makedirs(folder, exist_ok=True)
        label = (VIOLATION_TYPES.get(RULE_TYPES.get(ev.get("rule_id"))) or {}).get("label") or ev.get("rule_id") or "違規"
        t = float(ev.get("timestamp_sec") or 0)
        base = f"{i:03d}_{label}_{int(t // 60):02d}m{int(t % 60):02d}s"
        client._download(f"/jobs/{rid}/files/{ev['clip']['file']}", os.path.join(folder, base + ".mp4"))  # noqa: SLF001
        info = {"source_video": name, "event_id": ev.get("event_id"), "rule_id": ev.get("rule_id"), "label": label,
                "status": ev.get("status"), "timestamp_sec": t, "plate_text": ev.get("plate_text"),
                "clip_start_sec": ev["clip"].get("start_sec"), "clip_end_sec": ev["clip"].get("end_sec"),
                "event_offset_sec": ev["clip"].get("event_offset_sec"), "h264": ev["clip"].get("h264"),
                "triage_fps": TRIAGE_FPS}
        with open(os.path.join(folder, base + ".json"), "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
        saved += 1
    done_dir = os.path.join(user_dir, DONE_DIR)
    os.makedirs(done_dir, exist_ok=True)
    moved = _unique(os.path.join(done_dir, name))
    shutil.move(path, moved)
    _update(email, name, state="done", progress=1.0, clips=saved, found=len(result.get("events") or []),
            folder=os.path.basename(folder) if folder else None, moved_to=os.path.basename(moved),
            finished=_now(), elapsed_sec=result.get("elapsed_sec"))
