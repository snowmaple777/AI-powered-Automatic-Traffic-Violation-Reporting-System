"""Per-signal temporal evidence; missing detections are not treated as green."""
from collections import deque
import math

# 只有圓形燈參與紅／綠／黃判定。箭頭燈（模型類別 arrow）常與紅燈同時亮起、只允許特定方向，
# 若當成另一種顏色，會讓同一路口出現「顏色衝突」而把紅燈判成未知；保留在觀測中但不參與判定。
SIGNAL_COLORS = frozenset({"red", "green", "yellow"})


def signal_lights(lights):
    return [light for light in lights if light["class_name"].lower() in SIGNAL_COLORS]


class TemporalSignalState:
    def __init__(self, fps, anchor_confidence=.30, support_confidence=.18,
                 window_seconds=.5, max_missing_seconds=.20, min_samples=3,
                 min_mean_confidence=None, id_prefix="signal", yellow_confirm_seconds=0.,
                 dominance_confidence_ratio=None, dominance_size_ratio=None):
        """yellow_confirm_seconds：同一燈由紅轉黃時，黃燈需連續觀測這麼久才中斷紅燈記憶；
        在此之前黃燈不寫入歷史、也不觸發轉燈，紅燈證據照常延續。0 為原行為（立即中斷）。
        dominance_*_ratio：穩定燈顏色衝突時的主燈判定（見 _dominant）；None 為原行為（回報未知）。"""
        self.window=max(3,round(fps*window_seconds))
        self.dominance_confidence_ratio=dominance_confidence_ratio
        self.dominance_size_ratio=dominance_size_ratio if dominance_size_ratio is not None else float("inf")
        self.yellow_confirm=max(1,round(fps*yellow_confirm_seconds))
        self.max_missing=max(1,round(fps*max_missing_seconds))
        self.anchor_confidence=anchor_confidence
        self.support_confidence=support_confidence
        self.min_samples=min_samples
        self.min_mean_confidence=min_mean_confidence
        self.id_prefix=id_prefix
        self.tracks={}
        self.sequence=0
        self.selected=None
        self.evidence={"state":"unknown","reason":"insufficient_signal_evidence"}

    def update(self, lights, index):
        lights=signal_lights(lights)
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
                key=f"{self.id_prefix}-{self.sequence:04d}"
                self.tracks[key]={"history":deque(),"center":center,"last":index,"size":20.}
            track=self.tracks[key]
            color=light["class_name"].lower()
            size=max(box[2]-box[0],box[3]-box[1]) if box else 20.
            pending=track.get("pending_yellow",[])
            if (color=="yellow" and self.yellow_confirm>1 and track["history"]
                    and track["history"][-1][1]=="red"):
                # 紅燈中短暫讀成黃燈（夜間過曝等）：先累積，連續 yellow_confirm 次才算轉燈。
                if pending and index-pending[-1][0]>self.max_missing:
                    pending=[]
                pending.append((index,color,float(light["confidence"])))
                track["pending_yellow"]=pending
                if len(pending)<self.yellow_confirm:
                    track.update(center=center,last=index,size=size)
                    used.add(key)
                    continue
                # 確認轉黃：切斷紅燈記憶，先前累積的黃燈成為歷史（本次觀測於下方加入）。
                track["history"]=deque(pending[:-1])
                if key==self.selected:
                    transition=(key,color)
            track.pop("pending_yellow",None)
            # 同一燈出現不同顏色立即切斷舊紅燈記憶；不以多數決吞掉轉綠證據。
            if track["history"] and track["history"][-1][1]!=color:
                track["history"].clear()
                if key==self.selected and color in {"green","yellow"}:
                    transition=(key,color)
            track["history"].append((index,color,float(light["confidence"])))
            track.update(center=center,last=index,size=size)
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
                    and max(s[2] for s in samples)>=self.anchor_confidence
                    and (self.min_mean_confidence is None or
                         sum(s[2] for s in samples)/len(samples)>=self.min_mean_confidence)):
                stable.append((key,track,samples))
        if not stable:
            self.evidence={"state":"unknown","reason":"insufficient_signal_evidence"}
            return None
        # 無車道標定時，不把不同燈的紅／綠票混合。多個穩定燈衝突時，只有一個燈明顯佔優勢
        # （平均信心或框大小達倍數）才採用它的顏色並標記 dominant（較弱證據），否則回報未知。
        colors={samples[-1][1] for _,_,samples in stable}
        if len(colors)!=1:
            winner=self._dominant(stable)
            if winner is None:
                self.evidence={"state":"unknown","reason":"conflicting_signal_tracks"}
                return None
            key,track,samples=winner
            self.selected=key
            self.evidence={"state":samples[-1][1],"signal_id":key,"sample_count":len(samples),
                           "sample_frames":[s[0] for s in samples],"last_observed_frame":track["last"],
                           "max_confidence":max(s[2] for s in samples),"reason":"dominant_signal_track",
                           "dominant":True,"overridden":sorted({s[-1][1] for _,_,s in stable}-{samples[-1][1]})}
            return self.evidence["state"]
        key,track,samples=max(stable,key=lambda item:(item[0]==self.selected,len(item[2]),max(s[2] for s in item[2])))
        self.selected=key
        self.evidence={"state":samples[-1][1],"signal_id":key,"sample_count":len(samples),
                       "sample_frames":[s[0] for s in samples],"last_observed_frame":track["last"],
                       "max_confidence":max(s[2] for s in samples),"reason":"temporal_support"}
        return self.evidence["state"]

    def _dominant(self, stable):
        """回傳明顯佔優勢的穩定燈：對每個不同顏色的穩定燈，平均信心 >= dominance_confidence_ratio 倍，
        或框邊長 >= dominance_size_ratio 倍。沒有或停用（ratio 為 None）時回傳 None。"""
        if self.dominance_confidence_ratio is None:
            return None
        mean=lambda samples:sum(s[2] for s in samples)/len(samples)
        for item in sorted(stable,key=lambda item:mean(item[2]),reverse=True):
            color,conf,size=item[2][-1][1],mean(item[2]),item[1]["size"]
            if all(conf>=self.dominance_confidence_ratio*mean(other[2]) or
                   size>=self.dominance_size_ratio*other[1]["size"]
                   for other in stable if other[2][-1][1]!=color):
                return item
        return None


