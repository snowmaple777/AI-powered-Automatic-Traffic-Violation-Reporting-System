"""All geometry is in original-frame pixels; input images are uint8 BGR."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class FrameContext:
    index: int
    timestamp_sec: float = 0.0
    # 可選：呼叫後回傳本幀的鏡頭運動估計（road_compensation.MotionEstimate；無法取得時 None）。
    # 會阻塞到背景估計完成，絕不回傳未完成的結果；只在模型確實需要時才呼叫（例如標線決定是否重新推論）。
    motion_probe: Optional[Callable[[], Any]] = field(default=None, compare=False, repr=False)


@dataclass
class ModelResult:
    records: list[dict] = field(default_factory=list)
    # Large arrays stay in memory and are never serialized into JSONL.
    artifacts: dict[str, Any] = field(default_factory=dict)


class PerceptionModel(ABC):
    @abstractmethod
    def infer(self, frame, context: FrameContext,
              upstream: dict[str, ModelResult]) -> ModelResult:
        """Return records/artifacts without modifying frame or upstream results."""

    def reset(self):
        """Clear video-specific tracking state before a new video."""

    def close(self):
        """Release owned resources."""
