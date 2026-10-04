"""Portable hybrid inference; no pretrained backbone download is needed."""
_base_ = ['./segformer_b2_rlmd_weather_hybrid.py']
model = dict(backbone=dict(init_cfg=None, with_cp=False))
load_from = None
resume = False
