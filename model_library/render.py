"""Rendering is independent of the selected segmentation backend."""
import cv2
import numpy as np


def overlay_markings(frame, result, alpha=.35):
    mask = result.artifacts.get("mask")
    palette = result.artifacts.get("palette")
    if mask is None or palette is None:
        return frame.copy()
    # 以查表一次上色（原本每個類別各做一次全畫面比對）；超出調色盤的類別維持黑色。
    lut = np.zeros((256, 3), dtype=frame.dtype)
    for class_id, rgb in enumerate(palette[:256]):
        lut[class_id] = rgb[::-1]
    colors = lut[mask]
    output = frame.copy()
    blended = cv2.addWeighted(frame, 1-alpha, colors, alpha, 0)
    marked = mask != 0
    output[marked] = blended[marked]
    return output
