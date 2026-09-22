"""有界停止線幾何：不把局部線段延伸成橫跨全畫面的直線。"""
from copy import deepcopy
import math
import cv2
import numpy as np


def angle(component):
    x1,y1,x2,y2 = component["line_xyxy"]
    return math.degrees(math.atan2(y2-y1,x2-x1))


def line_y(component, x):
    x1,y1,x2,y2 = component["line_xyxy"]
    return y1+(x-x1)*(y2-y1)/max(x2-x1,1e-6)


def bounded_component(source, width, height, config):
    """驗證模型輸出，再將原本可能無限延伸的主軸裁回實際 component 範圍。"""
    try:
        x,y,w,h = map(float,source["bbox_xywh"])
        line = np.asarray(source["line_xyxy"],dtype=float)
        area = float(source["area"])
        if line.shape != (4,) or not np.isfinite([x,y,w,h,area,*line]).all():
            return None
        if w <= 0 or h <= 0 or area < config.min_component_area or w/h < config.min_aspect:
            return None
        left,top = max(0,int(x)),max(0,int(y))
        right,bottom = min(width,int(math.ceil(x+w))),min(height,int(math.ceil(y+h)))
        if right<=left or bottom<=top:
            return None
        ok,a,b = cv2.clipLine((left,top,right-left,bottom-top),tuple(np.rint(line[:2]).astype(int)),tuple(np.rint(line[2:]).astype(int)))
        if not ok or abs(b[0]-a[0]) < 2:
            return None
        a,b = sorted((a,b))
        result = deepcopy(source)
        result.update(area=int(min(area,(right-left)*(bottom-top))),
                      bbox_xywh=[left,top,right-left,bottom-top],
                      line_xyxy=[float(v) for v in (*a,*b)],
                      centroid=[(a[0]+b[0])/2,(a[1]+b[1])/2])
        return result if abs(angle(result))<=config.max_angle_degrees else None
    except (KeyError,TypeError,ValueError,OverflowError):
        return None


def occlusion_ratio(component, vehicles, width, height):
    # 只在有限線段上取樣，車框只當作遮擋線索，不視為精確物件分割。
    line = np.array(component["line_xyxy"]).reshape(2,2)
    points = np.linspace(line[0],line[1],80)
    visible = (points[:,0]>=0)&(points[:,0]<width)&(points[:,1]>=0)&(points[:,1]<height)
    points = points[visible]
    if not len(points):
        return 0.
    covered = np.zeros(len(points),bool)
    for vehicle in vehicles:
        x1,y1,x2,y2 = vehicle["bbox_xyxy"]
        covered |= (points[:,0]>=x1)&(points[:,0]<=x2)&(points[:,1]>=y1)&(points[:,1]<=y2)
    return float(covered.mean())


def merge_fragments(components, vehicles, width, height, config):
    """同類、近共線片段才可合併；較大的間隙還必須有遮擋線索。"""
    remaining = sorted(deepcopy(components),key=lambda c:c["bbox_xywh"][0])
    merged = []
    for item in remaining:
        combined = False
        for prior in merged:
            a,b = prior["line_xyxy"],item["line_xyxy"]
            gap = max(0.,b[0]-a[2])
            xmid = (a[2]+b[0])/2
            if (abs(angle(prior)-angle(item))>8 or gap>width*config.merge_gap_ratio
                    or abs(line_y(prior,xmid)-line_y(item,xmid))>height*config.merge_y_ratio):
                continue
            bridge = {"line_xyxy":[a[2],a[3],b[0],b[1]]}
            if gap>width*.008 and occlusion_ratio(bridge,vehicles,width,height)<config.min_occlusion_ratio:
                continue
            x=min(prior["bbox_xywh"][0],item["bbox_xywh"][0])
            y=min(prior["bbox_xywh"][1],item["bbox_xywh"][1])
            right=max(c["bbox_xywh"][0]+c["bbox_xywh"][2] for c in (prior,item))
            bottom=max(c["bbox_xywh"][1]+c["bbox_xywh"][3] for c in (prior,item))
            if right-x>width*.65:
                continue
            points=np.asarray([a[:2],a[2:],b[:2],b[2:]],np.float32)
            vx,vy,x0,y0=cv2.fitLine(points,cv2.DIST_L2,0,.01,.01).ravel()
            if abs(vx)<1e-6:
                continue
            left_line=min(a[0],b[0]); right_line=max(a[2],b[2])
            prior.update(bbox_xywh=[x,y,right-x,bottom-y],
                         area=min(prior["area"]+item["area"],(right-x)*(bottom-y)),
                         line_xyxy=[left_line,float(y0+(left_line-x0)*vy/vx),right_line,float(y0+(right_line-x0)*vy/vx)],
                         merged_fragments=prior.get("merged_fragments",1)+item.get("merged_fragments",1))
            prior["centroid"]=[(left_line+right_line)/2,(prior["line_xyxy"][1]+prior["line_xyxy"][3])/2]
            combined=True
            break
        if not combined:
            item["merged_fragments"]=1
            merged.append(item)
    return merged


def project(component, matrix, width, height, config):
    x,y,w,h=component["bbox_xywh"]
    points=np.float32([[x,y],[x+w,y],[x+w,y+h],[x,y+h],component["line_xyxy"][:2],component["line_xyxy"][2:]])
    transformed=cv2.perspectiveTransform(points[None],matrix)[0]
    if not np.isfinite(transformed).all():
        return None
    left,top=transformed[:4].min(axis=0)
    right,bottom=transformed[:4].max(axis=0)
    ratio=abs(cv2.contourArea(transformed[:4]))/max(1.,w*h)
    result=deepcopy(component)
    result.update(bbox_xywh=[float(left),float(top),float(right-left),float(bottom-top)],
                  area=max(1,int(component["area"]*ratio)),line_xyxy=transformed[4:].ravel().tolist())
    return bounded_component(result,width,height,config)
