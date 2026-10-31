import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import reply_budget as rb


def at(y, m, d, h=12, minute=0):
    return time.mktime((y, m, d, h, minute, 0, 0, 0, -1))


class TestReplyBudget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "reply_counts.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_three_replies_per_day_then_blocked(self):
        now = at(2026, 10, 31)
        for expected in (1, 2, 3):
            self.assertTrue(rb.can_reply(self.path, "媽媽", 3, now))
            self.assertEqual(rb.record_reply(self.path, "媽媽", now), expected)
        self.assertFalse(rb.can_reply(self.path, "媽媽", 3, now))                      # 第四次不行
        self.assertEqual(rb.count_today(self.path, "媽媽", now), 3)

    def test_each_chat_has_its_own_count(self):
        now = at(2026, 10, 31)
        for _ in range(3):
            rb.record_reply(self.path, "媽媽", now)
        self.assertTrue(rb.can_reply(self.path, "爸爸", 3, now))
        self.assertEqual(rb.count_today(self.path, "爸爸", now), 0)

    def test_resets_at_local_midnight(self):
        for _ in range(3):
            rb.record_reply(self.path, "媽媽", at(2026, 10, 31, 23, 59))
        self.assertFalse(rb.can_reply(self.path, "媽媽", 3, at(2026, 10, 31, 23, 59)))
        self.assertTrue(rb.can_reply(self.path, "媽媽", 3, at(2026, 11, 1, 0, 0)))      # 隔天零點重置
        self.assertEqual(rb.record_reply(self.path, "媽媽", at(2026, 11, 1, 0, 1)), 1)

    def test_survives_restart(self):
        now = at(2026, 10, 31)
        rb.record_reply(self.path, "媽媽", now)
        rb.record_reply(self.path, "媽媽", now)
        self.assertEqual(rb.count_today(self.path, "媽媽", now), 2)                     # 重新讀檔，不歸零
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"媽媽": {"date": "2026-10-31", "count": 2}})

    def test_old_days_are_dropped_when_recording(self):
        rb.record_reply(self.path, "爸爸", at(2026, 10, 30))
        rb.record_reply(self.path, "媽媽", at(2026, 10, 31))
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(list(json.load(f)), ["媽媽"])                              # 昨天的紀錄清掉，檔案不會一直長大

    def test_zero_or_negative_limit_means_unlimited(self):
        now = at(2026, 10, 31)
        for _ in range(10):
            rb.record_reply(self.path, "媽媽", now)
        self.assertTrue(rb.can_reply(self.path, "媽媽", 0, now))
        self.assertTrue(rb.can_reply(self.path, "媽媽", -1, now))

    def test_corrupt_or_odd_file_counts_as_zero(self):
        for content in ("{bad json", "[]", '{"媽媽": "three"}', '{"媽媽": {"date": "2026-10-31", "count": "x"}}'):
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(content)
            self.assertEqual(rb.count_today(self.path, "媽媽", at(2026, 10, 31)), 0, content)
            self.assertTrue(rb.can_reply(self.path, "媽媽", 3, at(2026, 10, 31)))
        self.assertEqual(rb.record_reply(self.path, "媽媽", at(2026, 10, 31)), 1)       # 壞檔可以被正常覆寫

    def test_missing_file(self):
        self.assertEqual(rb.count_today(os.path.join(self.tmp.name, "none.json"), "媽媽"), 0)


if __name__ == "__main__":
    unittest.main()
