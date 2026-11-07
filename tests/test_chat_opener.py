import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_reply
import chat_opener

MAIN = 11
ORIGIN = (100, 50)
SEARCH_BOX = (200, 100, 500, 150)                      # 螢幕座標：相對於視窗，上緣在 y=50、下緣在 y=100
TAB = ("全部", (60, 20))                               # 分頁標籤在搜尋欄上方
ECHO = ("Notify Bot", (150, 75))                       # 剛寫進搜尋欄的字，也會被 OCR 讀到 (位置在搜尋欄裡)
RESULT_TOP = ("Notify Bot", (267, 225))
RESULT_LOW = ("Notify Bot", (267, 396))


class FakeUi:
    """記錄每個動作的假 LINE 介面；螢幕內容 (截圖) 取決於目前選的分頁"""

    def __init__(self, *, main=MAIN, front=True, search_box=SEARCH_BOX, window_appears=True, windows_open=False,
                 tab=1, can_select=True, narrow=False, moves_even_when_failing=False):
        self.main = main
        self.front = front
        self.search_box = search_box
        self.window_appears = window_appears
        self.windows_open = windows_open
        self.tab = tab
        self.can_select = can_select
        self.narrow = narrow
        self.moves_even_when_failing = moves_even_when_failing
        self.events = []

    def find_chat_window(self, name):
        if self.windows_open:
            return [(99, name, "Qt", True)]
        if "double_click" in self.names() and self.window_appears:
            return [(99, name, "Qt", True)]
        return []

    def main_window(self):
        return self.main

    def bring_to_front(self, hwnd):
        self.events.append(("bring_to_front", hwnd))

    def is_front(self, hwnd):
        return self.front

    def search_box_rect(self, hwnd):
        return self.search_box

    def set_search_text(self, text):
        self.events.append(("set_search_text", text))

    def clear_search(self):
        self.events.append(("clear_search",))

    def selected_tab(self, hwnd):
        return self.tab

    def select_tab(self, hwnd, index):
        self.events.append(("select_tab", index))
        if self.can_select or self.moves_even_when_failing:
            self.tab = index                                  # moves_even_when_failing：點擊其實切過去了，只是事後驗證 (底線標記) 失敗
        return self.can_select

    def ensure_wide(self, hwnd):
        self.events.append(("ensure_wide",))
        return (0, 0, 546, 700) if self.narrow else None

    def restore_rect(self, hwnd, rect):
        self.events.append(("restore_rect", rect))

    def double_click(self, x, y):
        self.events.append(("double_click", x, y))

    def screenshot(self, hwnd):
        self.events.append(("screenshot",))
        return self.tab, ORIGIN                       # 「截圖」就是目前的分頁，read_boxes 依分頁回傳內容

    def names(self):
        return [e[0] for e in self.events]


def run(ui, boxes, name="Notify Bot", logs=None):
    """boxes: 所有分頁都一樣的 OCR 結果 (list)，或 {分頁索引: list}"""
    logs = logs if logs is not None else []
    read = (lambda shot: boxes[shot]) if isinstance(boxes, dict) else (lambda shot: boxes)
    with mock.patch.object(chat_opener.time, "time", side_effect=iter(range(0, 1000))):
        ok = chat_opener.open_chat_window(name, ui, read_boxes=read, log=logs.append, sleep=lambda s: None)
    return ok, logs


