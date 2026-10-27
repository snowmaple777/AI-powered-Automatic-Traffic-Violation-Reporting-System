"""Larger-context B2 training on 4GB: 768 crops + activation checkpointing.

Keep validation at the original 512/341 slide settings for comparability.
"""
_base_ = ['./segformer_b2_rlmd_expanded_weather_relearn.py']

crop_size = (768, 768)
model = dict(
    backbone=dict(with_cp=True),
    data_preprocessor=dict(size=crop_size))

# AMP produced non-finite losses on the 768 real-image execution check.
# Keep larger spatial context and use FP32 with activation checkpointing.
optim_wrapper = dict(
    _delete_=True,
    type='OptimWrapper', accumulative_counts=4,
    optimizer=dict(type='AdamW', lr=3e-5, betas=(0.9, 0.999),
                   weight_decay=0.01),
    paramwise_cfg=dict(custom_keys={
        'pos_block': dict(decay_mult=0.0),
        'norm': dict(decay_mult=0.0),
        'head': dict(lr_mult=3.0)}))

train_pipeline = [
    dict(type='LoadImageFromFile'),
    dict(type='LoadAnnotations', reduce_zero_label=False),
    dict(type='RandomResize', scale=(1920, 1080),
         ratio_range=(0.5, 2.0), keep_ratio=True),
    dict(type='RandomCrop', crop_size=crop_size, cat_max_ratio=0.75),
    dict(type='PhotoMetricDistortion'),
    dict(type='PackSegInputs')]
train_dataloader = dict(dataset=dict(pipeline=train_pipeline))

work_dir = 'work_dirs/segformer_b2_rlmd_expanded_weather_relearn_768'
