"""串流輸入：邊收串流邊錄成固定幀率的分段檔，分析端依序讀分段（4bq）。

設計：
- 收錄（StreamRecorder，背景執行緒）：用 OpenCV 開串流網址（rtsp/http/udp…）或本機檔，依來源時間戳把影格
  重新取樣成固定幀率（掉幀補前一張、多的丟掉），每 segment_seconds 秒寫成一個分段檔，關檔後記入 segments.jsonl。
  分析比串流慢時影格暫存在硬碟分段裡，不會在記憶體堆積。
- 分析（SegmentedFrames）：依序讀已關檔的分段，介面同 cv2.VideoCapture.read()，影格編號連續；
  時間軸仍是「幀號 ÷ fps」，所以規則與模型不用改。
- 剪片（SegmentedCapture）：違規片段從同一批分段剪，與分析看到的影格完全相同。

分段編碼預設 FFV1（無損），分析畫面與直接解碼相同；mp4v／MJPG 較省空間但有損，實測會改變偵測結果（refactor_log 4bq）。
無損編碼較慢（RTX 5060 筆電 CPU：720p 約 23 ms／幀、1080p 約 180 ms／幀），直播來源若編碼跟不上，stats 的 ingest_lag_seconds 會增加。
限制：串流中斷不自動重連（視為串流結束）；不錄聲音。
"""
from __future__ import annotations

import collections
import itertools
import argparse
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2

STREAM_SCHEMES = ("rtsp", "rtsps", "rtmp", "rtmps", "http", "https", "udp", "tcp", "srt")
INDEX_NAME = "segments.jsonl"
INFO_NAME = "stream.json"
STOP_NAME = "stop.flag"
CODEC_EXT = {"FFV1": ".avi", "MJPG": ".avi", "mp4v": ".mp4"}
LAG_WARNING_SECONDS = 10.0
DEFAULT_FPS = 30.0
MAX_SOURCE_FPS = 120.0
ESTIMATE_SECONDS = 3.0
ENCODER_THREADS = 4  # 同時編碼的分段數上限（記憶體上限約 4 段原始影格）
STANDARD_FPS = (5, 6, 7.5, 10, 12, 12.5, 15, 20, 23.976, 24, 25, 29.97, 30, 50, 59.94, 60)


def url_fps(source):
    """網址參數 fps=30 → 30.0；沒有或不合理時回傳 None。"""
    if not is_stream_url(source):
        return None
    try:
        value = float(parse_qs(urlparse(source).query).get("fps", [""])[0])
    except ValueError:
        return None
    return value if math.isfinite(value) and 0 < value <= MAX_SOURCE_FPS else None


def snap_fps(measured):
    """估計幀率貼齊常見值（差 4% 以內），否則取到小數一位。"""
    whole = round(measured)
    if whole in STANDARD_FPS and abs(whole - measured) <= measured * .01:  # 30 與 29.97 這類相近值優先取整數
        return float(whole)
    best = min(STANDARD_FPS, key=lambda f: abs(f - measured))
    return float(best) if abs(best - measured) <= measured * .10 else round(measured, 1)


def is_stream_url(text) -> bool:
    return isinstance(text, str) and urlparse(text).scheme.lower() in STREAM_SCHEMES


def input_source(text):
    """argparse type：串流網址保持字串（Path 會把 // 壓成 /），其餘當檔案路徑。"""
    return text if is_stream_url(text) else Path(text)


def stream_name(source) -> str:
    """輸出檔名用的名稱：檔案取 stem，網址取主機與路徑。"""
    if not is_stream_url(source):
        return Path(source).stem
    parsed = urlparse(source)
    raw = f"stream_{parsed.hostname or 'source'}_{parsed.path.strip('/')}"
    return re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_")[:80] or "stream"


def resample(timed_frames, fps):
    """(秒, 影格) 依序輸入 → 固定幀率影格。第 k 格取時間最接近 k/fps 的來源影格：掉幀時重複前一張，來源較快時丟掉多的。
    時間戳以第一格為 0；回傳 generator，每次產出 (影格, 是否為重複)。"""
    k, pending, pending_t, used = 0, None, None, False
    for t, frame in timed_frames:
        if pending is not None:
            while (k + .5) / fps <= t:
                yield pending, used
                used, k = True, k + 1
        pending, pending_t, used = frame, t, False
    if pending is not None:
        # 片尾：最後一張只填到它自己的時間點為止（同幀率時剛好一格）
        while k / fps <= pending_t + 1e-9:
            yield pending, used
            used, k = True, k + 1


