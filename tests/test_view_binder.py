import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_reply
import view_binder

CH_A = "a" * 40
CH_B = "b" * 40
HWND = 77


class FakeIo:
    """假的 LINE：click 之後依 writes 腳本寫入快取；全部動作都記錄下來，方便檢查順序與有沒有亂點"""

    def __init__(self, window=HWND, opens=True, centers=((100, 200), (100, 300), (100, 400), (100, 500)),
                 writes_per_click=None, fg=True, fg_after_click=True, click_ok=True, bind_result=(True, "已綁定"),
                 photos_after_scrolls=0, can_scroll=True, noise_ticks=0):
        self.window = window
        self.opens = opens
        self.centers = list(centers)
        self.writes_per_click = writes_per_click or []      # 第 N 次點擊寫入的 [(路徑, 頻道)]
        self.fg = fg
        self.fg_after_click = fg_after_click
        self.click_ok = click_ok
        self.bind_result = bind_result
        self.cache = {}
        self.clicks = []
        self.closed_viewer = 0
        self.bound = []
        self.finished = []
        self.opened_calls = 0
        self.clicked = False
        self.t = 0.0
        self.photos_after_scrolls = photos_after_scrolls      # 要往上捲幾頁照片才會出現 (0 = 一開始就看得到)
        self.can_scroll = can_scroll
        self.scroll_pos = 0                                    # 往上捲了幾頁 (淨值)
        self.scroll_log = []
        self.noise_ticks = noise_ticks                         # 前 N 次快照每次都多一個別的頻道的檔案 (模擬別的頻道還在寫)

    def scroll(self, hwnd, up):
        if not self.can_scroll:
            return False
        self.scroll_log.append(up)
        self.scroll_pos += 1 if up else -1
        return True

    def find_window(self):
        return self.window

    def open_window(self):
        self.opened_calls += 1
        if self.opens:
            self.window = HWND
        return self.opens

    def bring_to_front(self, hwnd):
        return True

    def foreground_matches(self):
        return self.fg_after_click if self.clicked else self.fg

    def photo_centers(self, hwnd):
        return list(self.centers) if self.scroll_pos >= self.photos_after_scrolls else []

    def click(self, hwnd, x, y):
        if not self.click_ok:
            return False
        self.clicks.append((x, y))
        self.clicked = True
        n = len(self.clicks) - 1
        if n < len(self.writes_per_click):
            for path, _ in self.writes_per_click[n]:
                self.cache[path] = (1000.0 + n, 10)
        return True

    def close_viewer(self):
        self.closed_viewer += 1

    def snapshot(self):
        if self.noise_ticks > 0:
            self.noise_ticks -= 1
            self.cache[f"{CH_B}/noise{self.noise_ticks}"] = (float(self.noise_ticks), 1)
        return dict(self.cache)

    def channel_of(self, path):
        return path.split("/")[0]

    def bind(self, ch):
        self.bound.append(ch)
        return self.bind_result

    def finish(self, hwnd, opened_by_us):
        self.finished.append(opened_by_us)


def run(io):
    clock = {"t": 0.0}

    def sleep(s):
        clock["t"] += s

    return view_binder.bind_by_opening("小明", io, sleep=sleep, now=lambda: clock["t"])


