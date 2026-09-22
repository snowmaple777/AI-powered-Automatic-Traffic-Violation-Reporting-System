from .base import ModelResult, PerceptionModel
from .legacy import VehicleDetector, TrafficLightDetector, RoadMarkingSegmenter


class VehicleModel(PerceptionModel):
    def __init__(self, **params):
        self.backend = VehicleDetector(**params)

    def infer(self, frame, context, upstream):
        return ModelResult(self.backend.infer(frame))

    def reset(self):
        self.backend.frame_index = -1
        self.backend.track_first_seen.clear()
        predictor = self.backend.model.predictor
        for tracker in getattr(predictor, "trackers", []):
            tracker.reset()

    def close(self):
        self.backend = None


class LightModel(PerceptionModel):
    def __init__(self, **params):
        self.backend = TrafficLightDetector(**params)

    def infer(self, frame, context, upstream):
        return ModelResult(self.backend.infer(frame))

    def close(self):
        self.backend = None


class MarkingModel(PerceptionModel):
    def __init__(self, **params):
        self.backend = RoadMarkingSegmenter(**params)

    def infer(self, frame, context, upstream):
        mask, records = self.backend.infer(frame)
        return ModelResult(records, {"mask": mask, "palette": self.backend.palette})

    def close(self):
        self.backend = None
