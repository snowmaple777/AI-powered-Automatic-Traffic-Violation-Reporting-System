import math
import time

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
    由 FrameProcessor 依鏡頭運動把遮罩推移到當幀（標線固定在路面上）。

    refresh_on_invalid_motion（預設關閉）：原本要沿用快取的幀，若本幀鏡頭運動無效或無法取得，改為重新推論，
    避免下游拿到未對齊的遮罩。需要 FrameContext.motion_probe；判斷時會等待背景鏡頭運動估計完成。"""

    def __init__(self, interval=1, activity_boost=False, activity_hold_seconds=1.0,
                 refresh_on_invalid_motion=False, **params):
        if not isinstance(interval, int) or interval < 1:
            raise ValueError("road_markings interval must be a positive integer")
        if not isinstance(activity_boost, bool):
            raise ValueError("activity_boost must be boolean")
        if not isinstance(refresh_on_invalid_motion, bool):
            raise ValueError("refresh_on_invalid_motion must be boolean")
        if (isinstance(activity_hold_seconds, bool)
                or not isinstance(activity_hold_seconds, (int, float))
                or not math.isfinite(activity_hold_seconds) or activity_hold_seconds < 0):
            raise ValueError("activity_hold_seconds must be finite and nonnegative")
        self.interval = interval
        self.activity_boost = activity_boost
        self.activity_hold_seconds = activity_hold_seconds
        self.refresh_on_invalid_motion = refresh_on_invalid_motion
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

    @property
    def schedule_enabled(self):
        """有任何排程選項啟用時，run_pipeline 才在 *_timings.json 輸出 road_marking_schedule。"""
        return self.activity_boost or self.refresh_on_invalid_motion

    def _motion_reason(self, context):
        """沿用快取前確認本幀鏡頭運動；無效或無法取得時要求重新推論。等待時間計入診斷。"""
        probe = getattr(context, "motion_probe", None)
        if probe is None:
            return "motion_unknown"
        started = time.perf_counter()
        motion = probe()
        self.schedule_diagnostics["motion_wait_seconds"] += time.perf_counter() - started
        if motion is None:
            return "motion_unknown"
        return None if motion.valid else "motion_invalid"

    def infer(self, frame, context, upstream):
        reason = (self._activity_reason(frame, context, upstream)
                  if self.activity_boost else None)
        baseline_due = self.cached is None or context.index % self.interval == 0
        if not baseline_due and reason is None and self.refresh_on_invalid_motion:
            reason = self._motion_reason(context)
        inferred = baseline_due or reason is not None
        if inferred:
            mask, records = self.backend.infer(frame)
            self.cached = (context.index, mask, records)
        if self.schedule_enabled:
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
        modes = [name for name, enabled in (("activity_boost", self.activity_boost),
                                            ("refresh_on_invalid_motion", self.refresh_on_invalid_motion)) if enabled]
        self.schedule_diagnostics = {
            "mode": "+".join(modes) or "fixed_interval", "baseline_interval": self.interval,
            "hold_seconds": self.activity_hold_seconds,
            "frames": 0, "inferences": 0, "extra_inferences": 0, "reasons": {},
            "motion_wait_seconds": 0.0}

    def close(self):
        self.backend = None
