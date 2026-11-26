"""管理常駐推論程序（pipeline_worker.py）：服務啟動時就載入模型，分析時不必再等。

- 一次只有一個工作使用它（推論服務本來就一次只跑一支影片）。
- 工作失敗、被取消（會直接結束程序）、程序意外結束，或累計 MAX_JOBS 個工作後，都在背景重開並重新載入。
- 工作指定的 device 與常駐程序不同時不使用它，改走原本「每次開新程序」的路徑。
- 改了程式或模型設定要重開推論服務，常駐程序不會自動套用。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

READY, FAILED, END = "@@WORKER_READY", "@@WORKER_FAILED", "@@JOB_END"
MAX_JOBS = 20


def _decode(raw):
    return raw.decode("utf-8", "replace").rstrip("\r\n")


def _tagged(line, tag):
    """標記後面的 JSON；標記可能不在行首（函式庫偶爾印出不換行的片段）。沒有標記回傳 None。"""
    at = line.find(tag + " ")
    if at < 0:
        return None
    try:
        return json.loads(line[at + len(tag):] or "{}")
    except ValueError:
        return {}


def _popen_flags():
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


class PreloadedPipeline:
    def __init__(self, python, root, extra_args=(), kill=None, script=None, max_jobs=MAX_JOBS):
        self.python, self.root, self.extra_args = python, Path(root), list(extra_args)
        self.script = Path(script) if script else Path(__file__).with_name("pipeline_worker.py")
        self.kill = kill or (lambda proc: proc.kill())
        self.max_jobs = max_jobs
        self.lock = threading.RLock()
        self.proc = None
        self.state = "stopped"  # starting / ready / busy / failed / stopped
        self.info, self.error, self.jobs_done, self.starts = {}, None, 0, 0
        self.started_at = None
        self.ready_event = threading.Event()

    # ------------------------------------------------------------------ 啟動
    def start(self):
        with self.lock:
            if self.proc is not None and self.proc.poll() is None and self.state in ("starting", "ready", "busy"):
                return
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            self.state, self.info, self.error, self.jobs_done = "starting", {}, None, 0
            self.started_at = time.time()
            self.starts += 1
            self.ready_event.clear()
            self.proc = subprocess.Popen([self.python, "-u", str(self.script), str(self.root), *self.extra_args],
                                         cwd=str(self.root), env=env, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **_popen_flags())
            proc = self.proc
        threading.Thread(target=self._wait_ready, args=(proc,), daemon=True).start()

    def _wait_ready(self, proc):
        tail = deque(maxlen=20)
        error = None
        for raw in iter(proc.stdout.readline, b""):
            line = _decode(raw)
            ready = _tagged(line, READY)
            if ready is not None:
                with self.lock:
                    if self.proc is proc:
                        self.info = ready
                        self.state = "ready"
                self.ready_event.set()
                return
            failed = _tagged(line, FAILED)
            if failed is not None:
                error = failed.get("error") or "載入模型失敗"
                break
            if line.strip():
                tail.append(line)
        with self.lock:
            if self.proc is proc:
                self.state = "failed"
                self.error = error or (tail[-1] if tail else "常駐程序在載入模型時結束")
        self.ready_event.set()

    # ------------------------------------------------------------------ 使用
    def acquire(self, device):
        """可用就回傳程序並標成使用中；否則 None（呼叫端改走每次開新程序）。"""
        with self.lock:
            if self.state != "ready" or self.proc is None or self.proc.poll() is not None:
                return None
            if device not in ("auto", "", None, self.info.get("device")):
                return None
            self.state = "busy"
            return self.proc

    @staticmethod
    def submit(proc, argv):
        proc.stdin.write((json.dumps({"argv": [str(a) for a in argv]}) + "\n").encode("utf-8"))
        proc.stdin.flush()

    @staticmethod
    def parse_end(line):
        """工作結束標記 → dict；不是標記就回傳 None。"""
        end = _tagged(line, END)
        if end is not None and "code" not in end:
            return {"code": 1, "error": "結束標記格式錯誤"}
        return end

    def release(self, proc, ok):
        """工作結束後呼叫。成功就回到待命；否則（或已達重開門檻）在背景重開。"""
        with self.lock:
            if self.proc is not proc:
                return
            self.jobs_done += 1
            if ok and proc.poll() is None and self.jobs_done < self.max_jobs:
                self.state = "ready"
                return
        self.restart()

    def restart(self):
        with self.lock:
            proc = self.proc
            self.proc = None
            self.state = "stopped"
        self._terminate(proc)
        self.start()

    def stop(self):
        with self.lock:
            proc, self.proc, self.state = self.proc, None, "stopped"
        self._terminate(proc)

    def _terminate(self, proc):
        if proc is None:
            return
        if proc.poll() is None:
            self.kill(proc)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        for pipe in (proc.stdin, proc.stdout):
            try:
                pipe.close()
            except (OSError, AttributeError):
                pass

    def status(self):
        with self.lock:
            out = {"enabled": True, "state": self.state, "jobs_done": self.jobs_done, "starts": self.starts,
                   "error": self.error}
            out.update({k: self.info.get(k) for k in ("device", "load_seconds", "warmup_seconds")})
            if self.state == "starting" and self.started_at:
                out["starting_for_sec"] = round(time.time() - self.started_at, 1)
            return out
