"""推論服務與介面存圖不可因路徑含中文（如「新增資料夾 (3)」）而失敗。"""
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

from inference_service import media
from ui.core import analyzers


class UnicodeImagePathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / '新增資料夾 (3)'
        self.dir.mkdir()
        self.frame = np.full((48, 64, 3), 128, np.uint8)

    def tearDown(self):
        self._tmp.cleanup()

    def test_draw_preview_writes_without_leftover_temp(self):
        out = self.dir / 'preview.jpg'
        record = {'frame': {'index': 3, 'timestamp_sec': 0.1}, 'observations': {}}
        self.assertTrue(media.draw_preview(self.frame.copy(), record, out))
        self.assertTrue(out.stat().st_size > 0)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ['preview.jpg'])

    def test_snapshot_reads_and_writes_unicode_paths(self):
        import cv2
        video = self.dir / '影片.avi'
        writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*'MJPG'), 10, (64, 48))
        self.assertTrue(writer.isOpened())
        for _ in range(5):
            writer.write(self.frame)
        writer.release()
        out = self.dir / '截圖.jpg'
        self.assertTrue(media.snapshot(video, out, frame_index=2))
        self.assertIsNotNone(cv2.imdecode(np.fromfile(str(out), np.uint8), cv2.IMREAD_COLOR))

    def test_ui_imwrite(self):
        out = os.path.join(str(self.dir), 'E001.jpg')
        self.assertTrue(analyzers._imwrite(out, self.frame))
        self.assertTrue(os.path.getsize(out) > 0)


if __name__ == '__main__':
    unittest.main()
