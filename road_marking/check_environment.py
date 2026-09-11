from pathlib import Path
import hashlib
import sys
import torch
import mmcv
import mmengine
import mmseg
from mmcv.ops import nms
from mmengine.config import Config
from mmseg.apis import init_model

root = Path(__file__).resolve().parent
cfg = Config.fromfile(str(root / 'configs/rlmd/segformer_b2_rlmd_rcs_accum4.py'))
weight = root / 'work_dirs/segformer_b2_rlmd_rcs_accum4/best_mIoU_iter_232000.pth'
print('Python:', sys.version)
print('Torch:', torch.__version__, 'CUDA build:', torch.version.cuda)
print('MMCV:', mmcv.__version__, 'MMEngine:', mmengine.__version__, 'MMSeg:', mmseg.__version__)
print('MMSeg source:', mmseg.__file__)
print('CUDA available:', torch.cuda.is_available())
if torch.cuda.is_available():
    print('GPU:', torch.cuda.get_device_name(0))
expected = (root / 'checkpoint.sha256').read_text().split()[0]
h = hashlib.sha256()
with weight.open('rb') as f:
    for block in iter(lambda: f.read(1024 * 1024), b''):
        h.update(block)
assert h.hexdigest() == expected, 'Checkpoint hash mismatch; recopy the package.'
device = 'cuda:0' if torch.cuda.is_available() else 'cpu'
model = init_model(cfg, str(weight), device=device)
assert model.decode_head.num_classes == 25
print('PASS: imports, MMCV compiled ops import, config, checkpoint hash and model initialization.')
print('Next: run a short video with --max-frames 10 --no-preview to test actual inference.')
