from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from violation_clips import export_violation_clips, validate_clip_options
from violation_visualization import (STATUS_COLORS, draw_violation_candidates,
                                     event_details, format_event_summary)


class ViolationClipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / 'source.mp4'
        self.data = self.root / 'source_detections.jsonl'
        self.vehicle = dict(object_id='vehicle-1', bbox_xyxy=[100, 100, 200, 210],
                            plate_text='ABC-1234', class_id=2, class_name='car',
                            track_id=1, confidence=.9, track_age_frames=30,
                            center=[150, 155], bottom_center=[150, 210])
        writer = cv2.VideoWriter(str(self.video), cv2.VideoWriter_fourcc(*'mp4v'), 10., (320, 240))
        self.assertTrue(writer.isOpened())
        with self.data.open('w', encoding='utf-8') as output:
            for index in range(20):
                writer.write(np.zeros((240, 320, 3), dtype=np.uint8))
                output.write(json.dumps(dict(
                    frame=dict(index=index, timestamp_sec=index / 10, fps=10., width=320, height=240),
                    observations=dict(vehicles=[self.vehicle]))) + '\n')
        writer.release()

    def event(self, status='suspected', index=1):
        return dict(event_id='event-1', object_id='vehicle-1', frame=index,
                    timestamp_sec=index / 10, status=status,
                    rule_id='red_light_stop_line_crossing', evidence={'vehicle': self.vehicle})

    def test_windows_colors_overlaps_and_end_boundaries(self):
        clips = export_violation_clips(self.video, self.data,
            [self.event('suspected', 1), self.event('confirmed', 18)], self.root / 'clips', 1, 1)
        self.assertEqual([(c['start_frame'], c['end_frame']) for c in clips], [(0, 11), (8, 19)])
        for clip in clips:
            cap = cv2.VideoCapture(clip['clip_path'])
            decoded = []
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                decoded.append(frame)
            cap.release()
            self.assertEqual(len(decoded), clip['frame_count'])
            self.assertEqual(len(decoded), 12)
            # Check actual encoded video border, allowing lossy MP4 compression.
            color = STATUS_COLORS[clip['event']['status']]
            self.assertTrue(np.allclose(decoded[0][155, 100], color, atol=35))
            self.assertTrue(Path(clip['clip_path']).with_suffix('.json').is_file())
            summary = Path(clip['clip_path']).with_suffix('.txt').read_text(encoding='utf-8-sig')
            self.assertIn('車輛編號：vehicle-1', summary)
            self.assertIn('車牌號碼：ABC-1234', summary)
            self.assertIn('違規理由：', summary)

    def test_summary_preserves_reason_and_plate_on_track_loss(self):
        event = self.event()
        event['description'] = '已越過停止線，尚缺紅燈時序證據。'
        event['object_id'] = 'rider-group-9'
        event['evidence']['vehicle'] = dict(self.vehicle, source_object_id='vehicle-1')
        info = event_details(event)
        self.assertEqual(info['vehicle_id'], 'vehicle-1')
        self.assertEqual(info['plate'], 'ABC-1234')
        self.assertEqual(info['reason'], event['description'])
        self.assertIn('疑似違規', format_event_summary(event))
        event['evidence']['vehicle']['plate_text'] = None
        self.assertEqual(event_details(event)['plate'], '未辨識')

    def test_empty_events_and_invalid_durations(self):
        target = self.root / 'empty'
        self.assertEqual(export_violation_clips(self.video, self.data, [], target), [])
        self.assertFalse(target.exists())
        for value in (-1, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                validate_clip_options(value, 5)

    def test_missing_track_does_not_reuse_old_box_and_group_resolves(self):
        event = self.event()
        frame = np.zeros((240, 320, 3), dtype=np.uint8)
        draw_violation_candidates(frame, [], [event], .2, hold_seconds=None, frame_index=2)
        self.assertEqual(frame[155, 100].tolist(), [0, 0, 0])
        event['object_id'] = 'rider-group-1'
        event['evidence']['vehicle'] = dict(self.vehicle, source_object_id='vehicle-1')
        draw_violation_candidates(frame, [self.vehicle], [event], .2, hold_seconds=None, frame_index=2)
        self.assertEqual(frame[155, 100].tolist(), list(STATUS_COLORS['suspected']))

    def test_finalize_suspected_event_is_exported_without_save_video(self):
        import run_pipeline
        from model_library.base import ModelResult
        from model_library.registry import STAGES

        config = self.root / 'config.json'
        config.write_text('{"version": 1, "models": {}}', encoding='utf-8')
        result = {name: ModelResult() for name in STAGES}
        result['vehicles'] = ModelResult([self.vehicle])
        mask = np.zeros((240, 320), dtype=np.uint8)
        mask[30:220, 70:73], mask[30:220, 78:81] = 8, 8
        result['road_markings'] = ModelResult([
            dict(class_id=8, class_name='solid double yellow', components=[])], {'mask': mask})
        pipeline = Mock(models={})
        pipeline.infer.return_value = result
        rule = Mock(rule_id='test', diagnostics={})
        rule.evaluate.return_value = []
        rule.finalize.return_value = [self.event('suspected', 1)]
        output = self.root / 'pipeline'
        with patch('sys.argv', ['run_pipeline.py', str(self.video), '--model-config', str(config),
                               '--output-dir', str(output), '--no-marking-compensation', '--rules', 'all',
                               '--max-frames', '5']), \
             patch('run_pipeline.ModelPipeline') as factory, \
             patch('run_pipeline.create_rules', return_value=[rule]):
            factory.return_value.__enter__.return_value = pipeline
            with ExitStack() as resources:
                run_pipeline.run(run_pipeline.parse_args(), resources)
        clips = list(output.glob('source_violation_clips/run_*/*.mp4'))
        self.assertEqual(len(clips), 1)
        metadata = json.loads(clips[0].with_suffix('.json').read_text(encoding='utf-8'))
        self.assertEqual(metadata['event']['status'], 'suspected')
        self.assertEqual(metadata['frame_count'], 5)
        self.assertFalse((output / 'source_annotated.mp4').exists())
        records = [json.loads(line) for line in
                   (output / 'source_detections.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(records), 5)
        self.assertTrue(all(len(record['observations']['double_lines']) == 1 for record in records))
        self.assertIn('double_line', (output / 'source_detections.csv').read_text(encoding='utf-8-sig'))

    def test_double_line_event_contract_exports_clip_and_summary(self):
        from violation_engine import create_rules, RuleContext
        rule = create_rules(['double_white_line_crossing'], RuleContext('source', 10, 320, 240))[0]
        event = rule.build_event({'index': 1, 'timestamp_sec': .1}, self.vehicle,
            {'class_name': 'solid double white', 'track_id': 'double-line-1',
             'centerline': [[60, 20], [60, 220]]},
            status='suspected', conditions={'crossing_observed': False})
        clips = export_violation_clips(self.video, self.data, [event], self.root / 'double_clips', .1, .1)
        self.assertEqual(len(clips), 1)
        summary = Path(clips[0]['clip_path']).with_suffix('.txt').read_text(encoding='utf-8-sig')
        self.assertIn('跨越雙白線', summary)
        self.assertIn('ABC-1234', summary)
        self.assertIn('vehicle-1', summary)


if __name__ == '__main__':
    unittest.main()
