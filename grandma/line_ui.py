"""操作 Windows 電腦版 LINE：打開聊天室、截取最新圖片、貼圖送出、關閉聊天室。

只能在 Windows 上執行。介面元素的位置靠 templates/ 裡的截圖比對。
"""
import ctypes
import io
import logging
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import pyautogui
import win32clipboard
import win32con
import win32gui
from PIL import Image, ImageGrab

from grandma.chat_image import find_latest_image

log = logging.getLogger(__name__)

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # 避免螢幕縮放讓截圖和點擊座標對不上
except OSError:
    pass

pyautogui.FAILSAFE = True  # 滑鼠甩到螢幕左上角可以緊急中止
pyautogui.PAUSE = 0.3


class UIError(Exception):
    pass


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def user_idle_seconds() -> float:
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
    return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000


def set_clipboard_text(text):
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()


def set_clipboard_image(path):
    with Image.open(path) as im:
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "BMP")
    data = buf.getvalue()[14:]  # CF_DIB 不含 BMP 檔頭
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_DIB, data)
    finally:
        win32clipboard.CloseClipboard()


class LineUI:
    def __init__(self, templates_dir, debug_dir, cfg):
        self.templates_dir = Path(templates_dir)
        self.debug_dir = Path(debug_dir)
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        self.cfg = cfg
        self.chat_hwnd = None
        self.chat_rect = None
        self.input_pos = None
        self.prev_hwnd = None

    # ---- 視窗與截圖 ----

    def _find_window(self, title):
        found = []

        def callback(hwnd, _):
            if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == title:
                found.append(hwnd)

        win32gui.EnumWindows(callback, None)
        return found[0] if found else None

    def _main_window(self):
        hwnd = self._find_window(self.cfg["main_window_title"])
        if not hwnd:
            raise UIError("找不到 LINE 主視窗，請確認電腦版 LINE 已開啟並登入")
        return hwnd

    def _activate(self, hwnd):
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        pyautogui.press("alt")  # 讓 Windows 允許切換前景視窗
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.6)

    def _screenshot(self, rect):
        return ImageGrab.grab(bbox=rect, all_screens=True)

    def _locate(self, name, rect):
        """在 rect 範圍內找模板 templates/<name>.png，回傳螢幕座標中心點或 None。"""
        path = self.templates_dir / f"{name}.png"
        if not path.exists():
            raise UIError(f"缺少模板 {path}，請參考 templates/README.md 截圖")
        tpl = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)  # 支援中文路徑
        shot = cv2.cvtColor(np.array(self._screenshot(rect)), cv2.COLOR_RGB2BGR)
        result = cv2.matchTemplate(shot, tpl, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(result)
        if score < self.cfg["match_threshold"]:
            return None
        return rect[0] + loc[0] + tpl.shape[1] // 2, rect[1] + loc[1] + tpl.shape[0] // 2

    def _require(self, name, rect):
        pos = self._locate(name, rect)
        if pos is None:
            self.save_debug(f"missing_{name}")
            raise UIError(f"畫面上找不到 {name}，截圖已存到 {self.debug_dir}")
        return pos

    def save_debug(self, tag):
        path = self.debug_dir / f"{datetime.now():%Y%m%d_%H%M%S}_{tag}.png"
        ImageGrab.grab(all_screens=True).save(path)
        return path

    # ---- 聊天室操作 ----

    def open_chat(self, name):
        self.prev_hwnd = win32gui.GetForegroundWindow()
        main = self._main_window()
        self._activate(main)

        x, y = self._require("search_box", win32gui.GetWindowRect(main))
        pyautogui.click(x, y)
        pyautogui.hotkey("ctrl", "a")
        set_clipboard_text(name)  # 用貼上的方式輸入，避開中文輸入法
        pyautogui.hotkey("ctrl", "v")
        time.sleep(1.5)
        for key in self.cfg["open_chat_keys"]:
            pyautogui.press(key)
            time.sleep(0.5)
        time.sleep(2)

        # 聊天室可能開成獨立視窗（標題是好友名稱），也可能開在主視窗右側
        self.chat_hwnd = self._find_window(name)
        target = self.chat_hwnd or main
        if self.chat_hwnd:
            self._activate(self.chat_hwnd)
        self.chat_rect = win32gui.GetWindowRect(target)
        self.input_pos = self._require("input_box", self.chat_rect)
        log.info("已打開聊天室（%s）", "獨立視窗" if self.chat_hwnd else "主視窗")

    def capture_latest_image(self):
        """截取對方最新傳來的圖片，找不到回傳 None。"""
        left, top, _, _ = self.chat_rect
        shot = np.array(self._screenshot(self.chat_rect).convert("RGB"))
        area_top = self.cfg["chat_header_px"]
        area_bottom = self.input_pos[1] - top - self.cfg["input_margin_px"]
        area_left = self.cfg["chat_left_px"]
        area = shot[area_top:area_bottom, area_left:]

        box = find_latest_image(area)
        if box is None:
            self.save_debug("no_image")
            return None
        x, y, w, h = box
        return Image.fromarray(area[y : y + h, x : x + w])

    def send_image(self, path):
        set_clipboard_image(path)
        pyautogui.click(*self.input_pos)
        time.sleep(0.3)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(1.5)
        for key in self.cfg["send_keys"]:
            pyautogui.press(key)
            time.sleep(0.8)
        time.sleep(1.5)

    def close_chat(self):
        """關掉聊天室，之後對方再傳訊息才不會被已讀。"""
        try:
            if self.chat_hwnd and win32gui.IsWindow(self.chat_hwnd):
                win32gui.PostMessage(self.chat_hwnd, win32con.WM_CLOSE, 0, 0)
                time.sleep(0.5)
            else:
                for key in self.cfg["close_keys"]:
                    pyautogui.press(key)
                    time.sleep(0.3)
            if self.cfg["minimize_after"]:
                main = self._find_window(self.cfg["main_window_title"])
                if main:
                    win32gui.ShowWindow(main, win32con.SW_MINIMIZE)
        finally:
            self.chat_hwnd = None
            if self.prev_hwnd and win32gui.IsWindow(self.prev_hwnd):
                try:
                    win32gui.SetForegroundWindow(self.prev_hwnd)
                except Exception:
                    pass
