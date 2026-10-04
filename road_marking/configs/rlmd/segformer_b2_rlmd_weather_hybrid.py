"""Inference-only: Gaussian slide logits (75%) plus whole logits (25%)."""
_base_ = ['./segformer_b2_rlmd_weather_smooth_slide.py']
model = dict(test_cfg=dict(whole_weight=0.25))
