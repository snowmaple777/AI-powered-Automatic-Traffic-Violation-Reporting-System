"""推論服務串流工作（4br）：參數、收錄狀態、停止收錄後照常整理結果。用假的 run_pipeline，不載入模型。"""
import io
import json
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from inference_service import jobs

FAKE_PIPELINE = textwrap.dedent('''
    import json, sys, time
    from pathlib import Path
    argv = sys.argv[1:]
    out = Path(argv[argv.index("--output-dir") + 1]); out.mkdir(parents=True, exist_ok=True)
    name = argv[argv.index("--stream-name") + 1]
    stop = Path(argv[argv.index("--stream-stop-file") + 1])
    (out / "argv.json").write_text(json.dumps(argv), encoding="utf-8")
    seg = out / f"{name}_stream"; seg.mkdir(exist_ok=True)
    det = (out / f"{name}_detections.jsonl").open("w", encoding="utf-8")
    frames = 0
    while not stop.exists() and frames < 3000:
        frames += 10
        info = dict(fps=10.0, width=64, height=48, frames=frames, reason=None, finished=False, error=None)
        (seg / "stream.json").write_text(json.dumps(info), encoding="utf-8")
        for i in range(frames - 10, frames):
            det.write(json.dumps({"frame": {"index": i}, "observations": {}}) + "\\n")
        det.flush()
        print(f"已處理 {frames} 幀，累計 10.00 FPS（含首次暖機），最近 10 幀 10.00 FPS", flush=True)
        time.sleep(0.2)
    info.update(reason="stopped", finished=True)
    (seg / "stream.json").write_text(json.dumps(info), encoding="utf-8")
    event = {"event_id": "e1", "rule_id": "r", "status": "suspected", "timestamp_sec": 0.5, "frame": None,
             "conditions": {"a": True}}
    (out / f"{name}_violations.jsonl").write_text(json.dumps(event) + "\\n", encoding="utf-8")
''')


def wait(predicate, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class StreamJobTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'run_pipeline.py').write_text(FAKE_PIPELINE, encoding='utf-8')
        probe = patch.object(jobs.Manager, '_probe_gpu', lambda self: None)
        probe.start()
        self.addCleanup(probe.stop)
        self.manager = jobs.Manager(self.root / 'data', self.root, sys.executable, preload=False)

    def tearDown(self):
        self._tmp.cleanup()

    def test_rejects_non_stream_urls(self):
        for url in ('', 'input/a.mp4', 'file:///C:/a.mp4', 'rtsp://'):
            with self.assertRaises(ValueError):
                self.manager.create_stream(url, {})

    def test_stream_job_reports_progress_and_stops_gracefully(self):
        job = self.manager.create_stream('http://127.0.0.1:8790/stream.mjpg', {'max_seconds': '60'})
        self.assertEqual((job['source'], job['input_name']), ('stream', '127.0.0.1/stream.mjpg'))
        self.assertTrue(wait(lambda: (job.get('stream') or {}).get('state') == 'recording'))
        self.assertTrue(wait(lambda: (job['stream'].get('recorded_sec') or 0) >= 2))
        public = self.manager.public(job)
        self.assertEqual(public['source'], 'stream')
        self.assertGreater(public['stream']['recorded_sec'], 0)
        self.manager.stop(job['id'])
        self.assertTrue((Path(job['dir']) / 'stop').exists())
        self.assertTrue(wait(lambda: job['state'] in jobs.FINAL_STATES))
        self.assertEqual(job['state'], 'done', job.get('error'))
        self.assertEqual(job['stream']['state'], 'stopped')
        self.assertEqual(job['video']['fps'], 10.0)
        argv = json.loads((Path(job['dir']) / 'outputs' / 'argv.json').read_text(encoding='utf-8'))
        self.assertEqual(argv[0], 'http://127.0.0.1:8790/stream.mjpg')
        self.assertEqual(argv[argv.index('--stream-max-seconds') + 1], '60.0')
        self.assertNotIn('--max-frames', argv)
        result = json.loads((Path(job['dir']) / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(len(result['events']), 1)
        self.assertTrue(any('已停止收錄' in x['msg'] for x in job['_log']))

    def test_stop_on_queued_stream_cancels(self):
        with patch.object(self.manager.queue, 'put'):  # 不讓工作開始
            job = self.manager.create_stream('rtsp://cam.local/live', {})
        self.manager.stop(job['id'])
        self.assertEqual(job['state'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