class TestFindResultRows(unittest.TestCase):
    def test_only_exact_names_below_the_search_box(self):
        boxes = [("Notify Bot", (500, 150)),                    # 搜尋欄裡剛貼上的字：在 min_y 以上，不算
                 ("Notify Bot", (267, 225)), ("notify  bot", (267, 396)),
                 ("Notify Bot 通知群", (267, 300)),               # 包含但不是完全相同：不算
                 ("Notify Bot2", (267, 340)),                     # 名稱多一截：不算
                 ("Notify", (267, 360))]
        self.assertEqual(chat_opener.find_result_rows(boxes, "Notify Bot", min_y=200), [(267, 225), (267, 396)])

    def test_group_rows_may_carry_the_member_count_but_nothing_else(self):
        # 真機：群組的搜尋結果是「理財幼幼班_Z (142)」，視窗標題只有名稱
        boxes = [("理財幼幼班_Z (142)", (300, 250)), ("理財幼幼班_Z（9）", (300, 300)), ("理財幼幼班_Z(7)", (300, 350)),
                 ("理財幼幼班_Z (x)", (300, 400)), ("理財幼幼班_Z (142) 群", (300, 450)), ("理財幼幼班_Z ()", (300, 500)),
                 ("舊理財幼幼班_Z (142)", (300, 550))]
        self.assertEqual(chat_opener.find_result_rows(boxes, "理財幼幼班_Z", min_y=200, allow_count=True), [(300, 250), (300, 300), (300, 350)])

    def test_ocr_reading_traditional_as_simplified_still_matches(self):
        # 真機：OCR 把「理財幼幼班_Z (142)」讀成「理财幼幼班_Z (142)」
        boxes = [("理财幼幼班_Z (142)", (300, 250)), ("理财幼幼班_Z", (300, 120))]
        self.assertEqual(chat_opener.find_result_rows(boxes, "理財幼幼班_Z", min_y=200, allow_count=True), [(300, 250)])

    NAME = "東南里李武雄服務處-公用"

    def test_real_search_result_with_a_truncated_name_count_and_time(self):
        # 真機 (2026-11-01)：名稱太長被列表截成「服...」，OCR 把名稱、成員數、時間讀成同一段；第二行是最後一則訊息的預覽
        boxes = [("东南里李武雄服務處-公用", (273, 124)),                  # 搜尋欄裡剛打的字 (在 min_y 以上)
                 ("東南里李武雄服..．（477）上午9:20", (371, 224)),
                 ("李武雄理事長傳送了圖", (320, 251))]
        self.assertEqual(chat_opener.find_result_rows(boxes, self.NAME, min_y=151, allow_count=True), [(371, 224)])

    def test_full_name_may_be_followed_by_count_and_time(self):
        for text in ("東南里李武雄服務處-公用（477）上午9:20", "東南里李武雄服務處-公用 (477) 下午 3:05", "東南里李武雄服務處-公用 昨天", "東南里李武雄服務處-公用"):
            self.assertEqual(chat_opener.find_result_rows([(text, (1, 300))], self.NAME, 0, allow_count=True), [(1, 300)], text)
        self.assertEqual(chat_opener.find_result_rows([("Notify Bot 上午9:20", (1, 300))], "Notify Bot", 0), [(1, 300)])

    def test_truncated_rows_are_refused_when_they_are_not_clearly_this_chat(self):
        N = self.NAME
        for text in ("東南里..（477）上午9:20",                      # 露出的前綴太短
                     "西南里李武雄服..（477）上午9:20",                 # 前綴不是名稱的開頭
                     "東南里李武雄服務處-公用..（477）上午9:20",         # 前綴已經是整個名稱：這不是截斷
                     "東南里李武雄服..（477）上午9:20 轉寄",             # 尾巴多了別的字
                     "東南里李武雄服..（x）上午9:20",                   # 括號裡不是數字
                     "東南里李武雄服.（477）上午9:20"):                 # 只有一個點，不是刪節號
            self.assertEqual(chat_opener.find_result_rows([(text, (1, 300))], N, 0, allow_count=True), [], text)

    def test_two_truncated_rows_are_ambiguous_so_nothing_is_clicked(self):
        boxes = [("東南里李武雄服..．（477）上午9:20", (1, 300)), ("東南里李武雄服..．（12）上午8:01", (1, 400))]
        self.assertEqual(chat_opener.find_result_rows(boxes, self.NAME, 0, allow_count=True), [])          # 另一個同前綴的聊天室也在結果裡：分不出來
        self.assertEqual(chat_opener.find_result_rows(boxes[:1], self.NAME, 0, allow_count=True), [(1, 300)])

    def test_an_exact_row_wins_over_truncated_rows(self):
        boxes = [("東南里李武雄服..（5）上午9:20", (1, 300)), ("東南里李武雄服務處-公用（477）", (1, 400)), ("東南里李武雄服..（9）上午9:21", (1, 500))]
        self.assertEqual(chat_opener.find_result_rows(boxes, self.NAME, 0, allow_count=True), [(1, 400)])

    def test_rows_above_the_search_box_bottom_never_count_even_when_truncated(self):
        self.assertEqual(chat_opener.find_result_rows([("東南里李武雄服..（477）上午9:20", (1, 100))], self.NAME, 151, allow_count=True), [])

    def test_a_one_to_one_target_never_matches_a_row_with_a_member_count(self):
        # 「名稱 (12)」那一列是同名的群組，不是這位好友：一對一 (預設) 不接受，只有要開群組時才接受
        boxes = [("羅毓斌 (12)", (1, 250)), ("羅毓斌（5）上午9:20", (1, 300))]
        self.assertEqual(chat_opener.find_result_rows(boxes, "羅毓斌", 0), [])
        self.assertEqual(chat_opener.find_result_rows(boxes, "羅毓斌", 0, allow_count=True), [(1, 250), (1, 300)])
        both = boxes + [("羅毓斌 上午9:20", (1, 400))]
        self.assertEqual(chat_opener.find_result_rows(both, "羅毓斌", 0), [(1, 400)])                 # 同名群組在上面也不會被點到

    def test_a_truncated_one_to_one_name_may_carry_a_time_but_not_a_member_count(self):
        name = "翁彦臻(公羽)凱基人壽/信託咨詢"
        self.assertEqual(chat_opener.find_result_rows([("翁彦臻(公羽)凱基人..上午9:20", (1, 300))], name, 0), [(1, 300)])
        self.assertEqual(chat_opener.find_result_rows([("翁彦臻(公羽)凱基人..（12）上午9:20", (1, 300))], name, 0), [])

    LAST = "李月琴@全球人壽"

    def test_real_search_result_where_ocr_dropped_the_last_character(self):
        # 真機 (2026-11-04)：整列名稱被讀成「李月琴@全球人」(沒有刪節號)，時間與預覽是別的框；搜尋欄裡的回聲在 min_y 以上
        boxes = [("李月琴@全球人", (273, 124)), ("聊天 1", (60, 200)), ("李月琴@全球人", (300, 232)),
                 ("星期六", (420, 232)), ("明年的桌月唇也出來了,順道", (320, 262)), ("訊息 1", (60, 330)), ("Notify Bot", (267, 360))]
        self.assertEqual(chat_opener.find_result_rows(boxes, self.LAST, min_y=151), [(300, 232)])

    def test_dropped_last_character_may_be_followed_by_a_time(self):
        self.assertEqual(chat_opener.find_result_rows([("李月琴@全球人 星期六", (1, 300))], self.LAST, 0), [(1, 300)])
        self.assertEqual(chat_opener.find_result_rows([("李月琴@全球人昨天", (1, 300))], self.LAST, 0), [(1, 300)])

    def test_dropped_last_character_is_refused_when_it_is_not_clearly_this_chat(self):
        N = self.LAST
        for text in ("李月琴@全球",                    # 少了兩個字
                     "李月琴@全球人X",                  # 尾巴多了別的字
                     "李月琴@全球人 (12)",              # 一對一不接受成員數
                     "李月琴@球人",                    # 前綴不是名稱的開頭
                     "李月琴@全球人壽險"):              # 多一截 (這不是少一個字)
            self.assertEqual(chat_opener.find_result_rows([(text, (1, 300))], N, 0), [], text)
        self.assertEqual(chat_opener.find_result_rows([("李月", (1, 300))], "李月琴", 0), [])        # 名稱太短，前綴不夠長

    def test_two_dropped_character_rows_are_ambiguous(self):
        boxes = [("李月琴@全球人", (1, 300)), ("李月琴@全球人", (1, 400))]
        self.assertEqual(chat_opener.find_result_rows(boxes, self.LAST, 0), [])
        # 與截斷的列合併計算：也分不出是哪一個
        mixed = [("李月琴@全球人", (1, 300)), ("李月琴@全球..上午9:20", (1, 400))]
        self.assertEqual(chat_opener.find_result_rows(mixed, self.LAST, 0), [])

    def test_a_full_name_row_wins_over_dropped_character_rows(self):
        boxes = [("李月琴@全球人", (1, 300)), ("李月琴@全球人壽", (1, 400)), ("李月琴@全球人", (1, 500))]
        self.assertEqual(chat_opener.find_result_rows(boxes, self.LAST, 0), [(1, 400)])

    def test_a_dropped_character_group_row_may_carry_the_member_count(self):
        self.assertEqual(chat_opener.find_result_rows([("理財幼幼班_ (142)", (1, 300))], "理財幼幼班_Z", 0, allow_count=True), [(1, 300)])
        self.assertEqual(chat_opener.find_result_rows([("理財幼幼班_ (142)", (1, 300))], "理財幼幼班_Z", 0), [])

    def test_sorted_top_down_and_empty(self):
        boxes = [("Mom", (1, 400)), ("Mom", (1, 250))]
        self.assertEqual(chat_opener.find_result_rows(boxes, "Mom", 0), [(1, 250), (1, 400)])
        self.assertEqual(chat_opener.find_result_rows([], "Mom", 0), [])
        self.assertEqual(chat_opener.find_result_rows([("x", (1, 1))], "", 0), [])


