"""畫面浮水印讀取（ui/core/overlay_scan.py）：OCR 文字的解析與投票、找疊字的文字列。
有浮水印辨識模型（PP-OCRv6_small_rec，下載模型.cmd）與 onnxruntime 時，再用合成影片跑一次完整流程。"""
import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import numpy as np

# 專案根目錄也有 core 套件（core.timing），把 ui/core 以另一個名字載入，避免名稱衝突
_UI_CORE = Path(__file__).resolve().parents[1] / 'ui' / 'core'
if 'ui_core' not in sys.modules:
    _spec = importlib.util.spec_from_file_location('ui_core', _UI_CORE / '__init__.py',
                                                   submodule_search_locations=[str(_UI_CORE)])
    sys.modules['ui_core'] = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(sys.modules['ui_core'])
from ui_core import overlay_scan as ov  # noqa: E402

TODAY = date(2026, 11, 28)


def coords(text):
    return ov.parse_coords(ov._normalize(text))


class ParseCoordsTests(unittest.TestCase):
    def test_decimal(self):
        self.assertEqual(coords('E121.519922 N25.102881 angle:132 35 km/h'), (25.102881, 121.519922))
        self.assertEqual(coords('000KM/H N:25.0048 E:121.3072'), (25.0048, 121.3072))

    def test_space_after_decimal_point(self):
        # 等寬字型常被讀成「N25. 102942」
        self.assertEqual(coords('E121.519857 N25. 102942'), (25.102942, 121.519857))

    def test_dms(self):
        lat, lon = coords('N 24°15\'19.4" E 120°43\'47.3"')
        self.assertAlmostEqual(lat, 24 + 15 / 60 + 19.4 / 3600, places=6)
        self.assertAlmostEqual(lon, 120 + 43 / 60 + 47.3 / 3600, places=6)

    def test_dms_minute_mark_read_as_degree(self):
        lat, lon = coords('N 24°16°5.96" E 120°48°5.61"')
        self.assertAlmostEqual(lat, 24 + 16 / 60 + 5.96 / 3600, places=6)
        self.assertAlmostEqual(lon, 120 + 48 / 60 + 5.61 / 3600, places=6)

    def test_dms_minute_mark_missing(self):
        lat, lon = coords('N 22°5934.0" E 120°1738.3"')
        self.assertAlmostEqual(lat, 22 + 59 / 60 + 34.0 / 3600, places=6)
        self.assertAlmostEqual(lon, 120 + 17 / 60 + 38.3 / 3600, places=6)

    def test_minutes_split_by_space(self):
        # s01：「120°15'29.4"」被讀成「120°1 5'29.4"」，不能變成 1 分 5 秒
        lat, lon = coords('N 23°00\'6.98" E 120°1 5\'29.4"')
        self.assertAlmostEqual(lon, 120 + 15 / 60 + 29.4 / 3600, places=6)

    def test_minute_mark_does_not_end_seconds(self):
        self.assertEqual(coords("E 120°1 5'29"), (None, None))

    def test_garbage_is_not_split_into_minutes_and_seconds(self):
        # 「120°15cue」不能被拆成 1 分 5 秒
        self.assertEqual(coords('N23°00E.98" € 120°15cue*'), (None, None))

    def test_outside_taiwan_is_ignored(self):
        self.assertEqual(coords('N35.681236 E139.767125'), (None, None))

    def test_half_readable(self):
        lat, lon = coords('N 22°5935.2" E 120°9738.5"')  # 經度的分讀成 97：只回傳緯度
        self.assertIsNotNone(lat)
        self.assertIsNone(lon)


