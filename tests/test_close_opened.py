import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_reply

HWND = 501
NAME = "小明"


def register(hwnd=HWND, name=NAME, age=0.0):
    auto_reply._BOT_OPENED_HWNDS[hwnd] = (auto_reply.time.time() - age, name)


class Base(unittest.TestCase):
    def setUp(self):
        auto_reply._BOT_OPENED_HWNDS.clear()
        auto_reply._BACKGROUND_WINDOWS.clear()
        self.addCleanup(auto_reply._BOT_OPENED_HWNDS.clear)
        self.addCleanup(auto_reply._BACKGROUND_WINDOWS.clear)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(auto_reply.time, "sleep").start()
        self.gui = mock.patch.object(auto_reply, "win32gui").start()
        self.posted = False
        self.close_works = True

        def post(hwnd, msg, w, l):
            if msg == auto_reply.win32con.WM_CLOSE:
                self.posted = True
        self.gui.PostMessage.side_effect = post
        # 視窗存在，直到我們送出 WM_CLOSE (close_works 為 False 時送了也不消失，模擬 LINE 沒理)
        self.gui.IsWindow.side_effect = lambda h: not (self.posted and self.close_works)
        self.gui.IsIconic.return_value = False
        self.gui.GetWindowText.return_value = NAME


class TestCloseOnlyWindowsTheBotOpened(Base):
    def test_a_window_the_bot_opened_is_closed_not_minimized(self):
        register()
        auto_reply._finish_with_window(HWND, {}, NAME)
        self.assertTrue(self.posted)
        self.gui.ShowWindow.assert_not_called()
        self.assertNotIn(HWND, auto_reply._BOT_OPENED_HWNDS)          # 關了就不再登記 (hwnd 之後可能被重用)

    def test_a_window_the_user_had_open_is_only_minimized(self):
        auto_reply._finish_with_window(HWND, {}, NAME)
        self.assertFalse(self.posted)
        self.gui.ShowWindow.assert_called_once()

    def test_switch_off_keeps_the_old_behaviour(self):
        register()
        auto_reply._finish_with_window(HWND, {"close_opened_windows": False}, NAME)
        self.assertFalse(self.posted)
        self.gui.ShowWindow.assert_called_once()

    def test_a_window_the_text_watcher_is_monitoring_is_never_closed(self):
        register()
        auto_reply._BACKGROUND_WINDOWS.add(HWND)                      # 關掉它下一則文字就偵測不到
        auto_reply._finish_with_window(HWND, {}, NAME)
        self.assertFalse(self.posted)
        self.gui.SetWindowPos.assert_called()                         # 照舊放到最底層

    def test_a_stale_registration_is_dropped_not_closed(self):
        register(age=auto_reply.BOT_OPENED_MAX_AGE + 1)
        auto_reply._finish_with_window(HWND, {}, NAME)
        self.assertFalse(self.posted)
        self.assertNotIn(HWND, auto_reply._BOT_OPENED_HWNDS)

    def test_a_window_whose_title_is_no_longer_the_registered_target_is_never_closed(self):
        # hwnd 被別的視窗重用、或使用者把它拿去聊別的：標題對不上就不碰 (LINE 主視窗、空標題也一樣)
        for title in ("LINE", "", "  ", "別人"):
            register()
            self.gui.GetWindowText.return_value = title
            self.assertFalse(auto_reply._close_if_bot_opened(HWND, {}, NAME), title)
            self.assertFalse(self.posted)
        self.assertNotIn(HWND, auto_reply._BOT_OPENED_HWNDS)

    def test_a_vanished_window_is_not_messaged(self):
        register()
        self.gui.IsWindow.side_effect = lambda h: False
        self.assertFalse(auto_reply._close_if_bot_opened(HWND, {}, NAME))
        self.gui.PostMessage.assert_not_called()

    def test_minimize_alone_never_closes_so_retry_paths_keep_their_window(self):
        register()
        auto_reply._minimize_if_configured(HWND, {}, NAME)
        self.assertFalse(self.posted)
        self.gui.ShowWindow.assert_called_once()
        self.assertIn(HWND, auto_reply._BOT_OPENED_HWNDS)             # 還登記著：之後真正結束時才關

    def test_a_close_that_did_not_take_effect_is_not_reported_and_falls_back_to_minimize(self):
        register()
        self.close_works = False                                      # LINE 沒理 WM_CLOSE
        auto_reply._finish_with_window(HWND, {}, NAME)
        self.assertTrue(self.posted)
        self.gui.ShowWindow.assert_called_once()                      # 改為最小化
        self.assertIn(HWND, auto_reply._BOT_OPENED_HWNDS)             # 保留登記，下次結束再試