class TestBindByOpening(unittest.TestCase):
    def test_success_single_channel_window_already_open(self):
        io = FakeIo(writes_per_click=[[(CH_A + "/x.eimg", CH_A)]])
        ok, msg, touched = run(io)
        self.assertTrue(ok)
        self.assertEqual(io.bound, [CH_A])
        self.assertEqual(io.finished, [False])          # 本來就開著的視窗不動
        self.assertEqual(io.clicks, [(100, 200)])       # 第一張就成功，不再點
        self.assertEqual(io.closed_viewer, 1)           # 放大檢視要關掉
        self.assertIn(CH_A + "/x.eimg", touched)

    def test_window_opened_by_us_is_handed_back(self):
        io = FakeIo(window=None, writes_per_click=[[(CH_A + "/x.eimg", CH_A)]])
        ok, _, _ = run(io)
        self.assertTrue(ok)
        self.assertEqual(io.opened_calls, 1)
        self.assertEqual(io.finished, [True])

    def test_cannot_open_window(self):
        io = FakeIo(window=None, opens=False)
        ok, _, _ = run(io)
        self.assertFalse(ok)
        self.assertEqual(io.clicks, [])
        self.assertEqual(io.finished, [])               # 什麼都沒開，沒有要放回去的

    def test_two_channels_written_does_not_bind(self):
        io = FakeIo(writes_per_click=[[(CH_A + "/x.eimg", CH_A), (CH_B + "/y.eimg", CH_B)]])
        ok, _, _ = run(io)
        self.assertFalse(ok)
        self.assertEqual(io.bound, [])

    def test_foreground_not_target_at_write_does_not_bind(self):
        io = FakeIo(writes_per_click=[[(CH_A + "/x.eimg", CH_A)]], fg_after_click=False)
        ok, msg, _ = run(io)
        self.assertFalse(ok)
        self.assertEqual(io.bound, [])
        self.assertIn("前景", msg)

    def test_foreground_not_target_before_click_does_not_click(self):
        io = FakeIo(fg=False)
        ok, _, _ = run(io)
        self.assertFalse(ok)
        self.assertEqual(io.clicks, [])

    def test_no_photo_to_click(self):
        io = FakeIo(centers=())
        ok, msg, _ = run(io)
        self.assertFalse(ok)
        self.assertIn("找不到可以點開的照片", msg)
        self.assertEqual(io.scroll_log.count(True), view_binder.MAX_SCROLL_PAGES)       # 往上捲到上限才放棄
        self.assertEqual(io.scroll_pos, 0)                                              # 而且捲回原位

    def test_scrolls_up_to_find_a_photo_then_scrolls_back(self):
        io = FakeIo(photos_after_scrolls=2, writes_per_click=[[(CH_A + "/x.eimg", CH_A)]])
        ok, _, _ = run(io)
        self.assertTrue(ok)
        self.assertEqual(io.scroll_log, [True, True, False, False])                     # 捲兩頁找到、綁完捲回去
        self.assertEqual(io.scroll_pos, 0)

    def test_photo_visible_at_once_never_scrolls(self):
        io = FakeIo(writes_per_click=[[(CH_A + "/x.eimg", CH_A)]])
        run(io)
        self.assertEqual(io.scroll_log, [])

    def test_scroll_refused_gives_up_without_scrolling_back(self):
        io = FakeIo(photos_after_scrolls=1, can_scroll=False)
        ok, _, _ = run(io)
        self.assertFalse(ok)
        self.assertEqual(io.scroll_log, [])
        self.assertEqual(io.clicks, [])

    def test_scrolls_back_even_when_binding_fails(self):
        io = FakeIo(photos_after_scrolls=1, writes_per_click=[[(CH_A + "/x.eimg", CH_A), (CH_B + "/y.eimg", CH_B)]])
        ok, _, _ = run(io)
        self.assertFalse(ok)
        self.assertEqual(io.scroll_pos, 0)

    def test_click_refused_when_covered(self):
        io = FakeIo(click_ok=False)
        ok, _, _ = run(io)
        self.assertFalse(ok)
        self.assertEqual(io.bound, [])

    def test_first_click_silent_second_click_binds(self):
        io = FakeIo(writes_per_click=[[], [(CH_A + "/x.eimg", CH_A)]])
        ok, _, _ = run(io)
        self.assertTrue(ok)
        self.assertEqual(io.clicks, [(100, 200), (100, 300)])

    def test_gives_up_after_max_clicks(self):
        io = FakeIo()
        ok, _, _ = run(io)
        self.assertFalse(ok)
        self.assertEqual(len(io.clicks), view_binder.MAX_CLICKS)
        self.assertEqual(io.bound, [])

    def test_lost_foreground_after_silent_click_aborts(self):
        io = FakeIo(fg_after_click=False)
        ok, _, _ = run(io)
        self.assertFalse(ok)
        self.assertEqual(len(io.clicks), 1)             # 前景不對就不再點下一張
        self.assertGreaterEqual(io.closed_viewer, 1)

    def test_waits_for_a_busy_channel_to_go_quiet_before_clicking(self):
        # 別的頻道在寫檔：寫完 (快照不再變) 才點，而且它的寫入不算成點圖造成的
        io = FakeIo(writes_per_click=[[(CH_A + "/x.eimg", CH_A)]], noise_ticks=2)
        ok, _, _ = run(io)
        self.assertTrue(ok)
        self.assertEqual(io.bound, [CH_A])

    def test_never_clicks_while_the_cache_keeps_changing(self):
        io = FakeIo(writes_per_click=[[(CH_A + "/x.eimg", CH_A)]], noise_ticks=1000)
        ok, msg, _ = run(io)
        self.assertFalse(ok)
        self.assertEqual(io.clicks, [])
        self.assertEqual(io.bound, [])
        self.assertIn("快取一直在變動", msg)

    def test_bind_refusal_is_reported(self):
        io = FakeIo(writes_per_click=[[(CH_A + "/x.eimg", CH_A)]], bind_result=(False, "已綁定給別人"))
        ok, msg, _ = run(io)
        self.assertFalse(ok)
        self.assertEqual(msg, "已綁定給別人")

    def test_exception_still_hands_window_back(self):
        io = FakeIo(window=None)
        io.photo_centers = mock.Mock(side_effect=RuntimeError("uia gone"))
        ok, msg, _ = run(io)
        self.assertFalse(ok)
        self.assertIn("uia gone", msg)
        self.assertEqual(io.finished, [True])


