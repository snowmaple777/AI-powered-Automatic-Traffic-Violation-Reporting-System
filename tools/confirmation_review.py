"""把確認實驗新增／升級事件輸出為前中後畫面，供逐件覆核；不修改標註。"""
import argparse
import json
from pathlib import Path
import cv2
import numpy as np


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('experiment', type=Path)
    p.add_argument('--variant', required=True)
    args = p.parse_args()
    manifest = json.loads((args.experiment / 'manifest.json').read_text(encoding='utf-8'))['manifest']
    baseline = next(iter(manifest['variants']))
    out = args.experiment / 'review'
    out.mkdir(exist_ok=True)
    videos = {p.stem: p for p in Path('input').rglob('*.mp4')}
    items = []
    for source in manifest['sources']:
        if source.get('truth'):
            truth = json.loads(Path(source['truth']).read_text(encoding='utf-8-sig'))
            videos.update({v: Path(s['file']) for v, s in truth['videos'].items()})
        source_dir = args.experiment / source['name']
        if not source_dir.exists():
            continue
        for folder in sorted(source_dir.iterdir()):
            if not (folder / 'done.json').exists():
                continue
            before = json.loads((folder / f'{baseline}.json').read_text(encoding='utf-8'))
            after = json.loads((folder / f'{args.variant}.json').read_text(encoding='utf-8'))
            for e in after:
                if e['status'] != 'confirmed' or any(b['status'] == 'confirmed' and b['rule_id'] == e['rule_id']
                    and b['object_id'] == e['object_id'] and abs(b['timestamp_sec'] - e['timestamp_sec']) <= 3 for b in before):
                    continue
                items.append(dict(source=source['name'], video=folder.name, event=e))
    for n, item in enumerate(items):
        e = item['event']
        t = e.get('confirmation_timestamp_sec', e['timestamp_sec'])
        path = videos[item['video']]
        cap = cv2.VideoCapture(str(path))
        tiles = []
        for delta in (-.5, 0, .5):
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0, t + delta) * 1000)
            ok, frame = cap.read()
            if not ok:
                raise RuntimeError(f'無法讀取 {path} {t + delta}')
            if delta == 0:
                vehicle = e.get('evidence', {}).get('confirmation_review', {}).get('after', {}).get('vehicle') or e['evidence'].get('vehicle', {})
                box = vehicle.get('bbox_xyxy')
                if box:
                    x1, y1, x2, y2 = map(round, box)
                    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 255), 2)
            frame = cv2.resize(frame, (640, 360))
            cv2.putText(frame, f"{item['video']} {t + delta:.2f}s", (8, 25), cv2.FONT_HERSHEY_SIMPLEX, .7, (0,255,255), 2)
            tiles.append(frame)
        cap.release()
        title = np.zeros((50, 1920, 3), dtype=np.uint8)
        cv2.putText(title, f"{n:03d} {item['source']} {e['object_id']} {e.get('confirmation_basis')}", (8, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, .65, (255,255,255), 1)
        dest = out / f"{n:03d}_{item['video']}.jpg"
        cv2.imencode('.jpg', np.vstack([title, np.hstack(tiles)]))[1].tofile(str(dest))
        item['image'] = str(dest)
    (out / 'index.json').write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'{len(items)} review sheets in {out}')


if __name__ == '__main__':
    main()
