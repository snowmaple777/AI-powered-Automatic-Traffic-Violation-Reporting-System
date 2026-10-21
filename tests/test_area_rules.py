import copy
import json
import tempfile
import unittest
import numpy as np
from pathlib import Path
from unittest.mock import patch

from area_rules import (settings_for, front_overlap, polygon, geometry_options,
                        attach_area_geometry, extract_traffic_regions)
from observation_schema import build_frame_observation
from violation_engine import RuleContext, create_rules


def records(kind="crosswalk", count=15, moving=0, camera=0):
    output = []
    for i in range(count):
        shift = camera * i
        x = 45 + shift + moving * i
        geometry = dict(source_frame=i, confidence=.95, method="synthetic_ground_truth",
                        front_edge=[[x, 60], [x+20, 60]],
                        footprint=[[x, 50], [x+20, 50], [x+20, 70], [x, 70]])
        v = dict(object_id="v1", class_name="car", confidence=.9, source_frame=i,
                 lane_id="lane-1", bbox_xyxy=[x, 20, x+20, 70], road_geometry=geometry,
                 plate_text="ABC-1234")
        r = build_frame_observation(i, i/10, 400, 300, 10, [v], [], [])
        r["observations"]["traffic_regions"] = [dict(region_id="r1", kind=kind,
            source_frame=i, confidence=.95, lane_id="lane-1",
            polygon=[[50+shift, 40], [100+shift, 40], [100+shift, 90], [50+shift, 90]],
            signal=dict(state="red", lane_id="lane-1", confidence=.9, source_frame=i))]
        r["postprocessing"]["road_marking_compensation"]["motion"] = dict(
            valid=True, matrix=[[1, 0, camera], [0, 1, 0], [0, 0, 1]])
        output.append(r)
    return output


def evaluate(rows, settings=None):
    kind = rows[0]["observations"]["traffic_regions"][0]["kind"]
    name = "crosswalk_stopping" if kind == "crosswalk" else "car_motorcycle_waiting_area"
    rule = create_rules([name], RuleContext("test", 10, 400, 300),
                        {name: {"settings": settings}})[0]
    events = []
    for row in rows:
        events.extend(rule.evaluate(row))
    events.extend(rule.finalize())
    return events


