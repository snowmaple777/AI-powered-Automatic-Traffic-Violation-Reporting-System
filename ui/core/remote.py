"""RemoteAnalyzer：透過 HTTP 呼叫推論服務（inference_service），把真實模型的結果轉成 UI 的違規格式。

推論服務可以在本機（http://127.0.0.1:8770）或雲端，UI 只差設定裡的網址。
"""
from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .analyzers import BaseAnalyzer, JobCancelled, VIOLATION_TYPES

# 組員規則 rule_id → UI 的違規類型
RULE_TYPES = {
    "red_light_stop_line_crossing": "red_light",
    "double_white_line_crossing": "double_white",
    "double_yellow_line_crossing": "double_yellow",
    "car_motorcycle_waiting_area": "moto_waiting_area",
    "crosswalk_stopping": "crosswalk_stopping",
}

# 模型車種 → (檢舉單車種, 顯示名稱)
VEHICLE_CLASSES = {
    "car": ("汽車", "小客車"), "truck": ("汽車", "貨車"), "bus": ("汽車", "公車"),
    "motorcycle": ("機車", "機車"), "bicycle": ("汽車", "自行車"),
}

# 判定條件的中文說明（True 代表符合）
CONDITION_LABELS = {
    # 紅燈越線
    "tracked_vehicle": "有持續追蹤到這台車",
    "usable_overlapping_stop_line": "車輛前方有可用的停止線",
    "before_line_observed": "看到車輛停在停止線前",
    "crossing_observed": "看到車輛越過停止線",
    "red_before_crossing": "越線前號誌為紅燈",
    "red_at_crossing": "越線當下號誌為紅燈",
    "strong_signal_evidence": "號誌燈號判斷證據充足",
    "stop_line_observed": "停止線為實際偵測（非推估）",
    "same_signal": "前後為同一個號誌",
    "same_stop_line": "前後為同一條停止線",
    # 雙黃線／雙白線
    "stable_origin": "跨越前的位置穩定",
    "stable_destination": "跨越後的位置穩定",
    "complete_band_crossing": "完整跨越雙線",
    "continuous_tracks": "車輛追蹤沒有中斷",
    "geometry_verified": "標線幾何已驗證",
    "uninterrupted_positions": "位置紀錄沒有缺漏",
    "directly_observed_pair": "兩條線都直接看到（非推估）",
    "transition_observed": "看到跨越的過程",
    "line_observed_at_footprint": "車輛所在位置確實看得到標線",
    "same_line_track": "全程為同一組標線",
    "camera_motion_compensated": "已補償鏡頭晃動",
    # 停等區／斑馬線
    "fresh_geometry": "區域位置為最新偵測",
    "temporal_support": "停留時間達到門檻",
    "lane_matched": "已配對到所在車道",
    "front_edge_from_lane": "停等區前緣來自車道偵測",
}
# 這兩個條件是「True 代表比較弱」，顯示時反過來
NEGATIVE_CONDITIONS = {
    "estimated_vehicle_geometry": "車輛位置為直接量測（非推估）",
    "predicted_region": "區域位置為直接偵測（非推估）",
}


def condition_list(conditions):
    out = []
    for c in conditions or []:
        key, val = c.get("key"), bool(c.get("ok"))
        if key in NEGATIVE_CONDITIONS:
            out.append({"key": key, "label": NEGATIVE_CONDITIONS[key], "ok": not val})
        else:
            out.append({"key": key, "label": CONDITION_LABELS.get(key, key), "ok": val})
    return out


def short_track(object_id):
    s = str(object_id or "")
    tail = s.rsplit("-", 1)[-1]
    return tail.lstrip("0") or s if tail.isdigit() else s


def to_ui_violation(ev, index, job_dir, files):
    vtype = RULE_TYPES.get(ev.get("rule_id"), "other")
    vt = VIOLATION_TYPES.get(vtype) or {
        "label": ev.get("rule_id") or "其他違規", "category": "其他", "fact": ev.get("description") or "",
        "desc": "{veh}（車牌 {plate}）" + (ev.get("description") or ""),
    }
    form_type, vlabel = VEHICLE_CLASSES.get(ev.get("vehicle_class"), ("汽車", ev.get("vehicle_class") or "車輛"))
    plate = (ev.get("plate_text") or "").strip()
    conds = condition_list(ev.get("conditions"))
    met, total = sum(c["ok"] for c in conds), len(conds)
    missing = [c["label"] for c in conds if not c["ok"]]
    t = float(ev.get("timestamp_sec") or 0)
    clip = ev.get("clip") or {}
    window = ev.get("clip_window") or {}
    reason = ""
    if missing:
        reason = "尚缺：" + "、".join(missing)
    elif ev.get("status") == "suspected":
        reason = "模型判定為疑似，送出前請人工確認"
    if not plate:
        reason = ("車牌未辨識，請在檢舉單補上。" + reason).strip()
    return {
        "id": f"V{index:03d}", "type": vtype, "label": vt["label"], "category": vt["category"], "fact": vt["fact"],
        "description": vt["desc"].format(veh=vlabel, plate=plate or "未辨識"),
        "model_description": ev.get("description"),
        "status": ev.get("status"), "reason": reason,
        "evidence_kind": "conditions", "confidence": (met / total) if total else 0.0,
        "conditions": conds, "conditions_met": met, "conditions_total": total,
        "plate": plate, "plate_confidence": float(ev.get("plate_confidence") or 0), "plate_missing": not plate,
        "vehicle_type": form_type, "vehicle_label": vlabel, "track_id": short_track(ev.get("object_id")),
        "t": round(t, 2),
        "t_start": round(float(clip.get("start_sec", window.get("start_sec", max(0.0, t - 5)))), 2),
        "t_end": round(float(clip.get("end_sec", window.get("end_sec", t + 5))), 2),
        "clip": files.get("clip"), "clip_h264": bool(clip.get("h264")), "snapshot": files.get("snapshot"),
        "rule_id": ev.get("rule_id"), "raw_event_id": ev.get("event_id"), "signal_state": ev.get("signal_state"),
        "distance_m": ev.get("distance_m"), "source": "model",
    }


