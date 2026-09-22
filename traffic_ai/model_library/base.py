"""All geometry is in original-frame pixels; input images are uint8 BGR."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FrameContext:
    index: int
    timestamp_sec: float = 0.0


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
