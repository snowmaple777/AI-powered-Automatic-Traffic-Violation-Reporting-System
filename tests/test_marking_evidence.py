"""標線證據閘門：推移（標線降頻）只對齊幾何，不能湊足「確認」所需的直接觀測。"""
import copy
import unittest

import numpy as np

from double_lines import attach_double_lines
from frame_processing import FrameProcessor
from model_library.base import FrameContext, ModelResult
from observation_schema import build_frame_observation, marking_evidence, marking_geometry_source
from road_compensation import CompensationConfig, MotionEstimate, RoadMarkingCompensator
from violation_engine import RedLightStopLineCrossingRule, RuleContext
try:  # unittest discover -s tests 時測試資料夾在 sys.path 上；從專案根目錄以模組名執行時則需 tests. 前綴
    from test_double_line_rules import LANE_CHANGE, run_records, sequence
    from test_marking_interval import CLASSES, _Done, marking_model, results_for
    from test_red_light_rule import LINE_A, frame as red_frame
except ModuleNotFoundError:
    from tests.test_double_line_rules import LANE_CHANGE, run_records, sequence
    from tests.test_marking_interval import CLASSES, _Done, marking_model, results_for
    from tests.test_red_light_rule import LINE_A, frame as red_frame

STILL = MotionEstimate(True, np.eye(3), "stationary")
BROKEN = MotionEstimate(False, reason="poor_spatial_coverage")


def source(index, source_frame, *, aligned=True, warped=True):
    direct = index == source_frame
    return dict(source_frame=source_frame, direct=direct, propagated=not direct, warped=warped and not direct,
                aligned=direct or aligned, age_frames=index - source_frame, motion_valid=warped)


class MarkingEvidenceReaderTests(unittest.TestCase):
    def test_old_records_fall_back_to_direct_and_17_propagated_frames_keep_their_source(self):
        record = build_frame_observation(4, .4, 10, 10, 10, [], [], [])
        self.assertEqual(marking_evidence(record), dict(source_frame=4, direct=True, propagated=False, warped=False,
                                                        aligned=True, age_frames=0, motion_valid=None, legacy=True))
        record["postprocessing"]["road_marking_source"] = dict(source_frame=3, propagated=True, warped=False)
        evidence = marking_evidence(record)
        self.assertEqual((evidence["direct"], evidence["aligned"], evidence["age_frames"], evidence["legacy"]),
                         (False, False, 1, True))
        self.assertEqual(marking_geometry_source(evidence), "stale")
        record["postprocessing"]["road_marking_source"] = source(4, 2)
        evidence = marking_evidence(record)
        self.assertEqual((evidence["direct"], evidence["aligned"], evidence["legacy"]), (False, True, False))
        self.assertEqual(marking_geometry_source(evidence), "propagated")


class FrameProcessorEvidenceTests(unittest.TestCase):
    def run_interval(self, interval, motions):
        model = marking_model(interval)
        processor = FrameProcessor(30, 200, 120, ["road_markings"])
        out = []
        for index, motion in enumerate(motions):
            marking = model.infer(None, FrameContext(index, index / 30), {})
            out.append(processor.process(None, index, index / 30, results_for(marking),
                                         motion_future=_Done(motion)).record["postprocessing"]["road_marking_source"])
        return out

    def test_one_failed_warp_leaves_the_chain_unaligned_until_the_next_direct_frame(self):
        # interval 4：第 2 幀運動失效後，第 3 幀即使推移成功，遮罩仍未對齊；第 4 幀重新推論。
        out = self.run_interval(4, [BROKEN, STILL, BROKEN, STILL, STILL])
        self.assertEqual([(s["direct"], s["warped"], s["aligned"], s["age_frames"]) for s in out],
                         [(True, False, True, 0), (False, True, True, 1), (False, False, False, 2),
                          (False, True, False, 3), (True, False, True, 0)])

    def test_interval_one_marks_every_frame_direct(self):
        out = self.run_interval(1, [BROKEN, STILL, BROKEN])
        self.assertTrue(all(s["direct"] and s["aligned"] for s in out))

    def test_replayed_propagated_mask_is_not_rewarped_or_promoted_to_direct(self):
        processor = FrameProcessor(30, 200, 120, ["road_markings"])
        mask = np.zeros((120, 200), np.uint8)
        mask[60:64, 40:120] = 3
        recorded = source(5, 4)
        marking = ModelResult([], {"mask": mask, "source_frame": 4, "classes": CLASSES, "min_pixels": 10,
                                   "prealigned_source": dict(recorded, legacy=False)})
        shift = MotionEstimate(True, np.array([[1, 0, 9], [0, 1, 0], [0, 0, 1]], float), "accepted")
        result = processor.process(None, 5, 5 / 30, results_for(marking), motion_future=_Done(shift))
        self.assertIs(result.marking_result.artifacts["mask"], mask)
        written = result.record["postprocessing"]["road_marking_source"]
        self.assertEqual((written["direct"], written["source_frame"], written["aligned"]), (False, 4, True))

    def test_new_video_starts_with_a_direct_aligned_frame(self):
        model = marking_model(2)
        for _ in range(2):  # 第二支影片：reset 後第一幀必定重新推論
            model.reset()
            processor = FrameProcessor(30, 200, 120, ["road_markings"])
            marking = model.infer(None, FrameContext(0, 0), {})
            first = processor.process(None, 0, 0, results_for(marking), motion_future=_Done(BROKEN))
            self.assertTrue(first.record["postprocessing"]["road_marking_source"]["direct"])