class CombinedSignalState:
    """強證據（原規則，可支持「確認」）＋弱證據與紅燈延續（只支持「疑似」）。

    強證據：沿用 TemporalSignalState 原參數，行為與先前完全相同。
    弱證據：獨立追蹤器，接受偵測器最低信心（0.12）的偵測，要求 1 秒內至少 6 次同色、
            平均信心 >= 0.15。實測燈號模型對清楚可見的紅燈信心常只有 0.14–0.25。
    延續：強或弱證據確立紅燈後，若之後暫時沒有任何燈號證據、當幀也沒有綠／黃燈偵測，
          維持紅燈 hold_seconds 秒（號誌常在車輛接近停止線時離開畫面）。
    evidence["strength"] 為 strong／dominant／weak／held；只有 strong 的紅燈能讓事件成為「確認」。
    主燈判定（dominant）：多個穩定燈顏色衝突時，若某燈對所有異色燈的平均信心 >= 1.5 倍或框邊長 >= 2 倍，
    採用它的顏色；這是較弱的證據，只支持「疑似」。沒有明顯主燈時仍回報未知。
    黃燈確認：紅燈中出現的黃燈需連續 yellow_confirm_seconds（預設 0.2 秒）才中斷紅燈，延續階段也一樣
    （連續這麼多幀都有黃燈才取消延續）。全片評估中同一燈「紅→黃→紅」的跳動多為 1–5 幀，真正的黃燈數十幀。
    """

    def __init__(self, fps, anchor_confidence=.30, window_seconds=.5, weak_evidence=True, hold_seconds=3.0,
                 yellow_confirm_seconds=.2, dominance_confidence_ratio=1.5, dominance_size_ratio=2.0):
        dominance = dict(dominance_confidence_ratio=dominance_confidence_ratio, dominance_size_ratio=dominance_size_ratio)
        self.strong = TemporalSignalState(fps, anchor_confidence=anchor_confidence, window_seconds=window_seconds,
                                          yellow_confirm_seconds=yellow_confirm_seconds, **dominance)
        self.weak = (TemporalSignalState(fps, anchor_confidence=.12, support_confidence=.12, window_seconds=1.0,
                                         max_missing_seconds=.4, min_samples=6, min_mean_confidence=.15,
                                         id_prefix="weak-signal", yellow_confirm_seconds=yellow_confirm_seconds,
                                         **dominance)
                     if weak_evidence else None)
        self.hold = max(0, round(fps * hold_seconds))
        self.yellow_confirm = max(1, round(fps * yellow_confirm_seconds))
        self.yellow_run = 0  # 連續含黃燈偵測的幀數
        self.last_red = None
        self.strength = None
        self.evidence = dict(self.strong.evidence, strength=None)

    def update(self, lights, index):
        strong = self.strong.update(lights, index)
        weak = self.weak.update(lights, index) if self.weak is not None else None
        yellow = any(l["class_name"].lower() == "yellow" and l["confidence"] >= .12 for l in lights)
        self.yellow_run = self.yellow_run + 1 if yellow else 0
        if strong is not None:
            # 衝突中以主燈判定者不算強證據：只支持「疑似」，不能讓事件成為「確認」。
            strength = "dominant" if self.strong.evidence.get("dominant") else "strong"
            state, evidence = strong, dict(self.strong.evidence)
        elif weak is not None:
            state, evidence, strength = weak, dict(self.weak.evidence), "weak"
        else:
            state, evidence, strength = None, dict(self.strong.evidence), None
            contrary = (any(l["class_name"].lower() == "green" and l["confidence"] >= .12 for l in lights)
                        or self.yellow_run >= self.yellow_confirm)
            if (self.last_red is not None and not contrary and index - self.last_red["frame"] <= self.hold
                    and self.strong.evidence.get("reason") == "insufficient_signal_evidence"):
                state, strength = "red", "held"
                evidence = dict(self.last_red["evidence"], reason="held_after_red",
                                held_from_frame=self.last_red["frame"])
        if state == "red" and strength in ("strong", "dominant", "weak"):
            self.last_red = {"frame": index, "evidence": dict(evidence, strength=strength)}
        elif state in {"green", "yellow"}:
            self.last_red = None
        self.strength = strength
        self.evidence = dict(evidence, strength=strength)
        return state
