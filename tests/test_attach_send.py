import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_reply

CHAT, DIALOG = 77, 88


class _Rect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class _Ctrl:
    """假的 UIA 控制項：只有測試會用到的屬性"""
    def __init__(self, class_name="", rect=(0, 0, 0, 0), children=(), exists=True):
        self.ClassName = class_name
        self.BoundingRectangle = _Rect(*rect)
        self._children = list(children)
        self._exists = exists

    def GetChildren(self):
        return list(self._children)

    def Exists(self, *args):
        return self._exists


def _fake_uia_for_input_panel(button_rects, panel_exists=True):
    buttons = [_Ctrl("LcButton", r) for r in button_rects]
    panel = _Ctrl("MessageInputPanel", children=[_Ctrl("LcWidget", children=buttons)], exists=panel_exists)
    window = types.SimpleNamespace(GroupControl=lambda **kw: panel)
    return types.SimpleNamespace(ControlFromHandle=lambda hwnd: window)


class _FakeValuePattern:
    def __init__(self, on_set):
        self.Value = ""
        self._on_set = on_set

    def SetValue(self, text):
        self.Value = self._on_set(text)


def _fake_uia_for_dialog(on_set=lambda text: text, edit_exists=True):
    edit = _Ctrl("Edit", exists=edit_exists)
    pattern = _FakeValuePattern(on_set)
    edit.GetValuePattern = lambda: pattern
    dialog = types.SimpleNamespace(EditControl=lambda **kw: edit)
    return types.SimpleNamespace(ControlFromHandle=lambda hwnd: dialog), pattern


