"""模型輸出 → 規則輸入的逐幀後處理（不載入模型、不寫檔）。

run_pipeline 的影片辨識與日後的其他入口共用同一條處理鏈，順序固定為：
合併車距／車牌 → 建立觀測紀錄 → 鏡頭運動 → 停止線補償 → 騎士配對 → 雙線 → 停等區／斑馬線區域。
"""
from dataclasses import dataclass
from typing import Any

from observation_schema import build_frame_observation
from road_compensation import attach_compensation
from rider_tracking import RiderTracker, attach_riders
from double_lines import attach_double_lines
from area_rules import attach_area_geometry


@dataclass
class FrameResult:
    record: dict              # 寫入 JSONL、交給違規規則的觀測紀錄
    vehicles: list            # 已合併車距／車牌欄位的車輛（CSV 與畫面標註使用）
    traffic_lights: list
    road_markings: list       # 模型原始標線紀錄
    plates: list
    marking_result: Any       # 標線模型的 ModelResult（含遮罩，供疊圖）
    compensation: Any = None  # CompensationResult；停用補償時為 None


class FrameProcessor:
    def __init__(self, fps, width, height, enabled_models, compensator=None, area_config=None,
                 ego_motion=None):
        self.fps, self.width, self.height = fps, width, height
        self.enabled_models = list(enabled_models)
        self.ego_motion = ego_motion
        self.compensator = compensator
        self.area_config = area_config
        self.rider_tracker = RiderTracker(fps)
        self.double_line_state = {}
        self.area_state = {}

    def process(self, frame, frame_index, timestamp, results):
        vehicles = [dict(item) for item in results["vehicles"].records]
        traffic_lights = results["traffic_lights"].records
        road_markings = results["road_markings"].records
        plates = results["ocr"].records if "ocr" in self.enabled_models else results["plates"].records
        distances = {item["object_id"]: item for item in results["depth"].records if item.get("object_id")}
        plate_map = {item["object_id"]: item for item in plates if item.get("object_id")}
        ocr_artifacts = results["ocr"].artifacts
        for vehicle in vehicles:
            object_id = vehicle.get("object_id")
            vehicle["distance_m"] = distances.get(object_id, {}).get("distance_m")
            vehicle["plate_text"] = (plate_map.get(object_id, {}).get("text") or
                                     ocr_artifacts.get("vehicle_plate_map", {}).get(object_id))
            vehicle["text_confidence"] = ocr_artifacts.get("vehicle_plate_confidence_map", {}).get(object_id)
        record = build_frame_observation(
            frame_index, timestamp, self.width, self.height, self.fps,
            vehicles, traffic_lights, road_markings,
            results["depth"].records, plates, self.enabled_models)
        motion = None
        if self.ego_motion is not None:
            motion, _ = self.ego_motion.update(
                frame, frame_index=frame_index, timestamp_sec=timestamp, vehicles=vehicles)
        compensation = None
        if self.compensator is not None:
            compensation = self.compensator.process(
                frame, frame_index=frame_index, timestamp_sec=timestamp,
                road_markings=road_markings, vehicles=vehicles, motion=motion)
            # 原始／補償結果均保存；規則的 road_markings 入口只接收通過品質閘門者。
            record = attach_compensation(record, compensation)
        if motion is not None:
            record["postprocessing"]["ego_motion"] = motion.to_dict()
        attach_riders(record, self.rider_tracker)
        mask = results["road_markings"].artifacts.get("mask")
        attach_double_lines(record, mask, self.double_line_state)
        if self.area_config is not None:
            attach_area_geometry(record, mask, self.area_state, self.area_config)
        return FrameResult(record, vehicles, traffic_lights, road_markings, plates,
                           results["road_markings"], compensation)