class TestOpenChatWindow(unittest.TestCase):
    def test_already_open_does_nothing(self):
        ui = FakeUi(windows_open=True)
        ok, _ = run(ui, [])
        self.assertTrue(ok)
        self.assertEqual(ui.events, [])                                                  # 不碰主視窗

    def test_happy_path_when_already_on_the_all_tab_never_switches_tabs(self):
        ui = FakeUi(tab=0)
        ok, _ = run(ui, [ECHO, RESULT_LOW, RESULT_TOP, ("別人", (200, 300))])
        self.assertTrue(ok)
        self.assertEqual(ui.events, [
            ("bring_to_front", MAIN),
            ("set_search_text", "Notify Bot"), ("screenshot",),
            ("double_click", 100 + 267, 50 + 225),                                       # 最上面那一列；搜尋欄裡的回聲文字不算
            ("clear_search",)])                                                          # 最後一定清掉搜尋欄

    def test_always_searches_in_the_all_tab_and_restores_the_users_tab(self):
        # 真機：主視窗停在別的分頁 (第 8 個) 時，在那個分頁找得到列、雙擊卻不開視窗；所以不論當前分頁找不找得到，一律先切到「全部」
        ui = FakeUi(tab=8)
        ok, _ = run(ui, [ECHO, RESULT_TOP])
        self.assertTrue(ok)
        self.assertEqual(ui.events, [
            ("bring_to_front", MAIN),
            ("ensure_wide",), ("select_tab", 0),
            ("set_search_text", ""), ("set_search_text", "Notify Bot"), ("screenshot",),     # 切分頁後先清搜尋欄再打名稱
            ("double_click", 100 + 267, 50 + 225),
            ("clear_search",), ("bring_to_front", MAIN),
            ("ensure_wide",), ("select_tab", 8)])
        self.assertEqual(ui.tab, 8)                                                      # 使用者原本的分頁還原

    def test_the_echo_of_the_search_text_is_never_double_clicked(self):
        # 真機實測的錯誤：搜尋欄自己的字被當成結果，雙擊到搜尋欄
        ui = FakeUi(tab=0)
        ok, logs = run(ui, [ECHO])
        self.assertFalse(ok)
        self.assertNotIn("double_click", ui.names())
        self.assertTrue(any("完全相符" in l for l in logs))

    def test_a_row_that_only_exists_in_another_tab_is_never_used(self):
        # 其他分頁的畫面 (截圖) 有符合的列，但我們在「全部」搜尋，看的是「全部」的畫面
        ui = FakeUi(tab=1)
        ok, _ = run(ui, {1: [ECHO, RESULT_TOP], 0: [ECHO]})
        self.assertFalse(ok)
        self.assertNotIn("double_click", ui.names())
        self.assertEqual(ui.tab, 1)

    def test_not_found_while_already_on_the_all_tab_gives_up_without_switching(self):
        ui = FakeUi(tab=0)
        ok, _ = run(ui, [ECHO])
        self.assertFalse(ok)
        self.assertNotIn("select_tab", ui.names())

    def test_unknown_current_tab_gives_up_without_switching(self):
        ui = FakeUi(tab=None)
        ok, _ = run(ui, [ECHO])
        self.assertFalse(ok)
        self.assertNotIn("select_tab", ui.names())

    def test_tab_switch_failure_gives_up_and_does_not_click(self):
        ui = FakeUi(tab=1, can_select=False)
        ok, logs = run(ui, {0: [ECHO, RESULT_TOP]})
        self.assertFalse(ok)
        self.assertNotIn("double_click", ui.names())
        self.assertEqual(ui.names().count("select_tab"), 2)                               # 切換 + 還原：點擊可能已經切過去、只是驗證失敗，所以一定要切回去
        self.assertTrue(any("全部" in l for l in logs))

    def test_a_failed_restore_of_the_users_tab_is_logged(self):
        ui = FakeUi(tab=1)
        real = ui.select_tab

        def flaky(hwnd, index):                               # 切到「全部」成功，切回原本的分頁失敗
            ok = real(hwnd, index)
            return ok if index == 0 else False
        ui.select_tab = flaky
        ok, logs = run(ui, [ECHO, RESULT_TOP])
        self.assertTrue(ok)                                   # 開窗本身成功
        self.assertTrue(any("切回原本" in l for l in logs))

    def test_a_tab_switch_that_moved_the_tab_but_failed_verification_is_still_undone(self):
        ui = FakeUi(tab=1, can_select=False, moves_even_when_failing=True)
        ok, _ = run(ui, {0: [ECHO, RESULT_TOP]})
        self.assertFalse(ok)
        self.assertEqual(ui.tab, 1)                                                       # 使用者原本的分頁被還原
        self.assertEqual(ui.events[-1], ("select_tab", 1))

    def test_not_found_in_the_all_tab_either_still_restores_the_tab(self):
        ui = FakeUi(tab=1)
        ok, _ = run(ui, [ECHO])
        self.assertFalse(ok)
        self.assertNotIn("double_click", ui.names())
        self.assertEqual(ui.events[-4:], [("clear_search",), ("bring_to_front", MAIN), ("ensure_wide",), ("select_tab", 1)])

    def test_narrow_window_is_widened_only_for_the_tab_switch_and_shrunk_right_after(self):
        # 真機：寬版面下雙擊好友不會彈出獨立視窗，搜尋與雙擊一定要在原本的寬度做
        ui = FakeUi(narrow=True, tab=1)
        ok, _ = run(ui, {0: [ECHO, RESULT_TOP]})
        self.assertTrue(ok)
        names = ui.names()
        first = names.index("ensure_wide")
        self.assertEqual(names[first:first + 3], ["ensure_wide", "select_tab", "restore_rect"])    # 拉寬 → 切分頁 → 馬上縮回
        self.assertLess(names.index("restore_rect"), names.index("double_click"))                   # 雙擊時已經是原本的寬度
        self.assertEqual(names.count("ensure_wide"), names.count("restore_rect"))                    # 每次拉寬都有縮回
        self.assertEqual(ui.events[-1], ("restore_rect", (0, 0, 546, 700)))

    def test_the_window_is_shrunk_back_even_when_the_tab_switch_fails(self):
        ui = FakeUi(narrow=True, tab=1, can_select=False)
        ok, _ = run(ui, {0: [ECHO, RESULT_TOP]})
        self.assertFalse(ok)
        self.assertEqual(ui.names().count("ensure_wide"), ui.names().count("restore_rect"))
        self.assertNotIn("double_click", ui.names())

    def test_found_on_the_all_tab_never_resizes_the_window(self):
        ui = FakeUi(narrow=True, tab=0)
        ok, _ = run(ui, [ECHO, RESULT_TOP])
        self.assertTrue(ok)
        self.assertNotIn("ensure_wide", ui.names())
        self.assertNotIn("restore_rect", ui.names())

    def test_no_main_window(self):
        ui = FakeUi(main=None)
        ok, logs = run(ui, [])
        self.assertFalse(ok)
        self.assertEqual(ui.events, [])
        self.assertTrue(any("主視窗" in l for l in logs))

    def test_main_window_not_in_front_aborts_before_touching_anything(self):
        ui = FakeUi(front=False)
        ok, _ = run(ui, [RESULT_TOP])
        self.assertFalse(ok)
        for forbidden in ("screenshot", "select_tab", "set_search_text", "double_click", "ensure_wide"):
            self.assertNotIn(forbidden, ui.names())                                      # 前景不對：什麼都不做
        self.assertEqual(ui.names()[-1], "clear_search")

    def test_no_search_box(self):
        ui = FakeUi(search_box=None)
        ok, logs = run(ui, [RESULT_TOP])
        self.assertFalse(ok)
        self.assertNotIn("double_click", ui.names())
        self.assertTrue(any("搜尋欄" in l for l in logs))

    def test_no_exact_match_means_no_click(self):
        ui = FakeUi(tab=0)
        ok, logs = run(ui, [("Notify Bot 通知群", (267, 225)), ("別人", (267, 300))])
        self.assertFalse(ok)
        self.assertNotIn("double_click", ui.names())                                     # 名稱必須完全相符，否則絕不點
        self.assertEqual(ui.names()[-1], "clear_search")

    def test_window_that_never_appears_is_a_failure_and_the_search_is_cleared(self):
        ui = FakeUi(window_appears=False, tab=0)
        ok, logs = run(ui, [RESULT_TOP])
        self.assertFalse(ok)
        self.assertIn("double_click", ui.names())
        self.assertEqual(ui.names()[-1], "clear_search")
        self.assertTrue(any("沒有出現" in l for l in logs))

    def test_focus_lost_while_searching_means_no_screenshot_and_no_click(self):
        ui = FakeUi()
        states = iter([True, False, False, False])                                       # 進場在前景，寫完搜尋字後被搶走

        ui.is_front = lambda hwnd: next(states)
        ok, _ = run(ui, [RESULT_TOP])
        self.assertFalse(ok)
        self.assertNotIn("screenshot", ui.names())
        self.assertNotIn("double_click", ui.names())

    def test_focus_lost_just_before_the_double_click_means_no_click(self):
        ui = FakeUi()
        states = iter([True, True, False, False])                                        # 截完結果的圖，雙擊前被搶走

        ui.is_front = lambda hwnd: next(states)
        ok, _ = run(ui, [RESULT_TOP])
        self.assertFalse(ok)
        self.assertEqual(ui.names().count("screenshot"), 1)
        self.assertNotIn("double_click", ui.names())                                      # 雙擊會點到別的視窗：絕不點

    def test_focus_lost_after_switching_to_all_means_no_click(self):
        ui = FakeUi(tab=1)
        states = iter([True, False, False, False])                                       # 切到「全部」並寫完搜尋字後被搶走

        ui.is_front = lambda hwnd: next(states)
        ok, _ = run(ui, {0: [ECHO, RESULT_TOP]})
        self.assertFalse(ok)
        self.assertNotIn("double_click", ui.names())

    def test_cleanup_errors_do_not_hide_the_result(self):
        ui = FakeUi(narrow=True, tab=1)

        def boom(*args):
            raise RuntimeError("uia gone")

        ui.clear_search = boom
        ui.restore_rect = boom
        ok, _ = run(ui, {0: [ECHO, RESULT_TOP]})
        self.assertTrue(ok)


