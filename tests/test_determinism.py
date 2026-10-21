"""同樣輸入必須產生同樣輸出，不受 Python 字串雜湊種子（PYTHONHASHSEED）影響。"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = r'''
import json, sys
import numpy as np
sys.path.insert(0, sys.argv[1])
from road_compensation import CompensationConfig, MotionEstimate, RoadMarkingCompensator

class StillCamera:
    def estimate(self, *args):
        return MotionEstimate(True, np.eye(3), "accepted", 100, 1.0, 0.1, 1.0)

compensator = RoadMarkingCompensator(CompensationConfig(), StillCamera())
frame = np.zeros((360, 640, 3), np.uint8)
def line(x, y, w=120):
    return {"area": w * 6, "bbox_xywh": [x, y, w, 6], "line_xyxy": [x, y + 3, x + w, y + 3], "centroid": [x + w / 2, y + 3]}
lines = [line(20 + 150 * k, 250 + 4 * k) for k in range(4)]
outputs = []
for index in range(8):
    visible = lines if index < 5 else []  # 之後全部消失，走「預測」分支
    markings = [{"class_id": 3, "class_name": "stop line", "pixels": 1, "components": visible}] if visible else []
    result = compensator.process(frame, frame_index=index, timestamp_sec=index / 30,
                                 road_markings=markings, vehicles=[])
    outputs.append([c["compensation"]["id"] for m in result.compensated_markings for c in m["components"]])
print(json.dumps(outputs))
'''


class DeterminismTests(unittest.TestCase):
    def test_compensator_output_order_is_independent_of_hash_seed(self):
        results = []
        for seed in ("1", "2", "3"):
            env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONDONTWRITEBYTECODE="1")
            proc = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT)], env=env,
                                  capture_output=True, text=True, check=True)
            results.append(json.loads(proc.stdout))
        self.assertTrue(any(results[0][5:]), "測試序列必須包含預測輸出")
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], results[2])


if __name__ == "__main__":
    unittest.main()
