"""幫一個資料夾裡的影片各產生一份「模擬 GPS 軌跡」（同名 .gpx，展示用）。

    python tools/make_sim_gpx.py ui/clips/123_at_gmail.com
    python tools/make_sim_gpx.py ui/clips/123_at_gmail.com --lat 22.6694 --lon 120.3026 --overwrite

- 起點：--lat/--lon（預設國立高雄大學），每支影片會在起點附近 1 公里內隨機錯開。
- 開始時間：影片檔的修改時間往前推影片長度（跟網頁上傳時的推算方式一樣）。
- 影片長度：有 OpenCV 或 ffprobe 就讀實際長度，都沒有就當 3 分鐘（軌跡比影片長沒關係）。
- 檔名當亂數種子，重跑會得到一樣的軌跡；GPX 裡會標註 simulated，網頁會顯示「模擬軌跡」。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import gps_sim  # noqa: E402

from core.gps_parser import TW  # noqa: E402

VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".m4v", ".ts", ".webm")


def video_seconds(path):
    try:
        import cv2  # type: ignore
        cap = cv2.VideoCapture(path)
        n, fps = cap.get(cv2.CAP_PROP_FRAME_COUNT), cap.get(cv2.CAP_PROP_FPS)
        cap.release()
        if n > 0 and fps > 0:
            return n / fps
    except Exception:  # noqa: BLE001
        pass
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
                             capture_output=True, text=True, timeout=20).stdout
        return float(json.loads(out)["format"]["duration"])
    except Exception:  # noqa: BLE001
        return 180.0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--lat", type=float, default=22.7335)
    ap.add_argument("--lon", type=float, default=120.2850)
    ap.add_argument("--spread", type=float, default=1000, help="起點錯開範圍（公尺）")
    ap.add_argument("--overwrite", action="store_true", help="已經有 .gpx 也重新產生")
    a = ap.parse_args()
    made = 0
    for cur, dirs, files in os.walk(a.folder):
        dirs[:] = [d for d in dirs if not d.startswith(("_", "."))]
        for f in sorted(files):
            if not f.lower().endswith(VIDEO_EXTS):
                continue
            path = os.path.join(cur, f)
            out = os.path.splitext(path)[0] + ".gpx"
            if os.path.exists(out) and not a.overwrite:
                print(f"略過（已有 GPX）：{f}")
                continue
            dur = video_seconds(path)
            rng = random.Random(f)
            lat = a.lat + rng.uniform(-1, 1) * a.spread / 111_320
            lon = a.lon + rng.uniform(-1, 1) * a.spread / 102_600
            start = datetime.fromtimestamp(os.path.getmtime(path), TW) - timedelta(seconds=dur)
            track = gps_sim.simulate(lat, lon, start.replace(microsecond=0), dur + 2, seed=f)
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(gps_sim.to_gpx(track, f))
            made += 1
            print(f"✓ {f}  {dur:5.1f} 秒  {len(track)} 點  起點 {lat:.5f},{lon:.5f}")
    print(f"完成，產生 {made} 個 GPX")


if __name__ == "__main__":
    main()
