"""規則設定組合（4by、4ca）：high_recall 設定檔只比預設多四個召回選項，且三個入口都能切換。"""
import json
import unittest
from pathlib import Path

import run_pipeline
from area_rules import area_rule_options
from double_lines import double_line_geometry_options, double_line_rule_options
from inference_service import jobs
from rule_profiles import PROFILES, apply_profile
from violation_engine import red_light_rule_options

ROOT = Path(__file__).resolve().parents[1]
DEFAULT, HIGH = ROOT / 'configs', ROOT / 'configs' / 'high_recall'


def raw(path):
    data = json.loads(path.read_text(encoding='utf-8'))
    data.pop('_說明', None)
    return data


class HighRecallConfigTests(unittest.TestCase):
    def test_differs_from_defaults_only_by_the_recall_options(self):
        # 預設設定日後改了，high_recall 也要跟著改；這裡確保兩邊只差這幾個召回選項
        expected = {
            'red_light_rules.json': {'stop_line_extension_widths': 1.0},
            'area_rules.json': {'waiting_side_occupancy_ratio': 0.8, 'crosswalk_parking_mode': True},
            'double_line_rules.json': {'geometry': {'trim_wide_rows': True}},
        }
        for name, extra in expected.items():
            base, high = raw(DEFAULT / name), raw(HIGH / name)
            self.assertEqual({k: v for k, v in high.items() if k not in extra}, base, name)
            self.assertEqual({k: high[k] for k in extra}, extra, name)

    def test_configs_load_through_the_normal_readers(self):
        self.assertEqual(red_light_rule_options(HIGH / 'red_light_rules.json')['stop_line_extension_widths'], 1.0)
        area = area_rule_options(HIGH / 'area_rules.json')
        self.assertEqual(area['car_motorcycle_waiting_area']['settings']['waiting_side_occupancy_ratio'], 0.8)
        self.assertTrue(area['crosswalk_stopping']['settings']['crosswalk_parking_mode'])
        self.assertEqual(double_line_geometry_options(HIGH / 'double_line_rules.json'), {'trim_wide_rows': True})
        self.assertEqual(double_line_rule_options(HIGH / 'double_line_rules.json'),
                         double_line_rule_options(DEFAULT / 'double_line_rules.json'))  # 雙線規則參數相同，只差幾何


class ProfileArgumentTests(unittest.TestCase):
    def test_run_pipeline_switches_all_three_configs(self):
        args = run_pipeline.parse_args(['x.mp4', '--rule-profile', 'high_recall'])
        self.assertEqual((args.red_light_config, args.area_rule_config, args.double_line_config),
                         (HIGH / 'red_light_rules.json', HIGH / 'area_rules.json', HIGH / 'double_line_rules.json'))
        default = run_pipeline.parse_args(['x.mp4'])
        self.assertEqual(default.red_light_config, DEFAULT / 'red_light_rules.json')

    def test_explicit_config_wins_over_profile(self):
        mine = ROOT / 'tests' / 'my_red_light.json'
        args = run_pipeline.parse_args(['x.mp4', '--rule-profile', 'high_recall', '--red-light-config', str(mine)])
        self.assertEqual(args.red_light_config, mine)
        self.assertEqual(args.area_rule_config, HIGH / 'area_rules.json')

    def test_apply_profile_without_attribute_is_default(self):
        class Args:
            area_rule_config = DEFAULT / 'area_rules.json'
        self.assertEqual(apply_profile(Args()).area_rule_config, DEFAULT / 'area_rules.json')

    def test_inference_service_knows_the_same_profiles(self):
        self.assertEqual(set(jobs.RULE_PROFILES), set(PROFILES))


if __name__ == '__main__':
    unittest.main()
