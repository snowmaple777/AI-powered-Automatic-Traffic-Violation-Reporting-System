"""RLMD palette, raw overlay, and filtered CW/SL counters."""
import cv2
import numpy as np

ALPHA = 0.45

CROSSWALK_ID = 2

STOP_LINE_ID = 3

CROSSWALK_MIN_COMPONENT = 300

STOPLINE_MIN_COMPONENT = 150

CROSSWALK_MIN_TOTAL = 1000

STOPLINE_MIN_TOTAL = 500

ROI_TOP_RATIO = 0.35

CROSSWALK_CONF_THRESHOLD = 0.7

STOPLINE_CONF_THRESHOLD = 0.6

PALETTE_RGB = [[0, 0, 0], [255, 242, 0], [34, 117, 76], [61, 72, 204], [237, 28, 36], [163, 73, 164], [185, 122, 87], [136, 0, 21], [112, 146, 190], [181, 230, 29], [153, 217, 234], [158, 159, 76], [121, 138, 134], [41, 64, 96], [7, 102, 146], [247, 153, 255], [255, 204, 153], [155, 255, 153], [255, 153, 173], [230, 224, 147], [35, 27, 87], [193, 158, 155], [109, 29, 78], [3, 164, 204], [175, 157, 185]]

def filter_components(binary_mask, min_area):
    """
    刪除太小的 connected components。

    input:
        binary_mask: 0 / 255

    output:
        filtered_mask: 0 / 255
        components: 保留下來的 component 資訊
    """
    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(binary_mask, connectivity=8)
    filtered = np.zeros_like(binary_mask)
    components = []
    for label in range(1, num_labels):
        area = stats[label, cv2.CC_STAT_AREA]
        if area < min_area:
            continue
        x = stats[label, cv2.CC_STAT_LEFT]
        y = stats[label, cv2.CC_STAT_TOP]
        w = stats[label, cv2.CC_STAT_WIDTH]
        h = stats[label, cv2.CC_STAT_HEIGHT]
        filtered[labels == label] = 255
        components.append({'area': int(area), 'x': int(x), 'y': int(y), 'w': int(w), 'h': int(h), 'cx': float(centroids[label][0]), 'cy': float(centroids[label][1])})
    return (filtered, components)

def process_special_classes(pred_mask, crosswalk_conf, stopline_conf):
    height, width = pred_mask.shape
    roi_top = int(height * ROI_TOP_RATIO)
    crosswalk_raw = np.zeros((height, width), dtype=np.uint8)
    crosswalk_valid = (pred_mask == CROSSWALK_ID) & (crosswalk_conf >= CROSSWALK_CONF_THRESHOLD)
    crosswalk_raw[crosswalk_valid] = 255
    crosswalk_raw[:roi_top, :] = 0
    crosswalk_clean, cross_components = filter_components(crosswalk_raw, CROSSWALK_MIN_COMPONENT)
    crosswalk_pixels = np.count_nonzero(crosswalk_clean)
    crosswalk_detected = crosswalk_pixels >= CROSSWALK_MIN_TOTAL
    stopline_raw = np.zeros((height, width), dtype=np.uint8)
    stopline_valid = (pred_mask == STOP_LINE_ID) & (stopline_conf >= STOPLINE_CONF_THRESHOLD)
    stopline_raw[stopline_valid] = 255
    stopline_raw[:roi_top, :] = 0
    stopline_clean, stop_components = filter_components(stopline_raw, STOPLINE_MIN_COMPONENT)
    stopline_pixels = np.count_nonzero(stopline_clean)
    stopline_detected = stopline_pixels >= STOPLINE_MIN_TOTAL
    return {'crosswalk_mask': crosswalk_clean, 'crosswalk_components': cross_components, 'crosswalk_pixels': crosswalk_pixels, 'crosswalk_detected': crosswalk_detected, 'stopline_mask': stopline_clean, 'stopline_components': stop_components, 'stopline_pixels': stopline_pixels, 'stopline_detected': stopline_detected}

def create_color_mask(pred_mask):
    h, w = pred_mask.shape
    color_mask = np.zeros((h, w, 3), dtype=np.uint8)
    for class_id, rgb in enumerate(PALETTE_RGB):
        bgr = (rgb[2], rgb[1], rgb[0])
        color_mask[pred_mask == class_id] = bgr
    return color_mask

def create_overlay(frame, pred_mask):
    color_mask = create_color_mask(pred_mask)
    blended = cv2.addWeighted(frame, 1.0 - ALPHA, color_mask, ALPHA, 0)
    output = frame.copy()
    foreground = pred_mask != 0
    output[foreground] = blended[foreground]
    return output

def draw_info(frame, frame_index, total_frames, infer_fps, processed):
    font = cv2.FONT_HERSHEY_SIMPLEX
    lines = [f'Frame: {frame_index}/{total_frames}', f'Inference FPS: {infer_fps:.2f}', 'Crosswalk: YES' if processed['crosswalk_detected'] else 'Crosswalk: NO', 'Stop line: YES' if processed['stopline_detected'] else 'Stop line: NO', f"CW pixels: {processed['crosswalk_pixels']}", f"SL pixels: {processed['stopline_pixels']}"]
    y = 35
    for text in lines:
        cv2.putText(frame, text, (20, y), font, 0.75, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(frame, text, (20, y), font, 0.75, (255, 255, 255), 2, cv2.LINE_AA)
        y += 32
    return frame
