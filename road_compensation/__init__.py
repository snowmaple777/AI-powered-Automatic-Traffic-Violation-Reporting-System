"""Model-independent stop-line compensation between perception and rules."""
from .contracts import CompensationConfig, CompensationResult, MotionEstimate
from .compensator import RoadMarkingCompensator
from .adapter import attach_compensation

__all__ = ["CompensationConfig", "CompensationResult", "MotionEstimate",
           "RoadMarkingCompensator", "attach_compensation"]
