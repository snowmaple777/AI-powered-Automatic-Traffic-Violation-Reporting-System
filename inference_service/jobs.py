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
from urllib.parse import urlparse
from datetime import datetime
from pathlib import Path

from . import media
from .preload import PreloadedPipeline

# run_pipeline --rule-profile 的選項（與專案根目錄 rule_profiles.PROFILES 相同；推論服務可指向別處的 pipeline，所以不直接匯入）
RULE_PROFILES = ("default", "high_recall")

STAGES = [
    ("queued", "排隊中"),
    ("loading", "載入模型"),
    ("analyzing", "逐幀辨識與違規判斷"),
    ("clipping", "剪輯違規片段"),
    ("packaging", "轉檔並整理結果"),
]
FINAL_STATES = ("done", "error", "cancelled", "interrupted")
STREAM_SCHEMES = ("rtsp", "rtsps", "rtmp", "rtmps", "http", "https", "udp", "tcp", "srt")
DEEP_RE = re.compile(r"深度分析第\s*(\d+)\s*段：([\d.]+)–([\d.]+)\s*秒")
STREAM_STEM = "stream"  # 串流工作的輸出檔名（run_pipeline --stream-name）
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


def event_plate(ev):
    """回傳 (車牌, 信心)。規則同 plate_backfill.event_plate_text：有頂層 plate（4w 車牌補充）就用它，
    text 為 null 也不退回（代表補充後判定沒有或互相矛盾）；舊紀錄沒有 plate 才用事件當下的車輛證據。
    推論服務可指向別的 pipeline 資料夾，所以不直接 import 專案模組。"""
    plate = ev.get("plate")
    if isinstance(plate, dict):
        return plate.get("text"), plate.get("text_confidence")
    vehicle = (ev.get("evidence") or {}).get("vehicle") or {}
    return vehicle.get("plate_text"), vehicle.get("text_confidence")


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
    def __init__(self, data_dir, pipeline_root, python_exe, extra_args=None, preload=True):
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
        # 常駐推論程序：服務啟動就載入模型（--no-preload 停用，回到每次開新程序）
        self.preload = PreloadedPipeline(self.python, self.root, self.extra_args, kill=_kill_tree) if preload else None
        if self.preload:
            self.preload.start()
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
        keys = ("id", "state", "stage", "stages", "created", "started", "finished", "options", "input_name", "source", "stream",
                "video", "frames_total", "frames_done", "fps", "fps_recent", "eta_sec", "stats", "found",
                "clips_done", "error", "preview_version", "has_result", "triage")
        out = {k: job.get(k) for k in keys}
        log = job.get("_log")
        out["log"] = list(log)[-60:] if log is not None else (job.get("log") or [])[-60:]
        out["log_total"] = job.get("_log_total", len(out["log"]))
        if job.get("state") == "queued":
            with self.lock:
                # 只算還在等的工作（已取消的不算），再加上正在跑的那一個
                waiting = [i for i in list(self.queue.queue)
                           if (self.jobs.get(i) or {}).get("state") == "queued" and not (self.jobs.get(i) or {}).get("_cancel")]
                running = sum(1 for j in self.jobs.values() if j.get("state") == "running")
            idx = waiting.index(job["id"]) if job["id"] in waiting else 0
            out["queue_position"] = idx + 1 if job["id"] in waiting else None
            out["jobs_ahead"] = idx + running
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
            "preload": self.preload.status() if self.preload else {"enabled": False, "state": "disabled"},
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
            # 4bu 長片切段：降頻篩選＋原始片段（不畫標註，之後要再分析）
            "triage": float(options.get("triage") or 0),
            "clip_raw": str(options.get("clip_raw") or "").lower() in ("1", "true", "yes"),
            "profile": options.get("profile") or "default",  # 4by 規則設定組合（high_recall＝長片切段用）
        }
        if opts["triage"] and not 2 <= opts["triage"] <= 30:
            shutil.rmtree(jdir, ignore_errors=True)
            raise ValueError("降頻篩選的幀率要在 2～30 FPS 之間（0＝每幀分析）")
        if opts["profile"] not in RULE_PROFILES:
            shutil.rmtree(jdir, ignore_errors=True)
            raise ValueError(f"規則設定組合只能是 {'、'.join(sorted(RULE_PROFILES))}")
        frames_total = meta["frames"] if meta and meta.get("frames") else None
        if opts["max_seconds"]:
            fps = (meta or {}).get("fps") or 30.0
            cap = int(math.ceil(opts["max_seconds"] * fps))
            frames_total = min(frames_total, cap) if frames_total else cap
        job = self._new_job(jid, jdir, opts, name, str(path), meta, frames_total)
        return self._enqueue(job, f"收到影片 {name}（{os.path.getsize(path) / 1e6:.1f} MB）")

    def create_stream(self, url, options):
        """串流工作：run_pipeline 以串流模式邊收邊分析；max_seconds 是收錄上限（不填就收到串流結束或按停止）。"""
        url = (url or "").strip()
        parsed = urlparse(url)
        if parsed.scheme.lower() not in STREAM_SCHEMES or not parsed.netloc:
            raise ValueError("串流網址格式不正確（例如 rtsp://… 或 http://…）")
        jid = datetime.now().strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:6]
        jdir = self.jobs_dir / jid
        jdir.mkdir(parents=True)
        opts = {
            "rules": options.get("rules") or "all",
            "max_seconds": float(options["max_seconds"]) if options.get("max_seconds") else None,
            "clip_before": float(options.get("clip_before") or 5),
            "clip_after": float(options.get("clip_after") or 5),
            "device": options.get("device") or "auto",
            "triage": float(options.get("triage") or 0),
            "profile": options.get("profile") or "default",
        }
        if opts["triage"] and not 2 <= opts["triage"] <= 30:
            raise ValueError("降頻篩選的幀率要在 2～30 FPS 之間（0＝每幀分析）")
        if opts["profile"] not in RULE_PROFILES:
            raise ValueError(f"規則設定組合只能是 {'、'.join(sorted(RULE_PROFILES))}")
        name = (parsed.hostname or "stream") + (parsed.path if parsed.path not in ("", "/") else "")
        job = self._new_job(jid, jdir, opts, name, url, None, None, source="stream",
                            stream={"state": "connecting"})
        return self._enqueue(job, f"串流 {name}")

    def _new_job(self, jid, jdir, opts, name, input_path, meta, frames_total, **extra):
        return dict({
            "id": jid, "dir": str(jdir), "state": "queued", "stage": "queued",
            "stages": [{"key": k, "label": l} for k, l in STAGES],
            "created": _now(), "started": None, "finished": None, "options": opts, "source": "file",
            "input_name": name, "input_path": input_path, "video": meta,
            "frames_total": frames_total, "frames_done": 0, "fps": None, "fps_recent": None, "eta_sec": None,
            "stats": {"objects": 0, "plates": 0, "lights": {}}, "found": [], "clips_done": 0,
            "error": None, "preview_version": 0, "has_result": False,
            "_log": deque(maxlen=500), "_log_total": 0, "_cancel": False,
        }, **extra)

    def _enqueue(self, job, message):
        with self.lock:
            self.jobs[job["id"]] = job
        self._log(job, message)
        self._save(job)
        self.queue.put(job["id"])
        self._cleanup_old()
        return job

    def stop(self, job_id):
        """串流工作：停止收錄，已收錄的部分照常分析完並整理結果（取消則是直接中止）。"""
        job = self.get(job_id)
        if not job or job.get("source") != "stream" or job.get("state") in FINAL_STATES:
            return job
        if job.get("state") == "queued":
            return self.cancel(job_id)
        (Path(job["dir"]) / "stop").write_text("stop", encoding="utf-8")
        if not job.get("_stop"):
            job["_stop"] = True
            self._log(job, "已停止收錄，分析剩下的畫面…")
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
        streaming = job.get("source") == "stream"
        cmd = [self.python, "-u", str(self.root / "run_pipeline.py"), str(job["input_path"]),
               "--output-dir", str(out_dir), "--rules", opts["rules"],
               "--clip-before", str(opts["clip_before"]), "--clip-after", str(opts["clip_after"]),
               "--device", opts["device"]]
        if streaming:
            cmd += ["--stream-name", STREAM_STEM, "--stream-stop-file", str(jdir / "stop")]
            if opts.get("max_seconds"):
                cmd += ["--stream-max-seconds", str(opts["max_seconds"])]
        elif opts.get("max_seconds"):
            cmd += ["--max-frames", str(job["frames_total"] or int(opts["max_seconds"] * 30))]
        if opts.get("triage"):  # 串流（4bs）與檔案（4bu 長片切段）都可降頻篩選
            cmd += ["--stream-triage-fps", f"{opts['triage']:g}"]
            job["triage"] = {"fps": opts["triage"], "windows": 0, "deep": None}
        if opts.get("clip_raw"):
            cmd += ["--clip-raw"]
        if opts.get("profile", "default") != "default":
            cmd += ["--rule-profile", opts["profile"]]
        cmd += self.extra_args
        job.update(state="running", stage="loading", started=_now())
        started = time.time()
        warm = self.preload.acquire(opts["device"]) if self.preload else None
        if warm is not None:
            self._log(job, "使用已預先載入的模型")
            self._save(job)
            proc = warm
            with self.lock:
                self.current = (job["id"], proc)
            try:
                self.preload.submit(proc, cmd[3:])
            except OSError as e:
                self.preload.release(proc, ok=False)
                raise RuntimeError(f"常駐推論程序沒有回應：{e}") from None
        else:
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            self._log(job, "載入模型中（第一次通常要 10～30 秒）…")
            self._save(job)
            proc = subprocess.Popen(cmd, cwd=str(self.root), env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, **kwargs)
            with self.lock:
                self.current = (job["id"], proc)

        stem = STREAM_STEM if streaming else Path(job["input_path"]).stem
        det_tail = JsonlTail(out_dir / f"{stem}_detections.jsonl")
        vio_tail = JsonlTail(out_dir / f"{stem}_violations.jsonl")
        seen_objects, seen_plates, found_ids = set(), set(), set()
        state = {"last_record": None, "last_preview": 0.0}
        if job.get("triage"):  # 降頻篩選（4bs）：篩選中的即時畫面來自篩選的逐幀紀錄
            state["triage_tail"] = JsonlTail(out_dir / f"{stem}_triage_detections.jsonl")

        finished = threading.Event()

        def monitor():
            while proc.poll() is None and not finished.is_set():
                self._poll_outputs(job, det_tail, vio_tail, seen_objects, seen_plates, found_ids, state)
                time.sleep(0.8)

        mon = threading.Thread(target=monitor, daemon=True)
        mon.start()
        end = None
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line.strip():
                continue
            if warm is not None:
                end = PreloadedPipeline.parse_end(line)
                if end is not None:
                    break
            m = PROGRESS_RE.search(line)
            if m:
                done = int(m.group(1))
                job["frames_done"] = max(job.get("frames_done") or 0, done)
                job["fps"], job["fps_recent"] = float(m.group(2)), float(m.group(3))
                job["stage"] = "analyzing"
                if job.get("triage"):
                    job["triage"]["deep"] = None  # 回到篩選
                if job.get("frames_total") and job["fps_recent"]:
                    job["eta_sec"] = round(max(0, job["frames_total"] - done) / max(job["fps_recent"], 0.01), 1)
            elif job.get("triage") and line.startswith("深度分析第"):
                m = DEEP_RE.search(line)
                if m:
                    job["triage"].update(windows=int(m.group(1)), deep=[float(m.group(2)), float(m.group(3))])
            elif "正在剪輯違規片段" in line:
                job["stage"], job["eta_sec"] = "clipping", None
            elif line.startswith("違規片段"):
                job["clips_done"] = job.get("clips_done", 0) + 1
            self._log(job, line)
        finished.set()
        if job.get("triage"):
            job["triage"]["deep"] = None
        if warm is not None:
            # 常駐程序不結束；沒收到結束標記代表它被取消或當掉了
            returncode = end.get("code", 1) if end is not None else 1
            self.preload.release(proc, ok=end is not None and returncode == 0 and not job.get("_cancel"))
        else:
            proc.wait()
            returncode = proc.returncode
        mon.join(timeout=3)
        self._poll_outputs(job, det_tail, vio_tail, seen_objects, seen_plates, found_ids, state, final=True)
        elapsed = time.time() - started

        if job.get("_cancel"):
            self._log(job, "已取消")
            return self._finish(job, "cancelled", "已取消")
        if returncode != 0:
            msgs = [x["msg"] for x in list(job["_log"])]
            err = next((m for m in reversed(msgs)
                        if re.search(r"(Error|Exception|錯誤|無法|找不到|不符|SystemExit)", m)), None)
            detail = err or (msgs[-1] if msgs else "")
            if warm is not None and end is not None and end.get("error"):
                detail = end["error"]
            elif warm is not None and end is None:
                detail = "常駐推論程序意外結束（已在背景重新載入）"
            return self._finish(job, "error", f"模型執行失敗（結束代碼 {returncode}）：{detail[-400:]}")

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

    def _stream_dir(self, job):
        return Path(job["dir"]) / "outputs" / f"{STREAM_STEM}_stream"

    def _poll_stream(self, job):
        """串流工作：讀 run_pipeline 收錄端的統計（每錄完一段更新），換算已收錄／已分析秒數與落後秒數。"""
        info = _read_json(self._stream_dir(job) / "stream.json")
        if not info or not info.get("fps"):
            return
        fps = info["fps"]
        recorded, analyzed = info.get("frames") or 0, job.get("frames_done") or 0
        state = ("error" if info.get("error") else "stopped" if info.get("reason") in ("stopped", "max_seconds")
                 else "ended" if info.get("finished") else "recording")
        job["stream"] = {"state": state, "fps": fps, "recorded_sec": round(recorded / fps, 1),
                         "analyzed_sec": round(analyzed / fps, 1), "behind_sec": round(max(0, recorded - analyzed) / fps, 1),
                         "ingest_lag_sec": info.get("ingest_lag_seconds"), "timestamps": info.get("timestamps"),
                         "error": info.get("error"), "triage": job.get("triage")}
        job["video"] = {"fps": fps, "width": info.get("width"), "height": info.get("height"),
                        "frames": recorded, "duration": round(recorded / fps, 3)}

    def _read_any_frame(self, job, index):
        if job.get("source") == "stream":
            return media.read_segment_frame(self._stream_dir(job), index)
        return media.read_frame(job["input_path"], index)

    def _poll_outputs(self, job, det_tail, vio_tail, seen_objects, seen_plates, found_ids, state, final=False):
        if job.get("source") == "stream":
            self._poll_stream(job)
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
        triage_records = state["triage_tail"].read() if state.get("triage_tail") else []
        if triage_records and not records:
            # 篩選的幀號是取樣序號；畫面用原幀號 stream_frame 讀
            rec = triage_records[-1]
            state["last_record"] = dict(rec, frame=dict(rec.get("frame") or {}, index=(rec.get("frame") or {}).get(
                "stream_frame", 0)))
        for ev in vio_tail.read():
            if ev.get("status") not in ("suspected", "confirmed") or ev.get("event_id") in found_ids:
                continue
            found_ids.add(ev.get("event_id"))
            plate_text, _ = event_plate(ev)
            job["found"].append({
                "event_id": ev.get("event_id"), "rule_id": ev.get("rule_id"), "status": ev.get("status"),
                "timestamp_sec": ev.get("timestamp_sec"), "vehicle_class": ev.get("vehicle_class"),
                "plate_text": plate_text, "met": len(ev.get("met_conditions") or []),
                "total": len(ev.get("conditions") or {}),
            })
            self._log(job, f"發現{'確認' if ev.get('status') == 'confirmed' else '疑似'}違規：{ev.get('rule_id')}"
                           f"（{plate_text or '車牌未辨識'}）@ {ev.get('timestamp_sec', 0):.1f}s")
        now = time.time()
        if state.get("last_record") and (final or now - state["last_preview"] > 1.0):
            rec = state["last_record"]
            idx = (rec.get("frame") or {}).get("index", 0)
            frame = self._read_any_frame(job, idx)
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
            plate_text, plate_confidence = event_plate(ev)
            conds = ev.get("conditions") or {}
            item = {
                "id": eid, "event_id": ev.get("event_id"), "rule_id": ev.get("rule_id"),
                "status": ev.get("status"), "status_label": ev.get("status_label"),
                "behavior": ev.get("behavior"), "description": ev.get("description"),
                "frame": ev.get("frame"), "timestamp_sec": ev.get("timestamp_sec"),
                "object_id": ev.get("object_id"), "vehicle_class": ev.get("vehicle_class"),
                "vehicle_confidence": vehicle.get("confidence"), "distance_m": vehicle.get("distance_m"),
                "plate_text": plate_text, "plate_confidence": plate_confidence,
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
            elif ev.get("frame") is not None and job.get("source") == "stream":
                frame = media.read_segment_frame(self._stream_dir(job), int(ev["frame"]))
                if frame is not None and media.write_jpg(result_dir / f"{eid}.jpg", frame, 88):
                    item["snapshot"] = f"{eid}.jpg"
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