class AreaRuleTests(unittest.TestCase):
    def test_mask_to_estimated_geometry_to_candidate_end_to_end(self):
        state, cfg = {}, geometry_options({"fixed_camera": True})
        mask = np.zeros((300, 400), np.uint8)
        mask[40:90, 30:110] = 2
        rows = records(count=15)
        for row in rows:
            row["observations"]["vehicles"][0].pop("road_geometry")
            row["observations"]["road_markings"] = [dict(class_name="crosswalk", class_id=2)]
            attach_area_geometry(row, mask, state, cfg)
        events = evaluate(rows)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["conditions"]["estimated_vehicle_geometry"])
        self.assertIn("近似", events[0]["description"])
        self.assertEqual(events[0]["status"], "suspected")

    def test_semantic_stripes_merge_without_joining_distant_crosswalks(self):
        mask = np.zeros((300, 400), np.uint8)
        for x in (50, 75, 100, 300, 325):
            mask[80:130, x:x+15] = 2
        cfg = geometry_options()
        regions = extract_traffic_regions(mask, [dict(class_name="crosswalk", class_id=2)], cfg)
        self.assertEqual(len(regions), 2)
        self.assertTrue(all(r["quality_kind"] == "heuristic_not_probability" for r in regions))
        mask[:] = 0
        mask[100:103, 20:200] = 20
        self.assertEqual(extract_traffic_regions(mask, [dict(class_name="motor waiting zone", class_id=20)], cfg), [])

    def test_geometry_tracking_occlusion_expiry_and_motion_failure(self):
        state, cfg = {}, geometry_options()
        mask = np.zeros((300, 400), np.uint8)
        mask[40:90, 50:100] = 2
        rows = records(count=8, camera=2)
        ids = []
        for i, row in enumerate(rows):
            row["observations"]["road_markings"] = [dict(class_name="crosswalk", class_id=2)]
            attach_area_geometry(row, mask if i == 0 else np.zeros_like(mask), state, cfg)
            regions = row["observations"]["traffic_regions"]
            if i <= 4:
                ids.append(regions[0]["region_id"])
                self.assertAlmostEqual(min(p[0] for p in regions[0]["polygon"]), 50+2*i)
                self.assertEqual(regions[0]["observed_frame"], 0)
                self.assertEqual(regions[0]["source_frame"], i)
            else:
                self.assertEqual(regions, [])
        self.assertEqual(len(set(ids)), 1)
        row = records()[0]
        row["observations"]["road_markings"] = [dict(class_name="crosswalk", class_id=2)]
        attach_area_geometry(row, mask, state, cfg)
        row = records()[1]
        row["postprocessing"] = {}
        attach_area_geometry(row, None, state, cfg)
        self.assertEqual(row["observations"]["traffic_regions"], [])

    def test_geometry_duplicate_ego_clipping_and_raw_preservation(self):
        row = records()[0]
        v = row["observations"]["vehicles"][0]
        v.pop("road_geometry")
        duplicate = dict(v, object_id="truck", class_name="truck", confidence=.8)
        ego = dict(v, object_id="ego", class_name="motorcycle", bbox_xyxy=[200, 200, 350, 290])
        row["observations"]["vehicles"].extend([duplicate, ego])
        raw = copy.deepcopy(row["observations"]["vehicles"])
        cfg = geometry_options({"ego_polygons": [[[.45, .6], [.95, .6], [.95, 1], [.45, 1]]]})
        attach_area_geometry(row, None, {}, cfg)
        self.assertEqual(row["observations"]["vehicles"], raw)
        self.assertEqual(len(row["observations"]["area_vehicles"]), 1)
        geometry = row["observations"]["area_vehicles"][0]["road_geometry"]
        self.assertTrue(geometry["estimated"])
        self.assertNotIn("front_edge", geometry)
        row = records()[0]
        row["observations"]["vehicles"][0].pop("road_geometry")
        row["observations"]["vehicles"][0]["bbox_xyxy"][0] = 0
        attach_area_geometry(row, None, {}, cfg)
        self.assertNotIn("road_geometry", row["observations"]["area_vehicles"][0])

    def test_calibrated_lane_signal_requires_continuous_unambiguous_red(self):
        cfg = geometry_options({"fixed_camera": True, "lanes": [dict(lane_id="L1",
            polygon=[[0, 0], [1, 0], [1, 1], [0, 1]], heading=[0, -1],
            signal_roi=[[0, 0], [.3, 0], [.3, .2], [0, .2]])]})
        state = {}
        mask = np.zeros((300, 400), np.uint8)
        mask[40:90, 50:100] = 20
        for i, row in enumerate(records(count=5)):
            row["observations"]["vehicles"][0].pop("road_geometry")
            row["observations"]["road_markings"] = [dict(class_name="motor waiting zone", class_id=20)]
            row["observations"]["traffic_lights"] = [dict(class_name="red", confidence=.9, center=[30, 30])]
            if i == 3:
                row["observations"]["traffic_lights"].append(dict(class_name="green", confidence=.9, center=[40, 30]))
            attach_area_geometry(row, mask, state, cfg)
            region = row["observations"]["traffic_regions"][0]
            self.assertEqual(bool(region["signal"]), i == 2)
            self.assertEqual(row["observations"]["area_vehicles"][0]["lane_id"], "L1")
            self.assertIn("front_edge", row["observations"]["area_vehicles"][0]["road_geometry"])
        with self.assertRaises(ValueError):
            geometry_options({"lanes": cfg["lanes"]})

    def test_plate_link_preserves_identity_only_with_unique_close_match(self):
        rows, state, cfg = records(count=4), {}, geometry_options()
        for i, row in enumerate(rows):
            vehicle = row["observations"]["vehicles"][0]
            vehicle.update(object_id=f"id{i}", text_confidence=.95)
            if i == 2:
                vehicle["source_frame"] = 1
                vehicle["object_id"] = "id1"
            if i == 3:
                vehicle["bbox_xyxy"] = [250, 20, 270, 70]
            attach_area_geometry(row, None, state, cfg)
            self.assertEqual(row["observations"]["area_vehicles"][0]["object_id"], "id3" if i == 3 else "id0")

    def test_crosswalk_stationary_camera_motion_and_dedup(self):
        for camera in (0, 3):
            events = evaluate(records(camera=camera))
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["timestamp_sec"], 1.)
            self.assertAlmostEqual(events[0]["evidence"]["measurement"], .75)
            self.assertEqual(events[0]["status"], "suspected")
            self.assertEqual(events[0]["evidence"]["vehicle"]["plate_text"], "ABC-1234")
            json.dumps(events, allow_nan=False)

    def test_moving_short_stay_and_overlap_threshold(self):
        self.assertEqual(evaluate(records(moving=3)), [])
        self.assertEqual(evaluate(records(count=9)), [])
        self.assertEqual(evaluate(records(), {"overlap_ratio": .8}), [])
        self.assertEqual(len(evaluate(records(), {"overlap_ratio": .75})), 1)

    def test_waiting_area_red_front_only(self):
        rows = records("motorcycle_waiting_area", count=8)
        for row in rows:
            row["observations"]["vehicles"][0]["road_geometry"].pop("footprint")
        events = evaluate(rows)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["timestamp_sec"], .2)
        self.assertEqual(events[0]["evidence"]["measurement_name"], "front_overlap_px")
        for change in ("green", "lane", "motorcycle", "stale_signal"):
            modified = copy.deepcopy(rows)
            for row in modified:
                v = row["observations"]["vehicles"][0]
                signal = row["observations"]["traffic_regions"][0]["signal"]
                if change == "green": signal["state"] = "green"
                if change == "lane": v["lane_id"] = "other"
                if change == "motorcycle": v["class_name"] = "motorcycle"
                if change == "stale_signal": signal["source_frame"] = -1
            self.assertEqual(evaluate(modified), [])

    def test_missing_geometry_motion_staleness_and_id_switch(self):
        for change in ("geometry", "motion", "cached", "stale_region", "identity"):
            rows = records()
            for i, row in enumerate(rows):
                v = row["observations"]["vehicles"][0]
                if change == "geometry": v.pop("road_geometry")
                if change == "motion": row["postprocessing"] = {}
                if change == "cached": v["source_frame"] = 0
                if change == "stale_region": row["observations"]["traffic_regions"][0]["source_frame"] = -1
                if change == "identity": v["object_id"] = str(i//5)
            self.assertEqual(evaluate(rows), [], change)

    def test_missing_region_and_frame_gap_reset_dwell(self):
        for change in ("missing", "gap"):
            rows = records()
            if change == "missing": rows[7]["observations"]["traffic_regions"] = []
            else: rows.pop(7)
            self.assertEqual(evaluate(rows), [])

    def test_duplicate_observations_cannot_accumulate_evidence(self):
        for field in ("vehicles", "traffic_regions"):
            rows = records()
            for row in rows:
                row["observations"][field] *= 2
            self.assertEqual(evaluate(rows), [])

    def test_green_on_cached_frame_cancels_intrusion(self):
        rows = records("motorcycle_waiting_area", count=4)
        rows[1]["observations"]["vehicles"][0]["source_frame"] = 0
        rows[1]["observations"]["traffic_regions"][0]["signal"]["state"] = "green"
        self.assertEqual(evaluate(rows), [])

    def test_front_segment_crosses_region_with_both_ends_outside(self):
        area = polygon([[10, 10], [30, 10], [30, 30], [10, 30]])
        self.assertAlmostEqual(front_overlap([[0, 20], [40, 20]], area), 20.)
        self.assertEqual(front_overlap([[0, 10], [40, 10]], area), 0.)
        self.assertEqual(front_overlap([[0, 0], [5, 5]], area), 0.)

    def test_old_records_all_rules_remain_compatible(self):
        row = build_frame_observation(0, 0, 400, 300, 10, [], [], [])
        row["observations"].pop("traffic_regions")
        rules = create_rules(["all"], RuleContext("test", 10, 400, 300))
        for rule in rules:
            self.assertEqual(rule.evaluate(row), [])

    def test_config_rejects_unknown_and_nonfinite_values(self):
        for cfg in ({"overlap_ratio": 1.1}, {"stationary_seconds": float("nan")},
                    {"min_observations": 1}, {"extra": 1}, {"min_confidence": True}):
            with self.assertRaises(ValueError): settings_for(cfg)

    def test_replay_cli_writes_candidate_and_diagnostics(self):
        from evaluate_violations import main
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, out, diag = root/"input.jsonl", root/"events.jsonl", root/"diagnostics.jsonl"
            source.write_text("".join(json.dumps(r)+"\n" for r in records()), encoding="utf-8")
            with patch("sys.argv", ["evaluate_violations.py", str(source), "--rules", "crosswalk_stopping",
                                    "--output", str(out), "--diagnostics", str(diag)]):
                main()
            self.assertEqual(len(out.read_text(encoding="utf-8").splitlines()), 1)
            self.assertEqual(len(diag.read_text(encoding="utf-8").splitlines()), 15)
