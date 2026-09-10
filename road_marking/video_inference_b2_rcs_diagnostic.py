import os
import argparse
from datetime import datetime
import sys
import time
import cv2
import numpy as np
import torch

from mmseg.apis import init_model, inference_model


# =========================================================
# 設定
# =========================================================

CONFIG_PATH = r"configs/rlmd/segformer_b2_rlmd_rcs_accum4.py"

CHECKPOINT_PATH = (
    r"work_dirs/segformer_b2_rlmd_rcs_accum4/"
    r"best_mIoU_iter_232000.pth"
)







BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, CONFIG_PATH)
CHECKPOINT_PATH = os.path.join(BASE_DIR, CHECKPOINT_PATH)

ALPHA = 0.45
SHOW_PREVIEW = True

# RLMD class ID
CROSSWALK_ID = 2
STOP_LINE_ID = 3

# ---------------------------------------------------------
# 後處理參數
# 先用這組測試，之後可以依影片調整
# ---------------------------------------------------------

# 單一 connected component 至少多大才保留
CROSSWALK_MIN_COMPONENT = 300
STOPLINE_MIN_COMPONENT = 150

# 所有有效 component 加起來至少多少 pixel 才算偵測到
CROSSWALK_MIN_TOTAL = 1000
STOPLINE_MIN_TOTAL = 500

# 只考慮畫面這個高度以下的標線
# 0.35 = 忽略最上方 35%
ROI_TOP_RATIO = 0.35

# Softmax confidence threshold
CROSSWALK_CONF_THRESHOLD = 0.70
STOPLINE_CONF_THRESHOLD = 0.60


# =========================================================
# RLMD Palette
# =========================================================