class CompensatorEvidenceTests(unittest.TestCase):
    @staticmethod
    def stop_line():
        return [{"class_id": 3, "class_name": "stop line", "pixels": 1, "components": [
            {"area": 900, "bbox_xywh": [200, 250, 150, 6], "line_xyxy": [200, 253, 350, 253], "centroid": [275, 253]}]}]

    def run_frames(self, directs):
        compensator = RoadMarkingCompensator(CompensationConfig())
        frame = np.zeros((360, 640, 3), np.uint8)
        out = []
        for index, direct in enumerate(directs):
            result = compensator.process(frame, frame_index=index, timestamp_sec=index / 30, road_markings=self.stop_line(),
                                         vehicles=[], motion=STILL if index else None, direct_observation=direct)
            out.append(result.compensated_markings[0]["components"][0]["compensation"])
        return out

    def test_propagated_matches_keep_existing_eligibility_but_not_direct_eligibility(self):
        out = self.run_frames([True, False, False, True, False, True])
        self.assertEqual([c["observed_frames"] for c in out], [1, 2, 3, 4, 5, 6])
        self.assertEqual([c["direct_observed_frames"] for c in out], [1, 1, 1, 2, 2, 3])
        self.assertEqual([c["rule_eligible"] for c in out], [False, False, True, True, True, True])
        self.assertEqual([c["direct_rule_eligible"] for c in out], [False] * 5 + [True])
        self.assertEqual(out[4]["last_direct_observed_frame"], 3)

    def test_all_direct_frames_make_both_eligibility_flags_identical(self):
        out = self.run_frames([True] * 5)
        self.assertEqual([c["rule_eligible"] for c in out], [c["direct_rule_eligible"] for c in out])

    def test_estimate_only_track_never_becomes_direct_eligible(self):
        out = self.run_frames([True] + [False] * 6)
        self.assertTrue(out[-1]["rule_eligible"])
        self.assertFalse(out[-1]["direct_rule_eligible"])