class TestLineMainUi(unittest.TestCase):
    def fake_uia(self, exists=True, rect=(100, 20, 300, 60)):
        pattern = mock.Mock()
        edit = mock.Mock()
        edit.Exists.return_value = exists
        edit.BoundingRectangle = types.SimpleNamespace(left=rect[0], top=rect[1], right=rect[2], bottom=rect[3])
        edit.GetValuePattern.return_value = pattern
        window = mock.Mock()
        window.EditControl.return_value = edit
        module = types.SimpleNamespace(ControlFromHandle=mock.Mock(return_value=window))
        return module, edit, pattern

    def test_search_box_and_text_go_through_uia_value_pattern(self):
        module, edit, pattern = self.fake_uia()
        ui = auto_reply.LineMainUi()
        ui._main = MAIN
        with mock.patch.dict(sys.modules, {"uiautomation": module}):
            self.assertEqual(ui.search_box_rect(MAIN), (100, 20, 300, 60))
            ui.set_search_text("Notify Bot")
            ui.clear_search()
        self.assertEqual([c.args for c in pattern.SetValue.call_args_list], [("Notify Bot",), ("",)])
        module.ControlFromHandle.return_value.EditControl.assert_called_with(ClassName="LcTextField")   # 只用主視窗的搜尋欄

    def test_missing_search_box(self):
        module, edit, pattern = self.fake_uia(exists=False)
        ui = auto_reply.LineMainUi()
        ui._main = MAIN
        with mock.patch.dict(sys.modules, {"uiautomation": module}):
            self.assertIsNone(ui.search_box_rect(MAIN))
            ui.clear_search()                                                            # 沒有搜尋欄：安靜略過
        pattern.SetValue.assert_not_called()

    def test_screenshot_returns_window_origin(self):
        ui = auto_reply.LineMainUi()
        with mock.patch.object(auto_reply, "get_window_true_rect", return_value=(10, 20, 110, 220)), \
             mock.patch.object(auto_reply.ImageGrab, "grab", return_value="IMG") as grab:
            self.assertEqual(ui.screenshot(MAIN), ("IMG", (10, 20)))
            grab.assert_called_once_with(bbox=(10, 20, 110, 220), all_screens=True)

    def test_bring_to_front_restores_a_minimized_window(self):
        ui = auto_reply.LineMainUi()
        with mock.patch.object(auto_reply.win32gui, "IsIconic", return_value=True), \
             mock.patch.object(auto_reply.win32gui, "ShowWindow") as show, \
             mock.patch.object(auto_reply, "activate_window") as activate:
            ui.bring_to_front(MAIN)
        show.assert_called_once_with(MAIN, auto_reply.win32con.SW_RESTORE)
        activate.assert_called_once_with(MAIN)

    def test_clicks_use_pyautogui(self):
        with mock.patch.object(auto_reply.pyautogui, "doubleClick") as dbl:
            auto_reply.LineMainUi().double_click(5, 6)
        dbl.assert_called_once_with(5, 6)


