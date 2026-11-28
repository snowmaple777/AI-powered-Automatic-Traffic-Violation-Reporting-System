# -*- coding: utf-8 -*-
"""掃描行車紀錄器畫面上的浮水印（OSD）：錄影時間與 GPS 座標。

上傳影片後只看開頭幾秒，讀疊在畫面上的字，預先填好「影片開始錄影時間」與 GPS 位置。
讀到的只是建議值，介面會請使用者確認；讀不到就什麼都不填。

流程：
1. 取開頭 3 秒的 6 張畫面（cv2）。
2. 找「每張畫面都在同一個位置的細亮筆畫」＝疊字所在的文字列（路景會動、字不會動）。
3. 每一列用 PaddleOCR 官方通用版 PP-OCRv6_small_rec（ONNX，CPU）讀字，
   原圖、灰階、反相灰階三種輸入各算一票。先用兩張畫面找出有時間／座標的文字列，其餘畫面只讀那幾列。
   （專案的車牌模型 PP-OCRv6_taiwan 微調後會丟掉 : . ° 等標點，不能用來讀浮水印。）
4. 日期（年、月、日）、時間、緯度、經度分開投票，一個字讀錯不會拖累其他欄位；
   時間與座標要至少兩票、而且比其他所有讀法加起來多兩票以上才採用；
   座標另外要有四分之一以上的讀數同意（模糊的座標列常常重複讀錯同一個字）。讀不準寧可不填。
   時間減去該畫面在影片中的秒數，推回影片第 0 秒的時間。

模型：models/ocr/PP-OCRv6_small_rec_onnx/inference.onnx（「下載模型.cmd」會下載並比對 SHA-256），
字典與車牌模型共用 models/ocr/ppocrv6_dict.txt。需要 onnxruntime、opencv-python、numpy（專案的 .venv 都有）。

單獨測試：python ui/core/overlay_scan.py 影片.mp4 [影片2 ...] [--debug]
"""
from __future__ import annotations

import math
import os
import re
import shutil
import statistics
import tempfile
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta

try:
    import cv2  # type: ignore
    import numpy as np
except ImportError:  # 介面其他功能不需要 OpenCV；沒有就不掃
    cv2 = None
    np = None

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_MODEL = os.path.join(PROJECT_DIR, "models", "ocr", "PP-OCRv6_small_rec_onnx", "inference.onnx")
DICT_PATH = os.path.join(PROJECT_DIR, "models", "ocr", "ppocrv6_dict.txt")
ENGINE = "PP-OCRv6_small_rec"

# 台灣（含金門、馬祖、澎湖、蘭嶼）的經緯度範圍：讀錯的座標多半落在範圍外
TW_LAT = (21.5, 26.5)
TW_LON = (118.0, 122.5)

SCAN_SECONDS = 3.0      # 只看開頭幾秒
SCAN_FRAMES = 6         # 取幾張畫面
PROBE_FRAMES = 2        # 先用幾張畫面找出有用的文字列
MAX_LINES = 12          # 第一輪最多讀幾列（路面靜止時標線也會被當成字）
BANDS = ((0.74, 1.0), (0.0, 0.16))  # 浮水印常見的位置：畫面下緣、上緣（高度比例）
N_VARIANTS = 3          # _variants() 產生幾種輸入，每種一票
COORD_SUPPORT = 0.25    # 座標至少要有這個比例的讀數同意
VERSION = 5             # 掃描方式改了就加一，介面的快取結果會重掃


