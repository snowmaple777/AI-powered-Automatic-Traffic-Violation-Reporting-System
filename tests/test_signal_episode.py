"""R1：只連接可解釋的短暫紅燈換號，反證／長缺口不升級。"""
import unittest
from test_red_light_rule import frame, LINE_A
from violation_engine import RedLightStopLineCrossingRule, RuleContext


def scenario(enabled, gap=0, contrary=False, shifted=False):
    rule = RedLightStopLineCrossingRule(RuleContext('t', 25, 1920, 1080),
                                       confirm_signal_episode=enabled)
    events = []
    for i in range(20 + gap):
        item = frame(i, 760 if i < 10 else 640, [LINE_A])
        if i == 10:
            rule.signal_tracker.strong.tracks.clear()
        if 10 <= i < 10 + gap:
            item['observations']['traffic_lights'] = []
        if shifted and i >= 10:
            item['observations']['traffic_lights'][0]['bbox_xyxy'] = [400, 100, 420, 140]
        if contrary and i == 10:
            item['observations']['traffic_lights'].append(dict(class_name='green', confidence=.5, bbox_xyxy=[400,100,420,140]))
        events += rule.evaluate(item)
    return events + rule.finalize()


class EpisodeTests(unittest.TestCase):
    def test_short_same_position_reidentification_can_confirm(self):
        self.assertEqual(scenario(False)[0]['status'], 'suspected')
        events = scenario(True)
        self.assertEqual(events[0]['status'], 'confirmed')
        self.assertEqual(events[0]['confirmation_basis'], ['same_direct_red_episode'])
        self.assertFalse(events[0]['conditions']['same_signal'])

    def test_contrary_color_long_gap_and_different_position_do_not_confirm(self):
        for options in ({'gap': 10}, {'contrary': True}, {'shifted': True}):
            self.assertFalse(any(e['status'] == 'confirmed' for e in scenario(True, **options)))


if __name__ == '__main__':
    unittest.main()
