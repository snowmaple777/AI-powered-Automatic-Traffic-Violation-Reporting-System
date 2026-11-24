"""串流模式（4bq）：固定幀率重新取樣、分段收錄與依序讀取、從分段剪片、run_pipeline 串流入口。"""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import cv2
import numpy as np

import run_pipeline
from model_library import ModelResult
from model_library.registry import STAGES
from stream_source import (SegmentedCapture, SegmentedFrames, StreamError, StreamProcess, StreamRecorder, input_source,
                           is_stream_url, resample, snap_fps, stream_name, url_fps)
from violation_clips import export_violation_clips

W, H, FPS, COUNT = 160, 120, 10.0, 25


def gray(index):
    return 20 + (index * 9) % 220


def mean(frame):
    return float(frame.mean())


class ResampleTests(unittest.TestCase):
    def run_resample(self, times, fps):
        return [(frame, dup) for frame, dup in resample(((t, i) for i, t in enumerate(times)), fps)]

    def test_same_rate_is_one_to_one(self):
        out = self.run_resample([i / 10 for i in range(10)], 10)
        self.assertEqual([f for f, _ in out], list(range(10)))
        self.assertFalse(any(d for _, d in out))

    def test_gap_repeats_previous_frame(self):
        out = self.run_resample([0, .1, .4, .5], 10)  # 0.2、0.3 秒掉幀
        self.assertEqual([f for f, _ in out], [0, 1, 1, 1, 2, 3])
        self.assertEqual([d for _, d in out], [False, False, True, True, False, False])

    def test_faster_source_drops_frames(self):
        out = self.run_resample([i / 20 for i in range(20)], 10)
        self.assertEqual(len(out), 10)
        self.assertEqual([f for f, _ in out], list(range(0, 20, 2)))


