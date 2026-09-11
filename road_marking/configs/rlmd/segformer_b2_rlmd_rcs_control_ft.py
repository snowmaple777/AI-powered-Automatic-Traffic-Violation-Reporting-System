_base_ = ['./segformer_b2_rlmd_rcs_accum4.py']

# New fine-tuning run: load weights, reset optimizer/scheduler/iteration.
load_from = 'work_dirs/segformer_b2_rlmd_rcs_accum4/best_mIoU_iter_232000.pth'
resume = False
randomness = dict(seed=42, diff_rank_seed=False)
optim_wrapper = dict(optimizer=dict(lr=6e-6))
max_iters = 16000
train_cfg = dict(max_iters=max_iters, val_interval=2000)
param_scheduler = [
    dict(type='LinearLR', start_factor=0.1, by_epoch=False, begin=0, end=400),
    dict(type='PolyLR', eta_min=0.0, power=1.0, by_epoch=False, begin=400, end=max_iters),
]
default_hooks = dict(checkpoint=dict(interval=2000, max_keep_ckpts=8, save_best='mIoU'))
work_dir = 'work_dirs/rlmd_stopcrop_control'