# -*- coding: utf-8 -*-
"""
PP-OCRv6 台灣車牌專屬推論 Socket Server (獨立進程，透過 localhost socket 通訊)
"""

import os
import sys
import socket
import struct
import cv2
import numpy as np

# Locate optional NVIDIA DLLs in this interpreter's environment.
import importlib.util
try:
    cudnn_spec = importlib.util.find_spec('nvidia.cudnn')
except ModuleNotFoundError:
    cudnn_spec = None
cudnn_bin = os.path.join(os.path.dirname(cudnn_spec.origin), 'bin') if cudnn_spec and cudnn_spec.origin else ''
if os.path.isdir(cudnn_bin) and hasattr(os, 'add_dll_directory'):
    try:
        _cudnn_directory = os.add_dll_directory(cudnn_bin)
    except Exception:
        pass

import paddle.inference as paddle_infer

# 前處理、解碼、重試策略與台灣車牌驗證和 ONNX 後端共用同一份實作。
# 本檔以腳本方式啟動，sys.path[0] 即 model_library；perception_core 只依賴 cv2／numpy。
from perception_core import PlateTextRecognizer


class PPOCRWorker(PlateTextRecognizer):
    def __init__(self, model_dir="checkpoints/PP-OCRv6_taiwan_infer", dict_path="train_data/ppocrv6_dict.txt", device="cuda"):
        model_dir = os.path.abspath(model_dir)
        if os.path.isfile(model_dir):
            model_dir = os.path.dirname(model_dir)
        model_file = os.path.join(model_dir, "inference.json")
        params_file = os.path.join(model_dir, "inference.pdiparams")
        if not os.path.isfile(model_file) or not os.path.isfile(params_file):
            raise FileNotFoundError(f"找不到 Paddle Inference 模型: {model_file} 或 {params_file}")

        # Paddle's Windows PIR loader cannot read non-ASCII model paths.
        # Stage only these two files, keeping the temporary directory alive
        # until the predictor has closed (the child process owns its lifetime).
        self._model_temp = None
        if os.name == "nt" and not model_dir.isascii():
            import tempfile
            import shutil
            self._model_temp = tempfile.TemporaryDirectory(prefix="traffic_ppocr_")
            model_file = shutil.copy2(model_file, self._model_temp.name)
            params_file = shutil.copy2(params_file, self._model_temp.name)
        config = paddle_infer.Config(model_file, params_file)
        if str(device).startswith("cuda"):
            device_id = int(str(device).split(":")[1]) if ":" in str(device) else 0
            config.enable_use_gpu(100, device_id)
            config.enable_memory_optim()
        else:
            config.disable_gpu()
            config.set_cpu_math_library_num_threads(4)

        self.predictor = paddle_infer.create_predictor(config)
        self.input_name = self.predictor.get_input_names()[0]
        self.output_name = self.predictor.get_output_names()[0]

        if not os.path.isfile(dict_path):
            raise FileNotFoundError(f"找不到字元字典檔案: {dict_path}")
        with open(dict_path, "r", encoding="utf-8") as f:
            self._setup_characters(line.strip("\r\n") for line in f)

    def _forward(self, tensor):
        input_tensor = self.predictor.get_input_handle(self.input_name)
        input_tensor.reshape(tensor.shape)
        input_tensor.copy_from_cpu(tensor)
        self.predictor.run()
        return self.predictor.get_output_handle(self.output_name).copy_to_cpu()[0]  # [T, C] softmax


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True, help="Parent listening port")
    parser.add_argument("--model-dir", type=str, default="checkpoints/PP-OCRv6_taiwan_infer")
    parser.add_argument("--dict-path", type=str, default="train_data/ppocrv6_dict.txt")
    parser.add_argument("--device", type=str, default="cuda")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect(("127.0.0.1", args.port))

    try:
        worker = PPOCRWorker(model_dir=args.model_dir, dict_path=args.dict_path, device=args.device)
        sock.sendall(b"READY\n")
    except Exception as e:
        err_msg = f"ERROR: {e}\n".encode("utf-8")
        sock.sendall(err_msg)
        sock.close()
        sys.exit(1)

    while True:
        hdr = recv_exact(sock, 8)
        if not hdr:
            break
        h, w = struct.unpack(">II", hdr)
        if h == 0 or w == 0:
            break
        raw_len = h * w * 3
        img_bytes = recv_exact(sock, raw_len)
        if not img_bytes:
            break

        img = np.frombuffer(img_bytes, dtype=np.uint8).reshape((h, w, 3))
        text, conf = worker.predict(img)

        encoded_text = text.encode("utf-8")
        resp_hdr = struct.pack(">If", len(encoded_text), float(conf))
        sock.sendall(resp_hdr + encoded_text)

    sock.close()

if __name__ == "__main__":
    main()