class ParseDateTimeTests(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(ov.parse_date_parts(ov._normalize('2021/12/28 17:11:56 MotoDVR'), TODAY), [(2021, 12, 28)])
        self.assertEqual(ov.parse_times('2021/12/28 17:11:56 MotoDVR'), [17 * 3600 + 11 * 60 + 56])

    def test_one_covered_year_digit(self):
        # 貼圖蓋住年份的一個字：補成最近、不晚於今年的年份
        self.assertEqual(ov.parse_date_parts(ov._normalize('20¥6/09/11 08:48:12'), TODAY), [(2026, 9, 11)])
        self.assertEqual(ov.parse_date_parts(ov._normalize('2¥26/09/14 08:48:11'), TODAY), [(2026, 9, 14)])

    def test_covered_separator(self):
        self.assertEqual(ov.parse_date_parts(ov._normalize('2026¥09/ 11 08:48:13'), TODAY), [(2026, 9, 11)])

    def test_invalid_fields_become_none(self):
        self.assertEqual(ov.parse_date_parts('2028/19/11', TODAY), [(None, None, 11)])

    def test_invalid_time(self):
        self.assertEqual(ov.parse_times('45:37:34 000KM/H'), [])

    def test_vote_date_per_field(self):
        parts = [(2026, 9, 14), (2026, 9, 11), (2026, 9, 11), (2026, 8, 11), (None, None, 11)]
        self.assertEqual(ov.vote_date(parts, 2, TODAY), (date(2026, 9, 11), 3))

    def test_vote_date_tie_is_rejected(self):
        parts = [(2021, 12, 28), (2021, 12, 28), (2021, 12, 26), (2021, 12, 26)]
        self.assertEqual(ov.vote_date(parts, 2, TODAY), (None, 0))

    def test_vote_date_rejects_future(self):
        self.assertEqual(ov.vote_date([(2026, 12, 25)] * 3, 2, TODAY), (None, 0))


class WinnerTests(unittest.TestCase):
    def dist(self, a, b):
        return abs(a - b)

    def test_majority(self):
        votes = [24.2554, 24.2555, 24.2554, 24.7554]
        self.assertEqual(sorted(ov._winner(votes, 0.003, self.dist)), [24.2554, 24.2554, 24.2555])

    def test_two_wrong_reads_cannot_beat_one_right_read(self):
        # s01：經度只讀到三次，兩次讀錯而且剛好一樣，一次讀對 → 不填
        self.assertEqual(ov._winner([120.2579, 120.7557, 120.7582], 0.003, self.dist), [])

    def test_two_reads_without_conflict(self):
        self.assertEqual(ov._winner([22.9995, 22.9996], 0.003, self.dist), [22.9995, 22.9996])

    def test_tie_means_unreadable(self):
        self.assertEqual(ov._winner([22.9930, 22.9931, 22.9597, 22.9598], 0.003, self.dist), [])

    def test_single_vote_is_not_enough(self):
        self.assertEqual(ov._winner([22.9930], 0.003, self.dist), [])


@unittest.skipIf(ov.cv2 is None, '需要 opencv-python')
class FindTextLinesTests(unittest.TestCase):
    def test_static_text_over_moving_road(self):
        cv2 = ov.cv2
        rng = np.random.default_rng(0)
        road = cv2.GaussianBlur(rng.integers(40, 200, (1080 + 400, 1920), dtype=np.uint8), (0, 0), 2)
        frames = []
        for i in range(6):
            bg = cv2.cvtColor(road[i * 60:i * 60 + 1080], cv2.COLOR_GRAY2BGR)  # 路面往前捲動
            cv2.putText(bg, f'2024/05/12 14:23:4{i // 2}', (1300, 1040), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 6)
            cv2.putText(bg, f'2024/05/12 14:23:4{i // 2}', (1300, 1040), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
            frames.append((i * 0.5, bg))
        boxes = ov.find_text_lines(frames)
        self.assertTrue(any(x0 <= 1310 and x1 >= 1600 and y0 <= 1020 <= y1 for x0, y0, x1, y1, _ in boxes), boxes)


class ScanUnavailableTests(unittest.TestCase):
    def test_missing_model_reports_reason(self):
        res = ov.scan('不存在.mp4', model_path=os.path.join(tempfile.gettempdir(), 'no_such_model.onnx'))
        self.assertFalse(res['available'])
        self.assertTrue(res['reason'])
        self.assertIsNone(res['recorded_at'])


@unittest.skipUnless(ov.cv2 is not None and ov.get_recognizer()[0] is not None,
                     '需要 opencv-python、onnxruntime 與浮水印辨識模型（下載模型.cmd）')
class ScanEndToEndTests(unittest.TestCase):
    def test_synthetic_dashcam_video(self):
        cv2 = ov.cv2
        rng = np.random.default_rng(1)
        road = cv2.GaussianBlur(rng.integers(30, 190, (720 + 800, 1280), dtype=np.uint8), (0, 0), 2)
        fd, path = tempfile.mkstemp(suffix='.avi')
        os.close(fd)
        try:
            fps = 10
            out = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*'MJPG'), fps, (1280, 720))
            for i in range(fps * 4):
                frame = cv2.cvtColor(road[i * 15:i * 15 + 720], cv2.COLOR_GRAY2BGR)
                for text, y in ((f'2024/05/12 14:23:{45 + i // fps:02d}', 655), ('N25.033964 E121.564468', 695)):
                    cv2.putText(frame, text, (820, y), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 5)
                    cv2.putText(frame, text, (820, y), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
                out.write(frame)
            out.release()
            res = ov.scan(path)
        finally:
            os.remove(path)
        self.assertTrue(res['available'])
        self.assertEqual(res['recorded_at'], '2024-05-12T14:23:45')
        self.assertAlmostEqual(res['lat'], 25.033964, places=4)
        self.assertAlmostEqual(res['lon'], 121.564468, places=4)


if __name__ == '__main__':
    unittest.main()
