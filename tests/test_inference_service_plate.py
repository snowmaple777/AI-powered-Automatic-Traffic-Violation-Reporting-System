"""推論服務顯示的車牌須與 plate_backfill.event_plate_text 一致（補充車牌優先，null 不退回）。"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from inference_service.jobs import Manager, event_plate
from plate_backfill import event_plate_text

AT_EVENT = {'vehicle': {'plate_text': 'ABC-1234', 'text_confidence': 0.7}}
CASES = {
    'backfilled': dict(evidence={'vehicle': {'plate_text': None}},
                       plate={'text': 'XYZ-9999', 'source': 'same_track_after_event', 'text_confidence': 0.93}),
    'conflict_null': dict(evidence=AT_EVENT, plate={'text': None, 'source': 'none', 'text_confidence': None}),
    'legacy': dict(evidence=AT_EVENT),
}


class InferenceServicePlateTests(unittest.TestCase):
    def test_matches_backfill_contract(self):
        for name, ev in CASES.items():
            with self.subTest(name):
                self.assertEqual(event_plate(ev)[0], event_plate_text(ev))
        self.assertEqual(event_plate(CASES['backfilled']), ('XYZ-9999', 0.93))
        self.assertEqual(event_plate(CASES['legacy']), ('ABC-1234', 0.7))

    def test_package_uses_backfilled_plate(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            out_dir = tmp / 'outputs'
            out_dir.mkdir()
            ev = dict(CASES['backfilled'], event_id='e1', rule_id='r', status='suspected',
                      timestamp_sec=3.0, frame=None, conditions={'a': True})
            (out_dir / 'clip_violations.jsonl').write_text(json.dumps(ev) + '\n', encoding='utf-8')
            job = {'id': 'j', 'dir': str(tmp), 'input_path': str(tmp / 'missing.mp4'),
                   'options': {'clip_before': 5.0, 'clip_after': 5.0}}
            result = Manager._package(SimpleNamespace(ffmpeg=None), job, out_dir, 'clip')
        item, = result['events']
        self.assertEqual((item['plate_text'], item['plate_confidence']), ('XYZ-9999', 0.93))


if __name__ == '__main__':
    unittest.main()