class TestAttachSend(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _make_image(self, name="send_me.jpg"):
        from PIL import Image
        path = os.path.join(self.temp_dir.name, name)
        Image.new("RGB", (400, 400), (9, 9, 9)).save(path)
        return path

    # ---------- 找附加檔案按鈕 ----------
    def test_attach_button_is_the_leftmost_input_panel_button(self):
        rects = [(1863, 1077, 1899, 1113), (66, 1077, 102, 1113), (21, 1077, 57, 1113), (147, 1077, 157, 1113)]
        with mock.patch.dict(sys.modules, {"uiautomation": _fake_uia_for_input_panel(rects)}):
            self.assertEqual(auto_reply._find_attach_button_center(CHAT), (39, 1095))   # 與順序無關，取最左邊那顆的中心

    def test_attach_button_none_when_missing_or_uia_fails(self):
        with mock.patch.dict(sys.modules, {"uiautomation": _fake_uia_for_input_panel([])}):
            self.assertIsNone(auto_reply._find_attach_button_center(CHAT))              # 輸入區沒有按鈕
        with mock.patch.dict(sys.modules, {"uiautomation": _fake_uia_for_input_panel([(1, 1, 9, 9)], panel_exists=False)}):
            self.assertIsNone(auto_reply._find_attach_button_center(CHAT))              # 找不到輸入區
        broken = types.SimpleNamespace(ControlFromHandle=mock.Mock(side_effect=RuntimeError("uia down")))
        with mock.patch.dict(sys.modules, {"uiautomation": broken}):
            self.assertIsNone(auto_reply._find_attach_button_center(CHAT))              # UIA 本身出錯

    # ---------- 找檔案對話框 ----------
    def _find_dialog(self, windows, line_pids=frozenset({10}), timeout=0.0):
        """windows: {hwnd: (類別, 可見, pid)}"""
        def enum(cb, extra):
            for hwnd in windows:
                cb(hwnd, extra)

        with mock.patch.object(auto_reply, "get_line_pids", return_value=set(line_pids)), \
             mock.patch.object(auto_reply.win32gui, "EnumWindows", side_effect=enum), \
             mock.patch.object(auto_reply.win32gui, "GetClassName", side_effect=lambda h: windows[h][0]), \
             mock.patch.object(auto_reply.win32gui, "IsWindowVisible", side_effect=lambda h: windows[h][1]), \
             mock.patch.object(auto_reply.win32process, "GetWindowThreadProcessId", side_effect=lambda h: (0, windows[h][2])), \
             mock.patch.object(auto_reply.time, "sleep"):
            return auto_reply._find_open_dialog(timeout)

    def test_find_open_dialog_only_accepts_visible_dialog_of_line(self):
        self.assertEqual(self._find_dialog({1: ("Qt663QWindowIcon", True, 10), 2: ("#32770", True, 10)}), 2)
        self.assertIsNone(self._find_dialog({1: ("#32770", True, 999)}))                # 別的程式的對話框
        self.assertIsNone(self._find_dialog({1: ("#32770", False, 10)}))                # 看不見的
        self.assertIsNone(self._find_dialog({1: ("Qt663QWindowIcon", True, 10)}))       # 不是標準對話框

    def test_find_open_dialog_waits_until_it_appears(self):
        state = {"calls": 0}
        windows = {2: ("#32770", True, 10)}

        def enum(cb, extra):
            state["calls"] += 1
            if state["calls"] >= 3:                                                     # 第三次掃描才出現
                for hwnd in windows:
                    cb(hwnd, extra)

        with mock.patch.object(auto_reply, "get_line_pids", return_value={10}), \
             mock.patch.object(auto_reply.win32gui, "EnumWindows", side_effect=enum), \
             mock.patch.object(auto_reply.win32gui, "GetClassName", return_value="#32770"), \
             mock.patch.object(auto_reply.win32gui, "IsWindowVisible", return_value=True), \
             mock.patch.object(auto_reply.win32process, "GetWindowThreadProcessId", return_value=(0, 10)), \
             mock.patch.object(auto_reply.time, "sleep"):
            self.assertEqual(auto_reply._find_open_dialog(timeout=60), 2)
        self.assertEqual(state["calls"], 3)

    # ---------- 填檔名 ----------
    def test_set_dialog_filename_verifies_the_value(self):
        path = r"C:\Users\yuyua\code\images\grandma_1.jpg"
        fake, pattern = _fake_uia_for_dialog()
        with mock.patch.dict(sys.modules, {"uiautomation": fake}), mock.patch.object(auto_reply.time, "sleep"):
            self.assertTrue(auto_reply._set_dialog_filename(DIALOG, path))
            self.assertEqual(pattern.Value, path)
        fake, _ = _fake_uia_for_dialog(on_set=lambda t: t.upper())                      # Windows 路徑不分大小寫
        with mock.patch.dict(sys.modules, {"uiautomation": fake}), mock.patch.object(auto_reply.time, "sleep"):
            self.assertTrue(auto_reply._set_dialog_filename(DIALOG, path))

    def test_set_dialog_filename_rejects_garbled_or_missing(self):
        path = r"C:\Users\yuyua\code\images\grandma_1.jpg"
        fake, _ = _fake_uia_for_dialog(on_set=lambda t: "C:\\U\\ㄅㄆㄇ")                  # 輸入法把路徑弄亂
        with mock.patch.dict(sys.modules, {"uiautomation": fake}), mock.patch.object(auto_reply.time, "sleep"):
            self.assertFalse(auto_reply._set_dialog_filename(DIALOG, path))
        fake, _ = _fake_uia_for_dialog(edit_exists=False)                               # 找不到檔名欄
        with mock.patch.dict(sys.modules, {"uiautomation": fake}), mock.patch.object(auto_reply.time, "sleep"):
            self.assertFalse(auto_reply._set_dialog_filename(DIALOG, path))
        fake, _ = _fake_uia_for_dialog(on_set=mock.Mock(side_effect=RuntimeError("no value pattern")))
        with mock.patch.dict(sys.modules, {"uiautomation": fake}), mock.patch.object(auto_reply.time, "sleep"):
            self.assertFalse(auto_reply._set_dialog_filename(DIALOG, path))

    # ---------- 整個送出步驟 ----------
    def _send(self, *, image=True, button=(39, 1095), dialog=DIALOG, set_ok=True, fg=(CHAT, DIALOG),
              dialog_closes=True):
        path = self._make_image() if image else os.path.join(self.temp_dir.name, "missing.jpg")
        state = {"open": True}
        events = []

        def is_window(h):
            return state["open"]

        def sleep(_):
            if dialog_closes and "enter" in events:
                state["open"] = False

        with mock.patch.object(auto_reply, "_find_attach_button_center", return_value=button), \
             mock.patch.object(auto_reply, "_find_open_dialog", return_value=dialog), \
             mock.patch.object(auto_reply, "_set_dialog_filename", return_value=set_ok), \
             mock.patch.object(auto_reply, "_close_dialog", side_effect=lambda h: events.append("close")) as close, \
             mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", side_effect=list(fg)), \
             mock.patch.object(auto_reply.win32gui, "IsWindow", side_effect=is_window), \
             mock.patch.object(auto_reply.win32gui, "IsWindowVisible", side_effect=is_window), \
             mock.patch.object(auto_reply.pyautogui, "click", side_effect=lambda x, y: events.append(("click", x, y))), \
             mock.patch.object(auto_reply.pyautogui, "press", side_effect=lambda k: events.append(k)), \
             mock.patch.object(auto_reply, "ATTACH_DIALOG_CLOSE_TIMEOUT", 0.0 if not dialog_closes else 5.0), \
             mock.patch.object(auto_reply.time, "sleep", side_effect=sleep), \
             mock.patch.object(auto_reply, "log_v"):
            result = auto_reply.send_image_via_attach_dialog(CHAT, path)
        return result, events

    def test_attach_send_happy_path(self):
        result, events = self._send()
        self.assertTrue(result)
        self.assertEqual(events, [("click", 39, 1095), "enter"])                         # 點按鈕、按一次 Enter，沒有取消對話框

    def test_attach_send_stops_before_clicking_when_unsure(self):
        result, events = self._send(image=False)
        self.assertEqual((result, events), (False, []))                                  # 圖片不存在
        result, events = self._send(button=None)
        self.assertEqual((result, events), (False, []))                                  # 找不到附加檔案按鈕
        result, events = self._send(fg=(5,))
        self.assertEqual((result, events), (False, []))                                  # 前景不是聊天室：不點

    def test_attach_send_cancels_the_dialog_when_anything_is_off(self):
        result, events = self._send(dialog=None)
        self.assertEqual((result, events), (False, [("click", 39, 1095)]))               # 點了但沒有對話框：沒東西可關
        result, events = self._send(set_ok=False)
        self.assertEqual((result, events), (False, [("click", 39, 1095), "close"]))      # 檔名填不對：關掉，不按 Enter
        result, events = self._send(fg=(CHAT, 5))
        self.assertEqual((result, events), (False, [("click", 39, 1095), "close"]))      # 對話框不在前景：不按 Enter
        result, events = self._send(dialog_closes=False)
        self.assertEqual((result, events), (False, [("click", 39, 1095), "enter", "close"]))   # Enter 後對話框沒關：取消，不當成已送出

    # ---------- send_grandma_image 不再貼上圖片 ----------
    def _run_send_flow(self, attach_result=True, verified=True):
        img = self._make_image("flow.jpg")
        calls = {"records": [], "minimized": []}
        patches = [
            mock.patch.object(auto_reply, "find_target_chat_window", return_value=[(CHAT, "小明", "Qt", True)]),
            mock.patch.object(auto_reply, "harvest_line_incoming_images"),
            mock.patch.object(auto_reply, "get_images", return_value=[img]),
            mock.patch.object(auto_reply, "choose_reply_image", return_value=img),
            mock.patch.object(auto_reply, "REPLY_COUNTS_PATH", os.path.join(self.temp_dir.name, "reply_counts.json")),
            mock.patch.object(auto_reply, "countdown_sleep"),
            mock.patch.object(auto_reply.win32gui, "IsIconic", return_value=False),
            mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=CHAT),
            mock.patch.object(auto_reply.win32gui, "GetWindowText", return_value="小明"),
            mock.patch.object(auto_reply, "activate_window"),
            mock.patch.object(auto_reply, "focus_chat_input", return_value=True),
            mock.patch.object(auto_reply, "is_last_message_from_self_in_chat", return_value=False),
            mock.patch.object(auto_reply, "_media_type_with_retry", return_value=True),
            mock.patch.object(auto_reply, "get_active_line_cache_dir", return_value="cache"),
            mock.patch.object(auto_reply, "get_all_cache_files", return_value={}),
            mock.patch.object(auto_reply, "load_chat_dirs_map", return_value={}),
            mock.patch.object(auto_reply, "verify_message_sent_to_chat", return_value=verified),
            mock.patch.object(auto_reply, "record_reply_sent", side_effect=lambda t: calls["records"].append(("reply", t))),
            mock.patch.object(auto_reply, "record_contact_sent", side_effect=lambda t, m, max_rounds: calls["records"].append(("sent", t))),
            mock.patch.object(auto_reply, "retire_image", side_effect=lambda p, images_dir: calls["records"].append(("retire", p))),
            mock.patch.object(auto_reply, "_minimize_if_configured", side_effect=lambda h, c, log_target=None: calls["minimized"].append(h)),
            mock.patch.object(auto_reply.notifier, "notify"),
            mock.patch.object(auto_reply.time, "sleep"),
            mock.patch.object(auto_reply, "log_v"),
        ]
        attach = mock.patch.object(auto_reply, "send_image_via_attach_dialog",
                                   side_effect=attach_result if isinstance(attach_result, Exception) else None,
                                   return_value=None if isinstance(attach_result, Exception) else attach_result)
        hotkey = mock.patch.object(auto_reply.pyautogui, "hotkey")
        for p in patches:
            p.start()
        attach_mock = attach.start()
        hotkey_mock = hotkey.start()
        try:
            result = auto_reply.send_grandma_image("小明", {"require_grandma_image": False, "time_aware_images": False,
                                                          "min_delay_seconds": 1, "max_delay_seconds": 1})
        finally:
            hotkey.stop()
            attach.stop()
            for p in reversed(patches):
                p.stop()
        return result, attach_mock, hotkey_mock, calls, img

    def test_send_flow_sends_through_attach_dialog_and_never_pastes(self):
        result, attach, hotkey, calls, img = self._run_send_flow()
        self.assertEqual(result, "SENT_CONFIRMED")
        attach.assert_called_once_with(CHAT, img)
        hotkey.assert_not_called()                                                       # 不再 Ctrl+V 貼上
        self.assertEqual([r[0] for r in calls["records"]], ["reply", "sent", "retire"])   # 記帳、百輪記憶、用過淘汰
        result, *_ = self._run_send_flow(verified=False)
        self.assertEqual(result, "SENT_UNCERTAIN")                                       # 氣泡暫時看不出來：記為已送出，不重送

    def test_send_flow_reports_not_sent_without_booking_when_attach_fails(self):
        result, attach, hotkey, calls, img = self._run_send_flow(attach_result=False)
        self.assertEqual(result, "NOT_SENT")
        self.assertEqual(calls["records"], [])                                           # 沒送出就不記帳、不淘汰圖片
        self.assertEqual(calls["minimized"], [CHAT])                                     # 視窗照設定縮回去
        result, attach, hotkey, calls, img = self._run_send_flow(attach_result=RuntimeError("boom"))
        self.assertEqual(result, "NOT_SENT")                                             # 步驟中途出例外同樣視為沒送出
        self.assertEqual(calls["records"], [])


if __name__ == "__main__":
    unittest.main()
