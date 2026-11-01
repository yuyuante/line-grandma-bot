import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_reply

GROUP = "東南里李武雄服務處-公用"
MAIN = 1


def window(hwnd, title, cls="Qt663QWindowIcon", visible=True):
    return (hwnd, title, cls, visible)


class GroupTestCase(unittest.TestCase):
    def setUp(self):
        auto_reply.set_group_targets([GROUP])
        self.addCleanup(auto_reply.set_group_targets, [])


class TestTitleMatching(GroupTestCase):
    def test_group_title_may_carry_the_member_count(self):
        for title in (GROUP, f"{GROUP} (477)", f"{GROUP}(477)", f"{GROUP}（477）", f"  {GROUP} (9)  "):
            self.assertTrue(auto_reply.title_matches_target(title, GROUP), title)

    def test_group_title_must_still_be_the_name(self):
        for title in (f"{GROUP} (x)", f"{GROUP} (477) (1)", f"{GROUP}2", f"舊{GROUP}", f"{GROUP} ()", "LINE", ""):
            self.assertFalse(auto_reply.title_matches_target(title, GROUP), title)

    def test_one_to_one_titles_stay_exact_and_never_accept_a_count(self):
        self.assertTrue(auto_reply.title_matches_target("林如婷", "林如婷"))
        self.assertTrue(auto_reply.title_matches_target("林如婷", "林如婷".upper()))
        self.assertFalse(auto_reply.title_matches_target("林如婷 (3)", "林如婷"))      # 1:1 的括號是未讀數，由原本的流程處理


class TestFindGroupWindow(GroupTestCase):
    def find(self, windows):
        with mock.patch.object(auto_reply, "get_all_line_windows", return_value=windows), \
             mock.patch.object(auto_reply, "find_line_main_window", return_value=MAIN), \
             mock.patch.object(auto_reply, "log_v"):
            return auto_reply.find_target_chat_window(GROUP)

    def test_finds_the_window_with_or_without_the_member_count(self):
        self.assertEqual(self.find([window(5, GROUP)]), [window(5, GROUP)])
        self.assertEqual(self.find([window(5, f"{GROUP} (477)")]), [window(5, f"{GROUP} (477)")])

    def test_ignores_main_window_hidden_and_other_chats(self):
        windows = [window(MAIN, GROUP), window(6, GROUP, visible=False), window(7, "別的聊天室"), window(8, "LINE"),
                   window(9, GROUP, cls="Qt663QWindowToolTipSaveBits")]
        self.assertEqual(self.find(windows), [])

    def test_two_windows_with_the_same_group_name_are_refused(self):
        self.assertEqual(self.find([window(5, GROUP), window(6, f"{GROUP} (477)")]), [])

    def test_a_duplicate_group_window_is_remembered_so_the_main_window_is_not_hijacked(self):
        auto_reply._DUPLICATE_WINDOW_SEEN.clear()
        self.assertEqual(self.find([window(5, GROUP), window(6, f"{GROUP} (477)")]), [])
        with mock.patch.object(auto_reply, "log_v"), mock.patch.object(auto_reply.chat_opener, "open_chat_window") as opener:
            self.assertFalse(auto_reply.open_missing_chat_window(GROUP, {}))
            opener.assert_not_called()                                                  # 不是沒有視窗，是有兩個：開窗也不會成功，不去搶主視窗
            auto_reply._DUPLICATE_WINDOW_SEEN[GROUP.lower()] -= auto_reply.DUPLICATE_WINDOW_SKIP_SECONDS + 1
            auto_reply.open_missing_chat_window(GROUP, {})
            opener.assert_called_once()                                                 # 過了幾秒 (使用者可能已關掉多餘的) 恢復正常

    def test_does_not_need_the_one_to_one_uia_check(self):
        with mock.patch.object(auto_reply, "verify_is_not_group_window_uia", return_value=False) as uia:
            self.assertEqual(len(self.find([window(5, f"{GROUP} (477)")])), 1)
        uia.assert_not_called()                                                           # 群組一定過不了 1:1 核驗，不能套用

    def test_names_not_in_group_targets_use_the_strict_one_to_one_path(self):
        auto_reply.set_group_targets([])
        with mock.patch.object(auto_reply, "get_all_line_windows", return_value=[window(5, f"{GROUP} (477)")]), \
             mock.patch.object(auto_reply, "find_line_main_window", return_value=MAIN), \
             mock.patch.object(auto_reply, "verify_is_not_group_window_uia", return_value=False), \
             mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=0), \
             mock.patch.object(auto_reply, "log_v"):
            self.assertEqual(auto_reply.find_target_chat_window(GROUP), [])


