"""把資料夾裡影片的模擬 GPX 換成「沿著真實道路走」的軌跡（展示用）。

    python ui/tools/make_road_gpx.py ui/clips/123_at_gmail.com
    python ui/tools/make_road_gpx.py ui/clips/123_at_gmail.com --lat 22.6694 --lon 120.3026

- 路線由 core/road_sim.py 產生：OSRM 規劃真實路線、只走有路名的路段，
  再用 Nominatim 確認起點／中點／終點都查得到路名，查不到就換一條。
- 時間沿用舊 GPX 的開始時間與點數，影片時間對齊不變；沒有舊 GPX 就跟 make_sim_gpx 一樣從影片推算。
- 舊 GPX 先備份到 <資料夾>/_old_gpx/（已經備份過就不再覆蓋，保留最原始的版本）。
- 位置仍然不是影片真正的拍攝地點，所以 GPX 照樣標註 simulated，網頁會顯示「模擬軌跡」。
- 檔名當亂數種子，重跑會得到一樣的路線。需要網路，全部跑完大約一兩分鐘。
"""
from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import road_sim  # noqa: E402
from core.gps_parser import TW  # noqa: E402
from make_sim_gpx import VIDEO_EXTS, video_seconds  # noqa: E402


def old_times(gpx_path):
    """舊 GPX 每一點的時間；沒有或讀不到回傳 []。"""
    try:
        with open(gpx_path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return []
    out = []
    for t in re.findall(r"<time>([^<]+)</time>", raw):
        try:
            out.append(datetime.fromisoformat(t.strip().replace("Z", "+00:00")))
        except ValueError:
            pass
    return out


def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--lat", type=float, default=22.7335, help="起點中心（預設國立高雄大學）")
    ap.add_argument("--lon", type=float, default=120.2850)
    ap.add_argument("--spread", type=float, default=1500, help="起點錯開範圍（公尺）")
    a = ap.parse_args()

    backup = os.path.join(a.folder, "_old_gpx")
    made, failed = 0, []
    for f in sorted(os.listdir(a.folder)):
        if not f.lower().endswith(VIDEO_EXTS):
            continue
        path = os.path.join(a.folder, f)
        out = os.path.splitext(path)[0] + ".gpx"
        times = old_times(out)
        if times:
            start, n = times[0], len(times)
        else:
            dur = video_seconds(path)
            start = (datetime.fromtimestamp(os.path.getmtime(path), TW) - timedelta(seconds=dur)).replace(microsecond=0)
            n = max(2, int(math.ceil(dur + 2)) + 1)
        print(f"・{f}（{n} 點）")
        try:
            track, roads = road_sim.simulate(a.lat, a.lon, start, n - 1, seed=f, n=n, spread=a.spread,
                                             check="ends", tries=12, log=print)
        except road_sim.Offline as e:
            print(f"\n✗ {e}\n  請確認這台電腦有網路（或沒有被防火牆／代理擋住）後再跑一次。已處理的檔案不受影響。")
            sys.exit(1)
        if not track:
            print("  ✗ 試了 12 次都找不到合適的路線，保留舊檔")
            failed.append(f)
            continue
        if os.path.exists(out):
            os.makedirs(backup, exist_ok=True)
            dst = os.path.join(backup, os.path.basename(out))
            if not os.path.exists(dst):
                shutil.copy2(out, dst)
        tmp = out + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(road_sim.to_gpx(track, f, roads))
        os.replace(tmp, out)
        made += 1
        print(f"  ✓ 起點 {track[0]['lat']:.5f},{track[0]['lon']:.5f}　經過：{' → '.join(roads)}")
    print(f"\n完成：更新 {made} 個 GPX" + (f"，失敗 {len(failed)} 個：{', '.join(failed)}" if failed else ""))
    if made:
        print(f"舊檔備份在 {backup}")


if __name__ == "__main__":
    main()
