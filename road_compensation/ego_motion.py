"""獨立的鏡頭運動階段：每幀估計一次前幀→當幀的路面單應矩陣，所有模組共用。

停止線補償、雙線、停等區／斑馬線與紅燈規則都使用同一份估計；
停用停止線補償時仍會執行，雙線與區域規則不再因此失效。
"""
import cv2

from .contracts import CompensationConfig, MotionEstimate
from .motion import RoadMotionEstimator, road_mask


class EgoMotionTracker:
    def __init__(self, config=None, estimator=None):
        self.config = config or CompensationConfig()
        self.estimator = estimator or RoadMotionEstimator(self.config)
        self.reset()

    def reset(self):
        self.previous_gray = self.previous_mask = None
        self.previous_index = self.previous_time = None

    def update(self, frame, *, frame_index, timestamp_sec, vehicles):
        """回傳 (MotionEstimate, reset_reason)。影格不連續或尺寸改變時重置，不跨段估計。"""
        cfg = self.config
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        mask = road_mask(frame.shape, vehicles, cfg.roi_top, cfg.roi_bottom)
        reset_reason = None
        if self.previous_gray is not None and (
                self.previous_gray.shape != gray.shape or frame_index != self.previous_index + 1
                or not 0 < timestamp_sec - self.previous_time <= cfg.max_frame_gap_seconds):
            self.reset()
            reset_reason = "discontinuous_input"
        motion = MotionEstimate(False, reason="first_frame")
        if self.previous_gray is not None:
            try:
                motion = self.estimator.estimate(self.previous_gray, gray, self.previous_mask, mask)
            except cv2.error:
                motion = MotionEstimate(False, reason="opencv_motion_error")
        self.previous_gray, self.previous_mask = gray, mask
        self.previous_index, self.previous_time = frame_index, timestamp_sec
        return motion, reset_reason