class NamingTests(unittest.TestCase):
    def test_urls_stay_strings_and_get_safe_names(self):
        self.assertTrue(is_stream_url('rtsp://192.168.1.5:554/live/ch0'))
        self.assertFalse(is_stream_url('input/a.mp4'))
        self.assertEqual(input_source('rtsp://192.168.1.5:554/live/ch0'), 'rtsp://192.168.1.5:554/live/ch0')
        self.assertIsInstance(input_source('input/a.mp4'), Path)
        self.assertEqual(stream_name('rtsp://192.168.1.5:554/live/ch0'), 'stream_192_168_1_5_live_ch0')
        self.assertEqual(stream_name(Path('input/a.mp4')), 'a')

    def test_fps_hint_and_snapping(self):
        self.assertEqual(url_fps('http://127.0.0.1:8790/stream.mjpg?fps=30'), 30.0)
        self.assertIsNone(url_fps('http://127.0.0.1:8790/stream.mjpg'))
        self.assertIsNone(url_fps('http://cam/x?fps=abc'))
        self.assertIsNone(url_fps('input/a.mp4'))
        self.assertEqual((snap_fps(29.9), snap_fps(27.7), snap_fps(14.6), snap_fps(37.0)), (30.0, 29.97, 15.0, 37.0))


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.video = self.root / 'source.avi'
        writer = cv2.VideoWriter(str(self.video), cv2.VideoWriter_fourcc(*'MJPG'), FPS, (W, H))
        for i in range(COUNT):
            writer.write(np.full((H, W, 3), gray(i), np.uint8))
        writer.release()

    def tearDown(self):
        self._tmp.cleanup()

    def record(self, **kwargs):
        recorder = StreamRecorder(self.video, self.root / 'seg', segment_seconds=1.0, **kwargs)
        recorder.start()
        recorder.wait_ready(30)
        return recorder

    def read_all(self, recorder):
        frames, source = [], SegmentedFrames(recorder)
        while True:
            ok, frame = source.read()
            if not ok:
                break
            frames.append(frame)
        source.release()
        return frames

    def test_segments_reproduce_frames_in_order(self):
        recorder = self.record()
        frames = self.read_all(recorder)
        recorder.join(30)
        self.assertEqual(recorder.reason, 'source_end')
        self.assertEqual(len(frames), COUNT)
        self.assertEqual([s['frames'] for s in recorder.segments], [10, 10, 5])
        # 預設 FFV1 無損：分段讀出的影格與直接讀原檔逐位元相同
        direct = cv2.VideoCapture(str(self.video))
        for i, frame in enumerate(frames):
            ok, expected = direct.read()
            self.assertTrue(ok and np.array_equal(frame, expected), f'第 {i} 幀')
        direct.release()
        stats = recorder.stats()
        self.assertEqual((stats['frames'], stats['duplicated'], stats['dropped'], stats['fps']), (COUNT, 0, 0, FPS))

    def test_lower_fps_and_max_seconds(self):
        recorder = self.record(fps=5.0, max_seconds=2.0)
        frames = self.read_all(recorder)
        recorder.join(30)
        self.assertEqual(recorder.reason, 'max_seconds')
        self.assertEqual(len(frames), 10)
        for k, frame in enumerate(frames):
            self.assertAlmostEqual(mean(frame), gray(2 * k), delta=6)

    def test_segmented_capture_seeks_across_segments(self):
        recorder = self.record()
        recorder.join(30)
        cap = SegmentedCapture(recorder.folder)
        self.assertEqual((cap.get(cv2.CAP_PROP_FPS), cap.get(cv2.CAP_PROP_FRAME_COUNT)), (FPS, COUNT))
        self.assertTrue(cap.set(cv2.CAP_PROP_POS_FRAMES, 13))
        for i in range(13, COUNT):
            ok, frame = cap.read()
            self.assertTrue(ok)
            self.assertAlmostEqual(mean(frame), gray(i), delta=6)
        self.assertEqual(cap.read(), (False, None))
        cap.release()

    def test_process_recorder_matches_direct_and_exits_cleanly(self):
        recorder = StreamProcess(self.video, self.root / 'proc', segment_seconds=1.0)
        recorder.start()
        recorder.wait_ready(60)
        frames = self.read_all(recorder)
        self.assertEqual(recorder.proc.wait(30), 0)
        direct = cv2.VideoCapture(str(self.video))
        self.assertEqual(len(frames), COUNT)
        for frame in frames:
            self.assertTrue(np.array_equal(frame, direct.read()[1]))
        direct.release()
        stats = recorder.stats()
        self.assertEqual((stats['finished'], stats['reason'], stats['frames']), (True, 'source_end', COUNT))
        recorder.stop()

    def test_process_recorder_honours_stop_file(self):
        flag = self.root / 'stop'
        flag.write_text('stop', encoding='utf-8')
        recorder = StreamProcess(self.video, self.root / 'proc2', segment_seconds=1.0, stop_files=[flag])
        recorder.start()
        recorder.wait_ready(60)
        frames = self.read_all(recorder)
        recorder.stop()
        self.assertEqual(len(frames), 10)  # 每 10 幀檢查一次停止檔
        self.assertEqual(recorder.stats()['reason'], 'stopped')

    def test_simulated_mjpeg_stream_through_process_recorder(self):
        """tools/simulate_stream.py 播出的 MJPEG（沒有時間戳）以網址 fps 參數取得正確時間軸。"""
        import importlib.util
        from http.server import ThreadingHTTPServer
        import threading
        spec = importlib.util.spec_from_file_location('simulate_stream', Path(run_pipeline.__file__).with_name('tools') / 'simulate_stream.py')
        sim = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sim)
        server = ThreadingHTTPServer(('127.0.0.1', 0), sim.make_handler(self.video, FPS, 95, False, 4.0))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        url = f'http://127.0.0.1:{server.server_address[1]}/stream.mjpg?fps={FPS:g}'
        recorder = StreamProcess(url, self.root / 'mjpeg', segment_seconds=1.0)
        recorder.start()
        recorder.wait_ready(60)
        frames = self.read_all(recorder)
        recorder.stop()
        stats = recorder.stats()
        self.assertEqual((stats['timestamps'], stats['fps'], stats['reason']), ('url_fps', FPS, 'source_end'))
        self.assertEqual(len(frames), COUNT)
        for i, frame in enumerate(frames):
            self.assertAlmostEqual(mean(frame), gray(i), delta=6)

    def test_unreachable_stream_reports_error(self):
        recorder = StreamRecorder('rtsp://127.0.0.1:9/none', self.root / 'bad', open_timeout_ms=3000)
        recorder.start()
        with self.assertRaises(StreamError):
            recorder.wait_ready(60)
        recorder.join(30)
        self.assertEqual(recorder.reason, 'error')

    def test_pipeline_stream_mode_and_clips_from_segments(self):
        config = self.root / 'config.json'
        config.write_text('{"version":1,"models":{}}', encoding='utf-8')
        pipeline = Mock(models={}, infer=Mock(return_value={s: ModelResult() for s in STAGES}))
        out = self.root / 'out'
        args = run_pipeline.parse_args([str(self.video), '--stream-input', '--stream-segment-seconds', '1',
            '--model-config', str(config), '--output-dir', str(out), '--device', 'cpu', '--rules', 'all',
            '--no-marking-compensation', '--no-violation-clips'])
        with ExitStack() as resources:
            timings = run_pipeline.run(args, resources, pipeline=pipeline)
        self.assertEqual(timings['frames'], COUNT)
        self.assertEqual(timings['completion']['status'], 'stream')
        self.assertEqual(timings['completion']['termination'], 'stream_end')
        self.assertEqual(timings['stream']['frames'], COUNT)
        records = [json.loads(l) for l in (out / 'source_detections.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([r['frame']['index'] for r in records], list(range(COUNT)))
        self.assertAlmostEqual(records[-1]['frame']['timestamp_sec'], (COUNT - 1) / FPS, places=3)
        event = dict(event_id='e1', object_id='vehicle-1', frame=12, timestamp_sec=1.2, status='suspected',
                     rule_id='red_light_stop_line_crossing', evidence={})
        clips = export_violation_clips(out / 'source_stream', out / 'source_detections.jsonl', [event],
                                       out / 'clips', 0.5, 0.5)
        self.assertEqual((clips[0]['start_frame'], clips[0]['end_frame'], clips[0]['frame_count']), (7, 17, 11))

    def test_ctrl_c_ends_stream_gracefully(self):
        config = self.root / 'config.json'
        config.write_text('{"version":1,"models":{}}', encoding='utf-8')
        results = {s: ModelResult() for s in STAGES}
        pipeline = Mock(models={}, infer=Mock(side_effect=[results] * 4 + [KeyboardInterrupt()]))
        out = self.root / 'out'
        args = run_pipeline.parse_args([str(self.video), '--stream-input', '--model-config', str(config),
            '--output-dir', str(out), '--device', 'cpu', '--rules', 'all', '--no-marking-compensation'])
        with ExitStack() as resources:
            timings = run_pipeline.run(args, resources, pipeline=pipeline)
        self.assertEqual((timings['frames'], timings['completion']['termination']), (4, 'interrupted'))
        self.assertEqual(len((out / 'source_detections.jsonl').read_text(encoding='utf-8').splitlines()), 4)
        self.assertTrue((out / 'source_timings.json').is_file())


if __name__ == '__main__':
    unittest.main()