PALETTE_RGB = [
    [0, 0, 0],
    [255, 242, 0],
    [34, 117, 76],
    [255, 165, 0],
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


# =========================================================
# Connected Components
# =========================================================

def filter_components(binary_mask, min_area):
    """
    刪除太小的 connected components。

    input:
        binary_mask: 0 / 255

    output:
        filtered_mask: 0 / 255
        components: 保留下來的 component 資訊
    """

    num_labels, labels, stats, centroids = (
        cv2.connectedComponentsWithStats(
            binary_mask,
            connectivity=8
        )
    )

    filtered = np.zeros_like(binary_mask)

    components = []

    # label 0 是 background
    for label in range(1, num_labels):

        area = stats[
            label,
            cv2.CC_STAT_AREA
        ]

        if area < min_area:
            continue

        x = stats[
            label,
            cv2.CC_STAT_LEFT
        ]

        y = stats[
            label,
            cv2.CC_STAT_TOP
        ]

        w = stats[
            label,
            cv2.CC_STAT_WIDTH
        ]

        h = stats[
            label,
            cv2.CC_STAT_HEIGHT
        ]

        filtered[labels == label] = 255

        components.append({
            "area": int(area),
            "x": int(x),
            "y": int(y),
            "w": int(w),
            "h": int(h),
            "cx": float(centroids[label][0]),
            "cy": float(centroids[label][1])
        })

    return filtered, components


# =========================================================
# Crosswalk / Stop line 後處理
# =========================================================

def process_special_classes(
    pred_mask,
    crosswalk_conf,
    stopline_conf
):

    height, width = pred_mask.shape

    roi_top = int(height * ROI_TOP_RATIO)

    # -----------------------------------------
    # Crosswalk
    # -----------------------------------------

    crosswalk_raw = np.zeros(
        (height, width),
        dtype=np.uint8
    )

    crosswalk_valid = (
        (pred_mask == CROSSWALK_ID)
        &
        (crosswalk_conf >= CROSSWALK_CONF_THRESHOLD)
    )

    crosswalk_raw[crosswalk_valid] = 255

    # 忽略畫面太上方
    crosswalk_raw[:roi_top, :] = 0

    crosswalk_clean, cross_components = (
        filter_components(
            crosswalk_raw,
            CROSSWALK_MIN_COMPONENT
        )
    )

    crosswalk_pixels = np.count_nonzero(
        crosswalk_clean
    )

    crosswalk_detected = (
        crosswalk_pixels >= CROSSWALK_MIN_TOTAL
    )

    # -----------------------------------------
    # Stop line
    # -----------------------------------------

    stopline_raw = np.zeros(
        (height, width),
        dtype=np.uint8
    )

    stopline_valid = (
        (pred_mask == STOP_LINE_ID)
        &
        (stopline_conf >= STOPLINE_CONF_THRESHOLD)
    )

    stopline_raw[stopline_valid] = 255

    stopline_raw[:roi_top, :] = 0

    stopline_clean, stop_components = (
        filter_components(
            stopline_raw,
            STOPLINE_MIN_COMPONENT
        )
    )

    stopline_pixels = np.count_nonzero(
        stopline_clean
    )

    stopline_detected = (
        stopline_pixels >= STOPLINE_MIN_TOTAL
    )

    return {
        "crosswalk_mask": crosswalk_clean,
        "crosswalk_components": cross_components,
        "crosswalk_pixels": crosswalk_pixels,
        "crosswalk_detected": crosswalk_detected,

        "stopline_mask": stopline_clean,
        "stopline_components": stop_components,
        "stopline_pixels": stopline_pixels,
        "stopline_detected": stopline_detected
    }


# =========================================================
# 彩色 segmentation
# =========================================================

def create_color_mask(pred_mask):

    h, w = pred_mask.shape

    color_mask = np.zeros(
        (h, w, 3),
        dtype=np.uint8
    )

    for class_id, rgb in enumerate(
        PALETTE_RGB
    ):

        bgr = (
            rgb[2],
            rgb[1],
            rgb[0]
        )

        color_mask[
            pred_mask == class_id
        ] = bgr

    return color_mask


def create_overlay(frame, pred_mask):

    color_mask = create_color_mask(
        pred_mask
    )

    blended = cv2.addWeighted(
        frame,
        1.0 - ALPHA,
        color_mask,
        ALPHA,
        0
    )

    output = frame.copy()

    foreground = pred_mask != 0

    output[foreground] = (
        blended[foreground]
    )

    return output


# =========================================================
# 畫 Crosswalk / Stop line 結果
# =========================================================

def draw_detection(
    frame,
    processed
):

    # Crosswalk component bounding box
    for component in (
        processed[
            "crosswalk_components"
        ]
    ):

        x = component["x"]
        y = component["y"]
        w = component["w"]
        h = component["h"]

        cv2.rectangle(
            frame,
            (x, y),
            (x + w, y + h),
            (0, 255, 0),
            2
        )

    # Stop line component bounding box
    for component in (
        processed[
            "stopline_components"
        ]
    ):

        x = component["x"]
        y = component["y"]
        w = component["w"]
        h = component["h"]

        cv2.rectangle(
            frame,
            (x, y),
            (x + w, y + h),
            (0, 0, 255),
            2
        )

    return frame


# =========================================================
# 左上資訊
# =========================================================

def draw_info(
    frame,
    frame_index,
    total_frames,
    infer_fps,
    processed
):

    font = cv2.FONT_HERSHEY_SIMPLEX

    lines = [
        f"Frame: {frame_index}/{total_frames}",
        f"Inference FPS: {infer_fps:.2f}",
        (
            "Crosswalk: YES"
            if processed["crosswalk_detected"]
            else "Crosswalk: NO"
        ),
        (
            "Stop line: YES"
            if processed["stopline_detected"]
            else "Stop line: NO"
        ),
        (
            "CW pixels: "
            f'{processed["crosswalk_pixels"]}'
        ),
        (
            "SL pixels: "
            f'{processed["stopline_pixels"]}'
        )
    ]

    y = 35

    for text in lines:

        # 黑色外框
        cv2.putText(
            frame,
            text,
            (20, y),
            font,
            0.75,
            (0, 0, 0),
            4,
            cv2.LINE_AA
        )

        # 白字
        cv2.putText(
            frame,
            text,
            (20, y),
            font,
            0.75,
            (255, 255, 255),
            2,
            cv2.LINE_AA
        )

        y += 32

    return frame


# =========================================================
# Main
# =========================================================

def main():

    parser = argparse.ArgumentParser(description='RLMD road marking video inference')
    parser.add_argument('video', help='Input video path')
    parser.add_argument('--no-preview', action='store_true')
    parser.add_argument('--max-frames', type=int, default=0, help='0 = full video')
    args = parser.parse_args()
    if args.max_frames < 0:
        parser.error('--max-frames must be >= 0')
    input_video = os.path.abspath(args.video)

    if not os.path.exists(input_video):

        print(
            "[ERROR] 找不到影片:",
            input_video
        )

        sys.exit(1)

    if not os.path.exists(CONFIG_PATH):

        print(
            "[ERROR] 找不到 config:",
            CONFIG_PATH
        )

        sys.exit(1)

    if not os.path.exists(
        CHECKPOINT_PATH
    ):

        print(
            "[ERROR] 找不到 checkpoint:",
            CHECKPOINT_PATH
        )

        sys.exit(1)

    # -----------------------------------------
    # Device
    # -----------------------------------------

    device = (
        "cuda:0"
        if torch.cuda.is_available()
        else "cpu"
    )

    print()
    print("==============================")
    print("RLMD Video Inference B2 + RCS + Confidence")
    print("==============================")

    print("Device:", device)

    if torch.cuda.is_available():

        print(
            "GPU:",
            torch.cuda.get_device_name(0)
        )

    # -----------------------------------------
    # Model
    # -----------------------------------------

    print()
    print("[INFO] Loading model...")

    model = init_model(
        CONFIG_PATH,
        CHECKPOINT_PATH,
        device=device
    )

    model.eval()

    print("[INFO] Model loaded.")

    # -----------------------------------------
    # Video
    # -----------------------------------------

    cap = cv2.VideoCapture(
        input_video
    )

    if not cap.isOpened():

        print(
            "[ERROR] 無法開啟影片"
        )

        sys.exit(1)

    width = int(
        cap.get(
            cv2.CAP_PROP_FRAME_WIDTH
        )
    )

    height = int(
        cap.get(
            cv2.CAP_PROP_FRAME_HEIGHT
        )
    )

    video_fps = cap.get(
        cv2.CAP_PROP_FPS
    )

    total_frames = int(
        cap.get(
            cv2.CAP_PROP_FRAME_COUNT
        )
    )

    if video_fps <= 0:
        video_fps = 30

    # -----------------------------------------
    # Output
    # -----------------------------------------

    directory = os.path.join(BASE_DIR, 'outputs')
    os.makedirs(directory, exist_ok=True)

    filename = os.path.basename(
        input_video
    )

    name, _ = os.path.splitext(
        filename
    )

    output_video = os.path.join(
        directory,
        name + "_seg_orange_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".mp4"
    )

    fourcc = (
        cv2.VideoWriter_fourcc(
            *"mp4v"
        )
    )

    writer = cv2.VideoWriter(
        output_video,
        fourcc,
        video_fps,
        (width, height)
    )

    print()
    print(
        f"Resolution: {width}x{height}"
    )

    print(
        f"Frames: {total_frames}"
    )

    print(
        f"Output: {output_video}"
    )

    print()

    # -----------------------------------------
    # Inference loop
    # -----------------------------------------

    if not writer.isOpened():
        cap.release()
        raise RuntimeError('Cannot create output video: ' + output_video)

    frame_index = 0
    total_start = time.time()

    while True:

        if args.max_frames and frame_index >= args.max_frames:
            break

        ret, frame = cap.read()

        if not ret:
            break

        frame_index += 1

        start = time.time()

        with torch.no_grad():

            result = inference_model(
                model,
                frame
            )

        pred_mask = (
            result
            .pred_sem_seg
            .data
            .squeeze(0)
            .detach()
            .cpu()
            .numpy()
            .astype(np.uint8)
        )

        logits = result.seg_logits.data

        if logits.dim() == 4:
            logits = logits.squeeze(0)

        probs = torch.softmax(logits, dim=0)

        crosswalk_conf = (
            probs[CROSSWALK_ID]
            .detach()
            .cpu()
            .numpy()
        )

        stopline_conf = (
            probs[STOP_LINE_ID]
            .detach()
            .cpu()
            .numpy()
        )

        # -------------------------------------
        # 保證尺寸相同
        # -------------------------------------

        if pred_mask.shape != (
            height,
            width
        ):

            pred_mask = cv2.resize(
                pred_mask,
                (width, height),
                interpolation=cv2.INTER_NEAREST
            )

            crosswalk_conf = cv2.resize(
                crosswalk_conf,
                (width, height),
                interpolation=cv2.INTER_LINEAR
            )

            stopline_conf = cv2.resize(
                stopline_conf,
                (width, height),
                interpolation=cv2.INTER_LINEAR
            )

        # -------------------------------------
        # 後處理
        # -------------------------------------

        processed = (
            process_special_classes(
                pred_mask,
                crosswalk_conf,
                stopline_conf
            )
        )

        # -------------------------------------
        # Overlay
        # -------------------------------------

        output_frame = (
            create_overlay(
                frame,
                pred_mask
            )
        )

        # 不繪製 Crosswalk / Stop line 的 bounding boxes
        # 偵測與 connected-component 過濾仍然保留，只關閉方框顯示。

        elapsed = (
            time.time() - start
        )

        infer_fps = (
            1.0 / elapsed
            if elapsed > 0
            else 0
        )

        output_frame = draw_info(
            output_frame,
            frame_index,
            total_frames,
            infer_fps,
            processed
        )

        writer.write(
            output_frame
        )

        # -------------------------------------
        # Preview
        # -------------------------------------

        if SHOW_PREVIEW and not args.no_preview:

            preview = output_frame

            if width > 1280:

                preview_width = 1280

                scale = (
                    preview_width
                    / width
                )

                preview_height = int(
                    height * scale
                )

                preview = cv2.resize(
                    output_frame,
                    (
                        preview_width,
                        preview_height
                    )
                )

            cv2.imshow(
                "RLMD SegFormer B2 + RCS",
                preview
            )

            if (
                cv2.waitKey(1)
                & 0xFF
            ) == ord("q"):

                break

        # -------------------------------------
        # Console
        # -------------------------------------

        if frame_index % 30 == 0:

            progress = (
                frame_index
                / total_frames
                * 100
            )

            print(
                f"[{frame_index}/"
                f"{total_frames}] "
                f"{progress:.1f}% | "
                f"FPS {infer_fps:.2f} | "
                f"CW "
                f'{processed["crosswalk_pixels"]} | '
                f"SL "
                f'{processed["stopline_pixels"]}'
            )

    # -----------------------------------------
    # Finish
    # -----------------------------------------

    cap.release()
    writer.release()
    cv2.destroyAllWindows()

    total_time = (
        time.time()
        - total_start
    )

    print()
    print("==============================")
    print("Finished")
    print("==============================")

    print(
        f"Frames: {frame_index}"
    )

    print(
        f"Time: {total_time:.1f} sec"
    )

    if total_time > 0:

        print(
            f"Average FPS: "
            f"{frame_index / total_time:.2f}"
        )

    print(
        f"Output: {output_video}"
    )


if __name__ == "__main__":
    main()