import copy
import unittest
from rider_tracking import RiderTracker, attach_riders


def objects(vehicle_id='bike-1', rider_id='person-1', offset=0):
    return [dict(object_id=rider_id,class_name='person',bbox_xyxy=[110+offset,20,150+offset,130],bottom_center=[130+offset,130]),
            dict(object_id=vehicle_id,class_name='motorcycle',bbox_xyxy=[100+offset,90,165+offset,170],bottom_center=[132.5+offset,170])]


class RiderTests(unittest.TestCase):
    def test_rule_consumes_group_identity_after_vehicle_id_switch(self):
        from violation_engine import RuleContext, RedLightStopLineCrossingRule
        tracker = RiderTracker(25)
        rule = RedLightStopLineCrossingRule(RuleContext('test',25,400,300),
                                           min_track_frames=1,min_line_stable_frames=1)
        events = []
        for i in range(12):
            rows = objects(vehicle_id='bike-1' if i<8 else 'bike-2')
            shift = 0 if i<8 else -40
            for v in rows:
                v['bbox_xyxy'][1] += shift
                v['bbox_xyxy'][3] += shift
                v['bottom_center'][1] += shift
                v['track_age_frames'] = 100
            row = {'frame':{'index':i,'timestamp_sec':i/25},'observations':{
                'vehicles':rows,'traffic_lights':[{'class_name':'red','confidence':.8,'bbox_xyxy':[100,0,120,20]}],
                'road_markings':[{'class_name':'stop line','components':[{
                    'area':800,'bbox_xywh':[90,146,100,8],'centroid':[140,150],
                    'line_xyxy':[90,150,190,150]}]}]}}
            attach_riders(row,tracker)
            events.extend(rule.evaluate(row))
        self.assertEqual(len(events),1)
        self.assertTrue(events[0]['object_id'].startswith('rider-group-'))
        self.assertEqual(events[0]['evidence']['vehicle']['source_object_id'],'bike-2')

    def test_confirm_and_preserve_ground_point_and_raw_records(self):
        tracker = RiderTracker(30)
        rows = objects()
        original = copy.deepcopy(rows)
        for i in range(3):
            record = {'frame':{'index':i},'observations':{'vehicles':rows}}
            attach_riders(record,tracker)
        group = record['observations']['rider_groups'][0]
        self.assertEqual(group['status'],'confirmed')
        self.assertEqual(group['bottom_center'],[132.5,170])
        self.assertEqual(record['observations']['rule_vehicles'][1]['object_id'],group['object_id'])
        self.assertEqual(rows,original)

    def test_vehicle_id_switch_retains_group_via_rider(self):
        tracker = RiderTracker(30)
        for i in range(3):
            before = tracker.update(objects(),i)[0]
        after = tracker.update(objects(vehicle_id='bike-2',offset=3),3)[0]
        self.assertEqual(before['object_id'],after['object_id'])
        self.assertEqual(after['status'],'confirmed')

    def test_ambiguous_overlap_is_not_associated(self):
        tracker = RiderTracker(30)
        rows = objects()
        rows.append(dict(rows[1],object_id='bike-2'))
        for i in range(5):
            self.assertEqual(tracker.update(rows,i),[])

    def test_walking_beside_vehicle_is_not_associated(self):
        rows = objects()
        rows[0]['bbox_xyxy'] = [20,20,70,170]
        self.assertEqual(RiderTracker(30).update(rows,0),[])

    def test_missing_vehicle_has_no_invented_ground_point(self):
        tracker = RiderTracker(30)
        first = tracker.update(objects(),0)[0]
        self.assertEqual(tracker.update(objects()[:1],1),[])
        later = tracker.update(objects(),20)[0]
        self.assertNotEqual(first['object_id'],later['object_id'])
        self.assertEqual(later['status'],'pending')

    def test_large_jump_does_not_transfer_identity(self):
        tracker = RiderTracker(30)
        first = tracker.update(objects(),0)[0]
        later = tracker.update(objects(offset=300),1)[0]
        self.assertNotEqual(first['object_id'],later['object_id'])
