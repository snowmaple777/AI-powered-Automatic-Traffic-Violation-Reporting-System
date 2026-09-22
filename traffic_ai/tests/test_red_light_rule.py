"""闖紅燈漏判回歸：不以指定車號或影片時間作為觸發條件。"""
import json
from pathlib import Path
import unittest
from signal_state import TemporalSignalState
from violation_engine import RuleContext, RedLightStopLineCrossingRule


def light(color="red", confidence=.8, x=100):
    return {"class_name":color,"confidence":confidence,"bbox_xyxy":[x,10,x+20,30]}


def observation(index, y, color="red", line_y=100, object_id="test-vehicle"):
    return {"frame":{"index":index,"timestamp_sec":index/25},"observations":{
        "vehicles":[{"object_id":object_id,"class_name":"truck","track_age_frames":100,
                     "bbox_xyxy":[120,y-40,180,y],"bottom_center":[150,y]}],
        "traffic_lights":[] if color is None else [light(color)],
        "road_markings":[{"class_name":"stop line","class_id":3,"pixels":800,
            "components":[{"area":800,"bbox_xywh":[100,line_y-4,100,8],
                           "centroid":[150,line_y],"line_xyxy":[100,line_y,200,line_y]}]}]}}


def rule(**options):
    return RedLightStopLineCrossingRule(RuleContext("test",25,400,300),
                                       min_track_frames=1,min_line_stable_frames=1,**options)


class SignalTests(unittest.TestCase):
    def test_intermittent_support_requires_high_confidence_anchor(self):
        tracker=TemporalSignalState(25)
        self.assertIsNone(tracker.update([light(confidence=.2)],0))
        self.assertIsNone(tracker.update([light(confidence=.22)],2))
        self.assertIsNone(tracker.update([light(confidence=.25)],4))
        self.assertEqual(tracker.update([light(confidence=.35)],5),"red")
        self.assertEqual(tracker.update([],8),"red")
        self.assertIsNone(tracker.update([],11))

    def test_duplicate_boxes_are_not_multiple_temporal_votes(self):
        tracker=TemporalSignalState(25)
        self.assertIsNone(tracker.update([light(),light(x=101),light(x=102)],0))
        self.assertEqual(len(tracker.tracks),1)

    def test_green_change_cancels_red_immediately(self):
        tracker=TemporalSignalState(25)
        for i in range(3):
            tracker.update([light()],i)
        self.assertEqual(tracker.update([light("green",.2)],3),"green")
        self.assertIsNone(tracker.update([],4))

    def test_spatially_distinct_conflicting_signals_are_unknown(self):
        tracker=TemporalSignalState(25)
        for i in range(3):
            state=tracker.update([light(),light("green",x=300)],i)
        self.assertIsNone(state)
        self.assertEqual(tracker.evidence["reason"],"conflicting_signal_tracks")


