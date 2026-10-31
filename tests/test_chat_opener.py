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
                 tab=1, can_select=True, narrow=False):
        self.main = main
        self.front = front
        self.search_box = search_box
        self.window_appears = window_appears
        self.windows_open = windows_open
        self.tab = tab
        self.can_select = can_select
        self.narrow = narrow
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
        if self.can_select:
            self.tab = index
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
                 ("Notify Bot(3)", (267, 340)),                   # 群組帶人數：不算
                 ("Notify", (267, 360))]
        self.assertEqual(chat_opener.find_result_rows(boxes, "Notify Bot", min_y=200), [(267, 225), (267, 396)])

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

    def test_happy_path_in_the_current_tab_never_switches_tabs(self):
        ui = FakeUi()
        ok, _ = run(ui, [ECHO, RESULT_LOW, RESULT_TOP, ("別人", (200, 300))])
        self.assertTrue(ok)
        self.assertEqual(ui.events, [
            ("bring_to_front", MAIN), ("ensure_wide",),
            ("set_search_text", "Notify Bot"), ("screenshot",),
            ("double_click", 100 + 267, 50 + 225),                                       # 最上面那一列；搜尋欄裡的回聲文字不算
            ("clear_search",)])                                                          # 最後一定清掉搜尋欄

    def test_the_echo_of_the_search_text_is_never_double_clicked(self):
        # 真機實測的錯誤：搜尋欄自己的字被當成結果，雙擊到搜尋欄
        ui = FakeUi(tab=0)
        ok, logs = run(ui, [ECHO])
        self.assertFalse(ok)
        self.assertNotIn("double_click", ui.names())
        self.assertTrue(any("完全相符" in l for l in logs))

    def test_not_found_in_the_current_tab_switches_to_all_then_restores_the_users_tab(self):
        ui = FakeUi(tab=1)
        ok, logs = run(ui, {1: [ECHO], 0: [ECHO, RESULT_TOP]})
        self.assertTrue(ok)
        self.assertEqual(ui.events, [
            ("bring_to_front", MAIN), ("ensure_wide",),
            ("set_search_text", "Notify Bot"), ("screenshot",),
            ("select_tab", 0),
            ("set_search_text", ""), ("set_search_text", "Notify Bot"), ("screenshot",),
            ("double_click", 100 + 267, 50 + 225),
            ("clear_search",), ("bring_to_front", MAIN), ("select_tab", 1)])             # 使用者原本的分頁要切回去 (先把主視窗帶回前景)
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
        ok, logs = run(ui, {1: [ECHO], 0: [ECHO, RESULT_TOP]})
        self.assertFalse(ok)
        self.assertNotIn("double_click", ui.names())
        self.assertEqual(ui.names().count("select_tab"), 1)                               # 沒切成功就沒有要還原的東西
        self.assertTrue(any("全部" in l for l in logs))

    def test_not_found_in_the_all_tab_either_still_restores_the_tab(self):
        ui = FakeUi(tab=1)
        ok, _ = run(ui, [ECHO])
        self.assertFalse(ok)
        self.assertNotIn("double_click", ui.names())
        self.assertEqual(ui.events[-3:], [("clear_search",), ("bring_to_front", MAIN), ("select_tab", 1)])

    def test_narrow_window_is_widened_and_always_restored(self):
        ui = FakeUi(narrow=True)
        ok, _ = run(ui, [ECHO, RESULT_TOP])
        self.assertTrue(ok)
        self.assertEqual(ui.events[-1], ("restore_rect", (0, 0, 546, 700)))
        ui = FakeUi(narrow=True)
        ok, _ = run(ui, [ECHO])                                                          # 失敗也要縮回去
        self.assertFalse(ok)
        self.assertEqual(ui.events[-1], ("restore_rect", (0, 0, 546, 700)))

    def test_wide_window_is_not_restored(self):
        ui = FakeUi(narrow=False)
        run(ui, [ECHO, RESULT_TOP])
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
        ui = FakeUi(window_appears=False)
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
        states = iter([True, True, True, False, False, False])                           # 切到「全部」並重新搜尋後被搶走

        ui.is_front = lambda hwnd: next(states)
        ok, _ = run(ui, {1: [ECHO], 0: [ECHO, RESULT_TOP]})
        self.assertFalse(ok)
        self.assertNotIn("double_click", ui.names())

    def test_cleanup_errors_do_not_hide_the_result(self):
        ui = FakeUi(narrow=True, tab=1)

        def boom(*args):
            raise RuntimeError("uia gone")

        ui.clear_search = boom
        ui.restore_rect = boom
        ok, _ = run(ui, {1: [ECHO], 0: [ECHO, RESULT_TOP]})
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
        with mock.patch.object(auto_reply.win32gui, "GetWindowRect", return_value=(0, 0, 1200, 800)), \
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
