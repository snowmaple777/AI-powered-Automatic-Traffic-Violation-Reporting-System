"""相容舊匯入路徑；實作已移至 model_library.detectors。"""
from .detectors import (ROOT, THIRD_PARTY, ROAD_MODEL_SHA256, VEHICLE_CLASSES,  # noqa: F401
                        VehicleDetector, TrafficLightDetector, RoadMarkingSegmenter,
                        mask_components, draw_boxes)
