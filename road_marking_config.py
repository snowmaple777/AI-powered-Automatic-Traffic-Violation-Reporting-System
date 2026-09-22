default_scope = "mmseg"

classes = (
    "background", "box junction", "crosswalk", "stop line",
    "solid single white", "solid single yellow", "solid single red",
    "solid double white", "solid double yellow", "dashed single white",
    "dashed single yellow", "left arrow", "straight arrow", "right arrow",
    "left straight arrow", "right straight arrow", "channelizing line",
    "motor prohibited", "slow", "motor priority lane", "motor waiting zone",
    "left turn box", "motor icon", "bike icon", "parking lot",
)

palette = [
    [0, 0, 0], [255, 242, 0], [34, 117, 76], [61, 72, 204],
    [237, 28, 36], [163, 73, 164], [185, 122, 87], [136, 0, 21],
    [112, 146, 190], [181, 230, 29], [153, 217, 234], [158, 159, 76],
    [121, 138, 134], [41, 64, 96], [7, 102, 146], [247, 153, 255],
    [255, 204, 153], [155, 255, 153], [255, 153, 173], [230, 224, 147],
    [35, 27, 87], [193, 158, 155], [109, 29, 78], [3, 164, 204],
    [175, 157, 185],
]

data_preprocessor = dict(
    type="SegDataPreProcessor", mean=[123.675, 116.28, 103.53],
    std=[58.395, 57.12, 57.375], bgr_to_rgb=True, pad_val=0,
    seg_pad_val=255, size=(512, 512))

model = dict(
    type="EncoderDecoder", data_preprocessor=data_preprocessor,
    backbone=dict(
        type="MixVisionTransformer", in_channels=3, embed_dims=64,
        num_stages=4, num_layers=[3, 4, 6, 3], num_heads=[1, 2, 5, 8],
        patch_sizes=[7, 3, 3, 3], sr_ratios=[8, 4, 2, 1],
        out_indices=(0, 1, 2, 3), mlp_ratio=4, qkv_bias=True,
        drop_rate=0.0, attn_drop_rate=0.0, drop_path_rate=0.1,
        init_cfg=None),
    decode_head=dict(
        type="SegformerHead", in_channels=[64, 128, 320, 512],
        in_index=[0, 1, 2, 3], channels=256, dropout_ratio=0.1,
        num_classes=25, norm_cfg=dict(type="BN", requires_grad=True),
        align_corners=False,
        loss_decode=dict(type="CrossEntropyLoss", use_sigmoid=False,
                         loss_weight=1.0)),
    train_cfg=dict(), test_cfg=dict(mode="whole"))

test_pipeline = [
    dict(type="LoadImageFromNDArray"),
    dict(type="Resize", scale=(1920, 1080), keep_ratio=True),
    dict(type="PackSegInputs"),
]

