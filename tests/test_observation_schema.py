import json
import unittest
from pathlib import Path

from observation_schema import build_frame_observation, validate_observation

ROOT = Path(__file__).resolve().parents[1]


class ObservationSchemaTests(unittest.TestCase):
    def test_minimal_record_is_valid(self):
        vehicle = dict(bbox_xyxy=[0, 0, 10, 10], class_name="car", object_id="vehicle-000001", source_frame=0)
        self.assertEqual(validate_observation(build_frame_observation(0, 0, 640, 360, 30, [vehicle], [], [])), [])

    def test_contract_violations_are_reported(self):
        record = build_frame_observation(3, .1, 640, 360, 30,
                                         [dict(bbox_xyxy=[5, 5, 1, 1], class_name="car", source_frame=9)], [], [])
        record["observations"]["double_lines"] = [{"centerline": [[0, 0]]}]
        record["postprocessing"]["ego_motion"] = {"valid": True}
        problems = validate_observation(record)
        for text in ("bbox_xyxy", "source_frame 晚於當幀", "double_lines", "ego_motion"):
            self.assertTrue(any(text in p for p in problems), (text, problems))

    def test_saved_outputs_follow_the_contract(self):
        """若本機有辨識輸出，抽查每個檔案的前 30 幀。"""
        paths = sorted((ROOT / "outputs").glob("**/*_detections.jsonl"))[:40]
        if not paths:
            self.skipTest("沒有既有輸出")
        for path in paths:
            with path.open(encoding="utf-8") as source:
                for n, line in zip(range(30), source):
                    self.assertEqual(validate_observation(json.loads(line)), [], f"{path.name} 第 {n} 行")


if __name__ == "__main__":
    unittest.main()
