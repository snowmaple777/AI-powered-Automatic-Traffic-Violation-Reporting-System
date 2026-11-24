"""把影片當成即時串流播出（測試串流分析用；不屬於網頁介面）。

以 HTTP MJPEG（multipart/x-mixed-replace，網路攝影機常見格式）依影片原本的幀率即時送出畫面；
每個連線各自從影片開頭播放，播完就結束（--loop 循環播放）。
MJPEG 沒有時間戳，網址帶 ?fps=影片幀率 讓收錄端用正確的時間軸（伺服器本身忽略這個參數）。

    .venv\\Scripts\\python.exe tools\\simulate_stream.py "input\\ctm01.mp4"
    → 在網頁「即時串流」貼上啟動畫面顯示的網址（例如 http://127.0.0.1:8790/stream.mjpg?fps=30）

JPEG 是有損壓縮（--quality 預設 100），分析結果可能與直接讀檔略有差異（品質 95 實測紅燈偵測少 17%）。
"""
import argparse
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2

BOUNDARY = "frame"


def make_handler(video, fps, quality, loop, speed):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 只印連線開始／結束
            pass

        def do_GET(self):
            if self.path.split("?")[0] not in ("/", "/stream.mjpg"):
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            client = f"{self.client_address[0]}:{self.client_address[1]}"
            print(f"開始播放給 {client}", flush=True)
            sent = 0
            try:
                while True:
                    cap = cv2.VideoCapture(str(video))
                    started, index = time.perf_counter(), 0
                    while True:
                        ok, frame = cap.read()
                        if not ok:
                            break
                        wait = started + index / (fps * speed) - time.perf_counter()
                        if wait > 0:
                            time.sleep(wait)
                        ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                        data = jpg.tobytes()
                        self.wfile.write(f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                                         f"Content-Length: {len(data)}\r\n\r\n".encode() + data + b"\r\n")
                        index += 1
                        sent += 1
                    cap.release()
                    if not loop:
                        break
                self.wfile.write(f"--{BOUNDARY}--\r\n".encode())
                print(f"播放完畢（{client}，{sent} 幀）", flush=True)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                print(f"{client} 已中斷連線（送出 {sent} 幀）", flush=True)

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", type=Path)
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 讓區網其他電腦也能連")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--loop", action="store_true", help="播完從頭循環")
    ap.add_argument("--speed", type=float, default=1.0, help="播放速度倍率（測試用，預設 1＝即時）")
    ap.add_argument("--quality", type=int, default=100, help="JPEG 品質 1–100（預設 100：PSNR 約 50 dB，較低會讓小號誌偵測變少）")
    args = ap.parse_args()
    cap = cv2.VideoCapture(str(args.video))
    if not cap.isOpened():
        sys.exit(f"無法開啟影片：{args.video}")
    fps, frames = cap.get(cv2.CAP_PROP_FPS) or 30.0, int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    server = ThreadingHTTPServer((args.host, args.port),
                                 make_handler(args.video, fps, args.quality, args.loop, args.speed))
    server.daemon_threads = True
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "") else args.host
    print("=" * 60)
    print(f"  模擬串流：{args.video.name}（{w}x{h}，{fps:g} FPS，{frames / fps:.1f} 秒{'，循環' if args.loop else ''}）")
    print(f"  串流網址：http://{shown}:{args.port}/stream.mjpg?fps={fps * args.speed:g}")
    print("  每個連線從頭播放；關閉：Ctrl+C")
    print("=" * 60, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已關閉")


if __name__ == "__main__":
    threading.current_thread().name = "simulate-stream"
    main()
