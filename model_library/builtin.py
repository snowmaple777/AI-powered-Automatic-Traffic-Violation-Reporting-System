import math

from .base import ModelResult, PerceptionModel
from .detectors import TrafficLightDetector, RoadMarkingSegmenter


class LightModel(PerceptionModel):
    def __init__(self, **params):
        self.backend = TrafficLightDetector(**params)

    def infer(self, frame, context, upstream):
        return ModelResult(self.backend.infer(frame))

    def close(self):
        self.backend = None


class MarkingModel(PerceptionModel):
    """interval > 1：只在 index % interval == 0 的幀推論，其餘幀回傳上次的遮罩並標記 source_frame，
    由 FrameProcessor 依鏡頭運動把遮罩推移到當幀（標線固定在路面上）。"""

    def __init__(self, interval=1, activity_boost=False, activity_hold_seconds=1.0, **params):
        if not isinstance(interval, int) or interval < 1:
            raise ValueError("road_markings interval must be a positive integer")
        if not isinstance(activity_boost, bool):
            raise ValueError("activity_boost must be boolean")
        if (isinstance(activity_hold_seconds, bool)
                or not isinstance(activity_hold_seconds, (int, float))
                or not math.isfinite(activity_hold_seconds) or activity_hold_seconds < 0):
            raise ValueError("activity_hold_seconds must be finite and nonnegative")
        self.interval = interval
        self.activity_boost = activity_boost
        self.activity_hold_seconds = activity_hold_seconds
        self.backend = RoadMarkingSegmenter(**params)
        self.reset()

    def _activity_reason(self, frame, context, upstream):
        """保守升頻：任何車輛／燈號皆視為活動，不宣稱空偵測代表安全。

        沿用原本的固定推論幀，僅補做額外推論；目前不處理畫面切換或運動品質。
        """
        now = context.timestamp_sec
        if not math.isfinite(now) or now < 0:
            raise ValueError("activity scheduling requires a finite nonnegative timestamp")
        shape = getattr(frame, "shape", None)
        previous = self._schedule_previous
        discontinuous = previous is None or (
            context.index != previous[0] + 1 or not 0 < now - previous[1] <= .2
            or shape != previous[2])
        self._schedule_previous = (context.index, now, shape)
        if discontinuous:
            self.cached = None
            self._activity_until = now + self.activity_hold_seconds
            return "startup_or_discontinuity"
        # 缺少上游輸出時保守升頻；快取框同樣只當排程提示，不是新證據。
        if any(name not in upstream for name in ("vehicles", "traffic_lights")):
            reason = "missing_upstream"
        elif upstream["vehicles"].records:
            reason = "vehicles_present"
        elif upstream["traffic_lights"].records:
            reason = "lights_present"
        else:
            return "activity_hold" if now < self._activity_until else None
        self._activity_until = now + self.activity_hold_seconds
        return reason

    def infer(self, frame, context, upstream):
        reason = (self._activity_reason(frame, context, upstream)
                  if self.activity_boost else None)
        baseline_due = self.cached is None or context.index % self.interval == 0
        inferred = baseline_due or reason is not None
        if inferred:
            mask, records = self.backend.infer(frame)
            self.cached = (context.index, mask, records)
        if self.activity_boost:
            stats = self.schedule_diagnostics
            stats["frames"] += 1
            stats["inferences"] += int(inferred)
            stats["extra_inferences"] += int(inferred and not baseline_due)
            why = reason or ("baseline_due" if baseline_due else "baseline_cache")
            stats["reasons"][why] = stats["reasons"].get(why, 0) + 1
        source, mask, records = self.cached
        return ModelResult(records, {"mask": mask, "palette": self.backend.palette, "source_frame": source,
                                     "classes": self.backend.classes, "min_pixels": self.backend.min_pixels})

    def reset(self):
        self.cached = None
        self._schedule_previous = None
        self._activity_until = 0.0
        self.schedule_diagnostics = {
            "mode": "activity_boost", "baseline_interval": self.interval,
            "hold_seconds": self.activity_hold_seconds,
            "frames": 0, "inferences": 0, "extra_inferences": 0, "reasons": {}}

    def close(self):
        self.backend = None
