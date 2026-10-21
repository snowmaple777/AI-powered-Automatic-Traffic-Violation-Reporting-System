"""鏡頭運動（前幀→當幀的路面單應矩陣）的讀取與驗證。"""
import math

import numpy as np


def road_motion(record):
    """從觀測紀錄取出鏡頭運動。

    新紀錄由獨立的鏡頭運動階段寫入 postprocessing.ego_motion；
    舊 JSONL 只有停止線補償層內的 motion，仍可重算。
    """
    post = record.get("postprocessing", {})
    motion = post.get("ego_motion")
    if motion is None:
        motion = post.get("road_marking_compensation", {}).get("motion", {})
    return motion or {}


def homography(motion, max_error_px=None):
    """有效時回傳 3x3 矩陣，否則 None。

    條件：valid、3x3、全為有限值、|det| >= 1e-9；有 max_error_px 時另要求
    0 <= reprojection_error_px <= max_error_px。
    """
    if not motion or not motion.get("valid"):
        return None
    try:
        matrix = np.asarray(motion.get("matrix"), dtype=float)
    except (TypeError, ValueError):
        return None
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all() or abs(np.linalg.det(matrix)) < 1e-9:
        return None
    if max_error_px is not None:
        try:
            error = float(motion.get("reprojection_error_px", 0))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(error) or not 0 <= error <= max_error_px:
            return None
    return matrix
