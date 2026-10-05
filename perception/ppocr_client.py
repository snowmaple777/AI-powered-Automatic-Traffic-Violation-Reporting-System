# -*- coding: utf-8 -*-
"""
PP-OCRv6 台灣車牌專屬微調推論客戶端 (透過本地 Socket 與獨立 Paddle Inference 進程通訊)
徹底隔離 PyTorch 與 Paddle 在 Windows 上的 cuDNN 衝突，支援全速 GPU 推論。
"""

import os
import sys
import socket
import struct
import subprocess
import time
import cv2
import numpy as np

def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)

class PaddleInferWorkerRecognizer:
    def __init__(self, model_dir="checkpoints/PP-OCRv6_taiwan_infer", dict_path="train_data/ppocrv6_dict.txt", device="cuda"):
        # 1. 建立監聽 socket
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.bind(("127.0.0.1", 0))
        server_sock.listen(1)
        port = server_sock.getsockname()[1]

        # 2. 清理環境變數，防止 PyTorch DLL 污染子進程
        env = dict(os.environ)
        paths = env.get("PATH", "").split(os.pathsep)
        clean_paths = [p for p in paths if "torch\\lib" not in p.lower() and "torch/lib" not in p.lower()]
        
        cudnn_bin = r"C:\Users\User\AppData\Local\Programs\Python\Python310\lib\site-packages\nvidia\cudnn\bin"
        if os.path.isdir(cudnn_bin):
            clean_paths.insert(0, cudnn_bin)
        env["PATH"] = os.pathsep.join(clean_paths)

        # 3. 啟動獨立子進程
        cmd = [
            sys.executable,
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "ppocr_worker.py"),
            "--port", str(port),
            "--model-dir", str(model_dir),
            "--dict-path", str(dict_path),
            "--device", str(device)
        ]
        self.proc = subprocess.Popen(cmd, env=env)

        # 4. 等待子進程連線
        server_sock.settimeout(20.0)
        self.conn, _ = server_sock.accept()
        server_sock.close()

        # 5. 握手確認
        ready_line = bytearray()
        while not ready_line.endswith(b"\n"):
            ch = self.conn.recv(1)
            if not ch:
                break
            ready_line.extend(ch)
        
        status_msg = ready_line.decode("utf-8", errors="ignore").strip()
        if status_msg != "READY":
            raise RuntimeError(f"PPOCR Worker 初始化失敗: {status_msg}")

    def predict(self, img):
        if img is None or img.size == 0 or img.shape[0] < 6 or img.shape[1] < 12:
            return "", 0.0

        if not img.flags['C_CONTIGUOUS']:
            img = np.ascontiguousarray(img)

        h, w = img.shape[:2]
        hdr = struct.pack(">II", h, w)
        self.conn.sendall(hdr + img.tobytes())

        resp_hdr = recv_exact(self.conn, 8)
        if not resp_hdr:
            return "", 0.0
        text_len, conf = struct.unpack(">If", resp_hdr)
        text = ""
        if text_len > 0:
            text_bytes = recv_exact(self.conn, text_len)
            text = text_bytes.decode("utf-8", errors="ignore") if text_bytes else ""
        return text, float(conf)

    def __call__(self, img, **kwargs):
        text, conf = self.predict(img)
        if text:
            return [[text, conf]], 0.0
        return None, 0.0

    def close(self):
        try:
            if hasattr(self, 'conn') and self.conn:
                self.conn.sendall(struct.pack(">II", 0, 0))
                self.conn.close()
            if hasattr(self, 'proc') and self.proc:
                self.proc.wait(timeout=2.0)
        except Exception:
            pass

    def __del__(self):
        self.close()
