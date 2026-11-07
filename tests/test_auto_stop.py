import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_reply


class Base(unittest.TestCase):
    def setUp(self):
        auto_reply._AUTO_STOP.update({"limit": 0, "replies": 0})
        self.addCleanup(auto_reply._AUTO_STOP.update, {"limit": 0, "replies": 0})


class TestResolveStopAfter(unittest.TestCase):
    def ask(self, answers):
        it = iter(answers)
        return lambda prompt: next(it)

    def test_command_line_number_wins_and_never_prompts(self):
        boom = mock.Mock(side_effect=AssertionError("不該詢問"))
        self.assertEqual(auto_reply.resolve_stop_after(3, True, boom), 3)
        self.assertEqual(auto_reply.resolve_stop_after(0, True, boom), 0)

    def test_negative_number_means_off(self):
        self.assertEqual(auto_reply.resolve_stop_after(-2, False), 0)

    def test_enter_alone_defaults_to_one(self):
        self.assertEqual(auto_reply.resolve_stop_after(None, True, self.ask(["  "])), 1)

    def test_typed_number_and_zero(self):
        self.assertEqual(auto_reply.resolve_stop_after(None, True, self.ask(["5"])), 5)
        self.assertEqual(auto_reply.resolve_stop_after(None, True, self.ask(["0"])), 0)

    def test_bad_input_asks_again(self):
        self.assertEqual(auto_reply.resolve_stop_after(None, True, self.ask(["abc", "-1", "2"])), 2)
        self.assertEqual(auto_reply.resolve_stop_after(None, True, self.ask(["²", "３"])), 3)    # 上標 2 重問；全形 3 可用

    def test_no_console_means_off_without_asking(self):
        boom = mock.Mock(side_effect=AssertionError("不該詢問"))
        self.assertEqual(auto_reply.resolve_stop_after(None, False, boom), 0)

    def test_closed_stdin_means_off(self):
        def eof(_):
            raise EOFError
        self.assertEqual(auto_reply.resolve_stop_after(None, True, eof), 0)


class TestCountingAndStopping(Base):
    def test_disabled_never_stops(self):
        for _ in range(5):
            auto_reply.note_reply_for_auto_stop()
        auto_reply.check_auto_stop()

    def test_stops_once_the_limit_is_reached(self):
        auto_reply._AUTO_STOP["limit"] = 2
        auto_reply.note_reply_for_auto_stop()
        auto_reply.check_auto_stop()
        auto_reply.note_reply_for_auto_stop()
        with self.assertRaises(auto_reply.AutoStopReached):
            auto_reply.check_auto_stop()

    def test_it_is_a_stop_request_so_the_loop_does_not_swallow_it(self):
        self.assertTrue(issubclass(auto_reply.AutoStopReached, auto_reply.StopRequested))
        self.assertFalse(issubclass(auto_reply.AutoStopReached, Exception))

    def test_every_recorded_reply_counts(self):
        with mock.patch.object(auto_reply, "record_reply_sent"), mock.patch.object(auto_reply.reply_budget, "record_reply"):
            auto_reply._record_reply("小明")
            auto_reply._record_reply("某群組")
        self.assertEqual(auto_reply._AUTO_STOP["replies"], 2)


class TestSuspend(unittest.TestCase):
    def test_failure_is_logged_not_raised(self):
        with mock.patch("ctypes.windll", create=True) as win, mock.patch.object(auto_reply, "log_v") as log:
            win.powrprof.SetSuspendState.return_value = 0
            auto_reply.suspend_computer()
            win.powrprof.SetSuspendState.side_effect = OSError("x")
            auto_reply.suspend_computer()
        self.assertEqual(log.call_count, 2)

    def test_asks_for_sleep_not_hibernate(self):
        with mock.patch("ctypes.windll", create=True) as win:
            win.powrprof.SetSuspendState.return_value = 1
            auto_reply.suspend_computer()
        win.powrprof.SetSuspendState.assert_called_once_with(False, False, False)


if __name__ == "__main__":
    unittest.main()
