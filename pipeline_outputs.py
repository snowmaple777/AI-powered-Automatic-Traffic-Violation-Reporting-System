"""逐幀輸出：偵測 CSV 與標註畫面。內容只取自 FrameResult，不做任何判定。"""
import csv
import json

from model_library.detectors import draw_boxes
from model_library.render import overlay_markings
from road_compensation.render import draw_compensation
from rider_tracking import draw_rider_groups
from double_lines import draw_double_lines
from area_rules import draw_area_geometry
from violation_visualization import draw_violation_candidates

CSV_FIELDS = ["frame", "timestamp_sec", "model", "class_id", "class_name", "confidence", "track_id",
              "object_id", "track_age_frames", "center", "bottom_center", "geometry", "distance_m",
              "plate_text", "raw_text", "text_confidence"]


class DetectionCsv:
    def __init__(self, stream):
        self.writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        self.writer.writeheader()

    def write(self, frame_index, timestamp, result):
        ts = round(timestamp, 3)
        for model_name, items in (("vehicle", result.vehicles), ("traffic_light", result.traffic_lights),
                                  ("plate", result.plates)):
            for item in items:
                self.writer.writerow({"frame": frame_index, "timestamp_sec": ts,
                    "model": model_name, "class_id": item["class_id"], "class_name": item["class_name"],
                    "confidence": item["confidence"], "track_id": item["track_id"],
                    "object_id": item.get("object_id", ""),
                    "track_age_frames": item.get("track_age_frames", ""),
                    "center": json.dumps(item["center"]),
                    "bottom_center": json.dumps(item["bottom_center"]),
                    "distance_m": item.get("distance_m"),
                    "plate_text": item.get("text", item.get("plate_text")),
                    "raw_text": item.get("raw_text"),
                    "text_confidence": item.get("text_confidence"),
                    "geometry": json.dumps(item["bbox_xyxy"])})
        for item in result.road_markings:
            self.writer.writerow({"frame": frame_index, "timestamp_sec": ts,
                "model": "road_marking", "class_id": item["class_id"], "class_name": item["class_name"],
                "confidence": "", "track_id": "", "object_id": "", "track_age_frames": "",
                "center": "", "bottom_center": "", "geometry": json.dumps(item["components"])})
        for line in result.record["observations"]["double_lines"]:
            self.writer.writerow({"frame": frame_index, "timestamp_sec": ts,
                "model": "double_line", "class_id": line["class_id"],
                "class_name": line["class_name"], "track_id": line["track_id"],
                "track_age_frames": line["observed_frames"], "geometry": json.dumps(line)})
        if result.compensation is not None:
            for item in result.compensation.compensated_markings:
                if any("compensation" in c for c in item["components"]):
                    self.writer.writerow({"frame": frame_index, "timestamp_sec": ts,
                        "model": "road_marking_compensated", "class_id": item["class_id"],
                        "class_name": item["class_name"], "geometry": json.dumps(item["components"])})


def annotate_frame(frame, result, recent_events, timestamp, draw_areas):
    observations = result.record["observations"]
    annotated = overlay_markings(frame, result.marking_result)
    grouped_members = {member for group in observations["rider_groups"] if group["status"] == "confirmed"
                       for member in (group["rider_id"], group["vehicle_id"])}
    draw_boxes(annotated, [v for v in result.vehicles if v.get("object_id") not in grouped_members],
               (255, 180, 0), "vehicle:")
    draw_boxes(annotated, result.traffic_lights, (0, 255, 255), "light:")
    draw_boxes(annotated, result.plates, (0, 0, 255), "plate:")
    if result.compensation is not None:
        draw_compensation(annotated, result.compensation)
    draw_rider_groups(annotated, observations["rider_groups"])
    draw_double_lines(annotated, observations["double_lines"])
    if draw_areas:
        draw_area_geometry(annotated, result.record)
    draw_violation_candidates(annotated, observations["rule_vehicles"], recent_events, timestamp,
                              area_vehicles=observations.get("area_vehicles"))
    return annotated
