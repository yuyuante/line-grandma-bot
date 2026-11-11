import os
import sys
import unittest
import pathlib
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_reply


class TestKeepAwake(unittest.TestCase):
    def test_releases_request_even_when_bot_exits(self):
        with mock.patch.object(auto_reply.ctypes.windll.kernel32, "SetThreadExecutionState", return_value=0x80000000) as state:
            with self.assertRaises(SystemExit):
                with auto_reply.keep_system_awake():
                    raise SystemExit(3)
        self.assertEqual(state.call_args_list, [mock.call(0x80000001), mock.call(0x80000000)])

    def test_failed_request_prevents_unprotected_start(self):
        with tempfile.TemporaryDirectory() as log_dir, mock.patch.object(auto_reply, "LOG_DIR", log_dir), mock.patch.object(auto_reply.ctypes.windll.kernel32, "SetThreadExecutionState", return_value=0) as state:
            started = False
            with self.assertRaises(OSError):
                with auto_reply.keep_system_awake():
                    started = True
            self.assertIn("機器人未啟動", pathlib.Path(log_dir, "startup_errors.log").read_text(encoding="utf-8"))
        self.assertFalse(started)
        state.assert_called_once_with(0x80000001)

    def test_keyboard_interrupt_releases_request(self):
        with mock.patch.object(auto_reply.ctypes.windll.kernel32, "SetThreadExecutionState", return_value=0x80000000) as state:
            with self.assertRaises(KeyboardInterrupt):
                with auto_reply.keep_system_awake():
                    raise KeyboardInterrupt()
        self.assertEqual(state.call_args_list, [mock.call(0x80000001), mock.call(0x80000000)])

    def test_release_failure_does_not_hide_bot_exception(self):
        with mock.patch.object(auto_reply.ctypes.windll.kernel32, "SetThreadExecutionState", side_effect=[0x80000000, 0]), mock.patch.object(auto_reply.logging, "warning") as warning:
            with self.assertRaisesRegex(RuntimeError, "bot failure"):
                with auto_reply.keep_system_awake():
                    raise RuntimeError("bot failure")
        warning.assert_called_once()
