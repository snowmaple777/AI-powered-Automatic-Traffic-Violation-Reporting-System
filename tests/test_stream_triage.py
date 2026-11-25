"""串流降頻篩選（4bs）：觸發 → 深度分析區間的合併與等待，以及 run_pipeline 的兩段式流程。"""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

import run_pipeline
from model_library import ModelPipeline, ModelResult
from model_library.registry import STAGES
from stream_triage import DeepWindows, held_events

W, H, FPS, COUNT = 160, 120, 10.0, 25


class DeepWindowsTests(unittest.TestCase):
    def test_triggers_merge_and_wait_for_hold(self):
        w = DeepWindows(10.0, before=1.0, after=0.5, hold=2.0)
        w.add(3.0)   # 20..35
        w.add(3.8)   # 28..43 → 與前一段合併
        w.add(10.0)  # 90..105
        self.assertEqual(w.pending, [[20, 43], [90, 105]])
        self.assertEqual(w.due(63), [])              # 43 + 20 幀等待 → 位置要超過 63
        self.assertEqual(w.due(64), [(20, 43)])
        self.assertEqual(w.pending, [[90, 105]])

    def test_analysed_frames_are_not_redone(self):
        w = DeepWindows(10.0, before=1.0, after=0.5, hold=0.0)
        w.add(3.0)
        (a, b), = w.due(100)
        w.finish(a, b)
        w.add(3.2)                                   # 22..37，已分析到 35 → 只剩 36..37
        self.assertEqual(w.pending, [[36, 37]])
        w.add(2.0)                                   # 10..25 全在已分析範圍內
        self.assertEqual((w.triggers, w.skipped), (3, 1))

    def test_final_flush_clips_to_last_frame_and_start_at_zero(self):
        w = DeepWindows(10.0, before=8.0, after=6.0)
        w.add(1.0)
        self.assertEqual(w.due(40), [])
        self.assertEqual(w.due(40, final=True), [(0, 39)])

    def test_window_starts_before_trigger_vehicle_first_seen(self):
        w = DeepWindows(10.0, before=8.0, after=6.0, hold=0.0, max_before=20.0)
        w.add(30.0, first_seen=15.0)                 # 首次出現前 1 秒 → 14 秒
        self.assertEqual(w.pending, [[140, 360]])
        w = DeepWindows(10.0, before=8.0, after=6.0, hold=0.0, max_before=20.0)
        w.add(30.0, first_seen=2.0)                  # 最多從觸發前 20 秒
        w.add(60.0, first_seen=55.0)                 # 比 before 晚出現 → 照 before
        self.assertEqual(w.pending, [[100, 360], [520, 660]])
        self.assertEqual(DeepWindows(10.0, before=8.0, max_before=3.0).max_before, 8.0)

    def test_held_events_reads_pending_candidates_without_changing_state(self):
        red = Mock(pending_events={'vehicle-000001': {'event_id': 'r1'}}, spec=['pending_events'])
        area = Mock(pending_confirmations=[{'event_id': 'a1'}], spec=['pending_confirmations'])
        line = Mock(dwell_pending=[{'event_id': 'd1'}], spec=['dwell_pending'])
        engine = Mock(rules=[red, area, line], pending=[(1.0, {'event_id': 'w1'})])
        self.assertEqual([e['event_id'] for e in held_events(engine)], ['r1', 'a1', 'd1', 'w1'])
        self.assertEqual(len(red.pending_events), 1)

    def test_model_pipeline_skip_leaves_empty_results(self):
        pipeline = ModelPipeline.__new__(ModelPipeline)
        vehicles, plates = Mock(), Mock()
        vehicles.infer.return_value = ModelResult(records=[{'object_id': 'vehicle-000001'}])
        pipeline.models = {'vehicles': vehicles, 'plates': plates}
        results = pipeline.infer(None, None, skip=('plates',))
        plates.infer.assert_not_called()
        self.assertEqual((len(results['vehicles'].records), results['plates'].records), (1, []))

    def test_invalid_settings(self):
        with self.assertRaises(ValueError):
            DeepWindows(0)
        with self.assertRaises(ValueError):
            DeepWindows(10, before=-1)


class FakeRule:
    """篩選與深度分析各建一個：第一個時間 ≥ at 的幀產生一筆疑似事件。"""
    rule_id = 'red_light_stop_line_crossing'

    def __init__(self, at, rule_id=None):
        self.at, self.fired, self.diagnostics = at, False, {}
        self.rule_id = rule_id or self.rule_id

    def _event(self, frame):
        return dict(event_id=f'{self.rule_id}-000001', rule_id=self.rule_id, status='suspected', frame=frame['index'],
                    timestamp_sec=frame['timestamp_sec'], object_id='vehicle-000001', conditions={}, evidence={})

    def evaluate(self, observation):
        frame = observation['frame']
        if self.fired or frame['timestamp_sec'] < self.at - 1e-9:
            return []
        self.fired = True
        return [self._event(frame)]

    def finalize(self):
        return []


