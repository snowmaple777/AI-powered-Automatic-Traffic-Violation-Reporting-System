"""馬路三寶檢舉輔助系統 — 本地 Web 介面伺服器（只用 Python 標準函式庫）。

用法：
    python server.py              # 開啟 http://127.0.0.1:8765
    python server.py --lan        # 讓同一個 Wi-Fi 的手機也能連（用戶終端展示）
    python server.py --port 9000 --no-browser
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import socket
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import auth, geo, gps_parser, store  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
# 這些路徑要登入，而且只能存取自己的資料
PRIVATE_PREFIXES = ("/api/jobs", "/api/cases", "/api/drafts", "/api/profile", "/api/upload", "/files/")
FILE_ROOTS = {"uploads": store.UPLOAD_DIR, "jobs": store.JOB_DIR, "cases": store.CASE_DIR}
mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("application/javascript", ".js")


def lan_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ips)


class Handler(BaseHTTPRequestHandler):
    server_version = "TrafficReportUI/1.0"

    def log_message(self, fmt, *args):  # 安靜一點，只印錯誤
        if args and str(args[1]).startswith(("4", "5")) and str(args[1]) != "404":
            sys.stderr.write("[HTTP] " + fmt % args + "\n")

    # ---------- 回應工具 ----------
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

    def _body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def _send_file(self, path, download_name=None):
        if not os.path.isfile(path):
            return self._error("檔案不存在", 404)
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            try:
                if a:
                    start = int(a)
                    end = int(b) if b else size - 1
                else:
                    start = max(0, size - int(b))
            except ValueError:
                pass
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-cache")
        if download_name:
            self.send_header("Content-Disposition", f"attachment; filename=\"{download_name}\"")
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

    # ---------- 路由 ----------
    def _token(self):
        t = self.headers.get("X-Auth-Token")
        if t:
            return t
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "tr_token":
                return unquote(v)
        return None

    def _login_user(self):
        """依 token 設定目前使用者；需要登入的路徑沒登入就回 401。"""
        email = auth.user_email(self._token())
        store.set_user(email)
        p = urlparse(self.path).path
        if not email and p.startswith(PRIVATE_PREFIXES):
            self._error("請先登入", 401)
            return False
        return True

    def do_GET(self):
        if not self._login_user():
            return
        try:
            self._route_get()
        except PermissionError as e:
            self._error(str(e), 401)
        except FileNotFoundError as e:
            self._error(str(e), 404)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._error(str(e), 500)

    def do_POST(self):
        if not self._login_user():
            return
        try:
            self._route_post()
        except PermissionError as e:
            self._error(str(e), 401)
        except FileNotFoundError as e:
            self._error(str(e), 404)
        except ValueError as e:
            self._error(str(e), 400)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._error(str(e), 500)

    def _route_get(self):
        u = urlparse(self.path)
        p, q = unquote(u.path), parse_qs(u.query)
        if p in ("/", "/index.html"):
            return self._send_file(os.path.join(WEB_DIR, "index.html"))
        if p.startswith("/static/"):
            f = os.path.normpath(os.path.join(WEB_DIR, p[len("/static/"):]))
            if not f.startswith(WEB_DIR):
                return self._error("forbidden", 403)
            return self._send_file(f)
        if p.startswith("/files/"):
            parts = p.split("/")[2:]
            root = FILE_ROOTS.get(parts[0]) if parts else None
            if not root or len(parts) < 2 or not store.owns_file(parts[0], parts[1]):
                return self._error("not found", 404)
            f = os.path.normpath(os.path.join(root, *[store.safe_name(x) for x in parts[1:]]))
            if not f.startswith(root):
                return self._error("forbidden", 403)
            return self._send_file(f)
        if p == "/api/autofill/health":
            return self._json(store.autofill_health())
        if p == "/api/inference/health":
            return self._json(store.inference_health())
        if p == "/api/presets":
            return self._json(geo.PRESETS)
        if p == "/api/geocode":
            return self._json(geo.reverse_geocode(float(q["lat"][0]), float(q["lon"][0])))
        if p == "/api/cities":
            return self._json([{"name": k, "code": v} for k, v in geo.CITY_CODES.items()])
        if p == "/api/jobs":
            return self._json(store.list_jobs())
        if p.startswith("/api/jobs/"):
            parts = p.split("/")
            job = store.get_job(parts[3])
            if not job:
                return self._error("找不到分析紀錄", 404)
            if len(parts) > 4 and parts[4] == "preview.jpg":
                return self._send_file(os.path.join(store.JOB_DIR, store.safe_name(parts[3]), "preview.jpg"))
            return self._json(job)
        if p == "/api/cases":
            return self._json(store.list_cases())
        if p.startswith("/api/cases/"):
            parts = p.split("/")
            if len(parts) > 4 and parts[4] == "autofill":
                return self._json(store.autofill_status(parts[3]))
            if len(parts) > 4 and parts[4] == "zip":
                path = store.zip_case(parts[3])
                return self._send_file(path, download_name=os.path.basename(path))
            case = store.get_case(parts[3])
            return self._json(case) if case else self._error("找不到案件", 404)
        if p == "/api/auth/me":
            user = auth.me(self.headers.get("X-Auth-Token"))
            return self._json({"user": user}) if user else self._error("尚未登入", 401)
        if p == "/api/profile":
            return self._json(store.get_profile())
        if p == "/api/drafts":
            return self._json(store.list_drafts())
        if p.startswith("/api/drafts/"):
            d = store.get_draft(p.split("/")[3])
            return self._json(d) if d else self._error("找不到草稿", 404)
        return self._error("not found", 404)

    def _route_post(self):
        u = urlparse(self.path)
        p, q = unquote(u.path), parse_qs(u.query)
        if p == "/api/upload":
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return self._error("沒有收到檔案")
            return self._json(store.save_upload(q.get("filename", ["video.mp4"])[0], self.rfile, n))
        if p == "/api/gps/parse":
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            try:
                track = gps_parser.parse_track(q.get("filename", ["track.csv"])[0], raw)
            except Exception as e:  # noqa: BLE001
                return self._error(f"GPS 檔解析失敗：{e}")
            if not track:
                return self._error("GPS 檔裡沒有可用的座標點（支援 CSV / GPX / NMEA）")
            first = track[0]
            return self._json({"count": len(track), "track": gps_parser.track_to_json(track),
                               "geo": geo.reverse_geocode(first["lat"], first["lon"])})
        if p == "/api/jobs":
            return self._json(store.create_job(self._body_json()))
        if p.startswith("/api/jobs/") and p.endswith("/cancel"):
            return self._json(store.cancel_job(p.split("/")[3]))
        if p == "/api/cases":
            return self._json(store.create_case(self._body_json()))
        if p.startswith("/api/cases/") and p.endswith("/autofill"):
            return self._json(store.start_autofill(p.split("/")[3], self._body_json()))
        if p.startswith("/api/cases/") and (p.endswith("/autofill/continue") or p.endswith("/autofill/cancel")):
            return self._json(store.autofill_status(p.split("/")[3], p.rsplit("/", 1)[1]))
        if p.startswith("/api/cases/") and p.endswith("/submit"):
            return self._json(store.submit_case(p.split("/")[3], self._body_json()))
        if p.startswith("/api/auth/"):
            return self._route_auth(p.split("/")[3], self._body_json())
        if p == "/api/profile":
            return self._json(store.save_profile(self._body_json()))
        if p == "/api/drafts":
            return self._json(store.create_draft(self._body_json()))
        if p.startswith("/api/drafts/") and p.endswith("/delete"):
            return self._json(store.delete_draft(p.split("/")[3]))
        if p.startswith("/api/drafts/"):
            return self._json(store.update_draft(p.split("/")[3], self._body_json()))
        return self._error("not found", 404)


def _auth_dispatch(handler, action, body):
    try:
        if action == "register":
            return handler._json(auth.register(body.get("name"), body.get("email"), body.get("password")))
        if action == "login":
            return handler._json(auth.login(body.get("email"), body.get("password")))
        if action == "google":
            return handler._json(auth.google_login(body.get("email"), body.get("name")))
        if action == "logout":
            return handler._json(auth.logout(handler.headers.get("X-Auth-Token")))
        if action == "reset":
            return handler._json(auth.reset_password(body.get("email")))
    except auth.AuthError as e:
        return handler._error(str(e), 401 if action == "login" else 400)
    return handler._error("not found", 404)


Handler._route_auth = lambda self, action, body: _auth_dispatch(self, action, body)


def main():
    ap = argparse.ArgumentParser(description="馬路三寶檢舉輔助系統 — 本地 Web 介面")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--lan", action="store_true", help="開放區網連線（手機展示用）")
    ap.add_argument("--no-browser", action="store_true", help="不要自動開瀏覽器")
    args = ap.parse_args()

    host = "0.0.0.0" if args.lan else "127.0.0.1"
    httpd = ThreadingHTTPServer((host, args.port), Handler)
    httpd.daemon_threads = True
    httpd.lan = args.lan
    url = f"http://127.0.0.1:{args.port}"
    print("=" * 56)
    print("  馬路三寶檢舉輔助系統 — 介面已啟動")
    print(f"  本機：{url}")
    print(f"  推論服務：{store.CONFIG['inference_url']}（真實模型模式會連到這裡）")
    if args.lan:
        for ip in lan_ips():
            print(f"  手機（同一個 Wi-Fi）：http://{ip}:{args.port}")
    print("  關閉：在這個視窗按 Ctrl+C")
    print("=" * 56)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已關閉")


if __name__ == "__main__":
    main()