class TestConfig(unittest.TestCase):
    def load(self, cfg):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False)
            with mock.patch.object(auto_reply, "CONFIG_PATH", path):
                return auto_reply.load_config()

    def tearDown(self):
        auto_reply.set_group_targets([])

    def test_groups_are_merged_into_targets_and_remembered(self):
        cfg = self.load({"targets": ["Notify Bot", "林如婷"], "group_targets": [GROUP, " 理財幼幼班_Z ", "", "林如婷"]})
        self.assertEqual(cfg["targets"], ["Notify Bot", "林如婷", GROUP, "理財幼幼班_Z"])      # 重複的不加第二次
        self.assertEqual(cfg["group_targets"], [GROUP, "理財幼幼班_Z", "林如婷"])
        self.assertTrue(auto_reply.is_group_target("理財幼幼班_z"))
        self.assertFalse(auto_reply.is_group_target("Notify Bot"))

    def test_group_settings_have_conservative_defaults_and_are_sanitised(self):
        cfg = self.load({"targets": []})
        self.assertEqual((cfg["group_targets"], cfg["group_max_replies_per_day"], cfg["group_cooldown_seconds"]), ([], 1, 600))
        cfg = self.load({"group_targets": ["g"], "group_max_replies_per_day": -3, "group_cooldown_seconds": 1})
        self.assertEqual((cfg["group_max_replies_per_day"], cfg["group_cooldown_seconds"]), (0, 5))
        cfg = self.load({"group_targets": "not a list", "group_max_replies_per_day": "x"})
        self.assertEqual((cfg["group_targets"], cfg["group_max_replies_per_day"]), ([], 1))
        self.assertFalse(auto_reply.GROUP_TARGETS)

    def test_removing_a_group_from_the_config_takes_effect_on_reload(self):
        self.load({"group_targets": [GROUP]})
        self.assertTrue(auto_reply.is_group_target(GROUP))
        self.load({"group_targets": []})
        self.assertFalse(auto_reply.is_group_target(GROUP))


class TestUnreadBaselines(GroupTestCase):
    def test_member_count_is_never_recorded_as_an_unread_count(self):
        auto_reply.LAST_PROCESSED_UNREAD.pop(GROUP, None)
        self.addCleanup(auto_reply.LAST_PROCESSED_UNREAD.pop, GROUP, None)
        self.addCleanup(auto_reply.LAST_PROCESSED_UNREAD.pop, "林如婷", None)
        windows = [window(5, f"{GROUP} (477)"), window(6, "林如婷 (3)")]
        with mock.patch.object(auto_reply, "get_all_line_windows", return_value=windows), mock.patch.object(auto_reply, "log_v"):
            auto_reply.init_unread_baselines([GROUP, "林如婷"])
        self.assertNotIn(GROUP, auto_reply.LAST_PROCESSED_UNREAD)                         # 群組的 (477) 是成員數
        self.assertEqual(auto_reply.LAST_PROCESSED_UNREAD.get("林如婷"), 3)                # 1:1 的 (3) 仍是未讀數


class TestBindView(GroupTestCase):
    def bind(self, title):
        files = iter([{}, {"p": (2.0, 100)}, {"p": (2.0, 100)}])
        clock = iter([0.0, 0.1, 0.2, 99.0])
        with mock.patch.object(auto_reply, "get_active_line_cache_dir", return_value="cache"), \
             mock.patch.object(auto_reply.os.path, "isdir", return_value=True), \
             mock.patch.object(auto_reply, "get_all_cache_files", side_effect=lambda d: next(files)), \
             mock.patch.object(auto_reply, "extract_chat_hash_from_path", return_value="a" * 40), \
             mock.patch.object(auto_reply, "foreground_window_title", return_value=title), \
             mock.patch.object(auto_reply, "bind_channel_manually", return_value=(True, "bound")) as bound, \
             mock.patch.object(auto_reply.time, "sleep"), mock.patch.object(auto_reply.time, "time", side_effect=lambda: next(clock)):
            return auto_reply.bind_channel_by_viewing(GROUP, [GROUP], seconds=1), bound

    def test_group_can_be_bound_while_its_title_carries_the_member_count(self):
        (ok, _), bound = self.bind(f"{GROUP} (477)")
        self.assertTrue(ok)
        bound.assert_called_once()

    def test_binding_is_refused_when_another_chat_is_in_front(self):
        (ok, msg), bound = self.bind("別的聊天室 (12)")
        self.assertFalse(ok)
        self.assertIn("前景視窗不是", msg)
        bound.assert_not_called()


class TestAutoBindIgnoresGroups(GroupTestCase):
    """自動綁定只認一對一：開著的群組視窗不算候選，不會擋住一對一的綁定，也不會被自動綁上"""

    def run_bind(self, windows):
        calls = []

        def fake_find(name):
            return windows.get(name, [])

        with mock.patch.object(auto_reply, "find_target_chat_window", side_effect=fake_find),              mock.patch.object(auto_reply, "verify_is_not_group_window_uia", return_value=True),              mock.patch.object(auto_reply, "get_all_open_chat_windows", side_effect=lambda: calls.append(1) or [(7, "林如婷", "Qt", True)]),              mock.patch.object(auto_reply.win32gui, "IsIconic", return_value=True), mock.patch.object(auto_reply, "log_v"):
            result = auto_reply.try_auto_bind_channel("a" * 40, "x.eimg", 100.0, [GROUP, "林如婷"], {}, 1)
        return result, calls

    def test_an_open_group_window_does_not_block_a_one_to_one_candidate(self):
        both = {GROUP: [(5, GROUP, "Qt", True)], "林如婷": [(7, "林如婷", "Qt", True)]}
        _, reached_the_next_check = self.run_bind(both)
        self.assertEqual(reached_the_next_check, [1])                                   # 只剩林如婷一位候選，才會往下做後面的核驗

    def test_a_group_window_alone_is_never_a_candidate(self):
        result, reached_the_next_check = self.run_bind({GROUP: [(5, GROUP, "Qt", True)]})
        self.assertIsNone(result)
        self.assertEqual(reached_the_next_check, [])                                   # 沒有一對一候選：直接拒絕，群組不會被自動綁上


if __name__ == "__main__":
    unittest.main()
