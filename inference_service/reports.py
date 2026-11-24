"""自動填表（autoForm）：在這台電腦開瀏覽器，幫你把案件填進警察局檢舉網站。

流程：UI 把案件資料夾打包成 zip 傳過來 → 解壓 → 執行 autoform_runner.py（裡面呼叫組員的 main.py）
→ 讀它印出的訊息判斷目前狀態，讓 UI 顯示「填表中／等你完成驗證／送出中／完成」。

注意：填表會開「看得到的瀏覽器」，驗證碼也要人來輸入，所以這部分只能在有螢幕的電腦上跑，
不能放到雲端 GPU 主機。之後辨識搬上雲端時，填表仍然留在使用者自己的電腦。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from .jobs import _kill_tree

# 狀態：starting → filling → waiting_verification → submitting → done／error／cancelled
ACTIVE = ("starting", "filling", "waiting_verification", "submitting")
STEPS = [("starting", "開啟瀏覽器"), ("filling", "填寫表單、上傳佐證"),
         ("waiting_verification", "人機驗證（請在瀏覽器操作）"), ("submitting", "送出並確認"), ("done", "完成")]
KEEP = 20


def _now():
    return datetime.now().isoformat(timespec="seconds")


class ReportManager:
    def __init__(self, data_dir, root: Path, python):
        self.dir = Path(data_dir) / "reports"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.root = Path(root)
        self.python = python
        self.runner = Path(__file__).resolve().parent / "autoform_runner.py"
        self.lock = threading.Lock()
        self.reports = {}
        self.procs = {}
        self._env_cache = (0, None)
        for p in sorted(self.dir.glob("*/report.json")):
            try:
                r = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if r.get("state") in ACTIVE or r.get("browser_open"):
                r.update(state="cancelled" if r.get("state") in ACTIVE else r["state"], browser_open=False)
                r["message"] = r.get("message") or "推論服務重新啟動，上次的填表已中斷"
                self._save(r)
            self.reports[r["id"]] = r

    # ---------------- 環境 ----------------
    @property
    def autoform_dir(self):
        for cand in (self.root / "reporting" / "autoForm", self.root / "autoForm"):
            if (cand / "main.py").exists():
                return cand
        return None

    def health(self):
        """autoForm 是否可用（有沒有程式、有沒有裝 playwright 與瀏覽器）。結果快取 60 秒。"""
        t, cached = self._env_cache
        if cached and time.time() - t < 60:
            return dict(cached, busy=self.active() is not None)
        d = self.autoform_dir
        info = {"available": False, "dir": str(d) if d else None, "playwright": False, "browser": False,
                "profile": bool(d and (d / "reporter_profile.json").exists())}
        if d:
            code = ("import os,sys\n"
                    "from playwright.sync_api import sync_playwright\n"
                    "with sync_playwright() as p:\n"
                    "    print('BROWSER' if os.path.exists(p.chromium.executable_path) else 'NOBROWSER')\n")
            try:
                out = subprocess.run([self.python, "-c", code], capture_output=True, text=True, timeout=30,
                                     encoding="utf-8", errors="replace")
                info["playwright"] = out.returncode == 0 or "BROWSER" in out.stdout
                info["browser"] = "BROWSER" in out.stdout and "NOBROWSER" not in out.stdout
            except Exception:  # noqa: BLE001
                pass
        info["available"] = bool(d and info["playwright"] and info["browser"])
        if not d:
            info["hint"] = "找不到 reporting/autoForm/main.py"
        elif not info["playwright"]:
            info["hint"] = "還沒安裝 playwright：重新執行 啟動推論服務.cmd 會自動安裝"
        elif not info["browser"]:
            info["hint"] = "還沒下載填表用的瀏覽器：重新執行 啟動推論服務.cmd 會自動下載"
        self._env_cache = (time.time(), info)
        return dict(info, busy=self.active() is not None)

    def active(self):
        with self.lock:
            for r in self.reports.values():
                if r["state"] in ACTIVE or r.get("browser_open"):
                    return r
        return None

    # ---------------- 建立 ----------------
    def create(self, body: bytes, q):
        d = self.autoform_dir
        if not d:
            raise ValueError("找不到 autoForm（reporting/autoForm/main.py）")
        busy = self.active()
        if busy:
            raise ValueError("已經有一個填表視窗在進行中，請先完成或取消它（關閉那個瀏覽器）")
        city = "".join(c for c in q.get("city", "kaohsiung") if c.isalnum() or c == "_") or "kaohsiung"
        test = q.get("test", "1") not in ("0", "false", "no")
        rid = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]
        rdir = self.dir / rid
        case_dir = rdir / "case"
        case_dir.mkdir(parents=True)
        profile = None
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as z:
                for info in z.infolist():
                    if info.is_dir():
                        continue
                    name = os.path.basename(info.filename)
                    if not name or name.startswith("."):
                        continue
                    data = z.read(info)
                    if name == "reporter_profile.json" and "/" not in info.filename.strip("/"):
                        profile = data
                        continue
                    (case_dir / name).write_bytes(data)
        except zipfile.BadZipFile:
            shutil.rmtree(rdir, ignore_errors=True)
            raise ValueError("收到的案件不是 zip 檔") from None
        if not (case_dir / "info.txt").exists():
            shutil.rmtree(rdir, ignore_errors=True)
            raise ValueError("案件裡沒有 info.txt")
        args = ["--city", city, "--case-dir", str(case_dir)]
        if profile:
            (rdir / "reporter_profile.json").write_bytes(profile)
            args += ["--profile", str(rdir / "reporter_profile.json")]
        r = {"id": rid, "created": _now(), "state": "starting", "test": test, "city": city,
             "case_name": q.get("name", ""), "message": "", "success": None, "browser_open": True,
             "log": [], "steps": [s for s, _ in STEPS], "step_labels": dict(STEPS), "finished": None}
        cmd = [self.python, "-u", str(self.runner), str(d)] + (["--test"] if test else []) + ["--"] + args
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(cmd, cwd=str(d), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, **kwargs)
        with self.lock:
            self.reports[rid] = r
            self.procs[rid] = proc
        self._log(r, f"開啟瀏覽器，準備填寫{'（測試模式：不會送出）' if test else ''}…")
        self._save(r)
        threading.Thread(target=self._watch, args=(r, proc), daemon=True).start()
        self._cleanup()
        return r

    # ---------------- 監看輸出 ----------------
    def _watch(self, r, proc):
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line.strip() or set(line.strip()) <= set("=-"):
                continue
            self._log(r, line)
            self._parse(r, line)
            self._save(r)
        code = proc.wait()
        with self.lock:
            self.procs.pop(r["id"], None)
        r["browser_open"] = False
        if r["state"] in ACTIVE:
            if r.get("cancel_requested"):
                r.update(state="cancelled", message="已取消，瀏覽器已關閉，沒有送出")
            else:
                r.update(state="error", message=r.get("message") or self._last_error(r) or f"填表程式結束（代碼 {code}）")
        r["finished"] = r["finished"] or _now()
        try:  # 檢舉人資料（含身分證）用完就刪
            (self.dir / r["id"] / "reporter_profile.json").unlink()
        except OSError:
            pass
        self._save(r)

    def _parse(self, r, line):
        s = r["state"]
        if "[Main] 啟動瀏覽器" in line or ("Driver" in line and s == "starting"):
            if s == "starting":
                r["state"] = "filling"
        if "【人機協作提示】" in line and s in ("starting", "filling"):
            r.update(state="waiting_verification", message="請到跳出的瀏覽器完成驗證碼／信箱驗證")
        elif ("[系統偵測]" in line or "收到繼續指令" in line) and s == "waiting_verification":
            r.update(state="filling" if "繼續" in line else "submitting", message="")
        elif ("最後送出" in line or "正在驗證是否檢舉成功" in line) and s in ACTIVE:
            r.update(state="submitting", message="")
        elif "[測試模式] 表單已填好" in line:
            r.update(state="done", success=True, message="測試完成：表單已填好，沒有送出。請到瀏覽器檢查內容，看完關掉瀏覽器即可。",
                     finished=_now())
        elif "🎉" in line or "已成功跑完" in line:
            if r["state"] != "done":
                r.update(state="done", success=True, finished=_now(),
                         message=("已送出，請到瀏覽器確認受理畫面（或收信完成認證），看完關掉瀏覽器即可。"
                                  if not r["test"] else r["message"]))
        elif "[⚠️ 系統警告] 填表流程已結束" in line:
            r.update(state="done", success=False, finished=_now(),
                     message="填表跑完了，但沒偵測到成功畫面，請到瀏覽器確認是否已送出")
        elif "[❌" in line or "[Main 錯誤]" in line:
            msg = line.split("]", 1)[-1].strip(" ：:") or line
            r.update(state="error", success=False, finished=_now(), message=msg)
        elif "等待手動驗證超時" in line:
            r["message"] = "等待驗證超時"
        elif "[Main 警告] 目前尚未實作" in line:
            r["warning"] = "這個縣市還沒有專屬的填表程式，autoForm 會改用高雄市網站示範"

    def _last_error(self, r):
        for line in reversed(r["log"]):
            t = line["text"]
            if any(k in t for k in ("Error", "Exception", "錯誤", "找不到", "無法")):
                return t
        return ""

    # ---------------- 操作 ----------------
    def continue_(self, rid):
        """給「按 Enter 繼續」的驗證模式用：把 Enter 送進 autoForm。"""
        r = self.get(rid)
        if not r:
            raise FileNotFoundError(rid)
        proc = self.procs.get(rid)
        if proc and proc.poll() is None and proc.stdin:
            try:
                proc.stdin.write(b"\n")
                proc.stdin.flush()
                self._log(r, "已通知 autoForm 繼續")
            except OSError:
                pass
        return r

    def cancel(self, rid):
        """取消／關閉瀏覽器。還沒按送出的話，什麼都不會送出去。"""
        r = self.get(rid)
        if not r:
            raise FileNotFoundError(rid)
        proc = self.procs.get(rid)
        if proc and proc.poll() is None:
            if r["state"] in ACTIVE:
                r["cancel_requested"] = True
            _kill_tree(proc)
            self._log(r, "已關閉填表瀏覽器")
            for _ in range(30):
                if not r.get("browser_open"):
                    break
                time.sleep(0.1)
        return r

    def get(self, rid):
        with self.lock:
            return self.reports.get(rid)

    def list(self):
        with self.lock:
            return [self.public(r) for r in sorted(self.reports.values(), key=lambda x: x["id"], reverse=True)]

    @staticmethod
    def public(r):
        out = {k: v for k, v in r.items() if k != "log"}
        out["log"] = r["log"][-60:]
        return out

    # ---------------- 工具 ----------------
    def _log(self, r, text):
        r["log"].append({"t": datetime.now().strftime("%H:%M:%S"), "text": text})
        if len(r["log"]) > 400:
            del r["log"][:100]
        print(f"[填表 {r['id']}] {text}", flush=True)

    def _save(self, r):
        p = self.dir / r["id"] / "report.json"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps({k: v for k, v in r.items() if k != "cancel_requested"},
                                      ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, p)
        except OSError:
            pass

    def _cleanup(self):
        with self.lock:
            old = sorted(self.reports)[:-KEEP]
            for rid in old:
                if self.reports[rid]["state"] in ACTIVE or rid in self.procs:
                    continue
                self.reports.pop(rid, None)
                shutil.rmtree(self.dir / rid, ignore_errors=True)