class RedLightEvidenceTests(unittest.TestCase):
    def run_rule(self, frames, **options):
        rule = RedLightStopLineCrossingRule(RuleContext("t", 25, 1920, 1080), **options)
        events = []
        for item in frames:
            events += rule.evaluate(item)
        return rule, events + rule.finalize()

    def crossing(self, line, sources=None):
        frames = [red_frame(i, 760, [line]) for i in range(10)] + [red_frame(i, 640, [line]) for i in range(10, 15)]
        for item in frames:
            if sources:
                item["postprocessing"]["road_marking_source"] = sources(item["frame"]["index"])
        return frames

    def test_compensated_line_built_from_estimates_can_only_be_suspected(self):
        line = dict(LINE_A, compensation={"id": "c1", "rule_eligible": True, "direct_rule_eligible": False})
        rule, events = self.run_rule(self.crossing(line))
        self.assertEqual([e["status"] for e in events], ["suspected"])
        self.assertFalse(events[0]["conditions"]["stop_line_direct_evidence"])
        self.assertIn("stop_line_not_directly_observed", rule.diagnostics["vehicles"][0]["blocked_by"])
        _, legacy = self.run_rule(self.crossing(line), require_direct_marking_evidence=False)
        self.assertEqual([e["status"] for e in legacy], ["confirmed"])
        self.assertNotIn("stop_line_direct_evidence", legacy[0]["conditions"])
        line["compensation"]["direct_rule_eligible"] = True
        _, direct = self.run_rule(self.crossing(line))
        self.assertEqual([e["status"] for e in direct], ["confirmed"])

    def test_raw_line_counts_only_direct_frames(self):
        # 未啟用補償：interval 2 的推移幀不計入直接觀測，但直接幀足夠時仍可確認。
        _, alternate = self.run_rule(self.crossing(LINE_A, lambda i: source(i, i - i % 2)))
        self.assertEqual([e["status"] for e in alternate], ["confirmed"])
        _, estimate_only = self.run_rule(self.crossing(LINE_A, lambda i: source(i, 0)))
        self.assertEqual([e["status"] for e in estimate_only], ["suspected"])
        self.assertFalse(estimate_only[0]["conditions"]["stop_line_direct_evidence"])

    def test_unaligned_mask_at_crossing_cannot_confirm(self):
        sources = lambda i: source(i, i, ) if i < 10 else source(i, 9, aligned=False, warped=False)
        _, events = self.run_rule(self.crossing(LINE_A, sources))
        self.assertEqual([e["status"] for e in events], ["suspected"])

    def test_old_records_without_direct_fields_keep_previous_behavior(self):
        line = dict(LINE_A, compensation={"id": "c1", "rule_eligible": True})
        _, events = self.run_rule(self.crossing(line))
        self.assertEqual([e["status"] for e in events], ["confirmed"])


class DoubleLineEvidenceTests(unittest.TestCase):
    @staticmethod
    def mark(records, sources, direct_counts=None):
        for record in records:
            index = record["frame"]["index"]
            evidence = sources(index)
            record["postprocessing"]["road_marking_source"] = evidence
            line = record["observations"]["double_lines"][0]
            line["geometry_source"] = marking_geometry_source(dict(evidence, legacy=False))
            if direct_counts is not None:
                line["direct_observed_frames"] = direct_counts(index)
        return records

    def test_interval_two_geometry_can_still_confirm(self):
        records = self.mark(sequence(LANE_CHANGE), lambda i: source(i, i - i % 2), lambda i: 5 + i // 2)
        events = run_records(records)
        self.assertEqual([e["status"] for e in events], ["confirmed"])
        self.assertTrue(events[0]["conditions"]["direct_marking_evidence"])

    def test_estimate_only_geometry_can_only_be_suspected(self):
        records = self.mark(sequence(LANE_CHANGE), lambda i: source(i, 0) if i else source(0, 0), lambda i: 1)
        events = run_records(records)
        self.assertEqual([e["status"] for e in events], ["suspected"])
        self.assertFalse(events[0]["conditions"]["direct_marking_evidence"])
        self.assertEqual([e["status"] for e in run_records(
            copy.deepcopy(records), settings={"require_direct_marking_evidence": False})], ["confirmed"])

    def test_unaligned_sample_after_crossing_downgrades(self):
        stale = lambda i: source(i, i - 1, aligned=False, warped=False) if i == 7 else source(i, i)
        events = run_records(self.mark(sequence(LANE_CHANGE), stale))
        self.assertEqual([e["status"] for e in events], ["suspected"])

    def test_touch_and_return_on_estimated_geometry_never_emits(self):
        points = LANE_CHANGE[:5] + [(125, 180)] * 5
        records = self.mark(sequence(points), lambda i: source(i, i - i % 2))
        self.assertEqual(run_records(records), [])

    def test_attach_counts_direct_observations_separately(self):
        records = sequence([(125, 200)] * 6)
        state = {}
        mask = np.zeros((300, 400), np.uint8)
        mask[20:281, 150:153], mask[20:281, 158:161] = 8, 8
        counts = []
        for record in records:
            index = record["frame"]["index"]
            record["observations"]["double_lines"] = []
            record["postprocessing"]["road_marking_source"] = source(index, index - index % 2)
            record["postprocessing"]["ego_motion"] = {"valid": True, "matrix": np.eye(3).tolist(),
                                                      "reprojection_error_px": 0}
            attach_double_lines(record, mask, state)
            line = record["observations"]["double_lines"][0]
            counts.append((line["observed_frames"], line["direct_observed_frames"], line["geometry_source"]))
        self.assertEqual(counts, [(1, 1, "direct"), (2, 1, "propagated"), (3, 2, "direct"),
                                  (4, 2, "propagated"), (5, 3, "direct"), (6, 3, "propagated")])


if __name__ == "__main__":
    unittest.main()
