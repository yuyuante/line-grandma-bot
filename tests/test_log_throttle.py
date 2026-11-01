import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_reply

MSG = "[安全隔離] 滑動視窗內偵測到 2 個不同之未知快取頻道，存在多頻道衝突風險，拒絕自動綁定！"


class TestIsolationLogThrottle(unittest.TestCase):
    def setUp(self):
        auto_reply._isolation_last.clear()
        self.addCleanup(auto_reply._isolation_last.clear)

    def test_first_message_prints_and_repeats_inside_the_interval_are_dropped(self):
        self.assertEqual(auto_reply.throttle_isolation(MSG, now=100.0), MSG)
        for t in (100.2, 101.0, 130.0, 159.9):
            self.assertIsNone(auto_reply.throttle_isolation(MSG, now=t))

    def test_prints_again_after_the_interval_and_reports_how_many_were_skipped(self):
        auto_reply.throttle_isolation(MSG, now=100.0)
        for t in (101.0, 102.0, 103.0):
            auto_reply.throttle_isolation(MSG, now=t)
        again = auto_reply.throttle_isolation(MSG, now=100.0 + auto_reply.ISOLATION_LOG_INTERVAL)
        self.assertEqual(again, MSG + " (其間略過 3 次相同訊息)")
        self.assertIsNone(auto_reply.throttle_isolation(MSG, now=161.0))              # 計時重新開始，計數歸零
        self.assertEqual(auto_reply.throttle_isolation(MSG, now=300.0), MSG + " (其間略過 1 次相同訊息)")

    def test_numbers_do_not_make_a_message_look_new(self):
        a = "[安全隔離] 目標好友「Notify Bot」異動狀態已逾時過期 (102.8s > 15.0s)，拒絕自動綁定！"
        b = "[安全隔離] 目標好友「Notify Bot」異動狀態已逾時過期 (103.8s > 15.0s)，拒絕自動綁定！"
        self.assertEqual(auto_reply.throttle_isolation(a, now=0.0), a)
        self.assertIsNone(auto_reply.throttle_isolation(b, now=1.0))

    def test_different_messages_are_throttled_independently(self):
        other = "[安全隔離] 桌面上偵測到非監控對象之聊天視窗 (「理財幼幼班_Z」)，存在歸屬混淆風險，拒絕自動綁定！"
        self.assertEqual(auto_reply.throttle_isolation(MSG, now=0.0), MSG)
        self.assertEqual(auto_reply.throttle_isolation(other, now=0.5), other)
        self.assertIsNone(auto_reply.throttle_isolation(MSG, now=1.0))
        self.assertIsNone(auto_reply.throttle_isolation(other, now=1.0))

    def test_other_messages_are_never_throttled(self):
        for _ in range(3):
            self.assertEqual(auto_reply.throttle_isolation("[送達確認] 成功送出", now=0.0), "[送達確認] 成功送出")
            self.assertEqual(auto_reply.throttle_isolation("\n⚠️ [警告] 某事", now=0.0), "\n⚠️ [警告] 某事")

    def test_log_v_prints_and_notifies_only_once_per_interval(self):
        with mock.patch("builtins.print") as printed, mock.patch.object(auto_reply.notifier, "on_log") as notified:
            for _ in range(5):
                auto_reply.log_v(MSG)
        self.assertEqual(printed.call_count, 1)
        self.assertEqual(notified.call_count, 1)                                        # 推播到 Notify Bot 的也不會洗版


if __name__ == "__main__":
    unittest.main()