class TestLineMainUiTabs(unittest.TestCase):
    def rect(self, l, t, r, b):
        return types.SimpleNamespace(left=l, top=t, right=r, bottom=b)

    def make(self, selected=1, view=(100, 10, 700, 70), tabs=((100, 10, 150, 70), (170, 10, 220, 70))):
        ui = auto_reply.LineMainUi()
        bar = mock.Mock()
        bar.BoundingRectangle = self.rect(*view)
        buttons = [mock.Mock(BoundingRectangle=self.rect(*t)) for t in tabs]
        ui._tab_buttons = lambda hwnd: (bar, buttons)
        marks = {}
        ui.selected_tab = lambda hwnd: marks.get("sel", selected)
        ui.is_front = lambda hwnd: True
        return ui, marks

    def test_select_tab_clicks_the_middle_and_verifies(self):
        ui, marks = self.make(selected=1)
        with mock.patch.object(auto_reply.win32gui, "GetWindowRect", return_value=(0, 0, 1000, 800)), \
             mock.patch.object(auto_reply.pyautogui, "click", side_effect=lambda x, y: marks.update(sel=0)) as click, \
             mock.patch.object(auto_reply.time, "sleep"):
            self.assertTrue(ui.select_tab(MAIN, 0))
        click.assert_called_once_with(125, 40)

    def test_select_tab_reports_failure_when_the_marker_does_not_move(self):
        ui, _ = self.make(selected=1)
        with mock.patch.object(auto_reply.win32gui, "GetWindowRect", return_value=(0, 0, 1000, 800)), \
             mock.patch.object(auto_reply.pyautogui, "click"), mock.patch.object(auto_reply.time, "sleep"):
            self.assertFalse(ui.select_tab(MAIN, 0))

    def test_select_tab_never_clicks_a_scrolled_away_tab_or_outside_the_window_or_without_focus(self):
        with mock.patch.object(auto_reply.win32gui, "GetWindowRect", return_value=(0, 0, 1000, 800)), \
             mock.patch.object(auto_reply.pyautogui, "click") as click, mock.patch.object(auto_reply.time, "sleep"):
            ui, _ = self.make(tabs=((-143, 10, -101, 70), (170, 10, 220, 70)))             # 捲到看不見
            self.assertFalse(ui.select_tab(MAIN, 0))
            ui, _ = self.make(view=(100, 10, 5000, 70), tabs=((1100, 10, 1150, 70),))      # 落在視窗外
            self.assertFalse(ui.select_tab(MAIN, 0))
            ui, _ = self.make()
            ui.is_front = lambda hwnd: False
            self.assertFalse(ui.select_tab(MAIN, 0))
            ui, _ = self.make()
            self.assertFalse(ui.select_tab(MAIN, 9))                                       # 沒有這個分頁
        click.assert_not_called()

    def test_select_tab_clicks_a_tab_whose_edge_is_slightly_clipped(self):
        # 真機：分頁列捲動了 5px，第一個分頁左緣在可視區外一點點，但中心點看得到，照樣可以點
        ui, marks = self.make(selected=1, view=(105, 10, 700, 70), tabs=((100, 10, 150, 70), (170, 10, 220, 70)))
        with mock.patch.object(auto_reply.win32gui, "GetWindowRect", return_value=(0, 0, 1000, 800)),              mock.patch.object(auto_reply.pyautogui, "click", side_effect=lambda x, y: marks.update(sel=0)) as click,              mock.patch.object(auto_reply.time, "sleep"):
            self.assertTrue(ui.select_tab(MAIN, 0))
        click.assert_called_once_with(125, 40)

    def test_select_tab_never_drags(self):
        with mock.patch.object(auto_reply.pyautogui, "mouseDown") as down, mock.patch.object(auto_reply.pyautogui, "moveTo") as move:
            ui, _ = self.make(tabs=((-143, 10, -101, 70),))
            ui.select_tab(MAIN, 0)
        down.assert_not_called()
        move.assert_not_called()                                                           # 分頁列在標題列上，拖曳會拖動整個視窗

    def test_ensure_wide_only_widens_a_narrow_window_and_keeps_position_and_height(self):
        ui = auto_reply.LineMainUi()
        with mock.patch.object(auto_reply.win32gui, "GetWindowRect", return_value=(1368, 50, 1914, 1124)), \
             mock.patch.object(auto_reply.win32gui, "MoveWindow") as move, mock.patch.object(auto_reply.time, "sleep"):
            self.assertEqual(ui.ensure_wide(MAIN), (1368, 50, 1914, 1124))
            move.assert_called_once_with(MAIN, 1368, 50, ui.MIN_MAIN_WIDTH, 1074, True)
            ui.restore_rect(MAIN, (1368, 50, 1914, 1124))
            move.assert_called_with(MAIN, 1368, 50, 546, 1074, True)
        with mock.patch.object(auto_reply.win32gui, "GetWindowRect", return_value=(0, 0, 1400, 800)), \
             mock.patch.object(auto_reply.win32gui, "MoveWindow") as move:
            self.assertIsNone(ui.ensure_wide(MAIN))
            move.assert_not_called()


class TestOpenMissingChatWindow(unittest.TestCase):
    def test_respects_the_config_switch(self):
        with mock.patch.object(auto_reply.chat_opener, "open_chat_window", return_value=True) as opener, \
             mock.patch.object(auto_reply, "log_v"):
            self.assertFalse(auto_reply.open_missing_chat_window("小明", {"auto_open_chat_window": False}))
            opener.assert_not_called()
            self.assertTrue(auto_reply.open_missing_chat_window("小明", {}))                # 預設開啟
            self.assertEqual(opener.call_args.args[0], "小明")
            self.assertIsInstance(opener.call_args.args[1], auto_reply.LineMainUi)


if __name__ == "__main__":
    unittest.main()
