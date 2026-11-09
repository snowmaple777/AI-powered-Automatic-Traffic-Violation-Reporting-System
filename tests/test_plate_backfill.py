"""車牌補充不能借用別台車、快取框或中斷軌跡，三個入口須一致。"""
from copy import deepcopy
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from plate_backfill import PlateBackfill, event_plate_text
from eval.metrics import evaluate
from violation_visualization import event_details, format_event_summary


def vehicle(i, text=None, oid="v1", box=None, confidence=.95, fresh=True):
    return dict(object_id=oid, bbox_xyxy=box or [40+i, 40, 100+i, 100],
                source_frame=i if fresh else i-1, plate_text=text, text_confidence=confidence)


def observation(i, *vehicles):
    return {"frame": dict(index=i, timestamp_sec=round(i*.1, 3), width=320, height=240, fps=10),
            "observations": {"vehicles": list(vehicles)}}


def event(text=None):
    return dict(event_id="test:e1", object_id="v1", rule_id="crosswalk_stopping", status="suspected",
                conditions={"test": True}, met_conditions=["test"], missing_conditions=[],
                frame=0, timestamp_sec=0., merged_events=[{"event_id":"child"}],
                evidence={"vehicle":vehicle(0,text)})


def sequence(text="ABC-1234"):
    return [observation(i, vehicle(i,text if i>=2 else None)) for i in range(11)]


def collect(rows, e=None, seconds=1, delayed=False):
    e = e or event()
    with PlateBackfill(seconds) as processor:
        result=[]
        for i,r in enumerate(rows):
            result += processor.update(r, [e] if i==0 and not delayed else [])
        result += processor.finalize([e] if delayed else [])
    return result[0]


class PlateBackfillTests(unittest.TestCase):
    def test_positive_preserves_all_original_fields_and_input(self):
        e=event();before=deepcopy(e);out=collect(sequence(),e)
        self.assertEqual({k:v for k,v in out.items() if k!="plate"},before)
        self.assertEqual(e,before)
        self.assertEqual(out['plate']['text'],'ABC-1234')
        self.assertEqual(out['plate']['observed_frame'],2)
        self.assertEqual(out['plate']['delay_seconds'],.2)
        self.assertNotIn('plate',out['merged_events'][0])

    def test_disabled_has_no_history_or_delay(self):
        with PlateBackfill() as p:
            self.assertIsNone(p.db)
            out=p.update(observation(0),[event()])
            self.assertEqual(out[0]['plate']['source'],'none')
            self.assertEqual(out[0]['plate']['window_seconds'],0)

    def test_delayed_engine_event_uses_original_window(self):
        self.assertEqual(collect(sequence()),collect(sequence(),delayed=True))

    def test_id_change_never_transfers_text(self):
        rows=[observation(0,vehicle(0))]+[observation(i,vehicle(i,'ABC-1234',oid='v2')) for i in range(1,11)]
        self.assertIsNone(collect(rows)['plate']['text'])

    def test_box_jump_rejects_candidate(self):
        rows=[observation(0,vehicle(0))]+[observation(i,vehicle(i,'ABC-1234',box=[200,40,260,100])) for i in range(1,11)]
        p=collect(rows)['plate'];self.assertIsNone(p['text'])
        self.assertIn('track_jump',{r['reason'] for r in p['rejected']})

    def test_conflict_does_not_overwrite_original_and_is_visible(self):
        out=collect(sequence('XYZ-9876'),event('ABC-1234'));p=out['plate']
        self.assertEqual(p['text'],'ABC-1234');self.assertTrue(p['conflict'])
        self.assertEqual(p['source'],'event_frame')
        self.assertIn('XYZ-9876',format_event_summary(out))
        self.assertIn('ABC-1234',format_event_summary(out))

    def test_shared_text_later_in_window_revokes_candidate(self):
        rows=sequence();rows[8]['observations']['vehicles'].append(vehicle(8,'ABC1234',oid='v2'))
        p=collect(rows)['plate'];self.assertIsNone(p['text'])
        self.assertIn('shared_with_other_track',{r['reason'] for r in p['rejected']})

    def test_gap_rejects_read_after_track_resumes(self):
        rows=[observation(i,*([vehicle(i,'ABC-1234' if i>=6 else None)] if i==0 or i>=6 else [])) for i in range(11)]
        p=collect(rows,delayed=True)['plate'];self.assertIsNone(p['text'])
        self.assertIn('track_gap',{r['reason'] for r in p['rejected']})

    def test_early_gap_does_not_publish_unchecked_candidate(self):
        rows=[observation(i,*([vehicle(i,'ABC-1234' if i>=1 else None)] if i<3 else [])) for i in range(11)]
        self.assertIsNone(collect(rows)['plate']['text'])
        self.assertIsNone(collect(rows,delayed=True)['plate']['text'])

    def test_cached_vehicle_text_is_not_a_read(self):
        rows=[observation(0,vehicle(0))]+[observation(i,vehicle(i,'ABC-1234',fresh=False)) for i in range(1,11)]
        self.assertIsNone(collect(rows)['plate']['text'])

    def test_low_confidence_is_rejected(self):
        rows=[observation(i,vehicle(i,'ABC-1234',confidence=.4)) for i in range(11)]
        p=collect(rows)['plate'];self.assertIsNone(p['text'])
        self.assertIn('low_confidence',{r['reason'] for r in p['rejected']})

    def test_two_conflicting_new_candidates_are_not_resolved_by_guessing(self):
        rows=sequence();rows[8]['observations']['vehicles'][0]['plate_text']='XYZ-9876'
        p=collect(rows)['plate'];self.assertIsNone(p['text']);self.assertFalse(p['conflict'])
        self.assertIn('plate_conflict',{r['reason'] for r in p['rejected']})

    def test_after_window_is_diagnostic_only(self):
        rows=[observation(i,vehicle(i,'ABC-1234' if i==11 else None)) for i in range(12)]
        p=collect(rows,delayed=True)['plate'];self.assertIsNone(p['text'])
        self.assertEqual(p['rejected'],[dict(text='ABC-1234',frame=11,reason='after_window')])

    def test_finalize_uses_available_prefix(self):
        self.assertEqual(collect(sequence()[:4])['plate']['text'],'ABC-1234')

    def test_normalized_original_plate_is_not_a_conflict(self):
        p=collect(sequence('ABC1234'),event('ABC-1234'))['plate']
        self.assertFalse(p['conflict']);self.assertEqual(p['text'],'ABC-1234')

    def test_invalid_window_and_non_monotonic_input(self):
        for seconds in (-1,float('nan'),float('inf')):
            with self.assertRaises(ValueError):PlateBackfill(seconds)
        with PlateBackfill(1) as p:
            p.update(observation(1))
            with self.assertRaises(ValueError):p.update(observation(1))

    def test_consumers_prefer_new_field_and_fallback_on_legacy(self):
        legacy=event('OLD-1234');self.assertEqual(event_plate_text(legacy),'OLD-1234')
        out=collect(sequence());self.assertIn('事件後 0.2 秒',format_event_summary(out))
        self.assertEqual(event_details(out,vehicle(2,'WRONG-123'))['plate'],'ABC-1234')
        out['plate']['text']=None
        self.assertIsNone(event_plate_text(out))

    def test_metrics_report_original_and_augmented_separately(self):
        truth={'videos':{'v':{'reviewed':True,'events':[{'rule_id':'crosswalk_stopping','start_sec':0,'end_sec':1,'plate':'ABC1234'}]}}}
        predictions={'v':[collect(sequence())]}
        self.assertEqual(evaluate(truth,predictions)[2:],(1,1))
        self.assertEqual(evaluate(truth,predictions,plate_at_event=True)[2:],(1,0))


