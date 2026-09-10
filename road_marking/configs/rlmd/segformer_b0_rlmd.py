_base_ = [
    '../segformer/segformer_mit-b0_8xb2-160k_ade20k-512x512.py'
]

# ===== Dataset =====

dataset_type = 'BaseSegDataset'
data_root = 'data/rlmd'

load_from = (
    'work_dirs/segformer_b0_rlmd/'
    'best_mIoU_iter_20000.pth'
)

work_dir = 'work_dirs/segformer_b0_rlmd_ce_dice'

classes = (
    'background',
    'box junction',
    'crosswalk',
    'stop line',
    'solid single white',
    'solid single yellow',
    'solid single red',
    'solid double white',
    'solid double yellow',
    'dashed single white',
    'dashed single yellow',
    'left arrow',
    'straight arrow',
    'right arrow',
    'left straight arrow',
    'right straight arrow',
    'channelizing line',
    'motor prohibited',
    'slow',
    'motor priority lane',
    'motor waiting zone',
    'left turn box',
    'motor icon',
    'bike icon',
    'parking lot',
)

palette = [
    [0, 0, 0],
    [255, 242, 0],
    [34, 117, 76],
    [61, 72, 204],
    [237, 28, 36],
    [163, 73, 164],
    [185, 122, 87],
    [136, 0, 21],
    [112, 146, 190],
    [181, 230, 29],
    [153, 217, 234],
    [158, 159, 76],
    [121, 138, 134],
    [41, 64, 96],
    [7, 102, 146],
    [247, 153, 255],
    [255, 204, 153],
    [155, 255, 153],
    [255, 153, 173],
    [230, 224, 147],
    [35, 27, 87],
    [193, 158, 155],
    [109, 29, 78],
    [3, 164, 204],
    [175, 157, 185],
]

metainfo = dict(
    classes=classes,
    palette=palette
)

crop_size = (512, 512)

train_pipeline = [
    dict(type='LoadImageFromFile'),
    dict(type='LoadAnnotations'),
    dict(
        type='RandomResize',
        scale=(1920, 1080),
        ratio_range=(0.5, 2.0),
        keep_ratio=True),
    dict(type='RandomCrop', crop_size=crop_size, cat_max_ratio=0.75),
    dict(type='RandomFlip', prob=0.5),
    dict(type='PackSegInputs')
]

test_pipeline = [
    dict(type='LoadImageFromFile'),
    dict(type='Resize', scale=(1920, 1080), keep_ratio=True),
    dict(type='LoadAnnotations'),
    dict(type='PackSegInputs')
]

train_dataloader = dict(
    batch_size=1,
    num_workers=2,
    dataset=dict(
        type=dataset_type,
        data_root=data_root,
        data_prefix=dict(
            img_path='images/train',
            seg_map_path='annotations/train'
        ),
        img_suffix='.jpg',
        seg_map_suffix='.png',
        metainfo=metainfo,
        pipeline=train_pipeline
    )
)

val_dataloader = dict(
    batch_size=1,
    num_workers=2,
    dataset=dict(
        type=dataset_type,
        data_root=data_root,
        data_prefix=dict(
            img_path='images/val',
            seg_map_path='annotations/val'
        ),
        img_suffix='.jpg',
        seg_map_suffix='.png',
        metainfo=metainfo,
        pipeline=test_pipeline
    )
)

test_dataloader = val_dataloader

# ===== Model =====

class_weight = [
    1.0,   # 0 background
    5.0,   # 1 box junction
    5.0,   # 2 crosswalk
    10.0,  # 3 stop line
    5.0,   # 4 solid single white
    5.0,   # 5 solid single yellow
    10.0,  # 6 solid single red
    10.0,  # 7 solid double white
    10.0,  # 8 solid double yellow
    5.0,   # 9 dashed single white
    20.0,  # 10 dashed single yellow
    10.0,  # 11 left arrow
    10.0,  # 12 straight arrow
    20.0,  # 13 right arrow
    10.0,  # 14 left straight arrow
    10.0,  # 15 right straight arrow
    10.0,  # 16 channelizing line
    10.0,  # 17 motor prohibited
    20.0,  # 18 slow
    10.0,  # 19 motor priority lane
    5.0,   # 20 motor waiting zone
    10.0,  # 21 left turn box
    20.0,  # 22 motor icon
    20.0,  # 23 bike icon
    10.0,  # 24 parking lot
]

model = dict(
    decode_head=dict(
        num_classes=25,

        loss_decode=[
            dict(
                type='CrossEntropyLoss',
                use_sigmoid=False,
                loss_weight=1.0,
                class_weight=class_weight
            ),

            dict(
                type='DiceLoss',
                loss_weight=1.0,
                ignore_index=255
            )
        ]
    )
)











# ===== Train =====

train_cfg = dict(
    type='IterBasedTrainLoop',
    max_iters=40000,
    val_interval=2000
)

default_hooks = dict(
    checkpoint=dict(
        type='CheckpointHook',
        by_epoch=False,
        interval=2000,
        save_best='mIoU'
    )
)