"""One synthetic-image forward pass; tests runtime, not recognition quality."""
from pathlib import Path
import numpy as np
import torch
from mmseg.apis import init_model, inference_model

root = Path(__file__).resolve().parent
device = 'cuda:0' if torch.cuda.is_available() else 'cpu'
print('Testing actual inference on:', device, flush=True)
model = init_model(str(root / 'configs/rlmd/segformer_b2_rlmd_rcs_accum4.py'),
                   str(root / 'work_dirs/segformer_b2_rlmd_rcs_accum4/best_mIoU_iter_232000.pth'),
                   device=device)
with torch.no_grad():
    result = inference_model(model, np.zeros((360, 640, 3), dtype=np.uint8))
assert tuple(result.pred_sem_seg.data.shape[-2:]) == (360, 640)
print('PASS: synthetic-image forward pass. This does not test recognition accuracy.')
