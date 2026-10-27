"""模型輸出 → 規則輸入的逐幀後處理（不載入模型、不寫檔）。

run_pipeline 的影片辨識與日後的其他入口共用同一條處理鏈，順序固定為：
合併車距／車牌 → 建立觀測紀錄 → 鏡頭運動 → 停止線補償 → 騎士配對 → 雙線 → 停等區／斑馬線區域。
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import time
from typing import Any

import cv2
import numpy as np

from observation_schema import build_frame_observation
from road_compensation import attach_compensation
from rider_tracking import RiderTracker, attach_riders
from double_lines import attach_double_lines
from area_rules import attach_area_geometry
from core.timing import StageTimer


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
        self.timer = StageTimer()
        # 鏡頭運動只依賴畫面與車框；車輛偵測一完成就在背景執行緒估計（OpenCV 會釋放 GIL），
        # 與其餘模型的 GPU 推論重疊。單一工作執行緒，逐幀依序，狀態不會交錯。
        self._motion_executor = None
        self.rider_tracker = RiderTracker(fps)
        self.double_line_state = {}
        self.area_state = {}
        self._last_marking_mask = None  # 前一幀處理後的標線遮罩（標線降頻時推移用）

    def start_motion(self, frame, frame_index, timestamp, vehicles):
        """提早啟動當幀的鏡頭運動估計；回傳 Future（未啟用鏡頭運動時為 None）。"""
        if self.ego_motion is None:
            return None
        if self._motion_executor is None:
            self._motion_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ego-motion")
        return self._motion_executor.submit(self._estimate_motion, frame, frame_index, timestamp, vehicles)

    def _estimate_motion(self, frame, frame_index, timestamp, vehicles):
        started = time.perf_counter()
        motion = self.ego_motion.update(frame, frame_index=frame_index, timestamp_sec=timestamp, vehicles=vehicles)
        return motion, time.perf_counter() - started

    def close(self):
        if self._motion_executor is not None:
            self._motion_executor.shutdown(wait=True)
            self._motion_executor = None

    def _propagate_markings(self, marking, frame_index, motion):
        """標線降頻：沿用前一幀處理後的遮罩，以前幀→當幀的路面單應矩陣推移到當幀，並由遮罩重建標線紀錄。

        鏡頭運動無效時沿用未推移的遮罩（warped=False）。回傳 (ModelResult, 來源資訊)。
        """
        from model_library.base import ModelResult
        from model_library.detectors import mask_components
        artifacts = marking.artifacts
        previous = self._last_marking_mask
        if previous is None:
            previous = artifacts["mask"]
        warped = bool(motion is not None and motion.valid)
        if warped:
            h, w = previous.shape
            mask = cv2.warpPerspective(previous, np.asarray(motion.matrix, dtype=np.float64), (w, h),
                                       flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        else:
            mask = previous
        records = mask_components(mask, artifacts["classes"], artifacts["min_pixels"])
        info = dict(source_frame=artifacts["source_frame"], propagated=True, warped=warped)
        return ModelResult(records, dict(artifacts, mask=mask)), info

    def process(self, frame, frame_index, timestamp, results, motion_future=None):
        """motion_future：start_motion() 的結果；None 時於此同步估計（結果相同）。"""
        timer = self.timer
        motion = None
        if motion_future is not None:
            with timer.measure("ego_motion_wait"):  # 並行估計後仍需等待的時間
                (motion, _), spent = motion_future.result()
            timer.seconds["ego_motion_parallel"] = timer.seconds.get("ego_motion_parallel", 0.0) + spent
        elif self.ego_motion is not None:
            with timer.measure("ego_motion"):
                motion, _ = self.ego_motion.update(
                    frame, frame_index=frame_index, timestamp_sec=timestamp,
                    vehicles=[dict(item) for item in results["vehicles"].records])
        marking = results["road_markings"]
        marking_source = None
        if marking.artifacts.get("source_frame") not in (None, frame_index):
            with timer.measure("marking_propagation"):
                marking, marking_source = self._propagate_markings(marking, frame_index, motion)
        self._last_marking_mask = marking.artifacts.get("mask")
        vehicles = [dict(item) for item in results["vehicles"].records]
        traffic_lights = results["traffic_lights"].records
        road_markings = marking.records
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
        if marking_source is not None:
            record["postprocessing"]["road_marking_source"] = marking_source
        compensation = None
        if self.compensator is not None:
            with timer.measure("stop_line_compensation"):
                compensation = self.compensator.process(
                    frame, frame_index=frame_index, timestamp_sec=timestamp,
                    road_markings=road_markings, vehicles=vehicles, motion=motion)
                # 原始／補償結果均保存；規則的 road_markings 入口只接收通過品質閘門者。
                record = attach_compensation(record, compensation)
        if motion is not None:
            record["postprocessing"]["ego_motion"] = motion.to_dict()
        with timer.measure("riders"):
            attach_riders(record, self.rider_tracker)
        mask = marking.artifacts.get("mask")
        with timer.measure("double_lines"):
            attach_double_lines(record, mask, self.double_line_state)
        if self.area_config is not None:
            with timer.measure("area_geometry"):
                attach_area_geometry(record, mask, self.area_state, self.area_config)
        return FrameResult(record, vehicles, traffic_lights, road_markings, plates,
                           marking, compensation)
