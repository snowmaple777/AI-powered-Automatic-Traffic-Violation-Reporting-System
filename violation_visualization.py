"""Chinese review cards shared by overview videos and exported event clips."""
from functools import lru_cache
import os
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


STATUS_COLORS = {"suspected": (0, 165, 255), "confirmed": (0, 0, 255)}
RULE_LABELS = {"red_light_stop_line_crossing": "紅燈越過停止線",
               "double_yellow_line_crossing": "跨越雙黃線",
               "double_white_line_crossing": "跨越雙白線"}


def find_event_vehicle(vehicles, event):
    evidence = event.get("evidence", {}).get("vehicle", {})
    ids = {v for v in (event.get("object_id"), evidence.get("source_object_id")) if v}
    return next((v for v in vehicles if v.get("object_id") in ids
                 or v.get("source_object_id") in ids), None)


def event_details(event, vehicle=None):
    """Use current matched text when available, retaining evidence on track loss."""
    evidence = event.get("evidence", {}).get("vehicle", {})
    vehicle = vehicle or {}
    confirmed = event.get("status") == "confirmed"
    reason = event.get("description") or (
        "紅燈期間由停止線前移至線後，符合紅燈越線規則。" if confirmed else
        "已由停止線前移至線後；紅燈或越線證據尚未完整。")
    if not event.get("description") and event.get("rule_id") != "red_light_stop_line_crossing":
        reason = RULE_LABELS.get(event.get("rule_id"), event.get("rule_id")) or "未提供違規理由"
    return dict(status="規則確認・待覆核" if confirmed else "疑似違規・待覆核",
                reason=reason,
                vehicle_id=vehicle.get("source_object_id") or evidence.get("source_object_id")
                    or vehicle.get("object_id") or event.get("object_id") or "未提供",
                plate=vehicle.get("plate_text") or evidence.get("plate_text")
                    or event.get("plate_text") or "未辨識",
                event_id=event.get("event_id", "未提供"),
                timestamp_sec=float(event.get("timestamp_sec", 0)))


def format_event_summary(event):
    info = event_details(event)
    return (f"{info['status']}\n"
            f"違規理由：{info['reason']}\n"
            f"車輛編號：{info['vehicle_id']}\n"
            f"車牌號碼：{info['plate']}\n"
            f"事件時間：{info['timestamp_sec']:.2f} 秒\n"
            f"事件編號：{info['event_id']}\n")


@lru_cache(maxsize=16)
def _font(size):
    candidates = [os.environ.get("TRAFFIC_AI_FONT"),
                  str(Path(os.environ.get("WINDIR", "C:/Windows"))/"Fonts/msjh.ttc"),
                  "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                  "/System/Library/Fonts/PingFang.ttc"]
    for path in candidates:
        if path and Path(path).is_file():
            return ImageFont.truetype(path, size)
    raise RuntimeError("找不到中文字型；請以 TRAFFIC_AI_FONT 指定支援繁體中文的字型檔")


def _wrap(text, font, width, max_lines=3):
    lines, line = [], ""
    for char in str(text).replace("\n", " "):
        if line and font.getlength(line+char) > width:
            lines.append(line)
            line = ""
        line += char
    if line:
        lines.append(line)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and font.getlength(lines[-1]+"…") > width:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    return lines


def draw_violation_candidates(frame, vehicles, events, timestamp_sec, hold_seconds=1.0,
                              frame_index=None):
    """Draw bounded, stacked cards; lost tracks never reuse stale evidence boxes."""
    active = [e for e in events if hold_seconds is None or
              0 <= timestamp_sec-e.get("confirmation_timestamp_sec", e["timestamp_sec"]) <= hold_seconds]
    if not active:
        return frame
    height, width = frame.shape[:2]
    size = max(11, min(28, round(width/70)))
    font = _font(size)
    pad, gap = max(6, size//2), max(4, size//3)
    card_width = min(width-2*gap, max(300, round(width*.45)))
    line_height = size+max(4, size//3)
    cards = []
    for event in active:
        status = "confirmed" if event.get("status") == "confirmed" else "suspected"
        color = STATUS_COLORS[status]
        vehicle = find_event_vehicle(vehicles, event)
        if vehicle is None and frame_index == event.get("frame"):
            vehicle = event.get("evidence", {}).get("vehicle")
        if vehicle and vehicle.get("bbox_xyxy"):
            x1,y1,x2,y2 = map(int,vehicle["bbox_xyxy"])
            cv2.rectangle(frame,(x1,y1),(x2,y2),color,4)
        info = event_details(event,vehicle)
        title_color = (130,150,255) if status == "confirmed" else (100,205,255)
        lines = [(info['status'],title_color)]
        for label,value in (("違規理由",info['reason']), ("車輛編號",info['vehicle_id']),
                            ("車牌號碼",info['plate'])):
            for text in _wrap(f"{label}：{value}",font,card_width-2*pad,
                              max_lines=3 if label=="違規理由" else 2):
                lines.append((text,(245,245,245)))
        timing = f"事件 {info['timestamp_sec']:.2f}s　畫面 {timestamp_sec:.2f}s"
        if vehicle is None:
            timing += "　目標暫未追蹤"
        lines.extend((s,(185,185,185)) for s in _wrap(timing,font,card_width-2*pad,2))
        cards.append((lines,color))
    image = Image.fromarray(cv2.cvtColor(frame,cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(image)
    top = gap
    for index,(lines,color) in enumerate(cards):
        card_height = 2*pad+line_height*len(lines)
        if top+card_height > height-gap:
            # Avoid stacking cards beyond the frame or on top of one another.
            footer = f"另有 {len(cards)-index} 筆事件"
            if top+line_height+pad <= height:
                draw.rectangle((gap,top,gap+card_width,top+line_height+pad),fill=(18,22,28))
                draw.text((gap+pad,top+pad//2),footer,font=font,fill=(255,255,255))
            break
        draw.rounded_rectangle((gap,top,gap+card_width,top+card_height),radius=pad,
                               fill=(18,22,28),outline=tuple(reversed(color)),width=2)
        for row,(text,text_color) in enumerate(lines):
            draw.text((gap+pad,top+pad+row*line_height),text,font=font,
                      fill=tuple(reversed(text_color)))
        top += card_height+gap
    frame[:] = cv2.cvtColor(np.asarray(image),cv2.COLOR_RGB2BGR)
    return frame
