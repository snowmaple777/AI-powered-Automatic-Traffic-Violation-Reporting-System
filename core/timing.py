"""輕量分段計時：累計各處理階段的總耗時與次數，用於找出非模型時間的瓶頸。"""
import time
from contextlib import contextmanager


class StageTimer:
    def __init__(self):
        self.seconds, self.counts = {}, {}

    @contextmanager
    def measure(self, stage):
        start = time.perf_counter()
        try:
            yield
        finally:
            self.seconds[stage] = self.seconds.get(stage, 0.0) + time.perf_counter() - start
            self.counts[stage] = self.counts.get(stage, 0) + 1

    def summary(self, frames):
        """每幀平均毫秒（依總耗時排序）。"""
        frames = max(1, frames)
        return {stage: round(1000 * total / frames, 2)
                for stage, total in sorted(self.seconds.items(), key=lambda item: -item[1])}
