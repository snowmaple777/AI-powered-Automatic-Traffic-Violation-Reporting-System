"""Isolate Paddle from PyTorch DLLs; bound IPC and reap the worker on all exits."""
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time

import numpy as np


def recv_exact(sock, length):
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length-len(data))
        if not chunk:
            raise RuntimeError("PP-OCRv6 worker disconnected")
        data.extend(chunk)
    return bytes(data)


class PaddleInferWorkerRecognizer:
    def __init__(self, model_dir, dict_path, device="cpu", timeout=60):
        self.conn = self.proc = None
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                       if "torch/lib" not in p.lower().replace("\\", "/"))
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.bind(("127.0.0.1",0))
            server.listen(1)
            server.settimeout(.25)
            cmd = [sys.executable,str(Path(__file__).with_name("ppocr_worker.py")),
                   "--port",str(server.getsockname()[1]),"--model-dir",str(Path(model_dir).resolve()),
                   "--dict-path",str(Path(dict_path).resolve()),"--device",str(device)]
            self.proc = subprocess.Popen(cmd,env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            deadline = time.monotonic()+timeout
            while self.conn is None:
                if self.proc.poll() is not None:
                    raise RuntimeError("PP-OCRv6 worker exited during startup; install requirements-perception.txt or use configs/onnx_vehicle.json")
                if time.monotonic() >= deadline:
                    raise TimeoutError("PP-OCRv6 worker startup timed out")
                try:
                    self.conn,_ = server.accept()
                except socket.timeout:
                    continue
            self.conn.settimeout(timeout)
            ready = bytearray()
            while not ready.endswith(b"\n") and len(ready) < 8192:
                ready.extend(recv_exact(self.conn,1))
            if ready != b"READY\n":
                raise RuntimeError(ready.decode("utf-8",errors="replace").strip())
        except BaseException:
            self.close()
            raise
        finally:
            server.close()

    def predict(self, image):
        if image is None or image.size == 0 or image.shape[0] < 6 or image.shape[1] < 12:
            return "",0.
        image = np.ascontiguousarray(image,dtype=np.uint8)
        h,w = image.shape[:2]
        self.conn.sendall(struct.pack(">II",h,w)+image.tobytes())
        length,confidence = struct.unpack(">If",recv_exact(self.conn,8))
        if length > 65536:
            raise RuntimeError("Invalid PP-OCRv6 response length")
        return recv_exact(self.conn,length).decode("utf-8"),float(confidence)

    def close(self):
        conn,self.conn = self.conn,None
        if conn is not None:
            try:
                conn.settimeout(.5)
                conn.sendall(struct.pack(">II",0,0))
            except OSError:
                pass
            finally:
                conn.close()
        proc,self.proc = self.proc,None
        if proc is not None:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
