"""Rendering is independent of the selected segmentation backend."""
import cv2
import numpy as np


def overlay_markings(frame, result, alpha=.35):
    mask = result.artifacts.get("mask")
    palette = result.artifacts.get("palette")
    if mask is None or palette is None:
        return frame.copy()
    colors = np.zeros_like(frame)
    for class_id, rgb in enumerate(palette):
        colors[mask == class_id] = rgb[::-1]
    output = frame.copy()
    blended = cv2.addWeighted(frame, 1-alpha, colors, alpha, 0)
    output[mask != 0] = blended[mask != 0]
    return output
