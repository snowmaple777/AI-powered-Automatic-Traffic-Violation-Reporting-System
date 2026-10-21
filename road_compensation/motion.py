"""以道路背景光流估計鏡頭運動，排除前後幀車輛區域。"""
import cv2
import numpy as np
from .contracts import MotionEstimate


def road_mask(shape, vehicles, roi_top, roi_bottom=1.0):
    h, w = shape[:2]
    mask = np.zeros((h, w), np.uint8)
    mask[int(h * roi_top):int(h * roi_bottom)] = 255
    for vehicle in vehicles:
        x1, y1, x2, y2 = vehicle["bbox_xyxy"]
        dx, dy = (x2-x1)*.05, (y2-y1)*.05
        left, right = max(0, min(w, int(x1-dx))), max(0, min(w, int(x2+dx)))
        top, bottom = max(0, min(h, int(y1-dy))), max(0, min(h, int(y2+dy)))
        mask[top:bottom, left:right] = 0
    return mask


class RoadMotionEstimator:
    """可替換接口：estimate(prev_gray, gray, prev_mask, mask) -> MotionEstimate。"""
    def __init__(self, config):
        self.config = config
        # 前一幀縮圖快取：下一幀的 previous 就是這一幀的 current（同一個陣列），不必重新縮放。
        # 以物件身分比對並保留引用，避免 id 被重用。（OpenCV 的 Python 介面不接受預建光流金字塔。）
        self._cached_source = None
        self._cached = None

    def _prepare(self, image, size):
        if self._cached_source is image and self._cached[0] == size:
            return self._cached[1]
        return cv2.resize(image, size)

    def _remember(self, image, size, small):
        self._cached_source, self._cached = image, (size, small)

    @staticmethod
    def _median_uint8(values):
        """uint8 差值的精確中位數（與 np.median 相同結果），以 256 格直方圖取代排序。"""
        counts = np.bincount(values, minlength=256)
        cumulative = np.cumsum(counts)
        n = int(cumulative[-1])
        low = int(np.searchsorted(cumulative, (n - 1) // 2 + 1))
        high = int(np.searchsorted(cumulative, n // 2 + 1))
        return (low + high) / 2.0

    @staticmethod
    def _covers(points, size):
        """點至少落在 3x3 格中的 4 格，且橫跨 25% 寬、15% 高。"""
        if not len(points):
            return False
        grid = np.minimum(2, (points / np.asarray(size, dtype=points.dtype) * 3).astype(int))
        span = np.ptp(points, axis=0)
        return len(np.unique(grid, axis=0)) >= 4 and span[0] >= size[0]*.25 and span[1] >= size[1]*.15

    def estimate(self, previous, current, previous_mask, current_mask):
        cfg = self.config
        h, w = current.shape
        scale = min(1., cfg.motion_width / w)
        size = (max(1, round(w*scale)), max(1, round(h*scale)))
        a = self._prepare(previous, size)
        b = self._prepare(current, size)
        self._remember(current, size, b)
        ma, mb = [cv2.resize(x, size, interpolation=cv2.INTER_NEAREST) for x in (previous_mask, current_mask)]
        points = cv2.goodFeaturesToTrack(a, maxCorners=cfg.motion_max_corners,
                                       qualityLevel=.01, minDistance=cfg.motion_min_distance, mask=ma)
        if points is None or len(points) < cfg.min_motion_points:
            return MotionEstimate(False, reason="insufficient_background_features")
        moved, status, _ = cv2.calcOpticalFlowPyrLK(a, b, points, None, winSize=(21,21), maxLevel=3)
        if moved is None:
            return MotionEstimate(False, reason="optical_flow_failed")
        back, back_status, _ = cv2.calcOpticalFlowPyrLK(b, a, moved, None, winSize=(21,21), maxLevel=3)
        if back is None:
            return MotionEstimate(False, reason="backward_flow_failed")
        p, q = points.reshape(-1,2), moved.reshape(-1,2)
        good = status.ravel().astype(bool) & back_status.ravel().astype(bool)
        good &= np.isfinite(q).all(axis=1) & (np.linalg.norm(back.reshape(-1,2)-p,axis=1) <= cfg.max_fb_error)
        good &= (q[:,0]>=0)&(q[:,0]<size[0])&(q[:,1]>=0)&(q[:,1]<size[1])
        indices = np.flatnonzero(good)
        # q 已確認位於縮圖範圍內（非負），astype(int) 與 int() 同為向零截斷。
        cells_q = q[indices].astype(int)
        indices = indices[mb[cells_q[:, 1], cells_q[:, 0]] != 0]
        p, q = p[indices], q[indices]
        if len(p) < cfg.min_motion_points:
            return MotionEstimate(False, reason="insufficient_reliable_flow")
        # 靜止判定：背景光流幾乎為零時，單應矩陣只會擬合到雜訊（全片評估中光流 < 0.5 px 的幀，
        # 道路中心仍被推移達 8 px），直接採用單位矩陣。靜止點須與一般估計相同地分佈在道路區域，
        # 否則行車中只有消失點附近的特徵也會看似不動。仍做亮度檢查，避免把切鏡當成靜止。
        flow = np.linalg.norm(q - p, axis=1) / scale
        still = flow <= cfg.stationary_flow_px
        if (np.median(flow) <= cfg.stationary_flow_px * .5 and still.mean() >= cfg.min_inlier_ratio
                and self._covers(p[still], size)):
            identity = np.eye(3)
            photo = float(self._median_uint8(cv2.absdiff(previous, current)[(previous_mask > 0) & (current_mask > 0)]))
            if photo > cfg.max_photometric_error:
                return MotionEstimate(False, reason="photometric_mismatch", photometric_error=photo)
            return MotionEstimate(True, identity, "stationary", int(still.sum()), float(still.mean()),
                                  float(np.median(flow)), photo)
        matrix, support = cv2.findHomography(p, q, cv2.RANSAC, 3.0)
        if matrix is None or support is None or not np.isfinite(matrix).all():
            return MotionEstimate(False, reason="homography_failed")
        support = support.ravel().astype(bool)
        count, ratio = int(support.sum()), float(support.mean())
        if count < cfg.min_motion_points or ratio < cfg.min_inlier_ratio:
            return MotionEstimate(False, reason="low_inlier_support", inliers=count, inlier_ratio=ratio)

        # 特徵不能只集中在一個小角落，否則外推到停止線位置容易大幅偏移。
        if not self._covers(p[support], size):
            return MotionEstimate(False, reason="poor_spatial_coverage", inliers=count, inlier_ratio=ratio)
        predicted = cv2.perspectiveTransform(p[support,None,:],matrix).reshape(-1,2)
        error = float(np.median(np.linalg.norm(predicted-q[support],axis=1)))
        if error > cfg.max_reprojection_error:
            return MotionEstimate(False, reason="high_reprojection_error")
        scaling = np.diag([size[0]/w, size[1]/h, 1.])
        matrix = np.linalg.inv(scaling) @ matrix @ scaling
        bottom = min(h - 1, h * cfg.roi_bottom)
        corners = np.float32([[0,h*cfg.roi_top],[w-1,h*cfg.roi_top],[w-1,bottom],[0,bottom]])
        warped = cv2.perspectiveTransform(corners[None],matrix)[0]
        area_ratio = abs(cv2.contourArea(warped)) / max(1.,cv2.contourArea(corners))
        if (not np.isfinite(warped).all() or not cv2.isContourConvex(warped)
                or not .65 <= area_ratio <= 1.5
                or np.max(np.linalg.norm(warped-corners,axis=1)) > np.hypot(w,h)*.2):
            return MotionEstimate(False, reason="implausible_camera_motion")
        # 抖動閘門：道路中心的位移不能遠大於實際量到的光流，否則是雜訊撐出的縮放／透視。
        # 全片評估 31 支影片的移動幀中，此比值 p99 為 1.09。
        center = np.float32([[[w/2, h*(cfg.roi_top+bottom/h)/2]]])
        shift = float(np.linalg.norm(cv2.perspectiveTransform(center, matrix)[0, 0] - center[0, 0]))
        if shift > cfg.max_flow_extrapolation * max(float(np.percentile(flow, 90)), cfg.stationary_flow_px):
            return MotionEstimate(False, reason="inconsistent_with_flow", inliers=count, inlier_ratio=ratio)

        # 配準後亮度差用來拒絕切鏡／嚴重失配；不能只相信 RANSAC 的少數內點。
        aligned = cv2.warpPerspective(previous,matrix,(w,h))
        valid_mask = (cv2.warpPerspective(previous_mask,matrix,(w,h),flags=cv2.INTER_NEAREST)>0)&(current_mask>0)
        if valid_mask.sum() < w*h*.05:
            return MotionEstimate(False, reason="insufficient_visible_road")
        photo = float(self._median_uint8(cv2.absdiff(aligned, current)[valid_mask]))
        if photo > cfg.max_photometric_error:
            return MotionEstimate(False, reason="photometric_mismatch", photometric_error=photo)
        return MotionEstimate(True,matrix,"accepted",count,ratio,error/scale,photo)
