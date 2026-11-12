"""產生目前版本的展示成果（進度回報用）：標註影片、違規片段、違規紀錄、動作備註與總覽頁。

短片：以 real_replay 重播保存的感知結果（JSONL＋遮罩＋原影片），用目前的後處理與規則重跑，
      輸出標註影片（--save-video）、違規片段、違規紀錄（*_violations.jsonl）與動作備註（--motion-notes）。
長片：用指定的違規紀錄與逐幀 JSONL 剪出違規片段（長片沒有保存遮罩，不重跑後處理）。
每個步驟都可中斷後重跑續做（已完成的影片會略過）；影片最後轉成 H.264 方便一般播放器播放。

  python tools/make_showcase.py short  --out outputs/showcase_20261110
  python tools/make_showcase.py encode --out outputs/showcase_20261110
  python tools/make_showcase.py long   --out outputs/showcase_20261110 --name A1 --video input/.../A1.mp4 \\
         --detections outputs/.../A1_detections.jsonl --violations outputs/.../A1_violations.jsonl
  python tools/make_showcase.py index  --out outputs/showcase_20261110
"""
import argparse
import html
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RULE = {"red_light_stop_line_crossing": "闖紅燈", "crosswalk_stopping": "停在斑馬線上",
        "car_motorcycle_waiting_area": "汽車駛入機車停等區", "double_yellow_line_crossing": "跨越雙黃線",
        "double_white_line_crossing": "跨越雙白線"}


def short(args):
    from tools.real_replay import replay
    started = time.time()
    folders = sorted(p for p in args.source.iterdir() if (p / f"{p.name}_detections.jsonl").is_file())
    for folder in folders:
        if args.videos and folder.name not in args.videos:
            continue
        target = args.out / "short" / folder.name
        if (target / ".done").is_file() or (target / "SKIPPED.txt").is_file():
            continue
        if time.time() - started > args.budget:
            print("PARTIAL", flush=True)
            return
        print("重播", folder.name, flush=True)
        try:
            replay(folder, target, extra=["--save-video", "--violation-clips", "--motion-notes"])
        except RuntimeError as error:  # 例：此平台無法解碼該影片
            (target / "SKIPPED.txt").write_text(str(error), encoding="utf-8")
            continue
        (target / ".done").write_text("ok", encoding="utf-8")
    print("DONE short", flush=True)


def encode(args):
    started = time.time()
    for path in sorted(args.out.rglob("*.mp4")):
        marker = path.with_suffix(".h264")
        if marker.is_file() or path.name.endswith(".tmp.mp4"):
            continue
        if time.time() - started > args.budget:
            print("PARTIAL", flush=True)
            return
        tmp = path.with_name(path.stem + ".tmp.mp4")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "24", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(tmp)], check=True)
        tmp.replace(path)
        marker.write_text("ok", encoding="utf-8")
    print("DONE encode", flush=True)


def long(args):
    from violation_clips import export_violation_clips
    target = args.out / "long" / args.name
    target.mkdir(parents=True, exist_ok=True)
    events = [json.loads(line) for line in args.violations.read_text(encoding="utf-8").splitlines() if line.strip()]
    shutil.copyfile(args.violations, target / f"{args.name}_violations.jsonl")
    export_violation_clips(args.video, args.detections, events, target / f"{args.name}_violation_clips")
    (target / ".done").write_text(json.dumps({"video": str(args.video), "detections": str(args.detections),
                                              "violations": str(args.violations)}, ensure_ascii=False), encoding="utf-8")
    print("DONE long", args.name, len(events), flush=True)


def snapshot(clip_mp4, image, before_seconds=5.0):
    """從違規片段取事件當下的畫面（片段從事件前 before_seconds 秒開始）。"""
    import cv2
    cap = cv2.VideoCapture(str(clip_mp4))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.set(cv2.CAP_PROP_POS_FRAMES, min(max(frames - 1, 0), int(round(before_seconds * fps))))
    ok, frame = cap.read()
    cap.release()
    if ok:
        ok, data = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        data.tofile(str(image))
    return ok


