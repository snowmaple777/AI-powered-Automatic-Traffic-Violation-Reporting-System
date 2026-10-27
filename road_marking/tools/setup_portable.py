"""Verify checked-in runtime files; hydrate only the branch's Git LFS model."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MODEL = 'work_dirs/segformer_b2_rlmd_expanded_weather_relearn_768/best_mIoU_iter_56000.pth'
MODEL_URL = 'https://github.com/snowmaple777/AI-powered-Automatic-Traffic-Violation-Reporting-System/raw/refs/heads/feature/road-marking/road_marking/' + MODEL


def sha(p):
    h = hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda: f.read(8*1024*1024), b''):
            h.update(b)
    return h.hexdigest()


def verify_runtime(root=ROOT):
    manifest = json.loads((root/'release/runtime_manifest.json').read_text(encoding='utf-8'))
    for row in manifest['files']:
        relative = Path(row['path'])
        if relative.is_absolute() or '..' in relative.parts or ':' in row['path']:
            raise ValueError('Unsafe runtime path')
        p = root/relative
        # GitHub Source ZIP may contain an LFS pointer. Fetch that exact model
        # from the SAME branch; never use a Release or a third-party model.
        pointer = p.is_file() and p.stat().st_size < 1024 and p.read_bytes().startswith(b'version https://git-lfs.github.com/spec/v1')
        if row['path'] == MODEL and (not p.exists() or pointer):
            p.parent.mkdir(parents=True, exist_ok=True)
            temporary = p.with_suffix('.pth.downloading')
            print('Retrieving this branch Git LFS model:', MODEL_URL, flush=True)
            request = urllib.request.Request(MODEL_URL, headers={'User-Agent':'road-marking-setup'})
            count = 0
            with urllib.request.urlopen(request, timeout=90) as source, temporary.open('wb') as output:
                while True:
                    block=source.read(4*1024*1024)
                    if not block:break
                    count += len(block)
                    if count > row['size']:raise ValueError('Model download exceeds approved size')
                    output.write(block)
            if count != row['size'] or sha(temporary) != row['sha256']:
                raise ValueError('Git LFS download SHA/size mismatch; not using it')
            temporary.replace(p)
        if not p.is_file() or p.stat().st_size != row['size'] or sha(p) != row['sha256']:
            raise ValueError('Required checked-in runtime file missing or changed: '+str(p))
    print('PASS branch model + 374-image validation set: every SHA256 verified', flush=True)


def main():
    import argparse
    parser=argparse.ArgumentParser(__doc__)
    parser.add_argument('--files-only',action='store_true')
    args=parser.parse_args()
    os.environ['MPLBACKEND']='Agg'
    verify_runtime()
    if args.files_only:return
    import imageio_ffmpeg
    import yaml
    candidates=list((Path(imageio_ffmpeg.__file__).resolve().parent/'binaries').glob('ffmpeg*.exe'))
    if len(candidates)!=1:raise FileNotFoundError('Expected bundled Windows FFmpeg in imageio-ffmpeg wheel')
    ffmpeg=candidates[0]
    encoders=subprocess.run([str(ffmpeg),'-hide_banner','-encoders'],capture_output=True,text=True,check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    if 'libx264' not in encoders.stdout:raise RuntimeError('Bundled FFmpeg lacks libx264')
    config=yaml.safe_load((ROOT/'configs/road_marking_infer.yaml').read_text(encoding='utf-8-sig'))
    config['overlay']['ffmpeg']=str(ffmpeg)
    target=ROOT/'work_dirs/portable_settings.yaml';target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(yaml.safe_dump(config,sort_keys=False,allow_unicode=True),encoding='utf-8')
    sys.path.insert(0,str(ROOT))
    import torch,mmcv,mmengine
    import mmseg.visualization
    from mmseg.utils.tokenizer import SimpleTokenizer
    from mmengine.config import Config
    from mmengine.utils import import_modules_from_strings
    if not torch.cuda.is_available():raise RuntimeError('NVIDIA CUDA GPU required; CPU fallback is disabled')
    cfg=Config.fromfile(str(ROOT/config['model_config']),lazy_import=False,import_custom_modules=False)
    if cfg.get('custom_imports'):import_modules_from_strings(**cfg.custom_imports)
    SimpleTokenizer()
    print('PASS CUDA/imports/tokenizer/FFmpeg; no inference or validation was run',flush=True)
    print('Run verify.cmd yourself before running videos.',flush=True)


if __name__=='__main__':main()
