"""Local-only integration fixture. Run with the project's .venv interpreter."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from http.server import ThreadingHTTPServer

import cv2
import numpy as np


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--inference-port", type=int, default=18770)
    parser.add_argument("--real", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[4]
    with tempfile.TemporaryDirectory(prefix="autoform-project-test-") as temp:
        base = Path(temp)
        ui = base / "ui"
        shutil.copytree(root / "ui" / "core", ui / "core", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(root / "ui" / "web", ui / "web")
        shutil.copy2(root / "ui" / "server.py", ui / "server.py")
        (ui / "config.json").write_text(json.dumps({"inference_url": f"http://127.0.0.1:{args.inference_port}"}), encoding="utf-8")
        clips = ui / "clips" / "fixture_at_example.invalid"
        clips.mkdir(parents=True)
        writer = cv2.VideoWriter(str(clips / "fixture.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), 10, (320, 240))
        if not writer.isOpened():
            raise RuntimeError("mp4v writer unavailable")
        try:
            for n in range(40):
                writer.write(np.full((240, 320, 3), n * 4, dtype=np.uint8))
        finally:
            writer.release()
        sys.path.insert(0, str(ui))
        import server
        # Keep geocoding and profile data out of external services during this test.
        server.address.reverse_address = lambda *_: {"ok": False}
        class FixtureHandler(server.Handler):
            def log_message(self, fmt, *args):
                pass
            def _route_post(self):
                from urllib.parse import urlparse
                path = urlparse(self.path).path
                if path == "/api/profile" or "/autofill" in path or path.endswith("/submit"):
                    print("Unexpected profile or submission request", flush=True)
                    return self._error("Disabled in fixture", 403)
                return super()._route_post()
        http = ThreadingHTTPServer(("127.0.0.1", args.port), FixtureHandler)
        http.lan = True
        inference = None
        try:
            if args.real:
                inference = subprocess.Popen([sys.executable, "-m", "inference_service.server",
                    "--port", str(args.inference_port), "--data-dir", str(base / "inference")], cwd=root)
            print("Fixture ready", flush=True)
            http.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            http.server_close()
            if inference:
                inference.terminate()
                inference.wait()


if __name__ == "__main__":
    main()