def index(args):
    from eval.metrics import evaluate, load_predictions
    truth = json.loads((ROOT / "eval/ground_truth.json").read_text(encoding="utf-8-sig"))
    out = args.out
    images = out / "images"
    images.mkdir(exist_ok=True)
    rows, sections = [], []
    for group in ("short", "long"):
        base = out / group
        if not base.is_dir():
            continue
        for folder in sorted(p for p in base.iterdir() if p.is_dir()):
            name = folder.name
            path = folder / f"{name}_violations.jsonl"
            if not path.is_file():
                continue
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            clips, offsets = {}, {}
            for meta in folder.glob("*_violation_clips/*/*.json"):
                if meta.name == "index.json":
                    continue
                data = json.loads(meta.read_text(encoding="utf-8"))
                event_id = (data.get("event") or {}).get("event_id")
                if event_id:
                    clips[event_id] = meta.with_suffix(".mp4")
                    offsets[event_id] = float(data.get("event_offset_sec", 5.0))
            annotated = folder / f"{name}_annotated.mp4"
            spec = truth["videos"].get(name, {})
            for event in events:
                clip = clips.get(event["event_id"])
                image = None
                if clip and clip.is_file():
                    image = images / f"{group}_{name}_{''.join(c if c.isalnum() or c in '-_' else '_' for c in event['event_id'])}.jpg"
                    if not image.is_file():
                        snapshot(clip, image, offsets.get(event["event_id"], 5.0))
                plate = ((event.get("plate") or {}).get("text") or (event.get("evidence", {}).get("vehicle") or {}).get("plate_text") or "")
                note = (event.get("motion_note") or {}).get("text") or ""
                rows.append(dict(group=group, video=name, time=round(event["timestamp_sec"], 2), frame=event["frame"],
                                 rule=RULE.get(event["rule_id"], event["rule_id"]),
                                 status="確認" if event["status"] == "confirmed" else "疑似",
                                 vehicle=event.get("vehicle_class"), plate=plate, motion_note=note,
                                 category=spec.get("category", ""), clip=str(clip.relative_to(out)) if clip else "",
                                 image=str(image.relative_to(out)) if image and image.is_file() else "",
                                 annotated=str(annotated.relative_to(out)) if annotated.is_file() else ""))
    short_folders = [p for p in (out / "short").iterdir() if p.is_dir()] if (out / "short").is_dir() else []
    _, metrics, plate_total, plate_correct = evaluate(truth, load_predictions(short_folders, "suspected"))
    (out / "events.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    import csv
    with open(out / "events.csv", "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["video"])
        writer.writeheader()
        writer.writerows(rows)
    metric_rows = "".join(f"<tr><td>{RULE.get(k, k)}</td><td>{v['tp']}</td><td>{v['fp']}</td><td>{v['fn']}</td></tr>"
                          for k, v in sorted(metrics.items()))
    cards = []
    for row in rows:
        media = (f'<img src="{html.escape(row["image"])}">' if row["image"] else "")
        links = " ".join(f'<a href="{html.escape(row[k])}">{label}</a>' for k, label in (("clip", "違規片段"), ("annotated", "完整標註影片")) if row[k])
        cards.append(f'<div class="card {"cf" if row["status"] == "確認" else ""}">{media}<div class="tx"><b>{html.escape(row["video"])}　{row["time"]} 秒</b>'
                     f'<span class="tag">{row["status"]}</span> {html.escape(row["rule"])}<br>{html.escape(row["vehicle"] or "")}'
                     f'{("・車牌 " + html.escape(row["plate"])) if row["plate"] else ""}<br><small>{html.escape(row["motion_note"])}</small><br>{links}</div></div>')
    long_rows = [r for r in rows if r["group"] == "long"]
    skipped = sorted(p.parent.name for p in (out / "short").glob("*/SKIPPED.txt")) if (out / "short").is_dir() else []
    skipped_note = (f"<p>本次未完成的短片：{'、'.join(skipped)}（見各資料夾 SKIPPED.txt），不計入上表。</p>" if skipped else "")
    page = f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><title>目前版本執行成果</title><style>
body{{font-family:system-ui,"Microsoft JhengHei",sans-serif;margin:16px;background:#fafafa;color:#222}}table{{border-collapse:collapse}}td,th{{border:1px solid #ccc;padding:3px 10px}}
.grid{{display:flex;flex-wrap:wrap;gap:10px}}.card{{width:330px;background:#fff;border:1px solid #ddd;border-top:5px solid #e67e22;border-radius:6px;overflow:hidden}}
.card.cf{{border-top-color:#c0392b}}.card img{{width:100%;display:block}}.tx{{padding:6px 8px;font-size:13px}}.tag{{background:#555;color:#fff;border-radius:3px;padding:0 5px;margin:0 4px}}</style></head><body>
<h1>目前版本執行成果</h1><p>短片：以保存的感知結果重播，用目前程式（含動作備註）重跑後處理與規則。長片：用目前規則的違規紀錄剪出片段。
事件全部需要人工覆核；「確認」代表規則條件成立，不代表法律上的違規認定。</p>
<h2>短片準確率（30 支計分、34 個標註事件）</h2><table><tr><th>規則</th><th>TP</th><th>FP</th><th>FN</th></tr>{metric_rows}</table>
<p>車牌完全正確：{plate_correct}/{plate_total}</p>{skipped_note}
<h2>事件（{len(rows)} 筆；長片 {len(long_rows)} 筆）</h2><div class="grid">{"".join(cards)}</div></body></html>"""
    (out / "index.html").write_text(page, encoding="utf-8")
    print("index", len(rows), "events", metrics, plate_correct, plate_total)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("step", choices=["short", "encode", "long", "index"])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=ROOT / "outputs/eval/baseline_18")
    parser.add_argument("--videos", nargs="*")
    parser.add_argument("--budget", type=float, default=float("inf"), help="秒數上限；超過就停在影片之間，可重跑續做")
    parser.add_argument("--name")
    parser.add_argument("--video", type=Path)
    parser.add_argument("--detections", type=Path)
    parser.add_argument("--violations", type=Path)
    args = parser.parse_args(argv)
    {"short": short, "encode": encode, "long": long, "index": index}[args.step](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
