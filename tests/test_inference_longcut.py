"""推論服務：檔案工作的降頻篩選與原始片段（4bu 長片切段）。用假的 run_pipeline，不載入模型。"""
import io
import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from inference_service import jobs, media

FAKE_PIPELINE = textwrap.dedent('''
    import json, sys, time
    from pathlib import Path
    argv = sys.argv[1:]
    video = Path(argv[0])
    out = Path(argv[argv.index("--output-dir") + 1]); out.mkdir(parents=True, exist_ok=True)
    (out / "argv.json").write_text(json.dumps(argv), encoding="utf-8")
    for done in (10, 20):
        print(f"已處理 {done} 幀，累計 10.00 FPS（含首次暖機），最近 10 幀 10.00 FPS", flush=True)
        time.sleep(0.4)
    print("深度分析第 1 段：0.5–1.6 秒（第 5–15 幀）", flush=True)
    time.sleep(0.6)
    print("已處理 30 幀，累計 10.00 FPS（含首次暖機），最近 10 幀 10.00 FPS", flush=True)
    time.sleep(0.4)
    event = {"event_id": "e1", "rule_id": "red_light_stop_line_crossing", "status": "suspected",
             "timestamp_sec": 1.0, "frame": 10, "conditions": {"a": True}}
    (out / f"{video.stem}_violations.jsonl").write_text(json.dumps(event) + "\\n", encoding="utf-8")
''')


def wait(predicate, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def tiny_video(path, frames=30):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), 10.0, (64, 48))
    for i in range(frames):
        writer.write(np.full((48, 64, 3), 40 + i * 5, np.uint8))
    writer.release()
    return path.read_bytes()


class FileTriageJobTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'run_pipeline.py').write_text(FAKE_PIPELINE, encoding='utf-8')
        probe = patch.object(jobs.Manager, '_probe_gpu', lambda self: None)
        probe.start()
        self.addCleanup(probe.stop)
        self.manager = jobs.Manager(self.root / 'data', self.root, sys.executable, preload=False)
        self.data = tiny_video(self.root / 'long.avi')

    def tearDown(self):
        self._tmp.cleanup()

    def create(self, options):
        return self.manager.create('long.avi', io.BytesIO(self.data), len(self.data), options)

    def test_file_job_passes_triage_and_raw_clips_and_reports_deep_windows(self):
        job = self.create({'triage': '10', 'clip_raw': '1', 'clip_before': '8', 'clip_after': '6',
                           'profile': 'high_recall'})
        self.assertEqual(job['frames_total'], 30)
        self.assertTrue(wait(lambda: (job.get('triage') or {}).get('deep') == [0.5, 1.6]))
        self.assertEqual(self.manager.public(job)['triage']['windows'], 1)
        self.assertTrue(wait(lambda: job['state'] in jobs.FINAL_STATES))
        self.assertEqual(job['state'], 'done', job.get('error'))
        self.assertIsNone(job['triage']['deep'])
        argv = json.loads((Path(job['dir']) / 'outputs' / 'argv.json').read_text(encoding='utf-8'))
        self.assertEqual(argv[argv.index('--stream-triage-fps') + 1], '10')
        self.assertIn('--clip-raw', argv)
        self.assertEqual(argv[argv.index('--clip-before') + 1], '8.0')
        self.assertNotIn('--stream-name', argv)
        self.assertEqual(argv[argv.index('--rule-profile') + 1], 'high_recall')  # 4by
        result = json.loads((Path(job['dir']) / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual([e['event_id'] for e in result['events']], ['e1'])

    def test_plain_file_job_has_no_triage(self):
        with patch.object(self.manager.queue, 'put'):
            job = self.create({})
        self.assertEqual((job['options']['triage'], job['options']['clip_raw'], job['options']['profile']),
                         (0.0, False, 'default'))
        self.assertIsNone(self.manager.public(job)['triage'])

    def test_rejects_out_of_range_triage(self):
        with self.assertRaises(ValueError):
            self.create({'triage': '1'})
        with self.assertRaises(ValueError):
            self.create({'profile': 'loose'})
        self.assertEqual(list((self.root / 'data' / 'jobs').iterdir()), [])  # 不留下半成品


class H264DetectionTests(unittest.TestCase):
    def test_is_h264(self):
        with tempfile.TemporaryDirectory() as tmp:
            mp4v = Path(tmp) / 'a.mp4'
            writer = cv2.VideoWriter(str(mp4v), cv2.VideoWriter_fourcc(*'mp4v'), 10.0, (64, 48))
            for _ in range(5):
                writer.write(np.zeros((48, 64, 3), np.uint8))
            writer.release()
            self.assertFalse(media.is_h264(mp4v))
            if os.name == 'nt' and getattr(cv2, 'CAP_MSMF', None) is not None and tmp.isascii():
                avc = Path(tmp) / 'b.mp4'
                writer = cv2.VideoWriter(str(avc), cv2.CAP_MSMF, cv2.VideoWriter_fourcc(*'avc1'), 10.0, (64, 48))
                if writer.isOpened():
                    for _ in range(5):
                        writer.write(np.zeros((48, 64, 3), np.uint8))
                    writer.release()
                    self.assertTrue(media.is_h264(avc))


if __name__ == '__main__':
    unittest.main()
