import json
import logging
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import notifier


class TestNotifier(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self.temp_dir.name, "notify_state.json")
        self.env_path = os.path.join(self.temp_dir.name, "notify.env")
        with open(self.env_path, "w", encoding="utf-8") as f:
            f.write("# comment\nLINE_TOKEN=tok123\nLINE_USER_ID=Uabc\n")
        self.env_patch = mock.patch.dict(os.environ, {"LINE_NOTIFY_ENV": self.env_path})
        self.env_patch.start()
        notifier._throttle.clear()
        notifier._alert_times.clear()
        notifier._warned.clear()
        notifier._creds = None
        notifier._creds_checked = False
        notifier._synced = False
        notifier.configure(True, 150, self.state_path)
        self.calls = []
        self.consumption = {"totalUsage": 0}
        self.push_error = None

        def fake_call(creds, method, path, body=None):
            self.calls.append((method, path, body, creds))
            if self.push_error is not None and method == "POST":
                raise self.push_error
            if path == "/message/quota/consumption":
                return self.consumption
            return {}

        self.call_patch = mock.patch.object(notifier, "_call", side_effect=fake_call)
        self.call_patch.start()
        self.print_patch = mock.patch("builtins.print")
        self.printed = self.print_patch.start()

    def tearDown(self):
        notifier.flush(3)
        self.print_patch.stop()
        self.call_patch.stop()
        self.env_patch.stop()
        notifier.configure(False)
        self.temp_dir.cleanup()

    def pushes(self):
        return [c for c in self.calls if c[0] == "POST"]

    def state(self):
        with open(self.state_path, encoding="utf-8") as f:
            return json.load(f)

    def test_disabled_does_nothing(self):
        notifier.configure(False)
        self.assertFalse(notifier.notify("hi"))
        notifier.on_log("[警告] x")
        notifier.flush(1)
        self.assertEqual(self.calls, [])

    def test_push_sends_text_with_timestamp_to_user(self):
        self.assertTrue(notifier.notify("🟢 已啟動"))
        self.assertTrue(notifier.flush(3))
        (method, path, body, creds), = self.pushes()
        self.assertEqual((method, path, creds["token"]), ("POST", "/message/push", "tok123"))
        self.assertEqual(body["to"], "Uabc")
        text = body["messages"][0]["text"]
        self.assertRegex(text, r"^\[\d\d:\d\d:\d\d\] 🟢 已啟動$")
        self.assertEqual(self.state()["count"], 1)

    def test_long_text_is_truncated(self):
        notifier.notify("字" * 2000)
        notifier.flush(3)
        text = self.pushes()[0][2]["messages"][0]["text"]
        self.assertEqual(len(text), notifier.MAX_TEXT)
        self.assertTrue(text.endswith("…"))

    def test_throttle_by_key(self):
        self.assertTrue(notifier.notify("a", key="k", throttle=1800))
        self.assertFalse(notifier.notify("b", key="k", throttle=1800))     # 30 分鐘內同類略過
        self.assertTrue(notifier.notify("c", key="other", throttle=1800))   # 別類不受影響
        notifier._throttle["k"] -= 1801                                      # 模擬時間過了 30 分鐘
        self.assertTrue(notifier.notify("d", key="k", throttle=1800))
        notifier.flush(3)
        self.assertEqual(len(self.pushes()), 3)

    def test_alerts_have_hourly_cap(self):
        results = [notifier.notify(f"e{i}", key=f"k{i}", throttle=1800, alert=True) for i in range(notifier.ALERT_HOURLY_MAX + 3)]
        self.assertEqual(results.count(True), notifier.ALERT_HOURLY_MAX)
        self.assertTrue(notifier.notify("normal event"))                      # 一般事件不受警告上限影響
        notifier._alert_times[:] = [t - 3601 for t in notifier._alert_times]  # 一小時後恢復
        self.assertTrue(notifier.notify("e again", key="fresh", throttle=1800, alert=True))

    def test_on_log_only_pushes_alert_tags_or_severe(self):
        notifier.on_log("[送達確認] 已送出")
        notifier.on_log("普通訊息")
        notifier.on_log("")
        notifier.flush(1)
        self.assertEqual(self.pushes(), [])
        notifier.on_log("[安全隔離] 拒絕自動綁定")
        notifier.on_log("\n[錯誤] 無法取得視窗")
        notifier.on_log("沒有標籤但嚴重", severe=True)
        notifier.flush(3)
        texts = [c[2]["messages"][0]["text"] for c in self.pushes()]
        self.assertEqual(len(texts), 3)
        self.assertTrue(all("⚠️" in t for t in texts))

    def test_on_log_groups_same_kind_ignoring_numbers_and_hashes(self):
        notifier.on_log("[安全隔離] 快取頻道「817c70f3...」逾時 (15.4s > 15.0s)，拒絕自動綁定！")
        notifier.on_log("[安全隔離] 快取頻道「2baee100...」逾時 (16.1s > 15.0s)，拒絕自動綁定！")
        notifier.flush(3)
        self.assertEqual(len(self.pushes()), 1)                               # 只差在數字與頻道代碼：同一類

    def test_monthly_cap_stops_pushing(self):
        notifier.configure(True, 2, self.state_path)
        for i in range(4):
            notifier.notify(f"m{i}")
        notifier.flush(3)
        self.assertEqual(len(self.pushes()), 2)
        self.assertEqual(self.state()["count"], 2)

    def test_first_push_syncs_usage_from_line(self):
        self.consumption = {"totalUsage": 148}                                # 手動測試也用掉額度
        notifier.configure(True, 150, self.state_path)
        for i in range(4):
            notifier.notify(f"s{i}")
        notifier.flush(3)
        self.assertEqual(len(self.pushes()), 2)                               # 148 + 2 = 150，之後不再推
        self.assertEqual([c[1] for c in self.calls if c[0] == "GET"], ["/message/quota/consumption"])   # 每次執行只查一次

    def test_new_month_resets_local_count(self):
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump({"month": "1999-01", "count": 999}, f)
        notifier.notify("x")
        notifier.flush(3)
        self.assertEqual(len(self.pushes()), 1)
        self.assertEqual(self.state()["count"], 1)

    def test_http_429_marks_quota_exhausted(self):
        self.push_error = urllib.error.HTTPError("u", 429, "Too Many", {}, None)
        notifier.notify("a")
        notifier.flush(3)
        self.assertEqual(self.state()["count"], 150)                          # 本月不再嘗試
        self.push_error = None
        notifier.notify("b")
        notifier.flush(3)
        self.assertEqual(len(self.pushes()), 1)

    def test_other_http_error_is_swallowed_and_not_counted(self):
        self.push_error = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
        notifier.notify("a")
        self.assertTrue(notifier.flush(3))
        self.assertFalse(os.path.exists(self.state_path))
        self.assertTrue(any("401" in str(c.args[0]) for c in self.printed.call_args_list))

    def test_network_error_does_not_break_worker(self):
        self.push_error = OSError("offline")
        notifier.notify("a")
        notifier.flush(3)
        self.push_error = None
        notifier.notify("b")
        notifier.flush(3)
        self.assertEqual(len(self.pushes()), 2)                               # 第一則失敗後 worker 還活著，第二則照送
        self.assertEqual(self.state()["count"], 1)

    def test_missing_credentials_warns_once_and_skips(self):
        with mock.patch.dict(os.environ, {"LINE_NOTIFY_ENV": os.path.join(self.temp_dir.name, "missing.env")}):
            notifier._creds_checked = False
            notifier._creds = None
            notifier.notify("a")
            notifier.notify("b")
            notifier.flush(3)
        self.assertEqual(self.calls, [])
        self.assertEqual(sum("LINE_TOKEN" in str(c.args[0]) for c in self.printed.call_args_list), 1)

    def test_logging_handler_forwards_warning_and_above_only(self):
        handler = notifier.LogHandler(level=logging.WARNING)
        log = logging.getLogger("notifier_test")
        log.propagate = False
        log.addHandler(handler)
        try:
            log.warning("[警告] 監聽迴圈發生短暫異常: boom")
            log.info("只是資訊")
            notifier.flush(3)
        finally:
            log.removeHandler(handler)
        texts = [c[2]["messages"][0]["text"] for c in self.pushes()]
        self.assertEqual(len(texts), 1)
        self.assertIn("監聽迴圈", texts[0])

    def test_install_logging_handler_is_idempotent(self):
        root = logging.getLogger()
        before = [h for h in root.handlers if isinstance(h, notifier.LogHandler)]
        notifier.install_logging_handler()
        notifier.install_logging_handler()
        after = [h for h in root.handlers if isinstance(h, notifier.LogHandler)]
        try:
            self.assertEqual(len(after), max(1, len(before)))
        finally:
            for h in after:
                if h not in before:
                    root.removeHandler(h)


if __name__ == "__main__":
    unittest.main()
