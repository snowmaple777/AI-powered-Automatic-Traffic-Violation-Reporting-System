"""Traffic AI 推論服務：把 run_pipeline.py 包成 HTTP API。

本機：  python -m inference_service.server                （預設 http://127.0.0.1:8770）
雲端：  python -m inference_service.server --host 0.0.0.0 --api-key <密鑰>

API（全部回傳 JSON，除了影像檔）
  GET    /health                       服務狀態、模型檔、GPU、是否忙碌
  GET    /jobs                         工作列表
  POST   /jobs?filename=&rules=&max_seconds=&clip_before=&clip_after=&device=
                                       body 直接放影片的二進位內容，回傳工作
  GET    /jobs/<id>                    進度（幀數、FPS、剩餘時間、已發現違規、紀錄）
  GET    /jobs/<id>/preview.jpg        即時畫面（畫上模型偵測框）
  GET    /jobs/<id>/result             最終結果（違規事件、片段與截圖檔名）
  GET    /jobs/<id>/files/<檔名>       片段 mp4／截圖 jpg
  POST   /jobs/<id>/cancel             取消
  DELETE /jobs/<id>                    刪除工作與檔案

自動填表（autoForm，會在這台電腦開瀏覽器）
  POST   /reports?city=&test=1&name=   body 放案件資料夾的 zip，開始填表（test=1 只填不送）
  GET    /reports                      填表紀錄
  GET    /reports/<id>                 狀態：starting/filling/waiting_verification/submitting/done/error/cancelled
  POST   /reports/<id>/continue        給「按 Enter 繼續」的驗證模式用
  POST   /reports/<id>/cancel          取消並關閉瀏覽器
設定 --api-key（或環境變數 INFERENCE_API_KEY）後，每個請求都要帶 X-API-Key 標頭。
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

if __package__ in (None, ""):  # 直接 python server.py 執行時
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "inference_service"

from inference_service.jobs import Manager, default_python, safe_name  # noqa: E402
from inference_service.reports import ReportManager  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
mimetypes.add_type("video/mp4", ".mp4")

MANAGER: Manager = None  # type: ignore
REPORTS: ReportManager = None  # type: ignore
API_KEY = ""


class Handler(BaseHTTPRequestHandler):
    server_version = "TrafficAIInference/1.0"

    def log_message(self, fmt, *args):
        if args and str(args[1])[:1] in ("4", "5"):
            sys.stderr.write("[HTTP] " + fmt % args + "\n")

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, msg, code=400):
        self._json({"error": msg}, code)

    def _file(self, path):
        path = Path(path)
        if not path.is_file():
            return self._error("檔案不存在", 404)
        size = path.stat().st_size
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            try:
                start = int(a) if a else max(0, size - int(b))
                end = int(b) if a and b else end
            except ValueError:
                pass
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 16, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _authorized(self):
        if not API_KEY or self.headers.get("X-API-Key") == API_KEY:
            return True
        self._error("API key 錯誤", 401)
        return False

    def _dispatch(self, method):
        if not self._authorized():
            return
        u = urlparse(self.path)
        parts = [unquote(p) for p in u.path.strip("/").split("/") if p]
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            self._route(method, parts, q)
        except FileNotFoundError:
            self._error("找不到", 404)
        except ValueError as e:
            self._error(str(e), 400)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._error(f"服務內部錯誤：{e}", 500)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _route(self, method, parts, q):
        if parts == ["health"] and method == "GET":
            return self._json(dict(MANAGER.health(), autoform=REPORTS.health()))
        if parts and parts[0] == "reports":
            return self._route_reports(method, parts, q)
        if parts == ["jobs"]:
            if method == "GET":
                return self._json(MANAGER.list())
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    return self._error("沒有收到影片")
                job = MANAGER.create(q.get("filename", "video.mp4"), self.rfile, length, q)
                return self._json(MANAGER.public(job), 201)
        if len(parts) >= 2 and parts[0] == "jobs":
            job = MANAGER.get(parts[1])
            if not job:
                return self._error("找不到這個工作", 404)
            jdir = Path(job["dir"])
            if len(parts) == 2:
                if method == "GET":
                    return self._json(MANAGER.public(job))
                if method == "DELETE":
                    MANAGER.delete(job["id"])
                    return self._json({"deleted": job["id"]})
            sub = parts[2] if len(parts) > 2 else ""
            if sub == "cancel" and method == "POST":
                return self._json(MANAGER.public(MANAGER.cancel(job["id"])))
            if sub == "preview.jpg" and method == "GET":
                return self._file(jdir / "preview.jpg")
            if sub == "result" and method == "GET":
                data = None
                if (jdir / "result.json").exists():
                    data = json.loads((jdir / "result.json").read_text(encoding="utf-8"))
                return self._json(data) if data else self._error("結果還沒好", 409)
            if sub == "files" and len(parts) == 4 and method == "GET":
                return self._file(jdir / "result" / safe_name(parts[3]))
        return self._error("not found", 404)

    def _route_reports(self, method, parts, q):
        if len(parts) == 1:
            if method == "GET":
                return self._json(REPORTS.list())
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > 500 * 1024 * 1024:
                    return self._error("沒有收到案件資料，或檔案太大")
                body = self.rfile.read(length)
                return self._json(REPORTS.public(REPORTS.create(body, q)), 201)
        rid = parts[1]
        if len(parts) == 2 and method == "GET":
            r = REPORTS.get(rid)
            return self._json(REPORTS.public(r)) if r else self._error("找不到這次填表", 404)
        if len(parts) == 3 and method == "POST" and parts[2] in ("continue", "cancel"):
            r = REPORTS.continue_(rid) if parts[2] == "continue" else REPORTS.cancel(rid)
            return self._json(REPORTS.public(r))
        return self._error("not found", 404)


def main():
    global MANAGER, REPORTS, API_KEY
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Traffic AI 推論服務")
    ap.add_argument("--host", default="127.0.0.1", help="雲端部署時用 0.0.0.0")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--pipeline-root", default=str(here.parent), help="run_pipeline.py 所在資料夾")
    ap.add_argument("--python", default=None, help="執行 run_pipeline.py 的 Python（預設 .venv）")
    ap.add_argument("--data-dir", default=str(here / "data"))
    ap.add_argument("--autoform-python", default=None, help="執行 autoForm 的 Python（預設同 --python）")
    ap.add_argument("--api-key", default=os.environ.get("INFERENCE_API_KEY", ""))
    args, extra = ap.parse_known_args()
    root = Path(args.pipeline_root).resolve()
    API_KEY = args.api_key
    MANAGER = Manager(args.data_dir, root, args.python or default_python(root), extra_args=extra)
    REPORTS = ReportManager(args.data_dir, root, args.autoform_python or MANAGER.python)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    h = MANAGER.health()
    print("=" * 60)
    print("  Traffic AI 推論服務已啟動")
    print(f"  網址：http://{args.host}:{args.port}   （UI 會連到這裡）")
    print(f"  Pipeline：{root}")
    print(f"  Python：{MANAGER.python}")
    missing = [m["path"] for m in h["models"] if m["enabled"] and not m["exists"]]
    print(f"  模型檔：{len(h['models']) - len(missing)}/{len(h['models'])} 個就緒"
          + ("" if not missing else "，缺少：" + "、".join(missing)))
    print(f"  ffmpeg：{'有' if h['ffmpeg'] else '沒有（片段不轉 H.264，UI 會改播原影片片段）'}")
    print("  GPU：偵測中…（可在 UI 或 /health 查看）")
    af = REPORTS.health()
    print(f"  自動填表：{'可以使用' if af['available'] else '暫時不能用（' + af.get('hint', '') + '）'}")
    print("  關閉：在這個視窗按 Ctrl+C")
    print("=" * 60)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已關閉")


if __name__ == "__main__":
    main()
