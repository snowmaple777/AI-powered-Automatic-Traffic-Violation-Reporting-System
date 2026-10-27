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


class YellowConfirmationTests(unittest.TestCase):
    # 30 fps、預設 0.2 秒 = 6 幀；強證據窗口 0.5 秒 = 15 幀。
    def test_brief_yellow_inside_red_keeps_red(self):
        state = TemporalSignalState(30, yellow_confirm_seconds=.2)
        for index in range(15):
            state.update([light("red", 100)], index)
        for index in range(15, 20):  # 5 幀黃燈，不足 6 幀
            self.assertEqual(state.update([light("yellow", 100)], index), "red", index)
        self.assertEqual(state.update([light("red", 100)], 20), "red")
        self.assertNotIn("pending_yellow", state.tracks[state.selected])

    def test_persistent_yellow_interrupts_red(self):
        state = TemporalSignalState(30, yellow_confirm_seconds=.2)
        for index in range(15):
            state.update([light("red", 100)], index)
        results = [state.update([light("yellow", 100)], index) for index in range(15, 21)]
        self.assertEqual(results[:5], ["red"] * 5)
        self.assertEqual(results[5], "yellow")
        self.assertEqual(state.evidence["reason"], "observed_signal_change")
        self.assertEqual([s[1] for s in state.tracks[state.selected]["history"]], ["yellow"] * 6)  # 紅燈記憶已切斷

    def test_default_zero_keeps_immediate_interruption(self):
        state = TemporalSignalState(10)
        for index in range(5):
            state.update([light("red", 100)], index)
        self.assertEqual(state.update([light("yellow", 100)], 5), "yellow")

    def test_hold_survives_single_yellow_frame_but_not_persistent_yellow(self):
        def held_after(yellow_frames):
            state = CombinedSignalState(10, yellow_confirm_seconds=.3)  # 3 幀
            for index in range(5):
                state.update([light("red", 100)], index)
            for index in range(5, 12):  # 紅燈消失，進入延續
                state.update([], index)
            result = None
            for index in range(12, 12 + yellow_frames):
                result = state.update([light("yellow", 300, .15)], index)  # 另一個低信心黃燈
            return result, state.strength
        self.assertEqual(held_after(1), ("red", "held"))
        self.assertNotEqual(held_after(3)[1], "held")


def sized(color, x, size, confidence):
    return dict(class_name=color, confidence=confidence, bbox_xyxy=[x, 10, x + size, 10 + size * 2])


class DominantTrackTests(unittest.TestCase):
    def run_frames(self, lights, frames=10, **kwargs):
        state = CombinedSignalState(30, **kwargs)
        result = None
        for index in range(frames):
            result = state.update(lights, index)
        return result, state

    def test_clearly_more_confident_light_wins_as_dominant_evidence(self):
        # ms03：近處紅燈信心約 0.65，遠處被讀成黃燈的小號誌約 0.35。
        result, state = self.run_frames([sized("red", 100, 10, .65), sized("yellow", 400, 10, .35)])
        self.assertEqual(result, "red")
        self.assertEqual(state.strength, "dominant")
        self.assertEqual(state.evidence["reason"], "dominant_signal_track")
        self.assertEqual(state.evidence["overridden"], ["yellow"])

    def test_much_larger_light_wins_even_with_similar_confidence(self):
        result, state = self.run_frames([sized("green", 100, 30, .40), sized("red", 400, 10, .45)])
        self.assertEqual(result, "green")
        self.assertEqual(state.strength, "dominant")

    def test_no_clear_winner_stays_unknown(self):
        result, state = self.run_frames([sized("red", 100, 12, .5), sized("green", 400, 10, .45)])
        self.assertIsNone(result)
        self.assertEqual(state.strong.evidence["reason"], "conflicting_signal_tracks")
        result, _ = self.run_frames([sized("red", 100, 10, .65), sized("yellow", 400, 10, .35)],
                                    dominance_confidence_ratio=None)
        self.assertIsNone(result)  # 停用時維持原行為


if __name__ == "__main__":
    unittest.main()