# ---------------------------------------------------------------------------
# 文字辨識：PP-OCRv6_small_rec（ONNX）
# ---------------------------------------------------------------------------
class Recognizer:
    """單行文字辨識。輸入 BGR 圖（一列字），輸出文字；CTC greedy 解碼，所有字元都可以出現。"""
    HEIGHT = 48

    def __init__(self, model_path: str, dict_path: str = DICT_PATH):
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        opts.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
        self.session = ort.InferenceSession(model_path, sess_options=opts, providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name
        with open(dict_path, encoding="utf-8") as f:
            self.chars = ["blank"] + [line.rstrip("\r\n") for line in f] + [" "]
        classes = self.session.get_outputs()[0].shape[-1]
        if isinstance(classes, int) and classes != len(self.chars):
            raise ValueError(f"字典有 {len(self.chars)} 類，模型輸出 {classes} 類，兩者不相符")

    def __call__(self, img) -> str:
        if img is None or img.size == 0 or img.shape[0] < 4 or img.shape[1] < 4:
            return ""
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        h, w = img.shape[:2]
        width = max(64, int(math.ceil(self.HEIGHT * w / h)))
        x = cv2.resize(img, (width, self.HEIGHT), interpolation=cv2.INTER_LINEAR).astype(np.float32)
        x = np.ascontiguousarray(((x / 255.0 - 0.5) / 0.5).transpose(2, 0, 1)[None])
        probs = self.session.run([self.output_name], {self.input_name: x})[0][0]
        text, last = [], 0
        for i in probs.argmax(axis=1):
            if i != 0 and i != last and i < len(self.chars):
                text.append(self.chars[i])
            last = i
        return "".join(text)


_engines: dict = {}
_engine_lock = threading.Lock()


def get_recognizer(model_path: str | None = None):
    """回傳 (Recognizer, None) 或 (None, 讀不了的原因)。載入一次後共用；失敗不快取（補上模型就能用）。"""
    model_path = model_path or DEFAULT_MODEL
    if cv2 is None:
        return None, "沒有安裝 opencv-python"
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return None, "沒有安裝 onnxruntime（用「啟動介面.cmd」以專案的 .venv 啟動就有）"
    if not os.path.isfile(model_path):
        return None, "找不到浮水印辨識模型，請先執行「下載模型.cmd」"
    if not os.path.isfile(DICT_PATH):
        return None, "找不到 models/ocr/ppocrv6_dict.txt，請先執行「下載模型.cmd」"
    with _engine_lock:
        if model_path not in _engines:
            try:
                _engines[model_path] = Recognizer(model_path)
            except Exception as e:  # noqa: BLE001
                return None, f"浮水印辨識模型載入失敗：{e}"
        return _engines[model_path], None


def _variants(crop):
    """三種輸入：原始彩色、RGB 最大值灰階（白／黃／藍字都偏亮）、反相灰階（白底黑字）。"""
    gray = crop.max(axis=2)
    return [crop, cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), cv2.cvtColor(255 - gray, cv2.COLOR_GRAY2BGR)]


# ---------------------------------------------------------------------------
# 取畫面與找文字列
# ---------------------------------------------------------------------------
def _open_video(path: str):
    """Windows 上部分 OpenCV 版本打不開含中文的路徑：退回到暫存目錄的英文檔名。"""
    cap = cv2.VideoCapture(path)
    if cap.isOpened() or path.isascii():
        return cap, None
    cap.release()
    alias = os.path.join(tempfile.gettempdir(), f"overlay_scan_{os.getpid()}_{time.time_ns()}{os.path.splitext(path)[1]}")
    try:
        os.link(path, alias)  # 同一個磁碟就用硬連結，不用複製整支影片
    except OSError:
        try:
            shutil.copyfile(path, alias)
        except OSError:
            return cv2.VideoCapture(path), None
    return cv2.VideoCapture(alias), alias


def sample_frames(path: str, seconds: float = SCAN_SECONDS, count: int = SCAN_FRAMES):
    """回傳 [(秒數, BGR 畫面), ...]：開頭 seconds 秒內平均取 count 張（不含第一幀）。"""
    cap, alias = _open_video(path)
    if not cap.isOpened():
        cap.release()
        if alias:
            os.remove(alias)
        return []
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        if not (1.0 <= fps <= 240.0):
            fps = 30.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        last = int(seconds * fps)
        if total > 0:
            last = min(last, total - 1)
        wanted = sorted({max(1, round(last * (i + 1) / count)) for i in range(count)})
        frames, idx = [], 0
        for target in wanted:
            while idx < target:  # 開頭幾秒直接循序讀，比 seek 準、也不慢
                if not cap.grab():
                    return frames
                idx += 1
            ok, frame = cap.read()
            idx += 1
            if not ok:
                break
            frames.append(((idx - 1) / fps, frame))
        return frames
    finally:
        cap.release()
        if alias:
            try:
                os.remove(alias)
            except OSError:
                pass


