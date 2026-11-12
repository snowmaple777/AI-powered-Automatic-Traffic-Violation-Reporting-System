"""近場可見車道邊界；只提供描述幾何，不提供違規確認證據或全路口拓樸。"""
import cv2
import numpy as np

from core.geometry import project_points
from observation_schema import marking_evidence
from road_marking_config import classes


BOUNDARY_IDS = tuple(classes.index(name) for name in (
    "solid single white", "solid single yellow", "solid double white",
    "solid double yellow", "dashed single white", "dashed single yellow"))


def extract_boundaries(mask, width, height):
    """逐連通塊取列中心線；不跨越遮擋補線，不把停止線／箭頭當車道邊界。

    縮至最寬 640 px，短虛線可能不支援車道判斷，此時保守回傳未知。
    """
    if mask is None:
        return []
    if mask.ndim != 2 or mask.shape != (height, width):
        raise ValueError("局部車道遮罩尺寸必須與原圖相同")
    scale = min(1., 640 / width)
    small = cv2.resize(mask, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_NEAREST)
    sy, sx = height / small.shape[0], width / small.shape[1]
    result = []
    for cid in BOUNDARY_IDS:
        binary = (small == cid).astype(np.uint8)
        binary[:round(small.shape[0] * .3)] = 0
        count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
        for component in range(1, count):
            x, y, w, h, area = stats[component]
            if h < max(8, small.shape[0] * .07) or area < 10:
                continue
            points = []
            for row in np.linspace(y, y + h - 1, min(16, h)).astype(int):
                xs = np.flatnonzero(labels[row, x:x+w] == component) + x
                if len(xs) and xs[-1] - xs[0] < max(12, small.shape[1] * .06):
                    points.append([float(np.median(xs) * sx), float(row * sy)])
            if len(points) >= 5 and points[-1][1] - points[0][1] >= height * .06:
                result.append(dict(class_name=classes[cid], points=points))
    return sorted(result, key=lambda b: (b["points"][-1][0], b["class_name"]))[:64]


def _x_at(boundary, y):
    points = np.asarray(boundary["points"], dtype=float)
    if np.any(np.diff(points[:, 1]) <= 0) or not points[0, 1] <= y <= points[-1, 1]:
        return None
    return float(np.interp(y, points[:, 1], points[:, 0]))


def local_lane(boundaries, box, width, height):
    """以車框底點尋找兩側可見邊界；回傳局部四邊形與朝畫面遠方的中心軸。

    此軸沒有道路法定行向；不能以它判定逆向。沒有兩側實際支援就回傳 None。
    """
    x, y = (box[0] + box[2]) / 2, box[3]
    far_y = y - max(height * .08, (box[3] - box[1]) * .4)
    candidates = []
    for b in boundaries:
        near, far = _x_at(b, y), _x_at(b, far_y)
        if near is not None and far is not None:
            candidates.append((near, far, b))
    left = sorted((b for b in candidates if b[0] < x), key=lambda b: -b[0])
    right = sorted((b for b in candidates if b[0] > x), key=lambda b: b[0])
    if not left or not right:
        return None
    l, r = left[0], right[0]
    near_w, far_w = r[0] - l[0], r[1] - l[1]
    if not max(width * .035, (box[2] - box[0]) * 1.1) <= near_w <= width * .75:
        return None
    if not .3 * near_w <= far_w <= 1.15 * near_w:
        return None
    # 明顯彎曲／斷裂的可見邊界不拿直線軸解釋轉彎。
    mid_y = (y + far_y) / 2
    for b, near, far in ((l[2], l[0], l[1]), (r[2], r[0], r[1])):
        mid = _x_at(b, mid_y)
        if mid is None or abs(mid - (near + far) / 2) > .08 * near_w:
            return None
    return dict(polygon=[[l[0], y], [r[0], y], [r[1], far_y], [l[1], far_y]],
                axis=[[(l[0]+r[0])/2, y], [(l[1]+r[1])/2, far_y]],
                width_px=near_w, boundary_classes=[l[2]["class_name"], r[2]["class_name"]],
                meaning="local_visible_corridor_not_legal_direction")


class LocalLanes:
    def __init__(self):
        self.boundaries = []
        self.source_frame = None
        self.source_time = None

    def update(self, record, mask, matrix, continuous):
        f, source = record["frame"], marking_evidence(record)
        direct = source["direct"] and not source["legacy"]
        if direct:
            self.boundaries = extract_boundaries(mask, f["width"], f["height"])
            self.source_frame, self.source_time = f["index"], f["timestamp_sec"]
            reason = "direct" if mask is not None else "missing_mask"
        elif (continuous and matrix is not None and source["aligned"] and self.source_time is not None
              and f["timestamp_sec"] - self.source_time <= .5):
            try:
                self.boundaries = [dict(b, points=project_points(b["points"], matrix)) for b in self.boundaries]
                reason = "propagated"
            except ValueError:
                self.boundaries = []
                reason = "projection_invalid"
        else:
            self.boundaries = []
            reason = "missing_provenance" if source["legacy"] else "unaligned_or_expired"
        return dict(boundaries=self.boundaries, geometry_source=reason,
                    source_frame=self.source_frame, direct=direct and mask is not None)


def draw_local_motion(frame, diagnostic):
    """原圖座標疊圖；英文短標籤避免 OpenCV 中文字形問題。"""
    for b in diagnostic.get("boundaries", []):
        cv2.polylines(frame, [np.rint(b["points"]).astype(np.int32)], False, (255, 230, 0), 2)
    for v in diagnostic.get("vehicles", []):
        lane = v.get("lane")
        if lane:
            cv2.polylines(frame, [np.rint(lane["polygon"]).astype(np.int32)], True, (0, 220, 180), 1)
        if v.get("motion_vector"):
            a, b = np.rint(v["motion_vector"]).astype(int)
            cv2.arrowedLine(frame, tuple(a), tuple(b), (255, 80, 255), 2, tipLength=.25)
    return frame
