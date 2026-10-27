"""燈號時間證據：箭頭燈不參與紅／綠／黃判定。"""
import unittest

from signal_state import CombinedSignalState, TemporalSignalState


def light(color, x, confidence=.9):
    return dict(class_name=color, confidence=confidence, bbox_xyxy=[x, 10, x + 10, 30])


class SignalStateTests(unittest.TestCase):
    def test_arrow_next_to_red_does_not_create_conflict(self):
        state = TemporalSignalState(10)
        for index in range(5):
            result = state.update([light("red", 100), light("arrow", 120)], index)
        self.assertEqual(result, "red")
        self.assertNotIn("arrow", {t["history"][-1][1] for t in state.tracks.values() if t["history"]})

    def test_arrow_alone_is_not_a_signal_and_does_not_cancel_held_red(self):
        state = CombinedSignalState(10)
        for index in range(5):
            state.update([light("red", 100)], index)
        for index in range(5, 10):  # 紅燈證據過期後只剩箭頭燈：維持紅燈延續，不被當成轉燈
            result = state.update([light("arrow", 120)], index)
        self.assertEqual(result, "red")
        self.assertEqual(state.strength, "held")
        self.assertIsNone(CombinedSignalState(10).update([light("arrow", 120)] , 0))


if __name__ == "__main__":
    unittest.main()
