from .base import ModelResult
from .registry import STAGES, create_model


class ModelPipeline:
    def __init__(self, config, device="cpu"):
        self.models = {}
        try:
            for name in STAGES:
                spec = config["models"].get(name, {"enabled": False})
                if spec.get("enabled", True):
                    self.models[name] = create_model(spec, device)
        except Exception:
            self.close()
            raise

    def infer(self, frame, context):
        results = {name: ModelResult() for name in STAGES}
        for name, model in self.models.items():
            result = model.infer(frame, context, results)
            if not isinstance(result, ModelResult):
                raise TypeError(f"{name}.infer must return ModelResult")
            results[name] = result
        return results

    def reset(self):
        for model in self.models.values():
            model.reset()

    def close(self):
        for model in self.models.values():
            model.close()

    def __enter__(self):
        self.reset()
        return self

    def __exit__(self, *exc):
        self.close()
