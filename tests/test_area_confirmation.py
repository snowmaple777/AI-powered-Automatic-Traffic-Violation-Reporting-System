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

    def test_invalid_switch(self):
        with self.assertRaises(ValueError):
            evaluate(interior(), {'confirm_crosswalk': 1})
        with self.assertRaises(ValueError):  # 4bh 移除的車頭方向確認
            evaluate(interior('motorcycle_waiting_area'), {'confirm_waiting_area': True})

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


def side_rows(count=20, box=(2, 20, 40, 70), clipped=False, region=((0, 40), (100, 40), (100, 90), (0, 90))):
    """沒有車道、貼左緣的汽車停在停等區內（紅燈）。clipped=True 時模擬裁切車框（沒有接地幾何）。"""
    rows = records('motorcycle_waiting_area', count=count)
    x1, y1, x2, y2 = box
    for row in rows:
        row['observations']['traffic_regions'][0]['polygon'] = [list(p) for p in region]
        row['observations']['traffic_lights'] = [dict(class_name='red', confidence=.9, bbox_xyxy=[10, 10, 20, 30])]
        v = row['observations']['vehicles'][0]
        v.pop('lane_id')
        v['bbox_xyxy'] = [x1, y1, x2, y2]
        if clipped:
            v.pop('road_geometry')
            v['geometry_unavailable_reason'] = 'clipped_vehicle'
        else:
            v['road_geometry'].pop('front_edge')
            v['road_geometry']['footprint'] = [[x1, y2 - 15], [x2, y2 - 15], [x2, y2], [x1, y2]]
    return rows


class SideOccupancyTests(unittest.TestCase):
    """4bl waiting_side_occupancy_ratio：貼邊車輛以接地佔用比例產生疑似。"""
    ON = {'waiting_side_occupancy_ratio': .65}

    def test_default_keeps_front_direction_block(self):
        self.assertEqual(evaluate(side_rows()), [])
        self.assertEqual(evaluate(side_rows(clipped=True)), [])

    def test_edge_vehicle_inside_region_is_suspected_only(self):
        for confirm in (False, True):
            events = evaluate(side_rows(), dict(self.ON, confirm_waiting_occupancy=confirm))
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event['status'], 'suspected')
            self.assertIs(event['conditions']['front_direction_available'], False)
            self.assertIs(event['conditions']['front_edge_from_lane'], False)
            self.assertEqual(event['evidence']['measurement_name'], 'side_footprint_overlap_ratio')
            self.assertAlmostEqual(event['evidence']['measurement'], 1.)
            self.assertAlmostEqual(event['evidence']['duration_sec'], .5)  # 佔用路徑持續 0.5 秒

    def test_clipped_box_needs_visible_bottom(self):
        events = evaluate(side_rows(box=(0, 0, 40, 70), clipped=True), self.ON)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['evidence']['vehicle']['bbox_xyxy'], [0, 0, 40, 70])
        self.assertEqual(evaluate(side_rows(box=(0, 200, 40, 300), clipped=True), self.ON), [])

    def test_partial_overlap_and_short_stay_are_not_reported(self):
        region = ((25, 40), (100, 40), (100, 90), (25, 90))  # 接地範圍約 40% 在區域內
        self.assertEqual(evaluate(side_rows(region=region), self.ON), [])
        self.assertEqual(len(evaluate(side_rows(region=region), {'waiting_side_occupancy_ratio': .3})), 1)
        self.assertEqual(evaluate(side_rows(count=5), self.ON), [])

    def test_vehicle_away_from_edges_keeps_front_path(self):
        rows = side_rows(box=(45, 20, 65, 70))
        old, new = evaluate(rows), evaluate(rows, self.ON)
        self.assertEqual(len(old), 1)
        self.assertEqual(old[0]['timestamp_sec'], new[0]['timestamp_sec'])
        self.assertEqual(old[0]['evidence']['measurement_name'], new[0]['evidence']['measurement_name'])
        self.assertIs(new[0]['conditions']['front_direction_available'], True)
        self.assertNotIn('front_direction_available', old[0]['conditions'])

    def test_paths_do_not_share_observations(self):
        # 前 4 幀貼邊（不足 0.5 秒，不報），之後離開邊緣：車頭路徑事件時間與未啟用時相同。
        rows = side_rows(box=(45, 20, 65, 70))
        edge = side_rows(count=4)
        for i in range(4):
            rows[i]['observations']['vehicles'][0] = edge[i]['observations']['vehicles'][0]
        old, new = evaluate(rows), evaluate(rows, self.ON)
        self.assertEqual([e['timestamp_sec'] for e in old], [e['timestamp_sec'] for e in new])
        self.assertEqual(len(new), 1)
        self.assertIs(new[0]['conditions']['front_direction_available'], True)

    def test_invalid_ratio(self):
        for value in (0, -.1, 1.5, True, float('nan'), '0.5'):
            with self.assertRaises(ValueError):
                evaluate(side_rows(), {'waiting_side_occupancy_ratio': value})


if __name__ == '__main__':
    unittest.main()
