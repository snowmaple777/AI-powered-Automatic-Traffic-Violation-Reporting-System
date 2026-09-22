"""Render review-required candidates independently of perception model drawing."""
import cv2


def draw_violation_candidates(frame, vehicles, events, timestamp_sec, hold_seconds=1.0):
    active=[event for event in events if 0<=timestamp_sec-event["timestamp_sec"]<=hold_seconds]
    for index,event in enumerate(active):
        status = 'RULE CONFIRMED' if event.get('status') == 'confirmed' else 'SUSPECTED'
        label=f'{status}: red-light crossing | {event["object_id"]} | {event["timestamp_sec"]:.2f}s'
        y=52+index*28
        cv2.rectangle(frame,(6,y-22),(min(frame.shape[1]-1,850),y+5),(0,0,0),-1)
        cv2.putText(frame,label,(12,y),cv2.FONT_HERSHEY_SIMPLEX,.65,(0,100,255),2,cv2.LINE_AA)
        vehicle=next((v for v in vehicles if v.get("object_id")==event["object_id"]),None)
        if vehicle is not None:
            x1,y1,x2,y2=map(int,vehicle["bbox_xyxy"])
            cv2.rectangle(frame,(x1,y1),(x2,y2),(0,100,255),4)
    return frame
