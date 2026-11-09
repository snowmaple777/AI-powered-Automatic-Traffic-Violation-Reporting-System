"""推論工作管理：排隊、執行 run_pipeline.py、讀進度、整理結果。

一次只跑一個工作（GPU 只有一張），其餘排隊。
每個工作的檔案都在 data/jobs/<id>/：
    input/<影片>            上傳的影片
    outputs/                run_pipeline.py 的輸出（--output-dir）
    result/                 整理後給 UI 的片段（H.264）與截圖
    job.json                狀態（服務重開後還看得到）
    result.json             最終結果
"""
from __future__ import annotations

import json
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path

from . import media

STAGES = [
    ("queued", "排隊中"),
    ("loading", "載入模型"),
    ("analyzing", "逐幀辨識與違規判斷"),
    ("clipping", "剪輯違規片段"),
    ("packaging", "轉檔並整理結果"),
]
FINAL_STATES = ("done", "error", "cancelled", "interrupted")
PROGRESS_RE = re.compile(r"已處理\s*(\d+)\s*幀，累計\s*([\d.]+)\s*FPS.*?最近\s*10\s*幀\s*([\d.]+)\s*FPS")
KEEP_JOBS = 20


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_name(name: str) -> str:
    base = os.path.basename(name or "")
    keep = "".join(c for c in base if c.isalnum() or c in "._-")
    return keep or "video.mp4"


def _write_json(path, data):
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


class JsonlTail:
    """一直讀還在被寫入的 jsonl 檔，只回傳完整的新行。"""

    def __init__(self, path):
        self.path, self.pos, self.buf = Path(path), 0, b""

    def read(self):
        if not self.path.exists():
            return []
        with open(self.path, "rb") as f:
            f.seek(self.pos)
            chunk = f.read()
            self.pos = f.tell()
        self.buf += chunk
        *lines, self.buf = self.buf.split(b"\n")
        out = []
        for line in lines:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line.decode("utf-8")))
                except ValueError:
                    pass
        return out