def find_text_lines(frames) -> list[tuple[int, int, int, int, int]]:
    """找出各張畫面都出現在同一位置的文字列，回傳 [(x0, y0, x1, y1, 字高), ...]（原圖座標）。"""
    h, w = frames[0][1].shape[:2]
    stroke = cv2.getStructuringElement(cv2.MORPH_RECT, (max(7, int(round(h * 0.012)) | 1),) * 2)
    gap = cv2.getStructuringElement(cv2.MORPH_RECT, (max(9, int(round(h * 0.022))), 3))
    need = max(2, int(np.ceil(len(frames) * 0.8)))
    boxes = []
    for y0f, y1f in BANDS:
        y0, y1 = int(h * y0f), int(h * y1f)
        votes = np.zeros((y1 - y0, w), np.uint8)
        for _, frame in frames:
            # 細而亮的筆畫（白、黃、藍字都會在 RGB 最大值上偏亮）：亮度的 top-hat
            votes += cv2.morphologyEx(frame[y0:y1].max(axis=2), cv2.MORPH_TOPHAT, stroke) > 45
        static = (votes >= need).astype(np.uint8) * 255
        lines = cv2.morphologyEx(static, cv2.MORPH_CLOSE, gap)  # 同一列的字連起來
        n, _, stats, _ = cv2.connectedComponentsWithStats(lines, 8)
        for i in range(1, n):
            x, y, bw, bh, area = (int(v) for v in stats[i])
            if not (h * 0.010 <= bh <= h * 0.07):     # 字高大約 1%～7% 畫面高
                continue
            if bw < bh * 3 or area < bw * bh * 0.15:  # 文字列要夠長、筆畫要夠密
                continue
            pad_y, pad_x = int(bh * 0.35) + 2, int(bh * 0.6) + 2
            boxes.append((max(0, x - pad_x), max(0, y0 + y - pad_y), min(w, x + bw + pad_x),
                          min(h, y0 + y + bh + pad_y), bh))
    # 越靠近畫面上下邊緣越像浮水印；太多列時只留最外側的
    boxes.sort(key=lambda b: min(b[1], h - b[3]))
    return boxes[:MAX_LINES]


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
_FIX = str.maketrans({"O": "0", "o": "0", "Q": "0", "D": "0", "l": "1", "I": "1", "|": "1", "!": "1",
                      "]": "1", "Z": "2", "z": "2", "B": "8", "S": "5", "s": "5", "G": "6", "g": "9"})
# 年／月／日；分隔符號可能被貼圖蓋住或認成別的符號（2026¥09/11），各欄位之後分開投票
_DATE = re.compile(r"(?<![\d])(2\S{2}\d)\s*[^\w\s:]\s*(\w{1,2})\s*[^\w\s:]\s*(\w{1,2})(?!\w)")
_TOD = re.compile(r"(?<![\d])(\d{1,2})\s*[:：]\s*(\d{2})\s*[:：]\s*(\d{2})(?!\d)")
_DEC = re.compile(r"(?<![\d.])(\d{2,3}\.\d{3,7})(?!\d)")
# 度分秒：分的符號常被讀成 ° 或漏掉；秒必須有小數、或後面接雙引號（' 是分的符號，不算），
# 避免把「120°1 5'29」這類亂碼拆成「1 分 5 秒」
_DMS = re.compile(r"(?<![\d])(\d{2,3})\s*[°º˚*]\s*(\d{1,2})\s*['’′`,°º˚\"]?\s*"
                  r"(\d{1,2}\.\d{1,3}|\d{1,2}(?=\s*[\"”″]))")
# 分被多讀出一個空白（°1 5'）：接回來
_SPLIT_MIN = re.compile(r"([°º˚*])\s*(\d)\s+(\d)(?=\s*['’′`])")


