"""影像平面的幾何工具。"""
import numpy as np


def project_points(points, matrix):
    """以 3x3 單應矩陣投影點列；投影不穩定（非有限值或位於地平線後方）時拋出 ValueError。"""
    homogeneous = np.column_stack((points, np.ones(len(points)))) @ matrix.T
    if not np.isfinite(homogeneous).all() or np.any(homogeneous[:, 2] <= 1e-6):
        raise ValueError("Unstable ground projection")
    return (homogeneous[:, :2] / homogeneous[:, 2:]).tolist()
