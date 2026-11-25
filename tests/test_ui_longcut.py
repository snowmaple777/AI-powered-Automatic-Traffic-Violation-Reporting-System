"""介面的長片自動切段（4bu）：排隊、上傳、進度、片段存進片段庫、長片移到 _已切分、取消與重開。
推論服務用假的 RemoteAnalyzer 方法代替，不連網路、不載入模型。"""
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# 專案根目錄也有 core 套件（core.timing），把 ui/core 以另一個名字載入，避免名稱衝突
_UI_CORE = Path(__file__).resolve().parents[1] / 'ui' / 'core'
if 'ui_core' not in sys.modules:
    _spec = importlib.util.spec_from_file_location('ui_core', _UI_CORE / '__init__.py',
                                                   submodule_search_locations=[str(_UI_CORE)])
    sys.modules['ui_core'] = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(sys.modules['ui_core'])
from ui_core import clips, longcut, remote, store  # noqa: E402

EMAIL = 'tester@example.com'


def wait(predicate, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class FakeService:
    """推論服務：上傳 → 兩次進度（第二次在逐幀確認）→ 完成，兩筆事件、其中一筆有片段。"""

    def __init__(self, gate=None):
        self.polls, self.uploaded, self.cancelled, self.gate, self.forever = 0, None, False, gate, False

    def health(self, timeout=2):
        return {'models': []}

    def upload(self, path, query, on_progress):
        self.uploaded = (Path(path).name, dict(query))
        on_progress(0.5)
        on_progress(1.0)
        return {'id': 'job1'}

    def req(self, method, path, timeout=10):
        if path == '/jobs/job1/cancel':
            self.cancelled = True
            return {}
        if path == '/jobs/job1/result':
            return {'elapsed_sec': 3.0, 'events': [
                {'event_id': 'a', 'rule_id': 'red_light_stop_line_crossing', 'status': 'suspected',
                 'timestamp_sec': 75.4, 'plate_text': None,
                 'clip': {'file': 'E001.mp4', 'h264': True, 'start_sec': 67.4, 'end_sec': 81.4, 'event_offset_sec': 8}},
                {'event_id': 'b', 'rule_id': 'crosswalk_stopping', 'status': 'suspected', 'timestamp_sec': 5,
                 'clip': None}]}
        if self.gate is not None:
            self.gate()
        self.polls += 1
        if self.polls == 1 or self.forever:
            return {'state': 'running', 'stage': 'analyzing', 'frames_done': 300, 'frames_total': 1200,
                    'eta_sec': 90, 'triage': {'fps': 10, 'windows': 0, 'deep': None}, 'found': []}
        if self.polls == 2:
            return {'state': 'running', 'stage': 'analyzing', 'frames_done': 600, 'frames_total': 1200,
                    'eta_sec': 60, 'triage': {'fps': 10, 'windows': 1, 'deep': [60.0, 81.4]}, 'found': [{}]}
        return {'state': 'done', 'stage': 'done', 'frames_done': 1200, 'frames_total': 1200}

    def download(self, path, dest):
        Path(dest).write_bytes(b'clip:' + path.encode())


class LongCutTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        config = patch.dict(store.CONFIG, {'clips_dir': str(self.root / 'clips'), 'inference_url': 'http://127.0.0.1:1'})
        config.start()
        self.addCleanup(config.stop)
        poll = patch.object(longcut, 'POLL_SECONDS', 0.01)
        poll.start()
        self.addCleanup(poll.stop)
        store.set_user(EMAIL)
        self.addCleanup(store.set_user, None)
        self.inbox = Path(clips.long_inbox())
        self.inbox.mkdir(parents=True, exist_ok=True)
        (self.inbox / 'trip.mp4').write_bytes(b'x' * 1000)

    def tearDown(self):
        self._tmp.cleanup()

    def fake(self, service):
        patches = [patch.object(remote.RemoteAnalyzer, 'health', lambda s, timeout=2: service.health()),
                   patch.object(remote.RemoteAnalyzer, '_upload', lambda s, p, q, cb: service.upload(p, q, cb)),
                   patch.object(remote.RemoteAnalyzer, '_req', lambda s, m, p, timeout=10: service.req(m, p)),
                   patch.object(remote.RemoteAnalyzer, '_download', lambda s, p, d: service.download(p, d))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def item(self):
        store.set_user(EMAIL)
        return next(x for x in longcut.list_longs()['items'] if x['name'] == 'trip.mp4')

    def test_cut_saves_clips_into_library_and_moves_the_long_video(self):
        service = FakeService()
        self.fake(service)
        seen = []
        orig = longcut._update

        def spy(email, name, **fields):
            seen.append(fields)
            return orig(email, name, **fields)

        with patch.object(longcut, '_update', spy):
            longcut.enqueue('trip.mp4')
            self.assertTrue(wait(lambda: self.item()['state'] in ('done', 'error')))
        item = self.item()
        self.assertEqual(item['state'], 'done', item.get('error'))
        self.assertEqual((item['clips'], item['found'], item['folder']), (1, 2, 'trip'))
        # 推論服務收到降頻篩選＋原始片段、前 8 後 6 秒
        self.assertEqual(service.uploaded[1], {'filename': 'trip.mp4', 'rules': 'all', 'triage': '10', 'clip_raw': '1',
                                               'clip_before': '8', 'clip_after': '6'})
        # 進度條：上傳 50% → 粗略切段 25% → 50%（逐幀確認 60–81.4 秒）→ 存成片段
        self.assertIn({'upload': 0.5}, seen)
        cutting = [s for s in seen if s.get('state') == 'cutting' and 'deep' in s]
        self.assertEqual([(s['progress'], s['deep']) for s in cutting], [(0.25, None), (0.5, [60.0, 81.4])])
        user_dir = Path(clips.user_dir())
        clip = user_dir / 'trip' / '001_闖紅燈_01m15s.mp4'
        self.assertEqual(clip.read_bytes(), b'clip:/jobs/job1/files/E001.mp4')
        info = json.loads(clip.with_suffix('.json').read_text(encoding='utf-8'))
        self.assertEqual((info['source_video'], info['clip_start_sec'], info['event_offset_sec']), ('trip.mp4', 67.4, 8))
        self.assertFalse((self.inbox / 'trip.mp4').exists())
        self.assertTrue((user_dir / '_已切分' / 'trip.mp4').exists())
        # 片段庫看得到切好的片段（_已切分、_切段紀錄 不會出現）
        listed = clips.list_clips()
        self.assertEqual([(c['group'], c['name']) for c in listed['clips']], [('trip', '001_闖紅燈_01m15s.mp4')])

    def test_service_down_is_reported_and_can_retry(self):
        def down(s, timeout=2):
            raise OSError('refused')
        with patch.object(remote.RemoteAnalyzer, 'health', down):
            longcut.enqueue('trip.mp4')
            self.assertTrue(wait(lambda: self.item()['state'] == 'error'))
        self.assertIn('連不到推論服務', self.item()['error'])
        self.assertTrue((self.inbox / 'trip.mp4').exists())
        self.fake(FakeService())
        longcut.enqueue('trip.mp4')
        self.assertTrue(wait(lambda: self.item()['state'] == 'done'))

    def test_cancel_while_cutting_cancels_the_remote_job(self):
        service = FakeService(gate=lambda: time.sleep(0.05))
        self.fake(service)
        service.forever = True  # 一直回報進行中
        longcut.enqueue('trip.mp4')
        self.assertTrue(wait(lambda: self.item()['state'] == 'cutting'))
        longcut.cancel('trip.mp4')
        self.assertTrue(wait(lambda: self.item()['state'] == 'cancelled'))
        self.assertTrue(service.cancelled)
        self.assertTrue((self.inbox / 'trip.mp4').exists())

    def test_resume_marks_interrupted_and_requeues_queued(self):
        longcut._write(EMAIL, {'name': 'trip.mp4', 'owner': EMAIL, 'state': 'cutting'})
        with patch.object(longcut, '_ensure_worker'):
            longcut.resume()
        self.assertEqual(self.item()['state'], 'error')
        self.assertIn('重試', self.item()['error'])
        longcut._write(EMAIL, {'name': 'trip.mp4', 'owner': EMAIL, 'state': 'queued'})
        with patch.object(longcut, '_ensure_worker') as worker, patch.object(longcut._queue, 'put') as put:
            longcut.resume()
        put.assert_called_once_with((EMAIL, 'trip.mp4'))
        worker.assert_called_once()

    def test_rejects_paths_outside_the_inbox(self):
        for name in ('../trip.mp4', '', 'missing.mp4'):
            with self.assertRaises(FileNotFoundError):
                longcut.enqueue(name)


if __name__ == '__main__':
    unittest.main()
