"""Portable RLMD video inference. Hybrid is the default."""
import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import torch
from mmengine import Config
from mmseg.apis import init_model, inference_model
from video_utils import create_overlay, draw_info, process_special_classes

ROOT = Path(__file__).resolve().parent
DEFAULT_CHECKPOINT = ROOT / 'models/segformer_b2_rlmd_768_best_56000.pth'


def get_config(mode='hybrid'):
    cfg = Config.fromfile(str(ROOT / 'configs/rlmd/inference.py'))
    cfg.model.test_cfg = dict(mode='whole') if mode == 'whole' else dict(
        mode='slide', crop_size=(512, 512), stride=(341, 341),
        blend='uniform' if mode == 'uniform' else 'gaussian',
        blend_sigma=.25, whole_weight=.25 if mode == 'hybrid' else 0.)
    return cfg


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('videos', nargs='+', type=Path)
    parser.add_argument('--checkpoint', type=Path, default=DEFAULT_CHECKPOINT)
    parser.add_argument('--mode', choices=['hybrid', 'smooth', 'whole', 'uniform'], default='hybrid')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'outputs')
    parser.add_argument('--device', default='cuda:0' if torch.cuda.is_available() else 'cpu')
    parser.add_argument('--no-preview', action='store_true')
    parser.add_argument('--max-frames', type=int, default=0, help='0 = full video')
    args = parser.parse_args(argv)
    if args.max_frames < 0:
        parser.error('--max-frames must be >= 0')
    for path in [args.checkpoint] + args.videos:
        if not path.is_file():
            parser.error(f'File not found: {path}. For weights, see models/README.md.')
    cfg = get_config(args.mode)
    model = init_model(cfg, str(args.checkpoint), device=args.device)
    model.eval()
    out = args.output_dir.resolve() / (datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '_' + args.mode)
    out.mkdir(parents=True, exist_ok=False)
    manifest = dict(checkpoint=str(args.checkpoint.resolve()), mode=args.mode,
                    test_cfg=dict(cfg.model.test_cfg), device=args.device,
                    note='Raw argmax overlay; CW/SL HUD uses confidence, ROI and component filters. No audio.', videos=[])
    try:
        for number, source in enumerate(args.videos, 1):
            cap = cv2.VideoCapture(str(source.resolve()))
            writer = None
            count = 0
            stopped = False
            entry = dict(input=str(source.resolve()), status='running')
            manifest['videos'].append(entry)
            try:
                if not cap.isOpened():
                    raise RuntimeError(f'Cannot open video: {source}')
                width, height = (int(cap.get(k)) for k in (cv2.CAP_PROP_FRAME_WIDTH, cv2.CAP_PROP_FRAME_HEIGHT))
                fps = cap.get(cv2.CAP_PROP_FPS)
                total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                if width <= 0 or height <= 0 or not np.isfinite(fps) or fps <= 0:
                    raise RuntimeError(f'Invalid video metadata: {source}')
                target = out / f'{number:02d}_{source.stem}_{args.mode}.mp4'
                writer = cv2.VideoWriter(str(target), cv2.VideoWriter_fourcc(*'mp4v'), fps, (width, height))
                if not writer.isOpened():
                    raise RuntimeError(f'Cannot write: {target}')
                entry.update(output=str(target), expected_frames=total, fps=fps)
                while not args.max_frames or count < args.max_frames:
                    ok, frame = cap.read()
                    if not ok:
                        break
                    start = time.perf_counter()
                    with torch.inference_mode():
                        result = inference_model(model, frame)
                        pred = result.pred_sem_seg.data[0].cpu().numpy().astype(np.uint8)
                        probs = result.seg_logits.data.float().softmax(dim=0)
                        processed = process_special_classes(pred, probs[2].cpu().numpy(), probs[3].cpu().numpy())
                    count += 1
                    rendered = draw_info(create_overlay(frame, pred), count, total,
                                         1 / max(time.perf_counter()-start, 1e-6), processed)
                    writer.write(rendered)
                    del result, probs
                    if not args.no_preview:
                        scale = min(1., 1280 / width)
                        cv2.imshow('RLMD - ' + args.mode, cv2.resize(rendered, (int(width*scale), int(height*scale))))
                        if cv2.waitKey(1) & 0xff == ord('q'):
                            stopped = True
                            break
                    if count % 30 == 0:
                        print(f'{source.name}: {count}/{total}', flush=True)
                if count == 0:
                    raise RuntimeError(f'No frames decoded: {source}')
                entry['status'] = 'preview_stopped' if stopped else 'complete'
                if args.max_frames and count >= args.max_frames:
                    entry['status'] = 'frame_limit'
                elif not stopped and total > 0 and count != total:
                    raise RuntimeError(f'Incomplete decode: {count}/{total} frames')
                print('Saved:', target, flush=True)
            except BaseException:
                entry['status'] = 'interrupted_or_failed'
                raise
            finally:
                entry['frames_written'] = count
                cap.release()
                if writer is not None:
                    writer.release()
                cv2.destroyAllWindows()
            if stopped:
                break
    finally:
        (out / 'run.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
