"""Select an installed ONNX provider without implicit package installation."""
import warnings
import os

# Inference must not install or upgrade packages behind the caller's back.
os.environ["YOLO_AUTOINSTALL"] = "false"


def yolo_device(weights, device):
    if str(weights).lower().endswith(".onnx") and str(device) != "cpu":
        import onnxruntime as ort
        if "CUDAExecutionProvider" not in ort.get_available_providers():
            warnings.warn("ONNX Runtime CUDA unavailable; YOLO ONNX runs on CPU")
            return "cpu"
    return device
