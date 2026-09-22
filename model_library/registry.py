"""Lazy factories, validated config, and config-relative model paths."""
import importlib
import json
from pathlib import Path

BACKENDS = {
    "yolo_vehicle": "model_library.builtin:VehicleModel",
    "yolo_light": "model_library.builtin:LightModel",
    "mmseg_marking": "model_library.builtin:MarkingModel",
    "onnx_depth": "model_library.depth:DepthModel",
    "yolo_plate": "model_library.plates:PlateModel",
    "rapidocr": "model_library.ocr:OCRModel",
}
STAGES = ("vehicles", "traffic_lights", "road_markings", "depth", "plates", "ocr")


def register_backend(name, factory):
    if name in BACKENDS:
        raise ValueError(f"Backend already registered: {name}")
    BACKENDS[name] = factory


def load_config(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if config.get("version") != 1 or not isinstance(config.get("models"), dict):
        raise ValueError("Model config requires version=1 and a models object")
    unknown = set(config["models"]) - set(STAGES)
    if unknown:
        raise ValueError(f"Unknown model stages: {sorted(unknown)}")
    for name, spec in config["models"].items():
        if not isinstance(spec, dict):
            raise ValueError(f"{name}: expected an object")
        if not isinstance(spec.get("enabled", True), bool):
            raise ValueError(f"{name}.enabled must be boolean")
        if not spec.get("enabled", True):
            continue
        if not spec.get("backend"):
            raise ValueError(f"{name}: backend is required")
        params = spec.setdefault("params", {})
        for key in ("weights", "config", "tracker"):
            if key in params:
                value = Path(params[key])
                value = value if value.is_absolute() else path.parent / value
                if not value.is_file():
                    raise FileNotFoundError(f"{name}.{key}: {value}")
                params[key] = str(value.resolve())
    enabled = {n for n, s in config["models"].items() if s.get("enabled", True)}
    for stage, required in (("plates", "vehicles"), ("ocr", "plates")):
        if stage in enabled and required not in enabled:
            raise ValueError(f"{stage} requires {required}")
    return config


def create_model(spec, device):
    target = BACKENDS.get(spec["backend"], spec["backend"])
    if isinstance(target, str):
        if ":" not in target:
            raise ValueError(f"Unknown backend: {target}")
        module, symbol = target.split(":", 1)
        target = getattr(importlib.import_module(module), symbol)
    params = dict(spec.get("params", {}))
    params.setdefault("device", device)
    model = target(**params)
    from .base import PerceptionModel
    if not isinstance(model, PerceptionModel):
        raise TypeError("Backend must inherit PerceptionModel")
    return model
