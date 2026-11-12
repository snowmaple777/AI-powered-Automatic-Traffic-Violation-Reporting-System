"""詳細診斷只讀取判定結果；涵蓋拒絕、候選生命週期與既有序列化相容。"""
import copy
import io
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch, Mock

import cv2
import numpy as np

from double_lines import vehicle_line_position, validate_rule_settings, write_final_double_line_diagnostics
from violation_engine import DoubleLineCrossingRule, RuleContext
from test_double_line_rules import sequence, LANE_CHANGE


def rule(**options):
    return DoubleLineCrossingRule(RuleContext("test", 10, 400, 300), color="yellow",
                                  settings=dict(detailed_diagnostics=True, **options))


def prime(r, count=3):
    records = sequence(LANE_CHANGE)
    for record in records[:count]:
        r.evaluate(record)
    return records


class DoubleLineDiagnosticsTests(unittest.TestCase):
    def test_rejection_codes_and_distances_preserve_return(self):
        record = sequence(LANE_CHANGE)[0]
        line = record["observations"]["double_lines"][0]
        cfg = validate_rule_settings({"max_extrapolation_ratio": 0})
        examples = [([120, -30, 130, 10], "outside_far_support"),
                    ([120, 250, 130, 290], "outside_near_support"),
                    ([150, -19, 160, 21], "endpoint_margin"),
                    ([1, 2, 1, 9], "invalid_bbox"), ([1, 2, float("nan"), 9], "invalid_bbox"),
                    ([1, 2], "invalid_bbox"), (["invalid"], "invalid_bbox")]
        for box, reason in examples:
            with self.subTest(box=box):
                v = dict(bbox_xyxy=box); detail = {}; original = copy.deepcopy(line)
                self.assertEqual(vehicle_line_position(v, line, cfg), vehicle_line_position(v, line, cfg, diagnostics=detail))
                self.assertEqual(detail["reason"], reason)
                self.assertEqual(line, original)
                json.dumps(detail, allow_nan=False)
                if reason.startswith("outside"):
                    self.assertAlmostEqual(detail["required_extension_ratio"], detail["required_extension_px"]/40)
                    self.assertEqual(detail["extension_limit_px"], 7)
                    self.assertEqual(detail["support_gap_px"], 10)
        v = record["observations"]["vehicles"][0]; detail = {}
        self.assertEqual(vehicle_line_position(v, line, cfg), vehicle_line_position(v, line, cfg, diagnostics=detail))
        self.assertEqual(detail, {})

    def test_projection_rejection_and_extrapolation_metadata(self):
        line = dict(centerline=[[0, 0], [10, 20], [20, 10]])
        cfg = validate_rule_settings({"max_extrapolation_ratio": 0})
        detail = {}
        self.assertIsNone(vehicle_line_position(dict(bbox_xyxy=[95, -35, 105, 5]), line, cfg, diagnostics=detail))
        self.assertEqual(detail["reason"], "unsupported_line_projection")
        record = sequence(LANE_CHANGE)[0]; line = record["observations"]["double_lines"][0]
        detail = {}; v = dict(bbox_xyxy=[140, -30, 170, 18])
        cfg = validate_rule_settings()
        result = vehicle_line_position(v, line, cfg, diagnostics=detail)
        self.assertEqual(result, vehicle_line_position(v, line, cfg))
        self.assertIsNotNone(result)
        self.assertTrue(result["extrapolated"])

    def test_low_confidence_clears_before_phase_without_new_side_evidence(self):
        r = rule(); records = prime(r); v = records[3]["observations"]["vehicles"][0]
        v["confidence"] = .28525
        r.evaluate(records[3])
        d = r.diagnostics["details"]; change = d["state_changes"][0]
        self.assertEqual((change["reason"], change["phase"], change["confidence"]), ("low_confidence", "armed", .28525))
        self.assertEqual(change["before_count"], 3)
        self.assertAlmostEqual(change["elapsed_seconds"], .3)
        self.assertFalse(d["positions"])

    def test_motion_discontinuity_missing_vehicle_and_projection_clear(self):
        for reason in ("motion_unavailable", "discontinuous_input", "vehicle_missing", "projection_invalid"):
            with self.subTest(reason=reason):
                r = rule(allow_motion_gaps=False); records = prime(r, 1 if reason=="vehicle_missing" else 3)
                row = records[1 if reason=="vehicle_missing" else 3]
                if reason=="motion_unavailable":row["postprocessing"]["road_marking_compensation"]["motion"]={"valid":False}
                if reason=="discontinuous_input":row["frame"]["index"]=30
                if reason=="vehicle_missing":row["observations"]["vehicles"]=[]
                if reason=="projection_invalid":
                    with patch("violation_engine.project_points", side_effect=ValueError("horizon")):r.evaluate(row)
                else:r.evaluate(row)
                self.assertIn(reason, [c["reason"] for c in r.diagnostics["details"]["state_changes"]])

    def test_line_missing_gap_invalid_vehicle_and_unsupported_clear(self):
        for reason in ("line_unavailable", "observation_gap", "invalid_vehicle", "position_unavailable"):
            with self.subTest(reason=reason):
                r=rule(max_frame_gap_sec=2); records=prime(r, 1 if reason=="position_unavailable" else 3)
                row=records[1 if reason=="position_unavailable" else 3]
                if reason=="observation_gap":row["frame"]["timestamp_sec"]=.8
                if reason=="invalid_vehicle":row["observations"]["vehicles"][0]["class_name"]="person"
                if reason=="position_unavailable":row["observations"]["vehicles"][0]["bbox_xyxy"]=[120,300,140,340]
                if reason=="line_unavailable":
                    with patch("violation_engine.resolve_rule_lines", return_value=[]):r.evaluate(row)
                else:r.evaluate(row)
                self.assertIn(reason,[c["reason"] for c in r.diagnostics["details"]["state_changes"]])
                if reason=="observation_gap":
                    prior=next(c for c in r.diagnostics["details"]["state_changes"] if c["reason"]==reason)
                    self.assertEqual(prior["sides"]["counts"]["1"],3)
                    self.assertEqual(sum(r.diagnostics["details"]["positions"][0]["sides"]["counts"].values()),1)
                if reason=="position_unavailable":
                    self.assertEqual(r.diagnostics["skipped_vehicles"][0]["reason"],"unsupported_footprint_or_invalid_box")
                    self.assertEqual(r.diagnostics["details"]["position_failures"][0]["reason"],"outside_near_support")

    def test_box_jump_and_origin_unstable(self):
        for reason in ("box_jump", "origin_not_stable"):
            r=rule(); records=prime(r,1); row=records[1]
            row["observations"]["vehicles"][0]["bbox_xyxy"]=[900,150,920,190] if reason=="box_jump" else [145,150,165,190]
            r.evaluate(row)
            self.assertIn(reason,[c["reason"] for c in r.diagnostics["details"]["state_changes"]])

    def test_return_timeout_and_rearm_are_logged(self):
        paths = [("returned_to_origin", LANE_CHANGE[:5]+[(125,175)], {}),
                 ("crossing_timeout", LANE_CHANGE[:4]+[(154,180)]*4, {"max_crossing_seconds":.25}),
                 ("rearmed", LANE_CHANGE+[(185,160)]*15, {})]
        for reason,points,settings in paths:
            r=rule(**settings); changes=[]
            for row in sequence(points):
                r.evaluate(row);changes.extend(r.diagnostics["details"]["state_changes"])
            self.assertIn(reason,[c["reason"] for c in changes])

    def test_handoff_is_transfer_not_lost_candidate(self):
        r=rule(); prime(r)
        old=next(iter(r.states)); line=copy.deepcopy(next(iter(r.line_memory.values())))
        line["track_id"]="replacement"
        r._hand_off_lines([line],np.eye(3))
        change=r.diagnostics["details"]["state_changes"][-1]
        self.assertEqual((change["reason"],change["action"]),("line_handoff","transferred"))
        self.assertIn((old[0],"replacement"),r.states)
        self.assertEqual(r._side_history[(old[0],"replacement")]["counts"]["1"],3)

    def test_side_zero_without_candidate_and_gap_duration(self):
        r=rule(); rows=sequence([(154,180)]*5)
        for row in rows[:3]:r.evaluate(row)
        self.assertFalse(r.states)
        s=r.diagnostics["details"]["positions"][0]["sides"]
        self.assertEqual((s["first_side"],s["last_side"],s["counts"]["0"]),(0,0,3))
        self.assertAlmostEqual(s["seconds"]["0"],.2)
        rows[3]["observations"]["vehicles"][0]["source_frame"]=2
        r.evaluate(rows[3]);self.assertFalse(r.diagnostics["details"]["positions"])
        r.evaluate(rows[4]);s=r.diagnostics["details"]["positions"][0]["sides"]
        self.assertEqual(s["counts"]["0"],4);self.assertAlmostEqual(s["seconds"]["0"],.2)

    def test_enabled_events_and_legacy_diagnostics_equal_disabled(self):
        a=rule(); b=DoubleLineCrossingRule(RuleContext("test",10,400,300),color="yellow")
        for row in sequence(LANE_CHANGE):
            self.assertEqual(json.dumps(a.evaluate(copy.deepcopy(row))),json.dumps(b.evaluate(copy.deepcopy(row))))
            self.assertEqual({k:v for k,v in a.diagnostics.items() if k!="details"},b.diagnostics)
        self.assertNotIn("detailed_diagnostics",a.settings)
        self.assertIsNone(b._side_history)
        self.assertEqual(a.finalize(),b.finalize())
        out=io.StringIO();write_final_double_line_diagnostics(out,[b]);self.assertEqual(out.getvalue(),"")
        write_final_double_line_diagnostics(out,[a]);final=json.loads(out.getvalue())
        self.assertTrue(final["finalize"])
        self.assertTrue(all(c["reason"]=="finalize" for c in final["rules"][a.rule_id]["state_changes"]))

    def test_settings_validation_and_cli_config(self):
        from tools.rule_eval import rule_options
        self.assertFalse(validate_rule_settings()["detailed_diagnostics"])
        for value in (1,"true",None):
            with self.assertRaises(ValueError):validate_rule_settings({"detailed_diagnostics":value})
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"double.json";path.write_text('{"version":1,"detailed_diagnostics":true}')
            options=rule_options(double_line_config=path)
            self.assertTrue(options["double_yellow_line_crossing"]["settings"]["detailed_diagnostics"])
            self.assertTrue(options["double_white_line_crossing"]["settings"]["detailed_diagnostics"])

    def test_three_entries_write_identical_events_and_final_diagnostics(self):
        import run_pipeline
        import evaluate_violations
        from tools.rule_eval import run_rules, rule_options
        from frame_processing import FrameProcessor
        from model_library.base import ModelResult
        from model_library.registry import STAGES
        records=sequence(LANE_CHANGE)
        def inject(self,image,index,timestamp,results,**kwargs):
            return Mock(record=copy.deepcopy(records[index]),marking_result=ModelResult())
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);video=root/'clip.mp4';video.write_bytes(b'fake')
            model=root/'model.json';model.write_text('{"version":1,"models":{}}')
            events=[]
            for enabled in (False,True):
                target=root/str(enabled);target.mkdir()
                cfg=target/'double.json';cfg.write_text(json.dumps(dict(version=1,detailed_diagnostics=enabled)))
                cap=Mock();cap.isOpened.return_value=True
                cap.get.side_effect=lambda k:{cv2.CAP_PROP_FPS:10,cv2.CAP_PROP_FRAME_WIDTH:400,
                    cv2.CAP_PROP_FRAME_HEIGHT:300,cv2.CAP_PROP_FRAME_COUNT:len(records)}.get(k,0)
                cap.read.side_effect=[(True,np.zeros((300,400,3),np.uint8))]*len(records)+[(False,None)]
                pipeline=Mock(models={},infer=Mock(return_value={s:ModelResult() for s in STAGES}))
                args=run_pipeline.parse_args([str(video),'--model-config',str(model),'--output-dir',str(target/'online'),
                    '--device','cpu','--rules','double_yellow_line_crossing','--no-violation-clips','--double-line-config',str(cfg)])
                with patch('run_pipeline.cv2.VideoCapture',return_value=cap),patch.object(FrameProcessor,'process',inject),\
                     patch('run_pipeline.DetectionCsv'),ExitStack() as resources:
                    run_pipeline.run(args,resources,pipeline=pipeline)
                source=target/'online/clip_detections.jsonl'
                run_rules(source,target/'rules','clip',rules=('double_yellow_line_crossing',),options=rule_options(double_line_config=cfg))
                with patch('sys.argv',['evaluate_violations.py',str(source),'--output',str(target/'events.jsonl'),
                    '--rules','double_yellow_line_crossing','--no-violation-clips','--double-line-config',str(cfg),
                    '--diagnostics',str(target/'diagnostics.jsonl')]):evaluate_violations.main()
                expected=(target/'online/clip_violations.jsonl').read_bytes()
                self.assertEqual(expected,(target/'rules/clip_violations.jsonl').read_bytes())
                self.assertEqual(expected,(target/'events.jsonl').read_bytes());events.append(expected)
                d=(target/'online/clip_rule_diagnostics.jsonl').read_bytes()
                self.assertEqual(d,(target/'rules/clip_rule_diagnostics.jsonl').read_bytes())
                self.assertEqual(d,(target/'diagnostics.jsonl').read_bytes())
                self.assertEqual(len(d.splitlines()),len(records)+int(enabled))
            self.assertEqual(*events)


if __name__=="__main__":unittest.main()
