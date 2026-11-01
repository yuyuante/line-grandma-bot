import datetime
import json
import logging
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_reply


class FileLogCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "logs")
        self.day = ["2026-11-01"]
        real_strftime = auto_reply.time.strftime

        def fake_strftime(fmt, *args):
            return self.day[0] if fmt == "%Y-%m-%d" else real_strftime(fmt, *args)

        self.stack = [
            mock.patch.object(auto_reply, "LOG_DIR", self.dir),
            mock.patch.object(auto_reply.time, "strftime", side_effect=fake_strftime),
            mock.patch("builtins.print"),
            mock.patch.object(auto_reply.notifier, "on_log"),
        ]
        self.mocks = [p.start() for p in self.stack]
        self.printed, self.on_log = self.mocks[2], self.mocks[3]
        self.saved = (dict(auto_reply._file_log_settings), dict(auto_reply._file_log_state))
        auto_reply._file_log_settings.update(enabled=True, keep_days=7)
        auto_reply._file_log_state.update(pruned_day=None)
        auto_reply._isolation_last.clear()

    def tearDown(self):
        for p in reversed(self.stack):
            p.stop()
        auto_reply._file_log_settings.clear()
        auto_reply._file_log_settings.update(self.saved[0])
        auto_reply._file_log_state.clear()
        auto_reply._file_log_state.update(self.saved[1])
        auto_reply._isolation_last.clear()
        self.tmp.cleanup()

    def path(self, day=None):
        return os.path.join(self.dir, f"auto_reply_{day or self.day[0]}.log")

    def lines(self, day=None):
        with open(self.path(day), encoding="utf-8") as f:
            return f.read().splitlines()

    def touch(self, name, folder=False):
        os.makedirs(self.dir, exist_ok=True)
        p = os.path.join(self.dir, name)
        if folder:
            os.makedirs(p, exist_ok=True)
        else:
            with open(p, "w", encoding="utf-8") as f:
                f.write("x")
        return p


class TestWritingTheFile(FileLogCase):
    def test_a_log_line_goes_to_the_screen_and_to_todays_file_with_the_same_text(self):
        auto_reply.log_v("機器人啟動了")
        shown = self.printed.call_args.args[0]
        self.assertEqual(self.lines(), [shown])
        self.assertRegex(shown, rf"^\[\d\d:\d\d:\d\d\]\[v{auto_reply.VERSION}\] 機器人啟動了$")

    def test_lines_are_appended_in_order_and_survive_a_second_run(self):
        auto_reply.log_v("第一行")
        auto_reply.log_v("第二行")
        self.assertEqual([l.split("] ", 1)[1] for l in self.lines()], ["第一行", "第二行"])
        auto_reply._file_log_state["pruned_day"] = None                                      # 模擬程式重新啟動
        auto_reply.log_v("重啟後")
        self.assertEqual(len(self.lines()), 3)                                               # 附加，不覆蓋

    def test_a_new_day_starts_a_new_file(self):
        auto_reply.log_v("今天")
        self.day[0] = "2026-11-02"
        auto_reply.log_v("明天")
        self.assertEqual(len(self.lines("2026-11-01")), 1)
        self.assertEqual(len(self.lines("2026-11-02")), 1)

    def test_a_leading_blank_line_in_the_message_is_kept_like_on_screen(self):
        auto_reply.log_v("\n偵測到新訊息")
        with open(self.path(), encoding="utf-8") as f:
            self.assertTrue(f.read().startswith("\n["))

    def test_switched_off_writes_nothing_and_creates_no_folder(self):
        auto_reply.configure_file_log({"log_to_file": False})
        auto_reply.log_v("不該寫檔")
        self.assertFalse(os.path.exists(self.dir))
        self.printed.assert_called()                                                         # 畫面照印

    def test_importing_the_module_never_writes_a_log_file(self):
        # 匯入模組與跑單元測試時不能碰到真的 logs/：預設關閉，main() 啟動時才由 configure_file_log 打開。
        # 真的 logs/ 裡可能本來就有機器人自己寫的檔，所以比較「匯入並呼叫 log_v 前後」的檔案大小，不看有沒有檔案
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = "; ".join([
            f"import sys; sys.path.insert(0, {root!r})",
            "import auto_reply, os",
            "snap = lambda: {n: os.path.getsize(os.path.join(auto_reply.LOG_DIR, n)) for n in (os.listdir(auto_reply.LOG_DIR) if os.path.isdir(auto_reply.LOG_DIR) else [])}",
            "before = snap()",
            "auto_reply.log_v('匯入後的第一行')",
            "after = snap()",
            "print(auto_reply._file_log_settings['enabled'], before == after)",
        ])
        out = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", code],
                             capture_output=True, text=True, encoding="utf-8", cwd=root).stdout.strip().splitlines()[-1]
        self.assertEqual(out, "False True")

    def test_throttled_isolation_messages_are_written_once(self):
        for _ in range(5):
            auto_reply.log_v("[安全隔離] 滑動視窗內偵測到 2 個不同之未知快取頻道，拒絕自動綁定！")
        self.assertEqual(len(self.lines()), 1)                                               # 不會被每秒數次的重複洗版

    def test_the_startup_banner_helper_prints_and_writes(self):
        auto_reply.banner_print("=" * 5)
        self.printed.assert_called_with("=====")
        self.assertEqual(self.lines(), ["====="])

    def test_the_notifiers_own_hints_are_written_with_a_timestamp(self):
        auto_reply._notifier_log_line("[通知] LINE 拒絕了推播 (HTTP 403)")
        self.assertRegex(self.lines()[0], rf"^\[\d\d:\d\d:\d\d\]\[v{auto_reply.VERSION}\] \[通知\] LINE 拒絕了推播")

    def test_logging_warnings_also_reach_the_file_but_not_info(self):
        handler = auto_reply._file_handler
        self.assertEqual(handler.level, logging.WARNING)
        record = logging.LogRecord("x", logging.WARNING, __file__, 1, "config.json 讀取失敗", None, None)
        handler.emit(record)
        self.assertIn("config.json 讀取失敗", self.lines()[0])
        self.assertIn(f"[v{auto_reply.VERSION}]", self.lines()[0])