class StreamError(RuntimeError):
    pass


class StreamRecorder(threading.Thread):
    """背景收錄串流成固定幀率分段。ready 事件之後 fps／width／height 才有效。"""

    def __init__(self, source, folder, fps=None, segment_seconds=2.0, codec="FFV1",
                 max_seconds=0.0, open_timeout_ms=15000, read_timeout_ms=15000, stop_files=()):
        super().__init__(daemon=True, name="stream-recorder")
        if codec not in CODEC_EXT:
            raise ValueError(f"不支援的分段編碼：{codec}（可用 {', '.join(CODEC_EXT)}）")
        if fps is not None and (not math.isfinite(fps) or fps <= 0):
            raise ValueError("串流幀率必須是正數")
        if not math.isfinite(segment_seconds) or segment_seconds <= 0:
            raise ValueError("分段秒數必須是正數")
        self.source, self.folder = str(source), Path(folder)
        self.requested_fps, self.segment_seconds, self.codec = fps, segment_seconds, codec
        self.max_seconds = max_seconds or 0.0
        self.open_timeout_ms, self.read_timeout_ms = open_timeout_ms, read_timeout_ms
        self.stop_files = [Path(f) for f in stop_files if f]  # 任一檔案出現就停止收錄（推論服務的「停止串流」等）
        self.timestamps = None      # source（來源時間戳）／estimated（估計幀率）／nominal（本機檔標稱幀率）
        self.fps = self.width = self.height = None
        self.source_fps = None
        self.segments = []          # 已關檔：{"file", "first_frame", "frames"}
        self.frames = 0             # 已寫入的固定幀率影格數
        self.decoded = self.duplicated = self.dropped = 0
        self.lag = self.max_lag = 0.0  # 收錄進度落後牆上時間的秒數（直播才有意義；本機檔通常為 0）
        self.reason = None          # source_end / max_seconds / stopped / error
        self.error = None
        self.finished = False
        self.ready = threading.Event()
        self.changed = threading.Condition()
        self.stop_requested = threading.Event()

    # ------------------------------------------------------------------ 收錄
    def _open(self):
        params = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self.open_timeout_ms,
                  cv2.CAP_PROP_READ_TIMEOUT_MSEC, self.read_timeout_ms]
        cap = cv2.VideoCapture(self.source, cv2.CAP_FFMPEG, params)
        if not cap.isOpened():
            cap.release()
            raise StreamError(f"無法開啟串流：{self.source}")
        return cap

    def _read(self, cap):
        ok, frame = cap.read()
        if ok:
            self.decoded += 1
        return ok, frame

    def _timed_frames(self, cap):
        """(相對秒數, 影格)。來源有可用時間戳就用時間戳；沒有時（例如網路攝影機的 MJPEG，OpenCV 只回報預設 25 FPS）
        先依前 1.5 秒的到達時間估計實際幀率，之後依序以「第幾張 ÷ 估計幀率」計時。
        不直接用到達時間：收錄若一度落後，積在連線裡的影格會一口氣讀進來，到達時間會擠在一起。"""
        ok, frame = self._read(cap)
        if not ok:
            self.reason = self.reason or "source_end"
            return
        msec = cap.get(cv2.CAP_PROP_POS_MSEC)
        valid = lambda m: math.isfinite(m) and 0 <= m < 1e12
        pending = [(time.monotonic(), frame)]
        if valid(msec):
            # 第一張的時間戳可能是 0；第二張不遞增時同樣改用估計
            ok2, frame2 = self._read(cap)
            msec2 = cap.get(cv2.CAP_PROP_POS_MSEC) if ok2 else msec
            if ok2:
                pending.append((time.monotonic(), frame2))
            if ok2 and valid(msec2) and msec2 > msec:
                self.timestamps = "source"
                yield 0.0, frame
                t0, previous = msec / 1000, 0.0
                rel = msec2 / 1000 - t0
                previous = rel
                yield rel, frame2
                while not self.stop_requested.is_set():
                    ok, frame = self._read(cap)
                    if not ok:
                        self.reason = self.reason or "source_end"
                        return
                    m = cap.get(cv2.CAP_PROP_POS_MSEC)
                    rel = m / 1000 - t0 if valid(m) else previous + 1 / self.source_fps
                    if rel <= previous:
                        rel = previous + 1 / self.source_fps
                    previous = rel
                    yield rel, frame
                self.reason = self.reason or "stopped"
                return
            if not ok2:
                self.reason = self.reason or "source_end"
        # 沒有可用時間戳：網址帶 fps=（不少網路攝影機與 tools/simulate_stream.py 都這樣給）就用它，否則估計幀率
        hinted = url_fps(self.source)
        if hinted:
            self.source_fps, self.timestamps = hinted, "url_fps"
        while not hinted and not self.reason and not self.stop_requested.is_set() and pending[-1][0] - pending[0][0] < ESTIMATE_SECONDS:
            ok, frame = self._read(cap)
            if not ok:
                self.reason = self.reason or "source_end"
                break
            pending.append((time.monotonic(), frame))
        if not hinted:
            if len(pending) >= 3 and is_stream_url(self.source):
                self.source_fps = snap_fps((len(pending) - 1) / max(1e-6, pending[-1][0] - pending[0][0]))
            self.timestamps = "estimated" if is_stream_url(self.source) else "nominal"
        index = 0
        for _, frame in pending:
            yield index / self.source_fps, frame
            index += 1
        while not self.reason and not self.stop_requested.is_set():
            ok, frame = self._read(cap)
            if not ok:
                self.reason = self.reason or "source_end"
                return
            yield index / self.source_fps, frame
            index += 1
        self.reason = self.reason or "stopped"

    def _encode(self, job):
        """編碼執行緒：把佇列裡的影格寫成一個分段檔（OpenCV 編碼時會釋放 GIL，多段可同時編碼）。"""
        segment, frames = job["segment"], job["queue"]
        writer = cv2.VideoWriter(str(self.folder / segment["file"]), cv2.VideoWriter_fourcc(*self.codec),
                                 self.fps, (self.width, self.height))
        try:
            if not writer.isOpened():
                job["error"] = f"無法建立分段檔：{self.folder / segment['file']}"
            while True:
                frame = frames.get()
                if frame is None:
                    return
                if not job["error"]:
                    writer.write(frame)
        finally:
            writer.release()

    def _publish(self, active, wait_all=False, keep=None):
        """依順序發布已編碼完成的分段；keep 表示最多保留幾段在編碼中（超過就等最舊的一段，形成背壓）。"""
        while active and (wait_all or not active[0]["thread"].is_alive() or (keep is not None and len(active) > keep)):
            job = active.popleft()
            job["thread"].join()
            if job["error"]:
                raise StreamError(job["error"])
            self._close_segment(job["segment"])

    def run(self):
        active, job = collections.deque(), None
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            (self.folder / INDEX_NAME).write_text("", encoding="utf-8")
            cap = self._open()
            try:
                reported = cap.get(cv2.CAP_PROP_FPS)
                self.source_fps = reported if math.isfinite(reported) and 0 < reported <= MAX_SOURCE_FPS else DEFAULT_FPS
                timed = self._timed_frames(cap)
                first = next(timed, None)  # 讀第一張時決定時間戳來源（必要時估計幀率），之後 source_fps 才確定
                if first is None:
                    raise StreamError("來源沒有任何影格")
                fps = float(self.requested_fps or self.source_fps)
                per_segment = max(1, round(self.segment_seconds * fps))
                limit = math.floor(self.max_seconds * fps) if self.max_seconds else 0
                for frame, duplicate in resample(itertools.chain([first], timed), fps):
                    if self.fps is None:
                        self.fps, (self.height, self.width) = fps, frame.shape[:2]
                        started = time.monotonic()
                        self._write_info()
                        self.ready.set()
                    if job is None:
                        self._publish(active, keep=ENCODER_THREADS - 1)
                        job = dict(segment=dict(file=f"seg_{len(self.segments) + len(active):05d}{CODEC_EXT[self.codec]}",
                                                first_frame=self.frames, frames=0),
                                   queue=queue.Queue(), error=None)
                        job["thread"] = threading.Thread(target=self._encode, args=(job,), daemon=True)
                        job["thread"].start()
                        active.append(job)
                    job["queue"].put(frame)
                    job["segment"]["frames"] += 1
                    self.frames += 1
                    self.duplicated += duplicate
                    if job["segment"]["frames"] >= per_segment:
                        job["queue"].put(None)
                        job = None
                        self._publish(active)
                    # 落後秒數以「已寫完並發布」的影格為準（扣掉湊滿一段本來就要等的時間），編碼跟不上時才看得出來
                    published = self.segments[-1]["first_frame"] + self.segments[-1]["frames"] if self.segments else 0
                    self.lag = max(0.0, time.monotonic() - started - published / fps - self.segment_seconds)
                    if self.lag > self.max_lag:
                        if self.max_lag <= LAG_WARNING_SECONDS < self.lag and is_stream_url(self.source):
                            print(f"警告：分段編碼跟不上串流（已落後 {self.lag:.0f} 秒）；可改用 --stream-codec MJPG 或降低解析度",
                                  flush=True)
                        self.max_lag = self.lag
                    if limit and self.frames >= limit:
                        self.reason = "max_seconds"
                        break
                    if self.stop_requested.is_set() or (self.frames % 10 == 0
                                                        and any(f.exists() for f in self.stop_files)):
                        self.reason = "stopped"
                        break
            finally:
                cap.release()
            self.dropped = max(0, self.decoded - (self.frames - self.duplicated))
        except Exception as e:  # noqa: BLE001
            self.error, self.reason = str(e) or type(e).__name__, "error"
        finally:
            if job is not None:
                job["queue"].put(None)
            try:
                self._publish(active, wait_all=True)
            except Exception as e:  # noqa: BLE001
                self.error, self.reason = self.error or str(e), "error"
            with self.changed:
                self.finished = True
                self.changed.notify_all()
            if self.fps is not None or self.error:
                self._write_info()  # finished 設好之後才寫，讀取端看到 finished 就代表統計是最終值
            self.ready.set()

    def _close_segment(self, segment):
        if not segment or not segment["frames"]:
            return
        with (self.folder / INDEX_NAME).open("a", encoding="utf-8") as f:
            f.write(json.dumps(segment, ensure_ascii=False) + "\n")
        with self.changed:
            self.segments.append(dict(segment))
            self.changed.notify_all()
        self._write_info()  # 每關一段更新統計，推論服務據此顯示收錄進度

    def _write_info(self):
        tmp = self.folder / (INFO_NAME + ".tmp")  # 先寫暫存再換名，讀取端不會讀到寫一半的檔案
        tmp.write_text(json.dumps(self.stats(), ensure_ascii=False, indent=2), encoding="utf-8")
        # Windows：讀取端（分析程序、推論服務）剛好開著舊檔時換名會被拒絕，稍後重試
        for attempt in range(50):
            try:
                os.replace(tmp, self.folder / INFO_NAME)
                return
            except PermissionError:
                if attempt == 49:
                    raise
                time.sleep(.01)

    def wait_segment(self, index):
        """等第 index 個分段關檔；收錄結束且沒有這一段時回傳 None。"""
        with self.changed:
            while index >= len(self.segments) and not self.finished:
                self.changed.wait(1.0)
            return self.segments[index] if index < len(self.segments) else None

    def stats(self):
        return dict(source=self.source, fps=self.fps, width=self.width, height=self.height,
                    source_fps=self.source_fps, timestamps=self.timestamps, codec=self.codec,
                    segment_seconds=self.segment_seconds,
                    frames=self.frames, decoded=self.decoded, duplicated=self.duplicated, dropped=self.dropped,
                    segments=len(self.segments), ingest_lag_seconds=round(self.lag, 1),
                    max_ingest_lag_seconds=round(self.max_lag, 1),
                    reason=self.reason, error=self.error, finished=self.finished)

    def wait_ready(self, timeout=None):
        if not self.ready.wait(timeout):
            raise StreamError("等待串流第一個影格逾時")
        if self.fps is None:
            raise StreamError(f"串流沒有任何影格：{self.error or self.reason or '來源已結束'}")

    def stop(self, timeout=30):
        self.stop_requested.set()
        if self.is_alive():
            self.join(timeout)


