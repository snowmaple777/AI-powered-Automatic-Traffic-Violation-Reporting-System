"""分析器（Analyzer）接口與「模擬分析器」。

UI 只認得 BaseAnalyzer 的介面：
    analyzer.analyze(job, report) -> list[dict]   # 違規事件列表
    report(stage, progress, message, preview=None)

現在使用 SimulatedAnalyzer：真的會讀取影片、抽幀、畫出偵測框、剪輯片段，
但「偵測結果與違規判斷」是依影片內容產生的假資料。
之後要接 traffic_ai / perception，只要新寫一個 Analyzer 產生同樣格式的事件即可
（見檔案最下方的 TrafficAIAnalyzer 範本）。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import subprocess
import time

try:
    import cv2  # type: ignore
except ImportError:  # 沒有 opencv 仍可用（只是沒有畫面）
    cv2 = None

# ---------------------------------------------------------------------------
# 分析流程階段（UI 進度條依此顯示）
# ---------------------------------------------------------------------------
STAGES = [
    ("receive", "接收影片與 GPS 資料"),
    ("decode", "影片解碼與逐格擷取"),
    ("detect", "物件判斷模型：車種／車牌／標線／號誌"),
    ("rules", "違規判斷邏輯"),
    ("clip", "依時間標記剪輯違規片段"),
    ("package", "整理違規資料"),
]

# ---------------------------------------------------------------------------
# 違規類型（文字對齊 autoForm info.txt 的「違規類別／違規事實」欄位）
# ---------------------------------------------------------------------------
VIOLATION_TYPES = {
    "red_light": {
        "label": "闖紅燈",
        "category": "闖紅燈",
        "fact": "汽機車駕駛人行經有燈光號誌管制之交岔路口闖紅燈",
        "desc": "{veh}（車牌 {plate}）於號誌為紅燈時越過停止線進入路口。",
        "vehicles": ["汽車", "機車"],
        "en": "RED LIGHT",
    },
    "double_white": {
        "label": "跨越雙白線變換車道",
        "category": "違規變換車道",
        "fact": "在劃有分向限制線（雙白線）之路段變換車道",
        "desc": "{veh}（車牌 {plate}）於雙白線路段跨越標線變換車道。",
        "vehicles": ["汽車", "機車"],
        "en": "CROSS DOUBLE WHITE",
    },
    "no_signal": {
        "label": "變換車道未打方向燈",
        "category": "未依規定使用燈光",
        "fact": "變換車道時未依規定使用方向燈",
        "desc": "{veh}（車牌 {plate}）變換車道前後均未顯示方向燈。",
        "vehicles": ["汽車", "機車"],
        "en": "NO TURN SIGNAL",
    },
    "red_line_parking": {
        "label": "紅線違規停車",
        "category": "違規停車",
        "fact": "在禁止臨時停車處所（紅線）停車",
        "desc": "{veh}（車牌 {plate}）停放於劃設紅線之路段。",
        "vehicles": ["汽車"],
        "en": "RED LINE PARKING",
    },
    "moto_lane": {
        "label": "機車行駛禁行機車道",
        "category": "不依規定行駛車道",
        "fact": "機車行駛於禁行機車道",
        "desc": "{veh}（車牌 {plate}）行駛於標示禁行機車之車道。",
        "vehicles": ["機車"],
        "en": "MOTO IN NO-MOTO LANE",
    },
}

SUSPECT_REASONS = [
    "車牌部分字元遮蔽，OCR 信心偏低",
    "號誌燈號判斷受逆光影響",
    "車輛被前車部分遮擋，軌跡不連續",
    "標線磨損，標線分割結果不完整",
]

PLATE_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # 台灣車牌不使用 I、O


def _plate(rng, vehicle):
    letters = "".join(rng.choice(PLATE_LETTERS) for _ in range(3))
    if vehicle == "機車":
        return f"{letters}-{rng.randint(0, 999):03d}"
    return f"{letters}-{rng.randint(0, 9999):04d}"


def _lerp(a, b, r):
    return a + (b - a) * r


def fmt_ts(sec: float) -> str:
    m, s = divmod(max(sec, 0.0), 60)
    return f"{int(m):02d}:{s:04.1f}"


class BaseAnalyzer:
    name = "base"
    display_name = "未定義"

    def analyze(self, job: dict, report) -> list:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 模擬分析器
# ---------------------------------------------------------------------------
class SimulatedAnalyzer(BaseAnalyzer):
    name = "simulated"
    display_name = "模擬模式（假資料，流程與輸出格式皆與正式版相同）"

    def __init__(self, speed: float = 1.0):
        self.speed = max(0.2, speed)

    # ---- 共用工具 ----
    def _sleep(self, sec):
        time.sleep(sec / self.speed)

    def _video_meta(self, path):
        meta = {"fps": 30.0, "frames": 0, "width": 1280, "height": 720, "duration": 30.0}
        if cv2 is None:
            return meta
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            return meta
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        meta.update(
            fps=float(fps), frames=frames,
            width=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1280),
            height=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 720),
            duration=frames / fps if frames and fps else 30.0,
        )
        cap.release()
        return meta

    def _plan(self, rng, duration):
        """產生背景車輛軌跡與違規事件。"""
        tracks, tid = [], 1
        t = 0.0
        while t < duration:
            life = rng.uniform(2.5, 7.0)
            cls = rng.choices(["car", "motorcycle", "bus", "truck", "person"], [6, 5, 1, 1, 1])[0]
            sx = rng.uniform(0.15, 0.85)
            bg_plate = None
            if cls in ("car", "motorcycle") and rng.random() < 0.5:
                bg_plate = _plate(rng, "汽車" if cls == "car" else "機車")
            tracks.append({
                "id": tid, "cls": cls, "t0": t, "t1": min(duration, t + life),
                "c0": (sx, rng.uniform(0.50, 0.58)), "c1": (sx + rng.uniform(-0.25, 0.25), rng.uniform(0.62, 0.80)),
                "s0": rng.uniform(0.05, 0.08), "s1": rng.uniform(0.12, 0.22),
                "conf": rng.uniform(0.62, 0.95), "plate": bg_plate, "violator": False,
            })
            tid += 1
            t += rng.uniform(0.6, 1.6)

        n_events = max(1, min(4, int(duration // 12) + 1))
        margin = min(3.0, duration * 0.15)
        seg = (duration - 2 * margin) / n_events
        events = []
        keys = list(VIOLATION_TYPES.keys())
        rng.shuffle(keys)
        for i in range(n_events):
            vkey = keys[i % len(keys)]
            vt = VIOLATION_TYPES[vkey]
            vehicle = rng.choice(vt["vehicles"])
            t_hit = margin + seg * i + rng.uniform(0.25, 0.75) * seg
            conf = round(rng.uniform(0.62, 0.97), 2)
            plate_conf = round(rng.uniform(0.70, 0.99), 2)
            confirmed = conf >= 0.80 and plate_conf >= 0.85
            plate = _plate(rng, vehicle)
            sx = rng.uniform(0.3, 0.7)
            track = {
                "id": tid, "cls": "car" if vehicle == "汽車" else "motorcycle",
                "t0": max(0.0, t_hit - 3.5), "t1": min(duration, t_hit + 3.5),
                "c0": (sx, 0.52), "c1": (sx + rng.uniform(-0.2, 0.2), 0.72),
                "s0": 0.08, "s1": 0.24, "conf": conf, "plate": plate, "violator": True, "vkey": vkey,
            }
            tracks.append(track)
            events.append({
                "id": f"V{i + 1:03d}",
                "type": vkey,
                "label": vt["label"],
                "category": vt["category"],
                "fact": vt["fact"],
                "description": vt["desc"].format(veh=vehicle, plate=plate),
                "status": "confirmed" if confirmed else "suspected",
                "confidence": conf,
                "reason": "" if confirmed else rng.choice(SUSPECT_REASONS),
                "plate": plate,
                "plate_confidence": plate_conf,
                "vehicle_type": vehicle,
                "track_id": tid,
                "t": round(t_hit, 2),
                "t_start": round(max(0.0, t_hit - 3.0), 2),
                "t_end": round(min(duration, t_hit + 3.0), 2),
            })
            tid += 1
        return tracks, events

    def _box(self, tr, t, w, h):
        if not (tr["t0"] <= t <= tr["t1"]):
            return None
        r = (t - tr["t0"]) / max(1e-6, tr["t1"] - tr["t0"])
        cx, cy = _lerp(tr["c0"][0], tr["c1"][0], r) * w, _lerp(tr["c0"][1], tr["c1"][1], r) * h
        s = _lerp(tr["s0"], tr["s1"], r) * w
        bw, bh = (s * 0.55, s * 1.0) if tr["cls"] in ("motorcycle", "person") else (s, s * 0.8)
        return int(cx - bw / 2), int(cy - bh / 2), int(cx + bw / 2), int(cy + bh / 2)

    def _draw(self, frame, tracks, t, stage_text, show_boxes=True, focus=None):
        h, w = frame.shape[:2]
        if show_boxes:
            for tr in tracks:
                b = self._box(tr, t, w, h)
                if not b:
                    continue
                hot = tr["violator"] and (focus is None or focus == tr["id"])
                color = (40, 40, 235) if hot else (235, 170, 40)
                cv2.rectangle(frame, b[:2], b[2:], color, 3 if hot else 2)
                label = f"#{tr['id']} {tr['cls']} {tr['conf']:.2f}"
                if tr["plate"]:
                    label += f" {tr['plate']}"
                cv2.putText(frame, label, (b[0], max(18, b[1] - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
                if hot:
                    vt = VIOLATION_TYPES[tr["vkey"]]
                    cv2.putText(frame, "VIOLATION: " + vt["en"], (b[0], b[3] + 24),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (40, 40, 235), 2)
                    if tr["vkey"] == "red_light":
                        cv2.rectangle(frame, (int(w * 0.46), int(h * 0.08)), (int(w * 0.54), int(h * 0.2)), (60, 60, 255), 2)
                        cv2.putText(frame, "light: RED", (int(w * 0.46), int(h * 0.07)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (60, 60, 255), 2)
                    if tr["vkey"] in ("double_white", "no_signal", "moto_lane"):
                        x = int(w * 0.5)
                        cv2.line(frame, (x - 6, int(h * 0.55)), (x - 60, h), (255, 255, 255), 2)
                        cv2.line(frame, (x + 6, int(h * 0.55)), (x - 48, h), (255, 255, 255), 2)
        cv2.rectangle(frame, (0, 0), (w, 34), (20, 20, 20), -1)
        cv2.putText(frame, f"SIMULATION | {stage_text} | t={fmt_ts(t)}", (10, 24),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)
        return frame

    def _write_clip(self, src, dst, ev, tracks, fps, ffmpeg):
        cap = cv2.VideoCapture(src)
        cap.set(cv2.CAP_PROP_POS_MSEC, ev["t_start"] * 1000)
        ok, frame = cap.read()
        if not ok:
            cap.release()
            return False, None
        h, w = frame.shape[:2]
        scale = min(1.0, 960 / w)
        size = (int(w * scale) // 2 * 2, int(h * scale) // 2 * 2)
        raw = dst + ".raw.mp4"
        vw = None
        for codec in ("avc1", "mp4v"):
            vw = cv2.VideoWriter(raw, cv2.VideoWriter_fourcc(*codec), fps, size)
            if vw.isOpened():
                break
        snapshot = None
        t = ev["t_start"]
        while ok and t <= ev["t_end"]:
            self._draw(frame, tracks, t, "EVIDENCE CLIP", focus=ev["track_id"])
            small = cv2.resize(frame, size)
            vw.write(small)
            if snapshot is None and t >= ev["t"]:
                snapshot = small.copy()
            ok, frame = cap.read()
            t += 1.0 / fps
        vw.release()
        cap.release()
        if ffmpeg:  # 轉成瀏覽器一定能播的 H.264
            r = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", raw, "-c:v", "libx264",
                                "-pix_fmt", "yuv420p", "-movflags", "+faststart", dst],
                               capture_output=True)
            if r.returncode == 0:
                os.remove(raw)
            else:
                os.replace(raw, dst)
        else:
            os.replace(raw, dst)
        return True, snapshot

    # ---- 主流程 ----
    def analyze(self, job, report):
        video = job["video_path"]
        out_dir = job["dir"]
        report("receive", 0.1, f"收到影片 {os.path.basename(video)}（{os.path.getsize(video) / 1e6:.1f} MB）")
        meta = self._video_meta(video)
        job["video_meta"] = meta
        self._sleep(0.6)
        gps_n = len(job.get("gps_track") or [])
        report("receive", 0.6, f"影片資訊：{meta['width']}x{meta['height']}，{meta['fps']:.0f} FPS，長度 {fmt_ts(meta['duration'])}")
        self._sleep(0.5)
        report("receive", 1.0, f"GPS：{'軌跡 %d 點' % gps_n if gps_n > 1 else '單點座標'}")

        seed = int(hashlib.md5(f"{os.path.getsize(video)}-{meta['frames']}".encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        duration = meta["duration"]
        tracks, events = self._plan(rng, duration)
        preview = os.path.join(out_dir, "preview.jpg")

        cap = cv2.VideoCapture(video) if cv2 else None
        n_samples = 24
        stats = job.setdefault("stats", {})
        stats.update(frames_total=meta["frames"], frames_done=0, objects=0, plates=0,
                     violations=0, confirmed=0, suspected=0)
        found = job.setdefault("found", [])

        def grab(t):
            if cap is None:
                return None
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, f = cap.read()
            return f if ok else None

        # decode
        for i in range(n_samples // 2):
            t = duration * i / n_samples
            f = grab(t)
            if f is not None:
                cv2.imwrite(preview, self._draw(f, tracks, t, "DECODING", show_boxes=False))
            stats["frames_done"] = int(t * meta["fps"])
            report("decode", (i + 1) / (n_samples // 2), f"擷取影格 {int(t * meta['fps'])}/{meta['frames']}", preview)
            self._sleep(0.25)

        # detect
        seen, plates_seen = set(), set()
        for i in range(n_samples):
            t = duration * (i + 0.5) / n_samples
            f = grab(t)
            if f is not None:
                cv2.imwrite(preview, self._draw(f, tracks, t, "DETECTING"))
            active = [tr for tr in tracks if tr["t0"] <= t <= tr["t1"]]
            seen.update(tr["id"] for tr in active)
            plates = [tr["plate"] for tr in active if tr["plate"]]
            plates_seen.update(plates)
            stats.update(frames_done=int(t * meta["fps"]), objects=len(seen), plates=len(plates_seen))
            msg = f"t={fmt_ts(t)} 偵測到 {len(active)} 個目標"
            if plates:
                msg += f"，車牌 OCR：{', '.join(plates)}"
            report("detect", (i + 1) / n_samples, msg, preview)
            self._sleep(0.35)
        stats["frames_done"] = meta["frames"]
        report("detect", 1.0, f"共追蹤 {len(seen)} 個目標（車輛 / 機車 / 行人）")
        if cap is not None:
            cap.release()

        # rules
        for i, ev in enumerate(events):
            tag = "確定違規" if ev["status"] == "confirmed" else "疑似違規"
            extra = "" if ev["status"] == "confirmed" else f"（{ev['reason']}）"
            found.append({k: ev[k] for k in ("id", "type", "label", "plate", "status", "confidence", "vehicle_type", "t")})
            stats["violations"] = len(found)
            stats[ev["status"]] += 1
            report("rules", (i + 1) / len(events),
                   f"Track #{ev['track_id']} {ev['vehicle_type']} {ev['plate']} @ {fmt_ts(ev['t'])} → {ev['label']}，"
                   f"信心 {ev['confidence']:.2f}，標記為「{tag}」{extra}")
            self._sleep(0.7)

        # clip
        ffmpeg = shutil.which("ffmpeg")
        for i, ev in enumerate(events):
            clip = os.path.join(out_dir, f"{ev['id']}.mp4")
            snap = os.path.join(out_dir, f"{ev['id']}.jpg")
            ok, snapshot = (False, None)
            if cv2 is not None:
                ok, snapshot = self._write_clip(video, clip, ev, tracks, meta["fps"], ffmpeg)
            if snapshot is not None:
                cv2.imwrite(snap, snapshot)
            ev["clip"] = os.path.basename(clip) if ok else None
            ev["snapshot"] = os.path.basename(snap) if snapshot is not None else None
            report("clip", (i + 1) / len(events), f"剪輯 {ev['id']}：{fmt_ts(ev['t_start'])} ~ {fmt_ts(ev['t_end'])}")
            self._sleep(0.3)

        # package
        with open(os.path.join(out_dir, "violations.json"), "w", encoding="utf-8") as f:
            json.dump(events, f, ensure_ascii=False, indent=2)
        report("package", 1.0, f"完成：{len(events)} 筆違規（確定 {sum(e['status'] == 'confirmed' for e in events)}、"
                               f"疑似 {sum(e['status'] == 'suspected' for e in events)}）")
        return events


# ---------------------------------------------------------------------------
# 正式版接法範本（尚未啟用）
# ---------------------------------------------------------------------------
class TrafficAIAnalyzer(BaseAnalyzer):
    """之後接 traffic_ai/run_pipeline.py 的範本。

    做法：用 subprocess 執行
        python run_pipeline.py <video> --rule all --save-video
    讀它輸出的違規 JSONL，把每一筆轉成與 SimulatedAnalyzer 相同欄位的 dict
    （id / type / label / status / confidence / plate / t / t_start / t_end / clip / snapshot ...）。
    """
    name = "traffic_ai"
    display_name = "正式模型（traffic_ai，尚未串接）"

    def analyze(self, job, report):
        raise NotImplementedError("尚未串接 traffic_ai，請先使用模擬模式")


ANALYZERS = {"simulated": SimulatedAnalyzer, "traffic_ai": TrafficAIAnalyzer}
