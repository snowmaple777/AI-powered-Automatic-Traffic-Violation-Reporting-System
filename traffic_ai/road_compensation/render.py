"""補償圖層與模型遮罩分開：青色實線=觀測；紫色虛線=預測；灰色=不可判定。"""
import cv2
import numpy as np


def draw_compensation(frame, result):
    for marking in result.compensated_markings:
        for component in marking["components"]:
            meta=component.get("compensation")
            if meta is None:
                continue
            line=np.asarray(component["line_xyxy"]).reshape(2,2)
            predicted=meta["source"]!="observed"
            color=(255,0,255) if predicted else (255,255,0)
            if not meta["rule_eligible"]:
                color=(150,150,150)
            if predicted:
                for i in range(0,20,2):
                    a=line[0]+(line[1]-line[0])*i/20
                    b=line[0]+(line[1]-line[0])*(i+1)/20
                    cv2.line(frame,tuple(a.astype(int)),tuple(b.astype(int)),color,3)
            else:
                cv2.line(frame,tuple(line[0].astype(int)),tuple(line[1].astype(int)),color,3)
            label=f'{meta["id"]} {meta["source"]} {meta["support_score"]:.2f}'
            origin=tuple(line[0].astype(int))
            cv2.putText(frame,label,origin,cv2.FONT_HERSHEY_SIMPLEX,.4,(0,0,0),3,cv2.LINE_AA)
            cv2.putText(frame,label,origin,cv2.FONT_HERSHEY_SIMPLEX,.4,color,1,cv2.LINE_AA)
    cv2.rectangle(frame,(5,4),(min(frame.shape[1]-1,630),32),(20,20,20),-1)
    cv2.putText(frame,"stop line: cyan=observed magenta dashed=predicted gray=unknown",
                (12,24),cv2.FONT_HERSHEY_SIMPLEX,.5,(255,255,255),1,cv2.LINE_AA)
    return frame
