"""停止線：背景運動補償 → 片段合併 → 配對／平滑 → 遮擋記憶 → 品質閘門。"""
from copy import deepcopy
from dataclasses import asdict, dataclass
import math
import cv2
import numpy as np
from .contracts import CompensationConfig, CompensationResult, MotionEstimate
from .geometry import angle, bounded_component, line_y, merge_fragments, occlusion_ratio, project
from .motion import RoadMotionEstimator, road_mask


@dataclass
class _Track:
    identity: str
    class_id: int
    component: dict
    observations: int
    last_observed_frame: int
    last_observed_time: float
    support: float
    uncertainty: float


class RoadMarkingCompensator:
    def __init__(self, config=None, motion_estimator=None):
        self.config=config or CompensationConfig()
        # 依賴注入讓鏡頭運動演算法能替換，也能使用確定性的運動值做測試。
        self.motion_estimator=motion_estimator or RoadMotionEstimator(self.config)
        self.reset()

    def reset(self):
        """每支新影片必須 reset；影格倒退、尺寸改變或不連續也會自動清除狀態。"""
        self.tracks={}
        self.sequence=0
        self.previous_gray=None
        self.previous_mask=None
        self.previous_index=None
        self.previous_time=None

    def process(self, frame, *, frame_index, timestamp_sec, road_markings, vehicles):
        """輸入：原圖 BGR、索引／秒、原始標線與車框；回傳獨立 CompensationResult。

        不載入模型，不執行違規規則，不修改傳入 frame、road_markings 或 vehicles。
        """
        if frame.ndim!=3 or frame.shape[2]!=3 or frame.dtype!=np.uint8:
            raise ValueError("frame must be uint8 BGR HxWx3")
        if frame_index<0 or not math.isfinite(timestamp_sec) or timestamp_sec<0:
            raise ValueError("frame_index and timestamp_sec must be nonnegative")
        cfg=self.config
        h,w=frame.shape[:2]
        raw=deepcopy(road_markings)
        gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
        mask=road_mask(frame.shape,vehicles,cfg.roi_top)
        reset_reason=None
        if self.previous_gray is not None and (
                self.previous_gray.shape!=gray.shape or frame_index!=self.previous_index+1
                or not 0<timestamp_sec-self.previous_time<=cfg.max_frame_gap_seconds):
            self.reset()
            reset_reason="discontinuous_input"
        motion=MotionEstimate(False,reason="first_frame")
        expired=[]
        if self.previous_gray is not None:
            try:
                motion=self.motion_estimator.estimate(self.previous_gray,gray,self.previous_mask,mask)
            except cv2.error:
                motion=MotionEstimate(False,reason="opencv_motion_error")
            # 鏡頭配準失敗時不使用 identity transform 猜位置，也不延續舊可信度。
            if not motion.valid:
                expired=[{"id":t.identity,"reason":motion.reason} for t in self.tracks.values()]
                self.tracks={}
                reset_reason=motion.reason

        projected={}
        if motion.valid:
            for key,track in self.tracks.items():
                component=project(track.component,motion.matrix,w,h,cfg)
                if component is not None:
                    projected[key]=component
                else:
                    expired.append({"id":key,"reason":"outside_frame_or_invalid_geometry"})
        self.tracks={k:t for k,t in self.tracks.items() if k in projected}

        # 僅處理設定的停止線類別；其他標線原樣傳遞，避免擴大此次變更範圍。
        observations=[]
        rejected=0
        for marking in raw:
            if marking["class_name"]!=cfg.target_class:
                continue
            valid=[]
            for component in marking["components"]:
                bounded=bounded_component(component,w,h,cfg)
                if bounded is not None:
                    valid.append(bounded)
                else:
                    rejected+=1
            for component in merge_fragments(valid,vehicles,w,h,cfg):
                observations.append((marking["class_id"],component))

        unused=set(self.tracks)
        outputs=[]
        for class_id,current in observations:
            best=None
            distance_limit=h*cfg.match_distance_ratio
            best_distance=distance_limit
            for key in sorted(unused):
                previous=projected[key]
                if self.tracks[key].class_id!=class_id or abs(angle(previous)-angle(current))>10:
                    continue
                a,b=previous["line_xyxy"],current["line_xyxy"]
                overlap=min(a[2],b[2])-max(a[0],b[0])
                if overlap<=0:
                    continue
                x=(max(a[0],b[0])+min(a[2],b[2]))/2
                distance=abs(line_y(previous,x)-line_y(current,x))
                if distance<best_distance:
                    best,best_distance=key,distance
            if best is None:
                self.sequence+=1
                key=f"comp-stop-{self.sequence:06d}"
                track=_Track(key,class_id,current,1,frame_index,timestamp_sec,0.,2.)
            else:
                unused.remove(best)
                key=best
                track=self.tracks[key]
                previous=projected[key]
                # 先投影再平滑，並保持當幀觀測的 x 範圍，不憑空擴大標線支撐區。
                line=current["line_xyxy"]
                for i in (0,2):
                    line[i+1]=cfg.smoothing_alpha*line[i+1]+(1-cfg.smoothing_alpha)*line_y(previous,line[i])
                current["centroid"]=[(line[0]+line[2])/2,(line[1]+line[3])/2]
                track.observations+=1
                track.uncertainty=2.+(1-cfg.smoothing_alpha)*best_distance+motion.reprojection_error_px
            track.component=current
            track.last_observed_frame=frame_index
            track.last_observed_time=timestamp_sec
            # support_score 是幾何／時序可靠度啟發值，並非模型 softmax 機率。
            track.support=.95*min(1.,track.observations/cfg.min_observations)
            self.tracks[key]=track
            outputs.append(self._annotate(track,"observed",timestamp_sec,vehicles,w,h))

        for key in unused:
            track=self.tracks[key]
            track.component=projected[key]
            age=timestamp_sec-track.last_observed_time
            occluded=occlusion_ratio(track.component,vehicles,w,h)>=cfg.min_occlusion_ratio
            ttl=cfg.max_occluded_seconds if occluded else cfg.max_missing_seconds
            if track.observations<cfg.min_observations or age>ttl:
                expired.append({"id":key,"reason":"expired" if age>ttl else "unconfirmed_history"})
                del self.tracks[key]
                continue
            track.uncertainty+=motion.reprojection_error_px+max(.5,h*.0005)
            outputs.append(self._annotate(track,"predicted_occluded" if occluded else "predicted_missing",timestamp_sec,vehicles,w,h))

        compensated=[deepcopy(m) for m in raw if m["class_name"]!=cfg.target_class]
        for class_id in sorted({c[0] for c in outputs}):
            components=[c for cid,c in outputs if cid==class_id]
            compensated.append({"class_id":class_id,"class_name":cfg.target_class,
                                "pixels":sum(c["area"] for c in components),"components":components})
        rule_markings=[]
        for marking in compensated:
            item=deepcopy(marking)
            if item["class_name"]==cfg.target_class:
                item["components"]=[c for c in item["components"] if c["compensation"]["rule_eligible"]]
                item["pixels"]=sum(c["area"] for c in item["components"])
                if not item["components"]:
                    continue
            rule_markings.append(item)
        usable=sum(c["compensation"]["rule_eligible"] for _,c in outputs)
        diagnostics={"enabled":True,"version":1,"motion":motion.to_dict(),
                     "status":"usable" if usable else "unknown", "eligible_stop_lines":usable,
                     "rejected_components":rejected,"expired_tracks":expired,
                     "reset_reason":reset_reason,"config":asdict(cfg)}
        self.previous_gray,self.previous_mask=gray,mask
        self.previous_index,self.previous_time=frame_index,timestamp_sec
        return CompensationResult(raw,compensated,rule_markings,diagnostics)

    def _annotate(self,track,source,timestamp,vehicles,width,height):
        age=max(0.,timestamp-track.last_observed_time)
        score=track.support*math.exp(-math.log(2)*age/self.config.confidence_half_life)
        component=deepcopy(track.component)
        component["compensation"]={
            "id":track.identity,"source":source,"support_score":round(score,5),
            "uncertainty_px":round(track.uncertainty,3),"observed_frames":track.observations,
            "last_observed_frame":track.last_observed_frame,"age_seconds":round(age,5),
            "occlusion_ratio":round(occlusion_ratio(component,vehicles,width,height),4),
            "rule_eligible":bool(track.observations>=self.config.min_observations
                                 and score>=self.config.min_rule_support
                                 and track.uncertainty<=height*self.config.max_uncertainty_ratio)}
        return track.class_id,component
