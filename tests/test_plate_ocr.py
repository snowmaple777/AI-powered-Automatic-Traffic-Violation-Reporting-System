import unittest

import numpy as np

from model_library.perception_core import (PlateOCRTracker, PlateTextRecognizer,
                                           ctc_greedy_decode)

CHARS = list("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-") + ["中"]


def probabilities(text, character, confidence=.95, distractor=None):
    """每個字元前後夾 blank 的 [T, C] softmax；distractor 讓某一步偏向非車牌字元。"""
    rows = []
    for step, char in enumerate(text):
        for index in (0, character.index(char)):
            row = np.full(len(character), (1 - confidence) / (len(character) - 1), np.float32)
            row[index] = confidence
            if index and distractor == step:
                row[:] = .01
                row[character.index("中")], row[index] = .8, .1
            rows.append(row)
    rows.append(rows[0])
    return np.stack(rows)


class FakeRecognizer(PlateTextRecognizer):
    def __init__(self, texts):
        self._setup_characters(CHARS)
        self.texts, self.calls = list(texts), 0

    def _forward(self, tensor):
        text, confidence = self.texts[min(self.calls, len(self.texts) - 1)]
        self.calls += 1
        return probabilities(text, self.character, confidence)


class PlateOcrTests(unittest.TestCase):
    def test_decode_confidence_uses_raw_probability(self):
        recognizer = FakeRecognizer([])
        clean = probabilities("ABC1234", recognizer.character)
        noisy = probabilities("ABC1234", recognizer.character, distractor=2)
        self.assertEqual(ctc_greedy_decode(clean, recognizer.character, recognizer.allowed_indices)[0], "ABC1234")
        text, confidence = ctc_greedy_decode(noisy, recognizer.character, recognizer.allowed_indices)
        self.assertEqual(text, "ABC1234")  # 中文字被排除，但
        self.assertLess(confidence, .85)   # 模型偏向中文的那一步拉低信心（舊算法約 0.96）

    def test_confident_valid_plate_exits_after_one_pass(self):
        recognizer = FakeRecognizer([("ABC1234", .95)])
        self.assertEqual(recognizer.predict(np.zeros((30, 90, 3), np.uint8))[0], "ABC1234")
        self.assertEqual(recognizer.calls, 1)

    def test_invalid_text_tries_enhanced_and_rotations(self):
        recognizer = FakeRecognizer([("AB", .6)])
        text, _ = recognizer.predict(np.zeros((30, 90, 3), np.uint8))
        self.assertEqual(text, "AB")
        self.assertEqual(recognizer.calls, 2 + len(PlateTextRecognizer.ROTATIONS))

    def test_rotation_with_valid_plate_wins(self):
        recognizer = FakeRecognizer([("AB", .6), ("AB", .6), ("ABC1234", .9)])
        text, confidence = recognizer.predict(np.zeros((30, 90, 3), np.uint8))
        self.assertEqual(text, "ABC1234")
        self.assertAlmostEqual(confidence, .9, places=5)
        self.assertEqual(recognizer.calls, 3)

    def test_unsupported_best_read_is_replaced_by_character_vote(self):
        tracker = PlateOCRTracker()
        tracker.update(1, 0, "ABC1284", .95, veh_cls=2)
        tracker.update(1, 4, "ABC1234", .80, veh_cls=2)
        self.assertEqual(tracker.get_plate(1)[0], "ABC-1284")  # 讀數不足三筆，不投票
        tracker.update(1, 8, "ARC1234", .85, veh_cls=2)
        self.assertEqual(tracker.get_plate(1)[0], "ABC-1234")
        self.assertEqual(tracker.get_plate_raw(1), ("ABC1284", .95))  # 畫面顯示原始最佳讀數不變

    def test_supported_best_read_is_not_overridden(self):
        tracker = PlateOCRTracker()
        for frame, text, score in [(0, "ABC1284", .95), (4, "ABC1284", .9),
                                   (8, "ABC1234", .8), (12, "ARC1234", .85), (16, "ABC1234", .8)]:
            tracker.update(1, frame, text, score, veh_cls=2)
        self.assertEqual(tracker.get_plate(1), ("ABC-1284", .95))


if __name__ == "__main__":
    unittest.main()
