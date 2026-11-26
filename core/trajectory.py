"""車輛影像軌跡（只記新偵測，不記快取框），供規則判斷橫向移動。座標為原圖像素。"""
from collections import deque


class TrackHistory:
    def __init__(self, seconds=3.0):
        self.seconds = seconds
        self.tracks = {}

    def update(self, now, index, vehicles, box_of=None):
        """vehicles：本幀規則用的車輛；box_of(vehicle) 回傳車框（預設 bbox_xyxy）。"""
        for vehicle in vehicles:
            identity = vehicle.get("object_id")
            if not identity or vehicle.get("source_frame", index) != index:
                continue
            box = box_of(vehicle) if box_of else vehicle.get("bbox_xyxy")
            if not box or box[2] <= box[0] or box[3] <= box[1]:
                continue
            history = self.tracks.setdefault(identity, deque())
            history.append((now, (box[0] + box[2]) / 2, box[3], box[2] - box[0], box[3] - box[1]))
        for identity in list(self.tracks):
            history = self.tracks[identity]
            while history and now - history[0][0] > self.seconds:
                history.popleft()
            if not history:
                del self.tracks[identity]

    def project(self, transform):
        """鏡頭運動補償：把歷史點以 transform(x, y) -> (x, y)（前幀→當幀）移到當幀座標。
        車寬沿用原值（只用於比例門檻）。transform 失敗（ValueError／OverflowError）時清除該車歷史。"""
        for identity in list(self.tracks):
            try:
                self.tracks[identity] = deque((t, *transform(x, y), w, h) for t, x, y, w, h in self.tracks[identity])
            except (ValueError, OverflowError):
                del self.tracks[identity]

    def displacement(self, identity, now, window):
        """window 秒內最早與最新觀測的差：dx（右為正）、dy（車框底邊，向下為正）、起點車寬。"""
        points = [p for p in self.tracks.get(identity, ()) if now - p[0] <= window + 1e-6]
        if len(points) < 2:
            return None
        first, last = points[0], points[-1]
        return {"dx": last[1] - first[1], "dy": last[2] - first[2], "width": first[3],
                "seconds": last[0] - first[0], "samples": len(points)}

    def lateral(self, identity, now, window, ratio=0.5):
        """橫向移動：水平位移至少起點車寬 × ratio，且至少是底邊垂直位移的 2 倍（橫越或轉彎的車）。"""
        d = self.displacement(identity, now, window)
        return bool(d and abs(d["dx"]) >= ratio * d["width"] and abs(d["dx"]) >= 2 * abs(d["dy"]))
