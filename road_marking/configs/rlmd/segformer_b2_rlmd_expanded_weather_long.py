"""Long continuation from the expanded-data run's best saved validation step.

160000 NEW micro-iterations, 40000 optimizer updates with accumulation=4.
Keep original uniform-slide validation for comparability (374 clear images).
Gaussian/hybrid video inference remains separately available.
"""
_base_ = ['./segformer_b2_rlmd_expanded_weather.py']

# Restart optimizer/schedule while retaining all learned model weights.
# Half the previous phase's peak LR; inherited head multiplier remains 10.
optim_wrapper = dict(optimizer=dict(lr=5e-6))
max_iters = 160000
param_scheduler = [
    dict(type='LinearLR', start_factor=0.1, begin=0, end=1000,
         by_epoch=False),
    dict(type='PolyLR', eta_min=0.0, power=1.0,
         begin=1000, end=max_iters, by_epoch=False)]
train_cfg = dict(max_iters=max_iters, val_interval=8000)
default_hooks = dict(checkpoint=dict(
    interval=8000, save_best='mIoU', rule='greater', max_keep_ckpts=5))

# Immutable regular checkpoint, rather than a best_* filename that a still
# running earlier phase could remove. Validation mIoU at this step: 58.20.
load_from = 'work_dirs/segformer_b2_rlmd_expanded_weather/iter_64000.pth'
resume = False
work_dir = 'work_dirs/segformer_b2_rlmd_expanded_weather_long'
