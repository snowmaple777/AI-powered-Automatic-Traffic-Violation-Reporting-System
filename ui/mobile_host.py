"""Start the project services used by the Android client."""
import os
from pathlib import Path
import socket
import subprocess
import sys
from urllib.parse import urlsplit

import server
from core import store


def main():
    endpoint = urlsplit(store.CONFIG["inference_url"])
    child = None
    # Manage only the bundled loopback service. A separately configured GPU host
    # or a service that was already running remains under its operator's control.
    if endpoint.scheme == "http" and endpoint.hostname in ("127.0.0.1", "localhost") and endpoint.port:
        try:
            with socket.create_connection((endpoint.hostname, endpoint.port), timeout=1):
                pass
        except OSError:
            root = Path(__file__).resolve().parents[1]
            env = dict(os.environ)
            if store.CONFIG.get("inference_api_key"):
                env["INFERENCE_API_KEY"] = store.CONFIG["inference_api_key"]
            child = subprocess.Popen([sys.executable, "-u", "-m", "inference_service.server",
                                      "--port", str(endpoint.port)], cwd=root, env=env)
    try:
        sys.argv[1:] = ["--lan", "--no-browser"]
        server.main()
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait()


if __name__ == "__main__":
    main()
