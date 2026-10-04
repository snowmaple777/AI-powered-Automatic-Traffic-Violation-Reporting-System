"""Verify local source, checkpoint integrity and model loading."""
import hashlib
from pathlib import Path
import torch
import mmseg
from mmcv.ops import nms
from mmseg.apis import init_model
from infer_video import DEFAULT_CHECKPOINT, get_config

root = Path(__file__).resolve().parent
assert Path(mmseg.__file__).resolve().is_relative_to(root / 'mmseg'), 'Wrong MMSeg installation; install this checkout editable.'
if not DEFAULT_CHECKPOINT.is_file():
    raise FileNotFoundError('Missing model. See models/README.md before installing/running.')
expected = (root / 'checkpoint.sha256').read_text().split()[0]
with DEFAULT_CHECKPOINT.open('rb') as f:
    digest = hashlib.sha256()
    for block in iter(lambda: f.read(1024*1024), b''):
        digest.update(block)
assert digest.hexdigest() == expected, 'Checkpoint SHA-256 mismatch'
model = init_model(get_config(), str(DEFAULT_CHECKPOINT), device='cpu')
assert model.decode_head.num_classes == 25
print('PASS: local imports, MMCV ops import, weight hash and model initialization.')
print('Torch:', torch.__version__, 'CUDA available:', torch.cuda.is_available())
