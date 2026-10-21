"""影格連續性：所有跨幀狀態（追蹤、規則、區域）共用同一個判斷。"""


def is_continuous(previous, frame, max_gap_sec):
    """previous／frame 為觀測紀錄的 frame 欄位（index、timestamp_sec、width、height）。

    沒有前一幀時視為連續；下一個索引、時間差在 (0, max_gap_sec] 且尺寸不變才算連續。
    """
    if not previous:
        return True
    return (frame["index"] == previous["index"] + 1
            and 0 < frame["timestamp_sec"] - previous["timestamp_sec"] <= max_gap_sec
            and all(frame.get(key) == previous.get(key) for key in ("width", "height")))
