"""串流降頻篩選（4bs）：平時每 n 幀分析 1 幀，規則出現候選事件時，把事件前後的時段用原幀率重新深度分析。

- 篩選：同一套模型與規則，只是每 n 幀取 1 幀、時間軸換成 fps/n（幀號是連續的取樣序號），候選事件只當觸發。
- 深度分析：觸發時間〔前 before 秒, 後 after 秒〕合併成影格區間，從收錄分段重讀，用全新的後處理與規則狀態、
  原幀率逐幀分析；最終事件、逐幀紀錄與剪片都只來自深度分析（4bo：前 8 後 6 秒可重現整片事件）。
- 觸發可能晚於事件時間（延後確認、停留判定），區間結束後再等 hold 秒才分析，讓接續的觸發併進同一段。
"""
import math

HOLD_SECONDS = 10.0  # 區間結束後再等幾秒才深度分析（觸發可能晚於事件時間）


class DeepWindows:
    """篩選觸發 → 待深度分析的影格區間。區間依序分析、不重疊；已分析過的影格不重做。"""

    def __init__(self, fps, before=8.0, after=6.0, hold=HOLD_SECONDS):
        for name, value in (("before", before), ("after", after), ("hold", hold)):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and >= 0")
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError("fps must be positive")
        self.fps, self.before, self.after, self.hold = fps, before, after, hold
        self.pending = []      # [[start, end]]，依序、不重疊
        self.done = -1         # 已深度分析到的最後一幀
        self.completed = []
        self.triggers = 0
        self.skipped = 0       # 觸發落在已分析過的區間內

    def add(self, timestamp):
        self.triggers += 1
        start = max(0, math.ceil((timestamp - self.before) * self.fps - 1e-9), self.done + 1)
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