class TestHistoryPhotoCenters(unittest.TestCase):
    """用合成的聊天室截圖測「哪些區塊是照片」：文字氣泡 (淺灰底) 與太矮、太窄、被切到的區塊都不能點"""

    def make(self):
        from PIL import Image, ImageDraw
        import random
        rnd = random.Random(1)
        img = Image.new("RGB", (600, 720), (255, 255, 255))
        d = ImageDraw.Draw(img)
        d.rectangle((70, 20, 320, 79), fill=(238, 238, 238))             # 矮的文字氣泡 (60px)
        d.rectangle((70, 100, 320, 219), fill=(238, 238, 238))           # 高的文字氣泡 (120px，多行)
        for x in range(70, 370):                                         # 照片：彩色雜訊 300x200
            for y in range(240, 440):
                img.putpixel((x, y), (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
        d.rectangle((70, 460, 110, 560), fill=(200, 50, 50))             # 窄而高 (40x100)：不是照片
        d.rectangle((70, 580, 320, 700), fill=(133, 226, 73))            # 高的綠色氣泡 (自己的訊息，250x120)
        return img

    def run_with(self, img, blocks):
        def is_content(px):
            return abs(px[0] - 255) + abs(px[1] - 255) + abs(px[2] - 255) > 30
        found = (img, 1.0, 56, 216, is_content, blocks)
        with mock.patch.object(auto_reply, "_left_message_blocks", return_value=found),              mock.patch.object(auto_reply, "get_window_true_rect", return_value=(100, 100, 700, 720)):
            return auto_reply._history_photo_centers(1, "left")

    def test_only_the_big_colourful_block_is_a_photo(self):
        blocks = [(700, 580), (560, 460), (439, 240), (219, 100), (79, 20)]          # 由下往上 (底, 頂)
        centers = self.run_with(self.make(), blocks)
        self.assertEqual(len(centers), 1)
        x, y = centers[0]
        self.assertTrue(100 + 70 <= x <= 100 + 370)                      # 落在照片的水平範圍 (加上視窗原點)
        self.assertEqual(y, 100 + int(620 * 0.18) + (240 + 439) // 2)    # 垂直中心 (加上截圖上緣在螢幕上的位置)

    def test_a_photo_cut_by_the_top_is_clickable_when_enough_of_it_shows_but_a_sliver_is_not(self):
        # 照片比歷史區還高時一定會被切到：露出的部分夠大 (>= 80) 就點露出部分的中心；只露出一小條不點
        self.assertEqual(len(self.run_with(self.make(), [(439, 1)])), 1)
        self.assertEqual(self.run_with(self.make(), [(60, 1)]), [])

    def test_no_blocks_or_unreadable_window_gives_nothing(self):
        with mock.patch.object(auto_reply, "_left_message_blocks", return_value=None):
            self.assertEqual(auto_reply._history_photo_centers(1, "left"), [])

    def test_newest_photo_first_and_limit(self):
        from PIL import Image
        import random
        rnd = random.Random(2)
        img = Image.new("RGB", (600, 900), (255, 255, 255))
        for top in (50, 300, 550):
            for x in range(70, 370):
                for y in range(top, top + 200):
                    img.putpixel((x, y), (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
        blocks = [(749, 550), (499, 300), (249, 50)]
        centers = self.run_with(img, blocks)
        self.assertEqual(len(centers), 3)
        self.assertGreater(centers[0][1], centers[1][1])                 # 最新 (最下面) 的在前
        self.assertGreater(centers[1][1], centers[2][1])


class TestSweep(unittest.TestCase):
    def setUp(self):
        auto_reply._VIEW_BIND_LAST_TRY.clear()
        auto_reply._VIEW_BIND_FAILS.clear()
        auto_reply._VIEW_BIND_WINDOWS.clear()
        auto_reply.PROCESSED_CACHE_FILES.clear()

    def test_in_view_bind_window(self):
        auto_reply._VIEW_BIND_WINDOWS.append((100.0, 110.0))
        self.assertTrue(auto_reply._in_view_bind_window(105.0))
        self.assertFalse(auto_reply._in_view_bind_window(99.0))
        self.assertFalse(auto_reply._in_view_bind_window(111.0))

    def test_sweep_skips_bound_and_backed_off_and_prefers_open_windows(self):
        tried = []

        def fake_bind(name, io, log=None):
            tried.append(name)
            return False, "no", {}

        targets = ["已綁定", "甲", "乙", "丙", "丁"]
        auto_reply._VIEW_BIND_LAST_TRY["丁"] = auto_reply.time.time()      # 剛試過
        with mock.patch.object(auto_reply.view_binder, "bind_by_opening", side_effect=fake_bind), \
             mock.patch.object(auto_reply, "find_target_chat_window", side_effect=lambda t: [(1, t, "c", True)] if t == "丙" else []):
            auto_reply.auto_bind_by_viewing({}, targets, {CH_A: "已綁定"})
        self.assertEqual(tried, ["丙", "甲", "乙"])                          # 已開著的先、最多 3 位、不含已綁定與剛試過的
        # 再觸發一次：這 3 位都在等待期內，只剩 丁 (剛才試過) -> 也在等待期內，所以誰都不試
        tried.clear()
        with mock.patch.object(auto_reply.view_binder, "bind_by_opening", side_effect=fake_bind), \
             mock.patch.object(auto_reply, "find_target_chat_window", return_value=[]):
            auto_reply.auto_bind_by_viewing({}, targets, {CH_A: "已綁定"})
        self.assertEqual(tried, [])

    def test_with_a_photo_time_only_targets_that_just_showed_activity_are_tried(self):
        now = auto_reply.time.time()
        tried = []
        state = {"甲": {"changed_at": now - 3}, "乙": {"changed_at": now - 600}, "丙": {"changed_at": 0.0}}
        with mock.patch.dict(auto_reply._TARGET_WINDOW_STATE, state, clear=True):
            with mock.patch.object(auto_reply, "find_target_chat_window", return_value=[]):
                with mock.patch.object(auto_reply.view_binder, "bind_by_opening",
                                       side_effect=lambda n, io, log=None: (tried.append(n), (False, "no", {}))[1]):
                    auto_reply.auto_bind_by_viewing({}, ["甲", "乙", "丙", "丁"], {}, since=now)
        self.assertEqual(tried, ["甲"])         # 甲：視窗剛變動。乙太久以前、丙只有基準線 (0)、丁沒有視窗紀錄

    def test_with_a_photo_time_and_no_sign_of_activity_nobody_is_opened(self):
        now = auto_reply.time.time()
        with mock.patch.dict(auto_reply._TARGET_WINDOW_STATE, {}, clear=True):
            with mock.patch.object(auto_reply.view_binder, "bind_by_opening") as m:
                auto_reply.auto_bind_by_viewing({}, ["甲", "乙"], {}, since=now)
        m.assert_not_called()

    def test_sweep_stops_at_first_success_and_marks_touched_processed(self):
        def fake_bind(name, io, log=None):
            return True, "ok", {"p/x.eimg": (123.0, 5)}

        with mock.patch.object(auto_reply.view_binder, "bind_by_opening", side_effect=fake_bind) as m, \
             mock.patch.object(auto_reply, "find_target_chat_window", return_value=[]):
            auto_reply.auto_bind_by_viewing({}, ["甲", "乙"], {})
        self.assertEqual(m.call_count, 1)
        self.assertIn(("p/x.eimg", 123.0), auto_reply.PROCESSED_CACHE_FILES)
        self.assertEqual(len(auto_reply._VIEW_BIND_WINDOWS), 1)

    def test_failed_targets_back_off_exponentially_and_success_resets(self):
        self.assertEqual(auto_reply._view_bind_wait("甲"), auto_reply.VIEW_BIND_RETRY_SECONDS)
        auto_reply._VIEW_BIND_FAILS["甲"] = 2
        self.assertEqual(auto_reply._view_bind_wait("甲"), auto_reply.VIEW_BIND_RETRY_SECONDS * 4)
        auto_reply._VIEW_BIND_FAILS["甲"] = 30
        self.assertEqual(auto_reply._view_bind_wait("甲"), auto_reply.VIEW_BIND_RETRY_MAX_SECONDS)
        with mock.patch.object(auto_reply.view_binder, "bind_by_opening", return_value=(False, "no", {})),              mock.patch.object(auto_reply, "find_target_chat_window", return_value=[]):
            auto_reply._VIEW_BIND_FAILS["甲"] = 0
            auto_reply.auto_bind_by_viewing({}, ["甲"], {})
        self.assertEqual(auto_reply._VIEW_BIND_FAILS["甲"], 1)
        with mock.patch.object(auto_reply.view_binder, "bind_by_opening", return_value=(True, "ok", {})),              mock.patch.object(auto_reply, "find_target_chat_window", return_value=[]):
            auto_reply._VIEW_BIND_LAST_TRY.clear()
            auto_reply.auto_bind_by_viewing({}, ["甲"], {})
        self.assertEqual(auto_reply._VIEW_BIND_FAILS["甲"], 0)

    def test_a_target_with_stray_spaces_is_still_recognised_as_bound(self):
        with mock.patch.object(auto_reply.view_binder, "bind_by_opening") as m,              mock.patch.object(auto_reply, "find_target_chat_window", return_value=[]):
            auto_reply.auto_bind_by_viewing({}, [" 小明 "], {CH_A: "小明"})
        m.assert_not_called()

    def test_right_side_is_scanned_only_when_the_left_side_has_too_few_photos(self):
        io = auto_reply._ViewBindIo("小明", {}, ["小明"])
        calls = []

        def fake(hwnd, side, limit=6):
            calls.append(side)
            return {"left": [(1, 1), (2, 2), (3, 3)], "right": [(9, 9)]}[side]

        with mock.patch.object(auto_reply, "_history_photo_centers", side_effect=fake):
            self.assertEqual(io.photo_centers(HWND), [(1, 1), (2, 2), (3, 3)])
        self.assertEqual(calls, ["left"])
        calls.clear()
        with mock.patch.object(auto_reply, "_history_photo_centers",
                               side_effect=lambda hwnd, side, limit=6: [(1, 1)] if side == "left" else [(9, 9)]):
            self.assertEqual(io.photo_centers(HWND), [(1, 1), (9, 9)])

    def test_click_refuses_when_another_window_is_on_top(self):
        io = auto_reply._ViewBindIo("小明", {}, ["小明"])
        with mock.patch.object(auto_reply, "_root_window_at", return_value=999), \
             mock.patch.object(auto_reply.pyautogui, "click") as click:
            self.assertFalse(io.click(HWND, 10, 10))
            click.assert_not_called()
        with mock.patch.object(auto_reply, "_root_window_at", return_value=HWND), \
             mock.patch.object(auto_reply.pyautogui, "click") as click:
            self.assertTrue(io.click(HWND, 10, 10))
            click.assert_called_once_with(10, 10)


if __name__ == "__main__":
    unittest.main()
