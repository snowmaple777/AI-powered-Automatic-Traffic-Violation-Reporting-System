"""以道路背景光流估計鏡頭運動，排除前後幀車輛區域。"""
import cv2
import numpy as np
from .contracts import MotionEstimate


def road_mask(shape, vehicles, roi_top):
    h, w = shape[:2]
    mask = np.zeros((h, w), np.uint8)
    mask[int(h * roi_top):] = 255
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

    def estimate(self, previous, current, previous_mask, current_mask):
        cfg = self.config
        h, w = current.shape
        scale = min(1., cfg.motion_width / w)
        size = (max(1, round(w*scale)), max(1, round(h*scale)))
        a, b = [cv2.resize(x, size) for x in (previous, current)]
        ma, mb = [cv2.resize(x, size, interpolation=cv2.INTER_NEAREST) for x in (previous_mask, current_mask)]
        points = cv2.goodFeaturesToTrack(a, maxCorners=600, qualityLevel=.01, minDistance=8, mask=ma)
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
        indices = [i for i in indices if mb[int(q[i,1]),int(q[i,0])] != 0]
        p, q = p[indices], q[indices]
        if len(p) < cfg.min_motion_points:
            return MotionEstimate(False, reason="insufficient_reliable_flow")
        matrix, support = cv2.findHomography(p, q, cv2.RANSAC, 3.0)
        if matrix is None or support is None or not np.isfinite(matrix).all():
            return MotionEstimate(False, reason="homography_failed")
        support = support.ravel().astype(bool)
        count, ratio = int(support.sum()), float(support.mean())
        if count < cfg.min_motion_points or ratio < cfg.min_inlier_ratio:
            return MotionEstimate(False, reason="low_inlier_support", inliers=count, inlier_ratio=ratio)

        # 特徵不能只集中在一個小角落，否則外推到停止線位置容易大幅偏移。
        cells = {(min(2,int(x/size[0]*3)), min(2,int(y/size[1]*3))) for x,y in p[support]}
        span = np.ptp(p[support], axis=0)
        if len(cells) < 4 or span[0] < size[0]*.25 or span[1] < size[1]*.15:
            return MotionEstimate(False, reason="poor_spatial_coverage", inliers=count, inlier_ratio=ratio)
        predicted = cv2.perspectiveTransform(p[support,None,:],matrix).reshape(-1,2)
        error = float(np.median(np.linalg.norm(predicted-q[support],axis=1)))
        if error > cfg.max_reprojection_error:
            return MotionEstimate(False, reason="high_reprojection_error")
        scaling = np.diag([size[0]/w, size[1]/h, 1.])
        matrix = np.linalg.inv(scaling) @ matrix @ scaling
        corners = np.float32([[0,h*cfg.roi_top],[w-1,h*cfg.roi_top],[w-1,h-1],[0,h-1]])
        warped = cv2.perspectiveTransform(corners[None],matrix)[0]
        area_ratio = abs(cv2.contourArea(warped)) / max(1.,cv2.contourArea(corners))
        if (not np.isfinite(warped).all() or not cv2.isContourConvex(warped)
                or not .65 <= area_ratio <= 1.5
                or np.max(np.linalg.norm(warped-corners,axis=1)) > np.hypot(w,h)*.2):
            return MotionEstimate(False, reason="implausible_camera_motion")

        # 配準後亮度差用來拒絕切鏡／嚴重失配；不能只相信 RANSAC 的少數內點。
        aligned = cv2.warpPerspective(previous,matrix,(w,h))
        valid_mask = (cv2.warpPerspective(previous_mask,matrix,(w,h),flags=cv2.INTER_NEAREST)>0)&(current_mask>0)
        if valid_mask.sum() < w*h*.05:
            return MotionEstimate(False, reason="insufficient_visible_road")
        photo = float(np.median(np.abs(aligned.astype(np.float32)-current)[valid_mask]))
        if photo > cfg.max_photometric_error:
            return MotionEstimate(False, reason="photometric_mismatch", photometric_error=photo)
        return MotionEstimate(True,matrix,"accepted",count,ratio,error/scale,photo)