class TestNeverBreakingTheBot(FileLogCase):
    def test_a_write_failure_is_swallowed_and_the_screen_and_push_still_work(self):
        blocker = os.path.join(self.tmp.name, "blocker")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("a file where the log folder should be")
        with mock.patch.object(auto_reply, "LOG_DIR", os.path.join(blocker, "logs")):
            auto_reply.log_v("寫不進去也不能出事")                                           # 不丟例外
        self.printed.assert_called()
        self.on_log.assert_called()

    def test_an_open_error_is_swallowed_too(self):
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            auto_reply._write_log_file("x")

    def test_concurrent_writers_never_interleave_lines(self):
        def work(n):
            for i in range(60):
                auto_reply.log_v(f"執行緒{n}-第{i}行-" + "字" * 50)

        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        lines = self.lines()
        self.assertEqual(len(lines), 8 * 60)
        self.assertEqual(len(set(lines)), 8 * 60)
        for line in lines:
            self.assertRegex(line, r"^\[\d\d:\d\d:\d\d\]\[v[\d.]+\] 執行緒\d-第\d+行-字{50}$")


class TestPruning(FileLogCase):
    def test_only_log_files_older_than_the_keep_period_are_deleted(self):
        for day in ("2026-10-20", "2026-10-25", "2026-10-26", "2026-10-31"):
            self.touch(f"auto_reply_{day}.log")
        auto_reply.log_v("今天")                                                              # 2026-11-01、保留 7 天 = 保留 10/26 之後
        left = sorted(os.listdir(self.dir))
        self.assertEqual(left, ["auto_reply_2026-10-26.log", "auto_reply_2026-10-31.log", "auto_reply_2026-11-01.log"])

    def test_the_boundary_is_exactly_keep_days_files(self):
        auto_reply._file_log_settings["keep_days"] = 3
        for day in ("2026-10-29", "2026-10-30", "2026-10-31"):
            self.touch(f"auto_reply_{day}.log")
        auto_reply.log_v("今天")
        self.assertEqual(sorted(os.listdir(self.dir)),
                         ["auto_reply_2026-10-30.log", "auto_reply_2026-10-31.log", "auto_reply_2026-11-01.log"])   # 共 3 天

    def test_other_files_folders_and_malformed_names_are_never_touched(self):
        keep = ["notes.txt", "auto_reply_2026-10-01.txt", "auto_reply_2026-13-45.log", "auto_reply_.log", "my_auto_reply_2026-10-01.log"]
        for name in keep:
            self.touch(name)
        self.touch("auto_reply_2026-10-01.log", folder=True)                                  # 名字符合但它是資料夾
        auto_reply.log_v("今天")
        for name in keep + ["auto_reply_2026-10-01.log"]:
            self.assertTrue(os.path.exists(os.path.join(self.dir, name)), name)

    def test_pruning_runs_once_per_day(self):
        auto_reply.log_v("今天第一行")
        stale = self.touch("auto_reply_2026-01-01.log")
        auto_reply.log_v("今天第二行")
        self.assertTrue(os.path.exists(stale))                                                # 同一天不重複掃
        self.day[0] = "2026-11-02"
        auto_reply.log_v("隔天")
        self.assertFalse(os.path.exists(stale))

    def test_keep_days_setting_is_read_from_the_config_and_clamped(self):
        auto_reply.configure_file_log({"log_keep_days": 3})
        self.assertEqual(auto_reply._file_log_settings["keep_days"], 3)
        auto_reply.configure_file_log({"log_keep_days": 0})
        self.assertEqual(auto_reply._file_log_settings["keep_days"], 1)
        auto_reply.configure_file_log({"log_keep_days": "many"})
        self.assertEqual(auto_reply._file_log_settings["keep_days"], 7)
        auto_reply.configure_file_log({})
        self.assertEqual((auto_reply._file_log_settings["enabled"], auto_reply._file_log_settings["keep_days"]), (True, 7))

    def test_pruning_a_missing_folder_is_harmless(self):
        auto_reply._prune_old_logs(7, datetime.date(2026, 11, 1))


class TestConfigKeys(unittest.TestCase):
    def load(self, cfg):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False)
            with mock.patch.object(auto_reply, "CONFIG_PATH", path):
                return auto_reply.load_config()

    def test_defaults_and_sanitising(self):
        cfg = self.load({"targets": []})
        self.assertEqual((cfg["log_to_file"], cfg["log_keep_days"]), (True, 7))
        cfg = self.load({"log_to_file": False, "log_keep_days": "3"})
        self.assertEqual((cfg["log_to_file"], cfg["log_keep_days"]), (False, 3))
        cfg = self.load({"log_to_file": "no", "log_keep_days": 0})
        self.assertEqual((cfg["log_to_file"], cfg["log_keep_days"]), (True, 1))              # 不是布林：沿用預設；天數最少 1
        cfg = self.load({"log_keep_days": None})
        self.assertEqual(cfg["log_keep_days"], 7)


if __name__ == "__main__":
    unittest.main()
