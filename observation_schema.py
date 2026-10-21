"""感知模型與違規規則之間的資料契約（逐幀觀測紀錄，即 *_detections.jsonl 的每一行）。

座標一律為原圖像素；時間為秒。欄位由下列階段依序寫入（frame_processing.FrameProcessor）：

frame                       build_frame_observation   index、timestamp_sec、width、height、fps
enabled_models              build_frame_observation   本次啟用的模型階段
observations.vehicles       模型（vehicles）          車框；source_frame≠index 表示沿用前幀的快取框，
                                                      規則不得當成新觀測。另含 distance_m、plate_text、text_confidence
observations.traffic_lights 模型（traffic_lights）
observations.road_markings  模型（road_markings）；啟用補償時改為只含通過品質閘門的停止線（rule_markings）
observations.road_markings_raw / road_markings_compensated   停止線補償層（road_compensation）
observations.depth / plates 模型（depth、plates 或 ocr）
observations.rider_groups / rule_vehicles                    騎士配對（rider_tracking）；規則以 rule_vehicles 為輸入
observations.double_lines   雙線幾何（double_lines.attach_double_lines）
observations.traffic_regions / area_vehicles                 停等區／斑馬線（area_rules.attach_area_geometry）
postprocessing.ego_motion   鏡頭運動（road_compensation.EgoMotionTracker），1.6 起新增；
                            讀取請用 core.motion.road_motion()，舊紀錄會退回 road_marking_compensation.motion
postprocessing.road_marking_compensation / rider_association / double_lines / area_geometry   各階段診斷

版本紀錄：1.6 新增 postprocessing.ego_motion（鏡頭運動成為獨立階段）。
"""
import math

SCHEMA_VERSION = "1.6"


def build_frame_observation(frame_index, timestamp_sec, width, height, fps,
                            vehicles, traffic_lights, road_markings,
                            depth=None, plates=None, enabled_models=None):
    """Return the only input shape consumed by violation rules."""
    return {
        "schema_version": SCHEMA_VERSION,
        "frame": {
            "index": frame_index,
            "timestamp_sec": round(timestamp_sec, 3),
            "width": width,
            "height": height,
            "fps": round(fps, 5),
        },
        "observations": {
            "vehicles": vehicles,
            "traffic_lights": traffic_lights,
            "road_markings": road_markings,
            "double_lines": [],
            "traffic_regions": [],
            "depth": depth or [],
            "plates": plates or [],
        },
        "enabled_models": enabled_models or [],
        "postprocessing": {"road_marking_compensation": {"enabled": False}},
    }


def _box_ok(box):
    return (isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) and math.isfinite(v) for v in box)
            and box[2] >= box[0] and box[3] >= box[1])


def validate_observation(record):
    """回傳違反契約之處的清單（空清單代表通過）。只檢查規則依賴的結構，不檢查數值合理性。"""
    problems = []
    frame, obs = record.get("frame"), record.get("observations")
    if not isinstance(frame, dict) or not isinstance(obs, dict):
        return ["frame／observations 必須是物件"]
    for key, kind in (("index", int), ("timestamp_sec", (int, float)), ("width", int), ("height", int), ("fps", (int, float))):
        if not isinstance(frame.get(key), kind) or isinstance(frame.get(key), bool):
            problems.append(f"frame.{key} 型別錯誤")
    for key in ("vehicles", "traffic_lights", "road_markings"):  # 各版本皆必備
        if not isinstance(obs.get(key), list):
            problems.append(f"observations.{key} 必須是陣列")
    for key in ("double_lines", "traffic_regions", "depth", "plates", "rule_vehicles", "area_vehicles"):
        if key in obs and not isinstance(obs[key], list):  # 較新版本才有；存在時須為陣列
            problems.append(f"observations.{key} 必須是陣列")
    if not isinstance(record.get("postprocessing"), dict):
        problems.append("postprocessing 必須是物件")
    for group in ("vehicles", "rule_vehicles", "area_vehicles"):
        for n, vehicle in enumerate(obs.get(group) or []):
            where = f"observations.{group}[{n}]"
            if not _box_ok(vehicle.get("bbox_xyxy")):
                problems.append(f"{where}.bbox_xyxy 無效")
            if not isinstance(vehicle.get("class_name"), str):
                problems.append(f"{where}.class_name 缺少")
            if vehicle.get("object_id") is not None and not isinstance(vehicle["object_id"], str):
                problems.append(f"{where}.object_id 必須是字串或 null")
            if "source_frame" in vehicle and not isinstance(vehicle["source_frame"], int):
                problems.append(f"{where}.source_frame 必須是整數")
            elif isinstance(vehicle.get("source_frame"), int) and vehicle["source_frame"] > frame.get("index", 0):
                problems.append(f"{where}.source_frame 晚於當幀")
    for n, marking in enumerate(obs.get("road_markings") or []):
        if not isinstance(marking.get("class_name"), str) or not isinstance(marking.get("components"), list):
            problems.append(f"observations.road_markings[{n}] 缺少 class_name／components")
    for n, line in enumerate(obs.get("double_lines") or []):
        if not line.get("track_id") or len(line.get("centerline") or []) < 2:
            problems.append(f"observations.double_lines[{n}] 缺少 track_id 或 centerline")
    motion = record.get("postprocessing", {}).get("ego_motion")
    if motion is not None and (not isinstance(motion.get("valid"), bool) or (motion["valid"] and motion.get("matrix") is None)):
        problems.append("postprocessing.ego_motion 無效")
    return problems
