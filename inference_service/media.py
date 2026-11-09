"""影像相關工具：讀影片資訊、畫即時預覽、片段轉 H.264、擷取截圖。

都只在「有 OpenCV」時才有作用；沒有的話回傳 None，服務照樣能跑。
"""
from __future__ import annotations

import os
import shutil
import subprocess

try:
    import cv2  # type: ignore
except ImportError:  # 推論服務也能用沒有 OpenCV 的 Python 啟動
    cv2 = None


def find_ffmpeg():
    """找 ffmpeg：系統 PATH → imageio-ffmpeg 套件附的執行檔。"""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg  # type: ignore
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


def video_meta(path):
    if cv2 is None:
        return None
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            return None
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        return {
            "fps": round(float(fps), 3), "frames": frames,
            "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0),
            "duration": round(frames / fps, 3) if fps else None,
        }
    finally:
        cap.release()


def read_frame(path, index):
    if cv2 is None:
        return None
    cap = cv2.VideoCapture(str(path))
    try:
        if index:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(index))
        ok, frame = cap.read()
        return frame if ok else None
    finally:
        cap.release()


LIGHT_COLORS = {"red": (60, 60, 255), "green": (80, 200, 80), "yellow": (40, 200, 240)}


def draw_preview(frame, record, out_path, max_width=960):
    """把 run_pipeline 的單幀辨識結果（detections.jsonl 的一行）畫在畫面上。"""
    if cv2 is None or frame is None:
        return False
    obs = (record or {}).get("observations", {})
    h, w = frame.shape[:2]
    thick = max(2, w // 640)
    font = max(0.6, w / 1600)
    # 停止線
    for mark in obs.get("road_markings", []) or []:
        if mark.get("class_name") != "stop line":
            continue
        for comp in mark.get("components", []) or []:
            line = comp.get("line_xyxy")
            if line and len(line) == 4:
                cv2.line(frame, (int(line[0]), int(line[1])), (int(line[2]), int(line[3])), (255, 255, 0), thick)
    # 號誌
    for light in obs.get("traffic_lights", []) or []:
        b = light.get("bbox_xyxy")
        if not b:
            continue
        color = LIGHT_COLORS.get(light.get("class_name"), (200, 200, 200))
        cv2.rectangle(frame, (int(b[0]), int(b[1])), (int(b[2]), int(b[3])), color, thick)
        cv2.putText(frame, f"light:{light.get('class_name')}", (int(b[0]), max(20, int(b[1]) - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, font, color, thick)
    # 車輛（rule_vehicles 已合併騎士與機車）
    for v in obs.get("rule_vehicles") or obs.get("vehicles") or []:
        b = v.get("bbox_xyxy")
        if not b:
            continue
        color = (235, 170, 40)
        cv2.rectangle(frame, (int(b[0]), int(b[1])), (int(b[2]), int(b[3])), color, thick)
        label = f"{v.get('class_name', '')} {v.get('confidence', 0):.2f}"
        if v.get("distance_m") is not None:
            label += f" {v['distance_m']:.0f}m"
        if v.get("plate_text"):
            label += f" {v['plate_text']}"
        cv2.putText(frame, label, (int(b[0]), max(20, int(b[1]) - 8)), cv2.FONT_HERSHEY_SIMPLEX, font, color, thick)
    info = (record or {}).get("frame", {})
    cv2.rectangle(frame, (0, 0), (w, int(42 * font / 0.8)), (20, 20, 20), -1)
    cv2.putText(frame, f"LIVE | frame {info.get('index', '?')} | t={info.get('timestamp_sec', 0):.1f}s",
                (12, int(30 * font / 0.8)), cv2.FONT_HERSHEY_SIMPLEX, font * 1.1, (255, 255, 255), thick)
    if w > max_width:
        frame = cv2.resize(frame, (max_width, int(h * max_width / w)))
    tmp = str(out_path) + ".tmp.jpg"
    cv2.imwrite(tmp, frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    os.replace(tmp, out_path)
    return True


def snapshot(video_path, out_path, seconds=None, frame_index=None, max_width=1280):
    """從影片擷取一格存成 jpg。"""
    if cv2 is None:
        return False
    cap = cv2.VideoCapture(str(video_path))
    try:
        if not cap.isOpened():
            return False
        if frame_index is not None:
            cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(frame_index)))
        elif seconds is not None:
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, float(seconds)) * 1000)
        ok, frame = cap.read()
        if not ok:
            return False
        h, w = frame.shape[:2]
        if w > max_width:
            frame = cv2.resize(frame, (max_width, int(h * max_width / w)))
        return bool(cv2.imwrite(str(out_path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88]))
    finally:
        cap.release()


def to_h264(src, dst, ffmpeg):
    """片段轉成瀏覽器能播的 H.264；失敗就原檔複製（UI 會改播原影片片段）。回傳是否為 H.264。"""
    if ffmpeg:
        r = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(src), "-c:v", "libx264",
                            "-preset", "veryfast", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                            "-an", str(dst)], capture_output=True)
        if r.returncode == 0 and os.path.exists(dst):
            return True
    shutil.copyfile(src, dst)
    return False
