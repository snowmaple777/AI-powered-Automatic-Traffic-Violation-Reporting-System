"""Fine-tune B2 on 2453 mixed clear/rain/night/custom training images.

The inherited 374-image validation split is unchanged. It does not establish
rain/night generalization; those external test sets are not configured yet.
"""
_base_ = ['./segformer_b2_rlmd_final_ft.py']

# New data adaptation: retain the established losses, RCS, AMP, no-flip
# pipeline, 512 crop and batch=1 / accumulation=4. Rebuild RCS on all images.
optim_wrapper = dict(optimizer=dict(lr=1e-5))
max_iters = 80000
param_scheduler = [
    dict(type='LinearLR', start_factor=0.1, begin=0, end=1000, by_epoch=False),
    dict(type='PolyLR', eta_min=0.0, power=1.0,
         begin=1000, end=max_iters, by_epoch=False)]
train_cfg = dict(max_iters=max_iters, val_interval=8000)
default_hooks = dict(checkpoint=dict(
    interval=8000, save_best='mIoU', rule='greater', max_keep_ckpts=10))

# Best overall validation mIoU from round 2: 60.41 at 24000 iterations.
# This loads weights only; optimizer and schedule start fresh.
load_from = 'work_dirs/segformer_b2_rlmd_final_ft_round2/iter_24000.pth'
resume = False
work_dir = 'work_dirs/segformer_b2_rlmd_expanded_weather'
