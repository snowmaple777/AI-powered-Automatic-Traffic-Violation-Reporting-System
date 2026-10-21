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

    # 可選：每個階段完成後呼叫 stage_callback(name, results)，讓呼叫端提早啟動只依賴
    # 該階段結果的 CPU 工作（例如車輛偵測後的鏡頭運動估計），與後續 GPU 推論並行。
    stage_callback = None

    def infer(self, frame, context):
        results = {name: ModelResult() for name in STAGES}
        for name, model in self.models.items():
            result = model.infer(frame, context, results)
            if not isinstance(result, ModelResult):
                raise TypeError(f"{name}.infer must return ModelResult")
            results[name] = result
            if self.stage_callback is not None:
                self.stage_callback(name, results)
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
