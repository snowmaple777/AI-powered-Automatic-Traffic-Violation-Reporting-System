"""補償契約、幾何、遮擋／失效及與未修改違規引擎的整合測試。"""
from copy import deepcopy
from dataclasses import replace
import json
import unittest
from contextlib import ExitStack
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import csv
import cv2
import numpy as np

from road_compensation import CompensationConfig, MotionEstimate, RoadMarkingCompensator, attach_compensation
from road_compensation.geometry import bounded_component, merge_fragments
from road_compensation.motion import RoadMotionEstimator, road_mask
from observation_schema import build_frame_observation
from violation_engine import RuleContext, RedLightStopLineCrossingRule


def marking(x=100,y=320,width=300):
    return {"class_id":3,"class_name":"stop line","pixels":width*8,
            "components":[{"area":width*8,"bbox_xywh":[x,y-4,width+1,9],
                           "centroid":[x+width/2,y],"line_xyxy":[0,y,639,y]}]}


def components(result):
    return [c for m in result.compensated_markings for c in m["components"] if "compensation" in c]


class FixedMotion:
    def __init__(self,dx=0,dy=0):
        self.matrix=np.array([[1.,0,dx],[0,1.,dy],[0,0,1.]])
        self.valid=True

    def estimate(self,*args):
        return MotionEstimate(self.valid,self.matrix,"test_motion" if self.valid else "test_failure",100,1.,0.,0.)


