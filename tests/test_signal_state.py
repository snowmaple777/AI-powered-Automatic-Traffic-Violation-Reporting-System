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


class SelectedContinuityTests(unittest.TestCase):
    """紅轉綠後主燈號變綠、側邊另有紅燈（行人／橫向號誌）：主燈號延續只會解出綠／黃，不會解出紅。"""

    def run_sequence(self, continuity, after):
        state = TemporalSignalState(10, selected_continuity=continuity)
        for index in range(5):  # 只有主燈號：確立為紅燈並成為 selected
            state.update([light("red", 100)], index)
        result = None
        for index in range(5, 15):
            result = state.update(after, index)
        return state, result

    def test_main_signal_turning_green_beside_side_red_is_green(self):
        state, result = self.run_sequence(True, [light("green", 100), light("red", 300, .6)])
        self.assertEqual(result, "green")
        self.assertEqual(state.evidence["reason"], "selected_signal_continuity")
        self.assertEqual(state.evidence["conflicting_colors"], ["green", "red"])

    def test_without_continuity_the_conflict_stays_unknown(self):
        state, result = self.run_sequence(False, [light("green", 100), light("red", 300, .6)])
        self.assertIsNone(result)
        self.assertEqual(state.evidence["reason"], "conflicting_signal_tracks")

    def test_red_main_signal_beside_side_green_stays_unknown(self):
        # 主燈號仍是紅燈：不得用延續新增紅燈證據
        state, result = self.run_sequence(True, [light("red", 100), light("green", 300, .6)])
        self.assertIsNone(result)
        self.assertEqual(state.evidence["reason"], "conflicting_signal_tracks")

    def test_combined_state_passes_the_option_to_both_trackers(self):
        state = CombinedSignalState(10, selected_continuity=True)
        self.assertTrue(state.strong.selected_continuity and state.weak.selected_continuity)
        self.assertFalse(CombinedSignalState(10).strong.selected_continuity)


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


if __name__ == "__main__":
    unittest.main()
