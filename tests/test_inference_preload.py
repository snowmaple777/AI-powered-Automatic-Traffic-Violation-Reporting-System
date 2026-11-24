"""推論服務常駐程序：重用、失敗與取消後重開、device 不同時改走每次開新程序。用假的 run_pipeline，不載入模型。"""
import json
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from inference_service import jobs
from inference_service.preload import PreloadedPipeline

FAKE_PIPELINE = textwrap.dedent('''
    import json, sys, time
    from pathlib import Path

    def fake(argv):
        video = Path(argv[0])
        out = Path(argv[argv.index("--output-dir") + 1])
        out.mkdir(parents=True, exist_ok=True)
        if "hang" in video.stem:
            time.sleep(600)
        if "fail" in video.stem:
            print("RuntimeError: boom", flush=True)
            return 1, "boom"
        record = {"frame": {"index": 0, "timestamp_sec": 0.0}, "observations": {"vehicles": []}}
        (out / f"{video.stem}_detections.jsonl").write_text(json.dumps(record) + "\\n", encoding="utf-8")
        event = {"event_id": "e1", "rule_id": "r", "status": "suspected", "timestamp_sec": 0.1, "frame": None,
                 "conditions": {"a": True}, "plate": {"text": "ABC-1234", "text_confidence": 0.9}}
        (out / f"{video.stem}_violations.jsonl").write_text(json.dumps(event) + "\\n", encoding="utf-8")
        print("已處理 10 幀，累計 5.00 FPS（含首次暖機），最近 10 幀 5.00 FPS", flush=True)
        return 0, None

    if __name__ == "__main__":
        sys.exit(fake(sys.argv[1:])[0])
''')

FAKE_WORKER = textwrap.dedent('''
    import json, sys
    sys.path.insert(0, sys.argv[1])
    from run_pipeline import fake
    # 真實函式庫偶爾印出不換行的片段，標記不一定在行首（實測發生過）
    sys.stdout.write(" ")
    print("@@WORKER_READY " + json.dumps({"device": "cuda:0", "load_seconds": 0.0, "warmup_seconds": 0.0}), flush=True)
    for line in sys.stdin:
        code, error = fake(json.loads(line)["argv"])
        sys.stdout.write("\\x1b[0m")
        print("@@JOB_END " + json.dumps({"code": code, "error": error}), flush=True)
''')


def wait(predicate, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class PreloadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        (self.tmp / 'run_pipeline.py').write_text(FAKE_PIPELINE, encoding='utf-8')
        (self.tmp / 'worker.py').write_text(FAKE_WORKER, encoding='utf-8')
        video = self.tmp / 'clip.avi'
        writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*'MJPG'), 10, (64, 48))
        for _ in range(5):
            writer.write(np.zeros((48, 64, 3), np.uint8))
        writer.release()
        self.video = video.read_bytes()
        probe = patch.object(jobs.Manager, '_probe_gpu', lambda self: None)
        probe.start()
        self.addCleanup(probe.stop)
        self.manager = jobs.Manager(self.tmp / 'data', self.tmp, sys.executable, preload=False)
        self.manager.preload = PreloadedPipeline(sys.executable, self.tmp, kill=jobs._kill_tree,
                                                 script=self.tmp / 'worker.py')
        self.manager.preload.start()
        self.assertTrue(self.manager.preload.ready_event.wait(20))

    def tearDown(self):
        self.manager.preload.stop()
        self._tmp.cleanup()

    def submit(self, name, device='auto'):
        import io
        job = self.manager.create(name, io.BytesIO(self.video), len(self.video), {'device': device})
        self.assertTrue(wait(lambda: job['state'] in jobs.FINAL_STATES), job['state'])
        return job

    def logs(self, job):
        return [x['msg'] for x in job['_log']]

    def test_reuses_worker_for_consecutive_jobs(self):
        pid = self.manager.preload.proc.pid
        for name in ('a.mp4', 'b.mp4'):
            job = self.submit(name)
            self.assertEqual(job['state'], 'done', job.get('error'))
            self.assertIn('使用已預先載入的模型', self.logs(job))
            result = json.loads((Path(job['dir']) / 'result.json').read_text(encoding='utf-8'))
            self.assertEqual(result['events'][0]['plate_text'], 'ABC-1234')
        self.assertEqual(self.manager.preload.proc.pid, pid)
        self.assertEqual(self.manager.preload.status()['jobs_done'], 2)
        self.assertEqual(self.manager.preload.status()['state'], 'ready')

    def test_failed_job_restarts_worker(self):
        job = self.submit('fail.mp4')
        self.assertEqual(job['state'], 'error')
        self.assertIn('boom', job['error'])
        self.assertTrue(wait(lambda: self.manager.preload.status()['state'] == 'ready'))
        self.assertEqual(self.manager.preload.starts, 2)
        self.assertEqual(self.submit('ok.mp4')['state'], 'done')

    def test_cancel_kills_and_restarts_worker(self):
        import io
        job = self.manager.create('hang.mp4', io.BytesIO(self.video), len(self.video), {})
        self.assertTrue(wait(lambda: self.manager.current is not None))
        self.manager.cancel(job['id'])
        self.assertTrue(wait(lambda: job['state'] in jobs.FINAL_STATES))
        self.assertEqual(job['state'], 'cancelled')
        self.assertTrue(wait(lambda: self.manager.preload.status()['state'] == 'ready'))
        self.assertEqual(self.manager.preload.starts, 2)

    def test_other_device_uses_fresh_process(self):
        job = self.submit('cpu.mp4', device='cpu')
        self.assertEqual(job['state'], 'done', job.get('error'))
        self.assertNotIn('使用已預先載入的模型', self.logs(job))
        self.assertEqual(self.manager.preload.status()['jobs_done'], 0)


if __name__ == '__main__':
    unittest.main()
