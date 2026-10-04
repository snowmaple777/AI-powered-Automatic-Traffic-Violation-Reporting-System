import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image


def parse_args():
    parser = argparse.ArgumentParser(description='Build RLMD per-image class statistics for RCS.')
    parser.add_argument('--ann-dir', default='data/rlmd/annotations/train')
    parser.add_argument('--out', default='data/rlmd/rcs_stats.json')
    return parser.parse_args()


def main():
    args = parse_args()
    ann_dir = Path(args.ann_dir)
    out_path = Path(args.out)

    if not ann_dir.exists():
        raise FileNotFoundError(f'Annotation directory not found: {ann_dir}')

    masks = sorted(ann_dir.glob('*.png'))
    if not masks:
        raise RuntimeError(f'No PNG masks found in: {ann_dir}')

    image_stats = {}
    class_totals = {}

    for i, mask_path in enumerate(masks, 1):
        with Image.open(mask_path) as img:
            mask = np.asarray(img)

        ids, counts = np.unique(mask, return_counts=True)
        per_image = {}
        for class_id, count in zip(ids, counts):
            class_id = int(class_id)
            count = int(count)
            per_image[str(class_id)] = count
            class_totals[str(class_id)] = class_totals.get(str(class_id), 0) + count

        image_stats[mask_path.stem] = per_image
        if i % 100 == 0 or i == len(masks):
            print(f'[{i}/{len(masks)}] {mask_path.name}')

    result = {
        'num_images': len(masks),
        'images': image_stats,
        'class_pixel_totals': class_totals,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print('\nDone.')
    print('Saved:', out_path)
    print('Images:', len(masks))
    print('Class totals:')
    for k in sorted(class_totals, key=lambda x: int(x)):
        print(f'  {k:>2}: {class_totals[k]}')


if __name__ == '__main__':
    main()