class CompensationTests(unittest.TestCase):
    def setUp(self):
        self.frame=np.zeros((480,640,3),np.uint8)
        self.motion=FixedMotion()
        self.comp=RoadMarkingCompensator(motion_estimator=self.motion)

    def process(self,index,markings=None,vehicles=None,comp=None):
        return (comp or self.comp).process(self.frame,frame_index=index,timestamp_sec=index/30,
                                          road_markings=[marking()] if markings is None else markings,
                                          vehicles=vehicles or [])

    def warm(self):
        for i in range(3):
            result=self.process(i)
        return result

    def test_warmup_and_no_input_mutation(self):
        raw=[marking()]
        original=deepcopy(raw)
        first=self.process(0,raw)
        self.assertEqual(raw,original)
        self.assertEqual(first.rule_markings,[])
        self.assertEqual(components(first)[0]["line_xyxy"],[100.,320.,400.,320.])
        self.process(1)
        third=self.process(2)
        self.assertEqual(third.diagnostics["status"],"usable")
        self.assertEqual(components(third)[0]["compensation"]["observed_frames"],3)
        first.raw_markings[0]["components"][0]["area"]=0
        self.assertEqual(raw,original)

    def test_camera_motion_applied_before_tracking_and_holdover(self):
        self.warm()
        self.motion.matrix[1,2]=10
        tracked=self.process(3,[marking(y=330)])
        line=components(tracked)[0]
        self.assertEqual(line["compensation"]["observed_frames"],4)
        self.assertAlmostEqual(line["line_xyxy"][1],330)
        predicted=self.process(4,[],[{"bbox_xyxy":[90,300,410,400]}])
        self.assertAlmostEqual(components(predicted)[0]["line_xyxy"][1],340)
        self.assertEqual(components(predicted)[0]["compensation"]["last_observed_frame"],3)

    def test_occlusion_decay_gate_and_expiry(self):
        self.warm()
        vehicle={"bbox_xyxy":[90,280,410,400]}
        scores=[]
        for i in range(3,10):
            result=self.process(i,[],[vehicle])
            scores.append(components(result)[0]["compensation"]["support_score"])
        self.assertTrue(all(a>b for a,b in zip(scores,scores[1:])))
        self.assertEqual(components(result)[0]["compensation"]["source"],"predicted_occluded")
        self.assertFalse(components(result)[0]["compensation"]["rule_eligible"])
        self.assertEqual(result.rule_markings,[])
        for i in range(10,19):
            result=self.process(i,[],[vehicle])
        self.assertEqual(components(result),[])
        self.assertEqual(result.diagnostics["status"],"unknown")

    def test_visible_missing_has_shorter_lifetime(self):
        self.warm()
        for i in range(3,7):
            result=self.process(i,[])
        self.assertEqual(components(result),[])

    def test_failed_motion_never_reuses_history(self):
        self.warm()
        self.motion.valid=False
        result=self.process(3,[],[{"bbox_xyxy":[0,0,640,480]}])
        self.assertEqual(components(result),[])
        self.assertEqual(result.diagnostics["reset_reason"],"test_failure")
        result=self.process(4)
        self.assertEqual(components(result)[0]["compensation"]["observed_frames"],1)
        self.assertEqual(result.rule_markings,[])

    def test_unconfirmed_history_is_not_extended(self):
        self.process(0)
        result=self.process(1,[],[{"bbox_xyxy":[0,0,640,480]}])
        self.assertEqual(components(result),[])

    def test_discontinuity_resets_and_unseen_line_is_never_invented(self):
        self.warm()
        result=self.process(20,[])
        self.assertEqual(components(result),[])
        self.assertEqual(result.diagnostics["reset_reason"],"discontinuous_input")
        self.comp.reset()
        self.assertEqual(components(self.process(0,[])),[])

    def test_other_marking_classes_are_preserved(self):
        item=marking()
        item["class_name"]="crosswalk"
        result=self.process(0,[item])
        self.assertEqual(result.rule_markings,[item])
        self.assertEqual(result.raw_markings,[item])

    def test_fragment_merge_requires_alignment_and_occluded_large_gap(self):
        cfg=CompensationConfig()
        a=bounded_component(marking(x=100,width=80)["components"][0],640,480,cfg)
        b=bounded_component(marking(x=200,width=80)["components"][0],640,480,cfg)
        self.assertEqual(len(merge_fragments([a,b],[],640,480,cfg)),2)
        merged=merge_fragments([a,b],[{"bbox_xyxy":[175,310,205,330]}],640,480,cfg)
        self.assertEqual(len(merged),1)
        self.assertEqual(merged[0]["merged_fragments"],2)
        self.assertEqual(merged[0]["area"],a["area"]+b["area"])
        b["line_xyxy"]=[200,350,280,350]
        self.assertEqual(len(merge_fragments([a,b],[{"bbox_xyxy":[0,0,640,480]}],640,480,cfg)),2)

    def test_invalid_geometry_and_config(self):
        item=marking()
        item["components"][0]["line_xyxy"]=[float("nan"),0,1,1]
        self.assertEqual(components(self.process(0,[item])),[])
        for kwargs in ({"max_occluded_seconds":-1},{"confidence_half_life":float("nan")},{"smoothing_alpha":1.5}):
            with self.assertRaises(ValueError):
                CompensationConfig(**kwargs)

    def test_adapter_isolated_and_serializable(self):
        result=self.warm()
        original=build_frame_observation(2,2/30,640,480,30,[],[],[marking()])
        before=deepcopy(original)
        adapted=attach_compensation(original,result)
        self.assertEqual(original,before)
        self.assertEqual(adapted["observations"]["road_markings_raw"],[marking()])
        self.assertIn("compensation",adapted["observations"]["road_markings"][0]["components"][0])
        json.dumps(adapted,allow_nan=False)

    def test_unchanged_rule_handles_short_occlusion_with_auditable_evidence(self):
        rule=RedLightStopLineCrossingRule(RuleContext("test",30,640,480),
                                        stable_signal_seconds=.01,min_track_frames=1,min_line_stable_frames=1,
                                        crossing_direction="toward")
        events=[]
        for i in range(10):
            bottom=300 if i<6 else (320 if i==6 else 345)
            vehicle={"object_id":"v1","class_name":"car","track_age_frames":100,
                     "bottom_center":[250,bottom],"bbox_xyxy":[100,240,400,bottom]}
            result=self.process(i,[] if i in (6,7) else [marking()],[vehicle])
            observation=build_frame_observation(i,i/30,640,480,30,[vehicle],
                                               [{"class_name":"red","confidence":.9}],[marking()])
            events.extend(rule.evaluate(attach_compensation(observation,result)))
        self.assertEqual(len(events),1)
        self.assertIn("compensation",events[0]["evidence"]["stop_line"])
        self.assertTrue(events[0]["review_required"])


