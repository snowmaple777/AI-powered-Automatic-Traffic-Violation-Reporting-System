"""Per-signal temporal evidence; missing detections are not treated as green."""
from collections import deque
import math


class TemporalSignalState:
    def __init__(self, fps, anchor_confidence=.30, support_confidence=.18,
                 window_seconds=.5, max_missing_seconds=.20, min_samples=3):
        self.window=max(3,round(fps*window_seconds))
        self.max_missing=max(1,round(fps*max_missing_seconds))
        self.anchor_confidence=anchor_confidence
        self.support_confidence=support_confidence
        self.min_samples=min_samples
        self.tracks={}
        self.sequence=0
        self.selected=None
        self.evidence={"state":"unknown","reason":"insufficient_signal_evidence"}

    def update(self, lights, index):
        self.tracks={k:t for k,t in self.tracks.items() if index-t["last"]<=self.window}
        used=set()
        transition=None
        for light in sorted(lights,key=lambda x:x["confidence"],reverse=True):
            if light["confidence"]<self.support_confidence:
                continue
            box=light.get("bbox_xyxy")
            center=None if box is None else ((box[0]+box[2])/2,(box[1]+box[3])/2)
            key=None
            best=float("inf")
            for candidate,track in self.tracks.items():
                if candidate in used:
                    continue
                old=track["center"]
                distance=0. if center is None and old is None else (
                    math.dist(center,old) if center is not None and old is not None else float("inf"))
                limit=max(20.,track["size"]*1.5)
                if distance<=limit and distance<best:
                    key,best=candidate,distance
            # 重疊偵測同一個燈不能在一幀內累積多票。
            if center is not None and any(self.tracks[k]["center"] is not None and
                    math.dist(center,self.tracks[k]["center"])<=max(10.,self.tracks[k]["size"]*.5) for k in used):
                continue
            if key is None:
                self.sequence+=1
                key=f"signal-{self.sequence:04d}"
                self.tracks[key]={"history":deque(),"center":center,"last":index,"size":20.}
            track=self.tracks[key]
            color=light["class_name"].lower()
            # 同一燈出現不同顏色立即切斷舊紅燈記憶；不以多數決吞掉轉綠證據。
            if track["history"] and track["history"][-1][1]!=color:
                track["history"].clear()
                if key==self.selected and color in {"green","yellow"}:
                    transition=(key,color)
            track["history"].append((index,color,float(light["confidence"])))
            track.update(center=center,last=index,size=max(box[2]-box[0],box[3]-box[1]) if box else 20.)
            used.add(key)
        if transition is not None:
            self.evidence={"state":transition[1],"signal_id":transition[0],
                           "reason":"observed_signal_change","last_observed_frame":index}
            return transition[1]
        stable=[]
        for key,track in self.tracks.items():
            while track["history"] and index-track["history"][0][0]>=self.window:
                track["history"].popleft()
            samples=list(track["history"])
            if (len(samples)>=self.min_samples and index-track["last"]<=self.max_missing
                    and max(s[2] for s in samples)>=self.anchor_confidence):
                stable.append((key,track,samples))
        if not stable:
            self.evidence={"state":"unknown","reason":"insufficient_signal_evidence"}
            return None
        # 無車道標定時，不把不同燈的紅／綠票混合。多個穩定燈衝突則回報未知。
        colors={samples[-1][1] for _,_,samples in stable}
        if len(colors)!=1:
            self.evidence={"state":"unknown","reason":"conflicting_signal_tracks"}
            return None
        key,track,samples=max(stable,key=lambda item:(item[0]==self.selected,len(item[2]),max(s[2] for s in item[2])))
        self.selected=key
        self.evidence={"state":samples[-1][1],"signal_id":key,"sample_count":len(samples),
                       "sample_frames":[s[0] for s in samples],"last_observed_frame":track["last"],
                       "max_confidence":max(s[2] for s in samples),"reason":"temporal_support"}
        return self.evidence["state"]
