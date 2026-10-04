"""Inference-only Gaussian blending; training recipe remains unchanged."""
_base_ = ['./segformer_b2_rlmd_expanded_weather.py']
model = dict(test_cfg=dict(
    mode='slide', crop_size=(512, 512), stride=(341, 341),
    blend='gaussian', blend_sigma=0.25))
