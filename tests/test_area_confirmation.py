"""區域確認路徑：直接證據、誤差帶、遮擋、方向及慢速爬行反例。"""
import copy
import unittest
from test_area_rules import records, evaluate
from violation_engine import create_rules, RuleContext


def interior(kind="crosswalk", count=30):
    rows = records(kind, count=count)
    for row in rows:
        row['observations']['traffic_regions'][0]['polygon'] = [[20, 30], [110, 30], [110, 100], [20, 100]]
    return rows


class ConfirmationTests(unittest.TestCase):
    def test_explicit_off_is_identical(self):
        self.assertEqual(evaluate(interior()), evaluate(interior(), {'confirm_crosswalk': False}))

    def test_direct_stay_upgrades_one_event_and_preserves_candidate_time(self):
        old = evaluate(interior())
        new = evaluate(interior(), {'confirm_crosswalk': True})
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]['status'], 'confirmed')
        self.assertEqual(old[0]['event_id'], new[0]['event_id'])
        self.assertEqual(old[0]['timestamp_sec'], new[0]['timestamp_sec'])
        self.assertGreater(new[0]['confirmation_timestamp_sec'], new[0]['timestamp_sec'])

    def test_propagated_frames_cannot_confirm_but_do_not_break_direct_samples(self):
        for alternating in (False, True):
            rows = interior()
            for i, r in enumerate(rows):
                r['observations']['traffic_regions'][0]['marking_source'] = 'direct' if alternating and i % 2 == 0 else 'propagated'
            events = evaluate(rows, {'confirm_crosswalk': True})
            self.assertEqual(events[0]['status'], 'confirmed' if alternating else 'suspected')

    def test_occluded_contact_and_edge_uncertainty_stay_suspected(self):
        rows = interior()
        for r in rows:
            other = copy.deepcopy(r['observations']['vehicles'][0])
            other.update(object_id='occluder', class_name='motorcycle', bbox_xyxy=[40, 50, 80, 95])
            r['observations']['vehicles'].append(other)
        self.assertEqual(evaluate(rows, {'confirm_crosswalk': True})[0]['status'], 'suspected')
        # 原重疊正好 50%，合理的位移擾動後不再成立。
        rows = interior()
        for r in rows:
            r['observations']['traffic_regions'][0]['polygon'] = [[55, 30], [110, 30], [110, 100], [55, 100]]
        self.assertEqual(evaluate(rows, {'confirm_crosswalk': True})[0]['status'], 'suspected')

    def test_creeping_within_short_window_is_not_confirmed(self):
        rows = records(count=35, moving=.6)
        for r in rows:
            r['observations']['traffic_regions'][0]['polygon'] = [[20, 30], [150, 30], [150, 100], [20, 100]]
        events = evaluate(rows, {'confirm_crosswalk': True, 'stationary_window_seconds': .5})
        self.assertTrue(events)
        self.assertTrue(all(e['status'] == 'suspected' for e in events))

    def test_waiting_requires_direction_and_strong_red(self):
        self.assertEqual(evaluate(interior('motorcycle_waiting_area'), {'confirm_waiting_area': True})[0]['status'], 'confirmed')
        rows = interior('motorcycle_waiting_area')
        for r in rows:
            v = r['observations']['vehicles'][0]
            v.pop('lane_id')
            v['road_geometry'].pop('front_edge')
            r['observations']['traffic_lights'] = [dict(class_name='red', confidence=.9, bbox_xyxy=[10,10,20,30])]
        self.assertEqual(evaluate(rows, {'confirm_waiting_area': True})[0]['status'], 'suspected')

    def test_invalid_switch(self):
        with self.assertRaises(ValueError):
            evaluate(interior(), {'confirm_crosswalk': 1})

    def test_waiting_forward_track_can_confirm_but_lateral_cannot(self):
        for lateral in (False, True):
            rows = interior('motorcycle_waiting_area', count=20)
            for i, r in enumerate(rows):
                v = r['observations']['vehicles'][0]
                v.pop('lane_id')
                v['road_geometry'].pop('front_edge')
                dx, dy = (i, 0) if lateral else (0, -i)
                v['bbox_xyxy'] = [45+dx, 20+dy, 65+dx, 70+dy]
                v['road_geometry']['footprint'] = [[45+dx,50+dy],[65+dx,50+dy],[65+dx,70+dy],[45+dx,70+dy]]
                r['observations']['traffic_lights'] = [dict(class_name='red', confidence=.9, bbox_xyxy=[10,10,20,30])]
            events = evaluate(rows, {'confirm_waiting_area': True})
            self.assertTrue(events)
            self.assertEqual(events[0]['status'], 'suspected' if lateral else 'confirmed')

    def test_unconfirmed_event_is_released_at_deadline_once(self):
        rows = interior(count=55)
        for r in rows:
            r['observations']['traffic_regions'][0]['marking_source'] = 'propagated'
        rule = create_rules(['crosswalk_stopping'], RuleContext('test',10,400,300),
                            {'crosswalk_stopping':{'settings':{'confirm_crosswalk':True}}})[0]
        live = []
        for row in rows:
            events = rule.evaluate(row)
            if events:
                self.assertAlmostEqual(row['frame']['timestamp_sec'], 4.)
            live.extend(events)
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]['status'], 'suspected')
        self.assertEqual(rule.finalize(), [])

    def test_waiting_full_occupancy_requires_deep_stationary_footprint(self):
        for moving in (False, True):
            rows = interior('motorcycle_waiting_area', count=30)
            for i, row in enumerate(rows):
                v = row['observations']['vehicles'][0]
                v.pop('lane_id')
                v['road_geometry'].pop('front_edge')
                row['observations']['traffic_lights'] = [dict(class_name='red', confidence=.9,bbox_xyxy=[10,10,20,30])]
                if moving:
                    dx=i*.5
                    v['bbox_xyxy']=[45+dx,20,65+dx,70]
                    v['road_geometry']['footprint']=[[45+dx,50],[65+dx,50],[65+dx,70],[45+dx,70]]
            events = evaluate(rows, {'confirm_waiting_occupancy': True})
            self.assertEqual(events[0]['status'], 'suspected' if moving else 'confirmed')
            if not moving:
                self.assertEqual(events[0]['confirmation_basis'], ['robust_waiting_area_occupancy'])

    def test_tiny_footprint_stays_suspected_with_resolution_gate(self):
        rows = interior()
        for r in rows:
            r['observations']['vehicles'][0]['road_geometry']['footprint'] = [[45,55],[65,55],[65,59],[45,59]]
        self.assertEqual(evaluate(rows, {'confirm_crosswalk':True})[0]['status'], 'confirmed')
        options = {'confirm_crosswalk':True,'confirmation_min_footprint_px':8.}
        self.assertEqual(evaluate(rows, options)[0]['status'], 'suspected')
        self.assertEqual(evaluate(interior(), options)[0]['status'], 'confirmed')
        for value in (0, -1, True, float('nan')):
            with self.assertRaises(ValueError):
                evaluate(rows, dict(options,confirmation_min_footprint_px=value))


if __name__ == '__main__':
    unittest.main()
