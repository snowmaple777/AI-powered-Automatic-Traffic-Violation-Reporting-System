"""Bounded final fine-tune from the verified 59.44 mIoU B2 checkpoint."""
_base_ = ['./segformer_b2_rlmd_rcs_accum4.py']

train_pipeline = [
    dict(type='LoadImageFromFile'),
    dict(type='LoadAnnotations', reduce_zero_label=False),
    dict(type='RandomResize', scale=(1920, 1080),
         ratio_range=(0.5, 2.0), keep_ratio=True),
    dict(type='RandomCrop', crop_size=(512, 512), cat_max_ratio=0.75),
    # No mirrored directional arrows or text with unchanged semantic labels.
    dict(type='PhotoMetricDistortion'),
    dict(type='PackSegInputs')]
train_dataloader = dict(dataset=dict(pipeline=train_pipeline))

# Retain the proven B2 AMP/AdamW, CE+Dice, class weights and RCS recipe.
optim_wrapper = dict(optimizer=dict(lr=1e-5))
max_iters = 40000
param_scheduler = [
    dict(type='LinearLR', start_factor=0.1, begin=0, end=500, by_epoch=False),
    dict(type='PolyLR', eta_min=0.0, power=1.0,
         begin=500, end=max_iters, by_epoch=False)]
train_cfg = dict(max_iters=max_iters, val_interval=8000)
default_hooks = dict(checkpoint=dict(
    interval=8000, save_best='mIoU', rule='greater', max_keep_ckpts=5))
randomness = dict(seed=42)
load_from = 'work_dirs/segformer_b2_rlmd_rcs_accum4_stage2_480k/best_mIoU_iter_152000.pth'
resume = False
work_dir = 'work_dirs/segformer_b2_rlmd_final_ft'
