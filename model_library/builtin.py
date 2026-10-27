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

    def __init__(self, interval=1, **params):
        if not isinstance(interval, int) or interval < 1:
            raise ValueError("road_markings interval must be a positive integer")
        self.interval = interval
        self.backend = RoadMarkingSegmenter(**params)
        self.cached = None

    def infer(self, frame, context, upstream):
        if self.cached is None or context.index % self.interval == 0:
            mask, records = self.backend.infer(frame)
            self.cached = (context.index, mask, records)
        source, mask, records = self.cached
        return ModelResult(records, {"mask": mask, "palette": self.backend.palette, "source_frame": source,
                                     "classes": self.backend.classes, "min_pixels": self.backend.min_pixels})

    def reset(self):
        self.cached = None

    def close(self):
        self.backend = None