class CrossingTests(unittest.TestCase):
    def test_partial_crossing_is_flushed_once_with_missing_conditions(self):
        detector = rule()
        for i in range(20):
            detector.evaluate(observation(i,130,"red" if i<5 else None))
        for i in range(20,25):
            self.assertEqual(detector.evaluate(observation(i,70,None)), [])
        events = detector.finalize()
        self.assertEqual(len(events),1)
        event = events[0]
        self.assertEqual(event['status'],'suspected')
        self.assertTrue(event['conditions']['crossing_observed'])
        self.assertIn('red_at_crossing',event['missing_conditions'])
        self.assertEqual(event['evidence']['signal_state'],'unknown')
        self.assertTrue(event['description'])
        self.assertEqual(detector.finalize(),[])

    def test_partial_is_replaced_by_confirmation(self):
        detector = rule()
        events = []
        for i,y in enumerate([130]*5+[70]*5):
            events.extend(detector.evaluate(observation(i,y)))
        events.extend(detector.finalize())
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['status'],'confirmed')
        self.assertEqual(events[0]['missing_conditions'],[])

    def test_red_only_stationary_before_line_is_not_suspected(self):
        detector = rule()
        for i in range(12):
            self.assertEqual(detector.evaluate(observation(i,130)),[])
        self.assertEqual(detector.finalize(),[])

    def test_beyond_line_without_crossing_is_not_suspected(self):
        detector = rule()
        row = None
        for i in range(8):
            row = observation(i,70)
            self.assertEqual(detector.evaluate(row),[])
        row['observations']['vehicles'][0]['class_name'] = 'changed'
        self.assertEqual(detector.finalize(),[])

    def test_upper_box_overlap_and_moving_top_do_not_imply_crossing(self):
        detector = rule()
        for i in range(12):
            row = observation(i,150)
            vehicle = row['observations']['vehicles'][0]
            vehicle['bbox_xyxy'] = [120,30+i*3,180,150]
            # Even a stale/incorrect center must not move the actual box bottom.
            vehicle['bottom_center'] = [150,130 if i<5 else 70]
            self.assertEqual(detector.evaluate(row),[])
        self.assertEqual(detector.finalize(),[])

    def test_group_uses_vehicle_bottom_not_rider_display_box(self):
        detector = rule()
        for i in range(12):
            row = observation(i,130 if i<5 else 70)
            row['observations']['vehicles'][0]['rider_group'] = {
                'vehicle_bbox_xyxy':[120,80,180,150]}
            self.assertEqual(detector.evaluate(row),[])
        self.assertEqual(detector.finalize(),[])

    def test_partial_crossing_evidence_snapshot_is_immutable(self):
        detector = rule()
        for i in range(5):
            detector.evaluate(observation(i,130))
        row = observation(5,70)
        detector.evaluate(row)
        row['observations']['vehicles'][0]['class_name'] = 'changed'
        event = detector.finalize()[0]
        self.assertEqual(event['status'],'suspected')
        self.assertEqual(event['evidence']['vehicle']['class_name'],'truck')

    def test_green_crossing_has_no_partial_event(self):
        detector = rule()
        for i,y in enumerate([130]*5+[70]*5):
            detector.evaluate(observation(i,y,'green'))
        self.assertEqual(detector.finalize(),[])

    def test_person_is_excluded_with_explicit_reason(self):
        detector = rule()
        row = observation(0, 130)
        row["observations"]["vehicles"][0]["class_name"] = "person"
        self.assertEqual(detector.evaluate(row), [])
        self.assertIn("unsupported_vehicle_class",
                      detector.diagnostics["skipped_vehicles"][0]["blocked_by"])

    def test_disjoint_line_is_not_extended_to_vehicle_lane(self):
        detector = rule()
        for i in range(8):
            row = observation(i, 130 if i < 5 else 70)
            row["observations"]["vehicles"][0].update(
                bbox_xyxy=[250, 30, 300, 130], bottom_center=[275, 130])
            self.assertEqual(detector.evaluate(row), [])
        self.assertEqual(detector.diagnostics["vehicles"], [])
        self.assertIn("no_overlapping_stop_line",
                      detector.diagnostics["skipped_vehicles"][0]["blocked_by"])

    def test_diagnostics_explain_missing_crossing(self):
        detector = rule()
        for i in range(10):
            self.assertEqual(detector.evaluate(observation(i, 130)), [])
        self.assertEqual(detector.diagnostics["vehicles"][0]["blocked_by"],
                         ["insufficient_after_line_observations"])

    def test_crossing_with_expired_signal_stays_unconfirmed(self):
        detector = rule()
        for i in range(20):
            detector.evaluate(observation(i, 130, "red" if i < 5 else None))
        for i in range(20, 23):
            self.assertEqual(detector.evaluate(observation(i, 70, None)), [])
        evidence = detector.diagnostics["vehicles"][0]
        self.assertGreaterEqual(evidence["after_count"], 2)
        self.assertIn("signal_not_confirmed_red", evidence["blocked_by"])
        self.assertIn("no_recent_red_before_crossing", evidence["blocked_by"])

    def test_away_crossing_can_skip_zero_band(self):
        detector=rule()
        events=[]
        for i,y in enumerate([130]*5+[70]*3):
            events.extend(detector.evaluate(observation(i,y)))
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]["evidence"]["crossing_direction"],"away")

    def test_reverse_direction_does_not_trigger_away_rule(self):
        detector=rule()
        events=[]
        for i,y in enumerate([70]*5+[130]*3):
            events.extend(detector.evaluate(observation(i,y)))
        self.assertEqual(events,[])

    def test_toward_direction_is_explicitly_supported(self):
        detector=rule(crossing_direction="toward")
        events=[]
        for i,y in enumerate([70]*5+[130]*3):
            events.extend(detector.evaluate(observation(i,y)))
        self.assertEqual(len(events),1)

    def test_green_crossing_and_later_red_do_not_create_event(self):
        detector=rule()
        events=[]
        for i in range(15):
            events.extend(detector.evaluate(observation(i,130 if i<5 else 70,"green" if i<9 else "red")))
        self.assertEqual(events,[])

    def test_long_gap_cannot_join_unrelated_positions(self):
        detector=rule()
        for i in range(5):
            detector.evaluate(observation(i,130))
        events=[]
        for i in range(25,32):
            events.extend(detector.evaluate(observation(i,70)))
        self.assertEqual(events,[])

    def test_vehicle_and_line_move_together_without_crossing(self):
        detector=rule()
        events=[]
        for i in range(15):
            events.extend(detector.evaluate(observation(i,130+i*3,line_y=100+i*3)))
        self.assertEqual(events,[])

    def test_existing_red_cleared_on_green_at_boundary(self):
        detector=rule()
        events=[]
        for i in range(10):
            events.extend(detector.evaluate(observation(i,130 if i<5 else 70,"red" if i<5 else "green")))
        self.assertEqual(events,[])

    def test_ms01_blue_truck_regression_and_negative_controls(self):
        path=Path(__file__).parent/"fixtures/ms01_red_light_regression.jsonl"
        rows=[json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        detector=RedLightStopLineCrossingRule(RuleContext("ms01",25,1920,1080))
        events=[event for row in rows for event in detector.evaluate(row)]
        self.assertEqual([(e["object_id"],e["frame"]) for e in events],[("vehicle-000001",86)])
        self.assertEqual(events[0]["vehicle_class"],"truck")
        self.assertTrue(events[0]["review_required"])
        # 相同幾何，移除紅燈或改為綠燈，不能依影片時間／車號硬產生候選。
        for lights in ([],[light("green")]):
            detector=RedLightStopLineCrossingRule(RuleContext("ms01",25,1920,1080))
            events=[]
            for row in rows:
                row["observations"]["traffic_lights"]=lights
                events.extend(detector.evaluate(row))
            self.assertEqual(events,[])


if __name__=="__main__":
    unittest.main()
