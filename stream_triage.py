"""串流降頻篩選（4bs）：平時每 n 幀分析 1 幀，規則出現候選事件時，把事件前後的時段用原幀率重新深度分析。

- 篩選：同一套模型與規則，只是每 n 幀取 1 幀、時間軸換成 fps/n（幀號是連續的取樣序號），候選事件只當觸發。
- 深度分析：觸發時間〔前 before 秒, 後 after 秒〕合併成影格區間，從收錄分段重讀，用全新的後處理與規則狀態、
  原幀率逐幀分析；最終事件、逐幀紀錄與剪片都只來自深度分析（4bo：前 8 後 6 秒可重現整片事件）。
- 觸發可能晚於事件時間（延後確認、停留判定），區間結束後再等 hold 秒才分析，讓接續的觸發併進同一段。
- 4bt：規則保留到片尾或數秒後才輸出的候選（闖紅燈疑似事件、停等區延後確認與合併、雙線停留）在出現當下就觸發
  （held_events）；區間從觸發車輛在篩選中第一次出現的前 1 秒開始（最多往前 max_before 秒），讓追蹤與規則狀態有前文；
  篩選略過不影響規則的模型（TRIAGE_SKIP_STAGES：深度只供車牌、車牌、OCR）。
"""
import math

HOLD_SECONDS = 10.0  # 區間結束後再等幾秒才深度分析（觸發可能晚於事件時間）
MAX_BEFORE_SECONDS = 20.0  # 依觸發車輛首次出現往前延伸時，最多從觸發前幾秒開始
FIRST_SEEN_LEAD_SECONDS = 1.0  # 從車輛首次出現再往前幾秒開始（追蹤與燈號狀態暖機）
TRIAGE_SKIP_STAGES = ("depth", "plates", "ocr")  # 篩選不跑的模型：規則不讀距離，車牌只用於停等區同車銜接


def held_events(engine):
    """規則與引擎目前保留、尚未輸出的候選事件（篩選把它們當觸發；不改動規則狀態）。"""
    held = []
    for rule in engine.rules:
        held.extend(getattr(rule, "pending_events", {}).values())   # 闖紅燈疑似事件：片尾才輸出
        held.extend(getattr(rule, "pending_confirmations", []))    # 區域規則延後確認：最多 3 秒
        held.extend(getattr(rule, "dwell_pending", []))            # 雙線停留：同幀稍後輸出
    held.extend(event for _, event in getattr(engine, "pending", []))  # 停等區等待併入闖紅燈
    return held


class DeepWindows:
    """篩選觸發 → 待深度分析的影格區間。區間依序分析、不重疊；已分析過的影格不重做。"""

    def __init__(self, fps, before=8.0, after=6.0, hold=HOLD_SECONDS, max_before=MAX_BEFORE_SECONDS):
        for name, value in (("before", before), ("after", after), ("hold", hold), ("max_before", max_before)):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and >= 0")
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError("fps must be positive")
        self.fps, self.before, self.after, self.hold = fps, before, after, hold
        self.max_before = max(before, max_before)
        self.pending = []      # [[start, end]]，依序、不重疊
        self.done = -1         # 已深度分析到的最後一幀
        self.completed = []
        self.triggers = 0
        self.skipped = 0       # 觸發落在已分析過的區間內

    def add(self, timestamp, first_seen=None):
        """first_seen：觸發車輛在篩選中第一次出現的時間；比 before 更早時從它的前 1 秒開始（最多 max_before 秒）。"""
        self.triggers += 1
        begin = timestamp - self.before
        if first_seen is not None:
            begin = min(begin, max(first_seen - FIRST_SEEN_LEAD_SECONDS, timestamp - self.max_before))
        start = max(0, math.ceil(begin * self.fps - 1e-9), self.done + 1)
        end = math.floor((timestamp + self.after) * self.fps + 1e-9)
        if end < start:
            self.skipped += 1
            return
        merged = []
        for a, b in sorted(self.pending + [[start, end]]):
            if merged and a <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        self.pending = merged

    def due(self, position, final=False):
        """position＝目前讀到的影格數（0..position-1 都已收錄）。回傳可以開始分析的區間（會從待辦移除）。
        final：串流結束，剩下的區間截到最後一幀全部回傳。"""
        out = []
        while self.pending:
            a, b = self.pending[0]
            if not final and b + self.hold * self.fps >= position:
                break
            self.pending.pop(0)
            b = min(b, position - 1)
            if a <= b:
                out.append((a, b))
        return out

    def finish(self, start, end):
        self.done = max(self.done, end)
        self.completed.append([start, end])

    def frames(self):
        return sum(b - a + 1 for a, b in self.completed)
