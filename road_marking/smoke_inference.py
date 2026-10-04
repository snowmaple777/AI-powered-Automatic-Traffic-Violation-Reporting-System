"""Real hybrid execution check on synthetic input, not an accuracy evaluation."""
import numpy as np
import torch
from mmseg.apis import init_model, inference_model
from infer_video import get_config, DEFAULT_CHECKPOINT

device = 'cuda:0' if torch.cuda.is_available() else 'cpu'
model = init_model(get_config(), str(DEFAULT_CHECKPOINT), device=device)
with torch.inference_mode():
    result = inference_model(model, np.zeros((360, 640, 3), dtype=np.uint8))
assert result.pred_sem_seg.data.shape[-2:] == (360, 640)
assert torch.isfinite(result.seg_logits.data).all()
print('PASS: hybrid inference on', device)
