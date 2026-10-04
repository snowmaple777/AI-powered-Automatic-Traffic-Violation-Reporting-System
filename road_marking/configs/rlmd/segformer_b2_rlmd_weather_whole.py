"""Whole-image inference at the existing 1920x1080 resize scale."""
_base_ = ['./segformer_b2_rlmd_expanded_weather.py']
model = dict(test_cfg=dict(_delete_=True, mode='whole'))