class Manager:
    def __init__(self, data_dir, pipeline_root, python_exe, extra_args=None):
        self.data_dir = Path(data_dir)
        self.jobs_dir = self.data_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.root = Path(pipeline_root)
        self.python = python_exe
        self.extra_args = list(extra_args or [])
        self.ffmpeg = media.find_ffmpeg()
        self.jobs = {}
        self.lock = threading.RLock()
        self.queue = queue.Queue()
        self.current = None  # (job_id, Popen)
        self.gpu = None
        self._load_existing()
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._probe_gpu, daemon=True).start()

    # ------------------------------------------------------------------ 狀態
    def _load_existing(self):
        for d in sorted(self.jobs_dir.iterdir()) if self.jobs_dir.exists() else []:
            data = _read_json(d / "job.json")
            if not data:
                continue
            if data.get("state") not in FINAL_STATES:
                data["state"] = "interrupted"
                data["error"] = "推論服務在分析途中被關閉"
                _write_json(d / "job.json", data)
            self.jobs[data["id"]] = data

    def _save(self, job):
        snap = {k: v for k, v in job.items() if not k.startswith("_")}
        snap["log"] = list(job.get("_log", []))[-200:]
        _write_json(Path(job["dir"]) / "job.json", snap)

    def public(self, job):
        keys = ("id", "state", "stage", "stages", "created", "started", "finished", "options", "input_name",
                "video", "frames_total", "frames_done", "fps", "fps_recent", "eta_sec", "stats", "found",
                "clips_done", "error", "preview_version", "has_result")
        out = {k: job.get(k) for k in keys}
        log = job.get("_log")
        out["log"] = list(log)[-60:] if log is not None else (job.get("log") or [])[-60:]
        out["log_total"] = job.get("_log_total", len(out["log"]))
        if job.get("state") == "queued":
            ids = list(self.queue.queue)
            out["queue_position"] = ids.index(job["id"]) + 1 if job["id"] in ids else None
        return out

    def get(self, job_id):
        with self.lock:
            return self.jobs.get(safe_name(job_id))

    def list(self):
        with self.lock:
            jobs = sorted(self.jobs.values(), key=lambda j: j.get("created", ""), reverse=True)
            out = []
            for j in jobs:
                item = {k: j.get(k) for k in ("id", "state", "stage", "created", "input_name",
                                              "frames_done", "frames_total")}
                item["violations"] = len(j.get("found") or [])
                out.append(item)
            return out

    def health(self):
        models = []
        cfg = _read_json(self.root / "configs" / "default.json") or {}
        root = self.root.resolve()
        for stage, spec in (cfg.get("models") or {}).items():
            spec = spec or {}
            params = spec.get("params") or {}
            for key in ("weights", "dict_path"):
                rel = params.get(key) or spec.get(key)
                if not rel:
                    continue
                path = (self.root / "configs" / rel).resolve()
                try:
                    shown = str(path.relative_to(root)).replace("\\", "/")
                except ValueError:
                    shown = str(path)
                models.append({"stage": stage, "path": shown, "exists": path.exists(),
                               "enabled": spec.get("enabled", True)})
        with self.lock:
            busy = self.current is not None
        return {
            "service": "traffic-ai-inference", "version": "1.0", "ok": True,
            "pipeline_root": str(self.root), "run_pipeline": (self.root / "run_pipeline.py").exists(),
            "python": self.python, "python_exists": Path(self.python).exists() or shutil.which(self.python) is not None,
            "models": models, "ffmpeg": bool(self.ffmpeg), "opencv": media.cv2 is not None,
            "gpu": self.gpu, "busy": busy, "queue_length": self.queue.qsize(),
        }

    def _probe_gpu(self):
        code = ("import json\ntry:\n import torch\n ok=torch.cuda.is_available()\n"
                " print(json.dumps({'cuda':ok,'name':torch.cuda.get_device_name(0) if ok else None,"
                "'torch':torch.__version__}))\nexcept Exception as e:\n print(json.dumps({'cuda':False,'error':str(e)}))")
        try:
            r = subprocess.run([self.python, "-c", code], capture_output=True, timeout=180, cwd=str(self.root))
            line = r.stdout.decode("utf-8", "replace").strip().splitlines()[-1]
            self.gpu = json.loads(line)
        except Exception as e:  # noqa: BLE001
            self.gpu = {"cuda": False, "error": f"無法偵測：{e}"}

    # ------------------------------------------------------------------ 建立
    def create(self, filename, stream, length, options):
        jid = datetime.now().strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:6]
        jdir = self.jobs_dir / jid
        (jdir / "input").mkdir(parents=True)
        name = safe_name(filename)
        if not name.lower().endswith((".mp4", ".mov", ".avi", ".mkv", ".m4v")):
            name += ".mp4"
        path = jdir / "input" / name
        remaining = length
        with open(path, "wb") as f:
            while remaining > 0:
                chunk = stream.read(min(1 << 20, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        if remaining > 0:
            shutil.rmtree(jdir, ignore_errors=True)
            raise ValueError("影片上傳不完整")
        meta = media.video_meta(path)
        opts = {
            "rules": options.get("rules") or "all",
            "max_seconds": float(options["max_seconds"]) if options.get("max_seconds") else None,
            "clip_before": float(options.get("clip_before") or 5),
            "clip_after": float(options.get("clip_after") or 5),
            "device": options.get("device") or "auto",
        }
        frames_total = meta["frames"] if meta and meta.get("frames") else None
        if opts["max_seconds"]:
            fps = (meta or {}).get("fps") or 30.0
            cap = int(math.ceil(opts["max_seconds"] * fps))
            frames_total = min(frames_total, cap) if frames_total else cap
        job = {
            "id": jid, "dir": str(jdir), "state": "queued", "stage": "queued",
            "stages": [{"key": k, "label": l} for k, l in STAGES],
            "created": _now(), "started": None, "finished": None, "options": opts,
            "input_name": name, "input_path": str(path), "video": meta,
            "frames_total": frames_total, "frames_done": 0, "fps": None, "fps_recent": None, "eta_sec": None,
            "stats": {"objects": 0, "plates": 0, "lights": {}}, "found": [], "clips_done": 0,
            "error": None, "preview_version": 0, "has_result": False,
            "_log": deque(maxlen=500), "_log_total": 0, "_cancel": False,
        }
        with self.lock:
            self.jobs[jid] = job
        self._log(job, f"收到影片 {name}（{os.path.getsize(path) / 1e6:.1f} MB）")
        self._save(job)
        self.queue.put(jid)
        self._cleanup_old()
        return job

    def _cleanup_old(self):
        with self.lock:
            done = [j for j in self.jobs.values() if j.get("state") in FINAL_STATES]
            done.sort(key=lambda j: j.get("created", ""))
            for j in done[:-KEEP_JOBS] if len(done) > KEEP_JOBS else []:
                shutil.rmtree(j["dir"], ignore_errors=True)
                self.jobs.pop(j["id"], None)

    def delete(self, job_id):
        job = self.get(job_id)
        if not job:
            return False
        if job.get("state") not in FINAL_STATES:
            self.cancel(job_id)
            time.sleep(1)
        shutil.rmtree(job["dir"], ignore_errors=True)
        with self.lock:
            self.jobs.pop(job["id"], None)
        return True

    # ------------------------------------------------------------------ 取消
    def cancel(self, job_id):
        job = self.get(job_id)
        if not job or job.get("state") in FINAL_STATES:
            return job
        job["_cancel"] = True
        with self.lock:
            cur = self.current
        if cur and cur[0] == job["id"]:
            _kill_tree(cur[1])
        if job.get("state") == "queued":
            self._finish(job, "cancelled", "已取消")
        return job

    # ------------------------------------------------------------------ 執行
    def _log(self, job, msg):
        job.setdefault("_log", deque(maxlen=500)).append({"time": datetime.now().strftime("%H:%M:%S"), "msg": msg})
        job["_log_total"] = job.get("_log_total", 0) + 1

    def _finish(self, job, state, error=None):
        job["state"], job["error"], job["finished"] = state, error, _now()
        if state == "done":
            job["stage"] = "done"
        self._save(job)

    def _worker(self):
        while True:
            jid = self.queue.get()
            job = self.get(jid)
            if not job or job.get("_cancel") or job.get("state") != "queued":
                continue
            try:
                self._run(job)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                self._log(job, f"錯誤：{e}")
                self._finish(job, "error", str(e))
            finally:
                with self.lock:
                    self.current = None

    def _run(self, job):
        jdir = Path(job["dir"])
        out_dir = jdir / "outputs"
        out_dir.mkdir(exist_ok=True)
        opts = job["options"]
        input_path = Path(job["input_path"])
        cmd = [self.python, "-u", str(self.root / "run_pipeline.py"), str(input_path),
               "--output-dir", str(out_dir), "--rules", opts["rules"],
               "--clip-before", str(opts["clip_before"]), "--clip-after", str(opts["clip_after"]),
               "--device", opts["device"]]
        if opts.get("max_seconds"):
            cmd += ["--max-frames", str(job["frames_total"] or int(opts["max_seconds"] * 30))]
        cmd += self.extra_args
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        job.update(state="running", stage="loading", started=_now())
        self._log(job, "載入模型中（第一次通常要 10～30 秒）…")
        self._save(job)
        started = time.time()
        proc = subprocess.Popen(cmd, cwd=str(self.root), env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, **kwargs)
        with self.lock:
            self.current = (job["id"], proc)

        stem = input_path.stem
        det_tail = JsonlTail(out_dir / f"{stem}_detections.jsonl")
        vio_tail = JsonlTail(out_dir / f"{stem}_violations.jsonl")
        seen_objects, seen_plates, found_ids = set(), set(), set()
        state = {"last_record": None, "last_preview": 0.0}

        def monitor():
            while proc.poll() is None:
                self._poll_outputs(job, det_tail, vio_tail, seen_objects, seen_plates, found_ids, state)
                time.sleep(0.8)

        mon = threading.Thread(target=monitor, daemon=True)
        mon.start()
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line.strip():
                continue
            m = PROGRESS_RE.search(line)
            if m:
                done = int(m.group(1))
                job["frames_done"] = max(job.get("frames_done") or 0, done)
                job["fps"], job["fps_recent"] = float(m.group(2)), float(m.group(3))
                job["stage"] = "analyzing"
                if job.get("frames_total") and job["fps_recent"]:
                    job["eta_sec"] = round(max(0, job["frames_total"] - done) / max(job["fps_recent"], 0.01), 1)
            elif "正在剪輯違規片段" in line:
                job["stage"], job["eta_sec"] = "clipping", None
            elif line.startswith("違規片段"):
                job["clips_done"] = job.get("clips_done", 0) + 1
            self._log(job, line)
        proc.wait()
        mon.join(timeout=3)
        self._poll_outputs(job, det_tail, vio_tail, seen_objects, seen_plates, found_ids, state, final=True)
        elapsed = time.time() - started

        if job.get("_cancel"):
            self._log(job, "已取消")
            return self._finish(job, "cancelled", "已取消")
        if proc.returncode != 0:
            msgs = [x["msg"] for x in list(job["_log"])]
            err = next((m for m in reversed(msgs)
                        if re.search(r"(Error|Exception|錯誤|無法|找不到|不符|SystemExit)", m)), None)
            detail = err or (msgs[-1] if msgs else "")
            return self._finish(job, "error", f"模型執行失敗（結束代碼 {proc.returncode}）：{detail[-400:]}")

        job["stage"] = "packaging"
        self._log(job, f"模型分析完成，用時 {elapsed:.0f} 秒；整理結果中…")
        self._save(job)
        result = self._package(job, out_dir, stem)
        result["elapsed_sec"] = round(elapsed, 1)
        _write_json(jdir / "result.json", result)
        job["has_result"] = True
        self._log(job, f"完成：{len(result['events'])} 筆違規"
                       f"（確認 {sum(e['status'] == 'confirmed' for e in result['events'])}、"
                       f"疑似 {sum(e['status'] == 'suspected' for e in result['events'])}）")
        self._finish(job, "done")

    def _poll_outputs(self, job, det_tail, vio_tail, seen_objects, seen_plates, found_ids, state, final=False):
        records = det_tail.read()
        lights = job["stats"].setdefault("lights", {})
        for rec in records:
            obs = rec.get("observations", {})
            for v in obs.get("rule_vehicles") or obs.get("vehicles") or []:
                if v.get("object_id"):
                    seen_objects.add(v["object_id"])
                if v.get("plate_text"):
                    seen_plates.add(v["plate_text"])
            for light in obs.get("traffic_lights") or []:
                name = light.get("class_name")
                if name:
                    lights[name] = lights.get(name, 0) + 1
            idx = (rec.get("frame") or {}).get("index")
            if idx is not None:
                job["frames_done"] = max(job.get("frames_done") or 0, idx + 1)
                if job.get("stage") == "loading":
                    job["stage"] = "analyzing"
        if records:
            state["last_record"] = records[-1]
            job["stats"]["objects"], job["stats"]["plates"] = len(seen_objects), len(seen_plates)
        for ev in vio_tail.read():
            if ev.get("status") not in ("suspected", "confirmed") or ev.get("event_id") in found_ids:
                continue
            found_ids.add(ev.get("event_id"))
            vehicle = (ev.get("evidence") or {}).get("vehicle") or {}
            job["found"].append({
                "event_id": ev.get("event_id"), "rule_id": ev.get("rule_id"), "status": ev.get("status"),
                "timestamp_sec": ev.get("timestamp_sec"), "vehicle_class": ev.get("vehicle_class"),
                "plate_text": vehicle.get("plate_text"), "met": len(ev.get("met_conditions") or []),
                "total": len(ev.get("conditions") or {}),
            })
            self._log(job, f"發現{'確認' if ev.get('status') == 'confirmed' else '疑似'}違規：{ev.get('rule_id')}"
                           f"（{vehicle.get('plate_text') or '車牌未辨識'}）@ {ev.get('timestamp_sec', 0):.1f}s")
        now = time.time()
        if state.get("last_record") and (final or now - state["last_preview"] > 1.0):
            rec = state["last_record"]
            idx = (rec.get("frame") or {}).get("index", 0)
            frame = media.read_frame(job["input_path"], idx)
            if media.draw_preview(frame, rec, Path(job["dir"]) / "preview.jpg"):
                job["preview_version"] = job.get("preview_version", 0) + 1
            state["last_preview"] = now

    # ------------------------------------------------------------------ 整理結果
    def _package(self, job, out_dir, stem):
        result_dir = Path(job["dir"]) / "result"
        result_dir.mkdir(exist_ok=True)
        # 違規事件：同一個 event_id 取最後一筆（狀態可能由疑似更新為確認）
        events = {}
        vio_path = out_dir / f"{stem}_violations.jsonl"
        if vio_path.exists():
            for line in vio_path.read_text(encoding="utf-8").splitlines():
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("status") in ("suspected", "confirmed"):
                    old = events.get(ev.get("event_id"))
                    if not old or not (old.get("status") == "confirmed" and ev.get("status") == "suspected"):
                        events[ev.get("event_id")] = ev
        # 片段：取最新一次 run_* 的 index.json
        clips = {}
        clip_root = out_dir / f"{stem}_violation_clips"
        runs = sorted(clip_root.glob("run_*"), key=lambda p: p.stat().st_mtime) if clip_root.exists() else []
        if runs:
            for meta in _read_json(runs[-1] / "index.json") or []:
                ev = meta.get("event") or {}
                meta["_local"] = runs[-1] / Path(meta.get("clip_path", "")).name
                clips[ev.get("event_id")] = meta
                events.setdefault(ev.get("event_id"), ev)
        video_meta = job.get("video") or {}
        fps = video_meta.get("fps") or 30.0
        out = []
        for n, ev in enumerate(sorted(events.values(), key=lambda e: (e.get("timestamp_sec") or 0)), 1):
            eid = f"E{n:03d}"
            vehicle = (ev.get("evidence") or {}).get("vehicle") or {}
            conds = ev.get("conditions") or {}
            item = {
                "id": eid, "event_id": ev.get("event_id"), "rule_id": ev.get("rule_id"),
                "status": ev.get("status"), "status_label": ev.get("status_label"),
                "behavior": ev.get("behavior"), "description": ev.get("description"),
                "frame": ev.get("frame"), "timestamp_sec": ev.get("timestamp_sec"),
                "object_id": ev.get("object_id"), "vehicle_class": ev.get("vehicle_class"),
                "vehicle_confidence": vehicle.get("confidence"), "distance_m": vehicle.get("distance_m"),
                "plate_text": vehicle.get("plate_text"), "plate_confidence": vehicle.get("text_confidence"),
                "signal_state": (ev.get("evidence") or {}).get("signal_state"),
                "conditions": [{"key": k, "ok": bool(v)} for k, v in conds.items()],
                "met": sum(1 for v in conds.values() if v), "total": len(conds),
                "missing": [k for k, v in conds.items() if not v],
                "review_required": ev.get("review_required", True),
                "clip": None, "snapshot": None,
            }
            meta = clips.get(ev.get("event_id"))
            if meta and Path(meta["_local"]).exists():
                dst = result_dir / f"{eid}.mp4"
                h264 = media.to_h264(meta["_local"], dst, self.ffmpeg)
                item["clip"] = {"file": dst.name, "h264": h264, "start_sec": meta.get("start_sec"),
                                "end_sec": meta.get("end_sec"), "event_offset_sec": meta.get("event_offset_sec")}
                if media.snapshot(meta["_local"], result_dir / f"{eid}.jpg", seconds=meta.get("event_offset_sec")):
                    item["snapshot"] = f"{eid}.jpg"
                job["clips_done"] = n
            elif ev.get("frame") is not None:
                if media.snapshot(job["input_path"], result_dir / f"{eid}.jpg", frame_index=ev["frame"]):
                    item["snapshot"] = f"{eid}.jpg"
            if not item["clip"]:
                t = float(ev.get("timestamp_sec") or 0)
                item["clip_window"] = {"start_sec": max(0.0, t - job["options"]["clip_before"]),
                                       "end_sec": t + job["options"]["clip_after"]}
            out.append(item)
        timings = _read_json(out_dir / f"{stem}_timings.json")
        return {"job_id": job["id"], "events": out, "video": video_meta, "fps": fps, "timings": timings,
                "options": job["options"], "stats": job.get("stats")}


def _kill_tree(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def default_python(root: Path):
    for cand in (root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python"):
        if cand.exists():
            return str(cand)
    return sys.executable