class SegmentedFrames:
    """分析端：依序讀 recorder 已關檔的分段，read() 與 cv2.VideoCapture 相同；串流結束回傳 (False, None)。"""

    def __init__(self, recorder):
        self.recorder = recorder
        self.next_segment = 0
        self.cap = None
        self.remaining = 0
        self.frames_read = 0

    def _segment(self):
        segment = self.recorder.wait_segment(self.next_segment)
        if segment is not None:
            self.next_segment += 1
        return segment

    def read(self):
        while self.remaining == 0:
            if self.cap is not None:
                self.cap.release()
                self.cap = None
            segment = self._segment()
            if segment is None:
                if self.recorder.error:
                    raise StreamError(f"串流收錄失敗：{self.recorder.error}")
                return False, None
            if segment["first_frame"] != self.frames_read:
                raise StreamError(f"分段不連續：預期從第 {self.frames_read} 幀開始，實際 {segment['first_frame']}")
            self.cap = cv2.VideoCapture(str(self.recorder.folder / segment["file"]))
            self.remaining = segment["frames"]
        ok, frame = self.cap.read()
        if not ok:
            raise StreamError(f"分段檔提早結束（第 {self.frames_read} 幀）")
        self.remaining -= 1
        self.frames_read += 1
        return True, frame

    def release(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None


class StreamProcess:
    """在獨立程序收錄串流（不與分析搶 GIL 與 CPU，估計幀率與編碼速度才準），介面與 StreamRecorder 相同：
    start／wait_ready／wait_segment／stats／stop，加上 fps、width、height、source_fps、folder、error。
    兩邊只透過分段資料夾溝通：segments.jsonl（已關檔分段）、stream.json（統計）、stop.flag（要求停止）。
    收錄程序的標準輸入接著本程序；本程序結束（包括被強制結束）時它讀到 EOF 就自行收尾。"""

    def __init__(self, source, folder, fps=None, segment_seconds=2.0, codec="FFV1", max_seconds=0.0,
                 open_timeout_ms=15000, read_timeout_ms=15000, stop_files=()):
        StreamRecorder(source, folder, fps, segment_seconds, codec, max_seconds)  # 只借用參數檢查
        self.source, self.folder = str(source), Path(folder)
        self.options = dict(fps=fps, segment_seconds=segment_seconds, codec=codec, max_seconds=max_seconds,
                            open_timeout_ms=open_timeout_ms, read_timeout_ms=read_timeout_ms)
        self.stop_files = [str(f) for f in stop_files if f]
        self.proc = None
        self.fps = self.width = self.height = self.source_fps = None
        self.error = None
        self._segments, self._index_pos, self._partial = [], 0, b""

    def start(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        for name in (INFO_NAME, INDEX_NAME, STOP_NAME):
            (self.folder / name).unlink(missing_ok=True)
        cmd = [sys.executable, str(Path(__file__).resolve()), "record", self.source, str(self.folder),
               "--segment-seconds", str(self.options["segment_seconds"]), "--codec", self.options["codec"],
               "--max-seconds", str(self.options["max_seconds"] or 0),
               "--open-timeout-ms", str(self.options["open_timeout_ms"]),
               "--read-timeout-ms", str(self.options["read_timeout_ms"])]
        if self.options["fps"]:
            cmd += ["--fps", str(self.options["fps"])]
        for f in self.stop_files:
            cmd += ["--stop-file", f]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    def _info(self):
        try:
            return json.loads((self.folder / INFO_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _ended(self, info):
        return bool(info and info.get("finished")) or (self.proc is not None and self.proc.poll() is not None)

    def wait_ready(self, timeout=60):
        end = time.time() + timeout
        while True:
            info = self._info()
            if info and info.get("fps"):
                self.fps, self.width, self.height = info["fps"], info["width"], info["height"]
                self.source_fps = info.get("source_fps")
                return
            if self._ended(info):
                info = self._info() or {}
                if info.get("fps"):
                    continue
                raise StreamError(f"串流沒有任何影格：{info.get('error') or info.get('reason') or '收錄程序已結束'}")
            if time.time() > end:
                raise StreamError("等待串流第一個影格逾時")
            time.sleep(.1)

    def _read_index(self):
        try:
            with (self.folder / INDEX_NAME).open("rb") as f:
                f.seek(self._index_pos)
                chunk = f.read()
        except OSError:
            return
        self._index_pos += len(chunk)
        *lines, self._partial = (self._partial + chunk).split(b"\n")
        self._segments += [json.loads(line) for line in lines if line.strip()]

    def wait_segment(self, index):
        while True:
            self._read_index()
            if index < len(self._segments):
                return self._segments[index]
            info = self._info()
            if self._ended(info):
                self._read_index()
                if index < len(self._segments):
                    return self._segments[index]
                info = self._info() or {}
                code = self.proc.poll() if self.proc else None
                self.error = info.get("error") or (f"收錄程序異常結束（代碼 {code}）"
                                                   if code not in (None, 0) and not info.get("finished") else None)
                return None
            time.sleep(.1)

    def stats(self):
        return self._info() or {}

    def stop(self, timeout=30):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            (self.folder / STOP_NAME).write_text("stop", encoding="utf-8")
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if self.proc.stdin:
            try:
                self.proc.stdin.close()
            except OSError:
                pass


def _record_main(argv):
    """收錄程序入口（由 StreamProcess 啟動）。"""
    ap = argparse.ArgumentParser(description="串流收錄程序")
    ap.add_argument("source")
    ap.add_argument("folder", type=Path)
    ap.add_argument("--fps", type=float, default=None)
    ap.add_argument("--segment-seconds", type=float, default=2.0)
    ap.add_argument("--codec", default="FFV1")
    ap.add_argument("--max-seconds", type=float, default=0.0)
    ap.add_argument("--open-timeout-ms", type=int, default=15000)
    ap.add_argument("--read-timeout-ms", type=int, default=15000)
    ap.add_argument("--stop-file", action="append", default=[])
    args = ap.parse_args(argv)
    recorder = StreamRecorder(args.source, args.folder, args.fps, args.segment_seconds, args.codec, args.max_seconds,
                              args.open_timeout_ms, args.read_timeout_ms,
                              stop_files=[args.folder / STOP_NAME, *args.stop_file])

    def watch_parent():
        try:
            sys.stdin.buffer.read()  # 父程序結束或關閉管線時回傳
        except (OSError, ValueError):
            pass
        recorder.stop_requested.set()

    threading.Thread(target=watch_parent, daemon=True).start()
    recorder.start()
    recorder.join()
    return 1 if recorder.error else 0


def _exit_now(code):
    """收錄完就直接結束：監看父程序的執行緒還卡在讀 stdin，正常關閉直譯器會在收尾時當掉（Windows 0xC0000005）。"""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def is_segment_folder(path) -> bool:
    path = Path(path)
    return path.is_dir() and (path / INDEX_NAME).is_file() and (path / INFO_NAME).is_file()


class SegmentedCapture:
    """剪片用：把分段資料夾當成一支影片（get／set(POS_FRAMES)／read／release）。"""

    def __init__(self, folder):
        self.folder = Path(folder)
        info = json.loads((self.folder / INFO_NAME).read_text(encoding="utf-8"))
        self.fps, self.width, self.height = info["fps"], info["width"], info["height"]
        # 收錄中也可開啟（4bs 深度分析）：只取完整的行，正在寫入的最後一行略過。
        text = (self.folder / INDEX_NAME).read_text(encoding="utf-8")
        self.segments = [json.loads(line) for line in text[:text.rfind("\n") + 1].splitlines() if line.strip()]
        self.total = sum(s["frames"] for s in self.segments)
        self.position, self.cap, self.current = 0, None, None

    def isOpened(self):  # noqa: N802 （與 cv2.VideoCapture 同名）
        return bool(self.segments)

    def get(self, prop):
        return {cv2.CAP_PROP_FPS: self.fps, cv2.CAP_PROP_FRAME_WIDTH: self.width,
                cv2.CAP_PROP_FRAME_HEIGHT: self.height, cv2.CAP_PROP_FRAME_COUNT: self.total,
                cv2.CAP_PROP_POS_FRAMES: self.position}.get(prop, 0)

    def set(self, prop, value):
        if prop != cv2.CAP_PROP_POS_FRAMES or not 0 <= int(value) < self.total:
            return False
        self.release()
        self.position = int(value)
        return True

    def _open_for(self, index):
        for segment in self.segments:
            if segment["first_frame"] <= index < segment["first_frame"] + segment["frames"]:
                self.release()
                self.cap, self.current = cv2.VideoCapture(str(self.folder / segment["file"])), segment
                for _ in range(index - segment["first_frame"]):  # 分段內逐格略過，不依賴編碼器的定位精度
                    if not self.cap.read()[0]:
                        return False
                return True
        return False

    def read(self):
        if self.position >= self.total:
            return False, None
        current = self.current
        if self.cap is None or not (current["first_frame"] <= self.position < current["first_frame"] + current["frames"]):
            if not self._open_for(self.position):
                return False, None
        ok, frame = self.cap.read()
        if ok:
            self.position += 1
        return ok, frame

    def release(self):
        if self.cap is not None:
            self.cap.release()
        self.cap, self.current = None, None


def open_capture(path):
    """剪片與其他讀原影片的地方：分段資料夾用 SegmentedCapture，其他照舊用 cv2.VideoCapture。"""
    return SegmentedCapture(path) if is_segment_folder(path) else cv2.VideoCapture(str(path))


if __name__ == "__main__" and sys.argv[1:2] == ["record"]:
    _exit_now(_record_main(sys.argv[2:]))