class MotionTests(unittest.TestCase):
    def test_real_optical_flow_translation(self):
        rng=np.random.default_rng(3)
        gray=rng.integers(0,256,(480,640),dtype=np.uint8)
        gray=cv2.GaussianBlur(gray,(5,5),0)
        transform=np.float64([[1,0,5],[0,1,3],[0,0,1]])
        shifted=cv2.warpPerspective(gray,transform,(640,480))
        cfg=CompensationConfig()
        mask=road_mask(gray.shape,[],cfg.roi_top)
        result=RoadMotionEstimator(cfg).estimate(gray,shifted,mask,mask)
        self.assertTrue(result.valid,result.reason)
        point=np.float32([[[320,350]]])
        projected=cv2.perspectiveTransform(point,result.matrix)
        np.testing.assert_allclose(projected,[[[325,353]]],atol=.8)

    def test_blank_road_and_scene_cut_rejected(self):
        cfg=CompensationConfig()
        estimator=RoadMotionEstimator(cfg)
        mask=road_mask((480,640),[],cfg.roi_top)
        blank=np.zeros((480,640),np.uint8)
        self.assertFalse(estimator.estimate(blank,blank,mask,mask).valid)
        rng=np.random.default_rng(42)
        a=rng.integers(0,256,(480,640),dtype=np.uint8)
        b=rng.integers(0,256,(480,640),dtype=np.uint8)
        self.assertFalse(estimator.estimate(a,b,mask,mask).valid)

    def test_vehicle_masks_exclude_dynamic_regions(self):
        mask=road_mask((480,640),[{"bbox_xyxy":[100,250,300,450]}],.4)
        self.assertEqual(mask[300,200],0)
        self.assertEqual(mask[100,400],0)
        self.assertEqual(mask[300,400],255)


class PipelineIntegrationTests(unittest.TestCase):
    def test_csv_and_bypass_with_fixed_model_outputs(self):
        # 用固定模型結果驗證主程式交接；不載入真實權重，也不依賴影片是否剛好出現停止線。
        import run_pipeline
        from model_library import ModelResult
        class Pipeline:
            models={"road_markings":object()}
            def __init__(self,*args):
                pass
            def __enter__(self):
                return self
            def __exit__(self,*args):
                pass
            def infer(self,*args):
                return {name:ModelResult([marking()] if name=="road_markings" else [])
                        for name in ("vehicles","traffic_lights","road_markings","depth","plates","ocr")}

        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            video=root/"test.mp4"
            writer=cv2.VideoWriter(str(video),cv2.VideoWriter_fourcc(*"mp4v"),30,(640,480))
            self.assertTrue(writer.isOpened())
            rng=np.random.default_rng(4)
            frame=rng.integers(0,256,(480,640,3),dtype=np.uint8)
            for _ in range(4):
                writer.write(frame)
            writer.release()
            for enabled in (True,False):
                output=root/str(enabled)
                args=SimpleNamespace(input=video,output_dir=output,device="cpu",max_frames=4,
                    model_config=None,marking_compensation=enabled,
                    marking_compensation_config=Path(run_pipeline.ROOT)/"configs/marking_compensation.json",
                    vehicle_conf=None,light_conf=None,marking_min_pixels=None,tracker=None,
                    save_video=False,rules="")
                with patch.object(run_pipeline,"ModelPipeline",Pipeline), patch.object(run_pipeline,"load_config",return_value={"models":{"road_markings":{}}}):
                    with ExitStack() as resources:
                        run_pipeline.run(args,resources)
                rows=[json.loads(l) for l in (output/"test_detections.jsonl").read_text(encoding="utf-8").splitlines()]
                with (output/"test_detections.csv").open(encoding="utf-8-sig") as source:
                    csv_rows=list(csv.DictReader(source))
                self.assertEqual(len(rows),4)
                self.assertEqual(any(r["model"]=="road_marking_compensated" for r in csv_rows),enabled)
                if enabled:
                    self.assertEqual(rows[0]["observations"]["road_markings_raw"],[marking()])
                    self.assertEqual(rows[0]["observations"]["road_markings"],[])
                    self.assertTrue(rows[-1]["postprocessing"]["road_marking_compensation"]["status"]=="usable")
                else:
                    self.assertEqual(rows[0]["observations"]["road_markings"],[marking()])
                    self.assertNotIn("road_markings_raw",rows[0]["observations"])


if __name__=="__main__":
    unittest.main()
