import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_reply

CHAT = 41


class FakeInput:
    """假的 UIA 輸入框：記錄寫入與清空，按 Enter 時 (若 sends=True) 當作 LINE 收下並清空"""

    def __init__(self, value="", sends=True, mangles=False):
        self.value = value
        self.sends = sends
        self.mangles = mangles
        self.sent = []
        self.writes = []

    def pattern(self):
        outer = self

        class Pattern:
            @property
            def Value(self):
                return outer.value

            def SetValue(self, text):
                outer.writes.append(text)
                outer.value = (text + "亂碼") if (outer.mangles and text) else text
        return Pattern()

    def press(self, key):
        if key == "enter" and self.sends and self.value:
            self.sent.append(self.value)
            self.value = ""


class TestSendTextViaInput(unittest.TestCase):
    def run_send(self, box, text="謝謝", front=True, found=True, focus_lost_after=None):
        edit = mock.Mock()
        edit.GetValuePattern.return_value = box.pattern()
        fg_calls = {"n": 0}

        def foreground():
            fg_calls["n"] += 1
            if focus_lost_after is not None and fg_calls["n"] > focus_lost_after:
                return CHAT + 1
            return CHAT if front else CHAT + 1

        with mock.patch.object(auto_reply, "_find_message_input", return_value=edit if found else None), \
             mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", side_effect=foreground), \
             mock.patch.object(auto_reply.pyautogui, "press", side_effect=box.press) as press, \
             mock.patch.object(auto_reply.time, "sleep"), mock.patch.object(auto_reply, "log_v") as log:
            clock = iter(x * 0.5 for x in range(1000))
            with mock.patch.object(auto_reply.time, "time", side_effect=lambda: next(clock)):
                return auto_reply.send_text_via_input(CHAT, text), press, log

    def test_writes_reads_back_presses_enter_and_waits_for_the_input_to_clear(self):
        box = FakeInput()
        ok, press, _ = self.run_send(box)
        self.assertTrue(ok)
        self.assertEqual(box.sent, ["謝謝"])
        self.assertEqual(box.writes, ["謝謝"])
        press.assert_called_once_with("enter")

    def test_an_unsent_draft_is_never_overwritten_or_sent(self):
        box = FakeInput(value="我先打到一半的訊息")
        ok, press, log = self.run_send(box)
        self.assertFalse(ok)
        self.assertEqual(box.writes, [])                                                       # 一個字都不寫
        self.assertEqual(box.value, "我先打到一半的訊息")                                       # 草稿原封不動
        press.assert_not_called()
        self.assertIn("草稿", log.call_args.args[0])

    def test_whitespace_only_input_counts_as_empty(self):
        box = FakeInput(value="  ")
        self.assertTrue(self.run_send(box)[0])

    def test_a_mismatch_on_read_back_is_cleared_and_not_sent(self):
        box = FakeInput(mangles=True)
        ok, press, _ = self.run_send(box)
        self.assertFalse(ok)
        press.assert_not_called()
        self.assertEqual(box.value, "")                                                        # 自己寫的字清掉，不留殘留
        self.assertEqual(box.sent, [])

    def test_if_enter_does_not_clear_the_input_the_text_is_removed_and_failure_reported(self):
        box = FakeInput(sends=False)
        ok, press, _ = self.run_send(box)
        self.assertFalse(ok)
        press.assert_called_once_with("enter")
        self.assertEqual(box.value, "")                                                        # 沒送出去的字不留在輸入框

    def test_never_writes_or_sends_when_the_window_is_not_in_front(self):
        box = FakeInput()
        ok, press, _ = self.run_send(box, front=False)
        self.assertFalse(ok)
        self.assertEqual(box.writes, [])
        press.assert_not_called()

    def test_focus_lost_between_writing_and_enter_removes_the_text_and_does_not_press_enter(self):
        box = FakeInput()
        ok, press, _ = self.run_send(box, focus_lost_after=1)
        self.assertFalse(ok)
        press.assert_not_called()
        self.assertEqual(box.value, "")

    def test_missing_input_and_blank_text_do_nothing(self):
        box = FakeInput()
        self.assertFalse(self.run_send(box, found=False)[0])
        self.assertFalse(self.run_send(box, text="   ")[0])
        self.assertFalse(self.run_send(box, text="")[0])
        self.assertEqual(box.writes, [])

    def test_an_exception_cleans_up_and_reports_failure(self):
        edit = mock.Mock()
        pattern = mock.Mock()
        pattern.Value = ""
        pattern.SetValue.side_effect = [RuntimeError("uia gone"), None]
        edit.GetValuePattern.return_value = pattern
        with mock.patch.object(auto_reply, "_find_message_input", return_value=edit), \
             mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=CHAT), \
             mock.patch.object(auto_reply.pyautogui, "press") as press, mock.patch.object(auto_reply, "log_v"):
            self.assertFalse(auto_reply.send_text_via_input(CHAT, "謝謝"))
        press.assert_not_called()


class TestFindMessageInput(unittest.TestCase):
    def test_uses_the_chat_windows_own_auto_suggest_text_area(self):
        edit = mock.Mock()
        edit.Exists.return_value = True
        window = mock.Mock()
        window.EditControl.return_value = edit
        module = types.SimpleNamespace(ControlFromHandle=mock.Mock(return_value=window))
        with mock.patch.dict(sys.modules, {"uiautomation": module}):
            self.assertIs(auto_reply._find_message_input(CHAT), edit)
        module.ControlFromHandle.assert_called_once_with(CHAT)
        window.EditControl.assert_called_once_with(ClassName="AutoSuggestTextArea")
        edit.Exists.return_value = False
        with mock.patch.dict(sys.modules, {"uiautomation": module}):
            self.assertIsNone(auto_reply._find_message_input(CHAT))


if __name__ == "__main__":
    unittest.main()