class HeldRule(FakeRule):
    """像闖紅燈的疑似事件：成立後放在 pending_events，片尾 finalize 才輸出。"""

    def __init__(self, at, rule_id=None):
        super().__init__(at, rule_id)
        self.pending_events = {}

    def evaluate(self, observation):
        for event in super().evaluate(observation):
            self.pending_events[event['object_id']] = event
        return []

    def finalize(self):
        events, self.pending_events = list(self.pending_events.values()), {}
        return events


class TriagePipelineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.video = self.root / 'source.avi'
        writer = cv2.VideoWriter(str(self.video), cv2.VideoWriter_fourcc(*'MJPG'), FPS, (W, H))
        for i in range(COUNT):
            writer.write(np.full((H, W, 3), 20 + i * 8, np.uint8))
        writer.release()
        self.config = self.root / 'config.json'
        self.config.write_text('{"version":1,"models":{}}', encoding='utf-8')
        self.out = self.root / 'out'

    def tearDown(self):
        self._tmp.cleanup()

    def args(self, *extra):
        return run_pipeline.parse_args([str(self.video), '--stream-input', '--stream-segment-seconds', '1',
            '--model-config', str(self.config), '--output-dir', str(self.out), '--device', 'cpu',
            '--no-marking-compensation', '--stream-triage-fps', '2', *extra])

    def test_triage_trigger_runs_deep_window_at_full_rate(self):
        pipeline = Mock(models={}, infer=Mock(return_value={s: ModelResult() for s in STAGES}))
        args = self.args('--rules', 'all', '--stream-triage-before', '0.5', '--stream-triage-after', '0.5')
        with patch.object(run_pipeline, 'create_rules', lambda names, context, options: [FakeRule(1.0)]), \
                ExitStack() as resources:
            timings = run_pipeline.run(args, resources, pipeline=pipeline)
        triage = timings['triage']
        self.assertEqual((timings['frames'], triage['frames'], triage['fps']), (COUNT, 5, 2.0))
        self.assertEqual((triage['triggers'], triage['deep_frames'], triage['windows_sec']), (1, 11, [[0.5, 1.6]]))
        # 篩選：取樣序號＋原幀號
        tri = [json.loads(l)['frame'] for l in (self.out / 'source_triage_detections.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([(f['index'], f['stream_frame']) for f in tri], [(i, 5 * i) for i in range(5)])
        # 深度分析：原幀率、原幀號，事件只來自深度分析
        deep = [json.loads(l)['frame'] for l in (self.out / 'source_detections.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([f['index'] for f in deep], list(range(5, 16)))
        self.assertEqual(deep[0]['fps'], FPS)
        events = [json.loads(l) for l in (self.out / 'source_violations.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([(e['frame'], e['timestamp_sec']) for e in events], [(10, 1.0)])
        self.assertIn('plate', events[0])
        clips = list((self.out / 'source_violation_clips').glob('run_*/*.mp4'))
        self.assertEqual(len(clips), 1)
        self.assertEqual(pipeline.reset.call_count, 3)  # run 開始、深度分析前後

    def test_slow_stream_falls_back_to_every_frame(self):
        pipeline = Mock(models={}, infer=Mock(return_value={s: ModelResult() for s in STAGES}))
        args = run_pipeline.parse_args([str(self.video), '--stream-input', '--stream-segment-seconds', '1',
            '--model-config', str(self.config), '--output-dir', str(self.out), '--device', 'cpu',
            '--no-marking-compensation', '--rules', 'all', '--no-violation-clips', '--stream-triage-fps', '8'])
        with ExitStack() as resources:
            timings = run_pipeline.run(args, resources, pipeline=pipeline)
        self.assertNotIn('triage', timings)
        self.assertEqual(timings['frames'], COUNT)

    def test_held_candidates_trigger_when_they_appear(self):
        # 4bt：保留到片尾才輸出的候選（HeldRule）若等到 finalize 才觸發，時段早已分析過、會被略過；改成出現當下就觸發
        pipeline = Mock(models={}, infer=Mock(return_value={s: ModelResult() for s in STAGES}))
        args = self.args('--rules', 'all', '--stream-triage-before', '0.5', '--stream-triage-after', '0.5')
        rules = lambda names, context, options: [HeldRule(0.5), FakeRule(1.5, 'double_yellow_line_crossing')]
        with patch.object(run_pipeline, 'create_rules', rules), patch.object(run_pipeline, 'HOLD_SECONDS', 0.2), \
                ExitStack() as resources:
            timings = run_pipeline.run(args, resources, pipeline=pipeline)
        triage = timings['triage']
        self.assertEqual((triage['triggers'], triage['skipped_triggers']), (2, 0))
        # 等到片尾才觸發的話，0.5 秒的時段在 1.5 秒那段分析完後才出現，會被略過（skipped_triggers＝1）
        self.assertEqual(triage['windows_sec'], [[0.0, 1.1], [1.1, 2.1]])
        rows = [json.loads(l) for l in (self.out / 'source_triage_violations.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([(r['rule_id'], r['triage_trigger']) for r in rows],
                         [('red_light_stop_line_crossing', 'held'), ('double_yellow_line_crossing', 'event')])
        events = [json.loads(l) for l in (self.out / 'source_violations.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertIn(('red_light_stop_line_crossing', 5), [(e['rule_id'], e['frame']) for e in events])
        self.assertIn(('double_yellow_line_crossing', 15), [(e['rule_id'], e['frame']) for e in events])

    def test_triage_skips_models_rules_do_not_need(self):
        results = {s: ModelResult() for s in STAGES}
        for extra, expected in (((), ('plates', 'ocr')), (('--stream-triage-all-models',), None)):
            pipeline = Mock(models={'plates': Mock(), 'ocr': Mock()}, infer=Mock(return_value=results))
            args = self.args('--rules', 'all', '--no-violation-clips', '--stream-triage-before', '0.5',
                             '--stream-triage-after', '0.5', *extra)
            with patch.object(run_pipeline, 'create_rules', lambda names, context, options: [FakeRule(1.0)]), \
                    ExitStack() as resources:
                timings = run_pipeline.run(args, resources, pipeline=pipeline)
            skips = [call.kwargs.get('skip') for call in pipeline.infer.call_args_list]
            # 篩選 5 幀略過車牌／OCR，深度分析 11 幀照常跑全部模型
            self.assertEqual(skips, [expected] * 5 + [None] * 11 if expected else [None] * 16)
            self.assertEqual(timings['triage']['skipped_stages'], list(expected or ()))

    def test_requires_rules(self):
        with self.assertRaises(SystemExit):
            with ExitStack() as resources:
                run_pipeline.run(self.args(), resources, pipeline=Mock(models={}))
        args = run_pipeline.parse_args([str(self.video), '--model-config', str(self.config), '--rules', 'all',
                                        '--output-dir', str(self.out), '--triage-fps', '2', '--save-video'])
        with self.assertRaises(SystemExit):
            with ExitStack() as resources:
                run_pipeline.run(args, resources, pipeline=Mock(models={}))

    def test_file_input_triage_reads_deep_windows_from_the_file(self):
        # 4bu 長片切段：檔案輸入不必收錄分段；深度分析另開只往前讀的讀取器，片段可輸出原始畫面
        pipeline = Mock(models={}, infer=Mock(return_value={s: ModelResult() for s in STAGES}))
        args = run_pipeline.parse_args([str(self.video), '--model-config', str(self.config), '--rules', 'all',
            '--output-dir', str(self.out), '--device', 'cpu', '--no-marking-compensation', '--triage-fps', '2',
            '--stream-triage-before', '0.5', '--stream-triage-after', '0.5', '--clip-raw',
            '--clip-before', '0.3', '--clip-after', '0.3'])
        with patch.object(run_pipeline, 'create_rules', lambda names, context, options: [FakeRule(1.0)]), \
                ExitStack() as resources:
            timings = run_pipeline.run(args, resources, pipeline=pipeline)
        self.assertNotIn('stream', timings)
        self.assertEqual(timings['completion']['status'], 'complete')
        self.assertEqual((timings['triage']['frames'], timings['triage']['windows_sec']), (5, [[0.5, 1.6]]))
        self.assertFalse((self.out / 'source_stream').exists())
        deep = [json.loads(l)['frame']['index'] for l in
                (self.out / 'source_detections.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(deep, list(range(5, 16)))
        # 深度分析讀到的是正確的幀：第 i 幀的灰階是 20 + 8i（MJPG 有損，容許誤差）
        frames = [call.args[0] for call in pipeline.infer.call_args_list[5:]]
        self.assertTrue(all(abs(float(f.mean()) - (20 + 8 * i)) < 6 for f, i in zip(frames, range(5, 16))))
        clip = json.loads(next((self.out / 'source_violation_clips').glob('run_*/index.json')).read_text(encoding='utf-8'))
        self.assertEqual((len(clip), clip[0]['annotated'], clip[0]['start_frame']), (1, False, 7))


if __name__ == '__main__':
    unittest.main()
