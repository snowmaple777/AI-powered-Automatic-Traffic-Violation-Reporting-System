"""公開接口：原圖 BGR、原圖像素座標、秒；不依賴模型或違規引擎。"""
from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CompensationConfig:
    target_class: str = "stop line"
    roi_top: float = 0.40
    motion_width: int = 960
    min_motion_points: int = 20
    min_inlier_ratio: float = 0.60
    max_fb_error: float = 1.5  # 縮圖像素；其餘幾何門檻皆以原圖像素或比例表示。
    max_reprojection_error: float = 2.5
    max_photometric_error: float = 35.0
    min_observations: int = 3
    min_component_area: int = 100
    min_aspect: float = 2.0
    max_angle_degrees: float = 35.0
    match_distance_ratio: float = 0.025
    merge_gap_ratio: float = 0.04
    merge_y_ratio: float = 0.006
    smoothing_alpha: float = 0.65
    min_occlusion_ratio: float = 0.35
    max_occluded_seconds: float = 0.50
    max_missing_seconds: float = 0.10
    confidence_half_life: float = 0.35
    min_rule_support: float = 0.65
    max_uncertainty_ratio: float = 0.01
    max_frame_gap_seconds: float = 0.20

    def __post_init__(self):
        # 設定錯誤立即報錯，避免 NaN 或負期限悄悄使品質閘門失效。
        for key, value in asdict(self).items():
            if key == "target_class":
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("target_class must be a nonempty string")
            elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{key} must be finite and positive")
        for key in ("motion_width", "min_motion_points", "min_observations", "min_component_area"):
            if not isinstance(getattr(self, key), int):
                raise ValueError(f"{key} must be an integer")
        for key in ("roi_top", "min_inlier_ratio", "smoothing_alpha", "min_occlusion_ratio", "min_rule_support"):
            if not 0 < getattr(self, key) < 1:
                raise ValueError(f"{key} must lie strictly between 0 and 1")
        if self.min_motion_points < 4:
            raise ValueError("min_motion_points must be at least 4")

    @classmethod
    def from_json(cls, path):
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if data.pop("version", None) != 1:
            raise ValueError("Compensation config requires version=1")
        return cls(**data)


@dataclass
class MotionEstimate:
    # matrix 為前幀→當幀的 3x3 單應矩陣；只有 valid=True 才能使用。
    valid: bool
    matrix: Any = None
    reason: str = "unavailable"
    inliers: int = 0
    inlier_ratio: float = 0.0
    reprojection_error_px: float = 0.0
    photometric_error: float = 0.0

    def to_dict(self):
        return {"valid": self.valid, "reason": self.reason, "inliers": int(self.inliers),
                "inlier_ratio": float(self.inlier_ratio),
                "reprojection_error_px": float(self.reprojection_error_px),
                "photometric_error": float(self.photometric_error),
                "matrix": self.matrix.tolist() if self.valid and self.matrix is not None else None}


@dataclass
class CompensationResult:
    # 三份資料均獨立於呼叫端輸入；raw 永遠不被平滑、延伸或覆寫。
    raw_markings: list[dict]
    compensated_markings: list[dict]
    rule_markings: list[dict]
    diagnostics: dict = field(default_factory=dict)