class RemoteAnalyzer(BaseAnalyzer):
    name = "remote"
    display_name = "真實模型（推論服務）"
    STAGES = [
        ("upload", "上傳影片到推論服務", 0.08),
        ("loading", "載入模型", 0.07),
        ("analyzing", "逐幀辨識與違規判斷", 0.70),
        ("clipping", "剪輯違規片段", 0.07),
        ("packaging", "轉檔並整理結果", 0.08),
    ]

    def __init__(self, base_url, api_key=""):
        self.base = base_url.rstrip("/")
        self.api_key = api_key

    # ---------------- HTTP 工具 ----------------
    def _headers(self):
        return {"X-API-Key": self.api_key} if self.api_key else {}

    def _req(self, method, path, timeout=10):
        req = urllib.request.Request(self.base + path, method=method, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8")).get("error")
            except Exception:  # noqa: BLE001
                msg = str(e)
            raise RuntimeError(f"推論服務回應錯誤：{msg}") from None

    def _download(self, path, dest):
        req = urllib.request.Request(self.base + path, headers=self._headers())
        with urllib.request.urlopen(req, timeout=60) as r, open(dest + ".part", "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
        os.replace(dest + ".part", dest)

    def _upload(self, video_path, query, on_progress):
        u = urllib.parse.urlparse(self.base)
        conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(u.hostname, u.port or (443 if u.scheme == "https" else 80), timeout=120)
        size = os.path.getsize(video_path)
        path = (u.path.rstrip("/") or "") + "/jobs?" + urllib.parse.urlencode(query)
        conn.putrequest("POST", path)
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(size))
        for k, v in self._headers().items():
            conn.putheader(k, v)
        conn.endheaders()
        sent = 0
        with open(video_path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                conn.send(chunk)
                sent += len(chunk)
                on_progress(sent / size if size else 1.0)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
        data = json.loads(body) if body else {}
        if resp.status >= 400:
            raise RuntimeError(f"推論服務拒絕這支影片：{data.get('error', resp.status)}")
        return data

    def health(self, timeout=2):
        return self._req("GET", "/health", timeout=timeout)

    # ---------------- 主流程 ----------------
    def analyze(self, job, report):
        self._check_cancel(job)
        report("upload", 0.0, f"連線到推論服務 {self.base} …")
        try:
            h = self.health(timeout=4)
        except Exception:  # noqa: BLE001
            raise RuntimeError(f"連不到推論服務（{self.base}）。請先執行「啟動推論服務.cmd」再重新分析。") from None
        missing = [m["path"] for m in h.get("models", []) if m.get("enabled", True) and not m.get("exists")]
        if missing:
            raise RuntimeError("推論服務缺少模型檔：" + "、".join(missing) + "。請先執行「下載模型.cmd」。")
        gpu = h.get("gpu") or {}
        report("upload", 0.02, "推論服務已連線（" + (f"GPU：{gpu.get('name')}" if gpu.get("cuda") else "沒有偵測到 GPU，將用 CPU 執行，速度會較慢") + "）")

        opts = job.get("options") or {}
        query = {"filename": job.get("video_name") or "video.mp4", "rules": opts.get("rules") or "all"}
        if opts.get("max_seconds"):
            query["max_seconds"] = opts["max_seconds"]
        last = [0.0]

        def on_up(frac):
            if frac - last[0] >= 0.1 or frac >= 1:
                last[0] = frac
                report("upload", frac, f"上傳影片 {frac * 100:.0f}%" if frac < 1 else None)

        remote = self._upload(job["video_path"], query, on_up)
        rid = remote["id"]
        job["remote"] = {"url": self.base, "job_id": rid}
        report("upload", 1.0, f"上傳完成，推論服務工作編號 {rid}")

        log_seen, preview_seen, stage = 0, 0, "loading"
        while True:
            if job.get("cancel"):
                try:
                    self._req("POST", f"/jobs/{rid}/cancel")
                except Exception:  # noqa: BLE001
                    pass
                raise JobCancelled()
            st = self._req("GET", f"/jobs/{rid}", timeout=10)
            state, sstage = st.get("state"), st.get("stage")
            frames_done, frames_total = st.get("frames_done") or 0, st.get("frames_total")
            found = st.get("found") or []
            job["stats"] = {
                "frames_done": frames_done, "frames_total": frames_total or 0,
                "objects": (st.get("stats") or {}).get("objects", 0), "plates": (st.get("stats") or {}).get("plates", 0),
                "violations": len(found), "confirmed": sum(f.get("status") == "confirmed" for f in found),
                "suspected": sum(f.get("status") == "suspected" for f in found),
            }
            job["fps"], job["eta_sec"] = st.get("fps_recent"), st.get("eta_sec")
            if st.get("video") and not job.get("video_meta"):
                job["video_meta"] = st["video"]
            job["found"] = [self._brief(f, i) for i, f in enumerate(found, 1)]

            if sstage in ("queued", "loading"):
                stage, prog = "loading", 0.1 if sstage == "queued" else 0.5
            elif sstage == "analyzing":
                stage, prog = "analyzing", (frames_done / frames_total) if frames_total else 0.5
            elif sstage == "clipping":
                stage, prog = "clipping", min(0.9, (st.get("clips_done") or 0) / max(1, len(found)))
            elif sstage in ("packaging", "done"):
                stage, prog = "packaging", 0.5
            # 新的紀錄行
            total, logs = st.get("log_total") or 0, st.get("log") or []
            new = logs[-(total - log_seen):] if total > log_seen else []
            log_seen = total
            msgs = [x.get("msg") for x in new if x.get("msg")]
            if sstage == "queued" and st.get("queue_position"):
                msgs.append(f"排隊中，前面還有 {st['queue_position'] - 1} 個工作")
            for m in msgs[:-1]:
                report(stage, prog, m)
            pv = st.get("preview_version") or 0
            got_preview = False
            if pv != preview_seen:
                try:
                    self._download(f"/jobs/{rid}/preview.jpg", os.path.join(job["dir"], "preview.jpg"))
                    preview_seen, got_preview = pv, True
                except Exception:  # noqa: BLE001
                    pass
            report(stage, prog, msgs[-1] if msgs else None, got_preview or None)

            if state == "done":
                break
            if state in ("error", "cancelled", "interrupted"):
                if state == "cancelled" and job.get("cancel"):
                    raise JobCancelled()
                raise RuntimeError(st.get("error") or f"推論服務工作 {state}")
            time.sleep(1.0)

        result = self._req("GET", f"/jobs/{rid}/result", timeout=30)
        events = []
        items = result.get("events") or []
        for i, ev in enumerate(items, 1):
            vid = f"V{i:03d}"
            files = {}
            if ev.get("clip"):
                self._download(f"/jobs/{rid}/files/{ev['clip']['file']}", os.path.join(job["dir"], f"{vid}.mp4"))
                files["clip"] = f"{vid}.mp4"
            if ev.get("snapshot"):
                self._download(f"/jobs/{rid}/files/{ev['snapshot']}", os.path.join(job["dir"], f"{vid}.jpg"))
                files["snapshot"] = f"{vid}.jpg"
            events.append(to_ui_violation(ev, i, job["dir"], files))
            report("packaging", 0.5 + 0.5 * i / max(1, len(items)), f"下載 {vid} 的佐證片段與截圖")
        if result.get("video"):
            job["video_meta"] = result["video"]
        job["stats"]["frames_done"] = job["stats"].get("frames_total") or job["stats"].get("frames_done")
        with open(os.path.join(job["dir"], "violations.json"), "w", encoding="utf-8") as f:
            json.dump(events, f, ensure_ascii=False, indent=2)
        report("packaging", 1.0, f"完成：{len(events)} 筆違規（確認 {sum(e['status'] == 'confirmed' for e in events)}、"
                                 f"疑似 {sum(e['status'] == 'suspected' for e in events)}），模型分析用時 "
                                 f"{result.get('elapsed_sec', 0):.0f} 秒")
        return events

    @staticmethod
    def _check_cancel(job):
        if job.get("cancel"):
            raise JobCancelled()

    @staticmethod
    def _brief(f, i):
        vtype = RULE_TYPES.get(f.get("rule_id"), "other")
        vt = VIOLATION_TYPES.get(vtype) or {"label": f.get("rule_id") or "違規"}
        total = f.get("total") or 0
        return {
            "id": f"F{i:02d}", "type": vtype, "label": vt["label"], "plate": f.get("plate_text") or "未辨識",
            "status": f.get("status"), "confidence": (f.get("met", 0) / total) if total else 0,
            "evidence_kind": "conditions", "conditions_met": f.get("met", 0), "conditions_total": total,
            "vehicle_type": VEHICLE_CLASSES.get(f.get("vehicle_class"), ("汽車", f.get("vehicle_class") or "車輛"))[1],
            "t": f.get("timestamp_sec") or 0,
        }
