_base_ = ['./segformer_b2_rlmd_rcs_control_ft.py']
custom_imports = dict(imports=['mmseg.datasets.rlmd_stopcrop_dataset'], allow_failed_imports=False)
train_dataloader = dict(dataset=dict(type='RLMDStopCropDataset', stop_crop_min_pixels=128))
work_dir = 'work_dirs/rlmd_stopcrop_trial'