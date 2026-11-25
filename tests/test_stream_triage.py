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
from model_library import ModelResult
from model_library.registry import STAGES
from stream_triage import DeepWindows

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

    def test_invalid_settings(self):
        with self.assertRaises(ValueError):
            DeepWindows(0)
        with self.assertRaises(ValueError):
            DeepWindows(10, before=-1)


class FakeRule:
    """篩選與深度分析各建一個：第一個時間 ≥ at 的幀產生一筆疑似事件。"""
    rule_id = 'red_light_stop_line_crossing'

    def __init__(self, at):
        self.at, self.fired, self.diagnostics = at, False, {}

    def evaluate(self, observation):
        frame = observation['frame']
        if self.fired or frame['timestamp_sec'] < self.at - 1e-9:
            return []
        self.fired = True
        return [dict(event_id='rlsl-000001', rule_id=self.rule_id, status='suspected', frame=frame['index'],
                     timestamp_sec=frame['timestamp_sec'], object_id='vehicle-000001', conditions={}, evidence={})]

    def finalize(self):
        return []


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

    def test_requires_rules_and_stream_mode(self):
        with self.assertRaises(SystemExit):
            with ExitStack() as resources:
                run_pipeline.run(self.args(), resources, pipeline=Mock(models={}))
        args = run_pipeline.parse_args([str(self.video), '--model-config', str(self.config), '--rules', 'all',
                                        '--output-dir', str(self.out), '--stream-triage-fps', '2'])
        with self.assertRaises(SystemExit):
            with ExitStack() as resources:
                run_pipeline.run(args, resources, pipeline=Mock(models={}))


if __name__ == '__main__':
    unittest.main()
