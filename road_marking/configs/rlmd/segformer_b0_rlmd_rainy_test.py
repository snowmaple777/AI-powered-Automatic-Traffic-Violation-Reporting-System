_base_ = [
    './segformer_b0_rlmd.py'
]

val_dataloader = dict(
    dataset=dict(
        data_root='data/rlmd_rainy',
        data_prefix=dict(
            img_path='images/val',
            seg_map_path='annotations/val'
        )
    )
)

test_dataloader = val_dataloader