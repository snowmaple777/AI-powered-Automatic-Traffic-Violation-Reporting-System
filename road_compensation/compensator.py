"""停止線：背景運動補償 → 片段合併 → 配對／平滑 → 遮擋記憶 → 品質閘門。"""
from core.fastcopy import clone
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
    # 只計模型直接觀測（標線降頻時推移遮罩的配對不計入），供「確認」條件使用。
    direct_observations: int = 0
    last_direct_frame: int | None = None
    last_direct_time: float | None = None


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
        self.previous_shape=None
        self.last_valid_motion_time=None  # 最近一次鏡頭運動有效（或影片開頭）的時間，計算失效持續秒數

    def process(self, frame, *, frame_index, timestamp_sec, road_markings, vehicles, motion=None,
                direct_observation=True):
        """輸入：原圖 BGR、索引／秒、原始標線與車框；回傳獨立 CompensationResult。

        motion 為 EgoMotionTracker 對同一幀的估計；提供時直接使用，不重複估計
        （兩者重置條件相同，結果與自行估計完全一致）。
        direct_observation=False：本幀標線是推移前幀遮罩得到的估計（標線降頻）。仍用於幾何對齊與
        既有的 observations／rule_eligible（行為不變），但不增加 direct_observations，
        因此 direct_rule_eligible（確認條件使用）只依模型直接觀測累積。
        不載入模型，不執行違規規則，不修改傳入 frame、road_markings 或 vehicles。
        """
        external_motion = motion
        if frame.ndim!=3 or frame.shape[2]!=3 or frame.dtype!=np.uint8:
            raise ValueError("frame must be uint8 BGR HxWx3")
        if frame_index<0 or not math.isfinite(timestamp_sec) or timestamp_sec<0:
            raise ValueError("frame_index and timestamp_sec must be nonnegative")
        cfg=self.config
        h,w=frame.shape[:2]
        raw=clone(road_markings)
        gray=mask=None
        if external_motion is None:
            gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
            mask=road_mask(frame.shape,vehicles,cfg.roi_top,cfg.roi_bottom)
        reset_reason=None
        if self.previous_index is not None and (
                self.previous_shape!=frame.shape[:2] or frame_index!=self.previous_index+1
                or not 0<timestamp_sec-self.previous_time<=cfg.max_frame_gap_seconds):
            self.reset()
            reset_reason="discontinuous_input"
        motion=MotionEstimate(False,reason="first_frame")
        expired=[]
        motion_gap=False
        motion_gap_seconds=0.
        if self.previous_index is not None:
            if external_motion is not None:
                motion=external_motion
            else:
                try:
                    motion=self.motion_estimator.estimate(self.previous_gray,gray,self.previous_mask,mask)
                except cv2.error:
                    motion=MotionEstimate(False,reason="opencv_motion_error")
            if not motion.valid:
                start=self.last_valid_motion_time if self.last_valid_motion_time is not None else self.previous_time
                motion_gap_seconds=timestamp_sec-start
                if cfg.allow_motion_gaps and motion_gap_seconds<=cfg.max_motion_gap_seconds:
                    # 短暫失敗：以「鏡頭不動」近似保留軌跡；未被觀測更新的線不提供給規則，
                    # 被觀測更新的線標記 motion_gap（規則只能據以產生疑似）。
                    motion_gap=True
                else:
                    # 鏡頭配準失敗過久（或未啟用容忍）：不使用 identity transform 猜位置，也不延續舊可信度。
                    expired=[{"id":t.identity,"reason":motion.reason} for t in self.tracks.values()]
                    self.tracks={}
                    reset_reason=motion.reason
        if motion.valid or self.previous_index is None:
            self.last_valid_motion_time=timestamp_sec

        projected={}
        if motion_gap:
            projected={key:clone(track.component) for key,track in self.tracks.items()}
        elif motion.valid:
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
                track.direct_observations=0
            else:
                unused.remove(best)
                key=best
                track=self.tracks[key]
                previous=projected[key]
                # 先投影再平滑，並保持當幀觀測的 x 範圍，不憑空擴大標線支撐區。
                # 運動失效期間舊位置未經投影，不拿來平滑，直接採用當幀觀測。
                line=current["line_xyxy"]
                if not motion_gap:
                    for i in (0,2):
                        line[i+1]=cfg.smoothing_alpha*line[i+1]+(1-cfg.smoothing_alpha)*line_y(previous,line[i])
                current["centroid"]=[(line[0]+line[2])/2,(line[1]+line[3])/2]
                track.observations+=1
                track.uncertainty=2.+(1-cfg.smoothing_alpha)*best_distance+motion.reprojection_error_px
            track.component=current
            track.last_observed_frame=frame_index
            track.last_observed_time=timestamp_sec
            if direct_observation:
                track.direct_observations+=1
                track.last_direct_frame,track.last_direct_time=frame_index,timestamp_sec
            # support_score 是幾何／時序可靠度啟發值，並非模型 softmax 機率。
            track.support=.95*min(1.,track.observations/cfg.min_observations)
            self.tracks[key]=track
            outputs.append(self._annotate(track,"observed",timestamp_sec,vehicles,w,h,motion_gap=motion_gap))

        for key in sorted(unused):  # 集合走訪順序隨字串雜湊種子而變，必須排序才可重現
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
            source="predicted_occluded" if occluded else "predicted_missing"
            outputs.append(self._annotate(track,source,timestamp_sec,vehicles,w,h,
                                          motion_gap=motion_gap,predicted_in_gap=motion_gap))

        compensated=[clone(m) for m in raw if m["class_name"]!=cfg.target_class]
        for class_id in sorted({c[0] for c in outputs}):
            components=[c for cid,c in outputs if cid==class_id]
            compensated.append({"class_id":class_id,"class_name":cfg.target_class,
                                "pixels":sum(c["area"] for c in components),"components":components})
        rule_markings=[]
        for marking in compensated:
            item=clone(marking)
            if item["class_name"]==cfg.target_class:
                item["components"]=[c for c in item["components"] if c["compensation"]["rule_eligible"]]
                item["pixels"]=sum(c["area"] for c in item["components"])
                if not item["components"]:
                    continue
            rule_markings.append(item)
        usable=sum(c["compensation"]["rule_eligible"] for _,c in outputs)
        direct_usable=sum(c["compensation"]["direct_rule_eligible"] for _,c in outputs)
        diagnostics={"enabled":True,"version":1,"motion":motion.to_dict(),
                     "status":"usable" if usable else "unknown", "eligible_stop_lines":usable,
                     "direct_observation":bool(direct_observation),"direct_eligible_stop_lines":direct_usable,
                     "rejected_components":rejected,"expired_tracks":expired,
                     "reset_reason":reset_reason,"motion_gap":motion_gap,
                     "motion_gap_seconds":round(motion_gap_seconds,5) if motion_gap else 0.,
                     "config":asdict(cfg)}
        self.previous_gray,self.previous_mask=gray,mask
        self.previous_index,self.previous_time=frame_index,timestamp_sec
        self.previous_shape=frame.shape[:2]
        return CompensationResult(raw,compensated,rule_markings,diagnostics)

    def _annotate(self,track,source,timestamp,vehicles,width,height,motion_gap=False,predicted_in_gap=False):
        age=max(0.,timestamp-track.last_observed_time)
        score=track.support*math.exp(-math.log(2)*age/self.config.confidence_half_life)
        # 「只計直接觀測」的同一套可靠度：次數與年齡都以模型直接觀測為準。
        direct_eligible=False
        direct_age=None
        if track.last_direct_time is not None:
            direct_age=max(0.,timestamp-track.last_direct_time)
            direct_score=(.95*min(1.,track.direct_observations/self.config.min_observations)
                          *math.exp(-math.log(2)*direct_age/self.config.confidence_half_life))
            direct_eligible=bool(track.direct_observations>=self.config.min_observations
                                 and direct_score>=self.config.min_rule_support
                                 and track.uncertainty<=height*self.config.max_uncertainty_ratio)
        component=clone(track.component)
        component["compensation"]={
            "id":track.identity,"source":source,"support_score":round(score,5),
            "uncertainty_px":round(track.uncertainty,3),"observed_frames":track.observations,
            "last_observed_frame":track.last_observed_frame,"age_seconds":round(age,5),
            "occlusion_ratio":round(occlusion_ratio(component,vehicles,width,height),4),
            "rule_eligible":bool(track.observations>=self.config.min_observations
                                 and score>=self.config.min_rule_support
                                 and track.uncertainty<=height*self.config.max_uncertainty_ratio),
            "direct_observed_frames":track.direct_observations,
            "last_direct_observed_frame":track.last_direct_frame,
            "direct_age_seconds":None if direct_age is None else round(direct_age,5),
            "direct_rule_eligible":direct_eligible}
        if predicted_in_gap:
            # 運動失效期間未被觀測的線位置只是未投影的舊值：保留記憶但不給規則使用。
            component["compensation"].update(rule_eligible=False,direct_rule_eligible=False)
        if motion_gap:
            component["compensation"]["motion_gap"]=True
        return track.class_id,component
