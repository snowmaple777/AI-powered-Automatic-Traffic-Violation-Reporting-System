"""Select an installed ONNX provider without implicit package installation."""
import warnings
import os
from functools import lru_cache

# Inference must not install or upgrade packages behind the caller's back.
os.environ["YOLO_AUTOINSTALL"] = "false"


@lru_cache(maxsize=1)
def prepare_onnx_cuda():
    """Load the installed PyTorch CUDA DLLs before creating ONNX sessions."""
    import onnxruntime as ort
    if "CUDAExecutionProvider" in ort.get_available_providers():
        import torch  # Loads matching CUDA/cuDNN DLLs shipped with PyTorch.
        if hasattr(ort, "preload_dlls"):
            ort.preload_dlls()
    return ort.get_available_providers()


def yolo_device(weights, device):
    if str(weights).lower().endswith(".onnx") and str(device) != "cpu":
        if "CUDAExecutionProvider" not in prepare_onnx_cuda():
            warnings.warn("ONNX Runtime CUDA unavailable; YOLO ONNX runs on CPU")
            return "cpu"
    return device


@lru_cache(maxsize=3)
def onnx_predictor(cuda_conv_search="HEURISTIC"):
    """Configure dynamic ONNX sessions before Ultralytics performs warmup."""
    if cuda_conv_search not in {"HEURISTIC", "EXHAUSTIVE", "DEFAULT"}:
        raise ValueError("Invalid CUDA convolution search mode")
    from ultralytics.models.yolo.detect import DetectionPredictor

    class TrafficPredictor(DetectionPredictor):
        def setup_model(self, model, verbose=True):
            super().setup_model(model, verbose)
            backend = self.model.backend
            if self.model.format != "onnx" or backend.use_io_binding:
                return
            session = backend.session
            if "CUDAExecutionProvider" not in session.get_providers():
                return
            options = session.get_provider_options()["CUDAExecutionProvider"]
            if options.get("cudnn_conv_algo_search") != cuda_conv_search:
                options["cudnn_conv_algo_search"] = cuda_conv_search
                session.set_providers([("CUDAExecutionProvider", options), "CPUExecutionProvider"])

    return TrafficPredictor
