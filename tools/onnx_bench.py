"""直接以 onnxruntime 量測車牌／車輛 ONNX 模型在不同輸入形狀模式下的單次推論耗時（排除 ultralytics）。

情境：固定 320、固定 640x256（路面掃描）、每幀「320、320、640x256」交替（現行車牌流程）、
多種長寬比輪流（ultralytics rect 縮放）。各情境搭配 cuDNN 卷積演算法搜尋 EXHAUSTIVE／HEURISTIC／DEFAULT。
python tools/onnx_bench.py
"""
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RECT_SHAPES = [(320, 320), (320, 256), (320, 192), (256, 320), (192, 320), (320, 224), (288, 320), (320, 160)]
SCENARIOS = {
    "固定 320x320": lambda i: (320, 320),
    "固定 256x640": lambda i: (256, 640),
    "交替 320,320,640": lambda i: (320, 320) if i % 3 < 2 else (256, 640),
    "rect 多形狀輪流": lambda i: RECT_SHAPES[i % len(RECT_SHAPES)],
}


def bench(session, shape_for, calls=45, warm=9):
    name = session.get_inputs()[0].name
    rng = np.random.default_rng(0)
    times = []
    for i in range(calls):
        h, w = shape_for(i)
        tensor = rng.random((1, 3, h, w), dtype=np.float32)
        start = time.perf_counter()
        session.run(None, {name: tensor})
        times.append(time.perf_counter() - start)
    steady = times[warm:]
    return {"median_ms": round(1000 * statistics.median(steady), 1), "max_ms": round(1000 * max(steady), 1),
            "first_ms": round(1000 * times[0], 1)}


def main():
    from model_library.runtime import prepare_onnx_cuda
    import onnxruntime as ort
    providers_available = prepare_onnx_cuda()
    report = {"onnxruntime": ort.__version__, "available": providers_available, "models": {}}
    models = {"plate": ROOT / "models/plate/license-plate-finetune-v1s.onnx",
              "vehicle": ROOT / "models/vehicle/yolo26s.onnx"}
    lines = [f"===== ONNX 推論微基準（onnxruntime {ort.__version__}）====="]
    for model_name, path in models.items():
        for search in ("EXHAUSTIVE", "HEURISTIC", "DEFAULT"):
            options = ort.SessionOptions()
            options.log_severity_level = 3
            session = ort.InferenceSession(str(path), options, providers=[
                ("CUDAExecutionProvider", {"device_id": 0, "cudnn_conv_algo_search": search}), "CPUExecutionProvider"])
            info = {"input": [str(d) for d in session.get_inputs()[0].shape], "providers": session.get_providers()}
            for scenario, shape_for in SCENARIOS.items():
                info[scenario] = bench(session, shape_for)
                print(f"{model_name} {search} {scenario}: {info[scenario]}", flush=True)
            report["models"][f"{model_name}/{search}"] = info
            lines.append(f"{model_name} [{search}] 輸入 {info['input']}  providers {info['providers'][0]}")
            lines.append("  " + "  ".join(f"{k}: 中位 {v['median_ms']} ms（最大 {v['max_ms']}）"
                                         for k, v in info.items() if isinstance(v, dict)))
            if model_name == "vehicle":
                break  # 車輛模型只需確認基準
    out = ROOT / "outputs/verify/profile"
    out.mkdir(parents=True, exist_ok=True)
    (out / "onnx_bench.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + "\n".join(lines))


if __name__ == "__main__":
    main()
