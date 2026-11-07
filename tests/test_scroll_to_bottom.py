import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_reply

HWND = 601


class TestScrollToBottom(unittest.TestCase):
    def setUp(self):
        mock.patch.object(auto_reply.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def test_a_window_you_are_viewing_is_never_scrolled(self):
        with mock.patch.object(auto_reply, "_scroll_chat") as scroll:
            auto_reply._scroll_chat_to_bottom(HWND, True)
        scroll.assert_not_called()

    def frames(self, *levels):
        from PIL import Image
        return [Image.new("L", (64, 64), v) for v in levels]

    def run_bottom(self, levels, scroll_ok=True):
        sigs = iter(self.frames(*levels))
        with mock.patch.object(auto_reply, "_scroll_chat", return_value=scroll_ok) as scroll,              mock.patch.object(auto_reply, "_wait_until_chat_settled", return_value=True),              mock.patch.object(auto_reply, "_chat_signature", side_effect=lambda h: next(sigs)):
            auto_reply._scroll_chat_to_bottom(HWND, False)
        return None, scroll

    def test_keeps_scrolling_down_until_the_screen_stops_changing(self):
        _, scroll = self.run_bottom([10, 60, 120, 120, 120])             # 前三輪畫面都在動，第四輪沒動 = 到底
        self.assertEqual(scroll.call_count, 4)
        for call in scroll.call_args_list:
            self.assertEqual(call.args, (HWND, -auto_reply.SCROLL_TO_BOTTOM_NOTCHES))     # 往下捲 (負數)

    def test_already_at_the_bottom_costs_two_rounds(self):
        _, scroll = self.run_bottom([50, 50, 50])
        self.assertEqual(scroll.call_count, 2)

    def test_never_scrolls_more_than_the_round_limit(self):
        _, scroll = self.run_bottom(list(range(0, 240, 10)))              # 畫面一直在變
        self.assertEqual(scroll.call_count, auto_reply.SCROLL_TO_BOTTOM_MAX_ROUNDS)

    def test_gives_up_when_the_list_cannot_be_scrolled_and_says_so(self):
        with mock.patch.object(auto_reply, "log_v") as log:
            _, scroll = self.run_bottom([1], scroll_ok=False)
        self.assertEqual(scroll.call_count, 1)
        self.assertTrue(any("沒有捲動" in str(c.args[0]) for c in log.call_args_list))

    def test_a_frame_that_is_still_moving_is_never_taken_for_the_bottom(self):
        # 畫面還沒穩定 (逾時) 時，兩張看起來差不多的畫面不能當成「到底」，要繼續捲
        sigs = iter(self.frames(50, 50, 50, 50, 50, 50))
        settled = iter([False, False, True, True])
        with mock.patch.object(auto_reply, "_scroll_chat", return_value=True) as scroll,              mock.patch.object(auto_reply, "_wait_until_chat_settled", side_effect=lambda h: next(settled)),              mock.patch.object(auto_reply, "_chat_signature", side_effect=lambda h: next(sigs)):
            auto_reply._scroll_chat_to_bottom(HWND, False)
        self.assertEqual(scroll.call_count, 3)                                           # 第三輪起畫面穩定且沒動才停

    def test_scroll_chat_refuses_when_another_window_covers_the_list(self):
        view = mock.Mock()
        view.Exists.return_value = True
        view.BoundingRectangle = mock.Mock(left=0, right=100, top=0, bottom=100)
        win = mock.Mock()
        win.Exists.return_value = True
        win.ListControl.return_value = view
        fake_auto = mock.Mock(ControlFromHandle=mock.Mock(return_value=win))
        with mock.patch.dict(sys.modules, {"uiautomation": fake_auto}), \
             mock.patch.object(auto_reply, "_root_window_at", return_value=999), \
             mock.patch.object(auto_reply.pyautogui, "moveTo") as move, \
             mock.patch.object(auto_reply.pyautogui, "scroll") as wheel:
            self.assertFalse(auto_reply._scroll_chat(HWND, -5))
        move.assert_not_called()
        wheel.assert_not_called()
        with mock.patch.dict(sys.modules, {"uiautomation": fake_auto}), \
             mock.patch.object(auto_reply, "_root_window_at", return_value=HWND), \
             mock.patch.object(auto_reply.pyautogui, "moveTo") as move, \
             mock.patch.object(auto_reply.pyautogui, "scroll") as wheel:
            self.assertTrue(auto_reply._scroll_chat(HWND, -5))
        move.assert_called_once_with(50, 50)
        wheel.assert_called_once_with(-600)                                              # 一格 = 120


class TestPeekMeasuresBeforeActivating(unittest.TestCase):
    def run_peek(self, viewing):
        order = []
        mock.patch.object(auto_reply.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)
        gui = mock.patch.object(auto_reply, "win32gui").start()
        gui.IsIconic.return_value = False
        gui.GetForegroundWindow.return_value = HWND
        mock.patch.object(auto_reply, "find_target_chat_window", return_value=[(HWND, "小明", "c", True)]).start()
        mock.patch.object(auto_reply, "_user_is_viewing", side_effect=lambda h: order.append("measure") or viewing).start()
        mock.patch.object(auto_reply, "activate_window", side_effect=lambda h: order.append("activate") or True).start()
        mock.patch.object(auto_reply, "_scroll_chat_to_bottom", side_effect=lambda h, v: order.append(("scroll", v))).start()
        mock.patch.object(auto_reply, "is_last_message_from_self_in_chat", return_value=True).start()
        auto_reply._peek_incoming_text("小明", {})
        return order

    def test_viewing_is_measured_before_the_window_is_activated(self):
        self.assertEqual(self.run_peek(False), ["measure", "activate", ("scroll", False)])

    def test_a_viewed_window_is_passed_through_as_viewed(self):
        self.assertEqual(self.run_peek(True), ["measure", "activate", ("scroll", True)])


if __name__ == "__main__":
    unittest.main()
