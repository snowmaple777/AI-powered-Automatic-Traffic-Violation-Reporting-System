"""Stronger adaptation of the backbone to the expanded training distribution.

Use the same starting weights and validation protocol as the low-LR run.
This is a new optimizer/schedule phase, not a resume of that run.
"""
_base_ = ['./segformer_b2_rlmd_expanded_weather_long.py']

# Backbone peak is 6x the low-LR phase. The already-trained segmentation
# head uses 3x rather than 10x, giving a 9e-5 peak (previously 5e-5).
optim_wrapper = dict(
    optimizer=dict(lr=3e-5),
    paramwise_cfg=dict(custom_keys={'head': dict(lr_mult=3.0)}))

max_iters = 160000
param_scheduler = [
    dict(type='LinearLR', start_factor=0.1, begin=0, end=2000,
         by_epoch=False),
    dict(type='PolyLR', eta_min=0.0, power=1.0,
         begin=2000, end=max_iters, by_epoch=False)]

load_from = 'work_dirs/segformer_b2_rlmd_expanded_weather/iter_64000.pth'
resume = False
work_dir = 'work_dirs/segformer_b2_rlmd_expanded_weather_relearn'