def _normalize(text: str) -> str:
    """數字為主的字詞，把常見的認錯字母換回數字（開頭的 N、E 等方位字母保留）。"""
    out = []
    for tok in text.split():
        if sum(c.isdigit() for c in tok) >= max(2, len(tok) // 2):
            head = tok[0] if tok[0] in "NSEWnsew" else ""
            tok = head + tok[len(head):].translate(_FIX)
        out.append(tok)
    # 等寬字型常被讀成「N25. 102942」：小數點後的空白拿掉；度分秒的分被拆開也接回來
    text = re.sub(r"(?<=\d\.)\s+(?=\d)", "", " ".join(out))
    return _SPLIT_MIN.sub(r"\1\2\3", text)


def _year(token: str, today: date):
    """年份最多允許一個字被遮住或認錯（例如貼圖蓋住 2026 的 0）：補上最近、不晚於今年的那個。"""
    if token.isdigit():
        return int(token) if 2010 <= int(token) <= today.year else None
    bad = [i for i, c in enumerate(token) if not c.isdigit()]
    if len(bad) != 1:
        return None
    years = [int(token[:bad[0]] + d + token[bad[0] + 1:]) for d in "0123456789"]
    years = [y for y in years if 2010 <= y <= today.year]
    return max(years) if years else None


def parse_date_parts(text: str, today: date | None = None):
    """回傳 [(年 或 None, 月 或 None, 日 或 None), ...]；讀不出來或不合理的欄位是 None。"""
    today = today or date.today()
    found = []
    for m in _DATE.finditer(text):
        y = _year(m.group(1), today)
        mo = int(m.group(2)) if m.group(2).isdigit() and 1 <= int(m.group(2)) <= 12 else None
        d = int(m.group(3)) if m.group(3).isdigit() and 1 <= int(m.group(3)) <= 31 else None
        if y or mo or d:
            found.append((y, mo, d))
    return found


def vote_date(parts, need: int = 2, today: date | None = None):
    """年、月、日各自多數決（每欄至少 need 票），組成合理日期才回傳。"""
    today = today or date.today()
    picked = []
    for i in range(3):
        c = Counter(p[i] for p in parts if p[i] is not None)
        if not c:
            return None, 0
        top = c.most_common(2)
        value, n = top[0]
        if n < need or (len(top) > 1 and top[1][1] == n):
            return None, 0
        picked.append((value, n))
    try:
        d = date(picked[0][0], picked[1][0], picked[2][0])
    except ValueError:
        return None, 0
    if not (date(2010, 1, 1) <= d <= today + timedelta(days=1)):
        return None, 0
    return d, min(n for _, n in picked)


def parse_times(text: str):
    """時間（當天第幾秒）。"""
    found = []
    for m in _TOD.finditer(text):
        hh, mm, ss = map(int, m.groups())
        if hh < 24 and mm < 60 and ss < 60:
            found.append(hh * 3600 + mm * 60 + ss)
    return found


def _sort_coord(v, lats, lons):
    if TW_LAT[0] <= v <= TW_LAT[1]:
        lats.append(v)
    elif TW_LON[0] <= v <= TW_LON[1]:
        lons.append(v)


def parse_coords(text: str):
    """回傳 (緯度 或 None, 經度 或 None)。支援 N25.1028 E121.5199、N:25.0048、N 24°15'19.4" 等；
    只接受台灣範圍。緯度、經度分開回傳：一半讀錯時另一半仍可投票。"""
    lats, lons = [], []
    for m in _DMS.finditer(text):
        deg, mi, se = int(m.group(1)), int(m.group(2)), float(m.group(3))
        if mi < 60 and se < 60:
            _sort_coord(deg + mi / 60 + se / 3600, lats, lons)
    for m in _DEC.finditer(text):
        _sort_coord(float(m.group(1)), lats, lons)
    return (lats[0] if lats else None), (lons[0] if lons else None)


def _cluster(values, tol, dist):
    """與某一票差距在 tol 內、票數最多的一群（票很少，直接兩兩比）。"""
    best = []
    for v in values:
        group = [u for u in values if dist(u, v) <= tol]
        if len(group) > len(best):
            best = group
    return best


def _winner(values, tol, dist, need=2, margin=2):
    """票數最多的一群；至少 need 票，而且要比「其他所有讀法加起來」多 margin 票以上。
    只有少數幾票時，兩個剛好一樣的錯誤讀法不能贏過一個正確讀法（寧可不填）。"""
    group = _cluster(values, tol, dist)
    return group if len(group) >= need and len(group) - (len(values) - len(group)) >= margin else []


def _useful(text: str) -> bool:
    t = _normalize(text)
    return bool(_DATE.search(t) or _TOD.search(t) or _DMS.search(t) or _DEC.search(t))


# ---------------------------------------------------------------------------
# 對外
# ---------------------------------------------------------------------------
def scan(path: str, model_path: str | None = None, seconds: float = SCAN_SECONDS,
         count: int = SCAN_FRAMES, debug: bool = False) -> dict:
    """掃影片開頭的浮水印。回傳
    {available, engine, recorded_at, date_votes, time_votes, lat, lon, coord_votes, frames, lines, elapsed}
    recorded_at：推回影片第 0 秒的當地時間（ISO 字串、無時區）；讀不到的欄位是 None。
    available=False 時 reason 說明原因（沒有模型、沒有 onnxruntime 等）。
    """
    t0 = time.time()
    result = {"available": False, "engine": None, "recorded_at": None, "date_votes": 0, "time_votes": 0,
              "lat": None, "lon": None, "coord_votes": 0, "frames": 0, "lines": 0, "elapsed": 0.0}
    rec, reason = get_recognizer(model_path)
    if rec is None:
        result["reason"] = reason
        return result
    result.update(available=True, engine=ENGINE)
    frames = sample_frames(path, seconds, count)
    result["frames"] = len(frames)
    boxes = find_text_lines(frames) if len(frames) >= 2 else []
    inputs: dict = {}
    reads: dict = {}

    def read(fi, bi, vi):
        if (fi, bi, vi) not in reads:
            if (fi, bi) not in inputs:
                x0, y0, x1, y1, _ = boxes[bi]
                inputs[(fi, bi)] = _variants(frames[fi][1][y0:y1, x0:x1])
            reads[(fi, bi, vi)] = rec(inputs[(fi, bi)][vi])
        return reads[(fi, bi, vi)]

    # 第一輪：兩張畫面、兩種輸入，讀所有候選列，留下讀得出時間／座標的列
    probe = sorted({0, len(frames) // 2})[:PROBE_FRAMES] if frames else []
    useful = [bi for bi in range(len(boxes)) if any(_useful(read(fi, bi, vi)) for fi in probe for vi in (0, 1))]
    result["lines"] = len(useful)
    texts = []
    if useful:
        # 第二輪：每張畫面 × 每種輸入 = 一票；同一票內各列依畫面位置（由上而下、由左而右）串起來
        order = sorted(useful, key=lambda bi: (boxes[bi][1] // max(1, boxes[bi][4]), boxes[bi][0]))
        dates, starts, lats, lons = [], [], [], []
        for fi in range(len(frames)):
            for vi in range(N_VARIANTS):
                text = _normalize(" ".join(read(fi, bi, vi) for bi in order))
                if not text.strip():
                    continue
                texts.append(text)
                dates += parse_date_parts(text)[:1]
                for s in parse_times(text)[:1]:
                    starts.append(s - frames[fi][0])
                lat, lon = parse_coords(text)
                if lat is not None:
                    lats.append(lat)
                if lon is not None:
                    lons.append(lon)

        need = 2  # 至少兩票一致才採用
        day, n_day = vote_date(dates, need)
        group = _winner(starts, 2.0, lambda a, b: abs(a - b), need)
        if day and group:
            # 浮水印的秒數是無條件捨去：畫面顯示 S 秒、位在影片第 t 秒，代表開頭落在 [S−t, S−t+1)。
            # 各票的區間取交集再取中點；交集是空的（讀錯或行車紀錄器的秒跳得不平均）就用中位數 + 0.5。
            # 回報時也無條件捨去到秒，跟影片第一張畫面的浮水印顯示一致。
            low, high = max(group), min(group) + 1
            sec = (low + high) / 2 if low < high else statistics.median(group) + 0.5
            start = datetime.combine(day, datetime.min.time()) + timedelta(seconds=math.floor(sec))
            result["recorded_at"] = start.isoformat(timespec="seconds")
            result["date_votes"], result["time_votes"] = n_day, len(group)
        # 幾秒內車子最多移動一兩百公尺；0.003 度約 300 公尺
        need_coord = max(need, math.ceil(COORD_SUPPORT * len(texts)))
        g_lat = _winner(lats, 0.003, lambda a, b: abs(a - b), need_coord)
        g_lon = _winner(lons, 0.003, lambda a, b: abs(a - b), need_coord)
        if g_lat and g_lon:
            result["lat"] = round(statistics.median(g_lat), 6)
            result["lon"] = round(statistics.median(g_lon), 6)
            result["coord_votes"] = min(len(g_lat), len(g_lon))
    if debug:
        result["texts"] = texts
        result["boxes"] = [boxes[i] for i in useful]
    result["elapsed"] = round(time.time() - t0, 2)
    return result


if __name__ == "__main__":  # 單獨測試：python ui/core/overlay_scan.py 影片.mp4 [影片2 ...] [--debug]
    import json
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for video in (a for a in sys.argv[1:] if a != "--debug"):
        res = scan(video, debug="--debug" in sys.argv)
        print(os.path.basename(video), json.dumps(res, ensure_ascii=False, default=str))
