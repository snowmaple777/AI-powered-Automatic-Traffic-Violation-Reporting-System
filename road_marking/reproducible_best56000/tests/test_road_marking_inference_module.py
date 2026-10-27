"""Temporal boundaries, transparent background, opacity and gate safety (CPU)."""
import copy,json,sys
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import unittest
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from road_marking_core import MajorityVote,EventTracker,load_settings,verification_settings,signature,require_verification
from road_marking_overlay import load_palette,render


class TestRoadMarkingInference(unittest.TestCase):
    def test_frame_folder_natural_order_and_explicit_fps(self):
        from PIL import Image
        from road_marking_infer import FrameSource
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);folder=Path(temporary.name)
        for name in ['frame_10.png','frame_2.png','frame_1.png']:Image.new('RGB',(16,12)).save(folder/name)
        with self.assertRaisesRegex(ValueError,'FPS|fps'):FrameSource(folder)
        source=FrameSource(folder,30)
        assert [p.name for p in source.files]==['frame_1.png','frame_2.png','frame_10.png']
        assert [(i,name) for i,name,frame in source]==[(0,'frame_1.png'),(1,'frame_2.png'),(2,'frame_10.png')]
        source.verify_unchanged();source.close()
    def test_majority_warmup_strict_half_and_transition(self):
        vote=MajorityVote(15,2)
        results=[vote.step([i<8,False]) for i in range(16)]
        assert all(r is None for r in results[:14])
        assert results[14].tolist()==[True,False]
        assert results[15].tolist()==[False,False]
        even=MajorityVote(4,1)
        for present in [True,True,False,False]:r=even.step([present])
        assert r.tolist()==[False]


    def test_event_inclusive_frames_exclusive_end_and_eof(self):
        e=EventTracker(['a','b'],30)
        e.step(0,[True,False],'raw');e.step(1,[True,True],'raw');e.step(2,[False,True],'raw')
        e.step(0,None,'voted');e.step(1,None,'voted');e.step(2,[True,False],'voted')
        rows=e.finish(2)
        a=next(r for r in rows if r['presence_source']=='raw' and r['class_id']==0)
        assert (a['start_frame'],a['end_frame'])==(0,1) and a['duration_seconds']==2/30
        b=next(r for r in rows if r['presence_source']=='raw' and r['class_id']==1)
        assert b['closed_at_eof']==1 and b['end_time_exclusive_seconds']==3/30
        voted=next(r for r in rows if r['presence_source']=='voted')
        assert voted['start_frame']==2 and voted['duration_seconds']==1/30


    def test_display_change_excluded_from_verification_profile(self):
        s=load_settings('configs/road_marking_infer.yaml');other=copy.deepcopy(s)
        other['overlay']['palette']='different-display-only.yaml';other['overlay']['large_alpha']=.1
        assert signature(verification_settings(s))==signature(verification_settings(other))
        other['inference']['alpha']=.5
        assert signature(verification_settings(s))!=signature(verification_settings(other))


    def test_gate_missing_failed_or_corrupt_is_rejected(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        tmp_path=Path(temporary.name)
        s=load_settings('configs/road_marking_infer.yaml');s['verification']['report']=str(tmp_path/'proof.json')
        model=SimpleNamespace(fingerprint='unit-fixture')
        with self.assertRaises(RuntimeError):require_verification(s,model)
        failed=dict(status='failed',fingerprint='unit-fixture');failed['payload_sha256']=signature(failed)
        (tmp_path/'proof.json').write_text(json.dumps(failed))
        with self.assertRaises(ValueError):require_verification(s,model)
        failed['status']='passed' # deliberately stale checksum; never a valid certificate
        (tmp_path/'proof.json').write_text(json.dumps(failed))
        with self.assertRaisesRegex(ValueError,'checksum'):require_verification(s,model)


    def test_palette_exact_rgb_opacity_and_background(self):
        s=load_settings('configs/road_marking_infer.yaml');p=load_palette(s['overlay']['palette'])
        assert p[14].tolist()==[47,191,143] and p[21].tolist()==[159,143,0]
        frame=np.full((120,200,3),100,dtype=np.uint8);prediction=np.zeros((120,200),dtype=np.uint8)
        prediction[10:110,10:12]=4;prediction[15:100,60:160]=20
        names=[str(i) for i in range(25)]
        output=render(frame,prediction,p,names,s,0,0.)
        assert output[20,10].tolist()==[224,224,20] # thin: RGB->BGR, alpha .8
        assert output[30,80].tolist()==[154,104,104] # large interior: alpha .35
        assert output[15,60].tolist()==[255,111,111] # 2px inner contour
        assert output[14,60].tolist()==[100,100,100] # background remains untouched
        assert np.all(output[0,:200]==100)

if __name__=="__main__":unittest.main()