class TestOpenMissingChatWindowRegisters(Base):
    def run_open(self, before, after, opened=True):
        with mock.patch.object(auto_reply, "get_all_line_windows", return_value=before), \
             mock.patch.object(auto_reply, "find_target_chat_window", return_value=after), \
             mock.patch.object(auto_reply.chat_opener, "open_chat_window", return_value=opened):
            return auto_reply.open_missing_chat_window(NAME, {})

    def test_a_window_that_appeared_because_of_the_open_is_registered(self):
        self.assertTrue(self.run_open([(1, "LINE", "c", True)], [(HWND, NAME, "c", True)]))
        self.assertEqual(list(auto_reply._BOT_OPENED_HWNDS), [HWND])
        self.assertEqual(auto_reply._BOT_OPENED_HWNDS[HWND][1], NAME)

    def test_a_window_that_was_already_on_the_desktop_is_never_registered(self):
        # 使用者本來就開著、只是比對沒認出來；opener 雙擊只是把它叫到前景，不能當成我們開的
        self.run_open([(1, "LINE", "c", True), (HWND, NAME, "c", True)], [(HWND, NAME, "c", True)])
        self.assertEqual(auto_reply._BOT_OPENED_HWNDS, {})

    def test_a_failed_open_registers_nothing(self):
        self.assertFalse(self.run_open([], [], opened=False))
        self.assertEqual(auto_reply._BOT_OPENED_HWNDS, {})


class TestSendWrapperClosesOnTerminalOutcomes(Base):
    def run_send(self, result=None, raises=False):
        impl = mock.Mock(side_effect=RuntimeError("boom")) if raises else mock.Mock(return_value=result)
        with mock.patch.object(auto_reply, "_send_grandma_image_impl", impl), \
             mock.patch.object(auto_reply, "find_target_chat_window", return_value=[(HWND, NAME, "c", True)]):
            try:
                return auto_reply.send_grandma_image(NAME, {})
            except RuntimeError:
                return "raised"

    def test_any_terminal_outcome_closes_a_window_the_bot_opened(self):
        for outcome in ("SENT", "SKIPPED_TEXT", "SKIPPED_NO_IMAGE", "SKIPPED_DAILY_LIMIT"):
            auto_reply._BOT_OPENED_HWNDS.clear()
            self.posted = False
            register()
            self.assertEqual(self.run_send(outcome), outcome)
            self.assertTrue(self.posted, outcome)

    def test_a_retry_outcome_keeps_the_window_for_the_retry(self):
        register()
        self.assertEqual(self.run_send("NOT_SENT"), "NOT_SENT")
        self.assertFalse(self.posted)
        self.assertIn(HWND, auto_reply._BOT_OPENED_HWNDS)

    def test_an_exception_leaves_the_window_alone(self):
        register()
        self.assertEqual(self.run_send(raises=True), "raised")
        self.assertFalse(self.posted)

    def test_no_window_lookup_when_the_bot_opened_nothing(self):
        with mock.patch.object(auto_reply, "_send_grandma_image_impl", return_value="SENT"), \
             mock.patch.object(auto_reply, "find_target_chat_window") as find:
            auto_reply.send_grandma_image(NAME, {})
        find.assert_not_called()


if __name__ == "__main__":
    unittest.main()