class EntryPointTests(unittest.TestCase):
    def test_online_and_both_replay_entries_are_identical(self):
        import cv2
        import numpy as np
        import run_pipeline
        import evaluate_violations
        from frame_processing import FrameProcessor
        from model_library.base import ModelResult
        from model_library.registry import STAGES
        from tools.rule_eval import run_rules
        def engine_factory(*args):
            return Mock(rules=[], evaluate=lambda r:[event()] if r['frame']['index']==0 else [], finalize=lambda:[])
        process=FrameProcessor.process
        def inject(self, image, index, timestamp, results, **kwargs):
            result=process(self,image,index,timestamp,results,**kwargs)
            result.record['observations']['vehicles']=sequence()[index]['observations']['vehicles']
            result.record['observations']['rule_vehicles']=result.record['observations']['vehicles']
            return result
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'clip.mp4';source.write_bytes(b'fake')
            config=root/'config.json';config.write_text('{"version":1,"models":{}}')
            cap=Mock();cap.isOpened.return_value=True
            cap.get.side_effect=lambda k:{cv2.CAP_PROP_FPS:10,cv2.CAP_PROP_FRAME_WIDTH:320,
                cv2.CAP_PROP_FRAME_HEIGHT:240,cv2.CAP_PROP_FRAME_COUNT:11}.get(k,0)
            cap.read.side_effect=[(True,np.zeros((240,320,3),np.uint8))]*11+[(False,None)]
            pipeline=Mock(models={},infer=Mock(return_value={s:ModelResult() for s in STAGES}))
            args=run_pipeline.parse_args([str(source),'--model-config',str(config),'--output-dir',str(root/'online'),
                '--device','cpu','--rules','all','--no-violation-clips','--plate-backfill-seconds','1'])
            with patch('run_pipeline.cv2.VideoCapture',return_value=cap),patch.object(FrameProcessor,'process',inject), \
                 patch('run_pipeline.ViolationEngine',side_effect=engine_factory),ExitStack() as resources:
                run_pipeline.run(args,resources,pipeline=pipeline)
            jsonl=root/'online/clip_detections.jsonl'
            with patch('tools.rule_eval.ViolationEngine',side_effect=engine_factory):
                events=run_rules(jsonl,root/'rules','clip',plate_backfill_seconds=1)
            with patch('evaluate_violations.ViolationEngine',side_effect=engine_factory),patch('sys.argv',
                ['evaluate_violations.py',str(jsonl),'--output',str(root/'evaluated.jsonl'),'--rules','all',
                 '--no-violation-clips','--plate-backfill-seconds','1']):
                evaluate_violations.main()
            online=[json.loads(l) for l in (root/'online/clip_violations.jsonl').read_text(encoding='utf-8').splitlines()]
            evaluated=[json.loads(l) for l in (root/'evaluated.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(online,events);self.assertEqual(events,evaluated)
            self.assertEqual(events[0]['plate']['source'],'same_track_after_event')


if __name__=='__main__':unittest.main()
