"""real_replay 讀遮罩：非 ASCII 路徑可讀；檔案損壞時報錯而不是當成沒有標線。"""
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from tools.real_replay import read_mask


class ReadMaskTests(unittest.TestCase):
    def test_non_ascii_path_and_missing_and_corrupt(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "新增資料夾 (3)"
            folder.mkdir()
            mask = np.zeros((6, 8), np.uint8)
            mask[2:4, 1:7] = 3
            ok, data = cv2.imencode(".png", mask)
            self.assertTrue(ok)
            path = folder / "000000.png"
            data.tofile(str(path))
            self.assertTrue(np.array_equal(read_mask(path), mask))
            self.assertIsNone(read_mask(folder / "000001.png"))
            (folder / "000002.png").write_bytes(b"not a png")
            with self.assertRaises(ValueError):
                read_mask(folder / "000002.png")


if __name__ == "__main__":
    unittest.main()
