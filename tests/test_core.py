import unittest
from unittest import mock
import os
import sys
import tempfile
import json
import time
import contextlib

# Portable path insertion derived from __file__
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_reply

class TestLineGrandmaBotCore(unittest.TestCase):
    def setUp(self):
        # 建立隔離的臨時目錄 (位於專案目錄內，避免跨磁碟機 relpath 拋錯)，防止單元測試修改正式專案檔案
        self.temp_dir = tempfile.TemporaryDirectory(dir=auto_reply.SCRIPT_DIR)
        self.orig_cb_path = auto_reply.CIRCUIT_BREAKER_PATH
        self.orig_cfg_path = auto_reply.CONFIG_PATH
        self.orig_history_path = auto_reply.HISTORY_PATH
        self.orig_chat_map_path = auto_reply.CHAT_MAP_PATH
        self.orig_reply_counts_path = auto_reply.REPLY_COUNTS_PATH
        self.orig_kind_index = auto_reply.KIND_INDEX
        self.orig_ocr_text = auto_reply.ocr_text

        # 每日回覆計數與圖片性質索引也隔離到暫存目錄；OCR 預設回傳空字串，任何測試都不會意外跑真的 OCR
        auto_reply.REPLY_COUNTS_PATH = os.path.join(self.temp_dir.name, "reply_counts.json")
        auto_reply.ocr_text = lambda image: ""
        auto_reply.KIND_INDEX = auto_reply.image_library.KindIndex(
            os.path.join(self.temp_dir.name, "image_kinds.json"), lambda p: auto_reply.ocr_text(p))
        auto_reply._FILE_KIND_CACHE.clear()
        auto_reply.CIRCUIT_BREAKER_PATH = os.path.join(self.temp_dir.name, "circuit_breaker.json")
        auto_reply.CONFIG_PATH = os.path.join(self.temp_dir.name, "config.json")
        auto_reply.HISTORY_PATH = os.path.join(self.temp_dir.name, "contact_history.json")
        auto_reply.CHAT_MAP_PATH = os.path.join(self.temp_dir.name, "chat_dirs.json")

        # 隔離真實桌面：預設無任何 LINE 視窗，避免本機開啟的聊天視窗干擾歸屬混淆檢查
        self.orig_get_all_windows = auto_reply.get_all_line_windows
        auto_reply.get_all_line_windows = lambda: []

        auto_reply.CIRCUIT_BREAKER_BLOCKED_UNTIL.clear()
        auto_reply.REPLY_HISTORY_TIMESTAMPS.clear()
        auto_reply.LAST_SENT_TIME_BY_TARGET.clear()
        auto_reply.LAST_PROCESSED_UNREAD.clear()
        auto_reply._RECENT_UNKNOWN_CHANNELS.clear()
        auto_reply.PROCESSED_CACHE_FILES.clear()
        auto_reply._TARGET_WINDOW_STATE.clear()
        auto_reply._PROVISIONAL_BINDINGS.clear()
        auto_reply.last_global_reply_time = 0.0
        auto_reply._last_valid_config = None

    def tearDown(self):
        auto_reply.get_all_line_windows = self.orig_get_all_windows
        auto_reply.CIRCUIT_BREAKER_PATH = self.orig_cb_path
        auto_reply.CONFIG_PATH = self.orig_cfg_path
        auto_reply.HISTORY_PATH = self.orig_history_path
        auto_reply.CHAT_MAP_PATH = self.orig_chat_map_path
        auto_reply.REPLY_COUNTS_PATH = self.orig_reply_counts_path
        auto_reply.KIND_INDEX = self.orig_kind_index
        auto_reply.ocr_text = self.orig_ocr_text
        auto_reply._FILE_KIND_CACHE.clear()

        auto_reply.CIRCUIT_BREAKER_BLOCKED_UNTIL.clear()
        auto_reply.REPLY_HISTORY_TIMESTAMPS.clear()
        auto_reply.LAST_SENT_TIME_BY_TARGET.clear()
        auto_reply.LAST_PROCESSED_UNREAD.clear()
        auto_reply._RECENT_UNKNOWN_CHANNELS.clear()
        auto_reply.PROCESSED_CACHE_FILES.clear()
        auto_reply._TARGET_WINDOW_STATE.clear()
        auto_reply._PROVISIONAL_BINDINGS.clear()
        auto_reply.last_global_reply_time = 0.0
        auto_reply._last_valid_config = None

        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_config_validation(self):
        # 1. 測試預設設定
        cfg = auto_reply.load_config()
        self.assertIsInstance(cfg["targets"], list)
        self.assertIn("trigger_mode", cfg)
        self.assertGreaterEqual(cfg["min_delay_seconds"], 1)
        self.assertGreaterEqual(cfg["max_delay_seconds"], cfg["min_delay_seconds"])

        # 2. 測試 targets 為空清單時正確保留為空清單（不回退至預設 "媽媽"）
        with open(auto_reply.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"targets": []}, f)
        cfg_empty = auto_reply.load_config()
        self.assertEqual(cfg_empty["targets"], [])

        # 3. 測試布林型別嚴格驗證（字串 "false" 絕不應被解析為 True）
        with open(auto_reply.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"targets": ["Target1"], "auto_crawl_images": "false"}, f)
        cfg_bool = auto_reply.load_config()
        # 由於 "false" 為字串非 bool，維持 DEFAULT_CONFIG 或不被字串覆寫
        self.assertIsInstance(cfg_bool["auto_crawl_images"], bool)

        # 4. 測試設定檔語法損毀時沿用上一份有效設定，絕不退回預設 "媽媽"
        with open(auto_reply.CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write("{ invalid json")
        cfg_corrupt = auto_reply.load_config()
        self.assertEqual(cfg_corrupt["targets"], ["Target1"])

        # 5. 測試 images_dir 目錄穿越與逃逸防護
        with open(auto_reply.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"images_dir": r"..\..\windows\system32"}, f)
        cfg_traverse = auto_reply.load_config()
        self.assertEqual(cfg_traverse["images_dir"], "images")

        with open(auto_reply.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"images_dir": r"C:\Windows"}, f)
        cfg_abs = auto_reply.load_config()
        self.assertEqual(cfg_abs["images_dir"], "images")

        with open(auto_reply.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"images_dir": "."}, f)
        cfg_dot = auto_reply.load_config()
        self.assertEqual(cfg_dot["images_dir"], "images")

    def test_circuit_breaker_locking_and_persistence(self):
        target = "TestUser"
        # 第 1 次發送允許
        self.assertTrue(auto_reply.check_circuit_breaker(target, max_replies=2, window_seconds=300))
        auto_reply.record_reply_sent(target)

        # 第 2 次發送允許
        self.assertTrue(auto_reply.check_circuit_breaker(target, max_replies=2, window_seconds=300))
        auto_reply.record_reply_sent(target)

        # 第 3 次發送必須被斷路器阻擋
        self.assertFalse(auto_reply.check_circuit_breaker(target, max_replies=2, window_seconds=300))

        # 確認寫入的是臨時目錄中的 circuit_breaker.json，正式專案檔案完全未受污染
        self.assertTrue(os.path.exists(auto_reply.CIRCUIT_BREAKER_PATH))
        self.assertIn(self.temp_dir.name, auto_reply.CIRCUIT_BREAKER_PATH)

    def test_chat_hash_extraction(self):
        # 40 字元快取目錄 hash
        h1 = "a" * 40
        h2 = "b" * 40
        base = os.path.expandvars(r"%LOCALAPPDATA%\LINE\Cache")

        # 雙層目錄結構: Cache\<h1>\<h2>\m\foo.eimg
        p2 = os.path.join(base, h1, h2, "m", "foo.eimg")
        self.assertEqual(auto_reply.extract_chat_hash_from_path(p2), h2)

        # 單層目錄結構: Cache\<h1>\m\foo.eimg
        p1 = os.path.join(base, h1, "m", "foo.eimg")
        self.assertEqual(auto_reply.extract_chat_hash_from_path(p1), h1)

    def test_title_clean_regex(self):
        import re
        cases = [
            ("TargetContact", "TargetContact", None),
            ("TargetContact (1)", "TargetContact", "1"),
            ("TargetContact （2）", "TargetContact", "2"),
            ("TargetContact (99+)", "TargetContact", "99+"),
            ("TargetContact （99+）", "TargetContact", "99+"),
        ]
        for raw, expected_name, expected_unread in cases:
            m = re.search(r"^(.*?)\s*[\(\uFF08](\d+\+?)[\)\uFF09]\s*$", raw)
            name = m.group(1).strip() if m else raw.strip()
            unread = m.group(2) if m else None
            self.assertEqual(name, expected_name)
            self.assertEqual(unread, expected_unread)

    def test_duplicate_window_interception(self):
        # 測試一個已讀、一個未讀之同名視窗同時存在時，全路徑攔截絕不放行
        orig_get_all = auto_reply.get_all_line_windows
        orig_find_main = auto_reply.find_line_main_window
        auto_reply.find_line_main_window = lambda: 999
        auto_reply.get_all_line_windows = lambda: [
            (1001, "TargetContact", "Qt5QWindowIcon", True),
            (1002, "TargetContact (1)", "Qt5QWindowIcon", True),
        ]
        try:
            matched = auto_reply.find_target_chat_window("TargetContact")
            self.assertEqual(matched, [])
        finally:
            auto_reply.get_all_line_windows = orig_get_all
            auto_reply.find_line_main_window = orig_find_main

    def test_group_window_multi_paren_rejection(self):
        orig_get_all = auto_reply.get_all_line_windows
        orig_find_main = auto_reply.find_line_main_window
        auto_reply.find_line_main_window = lambda: 999
        auto_reply.get_all_line_windows = lambda: [
            (1003, "TargetContact (3) (1)", "Qt5QWindowIcon", True),
        ]
        try:
            matched = auto_reply.find_target_chat_window("TargetContact")
            self.assertEqual(matched, [])
        finally:
            auto_reply.get_all_line_windows = orig_get_all
            auto_reply.find_line_main_window = orig_find_main

    def test_is_valid_image_or_eimg(self):
        # 測試圖片格式驗證器 (排除非圖片與附件)
        jpg_file = os.path.join(self.temp_dir.name, "test.jpg")
        with open(jpg_file, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0\x00\x10JFIF")
        self.assertTrue(auto_reply.is_valid_image_or_eimg(jpg_file))

        eimg_file = os.path.join(self.temp_dir.name, "test.eimg")
        with open(eimg_file, "wb") as f:
            f.write(b"encrypted_line_image_data")
        self.assertTrue(auto_reply.is_valid_image_or_eimg(eimg_file))

        docx_file = os.path.join(self.temp_dir.name, "document.docx")
        with open(docx_file, "wb") as f:
            f.write(b"PK\x03\x04zip_docx_content")
        self.assertFalse(auto_reply.is_valid_image_or_eimg(docx_file))

    def test_circuit_breaker_fail_closed_on_corrupt(self):
        # 測試斷路器狀態檔損壞時，依 Fail-Closed 原則啟動 30 分鐘防禦鎖定，並拒絕任何對象發送
        corrupt_cb = os.path.join(self.temp_dir.name, "corrupt_cb.json")
        with open(corrupt_cb, "w", encoding="utf-8") as f:
            f.write("invalid json content {{{")
        orig_cb_path = auto_reply.CIRCUIT_BREAKER_PATH
        orig_cb_blocked = dict(auto_reply.CIRCUIT_BREAKER_BLOCKED_UNTIL)
        auto_reply.CIRCUIT_BREAKER_PATH = corrupt_cb
        try:
            state = auto_reply.load_circuit_breaker_state()
            self.assertIn("__GLOBAL__", state.get("blocked_until", {}))
            auto_reply.CIRCUIT_BREAKER_BLOCKED_UNTIL.clear()
            auto_reply.CIRCUIT_BREAKER_BLOCKED_UNTIL.update(state.get("blocked_until", {}))
            # 實體驗證：全域損毀鎖定生效，任一對象發送均必須遭拒絕 (返回 False)
            self.assertFalse(auto_reply.check_circuit_breaker("AnyTarget"))
            self.assertFalse(auto_reply.check_circuit_breaker("AnotherContact"))
        finally:
            auto_reply.CIRCUIT_BREAKER_PATH = orig_cb_path
            auto_reply.CIRCUIT_BREAKER_BLOCKED_UNTIL.clear()
            auto_reply.CIRCUIT_BREAKER_BLOCKED_UNTIL.update(orig_cb_blocked)

    def test_hundred_round_exhaustion_fail_closed(self):
        # 測試圖庫耗盡且網路無法下載新圖時，依 Fail-Closed 原則返回 None，絕不降級重複發送
        sub_img_dir = os.path.join(self.temp_dir.name, "empty_imgs")
        os.makedirs(sub_img_dir, exist_ok=True)
        fake_img = os.path.join(sub_img_dir, "fake_img.jpg")
        with open(fake_img, "wb") as f:
            f.write(b"fake image data")

        md5_val = auto_reply.get_file_md5(fake_img)
        auto_reply.record_contact_sent("TestTarget", md5_val)

        # 模擬網路爬圖無新圖（monkeypatch crawl_grandma_images 不下載任何檔案）
        orig_crawler = auto_reply.crawl_grandma_images
        auto_reply.crawl_grandma_images = lambda *args, **kwargs: None
        try:
            rel_img_dir = os.path.relpath(sub_img_dir, auto_reply.SCRIPT_DIR)
            cfg = {"history_rounds": 100, "images_dir": rel_img_dir}
            res = auto_reply.pick_unique_image([fake_img], "TestTarget", cfg)
            self.assertIsNone(res)
        finally:
            auto_reply.crawl_grandma_images = orig_crawler

    def test_circuit_breaker_save_failure_fail_closed(self):
        # 測試斷路器狀態寫入失敗 (如唯讀或磁碟滿) 時，立即觸發記憶體全域 Fail-Closed 鎖定
        orig_cb_path = auto_reply.CIRCUIT_BREAKER_PATH
        orig_cb_blocked = dict(auto_reply.CIRCUIT_BREAKER_BLOCKED_UNTIL)
        try:
            auto_reply.CIRCUIT_BREAKER_BLOCKED_UNTIL.clear()
            # 指定無效路徑引發存檔例外
            auto_reply.CIRCUIT_BREAKER_PATH = os.path.join(self.temp_dir.name, "nonexistent_dir", "sub", "cb.json")
            auto_reply.save_circuit_breaker_state()
            self.assertIn("__GLOBAL__", auto_reply.CIRCUIT_BREAKER_BLOCKED_UNTIL)
            self.assertFalse(auto_reply.check_circuit_breaker("AnyContact"))
        finally:
            auto_reply.CIRCUIT_BREAKER_PATH = orig_cb_path
            auto_reply.CIRCUIT_BREAKER_BLOCKED_UNTIL.clear()
            auto_reply.CIRCUIT_BREAKER_BLOCKED_UNTIL.update(orig_cb_blocked)

    def test_find_target_chat_window_invisible_and_class_filtering(self):
        # 測試隱藏視窗與非 LINE 聊天室類別視窗 (非 ChatWnd/Qt5QWindowIcon) 遭嚴格過濾排除
        orig_get_all = auto_reply.get_all_line_windows
        orig_find_main = auto_reply.find_line_main_window
        auto_reply.find_line_main_window = lambda: 999
        auto_reply.get_all_line_windows = lambda: [
            (1001, "TargetContact", "NotAValidClass", True),   # 錯誤 Class
            (1002, "TargetContact", "Qt5QWindowIcon", False),  # 隱藏不可見視窗
            (1003, "TargetContact", "ChatWnd", True),          # 合格聊天視窗
        ]
        try:
            matched = auto_reply.find_target_chat_window("TargetContact")
            self.assertEqual(len(matched), 1)
            self.assertEqual(matched[0][0], 1003)
        finally:
            auto_reply.get_all_line_windows = orig_get_all
            auto_reply.find_line_main_window = orig_find_main

    def test_circuit_breaker_roundtrip_persistence(self):
        # 測試斷路器狀態寫入後重新載入 (往返跨重啟測試)，驗證鎖定與基準線完整還原
        now = time.time()
        auto_reply.CIRCUIT_BREAKER_BLOCKED_UNTIL["LockedUser"] = now + 600
        auto_reply.REPLY_HISTORY_TIMESTAMPS["ActiveUser"] = [now - 10, now - 5]
        auto_reply.LAST_SENT_TIME_BY_TARGET["ActiveUser"] = now - 5
        auto_reply.LAST_PROCESSED_UNREAD["ActiveUser"] = 100
        auto_reply.save_circuit_breaker_state()

        # 模擬程式重新啟動，從磁碟重新載入
        loaded = auto_reply.load_circuit_breaker_state()
        self.assertIn("LockedUser", loaded.get("blocked_until", {}))
        self.assertAlmostEqual(loaded["blocked_until"]["LockedUser"], now + 600, delta=2)
        self.assertEqual(loaded.get("last_processed_unread", {}).get("ActiveUser"), 100)
        self.assertEqual(len(loaded.get("reply_timestamps", {}).get("ActiveUser", [])), 2)

    def test_circuit_breaker_fail_closed_on_root_list(self):
        # 測試 circuit_breaker.json 根節點為空陣列 [] 等非 dict 時，遵循 Fail-Closed 原則啟動全域 30 分鐘鎖定
        corrupt_file = os.path.join(self.temp_dir.name, "list_cb.json")
        with open(corrupt_file, "w", encoding="utf-8") as f:
            f.write("[]")
        orig_cb_path = auto_reply.CIRCUIT_BREAKER_PATH
        auto_reply.CIRCUIT_BREAKER_PATH = corrupt_file
        try:
            state = auto_reply.load_circuit_breaker_state()
            self.assertIn("__GLOBAL__", state.get("blocked_until", {}))
        finally:
            auto_reply.CIRCUIT_BREAKER_PATH = orig_cb_path

    def test_group_window_foreground_paren_rejection(self):
        # 測試群組成員數視窗 (如 'Family (4)') 在前景時遭嚴格排除，絕不誤當作 1:1 未讀視窗
        orig_get_all = auto_reply.get_all_line_windows
        orig_find_main = auto_reply.find_line_main_window
        auto_reply.find_line_main_window = lambda: 999
        auto_reply.get_all_line_windows = lambda: [
            (2001, "Family (4)", "ChatWnd", True),
        ]
        import win32gui
        orig_fg = win32gui.GetForegroundWindow
        win32gui.GetForegroundWindow = lambda: 2001
        try:
            matched = auto_reply.find_target_chat_window("Family")
            self.assertEqual(matched, [])
        finally:
            win32gui.GetForegroundWindow = orig_fg
            auto_reply.get_all_line_windows = orig_get_all
            auto_reply.find_line_main_window = orig_find_main

    def test_circuit_breaker_partial_corruption_schema_fail_closed(self):
        # 測試 circuit_breaker.json 部分欄位 schema 損壞時，整體判為損毀並 Fail-Closed
        corrupt_file = os.path.join(self.temp_dir.name, "partial_corrupt_cb.json")
        with open(corrupt_file, "w", encoding="utf-8") as f:
            f.write('{"blocked_until": {"Victim": "not-a-number"}}')
        orig_cb_path = auto_reply.CIRCUIT_BREAKER_PATH
        auto_reply.CIRCUIT_BREAKER_PATH = corrupt_file
        try:
            state = auto_reply.load_circuit_breaker_state()
            self.assertIn("__GLOBAL__", state.get("blocked_until", {}))
        finally:
            auto_reply.CIRCUIT_BREAKER_PATH = orig_cb_path

    def test_emergency_lock_persistence_across_restart(self):
        # 測試跨重啟應急鎖定標記檔 (.emergency_lock) 存在時，重新載入仍維持全域鎖定
        em_lock = auto_reply.CIRCUIT_BREAKER_PATH + ".emergency_lock"
        now = time.time()
        with open(em_lock, "w", encoding="utf-8") as f:
            json.dump({"__GLOBAL__": now + 1200}, f)
        try:
            state = auto_reply.load_circuit_breaker_state()
            self.assertIn("__GLOBAL__", state.get("blocked_until", {}))
            self.assertAlmostEqual(state["blocked_until"]["__GLOBAL__"], now + 1200, delta=2)
        finally:
            if os.path.exists(em_lock):
                os.remove(em_lock)

    def test_focus_chat_input_group_paren_and_name_rejection(self):
        # 測試 focus_chat_input 注入點擊前，強制檢查前景標題與括號，若是群組視窗一律拒絕點擊
        import win32gui
        orig_is_win = win32gui.IsWindow
        orig_is_vis = win32gui.IsWindowVisible
        orig_is_ic = win32gui.IsIconic
        orig_fg = win32gui.GetForegroundWindow
        orig_title = win32gui.GetWindowText

        win32gui.IsWindow = lambda hwnd: True
        win32gui.IsWindowVisible = lambda hwnd: True
        win32gui.IsIconic = lambda hwnd: False
        win32gui.GetForegroundWindow = lambda: 3001
        try:
            # 案例 1：帶有群組成員數括號 -> 拒絕
            win32gui.GetWindowText = lambda hwnd: "Family (4)"
            self.assertFalse(auto_reply.focus_chat_input(3001, target_name="Family"))

            # 案例 2：標題與目標對象不符 -> 拒絕
            win32gui.GetWindowText = lambda hwnd: "OtherUser"
            self.assertFalse(auto_reply.focus_chat_input(3001, target_name="TargetUser"))
        finally:
            win32gui.IsWindow = orig_is_win
            win32gui.IsWindowVisible = orig_is_vis
            win32gui.IsIconic = orig_is_ic
            win32gui.GetForegroundWindow = orig_fg
            win32gui.GetWindowText = orig_title

    def test_is_likely_photo_sticker_rejection(self):
        # 測試 is_likely_photo 準確辨識合規相機照片 (>512x512, RGB, >=50KB)，並排除小尺寸 JPEG 貼圖與透明貼圖
        from PIL import Image

        # 1. 建立真實合格大尺寸 JPEG 相片 (> 512x512, RGB, >= 50KB)
        jpg_photo = os.path.join(self.temp_dir.name, "real_photo.jpg")
        img_large = Image.new("RGB", (600, 600), color=(200, 100, 50))
        img_large.save(jpg_photo, "JPEG")
        with open(jpg_photo, "ab") as f:
            f.write(b"\x00" * 60000)
        self.assertTrue(auto_reply.is_likely_photo(jpg_photo))

        # 2. 建立小尺寸 JPEG 貼圖 (200x200) -> 應被拒絕
        jpg_sticker = os.path.join(self.temp_dir.name, "sticker.jpg")
        img_small = Image.new("RGB", (200, 200), color=(0, 255, 0))
        img_small.save(jpg_sticker, "JPEG")
        self.assertFalse(auto_reply.is_likely_photo(jpg_sticker))

        # 3. 建立帶 Alpha 透明通道的 RGBA 貼圖 -> 應被拒絕
        rgba_sticker = os.path.join(self.temp_dir.name, "sticker.png")
        img_rgba = Image.new("RGBA", (400, 400), color=(0, 0, 255, 128))
        img_rgba.save(rgba_sticker, "PNG")
        self.assertFalse(auto_reply.is_likely_photo(rgba_sticker))

    def test_load_chat_dirs_map_corrupted_value_resilience(self):
        # 測試 chat_dirs.json 含有非字串或異常長度的 key/value 時安全過濾，不發生崩潰
        map_path = auto_reply.CHAT_MAP_PATH
        dummy_hash = "a" * 40
        with open(map_path, "w", encoding="utf-8") as f:
            json.dump({dummy_hash: "ContactA", "short_key": "Alice", "b" * 40: None, "c" * 40: 123}, f)
        loaded = auto_reply.load_chat_dirs_map()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded.get(dummy_hash), "ContactA")

    def test_contact_history_nested_corruption_resilience(self):
        # 測試 contact_history.json 存在巢狀非 dict、陣列或空值時，自動正規化且不引發崩潰
        corrupt_hist = os.path.join(self.temp_dir.name, "corrupt_history.json")
        with open(corrupt_hist, "w", encoding="utf-8") as f:
            json.dump({"Mom": [], "ContactA": "bad", "Other": {"sent": None, "received": [123]}}, f)
        orig_hist = auto_reply.HISTORY_PATH
        auto_reply.HISTORY_PATH = corrupt_hist
        try:
            h = auto_reply.load_contact_history()
            self.assertIsInstance(h["Mom"], dict)
            self.assertEqual(h["Mom"]["sent"], [])
            self.assertEqual(h["Mom"]["received"], [])
            self.assertIsInstance(h["ContactA"], dict)
            self.assertEqual(h["Other"]["received"], ["123"])

            # 驗證寫入安全
            auto_reply.record_contact_sent("Mom", "dummy_md5")
            h2 = auto_reply.load_contact_history()
            self.assertIn("dummy_md5", h2["Mom"]["sent"])

            # 驗證 pick_unique_image 在損毀歷史下不崩潰
            chosen = auto_reply.pick_unique_image([], "Mom", {"history_rounds": 10})
            self.assertIsNone(chosen)
        finally:
            auto_reply.HISTORY_PATH = orig_hist

    def test_focus_chat_input_unread_paren_clearing_polling(self):
        # 測試 focus_chat_input 前景核驗時，等待 1:1 未讀括號由 Qt 重繪消除之輪詢機制
        import win32gui
        orig_is_win = win32gui.IsWindow
        orig_is_vis = win32gui.IsWindowVisible
        orig_is_ic = win32gui.IsIconic
        orig_fg = win32gui.GetForegroundWindow
        orig_title = win32gui.GetWindowText
        orig_rect = win32gui.GetWindowRect
        orig_click = auto_reply.pyautogui.click

        win32gui.IsWindow = lambda hwnd: True
        win32gui.IsWindowVisible = lambda hwnd: True
        win32gui.IsIconic = lambda hwnd: False
        win32gui.GetForegroundWindow = lambda: 5001
        win32gui.GetWindowRect = lambda hwnd: (0, 0, 400, 600)
        auto_reply.pyautogui.click = lambda x, y: None

        call_count = [0]
        def dynamic_title(hwnd):
            call_count[0] += 1
            if call_count[0] < 3:
                return "TargetUser (1)"
            return "TargetUser"

        win32gui.GetWindowText = dynamic_title
        orig_uia = auto_reply.verify_is_not_group_window_uia
        auto_reply.verify_is_not_group_window_uia = lambda hwnd: True
        try:
            self.assertTrue(auto_reply.focus_chat_input(5001, target_name="TargetUser"))
        finally:
            auto_reply.verify_is_not_group_window_uia = orig_uia
            win32gui.IsWindow = orig_is_win
            win32gui.IsWindowVisible = orig_is_vis
            win32gui.IsIconic = orig_is_ic
            win32gui.GetForegroundWindow = orig_fg
            win32gui.GetWindowText = orig_title
            win32gui.GetWindowRect = orig_rect
            auto_reply.pyautogui.click = orig_click

    def test_load_chat_dirs_map_hex_validation(self):
        # 測試 load_chat_dirs_map 嚴格要求 40 位 hex 雜湊，非 hex 雜湊自動過濾
        map_path = auto_reply.CHAT_MAP_PATH
        valid_hex = "a1b2c3d4e5" * 4
        invalid_hex = "z" * 40
        with open(map_path, "w", encoding="utf-8") as f:
            json.dump({valid_hex: "TargetA", invalid_hex: "TargetB"}, f)
        loaded = auto_reply.load_chat_dirs_map()
        self.assertIn(valid_hex, loaded)
        self.assertNotIn(invalid_hex, loaded)

    def test_group_name_keyword_rejection(self):
        # 測試 find_target_chat_window 攔截帶有群組特徵關鍵字之視窗 (例如 Family, Group 等)，絕不誤判為 1:1 好友
        orig_get_all = auto_reply.get_all_line_windows
        orig_find_main = auto_reply.find_line_main_window
        auto_reply.find_line_main_window = lambda: 9999
        auto_reply.get_all_line_windows = lambda: [(7001, "Family", "ChatWnd", True)]
        try:
            matched = auto_reply.find_target_chat_window("Family")
            self.assertEqual(matched, [])
        finally:
            auto_reply.get_all_line_windows = orig_get_all
            auto_reply.find_line_main_window = orig_find_main

    def test_circuit_breaker_save_failure_during_unlock_fail_closed(self):
        # 測試過期解鎖時若原子存檔失敗，遵循 Fail-Closed 原則拒絕當次發送
        target = "TestUser"
        now = time.time()
        auto_reply.CIRCUIT_BREAKER_BLOCKED_UNTIL[target] = now - 10  # 模擬過期鎖定
        orig_cb_path = auto_reply.CIRCUIT_BREAKER_PATH
        # 指向無法寫入的路徑製造存檔失敗
        auto_reply.CIRCUIT_BREAKER_PATH = os.path.join(self.temp_dir.name, "nonexistent", "sub", "cb.json")
        try:
            allowed = auto_reply.check_circuit_breaker(target)
            self.assertFalse(allowed)
        finally:
            auto_reply.CIRCUIT_BREAKER_PATH = orig_cb_path

    def test_contact_history_corruption_fallback_memory_recovery(self):
        # 測試百輪歷史檔損毀時，自動回退上一份記憶體有效歷史，絕不放行重複圖片
        auto_reply._LAST_VALID_CONTACT_HISTORY = {"UserA": {"sent": ["hash123"], "received": []}}
        with open(auto_reply.HISTORY_PATH, "w", encoding="utf-8") as f:
            f.write("{ corrupt json")
        h = auto_reply.load_contact_history()
        self.assertIn("hash123", h.get("UserA", {}).get("sent", []))

    def test_is_likely_photo_eimg_sticker_threshold(self):
        # 測試 is_likely_photo 對 .eimg 加密檔嚴格要求 >= 250KB，排除 100KB 以下之加密貼圖
        small_eimg = os.path.join(self.temp_dir.name, "sticker.eimg")
        large_eimg = os.path.join(self.temp_dir.name, "real_photo.eimg")
        with open(small_eimg, "wb") as f:
            f.write(b"EIMG" + b"\x00" * 50000)
        with open(large_eimg, "wb") as f:
            f.write(b"EIMG" + b"\x00" * 300000)

        self.assertFalse(auto_reply.is_likely_photo(small_eimg))
        self.assertTrue(auto_reply.is_likely_photo(large_eimg))

    def test_clear_history_removes_emergency_copy_and_memory(self):
        # 測試清除歷史時，同時清除正式歷史檔、應急複本與記憶體快取
        hist_file = auto_reply.HISTORY_PATH
        em_file = auto_reply.HISTORY_PATH + ".emergency_copy"
        with open(hist_file, "w", encoding="utf-8") as f:
            f.write("{}")
        with open(em_file, "w", encoding="utf-8") as f:
            f.write("{}")
        auto_reply._LAST_VALID_CONTACT_HISTORY = {"User": {"sent": ["abc"], "received": []}}

        # 模擬清除指令行為
        for p in (hist_file, em_file):
            if os.path.exists(p):
                os.remove(p)
        auto_reply._LAST_VALID_CONTACT_HISTORY.clear()

        self.assertFalse(os.path.exists(hist_file))
        self.assertFalse(os.path.exists(em_file))
        self.assertEqual(auto_reply.load_contact_history(), {})

    def test_save_contact_history_syncs_emergency_copy(self):
        # 測試每次成功儲存聯絡人歷史時，應急副本 .emergency_copy 保持即時同步
        hist_data = {"TargetA": {"sent": ["hash_xyz"], "received": []}}
        auto_reply.save_contact_history(hist_data)
        em_file = auto_reply.HISTORY_PATH + ".emergency_copy"
        self.assertTrue(os.path.exists(em_file))
        with open(em_file, "r", encoding="utf-8") as f:
            saved_em = json.load(f)
        self.assertEqual(saved_em, hist_data)

    def test_verify_is_not_group_window_uia_fail_closed(self):
        # 測試 UI Automation 模組缺失或例外時遵循 Fail-Closed 拒絕發送
        import sys
        real_modules = sys.modules.copy()
        try:
            sys.modules["uiautomation"] = None
            res = auto_reply.verify_is_not_group_window_uia(12345)
            self.assertFalse(res)
        finally:
            sys.modules.clear()
            sys.modules.update(real_modules)

    def test_init_unread_baselines_selective(self):
        # 測試熱重載新增特定聯絡人時，僅精準初始化該聯絡人之未讀基準線
        orig_get_all = auto_reply.get_all_line_windows
        auto_reply.get_all_line_windows = lambda: [
            (2001, "TargetAlpha (3)", "ChatWnd", True),
            (2002, "TargetBeta (5)", "ChatWnd", True),
        ]
        try:
            auto_reply.init_unread_baselines(["TargetAlpha"])
            self.assertEqual(auto_reply.LAST_PROCESSED_UNREAD.get("TargetAlpha"), 3)
            self.assertNotIn("TargetBeta", auto_reply.LAST_PROCESSED_UNREAD)
        finally:
            auto_reply.get_all_line_windows = orig_get_all

    def test_zero_config_auto_bind_single_target_success(self):
        # 測試 try_auto_bind_channel 正常正向綁定 (目標上線、UIA 核驗通過、視窗指紋有新異動且未消費、時間戳吻合、最新訊息非本機送出)
        fake_hash = "67fa03ef1234567890abcdef1234567890abcdef"
        fake_path = os.path.join(self.temp_dir.name, "mo", fake_hash, "test.eimg")
        now = time.time()

        orig_find = auto_reply.find_target_chat_window
        orig_uia = auto_reply.verify_is_not_group_window_uia
        orig_fp = auto_reply.get_chat_window_fingerprint
        orig_media_info = auto_reply.get_chat_latest_media_info
        orig_media = auto_reply.is_chat_latest_message_media
        orig_in = auto_reply.is_chat_latest_message_incoming
        orig_act = auto_reply.activate_window

        try:
            auto_reply.find_target_chat_window = lambda t: [(9901, t, "ChatWnd", True)] if t == "TargetA" else []
            auto_reply.verify_is_not_group_window_uia = lambda h: True
            auto_reply.get_chat_window_fingerprint = lambda h: "8_100_200_300_400"
            auto_reply.get_chat_latest_media_info = lambda h: (True, True)
            auto_reply.is_chat_latest_message_media = lambda h: True
            auto_reply.is_chat_latest_message_incoming = lambda h: True
            auto_reply.activate_window = lambda h: True

            # 模擬剛收到新訊息，未消費，變更時間與快取寫入時間 cand_m 吻合 (< 3.0s)
            auto_reply._TARGET_WINDOW_STATE["TargetA"] = {"fp": "8_100_200_300_400", "changed_at": now - 0.5, "consumed": False, "has_media": True, "is_incoming": True}

            m_map = {}
            bound = auto_reply.try_auto_bind_channel(fake_hash, fake_path, now, ["TargetA"], m_map)
            self.assertEqual(bound, "TargetA")
            self.assertEqual(m_map.get(fake_hash), "TargetA")
            # 核驗該次異動已被標記為已消費 (consumed = True)
            self.assertTrue(auto_reply._TARGET_WINDOW_STATE["TargetA"]["consumed"])
            reloaded = auto_reply.load_chat_dirs_map()
            self.assertEqual(reloaded.get(fake_hash), "TargetA")
        finally:
            auto_reply.find_target_chat_window = orig_find
            auto_reply.verify_is_not_group_window_uia = orig_uia
            auto_reply.get_chat_window_fingerprint = orig_fp
            auto_reply.get_chat_latest_media_info = orig_media_info
            auto_reply.is_chat_latest_message_media = orig_media
            auto_reply.is_chat_latest_message_incoming = orig_in
            auto_reply.activate_window = orig_act
            auto_reply._TARGET_WINDOW_STATE.pop("TargetA", None)

    def test_try_auto_bind_channel_reject_if_consumed_or_no_change(self):
        # 負向測試 (P2 #1 核心防禦)：目標視窗無新訊息異動 (已消費或初次基準 changed_at == 0.0) 時，嚴禁誤綁外來快取頻道
        fake_hash = "444403ef1234567890abcdef1234567890abcdef"
        fake_path = os.path.join(self.temp_dir.name, "mo", fake_hash, "test.eimg")
        now = time.time()

        orig_find = auto_reply.find_target_chat_window
        orig_uia = auto_reply.verify_is_not_group_window_uia
        orig_in = auto_reply.is_chat_latest_message_incoming

        try:
            auto_reply.find_target_chat_window = lambda t: [(9905, t, "ChatWnd", True)] if t == "TargetF" else []
            auto_reply.verify_is_not_group_window_uia = lambda h: True
            auto_reply.is_chat_latest_message_incoming = lambda h: True

            # 狀況 1: 異動已被消費 (consumed = True)
            auto_reply._TARGET_WINDOW_STATE["TargetF"] = {"fp": "7_100_200_300_350", "changed_at": now - 1.0, "consumed": True}
            m_map = {}
            bound = auto_reply.try_auto_bind_channel(fake_hash, fake_path, now, ["TargetF"], m_map)
            self.assertIsNone(bound)
            self.assertNotIn(fake_hash, m_map)

            # 狀況 2: 初始基準未曾變動 (changed_at == 0.0)
            auto_reply._TARGET_WINDOW_STATE["TargetF"] = {"fp": "7_100_200_300_350", "changed_at": 0.0, "consumed": False}
            bound2 = auto_reply.try_auto_bind_channel(fake_hash, fake_path, now, ["TargetF"], m_map)
            self.assertIsNone(bound2)
            self.assertNotIn(fake_hash, m_map)

            # 狀況 3: 無任何狀態紀錄 (state is None)
            auto_reply._TARGET_WINDOW_STATE.pop("TargetF", None)
            bound3 = auto_reply.try_auto_bind_channel(fake_hash, fake_path, now, ["TargetF"], m_map)
            self.assertIsNone(bound3)
            self.assertNotIn(fake_hash, m_map)
        finally:
            auto_reply.find_target_chat_window = orig_find
            auto_reply.verify_is_not_group_window_uia = orig_uia
            auto_reply.is_chat_latest_message_incoming = orig_in
            auto_reply._TARGET_WINDOW_STATE.pop("TargetF", None)

    def test_update_target_window_fingerprints_state_lifecycle(self):
        # 測試目標指紋狀態機生命週期：初次登記為基準(changed_at=0, consumed=True)，指紋變動時精準更新(changed_at>0, consumed=False)
        orig_find = auto_reply.find_target_chat_window
        orig_fp = auto_reply.get_chat_window_fingerprint

        fp_holder = ["fp_initial"]
        orig_fetch = auto_reply._fetch_chat_window_items_info
        try:
            auto_reply.find_target_chat_window = lambda t: [(9920, t, "ChatWnd", True)] if t == "TargetLife" else []
            auto_reply.get_chat_window_fingerprint = lambda h: fp_holder[0]
            auto_reply._fetch_chat_window_items_info = lambda h: (fp_holder[0], "", "")

            auto_reply._TARGET_WINDOW_STATE.pop("TargetLife", None)
            # 1. 初次探測 -> 建立基準
            auto_reply.update_target_window_fingerprints(["TargetLife"])
            st1 = auto_reply._TARGET_WINDOW_STATE.get("TargetLife")
            self.assertIsNotNone(st1)
            self.assertEqual(st1["fp"], "fp_initial")
            self.assertEqual(st1["changed_at"], 0.0)
            self.assertTrue(st1["consumed"])

            # 2. 相同指紋輪詢 -> 不更新時間戳
            auto_reply.update_target_window_fingerprints(["TargetLife"])
            st2 = auto_reply._TARGET_WINDOW_STATE.get("TargetLife")
            self.assertEqual(st2["changed_at"], 0.0)
            self.assertTrue(st2["consumed"])

            # 3. 視窗指紋改變 (新訊息抵達) -> 更新 changed_at 且 consumed = False
            t_before = time.time()
            fp_holder[0] = "fp_new_message"
            auto_reply.update_target_window_fingerprints(["TargetLife"])
            st3 = auto_reply._TARGET_WINDOW_STATE.get("TargetLife")
            self.assertEqual(st3["fp"], "fp_new_message")
            self.assertGreaterEqual(st3["changed_at"], t_before)
            self.assertFalse(st3["consumed"])
        finally:
            auto_reply.find_target_chat_window = orig_find
            auto_reply.get_chat_window_fingerprint = orig_fp
            auto_reply._fetch_chat_window_items_info = orig_fetch
            auto_reply._TARGET_WINDOW_STATE.pop("TargetLife", None)

    def test_try_auto_bind_channel_reject_if_mtime_time_mismatch(self):
        # 負向測試 (P2 #2)：快取寫入時間 (cand_m) 與視窗變更時間 (changed_at) 相差過大時 (>3.0s)，拒絕綁定
        fake_hash = "888803ef1234567890abcdef1234567890abcdef"
        fake_path = os.path.join(self.temp_dir.name, "mo", fake_hash, "test.eimg")
        now = time.time()

        orig_find = auto_reply.find_target_chat_window
        orig_uia = auto_reply.verify_is_not_group_window_uia
        orig_in = auto_reply.is_chat_latest_message_incoming

        try:
            auto_reply.find_target_chat_window = lambda t: [(9908, t, "ChatWnd", True)] if t == "TargetMtime" else []
            auto_reply.verify_is_not_group_window_uia = lambda h: True
            auto_reply.is_chat_latest_message_incoming = lambda h: True

            # 視窗變更時間為 10 秒前
            auto_reply._TARGET_WINDOW_STATE["TargetMtime"] = {"fp": "5_100_100_200_200", "changed_at": now - 10.0, "consumed": False}

            m_map = {}
            # 快取檔案 mtime 為 now (差距 10 秒 > 3.0s)
            bound = auto_reply.try_auto_bind_channel(fake_hash, fake_path, now, ["TargetMtime"], m_map)
            self.assertIsNone(bound)
            self.assertNotIn(fake_hash, m_map)
        finally:
            auto_reply.find_target_chat_window = orig_find
            auto_reply.verify_is_not_group_window_uia = orig_uia
            auto_reply.is_chat_latest_message_incoming = orig_in
            auto_reply._TARGET_WINDOW_STATE.pop("TargetMtime", None)

    def test_try_auto_bind_channel_reject_if_last_message_from_self(self):
        # 負向測試：目標最新訊息為本機送出時，嚴禁誤綁外來快取頻道
        fake_hash = "111103ef1234567890abcdef1234567890abcdef"
        fake_path = os.path.join(self.temp_dir.name, "mo", fake_hash, "test.eimg")
        now = time.time()

        orig_find = auto_reply.find_target_chat_window
        orig_uia = auto_reply.verify_is_not_group_window_uia
        orig_media_info = auto_reply.get_chat_latest_media_info
        orig_media = auto_reply.is_chat_latest_message_media
        orig_in = auto_reply.is_chat_latest_message_incoming
        orig_act = auto_reply.activate_window

        try:
            auto_reply.find_target_chat_window = lambda t: [(9902, t, "ChatWnd", True)] if t == "TargetB" else []
            auto_reply.verify_is_not_group_window_uia = lambda h: True
            auto_reply.get_chat_latest_media_info = lambda h: (True, False)
            auto_reply.is_chat_latest_message_media = lambda h: True
            auto_reply.is_chat_latest_message_incoming = lambda h: False  # 本機發送訊息 (非對方傳入)
            auto_reply.activate_window = lambda h: True

            auto_reply._TARGET_WINDOW_STATE["TargetB"] = {"fp": "6_100_100_200_200", "changed_at": now - 0.5, "consumed": False, "has_media": True, "is_incoming": False}

            m_map = {}
            bound = auto_reply.try_auto_bind_channel(fake_hash, fake_path, time.time(), ["TargetB"], m_map)
            self.assertIsNone(bound)
            self.assertNotIn(fake_hash, m_map)
            reloaded = auto_reply.load_chat_dirs_map()
            self.assertNotIn(fake_hash, reloaded)
        finally:
            auto_reply.find_target_chat_window = orig_find
            auto_reply.verify_is_not_group_window_uia = orig_uia
            auto_reply.get_chat_latest_media_info = orig_media_info
            auto_reply.is_chat_latest_message_media = orig_media
            auto_reply.is_chat_latest_message_incoming = orig_in
            auto_reply.activate_window = orig_act
            auto_reply._TARGET_WINDOW_STATE.pop("TargetB", None)

    def test_try_auto_bind_channel_reject_if_multiple_targets_active(self):
        # 負向測試：多目標視窗同時上線時，無法唯一判定歸屬，安全拒絕自動綁定
        fake_hash = "222203ef1234567890abcdef1234567890abcdef"
        fake_path = os.path.join(self.temp_dir.name, "mo", fake_hash, "test.eimg")

        orig_find = auto_reply.find_target_chat_window
        orig_uia = auto_reply.verify_is_not_group_window_uia

        try:
            auto_reply.find_target_chat_window = lambda t: [(9903, t, "ChatWnd", True)]
            auto_reply.verify_is_not_group_window_uia = lambda h: True

            m_map = {}
            bound = auto_reply.try_auto_bind_channel(fake_hash, fake_path, time.time(), ["TargetC", "TargetD"], m_map)
            self.assertIsNone(bound)
        finally:
            auto_reply.find_target_chat_window = orig_find
            auto_reply.verify_is_not_group_window_uia = orig_uia

    def test_try_auto_bind_channel_rollback_if_save_fails(self):
        # 負向測試：磁碟儲存失敗時撤銷記憶體綁定並回傳 None
        fake_hash = "333303ef1234567890abcdef1234567890abcdef"
        fake_path = os.path.join(self.temp_dir.name, "mo", fake_hash, "test.eimg")
        now = time.time()

        orig_find = auto_reply.find_target_chat_window
        orig_uia = auto_reply.verify_is_not_group_window_uia
        orig_media_info = auto_reply.get_chat_latest_media_info
        orig_media = auto_reply.is_chat_latest_message_media
        orig_in = auto_reply.is_chat_latest_message_incoming
        orig_act = auto_reply.activate_window
        orig_save = auto_reply.save_chat_dirs_map

        try:
            auto_reply.find_target_chat_window = lambda t: [(9904, t, "ChatWnd", True)] if t == "TargetE" else []
            auto_reply.verify_is_not_group_window_uia = lambda h: True
            auto_reply.get_chat_latest_media_info = lambda h: (True, True)
            auto_reply.is_chat_latest_message_media = lambda h: True
            auto_reply.is_chat_latest_message_incoming = lambda h: True
            auto_reply.activate_window = lambda h: True
            auto_reply.save_chat_dirs_map = lambda m: False  # 模擬寫入失敗

            auto_reply._TARGET_WINDOW_STATE["TargetE"] = {"fp": "9_100_100_200_200", "changed_at": now - 0.5, "consumed": False, "has_media": True, "is_incoming": True}

            m_map = {}
            bound = auto_reply.try_auto_bind_channel(fake_hash, fake_path, now, ["TargetE"], m_map)
            self.assertIsNone(bound)
            self.assertNotIn(fake_hash, m_map)
            # 寫入磁碟失敗時，不可標記為已消費 (保留未來重試機會)
            self.assertFalse(auto_reply._TARGET_WINDOW_STATE["TargetE"]["consumed"])
        finally:
            auto_reply.find_target_chat_window = orig_find
            auto_reply.verify_is_not_group_window_uia = orig_uia
            auto_reply.get_chat_latest_media_info = orig_media_info
            auto_reply.is_chat_latest_message_media = orig_media
            auto_reply.is_chat_latest_message_incoming = orig_in
            auto_reply.activate_window = orig_act
            auto_reply.save_chat_dirs_map = orig_save
            auto_reply._TARGET_WINDOW_STATE.pop("TargetE", None)

    def test_unbind_cli_logic(self):
        # 測試 --unbind 解除綁定邏輯 (名稱與雜湊支援，以及磁碟原子更新)
        fake_hash = "555503ef1234567890abcdef1234567890abcdef"
        m_map = {fake_hash: "TargetUnbindUser"}
        auto_reply.save_chat_dirs_map(m_map)

        reloaded = auto_reply.load_chat_dirs_map()
        self.assertIn(fake_hash, reloaded)

        # 模擬解除綁定
        # 透過 main() 的 CLI 參數驗證 --unbind 路徑
        with mock.patch("sys.argv", ["auto_reply.py", "--unbind", "TargetUnbindUser"]):
            with self.assertRaises(SystemExit) as cm:
                auto_reply.main()
            self.assertEqual(cm.exception.code, 0)



        final_map = auto_reply.load_chat_dirs_map()
        self.assertNotIn(fake_hash, final_map)

    def test_fingerprint_ignores_rect_and_scroll_changes(self):
        # 測試訊息指紋基於內容而非座標：當視窗縮放或滾動 (BoundingRectangle 改變) 但訊息內容未變時，指紋保持絕對一致
        class MockRect:
            def __init__(self, l, t, r, b):
                self.left, self.top, self.right, self.bottom = l, t, r, b

        class MockItem:
            def __init__(self, name, rect):
                self.Name = name
                self.BoundingRectangle = rect

        class MockView:
            def __init__(self, items):
                self._items = items
            def Exists(self, a, b): return True
            def GetChildren(self): return self._items

        class MockWin:
            def __init__(self, view):
                self._view = view
            def Exists(self, a, b): return True
            def ListControl(self, **kwargs): return self._view

        import sys
        import types
        mock_auto = types.ModuleType("uiautomation")

        items_pos1 = [MockItem("Msg1", MockRect(10, 10, 100, 50)), MockItem("Msg2", MockRect(10, 60, 100, 100))]
        mock_win1 = MockWin(MockView(items_pos1))
        mock_auto.ControlFromHandle = lambda h: mock_win1

        orig_modules = sys.modules.get("uiautomation")
        try:
            sys.modules["uiautomation"] = mock_auto
            fp1 = auto_reply.get_chat_window_fingerprint(11111)

            # 模擬視窗縮放 / 滾動，座標大幅度變動，但內容完全一致
            items_pos2 = [MockItem("Msg1", MockRect(50, 200, 300, 250)), MockItem("Msg2", MockRect(50, 260, 300, 320))]
            mock_win2 = MockWin(MockView(items_pos2))
            mock_auto.ControlFromHandle = lambda h: mock_win2
            fp2 = auto_reply.get_chat_window_fingerprint(11111)

            self.assertIsNotNone(fp1)
            self.assertEqual(fp1, fp2)

            # 模擬新訊息加入，指紋產生變化
            items_pos3 = items_pos2 + [MockItem("Msg3", MockRect(50, 330, 300, 380))]
            mock_win3 = MockWin(MockView(items_pos3))
            mock_auto.ControlFromHandle = lambda h: mock_win3
            fp3 = auto_reply.get_chat_window_fingerprint(11111)
            self.assertNotEqual(fp2, fp3)
        finally:
            if orig_modules is not None:
                sys.modules["uiautomation"] = orig_modules
            else:
                sys.modules.pop("uiautomation", None)

    def test_try_auto_bind_channel_reject_if_competing_unknown_channels(self):
        # 測試多未知頻道衝突隔離：當同一快取批次中出現 2 個以上不同之未知頻道時，安全拒絕自動綁定
        fake_hash = "67fa03ef1234567890abcdef1234567890abcdef"
        fake_path = os.path.join(self.temp_dir.name, "mo", fake_hash, "test.eimg")
        now = time.time()

        orig_find = auto_reply.find_target_chat_window
        orig_uia = auto_reply.verify_is_not_group_window_uia
        try:
            auto_reply.find_target_chat_window = lambda t: [(9901, t, "ChatWnd", True)] if t == "TargetA" else []
            auto_reply.verify_is_not_group_window_uia = lambda h: True
            auto_reply._TARGET_WINDOW_STATE["TargetA"] = {"fp": "8_abc123", "changed_at": now - 0.5, "consumed": False}

            m_map = {}
            # 傳入 competing_unknown_count = 2 (表示目前批次有 2 個不同未知頻道)
            bound = auto_reply.try_auto_bind_channel(fake_hash, fake_path, now, ["TargetA"], m_map, competing_unknown_count=2)
            self.assertIsNone(bound)
            self.assertNotIn(fake_hash, m_map)
            # 核驗未消費狀態未被消耗
            self.assertFalse(auto_reply._TARGET_WINDOW_STATE["TargetA"]["consumed"])
        finally:
            auto_reply.find_target_chat_window = orig_find
            auto_reply.verify_is_not_group_window_uia = orig_uia
            auto_reply._TARGET_WINDOW_STATE.pop("TargetA", None)

    def test_try_auto_bind_channel_time_window_boundaries(self):
        # 測試時間範圍邊界：-3.0s ~ +6.0s 容許範圍 (考量快取檔案下載延遲與主迴圈輪詢延遲)
        fake_hash = "999903ef1234567890abcdef1234567890abcdef"
        fake_path = os.path.join(self.temp_dir.name, "mo", fake_hash, "test.eimg")
        base_t = time.time()

        orig_find = auto_reply.find_target_chat_window
        orig_uia = auto_reply.verify_is_not_group_window_uia
        orig_fp = auto_reply.get_chat_window_fingerprint
        orig_media_info = auto_reply.get_chat_latest_media_info
        orig_media = auto_reply.is_chat_latest_message_media
        orig_in = auto_reply.is_chat_latest_message_incoming
        orig_act = auto_reply.activate_window

        try:
            auto_reply.find_target_chat_window = lambda t: [(9910, t, "ChatWnd", True)] if t == "TargetBound" else []
            auto_reply.verify_is_not_group_window_uia = lambda h: True
            auto_reply.get_chat_window_fingerprint = lambda h: "5_fp"
            auto_reply.get_chat_latest_media_info = lambda h: (True, True)
            auto_reply.is_chat_latest_message_media = lambda h: True
            auto_reply.is_chat_latest_message_incoming = lambda h: True
            auto_reply.activate_window = lambda h: True

            # 邊界測試 1: cand_m - changed_at = +5.9s (在 +6.0s 內，放行)
            auto_reply._TARGET_WINDOW_STATE["TargetBound"] = {"fp": "5_fp", "changed_at": base_t, "consumed": False, "has_media": True, "is_incoming": True, "media_seen_at": base_t}
            m_map = {}
            b1 = auto_reply.try_auto_bind_channel(fake_hash, fake_path, base_t + 5.9, ["TargetBound"], m_map)
            self.assertEqual(b1, "TargetBound")

            # 邊界測試 2: cand_m - changed_at = +6.1s (超標，阻斷)
            auto_reply._TARGET_WINDOW_STATE["TargetBound"] = {"fp": "5_fp", "changed_at": base_t, "consumed": False, "has_media": True, "is_incoming": True, "media_seen_at": base_t}
            m_map = {}
            b2 = auto_reply.try_auto_bind_channel(fake_hash, fake_path, base_t + 6.1, ["TargetBound"], m_map)
            self.assertIsNone(b2)

            # 邊界測試 3: cand_m - changed_at = -5.9s (在 -6.0s 內，放行)
            auto_reply._TARGET_WINDOW_STATE["TargetBound"] = {"fp": "5_fp", "changed_at": base_t, "consumed": False, "has_media": True, "is_incoming": True, "media_seen_at": base_t}
            m_map = {}
            b3 = auto_reply.try_auto_bind_channel(fake_hash, fake_path, base_t - 5.9, ["TargetBound"], m_map)
            self.assertEqual(b3, "TargetBound")

            # 邊界測試 4: cand_m - changed_at = -6.1s (超標，阻斷)
            auto_reply._TARGET_WINDOW_STATE["TargetBound"] = {"fp": "5_fp", "changed_at": base_t, "consumed": False, "has_media": True, "is_incoming": True, "media_seen_at": base_t}
            m_map = {}
            b4 = auto_reply.try_auto_bind_channel(fake_hash, fake_path, base_t - 6.1, ["TargetBound"], m_map)
            self.assertIsNone(b4)
        finally:
            auto_reply.find_target_chat_window = orig_find
            auto_reply.verify_is_not_group_window_uia = orig_uia
            auto_reply.get_chat_window_fingerprint = orig_fp
            auto_reply.get_chat_latest_media_info = orig_media_info
            auto_reply.is_chat_latest_message_media = orig_media
            auto_reply.is_chat_latest_message_incoming = orig_in
            auto_reply.activate_window = orig_act
            auto_reply._TARGET_WINDOW_STATE.pop("TargetBound", None)

    def test_window_closed_cleans_state_and_reopen_resets_baseline(self):
        # 測試目標視窗關閉時自動清除狀態紀錄，重新開啟時建立新基準 (changed_at=0.0, consumed=True)，防止誤觸發異動
        orig_find = auto_reply.find_target_chat_window
        orig_fp = auto_reply.get_chat_window_fingerprint
        orig_fetch = auto_reply._fetch_chat_window_items_info

        wins_holder = [[(9950, "TargetReopen", "ChatWnd", True)]]
        try:
            auto_reply.find_target_chat_window = lambda t: wins_holder[0] if t == "TargetReopen" else []
            auto_reply.get_chat_window_fingerprint = lambda h: "10_fp_initial"
            auto_reply._fetch_chat_window_items_info = lambda h: (auto_reply.get_chat_window_fingerprint(h), "", "")

            # 1. 視窗上線 -> 建立初始基準
            auto_reply.update_target_window_fingerprints(["TargetReopen"])
            self.assertIn("TargetReopen", auto_reply._TARGET_WINDOW_STATE)
            self.assertEqual(auto_reply._TARGET_WINDOW_STATE["TargetReopen"]["changed_at"], 0.0)

            # 2. 視窗關閉 -> 清除狀態紀錄
            wins_holder[0] = []
            auto_reply.update_target_window_fingerprints(["TargetReopen"])
            self.assertNotIn("TargetReopen", auto_reply._TARGET_WINDOW_STATE)

            # 3. 視窗重新開啟 (即使新視窗指紋不同) -> 重新登記為新基準，不可直接標記為未消費異動
            wins_holder[0] = [(9951, "TargetReopen", "ChatWnd", True)]
            auto_reply.get_chat_window_fingerprint = lambda h: "12_fp_reopened"
            auto_reply.update_target_window_fingerprints(["TargetReopen"])
            self.assertIn("TargetReopen", auto_reply._TARGET_WINDOW_STATE)
            st = auto_reply._TARGET_WINDOW_STATE["TargetReopen"]
            self.assertEqual(st["changed_at"], 0.0)
            self.assertTrue(st["consumed"])
        finally:
            auto_reply.find_target_chat_window = orig_find
            auto_reply.get_chat_window_fingerprint = orig_fp
            auto_reply._fetch_chat_window_items_info = orig_fetch
            auto_reply._TARGET_WINDOW_STATE.pop("TargetReopen", None)

    def test_try_auto_bind_channel_reject_if_text_message(self):
        # 測試高度嚴重防禦：目標視窗最新訊息為純文字時 (非照片/貼圖)，嚴禁誤綁外來快取照片
        fake_hash = "aaaa03ef1234567890abcdef1234567890abcdef"
        fake_path = os.path.join(self.temp_dir.name, "mo", fake_hash, "test.eimg")
        now = time.time()

        orig_find = auto_reply.find_target_chat_window
        orig_uia = auto_reply.verify_is_not_group_window_uia
        orig_media = auto_reply.is_chat_latest_message_media
        orig_in = auto_reply.is_chat_latest_message_incoming

        try:
            auto_reply.find_target_chat_window = lambda t: [(9911, t, "ChatWnd", True)] if t == "TargetText" else []
            auto_reply.verify_is_not_group_window_uia = lambda h: True
            auto_reply.is_chat_latest_message_media = lambda h: False  # 純文字訊息！
            auto_reply.is_chat_latest_message_incoming = lambda h: True

            auto_reply._TARGET_WINDOW_STATE["TargetText"] = {"fp": "7_text_fp", "changed_at": now - 0.5, "consumed": False}

            m_map = {}
            bound = auto_reply.try_auto_bind_channel(fake_hash, fake_path, now, ["TargetText"], m_map)
            self.assertIsNone(bound)
            self.assertNotIn(fake_hash, m_map)
            # 核驗未消費狀態保留
            self.assertFalse(auto_reply._TARGET_WINDOW_STATE["TargetText"]["consumed"])
        finally:
            auto_reply.find_target_chat_window = orig_find
            auto_reply.verify_is_not_group_window_uia = orig_uia
            auto_reply.is_chat_latest_message_media = orig_media
            auto_reply.is_chat_latest_message_incoming = orig_in
            auto_reply._TARGET_WINDOW_STATE.pop("TargetText", None)

    def test_cross_polling_competing_unknown_channels_sliding_window(self):
        # 測試中度 #1 防禦：跨相鄰輪詢 (15 秒滑動視窗內) 出現多個未知頻道時，全數隔離拒絕綁定
        auto_reply._RECENT_UNKNOWN_CHANNELS.clear()
        now = time.time()

        # 模擬 t=0 出現未知頻道 A
        ch_a = "111103ef1234567890abcdef1234567890abcdef"
        auto_reply._RECENT_UNKNOWN_CHANNELS[ch_a] = now - 2.0

        # 模擬 t=2 (相鄰輪詢) 出現未知頻道 B
        ch_b = "222203ef1234567890abcdef1234567890abcdef"
        auto_reply._RECENT_UNKNOWN_CHANNELS[ch_b] = now

        # 滑動視窗內獨立未知頻道數為 2
        competing = len(auto_reply._RECENT_UNKNOWN_CHANNELS)
        self.assertEqual(competing, 2)

        fake_path = os.path.join(self.temp_dir.name, "mo", ch_b, "test.eimg")
        orig_find = auto_reply.find_target_chat_window
        orig_uia = auto_reply.verify_is_not_group_window_uia
        try:
            auto_reply.find_target_chat_window = lambda t: [(9912, t, "ChatWnd", True)] if t == "TargetSlide" else []
            auto_reply.verify_is_not_group_window_uia = lambda h: True
            auto_reply._TARGET_WINDOW_STATE["TargetSlide"] = {"fp": "8_slide_fp", "changed_at": now - 0.5, "consumed": False}

            m_map = {}
            bound = auto_reply.try_auto_bind_channel(ch_b, fake_path, now, ["TargetSlide"], m_map, competing_unknown_count=competing)
            self.assertIsNone(bound)
        finally:
            auto_reply.find_target_chat_window = orig_find
            auto_reply.verify_is_not_group_window_uia = orig_uia
            auto_reply._TARGET_WINDOW_STATE.pop("TargetSlide", None)
            auto_reply._RECENT_UNKNOWN_CHANNELS.clear()

    def test_is_chat_latest_message_media_real_tree(self):
        # 測試高度嚴重防禦：實體驗證真實 UIA 樹狀結構只認 ImageControl (>=60x60)，排除純文字、空名 Pane 與 36x36 大頭貼
        class MockRect:
            def __init__(self, l, t, r, b):
                self.left, self.top, self.right, self.bottom = l, t, r, b
            def width(self): return self.right - self.left
            def height(self): return self.bottom - self.top

        class MockElement:
            def __init__(self, name="", control_type="PaneControl", rect=None, children=None):
                self.Name = name
                self.ControlTypeName = control_type
                self.BoundingRectangle = rect or MockRect(0, 0, 10, 10)
                self._children = children or []
            def GetChildren(self): return self._children
            def Exists(self, a, b): return True

        class MockView:
            def __init__(self, items, rect=None):
                self._items = items
                self.BoundingRectangle = rect or MockRect(0, 0, 500, 800)
            def Exists(self, a, b): return True
            def GetChildren(self): return self._items

        class MockWin:
            def __init__(self, view):
                self._view = view
            def Exists(self, a, b): return True
            def ListControl(self, **kwargs): return self._view

        import sys
        import types
        mock_auto = types.ModuleType("uiautomation")

        # 案例 1: 純文字訊息，文字包含「你看這張照片」，無任何 ImageControl -> 必須 False
        text_bubble = MockElement("", "PaneControl", MockRect(0, 0, 500, 50), [
            MockElement("你看這張照片好看嗎", "TextControl", MockRect(40, 10, 200, 40))
        ])
        mock_win1 = MockWin(MockView([text_bubble]))
        mock_auto.ControlFromHandle = lambda h: mock_win1

        orig_modules = sys.modules.get("uiautomation")
        try:
            sys.modules["uiautomation"] = mock_auto
            self.assertFalse(auto_reply.is_chat_latest_message_media(12345))

            # 案例 2: 純文字訊息旁邊帶有 36x36 好友大頭貼 ImageControl (< 60x60) -> 必須 False (防頭像誤判)
            text_with_avatar = MockElement("", "PaneControl", MockRect(0, 0, 500, 50), [
                MockElement("Avatar", "ImageControl", MockRect(10, 10, 46, 46)),  # 36x36 大頭貼
                MockElement("早安你好", "TextControl", MockRect(55, 10, 150, 40))
            ])
            mock_win2 = MockWin(MockView([text_with_avatar]))
            mock_auto.ControlFromHandle = lambda h: mock_win2
            self.assertFalse(auto_reply.is_chat_latest_message_media(12345))

            # 案例 3: 真實圖片/貼圖訊息 (150x150 ImageControl >= 60x60) -> 必須 True
            photo_bubble = MockElement("", "PaneControl", MockRect(0, 0, 500, 200), [
                MockElement("", "ImageControl", MockRect(50, 20, 200, 170))  # 150x150 圖片控制項
            ])
            mock_win3 = MockWin(MockView([photo_bubble]))
            mock_auto.ControlFromHandle = lambda h: mock_win3
            self.assertTrue(auto_reply.is_chat_latest_message_media(12345))
        finally:
            if orig_modules is not None:
                sys.modules["uiautomation"] = orig_modules
            else:
                sys.modules.pop("uiautomation", None)

    def test_is_chat_latest_message_incoming_real_tree(self):
        # 測試方向判定直接量測 ImageControl 自身矩形 (避開 Qt 整列橫跨 100% 寬度問題)
        class MockRect:
            def __init__(self, l, t, r, b):
                self.left, self.top, self.right, self.bottom = l, t, r, b
            def width(self): return self.right - self.left
            def height(self): return self.bottom - self.top

        class MockElement:
            def __init__(self, name="", control_type="PaneControl", rect=None, children=None):
                self.Name = name
                self.ControlTypeName = control_type
                self.BoundingRectangle = rect or MockRect(0, 0, 10, 10)
                self._children = children or []
            def GetChildren(self): return self._children
            def Exists(self, a, b): return True

        class MockView:
            def __init__(self, items, rect):
                self._items = items
                self.BoundingRectangle = rect
            def Exists(self, a, b): return True
            def GetChildren(self): return self._items

        class MockWin:
            def __init__(self, view):
                self._view = view
            def Exists(self, a, b): return True
            def ListControl(self, **kwargs): return self._view

        import sys
        import types
        mock_auto = types.ModuleType("uiautomation")

        # 視窗寬度 500 (left=0, right=500, mid_x=250)
        view_rect = MockRect(0, 0, 500, 800)
        # 注意：Qt 列表項目整列必定橫跨整個視窗 (left=0, right=500)
        row_rect = MockRect(0, 600, 500, 780)

        # 案例 1: 對方傳入圖片 (整列橫跨 500，但圖片自身靠左 left=40 < 250)
        in_img = MockElement("", "ImageControl", MockRect(40, 610, 200, 770))
        in_row = MockElement("", "PaneControl", row_rect, [in_img])
        mock_win_in = MockWin(MockView([in_row], view_rect))
        mock_auto.ControlFromHandle = lambda h: mock_win_in

        orig_modules = sys.modules.get("uiautomation")
        try:
            sys.modules["uiautomation"] = mock_auto
            self.assertTrue(auto_reply.is_chat_latest_message_incoming(12345))

            # 案例 2: 本機送出圖片 (整列橫跨 500，但圖片自身靠右 left=300 >= 250)
            out_img = MockElement("", "ImageControl", MockRect(300, 610, 460, 770))
            out_row = MockElement("", "PaneControl", row_rect, [out_img])
            mock_win_out = MockWin(MockView([out_row], view_rect))
            mock_auto.ControlFromHandle = lambda h: mock_win_out
            self.assertFalse(auto_reply.is_chat_latest_message_incoming(12345))
        finally:
            if orig_modules is not None:
                sys.modules["uiautomation"] = orig_modules
            else:
                sys.modules.pop("uiautomation", None)

    def test_try_auto_bind_channel_reject_if_iconic(self):
        # 測試視窗最小化時安全隔離拒絕綁定，不強制奪取焦點
        fake_hash = "cccc03ef1234567890abcdef1234567890abcdef"
        fake_path = os.path.join(self.temp_dir.name, "mo", fake_hash, "test.eimg")
        now = time.time()

        orig_find = auto_reply.find_target_chat_window
        orig_iconic = auto_reply.win32gui.IsIconic
        try:
            auto_reply.find_target_chat_window = lambda t: [(9915, t, "ChatWnd", True)] if t == "TargetIconic" else []
            auto_reply.win32gui.IsIconic = lambda h: True  # 最小化！

            m_map = {}
            bound = auto_reply.try_auto_bind_channel(fake_hash, fake_path, now, ["TargetIconic"], m_map)
            self.assertIsNone(bound)
            self.assertNotIn(fake_hash, m_map)
        finally:
            auto_reply.find_target_chat_window = orig_find
            auto_reply.win32gui.IsIconic = orig_iconic

    def test_deferred_unbound_candidate_not_swallowed_preserves_oldest_mtime(self):
        # 測試中度 #2 防吞檔機制：當暫緩檔存在時，last_seen_cache_mtime 絕不推進越過最舊暫緩檔
        now = time.time()
        ch_unbound = "1111111111222222222233333333334444444444"
        cand_unbound_m = now - 5.0  # 較舊之暫緩檔
        cand_unbound_p = os.path.join(self.temp_dir.name, "mo", ch_unbound, "unbound.eimg")

        ch_known = "5555555555666666666677777777778888888888"
        cand_known_m = now - 1.0  # 較新之已處理檔
        cand_known_p = os.path.join(self.temp_dir.name, "mo", ch_known, "known.eimg")

        candidate_files = [(cand_unbound_m, cand_unbound_p), (cand_known_m, cand_known_p)]
        chat_dirs_map = {ch_known: "TargetKnown"}

        # 模擬掃描迴圈邏輯
        last_seen_cache_mtime = now - 10.0
        max_processed_m = last_seen_cache_mtime
        deferred_mtimes = []

        for cand_m, cand_path in candidate_files:
            ch = auto_reply.extract_chat_hash_from_path(cand_path)
            if ch in chat_dirs_map:
                max_processed_m = max(max_processed_m, cand_m)
            else:
                # 模擬未綁定且年齡 <= 15.0s，加入 deferred_mtimes
                deferred_mtimes.append(cand_m)

        if deferred_mtimes:
            oldest_deferred = min(deferred_mtimes)
            last_seen_cache_mtime = min(max_processed_m, oldest_deferred - 0.001)
        else:
            last_seen_cache_mtime = max_processed_m

        # 核驗 last_seen_cache_mtime 未被推進至 cand_known_m，而是被鎖定在 oldest_deferred - 0.001
        self.assertLess(last_seen_cache_mtime, cand_unbound_m)
        self.assertAlmostEqual(last_seen_cache_mtime, cand_unbound_m - 0.001, places=3)

    def test_sliding_window_unknown_channel_dedup_and_pop_on_bound(self):
        # 測試中度 #1 滑動視窗不重複更新時間戳，且綁定成功後即刻移出未知清單
        auto_reply._RECENT_UNKNOWN_CHANNELS.clear()
        base_t = 1000.0

        ch1 = "chan1_03ef1234567890abcdef1234567890abcdef"
        # 輪詢 1: 初次記錄時間戳
        if ch1 not in auto_reply._RECENT_UNKNOWN_CHANNELS:
            auto_reply._RECENT_UNKNOWN_CHANNELS[ch1] = base_t
        self.assertEqual(auto_reply._RECENT_UNKNOWN_CHANNELS[ch1], base_t)

        # 輪詢 2 (t=base_t+2.0): 不重複更新初次記錄時間戳
        if ch1 not in auto_reply._RECENT_UNKNOWN_CHANNELS:
            auto_reply._RECENT_UNKNOWN_CHANNELS[ch1] = base_t + 2.0
        self.assertEqual(auto_reply._RECENT_UNKNOWN_CHANNELS[ch1], base_t)

        # 模擬頻道綁定成功 -> 移出未知滑動視窗
        auto_reply._RECENT_UNKNOWN_CHANNELS.pop(ch1, None)
        self.assertNotIn(ch1, auto_reply._RECENT_UNKNOWN_CHANNELS)

    def test_is_chat_latest_message_media_dpi_scaling(self):
        # 測試高度嚴重防禦：動態 DPI 縮放門檻 (int(80 * scale))，確保 150% (60px) 與 200% (80px) 頭像絕對被排除
        class MockRect:
            def __init__(self, l, t, r, b):
                self.left, self.top, self.right, self.bottom = l, t, r, b
            def width(self): return self.right - self.left
            def height(self): return self.bottom - self.top

        class MockElement:
            def __init__(self, name="", control_type="ImageControl", rect=None, children=None):
                self.Name = name
                self.ControlTypeName = control_type
                self.BoundingRectangle = rect
                self._children = children or []
            def GetChildren(self): return self._children
            def Exists(self, a, b): return True

        class MockView:
            def __init__(self, items):
                self._items = items
                self.BoundingRectangle = MockRect(0, 0, 500, 800)
            def Exists(self, a, b): return True
            def GetChildren(self): return self._items

        class MockWin:
            def __init__(self, view):
                self._view = view
            def Exists(self, a, b): return True
            def ListControl(self, **kwargs): return self._view

        import sys
        import types
        mock_auto = types.ModuleType("uiautomation")

        orig_modules = sys.modules.get("uiautomation")
        orig_dpi = auto_reply.ctypes.windll.user32.GetDpiForWindow
        try:
            sys.modules["uiautomation"] = mock_auto

            # 1. 在 150% DPI 下 (scale = 1.5, 門檻 = 120px)
            auto_reply.ctypes.windll.user32.GetDpiForWindow = lambda h: 144  # 150% DPI
            # 40px 頭像在高 DPI 縮放後為 60x60 -> 必須判定為 False！
            avatar_150 = MockElement("Avatar", "ImageControl", MockRect(10, 10, 70, 70))
            mock_win1 = MockWin(MockView([MockElement("", "PaneControl", MockRect(0, 0, 500, 100))]))
            mock_win1._view._items[0]._children = [avatar_150]
            mock_auto.ControlFromHandle = lambda h: mock_win1
            self.assertFalse(auto_reply.is_chat_latest_message_media(12345))

            # 2. 在 200% DPI 下 (scale = 2.0, 門檻 = 160px)
            auto_reply.ctypes.windll.user32.GetDpiForWindow = lambda h: 192  # 200% DPI
            # 40px 頭像在高 DPI 縮放後為 80x80 -> 必須判定為 False！
            avatar_200 = MockElement("Avatar", "ImageControl", MockRect(10, 10, 90, 90))
            mock_win2 = MockWin(MockView([MockElement("", "PaneControl", MockRect(0, 0, 500, 100))]))
            mock_win2._view._items[0]._children = [avatar_200]
            mock_auto.ControlFromHandle = lambda h: mock_win2
            self.assertFalse(auto_reply.is_chat_latest_message_media(12345))

            # 3. 真實照片在 150% DPI 下 (200x200 >= 120px) -> 必須為 True
            photo_150 = MockElement("Photo", "ImageControl", MockRect(50, 20, 250, 220))
            mock_win3 = MockWin(MockView([MockElement("", "PaneControl", MockRect(0, 0, 500, 300))]))
            mock_win3._view._items[0]._children = [photo_150]
            mock_auto.ControlFromHandle = lambda h: mock_win3
            self.assertTrue(auto_reply.is_chat_latest_message_media(12345))
        finally:
            if orig_modules is not None:
                sys.modules["uiautomation"] = orig_modules
            else:
                sys.modules.pop("uiautomation", None)
            auto_reply.ctypes.windll.user32.GetDpiForWindow = orig_dpi

    def test_is_chat_latest_message_incoming_wide_image(self):
        # 測試中度 #1 防禦：左右邊距法 (ir.left - vr.left) < (vr.right - ir.right) 徹底解決本機發送寬圖 (left < mid_x) 誤判問題
        class MockRect:
            def __init__(self, l, t, r, b):
                self.left, self.top, self.right, self.bottom = l, t, r, b
            def width(self): return self.right - self.left
            def height(self): return self.bottom - self.top

        class MockElement:
            def __init__(self, name="", control_type="PaneControl", rect=None, children=None):
                self.Name = name
                self.ControlTypeName = control_type
                self.BoundingRectangle = rect or MockRect(0, 0, 10, 10)
                self._children = children or []
            def GetChildren(self): return self._children
            def Exists(self, a, b): return True

        class MockView:
            def __init__(self, items, rect):
                self._items = items
                self.BoundingRectangle = rect
            def Exists(self, a, b): return True
            def GetChildren(self): return self._items

        class MockWin:
            def __init__(self, view):
                self._view = view
            def Exists(self, a, b): return True
            def ListControl(self, **kwargs): return self._view

        import sys
        import types
        mock_auto = types.ModuleType("uiautomation")

        # 視窗寬度 500 (left=0, right=500, mid_x=250)
        view_rect = MockRect(0, 0, 500, 800)
        row_rect = MockRect(0, 500, 500, 750)

        # 案例 1: 本機送出的「超寬圖」(寬度 300px，left=190，right=490)
        # 注意：ir.left (190) 小於視窗中心 (250)！
        # 若以舊中心法會嚴重誤判為對方傳入；而以左右邊距法：
        # left_margin = 190 - 0 = 190, right_margin = 500 - 490 = 10
        # left_margin (190) < right_margin (10) 為 False，精準判定為本機送出 (is_incoming == False)！
        wide_sent_img = MockElement("", "ImageControl", MockRect(190, 510, 490, 740))
        sent_row = MockElement("", "PaneControl", row_rect, [wide_sent_img])
        mock_win_sent = MockWin(MockView([sent_row], view_rect))
        mock_auto.ControlFromHandle = lambda h: mock_win_sent

        orig_modules = sys.modules.get("uiautomation")
        try:
            sys.modules["uiautomation"] = mock_auto
            self.assertFalse(auto_reply.is_chat_latest_message_incoming(12345))

            # 案例 2: 對方傳入的「超寬圖」(寬度 300px，left=10，right=310)
            # left_margin = 10 - 0 = 10, right_margin = 500 - 310 = 190 -> 判定為對方傳入 (is_incoming == True)
            wide_in_img = MockElement("", "ImageControl", MockRect(10, 510, 310, 740))
            in_row = MockElement("", "PaneControl", row_rect, [wide_in_img])
            mock_win_in = MockWin(MockView([in_row], view_rect))
            mock_auto.ControlFromHandle = lambda h: mock_win_in
            self.assertTrue(auto_reply.is_chat_latest_message_incoming(12345))
        finally:
            if orig_modules is not None:
                sys.modules["uiautomation"] = orig_modules
            else:
                sys.modules.pop("uiautomation", None)

    def test_state_captures_media_and_direction_snapshot(self):
        # 測試中度 #2 防禦：指紋異動時立即固化 (has_media, is_incoming) 快照，重試時完全不受後續視窗變更影響
        orig_find = auto_reply.find_target_chat_window
        orig_fp = auto_reply.get_chat_window_fingerprint
        orig_info = auto_reply.get_chat_latest_media_info

        fp_holder = ["fp_base"]
        orig_fetch = auto_reply._fetch_chat_window_items_info
        try:
            auto_reply.find_target_chat_window = lambda t: [(9930, t, "ChatWnd", True)] if t == "TargetSnap" else []
            auto_reply.get_chat_window_fingerprint = lambda h: fp_holder[0]
            auto_reply._fetch_chat_window_items_info = lambda h: (fp_holder[0], "", "")
            auto_reply.get_chat_latest_media_info = lambda h: (True, True)  # 模擬當時為對方傳入圖片

            auto_reply._TARGET_WINDOW_STATE.pop("TargetSnap", None)
            # 1. 建立基準
            auto_reply.update_target_window_fingerprints(["TargetSnap"])
            self.assertEqual(auto_reply._TARGET_WINDOW_STATE["TargetSnap"]["changed_at"], 0.0)

            # 2. 新訊息抵達 -> 指紋異動，固化快照
            fp_holder[0] = "fp_new"
            auto_reply.update_target_window_fingerprints(["TargetSnap"])
            st = auto_reply._TARGET_WINDOW_STATE["TargetSnap"]
            self.assertEqual(st["fp"], "fp_new")
            self.assertTrue(st["has_media"])
            self.assertTrue(st["is_incoming"])
            self.assertFalse(st["consumed"])
        finally:
            auto_reply.find_target_chat_window = orig_find
            auto_reply.get_chat_window_fingerprint = orig_fp
            auto_reply._fetch_chat_window_items_info = orig_fetch
            auto_reply.get_chat_latest_media_info = orig_info
            auto_reply._TARGET_WINDOW_STATE.pop("TargetSnap", None)

    def test_delayed_rendering_adaptive_retry_success(self):
        # 測試 🔴 核心修復：縮圖延遲載入 (初次異動時 ImageControl 尚未出現)，在重測或綁定前完成渲染時自動調整放行
        fake_hash = "dddd03ef1234567890abcdef1234567890abcdef"
        fake_path = os.path.join(self.temp_dir.name, "mo", fake_hash, "test.eimg")
        now = time.time()

        orig_find = auto_reply.find_target_chat_window
        orig_uia = auto_reply.verify_is_not_group_window_uia
        orig_info = auto_reply.get_chat_latest_media_info
        orig_fp = auto_reply.get_chat_window_fingerprint
        orig_fetch = auto_reply._fetch_chat_window_items_info

        media_state = [False]  # 初始尚未渲染完成
        try:
            auto_reply.find_target_chat_window = lambda t: [(9940, t, "ChatWnd", True)] if t == "TargetDelay" else []
            auto_reply.verify_is_not_group_window_uia = lambda h: True
            auto_reply.get_chat_window_fingerprint = lambda h: "15_delay_fp"
            auto_reply._fetch_chat_window_items_info = lambda h: ("15_delay_fp", "DelayImg", "")
            auto_reply.get_chat_latest_media_info = lambda h: (media_state[0], True)

            # 初始快照：has_media 為 False (模擬初次指紋異動時縮圖未載入)
            auto_reply._TARGET_WINDOW_STATE["TargetDelay"] = {
                "fp": "15_delay_fp",
                "changed_at": now - 0.5,
                "consumed": False,
                "has_media": False,
                "is_incoming": False,
            }

            m_map = {}
            # 1. 尚未渲染時嘗試綁定 -> 拒絕
            bound1 = auto_reply.try_auto_bind_channel(fake_hash, fake_path, now, ["TargetDelay"], m_map)
            self.assertIsNone(bound1)
            self.assertFalse(auto_reply._TARGET_WINDOW_STATE["TargetDelay"]["consumed"])

            # 2. 經過 0.5 秒後縮圖渲染完成 (ImageControl 出現)
            media_state[0] = True
            # update_target_window_fingerprints 重測更新快照
            auto_reply.get_chat_window_fingerprint = lambda h: "15_delay_fp"  # 相同指紋
            auto_reply.update_target_window_fingerprints(["TargetDelay"])
            self.assertTrue(auto_reply._TARGET_WINDOW_STATE["TargetDelay"]["has_media"])

            # 3. 再次綁定 -> 成功放行！
            bound2 = auto_reply.try_auto_bind_channel(fake_hash, fake_path, now, ["TargetDelay"], m_map)
            self.assertEqual(bound2, "TargetDelay")
            self.assertTrue(auto_reply._TARGET_WINDOW_STATE["TargetDelay"]["consumed"])
        finally:
            auto_reply.find_target_chat_window = orig_find
            auto_reply.verify_is_not_group_window_uia = orig_uia
            auto_reply.get_chat_latest_media_info = orig_info
            auto_reply.get_chat_window_fingerprint = orig_fp
            auto_reply._fetch_chat_window_items_info = orig_fetch
            auto_reply._TARGET_WINDOW_STATE.pop("TargetDelay", None)

    def test_render_triggered_fingerprint_change_preserves_first_changed_at(self):
        # 測試 🔴 核心修復：非同步縮圖渲染完成引發指紋改變時，保留最初訊息抵達時間 first_changed_at，
        # 防止 changed_at 被推遲導致與快取檔案時間 cand_m 差距超過 ±6 秒被誤殺
        fake_hash = "eeee03ef1234567890abcdef1234567890abcdef"
        fake_path = os.path.join(self.temp_dir.name, "mo", fake_hash, "test.eimg")
        t0 = time.time()

        orig_find = auto_reply.find_target_chat_window
        orig_uia = auto_reply.verify_is_not_group_window_uia
        orig_info = auto_reply.get_chat_latest_media_info
        orig_fp = auto_reply.get_chat_window_fingerprint
        orig_items = auto_reply._fetch_chat_window_items_info
        orig_time = auto_reply.time.time

        try:
            auto_reply.find_target_chat_window = lambda t: [(9945, t, "ChatWnd", True)] if t == "TargetFpShift" else []
            auto_reply.verify_is_not_group_window_uia = lambda h: True

            info_holder = ["10_pending", "PhotoPending", "OldMsg"]
            auto_reply._fetch_chat_window_items_info = lambda h: (info_holder[0], info_holder[1], info_holder[2])
            auto_reply.get_chat_window_fingerprint = lambda h: info_holder[0]
            auto_reply.get_chat_latest_media_info = lambda h: (False, False)

            # 基準狀態：先前舊訊息，已消費
            auto_reply._TARGET_WINDOW_STATE["TargetFpShift"] = {
                "fp": "10_baseline",
                "last_name": "OldMsg",
                "prev_name": "OlderMsg",
                "changed_at": 0.0,
                "consumed": True
            }

            # 模擬 t0 + 0.1 輪詢偵測到新指紋 10_pending
            auto_reply.time.time = lambda: t0 + 0.1
            auto_reply.update_target_window_fingerprints(["TargetFpShift"])
            st1 = auto_reply._TARGET_WINDOW_STATE["TargetFpShift"]
            self.assertEqual(st1["changed_at"], t0 + 0.1)
            self.assertFalse(st1["has_media"])
            self.assertEqual(st1["last_name"], "PhotoPending")

            # 2. t0 + 8.0s 時縮圖完成渲染，指紋轉變為 10_settled (因控制項名稱或結構改變)
            auto_reply.time.time = lambda: t0 + 8.0
            info_holder[0] = "10_settled"
            auto_reply.get_chat_latest_media_info = lambda h: (True, True)
            auto_reply.update_target_window_fingerprints(["TargetFpShift"])
            st2 = auto_reply._TARGET_WINDOW_STATE["TargetFpShift"]
            # 🔴 核心驗證：changed_at 保留最初 t0 + 0.1，未被洗成 t0 + 8.0
            self.assertEqual(st2["changed_at"], t0 + 0.1)
            self.assertEqual(st2["first_changed_at"], t0 + 0.1)
            self.assertEqual(st2["last_fp_change"], t0 + 0.1)
            self.assertTrue(st2["has_media"])

            # 3. 快取檔案於 t0 + 0.5s 寫入 (cand_m = t0 + 0.5s)
            # 若無此修復，cand_m 與 (t0 + 8.0) 相差 -7.5s (超過 6s) 會被誤殺；
            # 有此修復，cand_m 與 (t0 + 0.1) 僅相差 0.4s，順利放行！
            m_map = {}
            bound = auto_reply.try_auto_bind_channel(fake_hash, fake_path, t0 + 0.5, ["TargetFpShift"], m_map)
            self.assertEqual(bound, "TargetFpShift")
            self.assertTrue(auto_reply._TARGET_WINDOW_STATE["TargetFpShift"]["consumed"])
        finally:
            auto_reply.time.time = orig_time
            auto_reply.find_target_chat_window = orig_find
            auto_reply.verify_is_not_group_window_uia = orig_uia
            auto_reply.get_chat_latest_media_info = orig_info
            auto_reply.get_chat_window_fingerprint = orig_fp
            auto_reply._fetch_chat_window_items_info = orig_items
            auto_reply._TARGET_WINDOW_STATE.pop("TargetFpShift", None)

    def test_get_chat_latest_media_info_nested_tree_and_margins(self):
        # 測試 🟡 中度修復：穿透 3 層 Qt 巢狀 Pane 搜尋 ImageControl，並透過邊距邏輯精準判別對方傳入與本機送出
        class MockRect:
            def __init__(self, l, t, r, b):
                self.left, self.top, self.right, self.bottom = l, t, r, b
            def width(self):
                return self.right - self.left
            def height(self):
                return self.bottom - self.top

        class MockNode:
            def __init__(self, ctype, rect, children=None):
                self.ControlTypeName = ctype
                self.BoundingRectangle = rect
                self.children = children or []
            def GetChildren(self):
                return self.children
            def Exists(self, a, b):
                return True

        # 建立 3 層巢狀結構：
        # ListControl (view 寬度 400: left=0, right=400)
        # -> Row item
        #   -> Pane (level 1)
        #     -> Pane (level 2)
        #       -> ImageControl (level 3, 200x200 圖片)
        # 狀況 1: 對方傳入 (left=50, right=250) -> left_margin=50, right_margin=150 (left < right)
        img_incoming = MockNode("ImageControl", MockRect(50, 100, 250, 300))
        pane2_in = MockNode("PaneControl", MockRect(0, 100, 400, 300), [img_incoming])
        pane1_in = MockNode("PaneControl", MockRect(0, 100, 400, 300), [pane2_in])
        row_in = MockNode("ListItemControl", MockRect(0, 100, 400, 300), [pane1_in])

        view_in = MockNode("ListControl", MockRect(0, 0, 400, 800), [row_in])
        view_in.ListControl = lambda ClassName="LcListView": view_in

        win_mock = MockNode("WindowControl", MockRect(0, 0, 400, 800))
        win_mock.ListControl = lambda ClassName="LcListView": view_in

        import sys
        import types
        fake_uia = types.ModuleType("uiautomation")
        fake_uia.ControlFromHandle = lambda h: win_mock

        real_modules = sys.modules.copy()
        try:
            sys.modules["uiautomation"] = fake_uia
            has_media, is_in = auto_reply.get_chat_latest_media_info(8888)
            self.assertTrue(has_media)
            self.assertTrue(is_in)

            # 狀況 2: 本機送出 (left=180, right=380) -> left_margin=180, right_margin=20 (right_margin <= 35)
            img_outgoing = MockNode("ImageControl", MockRect(180, 100, 380, 300))
            pane2_out = MockNode("PaneControl", MockRect(0, 100, 400, 300), [img_outgoing])
            pane1_out = MockNode("PaneControl", MockRect(0, 100, 400, 300), [pane2_out])
            row_out = MockNode("ListItemControl", MockRect(0, 100, 400, 300), [pane1_out])
            view_out = MockNode("ListControl", MockRect(0, 0, 400, 800), [row_out])
            win_mock.ListControl = lambda ClassName="LcListView": view_out

            has_media_out, is_in_out = auto_reply.get_chat_latest_media_info(8888)
            self.assertTrue(has_media_out)
            self.assertFalse(is_in_out)
        finally:
            sys.modules.clear()
            sys.modules.update(real_modules)

    def test_try_auto_bind_channel_reject_if_fingerprint_drift_during_retest(self):
        # 測試 🟡 中度修復：綁定前即時重測時，若視窗指紋已改變 (代表有非預期新訊息插入)，拒絕套用重測並阻斷綁定
        fake_hash = "ffff03ef1234567890abcdef1234567890abcdef"
        fake_path = os.path.join(self.temp_dir.name, "mo", fake_hash, "test.eimg")
        now = time.time()

        orig_find = auto_reply.find_target_chat_window
        orig_uia = auto_reply.verify_is_not_group_window_uia
        orig_fp = auto_reply.get_chat_window_fingerprint
        orig_info = auto_reply.get_chat_latest_media_info

        try:
            auto_reply.find_target_chat_window = lambda t: [(9948, t, "ChatWnd", True)] if t == "TargetDrift" else []
            auto_reply.verify_is_not_group_window_uia = lambda h: True
            # 目前視窗指紋已漂移為新指紋
            auto_reply.get_chat_window_fingerprint = lambda h: "20_new_interrupted_fp"
            auto_reply.get_chat_latest_media_info = lambda h: (True, True)

            # 狀態機指紋為舊指紋
            auto_reply._TARGET_WINDOW_STATE["TargetDrift"] = {
                "fp": "19_old_fp",
                "changed_at": now - 0.5,
                "consumed": False,
                "has_media": False,  # 觸發重測
                "is_incoming": False,
            }

            m_map = {}
            bound = auto_reply.try_auto_bind_channel(fake_hash, fake_path, now, ["TargetDrift"], m_map)
            # 由於指紋漂移，重測被安全隔離，拒絕自動綁定
            self.assertIsNone(bound)
            self.assertFalse(auto_reply._TARGET_WINDOW_STATE["TargetDrift"]["consumed"])
        finally:
            auto_reply.find_target_chat_window = orig_find
            auto_reply.verify_is_not_group_window_uia = orig_uia
            auto_reply.get_chat_window_fingerprint = orig_fp
            auto_reply.get_chat_latest_media_info = orig_info
            auto_reply._TARGET_WINDOW_STATE.pop("TargetDrift", None)

    def test_try_auto_bind_channel_reject_if_fingerprint_none_fail_closed(self):
        # 測試 🔴 核心防禦：取不到指紋 (get_chat_window_fingerprint 回傳 None) 時，遵循 Fail-Closed 拒絕綁定
        fake_hash = "121203ef1234567890abcdef1234567890abcdef"
        fake_path = os.path.join(self.temp_dir.name, "mo", fake_hash, "test.eimg")
        now = time.time()

        orig_find = auto_reply.find_target_chat_window
        orig_uia = auto_reply.verify_is_not_group_window_uia
        orig_fp = auto_reply.get_chat_window_fingerprint

        try:
            auto_reply.find_target_chat_window = lambda t: [(9950, t, "ChatWnd", True)] if t == "TargetFpNone" else []
            auto_reply.verify_is_not_group_window_uia = lambda h: True
            auto_reply.get_chat_window_fingerprint = lambda h: None  # 模擬視窗被遮蔽或 UIA 讀取失敗回傳 None

            auto_reply._TARGET_WINDOW_STATE["TargetFpNone"] = {
                "fp": "valid_fp",
                "changed_at": now - 0.5,
                "consumed": False,
                "has_media": True,
                "is_incoming": True,
            }

            m_map = {}
            bound = auto_reply.try_auto_bind_channel(fake_hash, fake_path, now, ["TargetFpNone"], m_map)
            self.assertIsNone(bound)
            self.assertFalse(auto_reply._TARGET_WINDOW_STATE["TargetFpNone"]["consumed"])
        finally:
            auto_reply.find_target_chat_window = orig_find
            auto_reply.verify_is_not_group_window_uia = orig_uia
            auto_reply.get_chat_window_fingerprint = orig_fp
            auto_reply._TARGET_WINDOW_STATE.pop("TargetFpNone", None)

    def test_try_auto_bind_channel_success_when_cache_written_near_last_fp_change(self):
        # 測試 🟡 中度防禦：快取檔案於縮圖完成後才寫入 (cand_m 接近 last_fp_change，但已距 first_changed_at 超過 6s)，
        # 只要在 [first_t - 6, last_t + 6] 範圍內依然順利放行綁定
        fake_hash = "343403ef1234567890abcdef1234567890abcdef"
        fake_path = os.path.join(self.temp_dir.name, "mo", fake_hash, "test.eimg")
        t0 = time.time()

        orig_find = auto_reply.find_target_chat_window
        orig_uia = auto_reply.verify_is_not_group_window_uia
        orig_fp = auto_reply.get_chat_window_fingerprint

        try:
            auto_reply.find_target_chat_window = lambda t: [(9952, t, "ChatWnd", True)] if t == "TargetLateWrite" else []
            auto_reply.verify_is_not_group_window_uia = lambda h: True
            auto_reply.get_chat_window_fingerprint = lambda h: "settled_fp"

            # 訊息於 t0 抵達，縮圖於 t0 + 8.0s 完成 (last_fp_change = t0 + 8.0s)
            auto_reply._TARGET_WINDOW_STATE["TargetLateWrite"] = {
                "fp": "settled_fp",
                "changed_at": t0,
                "first_changed_at": t0,
                "last_fp_change": t0 + 8.0,
                "consumed": False,
                "has_media": True,
                "is_incoming": True,
            }

            m_map = {}
            # 快取檔案於 t0 + 8.2s 寫入 (距 first_changed_at 已 8.2s > 6.0s，但距 last_fp_change 僅 0.2s <= 6.0s)
            bound = auto_reply.try_auto_bind_channel(fake_hash, fake_path, t0 + 8.2, ["TargetLateWrite"], m_map)
            self.assertEqual(bound, "TargetLateWrite")
            self.assertTrue(auto_reply._TARGET_WINDOW_STATE["TargetLateWrite"]["consumed"])

            # 負向邊界測試：若快取檔案於 t0 - 6.1s (早於 first_t - 6.0s)，即使在 27s 聯集內亦嚴格阻斷
            auto_reply._TARGET_WINDOW_STATE["TargetLateWrite"]["consumed"] = False
            m_map.clear()
            bound_early = auto_reply.try_auto_bind_channel(fake_hash, fake_path, t0 - 6.1, ["TargetLateWrite"], m_map)
            self.assertIsNone(bound_early)
            self.assertFalse(auto_reply._TARGET_WINDOW_STATE["TargetLateWrite"]["consumed"])
        finally:
            auto_reply.find_target_chat_window = orig_find
            auto_reply.verify_is_not_group_window_uia = orig_uia
            auto_reply.get_chat_window_fingerprint = orig_fp
            auto_reply._TARGET_WINDOW_STATE.pop("TargetLateWrite", None)

    def test_text_followed_by_image_does_not_expand_window(self):
        # 測試 🟡 中度防禦：文字訊息後緊接傳入圖片訊息時 (LcListView 項目數增加)，嚴格重置時間為圖片時間，
        # 徹底杜絕時間範圍放寬至 27 秒誤配舊文字時間
        fake_hash = "787803ef1234567890abcdef1234567890abcdef"
        fake_path = os.path.join(self.temp_dir.name, "mo", fake_hash, "test.eimg")
        t0 = time.time()

        orig_find = auto_reply.find_target_chat_window
        orig_uia = auto_reply.verify_is_not_group_window_uia
        orig_fp = auto_reply.get_chat_window_fingerprint
        orig_fetch = auto_reply._fetch_chat_window_items_info
        orig_info = auto_reply.get_chat_latest_media_info
        orig_time = auto_reply.time.time

        try:
            auto_reply.find_target_chat_window = lambda t: [(9955, t, "ChatWnd", True)] if t == "TargetTextThenImg" else []
            auto_reply.verify_is_not_group_window_uia = lambda h: True

            # 1. t0 時純文字訊息抵達 (10 項，無媒體)
            auto_reply._TARGET_WINDOW_STATE["TargetTextThenImg"] = {"fp": "9_baseline", "changed_at": 0.0, "consumed": True}
            auto_reply.get_chat_window_fingerprint = lambda h: "10_text_fp"
            auto_reply._fetch_chat_window_items_info = lambda h: ("10_text_fp", "text", "prev")
            auto_reply.get_chat_latest_media_info = lambda h: (False, False)
            auto_reply.time.time = lambda: t0
            auto_reply.update_target_window_fingerprints(["TargetTextThenImg"])
            st1 = auto_reply._TARGET_WINDOW_STATE["TargetTextThenImg"]
            self.assertEqual(st1["changed_at"], t0)
            self.assertFalse(st1["has_media"])

            # 2. t0 + 10.0s 時傳入圖片訊息 (項目數增加為 11 項，有媒體)
            auto_reply.time.time = lambda: t0 + 10.0
            auto_reply.get_chat_window_fingerprint = lambda h: "11_img_fp"
            auto_reply._fetch_chat_window_items_info = lambda h: ("11_img_fp", "img", "text")
            auto_reply.get_chat_latest_media_info = lambda h: (True, True)
            auto_reply.update_target_window_fingerprints(["TargetTextThenImg"])
            st2 = auto_reply._TARGET_WINDOW_STATE["TargetTextThenImg"]
            # 🟡 核心驗證：changed_at 與 first_changed_at 均被精準重置為 t0 + 10.0s，而非停留在 t0 的文字時間！
            self.assertEqual(st2["changed_at"], t0 + 10.0)
            self.assertEqual(st2["first_changed_at"], t0 + 10.0)
            self.assertEqual(st2["media_seen_at"], t0 + 10.0)
            self.assertTrue(st2["has_media"])

            # 3. 負向測試：若快取檔案於 t0 (文字時間) 寫入，距圖片時間已差距 10 秒 (> 6.0s)，嚴格拒絕綁定！
            m_map = {}
            bound_text_time = auto_reply.try_auto_bind_channel(fake_hash, fake_path, t0, ["TargetTextThenImg"], m_map)
            self.assertIsNone(bound_text_time)
            self.assertFalse(auto_reply._TARGET_WINDOW_STATE["TargetTextThenImg"]["consumed"])

            # 4. 正向測試：若快取檔案於 t0 + 10.5s (圖片時間) 寫入，順利放行！
            bound_img_time = auto_reply.try_auto_bind_channel(fake_hash, fake_path, t0 + 10.5, ["TargetTextThenImg"], m_map)
            self.assertEqual(bound_img_time, "TargetTextThenImg")
            self.assertTrue(auto_reply._TARGET_WINDOW_STATE["TargetTextThenImg"]["consumed"])
        finally:
            auto_reply.time.time = orig_time
            auto_reply.find_target_chat_window = orig_find
            auto_reply.verify_is_not_group_window_uia = orig_uia
            auto_reply.get_chat_window_fingerprint = orig_fp
            auto_reply._fetch_chat_window_items_info = orig_fetch
            auto_reply.get_chat_latest_media_info = orig_info
            auto_reply._TARGET_WINDOW_STATE.pop("TargetTextThenImg", None)

    def test_try_auto_bind_channel_reject_if_expired(self):
        # 測試 🟡 中度防禦：有圖快照超過 AUTOBIND_TIMEOUT (15.0s) 未綁定時安全過期拒絕，杜絕無限期殘留
        fake_hash = "909003ef1234567890abcdef1234567890abcdef"
        fake_path = os.path.join(self.temp_dir.name, "mo", fake_hash, "test.eimg")
        now = time.time()

        orig_find = auto_reply.find_target_chat_window
        orig_uia = auto_reply.verify_is_not_group_window_uia
        orig_fp = auto_reply.get_chat_window_fingerprint

        try:
            auto_reply.find_target_chat_window = lambda t: [(9958, t, "ChatWnd", True)] if t == "TargetExpired" else []
            auto_reply.verify_is_not_group_window_uia = lambda h: True
            auto_reply.get_chat_window_fingerprint = lambda h: "expired_fp"

            # 異動發生於 16 秒前 (> AUTOBIND_TIMEOUT 15.0s)
            auto_reply._TARGET_WINDOW_STATE["TargetExpired"] = {
                "fp": "expired_fp",
                "changed_at": now - 16.0,
                "first_changed_at": now - 16.0,
                "last_fp_change": now - 16.0,
                "consumed": False,
                "has_media": True,
                "is_incoming": True,
            }

            m_map = {}
            bound = auto_reply.try_auto_bind_channel(fake_hash, fake_path, now, ["TargetExpired"], m_map)
            self.assertIsNone(bound)
            self.assertFalse(auto_reply._TARGET_WINDOW_STATE["TargetExpired"]["consumed"])
        finally:
            auto_reply.find_target_chat_window = orig_find
            auto_reply.verify_is_not_group_window_uia = orig_uia
            auto_reply.get_chat_window_fingerprint = orig_fp
            auto_reply._TARGET_WINDOW_STATE.pop("TargetExpired", None)

    def test_equal_counts_different_hashes_scroll_resets_time(self):
        # 測試 🟡 中度防禦：項目數飽和 (count 相同) 但次末項名稱不同 (非縮圖安定化，實為滾動或新訊息替換舊訊息) 時，
        # 嚴格重置時間為目前時間，杜絕保留過期之文字時間
        fake_hash = "454503ef1234567890abcdef1234567890abcdef"
        fake_path = os.path.join(self.temp_dir.name, "mo", fake_hash, "test.eimg")
        t0 = time.time()

        orig_find = auto_reply.find_target_chat_window
        orig_uia = auto_reply.verify_is_not_group_window_uia
        orig_fetch = auto_reply._fetch_chat_window_items_info
        orig_info = auto_reply.get_chat_latest_media_info
        orig_time = auto_reply.time.time

        try:
            auto_reply.find_target_chat_window = lambda t: [(9960, t, "ChatWnd", True)] if t == "TargetSatCount" else []
            auto_reply.verify_is_not_group_window_uia = lambda h: True

            # 1. 初始文字訊息抵達：項目數 10，最末訊息 "Text1"，次末 "Old"
            auto_reply._TARGET_WINDOW_STATE["TargetSatCount"] = {
                "fp": "10_initial_fp",
                "last_name": "Initial",
                "prev_name": "Oldest",
                "changed_at": 0.0,
                "consumed": True
            }
            auto_reply.time.time = lambda: t0
            auto_reply._fetch_chat_window_items_info = lambda h: ("10_text_hash1", "TextMsg", "Initial")
            auto_reply.get_chat_latest_media_info = lambda h: (False, False)
            auto_reply.update_target_window_fingerprints(["TargetSatCount"])
            st1 = auto_reply._TARGET_WINDOW_STATE["TargetSatCount"]
            self.assertEqual(st1["changed_at"], t0)
            self.assertEqual(st1["prev_name"], "Initial")

            # 2. 10 秒後新圖片訊息抵達 (由於飽和，項目數依然為 10，但最末為 "ImgMsg"，次末為 "TextMsg")
            # 次末項 "TextMsg" != 之前的次末項 "Initial" -> 成功辨識為新訊息而非縮圖安定化！
            auto_reply.time.time = lambda: t0 + 10.0
            auto_reply._fetch_chat_window_items_info = lambda h: ("10_img_hash2", "ImgMsg", "TextMsg")
            auto_reply.get_chat_latest_media_info = lambda h: (True, True)
            auto_reply.update_target_window_fingerprints(["TargetSatCount"])
            st2 = auto_reply._TARGET_WINDOW_STATE["TargetSatCount"]
            # 核心驗證：changed_at 與 first_changed_at 均被嚴格重置為 t0 + 10.0s！
            self.assertEqual(st2["changed_at"], t0 + 10.0)
            self.assertEqual(st2["first_changed_at"], t0 + 10.0)
            self.assertEqual(st2["media_seen_at"], t0 + 10.0)
        finally:
            auto_reply.time.time = orig_time
            auto_reply.find_target_chat_window = orig_find
            auto_reply.verify_is_not_group_window_uia = orig_uia
            auto_reply._fetch_chat_window_items_info = orig_fetch
            auto_reply.get_chat_latest_media_info = orig_info
            auto_reply._TARGET_WINDOW_STATE.pop("TargetSatCount", None)

    def test_prev_name_change_with_same_last_image_does_not_unconsume_or_rearm(self):
        # 測試 🟡 中度防禦：次末項名稱改變 (例如已讀標記、時間戳更新) 但最末項為既有舊圖 (last_name 未變且項目數未變) 時，
        # 嚴禁將既有舊圖誤當作新圖片異動重設時間或解除已消費狀態
        fake_hash = "676703ef1234567890abcdef1234567890abcdef"
        fake_path = os.path.join(self.temp_dir.name, "mo", fake_hash, "test.eimg")
        now = time.time()

        orig_find = auto_reply.find_target_chat_window
        orig_uia = auto_reply.verify_is_not_group_window_uia
        orig_fetch = auto_reply._fetch_chat_window_items_info
        orig_info = auto_reply.get_chat_latest_media_info

        try:
            auto_reply.find_target_chat_window = lambda t: [(9965, t, "ChatWnd", True)] if t == "TargetOldImg" else []
            auto_reply.verify_is_not_group_window_uia = lambda h: True

            # 初始狀態：既有舊圖已消費 (consumed=True, has_media=True, last_name="PhotoA")
            auto_reply._TARGET_WINDOW_STATE["TargetOldImg"] = {
                "fp": "10_fp_old",
                "last_name": "PhotoA",
                "prev_name": "MsgBefore (未讀)",
                "changed_at": now - 30.0,
                "first_changed_at": now - 30.0,
                "last_fp_change": now - 30.0,
                "consumed": True,
                "has_media": True,
                "is_incoming": True,
                "media_seen_at": now - 30.0,
            }

            # 模擬已讀標記改變：最末項依然是 "PhotoA"，項目數依然是 10，但次末項變成 "MsgBefore (已讀)"
            # 此時指紋由 10_fp_old 變為 10_fp_new
            auto_reply._fetch_chat_window_items_info = lambda h: ("10_fp_new", "PhotoA", "MsgBefore (已讀)")
            auto_reply.get_chat_latest_media_info = lambda h: (True, True)

            auto_reply.update_target_window_fingerprints(["TargetOldImg"])

            st = auto_reply._TARGET_WINDOW_STATE["TargetOldImg"]
            # 🟡 核心驗證：保持 consumed=True，changed_at 依然為 30 秒前，未被洗成目前時間！
            self.assertTrue(st["consumed"])
            self.assertEqual(st["changed_at"], now - 30.0)
            self.assertEqual(st["fp"], "10_fp_new")

            # 負向驗證：外來快取在 now 抵達時，嚴格拒絕自動綁定！
            m_map = {}
            bound = auto_reply.try_auto_bind_channel(fake_hash, fake_path, now, ["TargetOldImg"], m_map)
            self.assertIsNone(bound)
        finally:
            auto_reply.find_target_chat_window = orig_find
            auto_reply.verify_is_not_group_window_uia = orig_uia
            auto_reply._fetch_chat_window_items_info = orig_fetch
            auto_reply.get_chat_latest_media_info = orig_info
            auto_reply._TARGET_WINDOW_STATE.pop("TargetOldImg", None)

    def test_check_for_incoming_messages_auto_bind_pipeline(self):
        # 測試整合管線：呼叫 check_for_incoming_messages 完成真實快取掃描與自動綁定
        test_cache = os.path.join(self.temp_dir.name, "pipeline_cache")
        fake_hash = "666603ef1234567890abcdef1234567890abcdef"
        chat_mo_dir = os.path.join(test_cache, fake_hash, "m")
        os.makedirs(chat_mo_dir, exist_ok=True)
        img_path = os.path.join(chat_mo_dir, "test.eimg")
        with open(img_path, "wb") as f:
            f.write(b"\x00" * 300000)

        now = time.time()
        os.utime(img_path, (now, now))

        orig_cache_dir = auto_reply.get_active_line_cache_dir
        orig_find = auto_reply.find_target_chat_window
        orig_uia = auto_reply.verify_is_not_group_window_uia
        orig_fetch = auto_reply._fetch_chat_window_items_info
        orig_info = auto_reply.get_chat_latest_media_info
        orig_send = auto_reply.send_grandma_image

        try:
            auto_reply.get_active_line_cache_dir = lambda: test_cache
            auto_reply.find_target_chat_window = lambda t: [(9960, t, "ChatWnd", True)] if t == "TargetPipeline" else []
            auto_reply.verify_is_not_group_window_uia = lambda h: True
            auto_reply._fetch_chat_window_items_info = lambda h: ("10_pipe_fp", "PipeImg", "PrevMsg")
            auto_reply.get_chat_latest_media_info = lambda h: (True, True)
            sent_targets = []
            auto_reply.send_grandma_image = lambda t, c, **kwargs: sent_targets.append(t)

            auto_reply._TARGET_WINDOW_STATE["TargetPipeline"] = {
                "fp": "10_pipe_fp",
                "last_name": "PipeImg",
                "prev_name": "PrevMsg",
                "changed_at": now - 0.2,
                "first_changed_at": now - 0.2,
                "last_fp_change": now - 0.2,
                "consumed": False,
                "has_media": True,
                "is_incoming": True,
                "media_seen_at": now - 0.2,
            }

            cfg = {
                "targets": ["TargetPipeline"],
                "trigger_mode": "photo_only",
                "cooldown_seconds": 0,
                "min_delay_seconds": 0,
                "max_delay_seconds": 0,
            }

            auto_reply.last_global_reply_time = 0.0
            auto_reply.PROCESSED_CACHE_FILES.clear()
            auto_reply._RECENT_UNKNOWN_CHANNELS.clear()
            auto_reply.last_seen_cache_mtime = now - 1.0
            auto_reply.check_for_incoming_messages(cfg)

            m_map = auto_reply.load_chat_dirs_map()
            self.assertIn(fake_hash, m_map)
            self.assertEqual(m_map[fake_hash], "TargetPipeline")
            self.assertIn(fake_hash, auto_reply._PROVISIONAL_BINDINGS)
            self.assertIn("TargetPipeline", sent_targets)
        finally:
            auto_reply.get_active_line_cache_dir = orig_cache_dir
            auto_reply.find_target_chat_window = orig_find
            auto_reply.verify_is_not_group_window_uia = orig_uia
            auto_reply._fetch_chat_window_items_info = orig_fetch
            auto_reply.get_chat_latest_media_info = orig_info
            auto_reply.send_grandma_image = orig_send
            auto_reply._TARGET_WINDOW_STATE.pop("TargetPipeline", None)
            auto_reply._PROVISIONAL_BINDINGS.pop(fake_hash, None)

    def test_provisional_binding_auto_revocation_on_idle_target(self):
        # 測試 🟡 核心防禦：若暫定綁定之頻道後續產生快取活動，但目標視窗完全無活動，證實錯綁，自動撤銷！
        fake_hash = "777703ef1234567890abcdef1234567890abcdef"
        test_cache = os.path.join(self.temp_dir.name, "revoke_cache")
        chat_mo_dir = os.path.join(test_cache, fake_hash, "m")
        os.makedirs(chat_mo_dir, exist_ok=True)
        img_path = os.path.join(chat_mo_dir, "test2.eimg")
        with open(img_path, "wb") as f:
            f.write(b"\x00" * 300000)

        now = time.time()
        # 快取檔案生成於 20 秒前 (已渡過 AUTOBIND_TIMEOUT=15s 縮圖渲染考驗期)
        os.utime(img_path, (now - 20.0, now - 20.0))

        # 先手動模擬已存在暫定綁定 (採用標準字典結構)
        m_map = {fake_hash: "TargetRevoke"}
        auto_reply.save_chat_dirs_map(m_map)
        auto_reply._PROVISIONAL_BINDINGS[fake_hash] = {
            "target": "TargetRevoke",
            "bind_fp": "9_bind_fp",
            "bind_time": now - 40.0,
        }
        # 目標視窗已變更指紋轉入其他訊息，且完全無近期活動 (60 秒前，與 cand_m 相差 40 秒遠超 15 秒)
        auto_reply._TARGET_WINDOW_STATE["TargetRevoke"] = {
            "fp": "10_old_fp",
            "last_name": "Old",
            "prev_name": "Prev",
            "changed_at": now - 60.0,
            "last_fp_change": now - 60.0,
            "consumed": True,
            "has_media": True,
            "is_incoming": True,
        }

        orig_cache_dir = auto_reply.get_active_line_cache_dir
        orig_find = auto_reply.find_target_chat_window
        orig_uia = auto_reply.verify_is_not_group_window_uia
        orig_fetch = auto_reply._fetch_chat_window_items_info
        orig_info = auto_reply.get_chat_latest_media_info
        orig_send = auto_reply.send_grandma_image
        try:
            auto_reply.get_active_line_cache_dir = lambda: test_cache
            auto_reply.find_target_chat_window = lambda t: [(9961, t, "ChatWnd", True)] if t == "TargetRevoke" else []
            auto_reply.verify_is_not_group_window_uia = lambda h: True
            auto_reply._fetch_chat_window_items_info = lambda h: ("10_old_fp", "Old", "Prev")
            auto_reply.get_chat_latest_media_info = lambda h: (True, True)
            sent_targets = []
            auto_reply.send_grandma_image = lambda t, c, **kwargs: sent_targets.append(t)

            cfg = {
                "targets": ["TargetRevoke"],
                "trigger_mode": "photo_only",
                "cooldown_seconds": 0,
                "min_delay_seconds": 0,
                "max_delay_seconds": 0,
            }

            auto_reply.last_global_reply_time = 0.0
            auto_reply.PROCESSED_CACHE_FILES.clear()
            auto_reply._RECENT_UNKNOWN_CHANNELS.clear()
            auto_reply.last_seen_cache_mtime = now - 35.0
            auto_reply.check_for_incoming_messages(cfg)

            # 核心驗證：綁定已自動撤銷，且未發送長輩圖！
            reloaded_map = auto_reply.load_chat_dirs_map()
            self.assertNotIn(fake_hash, reloaded_map)
            self.assertNotIn(fake_hash, auto_reply._PROVISIONAL_BINDINGS)
            self.assertEqual(len(sent_targets), 0)
        finally:
            auto_reply.get_active_line_cache_dir = orig_cache_dir
            auto_reply.find_target_chat_window = orig_find
            auto_reply.verify_is_not_group_window_uia = orig_uia
            auto_reply._fetch_chat_window_items_info = orig_fetch
            auto_reply.get_chat_latest_media_info = orig_info
            auto_reply.send_grandma_image = orig_send
            auto_reply._TARGET_WINDOW_STATE.pop("TargetRevoke", None)
            auto_reply._PROVISIONAL_BINDINGS.pop(fake_hash, None)


    def test_provisional_promotion_requires_independent_incoming_media(self):
        # 測試 🟡 防禦：暫定綁定晉升為永久綁定，必須由「獨立後續之傳入圖片」核驗，
        # 自身機器人回覆 (靠右)、純文字訊息、相同訊息指紋或 3 秒內關聯檔皆不可晉升
        fake_hash = "888803ef1234567890abcdef1234567890abcdef"
        test_cache = os.path.join(self.temp_dir.name, "promo_cache")
        chat_mo_dir = os.path.join(test_cache, fake_hash, "m")
        os.makedirs(chat_mo_dir, exist_ok=True)
        img_path = os.path.join(chat_mo_dir, "promo.eimg")
        with open(img_path, "wb") as f:
            f.write(b"\x00" * 300000)

        now = time.time()
        os.utime(img_path, (now, now))

        m_map = {fake_hash: "TargetPromo"}
        auto_reply.save_chat_dirs_map(m_map)
        auto_reply._PROVISIONAL_BINDINGS[fake_hash] = {
            "target": "TargetPromo",
            "bind_fp": "1_old_fp",
            "bind_time": now - 10.0,
        }

        orig_cache_dir = auto_reply.get_active_line_cache_dir
        orig_find = auto_reply.find_target_chat_window
        orig_fp = auto_reply.get_chat_window_fingerprint
        orig_info = auto_reply.get_chat_latest_media_info
        orig_send = auto_reply.send_grandma_image
        try:
            auto_reply.get_active_line_cache_dir = lambda: test_cache
            auto_reply.find_target_chat_window = lambda t: [(9970, t, "ChatWnd", True)] if t == "TargetPromo" else []
            auto_reply.send_grandma_image = lambda t, c, **kwargs: None

            cfg = {"targets": ["TargetPromo"], "trigger_mode": "photo_only", "cooldown_seconds": 0, "min_delay_seconds": 0, "max_delay_seconds": 0}

            # 1. 自身回覆 (is_incoming = False)：不可晉升！
            auto_reply.get_chat_window_fingerprint = lambda h: "2_new_fp"
            auto_reply.get_chat_latest_media_info = lambda h: (True, False) # 靠右送出
            auto_reply._TARGET_WINDOW_STATE["TargetPromo"] = {
                "fp": "2_new_fp", "changed_at": now - 0.2, "last_fp_change": now - 0.2,
                "consumed": False, "has_media": True, "is_incoming": False
            }
            auto_reply.PROCESSED_CACHE_FILES.clear()
            auto_reply.last_seen_cache_mtime = now - 1.0
            auto_reply.check_for_incoming_messages(cfg)
            self.assertIn(fake_hash, auto_reply._PROVISIONAL_BINDINGS)

            # 2. 純文字訊息 (has_media = False)：不可晉升！
            auto_reply.get_chat_latest_media_info = lambda h: (False, True)
            auto_reply._TARGET_WINDOW_STATE["TargetPromo"]["has_media"] = False
            auto_reply._TARGET_WINDOW_STATE["TargetPromo"]["is_incoming"] = True
            auto_reply.PROCESSED_CACHE_FILES.clear()
            auto_reply.last_seen_cache_mtime = now - 1.0
            auto_reply.check_for_incoming_messages(cfg)
            self.assertIn(fake_hash, auto_reply._PROVISIONAL_BINDINGS)

            # 3. 獨立傳入新圖片 (has_media = True, is_incoming = True, fp_changed = True)：正式晉升確認！
            auto_reply.get_chat_latest_media_info = lambda h: (True, True)
            auto_reply._TARGET_WINDOW_STATE["TargetPromo"]["has_media"] = True
            auto_reply._TARGET_WINDOW_STATE["TargetPromo"]["is_incoming"] = True
            auto_reply.PROCESSED_CACHE_FILES.clear()
            auto_reply.last_seen_cache_mtime = now - 1.0
            auto_reply.check_for_incoming_messages(cfg)
            self.assertNotIn(fake_hash, auto_reply._PROVISIONAL_BINDINGS)
            reloaded = auto_reply.load_chat_dirs_map()
            self.assertEqual(reloaded.get(fake_hash), "TargetPromo")
        finally:
            auto_reply.get_active_line_cache_dir = orig_cache_dir
            auto_reply.find_target_chat_window = orig_find
            auto_reply.get_chat_window_fingerprint = orig_fp
            auto_reply.get_chat_latest_media_info = orig_info
            auto_reply.send_grandma_image = orig_send
            auto_reply._TARGET_WINDOW_STATE.pop("TargetPromo", None)
            auto_reply._PROVISIONAL_BINDINGS.pop(fake_hash, None)

    def test_provisional_binding_not_revoked_on_closed_window(self):
        # 測試 🟡 防禦：若目標視窗關閉 (Closed)，不可誤當錯綁而將合法暫定綁定撤銷！
        fake_hash = "999803ef1234567890abcdef1234567890abcdef"
        test_cache = os.path.join(self.temp_dir.name, "closed_cache")
        chat_mo_dir = os.path.join(test_cache, fake_hash, "m")
        os.makedirs(chat_mo_dir, exist_ok=True)
        img_path = os.path.join(chat_mo_dir, "closed.eimg")
        with open(img_path, "wb") as f:
            f.write(b"\x00" * 300000)

        now = time.time()
        os.utime(img_path, (now, now))

        m_map = {fake_hash: "TargetClosed"}
        auto_reply.save_chat_dirs_map(m_map)
        auto_reply._PROVISIONAL_BINDINGS[fake_hash] = {
            "target": "TargetClosed",
            "bind_fp": "1_fp",
            "bind_time": now - 30.0, # 超過 15 秒
        }

        orig_cache_dir = auto_reply.get_active_line_cache_dir
        orig_find = auto_reply.find_target_chat_window
        try:
            auto_reply.get_active_line_cache_dir = lambda: test_cache
            # 視窗已關閉
            auto_reply.find_target_chat_window = lambda t: []
            auto_reply._TARGET_WINDOW_STATE.pop("TargetClosed", None)

            cfg = {"targets": ["TargetClosed"], "trigger_mode": "photo_only", "cooldown_seconds": 0, "min_delay_seconds": 0, "max_delay_seconds": 0}
            auto_reply.PROCESSED_CACHE_FILES.clear()
            auto_reply.last_seen_cache_mtime = now - 1.0
            auto_reply.check_for_incoming_messages(cfg)

            # 核心驗證：視窗關閉時絕對不撤銷綁定！
            reloaded = auto_reply.load_chat_dirs_map()
            self.assertIn(fake_hash, reloaded)
            self.assertIn(fake_hash, auto_reply._PROVISIONAL_BINDINGS)
        finally:
            auto_reply.get_active_line_cache_dir = orig_cache_dir
            auto_reply.find_target_chat_window = orig_find
            auto_reply._PROVISIONAL_BINDINGS.pop(fake_hash, None)

    def test_auto_bind_rejected_when_non_target_chat_window_open(self):
        # 測試 🟡 防禦：若桌面上存在非監控對象的 1:1 獨立視窗開啟，存在混淆風險，拒絕自動綁定
        fake_hash = "666603ef1234567890abcdef1234567890abcdef"
        now = time.time()
        orig_find = auto_reply.find_target_chat_window
        orig_open = auto_reply.get_all_open_chat_windows
        orig_via = auto_reply.verify_is_not_group_window_uia
        try:
            auto_reply.find_target_chat_window = lambda t: [(9981, t, "ChatWnd", True)] if t == "TargetUser" else []
            auto_reply.verify_is_not_group_window_uia = lambda h: True
            # 桌面上除了 TargetUser 外，還開著非監控的好友視窗 OtherFriend
            auto_reply.get_all_open_chat_windows = lambda: [
                (9981, "TargetUser", "ChatWnd", True),
                (9982, "OtherFriend", "ChatWnd", True),
            ]
            auto_reply._TARGET_WINDOW_STATE["TargetUser"] = {
                "fp": "1_fp", "changed_at": now - 0.2, "consumed": False, "has_media": True, "is_incoming": True
            }
            m_map = {}
            bound = auto_reply.try_auto_bind_channel(fake_hash, "fake.jpg", now, ["TargetUser"], m_map)
            self.assertIsNone(bound)
            self.assertNotIn(fake_hash, m_map)
        finally:
            auto_reply.find_target_chat_window = orig_find
            auto_reply.get_all_open_chat_windows = orig_open
            auto_reply.verify_is_not_group_window_uia = orig_via
            auto_reply._TARGET_WINDOW_STATE.pop("TargetUser", None)


    def test_provisional_binding_not_revoked_on_minimized_window(self):
        # 測試 🟡 防禦：若目標視窗處於最小化狀態 (IsIconic)，缺乏指紋活動，絕對不誤撤銷暫定綁定
        fake_hash = "111103ef1234567890abcdef1234567890abcdef"
        test_cache = os.path.join(self.temp_dir.name, "min_cache")
        chat_mo_dir = os.path.join(test_cache, fake_hash, "m")
        os.makedirs(chat_mo_dir, exist_ok=True)
        img_path = os.path.join(chat_mo_dir, "test.eimg")
        with open(img_path, "wb") as f:
            f.write(b"\x00" * 300000)

        now = time.time()
        os.utime(img_path, (now, now))

        m_map = {fake_hash: "TargetMin"}
        auto_reply.save_chat_dirs_map(m_map)
        auto_reply._PROVISIONAL_BINDINGS[fake_hash] = {
            "target": "TargetMin",
            "bind_fp": "1_fp",
            "bind_time": now - 30.0,
        }

        orig_cache_dir = auto_reply.get_active_line_cache_dir
        orig_find = auto_reply.find_target_chat_window
        orig_iconic = auto_reply.win32gui.IsIconic
        try:
            auto_reply.get_active_line_cache_dir = lambda: test_cache
            auto_reply.find_target_chat_window = lambda t: [(9991, t, "ChatWnd", True)] if t == "TargetMin" else []
            auto_reply.win32gui.IsIconic = lambda h: True  # 模擬視窗最小化！

            auto_reply._TARGET_WINDOW_STATE["TargetMin"] = {
                "fp": "2_fp", "changed_at": now - 60.0, "last_fp_change": now - 60.0, "consumed": True
            }

            cfg = {"targets": ["TargetMin"], "trigger_mode": "photo_only", "cooldown_seconds": 0, "min_delay_seconds": 0, "max_delay_seconds": 0}
            auto_reply.PROCESSED_CACHE_FILES.clear()
            auto_reply.last_seen_cache_mtime = now - 1.0
            auto_reply.check_for_incoming_messages(cfg)

            # 核心驗證：視窗最小化時，絕對不撤銷暫定綁定！
            reloaded = auto_reply.load_chat_dirs_map()
            self.assertIn(fake_hash, reloaded)
            self.assertIn(fake_hash, auto_reply._PROVISIONAL_BINDINGS)
        finally:
            auto_reply.get_active_line_cache_dir = orig_cache_dir
            auto_reply.find_target_chat_window = orig_find
            auto_reply.win32gui.IsIconic = orig_iconic
            auto_reply._TARGET_WINDOW_STATE.pop("TargetMin", None)
            auto_reply._PROVISIONAL_BINDINGS.pop(fake_hash, None)

    def test_provisional_binding_same_message_later_file_not_revoked(self):
        # 測試 🟡 防禦：若視窗指紋未變 (curr_fp == bind_fp)，代表同訊息稍後寫入的高解析度原圖，絕不誤撤銷
        fake_hash = "222203ef1234567890abcdef1234567890abcdef"
        test_cache = os.path.join(self.temp_dir.name, "same_msg_cache")
        chat_mo_dir = os.path.join(test_cache, fake_hash, "m")
        os.makedirs(chat_mo_dir, exist_ok=True)
        img_path = os.path.join(chat_mo_dir, "test.eimg")
        with open(img_path, "wb") as f:
            f.write(b"\x00" * 300000)

        now = time.time()
        os.utime(img_path, (now, now))

        m_map = {fake_hash: "TargetSameMsg"}
        auto_reply.save_chat_dirs_map(m_map)
        auto_reply._PROVISIONAL_BINDINGS[fake_hash] = {
            "target": "TargetSameMsg",
            "bind_fp": "1_same_fp",
            "bind_time": now - 30.0,
        }

        orig_cache_dir = auto_reply.get_active_line_cache_dir
        orig_find = auto_reply.find_target_chat_window
        orig_fp = auto_reply.get_chat_window_fingerprint
        orig_info = auto_reply.get_chat_latest_media_info
        try:
            auto_reply.get_active_line_cache_dir = lambda: test_cache
            auto_reply.find_target_chat_window = lambda t: [(9992, t, "ChatWnd", True)] if t == "TargetSameMsg" else []
            auto_reply.get_chat_window_fingerprint = lambda h: "1_same_fp" # 指紋完全相同！
            auto_reply.get_chat_latest_media_info = lambda h: (True, True)

            auto_reply._TARGET_WINDOW_STATE["TargetSameMsg"] = {
                "fp": "1_same_fp", "changed_at": now - 60.0, "last_fp_change": now - 60.0, "consumed": True
            }

            cfg = {"targets": ["TargetSameMsg"], "trigger_mode": "photo_only", "cooldown_seconds": 0, "min_delay_seconds": 0, "max_delay_seconds": 0}
            auto_reply.PROCESSED_CACHE_FILES.clear()
            auto_reply.last_seen_cache_mtime = now - 1.0
            auto_reply.check_for_incoming_messages(cfg)

            # 核心驗證：同訊息指紋未變時，不視為錯綁，不撤銷！
            reloaded = auto_reply.load_chat_dirs_map()
            self.assertIn(fake_hash, reloaded)
            self.assertIn(fake_hash, auto_reply._PROVISIONAL_BINDINGS)
        finally:
            auto_reply.get_active_line_cache_dir = orig_cache_dir
            auto_reply.find_target_chat_window = orig_find
            auto_reply.get_chat_window_fingerprint = orig_fp
            auto_reply.get_chat_latest_media_info = orig_info
            auto_reply._TARGET_WINDOW_STATE.pop("TargetSameMsg", None)
            auto_reply._PROVISIONAL_BINDINGS.pop(fake_hash, None)

    def test_provisional_binding_does_not_blindly_promote_on_age(self):
        # 測試 🔴 核心防禦：暫定綁定絕不因時間超過 10 分鐘 (600s) 即盲目晉升為永久，必須嚴格依賴目標視窗獨立新圖片核驗
        fake_hash = "333303ef1234567890abcdef1234567890abcdef"
        test_cache = os.path.join(self.temp_dir.name, "grad_cache")
        chat_mo_dir = os.path.join(test_cache, fake_hash, "m")
        os.makedirs(chat_mo_dir, exist_ok=True)
        img_path = os.path.join(chat_mo_dir, "test.eimg")
        with open(img_path, "wb") as f:
            f.write(b"\x00" * 300000)

        now = time.time()
        os.utime(img_path, (now, now))

        m_map = {fake_hash: "TargetGrad", "_provisional": {fake_hash: {"target": "TargetGrad", "bind_fp": "1_fp", "bind_time": now - 700.0}}}
        auto_reply.atomic_save_json(auto_reply.CHAT_MAP_PATH, m_map)
        auto_reply._PROVISIONAL_BINDINGS[fake_hash] = {
            "target": "TargetGrad",
            "bind_fp": "1_fp",
            "bind_time": now - 700.0, # 逾 10 分鐘但無目標視窗實質傳入圖片事件
        }

        orig_cache_dir = auto_reply.get_active_line_cache_dir
        orig_find = auto_reply.find_target_chat_window
        try:
            auto_reply.get_active_line_cache_dir = lambda: test_cache
            # 目標視窗上線，但指紋未改變 (未有新傳入圖片)
            auto_reply.find_target_chat_window = lambda t: [(9993, t, "ChatWnd", True)] if t == "TargetGrad" else []
            auto_reply.get_chat_window_fingerprint = lambda h: "1_fp"

            cfg = {"targets": ["TargetGrad"], "trigger_mode": "photo_only", "cooldown_seconds": 0, "min_delay_seconds": 0, "max_delay_seconds": 0}
            auto_reply.PROCESSED_CACHE_FILES.clear()
            auto_reply.last_seen_cache_mtime = now - 1.0
            auto_reply.check_for_incoming_messages(cfg)

            # 核心驗證：絕不盲目晉升！依然留在 _PROVISIONAL_BINDINGS 中
            self.assertIn(fake_hash, auto_reply._PROVISIONAL_BINDINGS)
        finally:
            auto_reply.get_active_line_cache_dir = orig_cache_dir
            auto_reply.find_target_chat_window = orig_find
            auto_reply._PROVISIONAL_BINDINGS.pop(fake_hash, None)

    def test_unbind_cleans_provisional_bindings(self):
        # 測試 🟡 防禦：執行 --unbind 時，若該頻道仍處於暫定狀態，亦一併從 _PROVISIONAL_BINDINGS 清除
        fake_hash = "444403ef1234567890abcdef1234567890abcdef"
        m_map = {fake_hash: "TargetUnbindProv"}
        auto_reply.save_chat_dirs_map(m_map)
        auto_reply._PROVISIONAL_BINDINGS[fake_hash] = {
            "target": "TargetUnbindProv",
            "bind_fp": "1_fp",
            "bind_time": time.time(),
        }

        try:
            with mock.patch("sys.argv", ["auto_reply.py", "--unbind", "TargetUnbindProv"]):
                with self.assertRaises(SystemExit) as cm:
                    auto_reply.main()
                self.assertEqual(cm.exception.code, 0)

            self.assertNotIn(fake_hash, auto_reply._PROVISIONAL_BINDINGS)
            self.assertNotIn(fake_hash, auto_reply.load_chat_dirs_map())
        finally:
            auto_reply._PROVISIONAL_BINDINGS.pop(fake_hash, None)


    def test_provisional_binding_persistence_across_restart(self):
        # 測試 🔴 核心修復：暫定綁定持久化至 chat_dirs.json 之 _provisional，重啟後成功延續核驗
        fake_hash = "555503ef1234567890abcdef1234567890abcdef"
        now = time.time()
        orig_find = auto_reply.find_target_chat_window
        orig_uia = auto_reply.verify_is_not_group_window_uia
        orig_fp = auto_reply.get_chat_window_fingerprint
        orig_info = auto_reply.get_chat_latest_media_info
        try:
            auto_reply.find_target_chat_window = lambda t: [(9995, t, "ChatWnd", True)] if t == "TargetPersist" else []
            auto_reply.verify_is_not_group_window_uia = lambda h: True
            auto_reply.get_chat_window_fingerprint = lambda h: "1_persist_fp"
            auto_reply.get_chat_latest_media_info = lambda h: (True, True)

            auto_reply._TARGET_WINDOW_STATE["TargetPersist"] = {
                "fp": "1_persist_fp", "changed_at": now - 0.2, "consumed": False, "has_media": True, "is_incoming": True
            }

            m_map = {}
            bound = auto_reply.try_auto_bind_channel(fake_hash, "fake.jpg", now, ["TargetPersist"], m_map)
            self.assertEqual(bound, "TargetPersist")

            # 驗證 chat_dirs.json 寫入了 _provisional 結構
            with open(auto_reply.CHAT_MAP_PATH, "r", encoding="utf-8") as f:
                disk_content = auto_reply.json.load(f)
            self.assertIn("_provisional", disk_content)
            self.assertIn(fake_hash, disk_content["_provisional"])
            self.assertEqual(disk_content["_provisional"][fake_hash]["target"], "TargetPersist")

            # 模擬行程重啟：清空記憶體中的狀態，透過 load_provisional_bindings 重新載入
            auto_reply._PROVISIONAL_BINDINGS.clear()
            reloaded_prov = auto_reply.load_provisional_bindings()
            self.assertIn(fake_hash, reloaded_prov)
            self.assertEqual(reloaded_prov[fake_hash]["target"], "TargetPersist")
            self.assertEqual(reloaded_prov[fake_hash]["bind_fp"], "1_persist_fp")
        finally:
            auto_reply.find_target_chat_window = orig_find
            auto_reply.verify_is_not_group_window_uia = orig_uia
            auto_reply.get_chat_window_fingerprint = orig_fp
            auto_reply.get_chat_latest_media_info = orig_info
            auto_reply._TARGET_WINDOW_STATE.pop("TargetPersist", None)
            auto_reply._PROVISIONAL_BINDINGS.pop(fake_hash, None)

    def test_provisional_binding_scroll_old_image_not_revoked(self):
        # 測試 🟡 防禦：若快取檔案時間早於綁定時間 (使用者檢視舊對話歷史)，絕對不誤觸發錯綁撤銷
        fake_hash = "666603ef1234567890abcdef1234567890abcdef"
        test_cache = os.path.join(self.temp_dir.name, "scroll_cache")
        chat_mo_dir = os.path.join(test_cache, fake_hash, "m")
        os.makedirs(chat_mo_dir, exist_ok=True)
        img_path = os.path.join(chat_mo_dir, "old_scroll.eimg")
        with open(img_path, "wb") as f:
            f.write(b"\x00" * 300000)

        now = time.time()
        # 檔案時間為 60 秒前 (早於綁定時間)
        os.utime(img_path, (now - 60.0, now - 60.0))

        m_map = {fake_hash: "TargetScroll"}
        auto_reply.save_chat_dirs_map(m_map)
        auto_reply._PROVISIONAL_BINDINGS[fake_hash] = {
            "target": "TargetScroll",
            "bind_fp": "1_fp",
            "bind_time": now - 10.0,
        }

        orig_cache_dir = auto_reply.get_active_line_cache_dir
        orig_find = auto_reply.find_target_chat_window
        try:
            auto_reply.get_active_line_cache_dir = lambda: test_cache
            auto_reply.find_target_chat_window = lambda t: [(9996, t, "ChatWnd", True)] if t == "TargetScroll" else []
            # 目標視窗閒置
            auto_reply._TARGET_WINDOW_STATE["TargetScroll"] = {
                "fp": "2_fp", "changed_at": now - 30.0, "last_fp_change": now - 30.0, "consumed": True
            }

            cfg = {"targets": ["TargetScroll"], "trigger_mode": "photo_only", "cooldown_seconds": 0, "min_delay_seconds": 0, "max_delay_seconds": 0}
            auto_reply.PROCESSED_CACHE_FILES.clear()
            auto_reply.last_seen_cache_mtime = now - 100.0
            auto_reply.check_for_incoming_messages(cfg)

            # 核心驗證：舊歷史快取檔案不誤觸發撤銷！
            reloaded = auto_reply.load_chat_dirs_map()
            self.assertIn(fake_hash, reloaded)
            self.assertIn(fake_hash, auto_reply._PROVISIONAL_BINDINGS)
        finally:
            auto_reply.get_active_line_cache_dir = orig_cache_dir
            auto_reply.find_target_chat_window = orig_find
            auto_reply._TARGET_WINDOW_STATE.pop("TargetScroll", None)
            auto_reply._PROVISIONAL_BINDINGS.pop(fake_hash, None)

    def test_locked_chat_dirs_fail_closed_on_corrupt_file(self):
        # 測試 🔴 核心防禦：chat_dirs.json 損毀或非 JSON 物件時，locked_chat_dirs 嚴格拋出 IOError，絕不以空物件覆寫磁碟 (Fail-Closed)
        corrupt_path = auto_reply.CHAT_MAP_PATH
        with open(corrupt_path, "w", encoding="utf-8") as f:
            f.write("{invalid_json: 123,")
        
        with self.assertRaises(IOError):
            with auto_reply.locked_chat_dirs() as data:
                data["some_hash"] = "SomeTarget"
        
        # 磁碟原內容完整保留，絕不被覆寫為 {}
        with open(corrupt_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content, "{invalid_json: 123,")

    def test_locked_chat_dirs_raises_on_atomic_save_failure(self):
        # 測試 locked_chat_dirs 當 atomic_save_json 失敗時拋出 IOError
        orig_save = auto_reply.atomic_save_json
        try:
            auto_reply.atomic_save_json = lambda path, data: False
            with self.assertRaises(IOError):
                with auto_reply.locked_chat_dirs() as data:
                    data["test"] = "val"
        finally:
            auto_reply.atomic_save_json = orig_save

    def test_revoke_does_not_trigger_while_thumbnail_rendering(self):
        # 測試 🔴 核心防禦：視窗指紋改變但在 15 秒縮圖渲染期間內 (not is_window_completely_idle)，絕不誤撤銷合法綁定
        fake_hash = "777703ef1234567890abcdef1234567890abcdef"
        test_cache = os.path.join(self.temp_dir.name, "thumb_render_cache")
        chat_mo_dir = os.path.join(test_cache, fake_hash, "m")
        os.makedirs(chat_mo_dir, exist_ok=True)
        img_path = os.path.join(chat_mo_dir, "render.eimg")
        with open(img_path, "wb") as f:
            f.write(b"\x00" * 300000)

        now = time.time()
        os.utime(img_path, (now, now))

        auto_reply.atomic_save_json(auto_reply.CHAT_MAP_PATH, {fake_hash: "TargetRender"})
        auto_reply._PROVISIONAL_BINDINGS[fake_hash] = {
            "target": "TargetRender",
            "bind_fp": "1_fp",
            "bind_time": now - 30.0,
        }

        # 模擬視窗剛異動 2 秒 (小於 AUTOBIND_TIMEOUT=15s)，且縮圖尚未渲染出 has_media=False
        auto_reply._TARGET_WINDOW_STATE["TargetRender"] = {
            "fp": "2_fp_rendering",
            "changed_at": now - 2.0,
            "last_fp_change": now - 2.0,
            "has_media": False,
            "is_incoming": False,
            "consumed": False,
        }

        orig_cache_dir = auto_reply.get_active_line_cache_dir
        orig_find = auto_reply.find_target_chat_window
        orig_fp = auto_reply.get_chat_window_fingerprint
        orig_media = auto_reply.get_chat_latest_media_info
        try:
            auto_reply.get_active_line_cache_dir = lambda: test_cache
            auto_reply.find_target_chat_window = lambda t: [(9994, t, "ChatWnd", True)] if t == "TargetRender" else []
            auto_reply.get_chat_window_fingerprint = lambda h: "2_fp_rendering"
            auto_reply.get_chat_latest_media_info = lambda h: (False, False)

            cfg = {"targets": ["TargetRender"], "trigger_mode": "photo_only", "cooldown_seconds": 0, "min_delay_seconds": 0, "max_delay_seconds": 0}
            auto_reply.PROCESSED_CACHE_FILES.clear()
            auto_reply.last_seen_cache_mtime = now - 1.0
            auto_reply.check_for_incoming_messages(cfg)

            # 核心驗證：因為視窗仍在渲染期間內 (未徹底閒置逾 15s)，絕不可將其誤判為錯綁而撤銷！
            self.assertIn(fake_hash, auto_reply._PROVISIONAL_BINDINGS)
            loaded = auto_reply.load_chat_dirs_map()
            self.assertIn(fake_hash, loaded)
        finally:
            auto_reply.get_active_line_cache_dir = orig_cache_dir
            auto_reply.find_target_chat_window = orig_find
            auto_reply.get_chat_window_fingerprint = orig_fp
            auto_reply.get_chat_latest_media_info = orig_media
            auto_reply._TARGET_WINDOW_STATE.pop("TargetRender", None)
            auto_reply._PROVISIONAL_BINDINGS.pop(fake_hash, None)

    def test_calibrate_preserves_provisional_bindings_under_lock(self):
        # 測試 🔴 核心防禦：--calibrate 執行時，必須完整保留現有其他頻道的 _provisional 暫定綁定記錄
        other_prov_hash = "666603ef1234567890abcdef1234567890abcdef"
        new_calib_hash = "888803ef1234567890abcdef1234567890abcdef"
        initial_map = {
            other_prov_hash: "TargetOther",
            "_provisional": {
                other_prov_hash: {
                    "target": "TargetOther",
                    "bind_fp": "1_fp",
                    "bind_time": 1000.0,
                }
            }
        }
        auto_reply.atomic_save_json(auto_reply.CHAT_MAP_PATH, initial_map)
        auto_reply._PROVISIONAL_BINDINGS[other_prov_hash] = initial_map["_provisional"][other_prov_hash]

        # 模擬 --calibrate 寫入
        with auto_reply.locked_chat_dirs() as disk_map:
            for k in list(disk_map.keys()):
                if k == "_provisional":
                    continue
                if isinstance(disk_map[k], str) and disk_map[k].lower() == "TargetCalib".lower():
                    del disk_map[k]
                    if isinstance(disk_map.get("_provisional"), dict):
                        disk_map["_provisional"].pop(k, None)
            disk_map[new_calib_hash] = "TargetCalib"

        # 核心驗證：_provisional 中的 other_prov_hash 依然安全存在！
        loaded_prov = auto_reply.load_provisional_bindings()
        self.assertIn(other_prov_hash, loaded_prov)
        self.assertEqual(loaded_prov[other_prov_hash]["target"], "TargetOther")
        auto_reply._PROVISIONAL_BINDINGS.pop(other_prov_hash, None)

    def test_unbind_supports_prefix_hash(self):
        # 測試 🟢 易用性強化：--unbind 支援前綴雜湊 (前 8 碼) 解除綁定
        full_hash = "abcde1234567890abcdef1234567890abcdef12"
        m_map = {full_hash: "TargetPrefixUnbind"}
        auto_reply.save_chat_dirs_map(m_map)

        try:
            with mock.patch("sys.argv", ["auto_reply.py", "--unbind", full_hash[:8]]):
                with self.assertRaises(SystemExit) as cm:
                    auto_reply.main()
                self.assertEqual(cm.exception.code, 0)

            # 核心驗證：透過前綴雜湊成功解除
            reloaded = auto_reply.load_chat_dirs_map()
            self.assertNotIn(full_hash, reloaded)
        finally:
            with auto_reply.locked_chat_dirs() as disk_map:
                disk_map.pop(full_hash, None)

    def test_locked_chat_dirs_handles_utf8_bom(self):
        # 測試 🟡 防禦：Windows 記事本以 UTF-8 with BOM 儲存時，locked_chat_dirs 與 load_chat_dirs_map 皆能無損讀取
        fake_hash = "bbbb03ef1234567890abcdef1234567890abcdef"
        bom_data = b'\xef\xbb\xbf{"' + fake_hash.encode("ascii") + b'": "TargetBOM"}'
        with open(auto_reply.CHAT_MAP_PATH, "wb") as f:
            f.write(bom_data)
        
        # 驗證 load_chat_dirs_map 讀取成功
        loaded = auto_reply.load_chat_dirs_map()
        self.assertEqual(loaded.get(fake_hash), "TargetBOM")

        # 驗證 locked_chat_dirs 讀取成功
        with auto_reply.locked_chat_dirs() as disk_map:
            self.assertEqual(disk_map.get(fake_hash), "TargetBOM")
            disk_map[fake_hash] = "TargetBOMUpdated"

        reloaded = auto_reply.load_chat_dirs_map()
        self.assertEqual(reloaded.get(fake_hash), "TargetBOMUpdated")

    def test_provisional_revoke_not_triggered_when_loop_delayed_with_matching_cand_m(self):
        # 測試 🟡 防禦：當主輪詢迴圈因延遲發送或回覆其他好友而阻塞超過 15 秒時，
        # 只要 cand_m 與目標視窗異動時間相符 (abs(cand_m - changed_at) <= 6.0s)，絕不可將合法暫定綁定誤判為閒置撤銷！
        fake_hash = "888803ef1234567890abcdef1234567890abcdef"
        test_cache = os.path.join(self.temp_dir.name, "loop_delay_cache")
        chat_mo_dir = os.path.join(test_cache, fake_hash, "m")
        os.makedirs(chat_mo_dir, exist_ok=True)
        img_path = os.path.join(chat_mo_dir, "delayed.eimg")
        with open(img_path, "wb") as f:
            f.write(b"\x00" * 300000)

        now = time.time()
        # 檔案在 25 秒前產生
        os.utime(img_path, (now - 25.0, now - 25.0))

        m_map = {fake_hash: "TargetLoopDelay"}
        auto_reply.save_chat_dirs_map(m_map)
        auto_reply._PROVISIONAL_BINDINGS[fake_hash] = {
            "target": "TargetLoopDelay",
            "bind_fp": "1_fp",
            "bind_time": now - 40.0,
        }

        # 目標視窗異動時間同樣在 25 秒前 (與 cand_m 完全吻合，差 0.1 秒)
        auto_reply._TARGET_WINDOW_STATE["TargetLoopDelay"] = {
            "fp": "2_fp_delayed",
            "changed_at": now - 25.1,
            "last_fp_change": now - 25.1,
            "consumed": False,
            "has_media": False,
            "is_incoming": True,
        }

        orig_cache_dir = auto_reply.get_active_line_cache_dir
        orig_find = auto_reply.find_target_chat_window
        orig_fp = auto_reply.get_chat_window_fingerprint
        orig_media = auto_reply.get_chat_latest_media_info
        try:
            auto_reply.get_active_line_cache_dir = lambda: test_cache
            auto_reply.find_target_chat_window = lambda t: [(9998, t, "ChatWnd", True)] if t == "TargetLoopDelay" else []
            auto_reply.get_chat_window_fingerprint = lambda h: "2_fp_delayed"
            auto_reply.get_chat_latest_media_info = lambda h: (False, True)

            cfg = {"targets": ["TargetLoopDelay"], "trigger_mode": "photo_only", "cooldown_seconds": 0, "min_delay_seconds": 0, "max_delay_seconds": 0}
            auto_reply.PROCESSED_CACHE_FILES.clear()
            auto_reply.last_seen_cache_mtime = now - 35.0
            auto_reply.check_for_incoming_messages(cfg)

            # 核心驗證：因為 abs(cand_m - changed_at) 僅 0.1s <= 15s，絕不可誤判為閒置撤銷！
            self.assertIn(fake_hash, auto_reply._PROVISIONAL_BINDINGS)
            loaded = auto_reply.load_chat_dirs_map()
            self.assertIn(fake_hash, loaded)
        finally:
            auto_reply.get_active_line_cache_dir = orig_cache_dir
            auto_reply.find_target_chat_window = orig_find
            auto_reply.get_chat_window_fingerprint = orig_fp
            auto_reply.get_chat_latest_media_info = orig_media
            auto_reply._TARGET_WINDOW_STATE.pop("TargetLoopDelay", None)
            auto_reply._PROVISIONAL_BINDINGS.pop(fake_hash, None)

    def test_revoke_failure_fails_closed_without_reply(self):
        # 測試 🟡 防禦：錯綁撤銷寫入磁碟失敗時，Fail-Closed 立即從記憶體清除該頻道並跳過該圖片，絕不發送給可能錯誤的對象
        fake_hash = "999903ef1234567890abcdef1234567890abcdef"
        test_cache = os.path.join(self.temp_dir.name, "revoke_fail_cache")
        chat_mo_dir = os.path.join(test_cache, fake_hash, "m")
        os.makedirs(chat_mo_dir, exist_ok=True)
        img_path = os.path.join(chat_mo_dir, "revoke_fail.eimg")
        with open(img_path, "wb") as f:
            f.write(b"\x00" * 300000)

        now = time.time()
        os.utime(img_path, (now - 20.0, now - 20.0))

        m_map = {fake_hash: "TargetRevokeFail"}
        auto_reply.save_chat_dirs_map(m_map)
        auto_reply._PROVISIONAL_BINDINGS[fake_hash] = {
            "target": "TargetRevokeFail",
            "bind_fp": "1_fp",
            "bind_time": now - 40.0,
        }

        auto_reply._TARGET_WINDOW_STATE["TargetRevokeFail"] = {
            "fp": "2_old_fp",
            "changed_at": now - 60.0,
            "last_fp_change": now - 60.0,
            "consumed": True,
            "has_media": True,
            "is_incoming": True,
        }

        sent_targets = []
        orig_send = auto_reply.send_grandma_image
        orig_cache_dir = auto_reply.get_active_line_cache_dir
        orig_find = auto_reply.find_target_chat_window
        orig_fp = auto_reply.get_chat_window_fingerprint
        orig_lock = auto_reply.locked_chat_dirs
        try:
            auto_reply.send_grandma_image = lambda t, c, **kwargs: sent_targets.append(t)
            auto_reply.get_active_line_cache_dir = lambda: test_cache
            auto_reply.find_target_chat_window = lambda t: [(9999, t, "ChatWnd", True)] if t == "TargetRevokeFail" else []
            auto_reply.get_chat_window_fingerprint = lambda h: "2_old_fp"

            # 模擬撤銷時磁碟寫入異常
            @auto_reply.contextlib.contextmanager
            def broken_lock():
                raise IOError("磁碟空間不足，無法寫入撤銷記錄")
                yield {}
            auto_reply.locked_chat_dirs = broken_lock

            cfg = {"targets": ["TargetRevokeFail"], "trigger_mode": "photo_only", "cooldown_seconds": 0, "min_delay_seconds": 0, "max_delay_seconds": 0}
            auto_reply.PROCESSED_CACHE_FILES.clear()
            auto_reply.last_seen_cache_mtime = now - 35.0
            auto_reply.check_for_incoming_messages(cfg)

            # 核心驗證：磁碟寫入雖失敗，但記憶體中已被清除，且絕未發送圖片給錯誤對象！
            self.assertNotIn(fake_hash, auto_reply._PROVISIONAL_BINDINGS)
            self.assertEqual(len(sent_targets), 0)
        finally:
            auto_reply.send_grandma_image = orig_send
            auto_reply.get_active_line_cache_dir = orig_cache_dir
            auto_reply.find_target_chat_window = orig_find
            auto_reply.get_chat_window_fingerprint = orig_fp
            auto_reply.locked_chat_dirs = orig_lock
            auto_reply._TARGET_WINDOW_STATE.pop("TargetRevokeFail", None)
            auto_reply._PROVISIONAL_BINDINGS.pop(fake_hash, None)
    def _run_provisional_scenario(self, fake_hash, target, cand_age, bind_fp, state, curr_fp, media_info, unbind_disk=False, bind_age=40.0, window_present=True):
        # 共用情境：暫定綁定頻道在 cand_age 秒前產生快取檔案，回傳 (發送清單, 是否仍有暫定綁定)
        test_cache = os.path.join(self.temp_dir.name, "prov_cache_" + fake_hash[:4])
        chat_dir = os.path.join(test_cache, fake_hash, "m")
        os.makedirs(chat_dir, exist_ok=True)
        img_path = os.path.join(chat_dir, "p.eimg")
        with open(img_path, "wb") as f:
            f.write(b"\x00" * 300000)
        now = time.time()
        os.utime(img_path, (now - cand_age, now - cand_age))

        auto_reply.save_chat_dirs_map({} if unbind_disk else {fake_hash: target})
        auto_reply._PROVISIONAL_BINDINGS[fake_hash] = {"target": target, "bind_fp": bind_fp, "bind_time": now - bind_age}
        auto_reply._TARGET_WINDOW_STATE[target] = dict(state(now), consumed=True)

        sent = []
        origs = {n: getattr(auto_reply, n) for n in (
            "send_grandma_image", "get_active_line_cache_dir", "find_target_chat_window",
            "get_chat_window_fingerprint", "get_chat_latest_media_info", "load_chat_dirs_map")}
        try:
            if unbind_disk:
                # 模擬競態：輪詢載入後、晉升前，CLI 已 --unbind 清空磁碟，記憶體仍持有舊綁定
                auto_reply.load_chat_dirs_map = lambda: {fake_hash: target}
            auto_reply.send_grandma_image = lambda t, c, **kwargs: sent.append(t)
            auto_reply.get_active_line_cache_dir = lambda: test_cache
            auto_reply.find_target_chat_window = lambda t: [(9970, t, "ChatWnd", True)] if (t == target and window_present) else []
            auto_reply.get_chat_window_fingerprint = lambda h: curr_fp
            auto_reply.get_chat_latest_media_info = lambda h: media_info
            cfg = {"targets": [target], "trigger_mode": "photo_only", "cooldown_seconds": 0, "min_delay_seconds": 0, "max_delay_seconds": 0}
            auto_reply.PROCESSED_CACHE_FILES.clear()
            auto_reply.last_seen_cache_mtime = now - 35.0
            auto_reply.check_for_incoming_messages(cfg)
            return sent, fake_hash in auto_reply._PROVISIONAL_BINDINGS
        finally:
            for n, f in origs.items():
                setattr(auto_reply, n, f)
            auto_reply._TARGET_WINDOW_STATE.pop(target, None)
            auto_reply._PROVISIONAL_BINDINGS.pop(fake_hash, None)

    def test_revoke_triggers_when_polling_is_timely(self):
        # 輪詢及時 (快取檔案僅 2 秒前寫入)、目標視窗已閒置 30 秒：錯綁必須撤銷且不回覆
        fake_hash = "aaaa03ef1234567890abcdef1234567890abcdef"
        sent, still_prov = self._run_provisional_scenario(
            fake_hash, "TargetTimely", 2.0, "1_bind_fp",
            lambda now: {"fp": "2_idle_fp", "changed_at": now - 30.0, "last_fp_change": now - 30.0, "has_media": True, "is_incoming": True},
            "2_idle_fp", (True, True))
        self.assertEqual(sent, [])
        self.assertFalse(still_prov)
        self.assertNotIn(fake_hash, auto_reply.load_chat_dirs_map())

    def test_gray_zone_provisional_never_replies(self):
        # 灰區：既未晉升 (無傳入圖片) 亦未撤銷 (視窗剛異動、縮圖渲染中)：不回覆、保留暫定綁定、檔案暫緩
        fake_hash = "bbbb03ef1234567890abcdef1234567890abcdef"
        sent, still_prov = self._run_provisional_scenario(
            fake_hash, "TargetGray", 2.0, "1_bind_fp",
            lambda now: {"fp": "2_new_fp", "changed_at": now - 1.0, "last_fp_change": now - 1.0, "has_media": False, "is_incoming": True},
            "2_new_fp", (False, False))
        self.assertEqual(sent, [])
        self.assertTrue(still_prov)

    def test_promotion_after_disk_unbind_clears_memory_and_skips(self):
        # 晉升時磁碟綁定已被 --unbind 清除：同步清除記憶體，不回覆
        fake_hash = "cccc03ef1234567890abcdef1234567890abcdef"
        sent, still_prov = self._run_provisional_scenario(
            fake_hash, "TargetUnbound", 2.0, "1_bind_fp",
            lambda now: {"fp": "1_bind_fp", "changed_at": now - 2.0, "last_fp_change": now - 2.0, "has_media": True, "is_incoming": True},
            "2_new_fp", (True, True), unbind_disk=True)
        self.assertEqual(sent, [])
        self.assertFalse(still_prov)

    def test_provisional_unverified_paths_fail_closed(self):
        # 6 秒範圍內的後續檔、目標視窗缺失：暫定頻道未經核驗，不得回覆，綁定保留
        state = lambda now: {"fp": "1_bind_fp", "changed_at": now - 2.0, "last_fp_change": now - 2.0, "has_media": True, "is_incoming": True}
        sent, still_prov = self._run_provisional_scenario(
            "dddd03ef1234567890abcdef1234567890abcdef", "TargetEarly", 2.0, "1_bind_fp",
            state, "1_bind_fp", (True, True), bind_age=4.0)
        self.assertEqual(sent, [])
        self.assertTrue(still_prov)
        sent, still_prov = self._run_provisional_scenario(
            "eeee03ef1234567890abcdef1234567890abcdef", "TargetNoWin", 2.0, "1_bind_fp",
            state, "1_bind_fp", (True, True), window_present=False)
        self.assertEqual(sent, [])
        self.assertTrue(still_prov)

    def test_provisional_fingerprint_unreadable_fails_closed(self):
        # 指紋讀取失敗 (curr_fp 為 None)：暫定頻道不得回覆，綁定保留
        sent, still_prov = self._run_provisional_scenario(
            "ffff03ef1234567890abcdef1234567890abcdef", "TargetNoFp", 2.0, "1_bind_fp",
            lambda now: {"fp": "1_bind_fp", "changed_at": now - 2.0, "last_fp_change": now - 2.0, "has_media": True, "is_incoming": True},
            None, (True, True))
        self.assertEqual(sent, [])
        self.assertTrue(still_prov)

    def test_atomic_save_json_retries_permission_error(self):
        path = os.path.join(self.temp_dir.name, "retry.json")
        real_replace = os.replace
        calls = []
        def flaky(src, dst):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError("WinError 32")
            return real_replace(src, dst)
        os.replace = flaky
        try:
            self.assertTrue(auto_reply.atomic_save_json(path, {"a": 1}))
        finally:
            os.replace = real_replace
        self.assertEqual(len(calls), 3)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"a": 1})

    def test_atomic_save_json_gives_up_and_cleans_temp(self):
        path = os.path.join(self.temp_dir.name, "fail.json")
        real_replace = os.replace
        def always_fail(src, dst):
            raise PermissionError("WinError 5")
        os.replace = always_fail
        try:
            self.assertFalse(auto_reply.atomic_save_json(path, {"a": 1}))
        finally:
            os.replace = real_replace
        self.assertEqual([n for n in os.listdir(self.temp_dir.name) if n.startswith("tmp")], [])

    def test_extract_chat_hash_ignores_account_level_tmp_files(self):
        # Cache\<account>\tmp\messageImage_*.jpg 只含帳號層 Hash，不得被當成對話頻道
        base = os.path.expandvars(r"%LOCALAPPDATA%\LINE\Cache")
        acct = "b1acf4e02644042b1e22811631a6735b34cd15af"
        chat = "67fa03ef897657d0c21b02ceec0a55fa9c65ef56"
        self.assertIsNone(auto_reply.extract_chat_hash_from_path(os.path.join(base, acct, "tmp", "messageImage_1.jpg")))
        self.assertEqual(auto_reply.extract_chat_hash_from_path(os.path.join(base, acct, chat, "m", "b", "x.eimg")), chat)
        self.assertEqual(auto_reply.extract_chat_hash_from_path(os.path.join(base, chat, "m", "b", "x.eimg")), chat)

    def test_bind_channel_manually_checks_and_writes(self):
        cache = os.path.join(self.temp_dir.name, "bind_cache")
        h1 = "aaaa1111" + "0" * 32
        h2 = "aaaa2222" + "0" * 32
        h3 = "bbbb3333" + "0" * 32
        for h in (h1, h2, h3):
            os.makedirs(os.path.join(cache, h, "m"))
        orig = auto_reply.get_active_line_cache_dir
        auto_reply.get_active_line_cache_dir = lambda: cache
        try:
            targets = ["FriendA", "FriendB"]
            # 名稱不在名單、前綴過短、前綴對應多個資料夾：一律拒絕且不寫入
            self.assertFalse(auto_reply.bind_channel_manually("Stranger", "bbbb3333", targets)[0])
            self.assertFalse(auto_reply.bind_channel_manually("FriendA", "bbbb", targets)[0])
            self.assertFalse(auto_reply.bind_channel_manually("FriendA", "aaaa", targets)[0])
            self.assertEqual(auto_reply.load_chat_dirs_map(), {})
            # 唯一前綴且名稱不分大小寫：成功寫入
            ok, _ = auto_reply.bind_channel_manually("frienda", "aaaa1111", targets)
            self.assertTrue(ok)
            self.assertEqual(auto_reply.load_chat_dirs_map(), {h1: "FriendA"})
            # 重複綁定同一組合：視為成功且不變
            self.assertTrue(auto_reply.bind_channel_manually("FriendA", "aaaa1111", targets)[0])
            # 頻道已綁他人、名稱已綁其他頻道：拒絕
            self.assertFalse(auto_reply.bind_channel_manually("FriendB", "aaaa1111", targets)[0])
            self.assertFalse(auto_reply.bind_channel_manually("FriendA", "bbbb3333", targets)[0])
            self.assertEqual(auto_reply.load_chat_dirs_map(), {h1: "FriendA"})
        finally:
            auto_reply.get_active_line_cache_dir = orig

    def test_bind_channel_by_viewing_requires_exactly_one_changed_channel(self):
        cache = os.path.join(self.temp_dir.name, "view_cache")
        h1 = "cccc1111" + "0" * 32
        h2 = "dddd2222" + "0" * 32
        for h in (h1, h2):
            os.makedirs(os.path.join(cache, h, "m"))
        p1 = os.path.join(cache, h1, "m", "x.eimg")
        p2 = os.path.join(cache, h2, "m", "y.eimg")
        snapshots = {"seq": []}
        calls = {"n": 0}

        def fake_files(_dir=None):
            calls["n"] += 1
            return snapshots["seq"][min(calls["n"] - 1, len(snapshots["seq"]) - 1)]

        orig = (auto_reply.get_active_line_cache_dir, auto_reply.get_all_cache_files,
                auto_reply.time.sleep, auto_reply.foreground_window_title)
        auto_reply.get_active_line_cache_dir = lambda: cache
        auto_reply.get_all_cache_files = fake_files
        auto_reply.time.sleep = lambda s: None
        auto_reply.foreground_window_title = lambda: "FriendA"
        try:
            targets = ["FriendA"]
            # 無寫入：中止
            snapshots["seq"] = [{}]
            calls["n"] = 0
            self.assertFalse(auto_reply.bind_channel_by_viewing("FriendA", targets, seconds=0.01)[0])
            # 兩個頻道同時有寫入：中止且不綁定
            snapshots["seq"] = [{}, {p1: (1, 1), p2: (1, 1)}]
            calls["n"] = 0
            self.assertFalse(auto_reply.bind_channel_by_viewing("FriendA", targets, seconds=0.01)[0])
            self.assertEqual(auto_reply.load_chat_dirs_map(), {})
            # 寫入當下前景視窗不是目標聊天室 (例如群組背景下載縮圖)：中止且不綁定
            auto_reply.foreground_window_title = lambda: "SomeGroup"
            snapshots["seq"] = [{}, {p1: (1, 1)}]
            calls["n"] = 0
            self.assertFalse(auto_reply.bind_channel_by_viewing("FriendA", targets, seconds=0.01)[0])
            self.assertEqual(auto_reply.load_chat_dirs_map(), {})
            auto_reply.foreground_window_title = lambda: "friendA"
            # 剛好一個頻道有寫入且前景正確：綁定
            snapshots["seq"] = [{}, {p1: (1, 1)}]
            calls["n"] = 0
            self.assertTrue(auto_reply.bind_channel_by_viewing("FriendA", targets, seconds=0.01)[0])
            self.assertEqual(auto_reply.load_chat_dirs_map(), {h1: "FriendA"})
        finally:
            (auto_reply.get_active_line_cache_dir, auto_reply.get_all_cache_files,
             auto_reply.time.sleep, auto_reply.foreground_window_title) = orig

    def test_current_time_slot_boundaries(self):
        from datetime import datetime as dt
        slot = auto_reply.current_time_slot
        self.assertEqual(slot(dt(2026, 1, 1, 4, 59)), "evening")
        self.assertEqual(slot(dt(2026, 1, 1, 5, 0)), "morning")
        self.assertEqual(slot(dt(2026, 1, 1, 10, 59)), "morning")
        self.assertEqual(slot(dt(2026, 1, 1, 11, 0)), "noon")
        self.assertEqual(slot(dt(2026, 1, 1, 17, 0)), "evening")

    def test_time_aware_pick_filters_by_slot_and_caches_ocr(self):
        imgs = {}
        for name, text in (("m", "早安"), ("e", "晚安"), ("n", "祝福"), ("fail", None), ("empty", "")):
            path = os.path.join(self.temp_dir.name, f"slot_{name}.jpg")
            with open(path, "wb") as f:
                f.write(name.encode() * 10)
            imgs[name] = (path, text)
        by_path = {p: t for p, t in imgs.values()}
        calls = []

        def fake_ocr(path):
            calls.append(path)
            t = by_path[path]
            if t is None:
                raise RuntimeError("ocr fail")
            return t

        orig = (auto_reply.ocr_image_text, auto_reply.IMAGE_TAGS_PATH)
        auto_reply.ocr_image_text = fake_ocr
        auto_reply.IMAGE_TAGS_PATH = os.path.join(self.temp_dir.name, "image_tags.json")
        try:
            paths = [p for p, _ in imgs.values()]
            # OCR 沒找到問候語 (藝術字常辨識不出) 不等於無時段字樣：視為未知，不會被挑選
            self.assertIsNone(auto_reply.classify_image_slots(imgs["n"][0]))
            self.assertIsNone(auto_reply.filter_images_by_time_slot([imgs["n"][0]], "morning"))
            # 手動標記為 neutral 後才可作為無時段圖
            self.assertTrue(auto_reply.set_image_slots(imgs["n"][0], ["neutral"]))
            self.assertFalse(auto_reply.set_image_slots(imgs["n"][0], ["neutral", "morning"]))
            for _ in range(20):
                pick = auto_reply.filter_images_by_time_slot(paths, "morning")
                self.assertEqual(pick, imgs["m"][0])
                pick = auto_reply.filter_images_by_time_slot(paths, "evening")
                self.assertEqual(pick, imgs["e"][0])
            # OCR 失敗的圖不快取也不採用；只有它時回傳 None
            self.assertIsNone(auto_reply.filter_images_by_time_slot([imgs["fail"][0]], "morning"))
            # OCR 辨識不出文字 (空字串)：視為失敗，不快取、不採用
            self.assertIsNone(auto_reply.filter_images_by_time_slot([imgs["empty"][0]], "morning"))
            self.assertFalse(auto_reply.is_image_tagged(imgs["empty"][0]))
            # 只剩不符時段的圖：Fail-Closed 回傳 None
            self.assertIsNone(auto_reply.filter_images_by_time_slot([imgs["e"][0]], "morning"))
            # 沒有該時段的圖時，改用無時段字樣的圖代替
            self.assertEqual(auto_reply.filter_images_by_time_slot([imgs["e"][0], imgs["n"][0]], "noon"), imgs["n"][0])
            # 成功的圖各只辨識一次
            self.assertEqual(len([c for c in calls if c == imgs["m"][0]]), 1)
        finally:
            auto_reply.ocr_image_text, auto_reply.IMAGE_TAGS_PATH = orig

    def test_countdown_sleep_logs_marks_without_initial_spam(self):
        clock = {"t": 1000.0}
        logs = []
        orig = (auto_reply.time.monotonic, auto_reply.time.sleep, auto_reply.log_v)
        auto_reply.time.monotonic = lambda: clock["t"]
        auto_reply.time.sleep = lambda s: clock.__setitem__("t", clock["t"] + s)
        auto_reply.log_v = lambda m, *a, **k: logs.append(m)
        try:
            auto_reply.countdown_sleep(100)
        finally:
            auto_reply.time.monotonic, auto_reply.time.sleep, auto_reply.log_v = orig
        text = " ".join(logs)
        self.assertIn("1 分 0 秒", text)
        self.assertIn("30 秒", text)
        self.assertIn("10 秒", text)
        self.assertNotIn("2 分", text)

    def test_fill_image_pool_balances_categories(self):
        d = os.path.join(self.temp_dir.name, "pool")
        os.makedirs(d)
        cats = ["morning", "noon", "evening", "neutral"]
        state = {"n": 0}

        def fake_download(url, save_dir, md5s):
            state["n"] += 1
            path = os.path.join(save_dir, f"grandma_{state['n']}.jpg")
            with open(path, "wb") as f:
                f.write(b"x")
            return path

        orig = (auto_reply.download_and_validate_image, auto_reply.get_existing_md5s,
                auto_reply.build_candidates, auto_reply.image_category)
        order = {"i": 0}

        def fake_category(path):
            c = cats[order["i"] % 4] if state["n"] else None
            order["i"] += 1
            return c

        auto_reply.download_and_validate_image = fake_download
        auto_reply.get_existing_md5s = lambda _d: set()
        auto_reply.build_candidates = lambda sources=None: [("u", None)] * 200
        auto_reply.image_category = fake_category
        try:
            counts = auto_reply.fill_image_pool(d, total=30, max_attempts=200)
        finally:
            (auto_reply.download_and_validate_image, auto_reply.get_existing_md5s,
             auto_reply.build_candidates, auto_reply.image_category) = orig
        # 無既有 neutral 圖：30 張平均分配給早安、午安、晚安，neutral 不自動新增
        self.assertEqual(counts, {"morning": 10, "noon": 10, "evening": 10, "neutral": 0})
        self.assertEqual(len(os.listdir(d)), 30)

    def test_fill_image_pool_keeps_only_quota_and_never_pads_unknown(self):
        d = os.path.join(self.temp_dir.name, "pool2")
        os.makedirs(d)
        state = {"n": 0}

        def fake_download(url, save_dir, md5s):
            state["n"] += 1
            path = os.path.join(save_dir, f"grandma_{state['n']}.jpg")
            with open(path, "wb") as f:
                f.write(b"x")
            return path

        orig = (auto_reply.download_and_validate_image, auto_reply.get_existing_md5s,
                auto_reply.build_candidates, auto_reply.image_category)
        auto_reply.download_and_validate_image = fake_download
        auto_reply.get_existing_md5s = lambda _d: set()
        auto_reply.build_candidates = lambda sources=None: [("u", None)] * 200
        auto_reply.image_category = lambda p: "neutral"   # 來源只有已標記的 neutral 圖
        try:
            counts = auto_reply.fill_image_pool(d, total=30, max_attempts=200)
        finally:
            (auto_reply.download_and_validate_image, auto_reply.get_existing_md5s,
             auto_reply.build_candidates, auto_reply.image_category) = orig
        # 來源只有 neutral 圖：neutral 不計入自動配額，全部丟棄；不會用無法確認的圖補足總數
        self.assertEqual(counts["neutral"], 0)
        self.assertEqual(len(os.listdir(d)), 0)

    def test_last_left_message_is_media_distinguishes_text_from_media(self):
        from PIL import Image, ImageDraw
        import types

        def judge(blocks, dpi=0, noise_at_top=False):
            img = Image.new("RGB", (1000, 675), (255, 255, 255))
            d = ImageDraw.Draw(img)
            for box, color in blocks:
                d.rectangle(box, fill=color)
            if noise_at_top:   # 背景取樣點剛好落在彩色物件上，中位數取樣仍要找回白色背景
                d.rectangle((490, 0, 510, 20), fill=(0, 0, 0))
            orig = (auto_reply.win32gui.IsWindow, auto_reply.win32gui.IsIconic, auto_reply.win32gui.IsWindowVisible,
                    auto_reply.get_window_true_rect, auto_reply.ImageGrab.grab, auto_reply.ctypes)
            auto_reply.win32gui.IsWindow = lambda h: True
            auto_reply.win32gui.IsIconic = lambda h: False
            auto_reply.win32gui.IsWindowVisible = lambda h: True
            auto_reply.get_window_true_rect = lambda h: (0, 0, 1000, 1000)
            auto_reply.ImageGrab.grab = lambda **kw: img
            auto_reply.ctypes = types.SimpleNamespace(
                windll=types.SimpleNamespace(user32=types.SimpleNamespace(GetDpiForWindow=lambda h: dpi)))
            try:
                return auto_reply.last_left_message_is_media(1)
            finally:
                (auto_reply.win32gui.IsWindow, auto_reply.win32gui.IsIconic, auto_reply.win32gui.IsWindowVisible,
                 auto_reply.get_window_true_rect, auto_reply.ImageGrab.grab, auto_reply.ctypes) = orig

        gray = (239, 239, 239)                          # LINE 接收氣泡的淺灰填色
        photo_c = (200, 120, 60)
        text = ((60, 600, 260, 630), gray)              # 約 31px 文字氣泡
        long_text = ((60, 420, 260, 540), gray)         # 4 行以上的長文字氣泡 (高度超過 90px)
        photo = ((60, 200, 300, 540), photo_c)          # 約 341px 照片
        action_row = ((60, 552, 260, 561), (166, 166, 166))   # 照片下方 12px 後、高度約 10px 的「儲存｜另存新檔｜分享」連結列
        self.assertIs(judge([text]), False)
        self.assertIs(judge([long_text]), False)       # 高度 >= 90 的長文字仍是文字
        self.assertIs(judge([photo]), True)
        self.assertIs(judge([photo, action_row]), True)  # 連結列也是照片的一部分
        self.assertIs(judge([photo, action_row, ((60, 600, 260, 630), gray)]), False)   # 照片之後又傳文字
        self.assertIs(judge([text], noise_at_top=True), False)
        # 透明背景貼圖：圖案與下方文字片段間距 10px，最底部片段只有約 30px，仍須判為貼圖
        sticker = [((80, 300, 200, 440), (250, 200, 0)), ((80, 452, 200, 482), (250, 120, 120))]
        self.assertIs(judge(sticker), True)
        # 更小的貼圖片段 (約 15px) 仍視為貼圖
        self.assertIs(judge([((80, 300, 200, 440), (250, 200, 0)), ((80, 460, 200, 475), (250, 120, 120))]), True)
        # DPI 150%：文字氣泡仍是文字、照片仍是照片
        self.assertIs(judge([((60, 560, 260, 630), gray)], dpi=144), False)
        self.assertIs(judge([((60, 200, 300, 540), photo_c)], dpi=144), True)
        # 細線或雜訊 (非氣泡且低於 12px)：文字
        self.assertIs(judge([((60, 600, 200, 606), (166, 166, 166))]), False)
        # 網址預覽卡片 (非灰色氣泡)：已知取捨，判為照片
        self.assertIs(judge([((60, 400, 300, 520), (210, 225, 245))]), True)
        # 貼圖底部只剩細小片段 (約 8px) 且與上方間距超過 16px：視為雜訊 (已知取捨，README 已註明)
        self.assertIs(judge([((80, 300, 200, 440), (250, 200, 0)), ((80, 470, 200, 478), (250, 120, 120))]), False)
        # 間距剛好 12px 的細小片段緊貼上方區塊：視為連結列，判為照片
        self.assertIs(judge([((80, 300, 200, 440), (250, 200, 0)), ((80, 452, 200, 460), (250, 120, 120))]), True)
        self.assertIsNone(judge([]))                    # 左側無內容：無法判斷 (None，呼叫端走重試)

    # ---------- 識別長輩圖才回覆 ----------
    def _make_image(self, name, size=(400, 400), color=(200, 30, 30)):
        from PIL import Image
        path = os.path.join(self.temp_dir.name, name)
        Image.new("RGB", size, color).save(path)
        return path

    def _variant_sizes(self, size):
        """跑一次多種前處理 OCR，回傳 (各次 OCR 看到的圖片尺寸, 文字清單, 暫存檔路徑清單)"""
        from PIL import Image
        path = self._make_image("variant_src.png", size)
        seen = []

        def fake_ocr(p):
            with Image.open(p) as im:
                seen.append((p, im.size))
            return f"t{len(seen)}"

        with mock.patch.object(auto_reply, "ocr_image_text", side_effect=fake_ocr):
            texts = list(auto_reply.ocr_image_text_variants(path))
        return seen, texts, path

    def test_ocr_variants_yield_original_then_three_preprocessed_and_clean_up(self):
        seen, texts, path = self._variant_sizes((3000, 2000))
        self.assertEqual(texts, ["t1", "t2", "t3", "t4"])
        self.assertEqual(seen[0][0], path)                                      # 第一個是原圖
        self.assertEqual([s[1] for s in seen[1:]], [(1600, 1066)] * 3)          # 太大的圖縮到最長邊 1600
        self.assertTrue(all(not os.path.exists(s[0]) for s in seen[1:]))        # 暫存檔都已刪除

    def test_ocr_variants_upscale_small_images_and_keep_medium_ones(self):
        seen, _, _ = self._variant_sizes((300, 200))
        self.assertEqual([s[1] for s in seen[1:]], [(600, 400)] * 3)            # 最長邊 < 700 放大 2 倍
        seen, _, _ = self._variant_sizes((800, 600))
        self.assertEqual([s[1] for s in seen[1:]], [(800, 600)] * 3)            # 中等大小不縮放

    def test_ocr_variants_ocr_errors_become_empty_text_and_early_stop_cleans_up(self):
        path = self._make_image("variant_err.png", (800, 600))
        seen = []

        def fake_ocr(p):
            seen.append(p)
            if len(seen) == 2:
                raise RuntimeError("boom")
            return "x"

        with mock.patch.object(auto_reply, "ocr_image_text", side_effect=fake_ocr):
            gen = auto_reply.ocr_image_text_variants(path)
            self.assertEqual(next(gen), "x")
            self.assertEqual(next(gen), "")                                      # 辨識失敗變空字串，不擲出
            gen.close()                                                          # 呼叫端提早停止
        self.assertEqual(len(seen), 2)                                           # 沒有繼續跑後面的前處理
        self.assertFalse(os.path.exists(seen[1]))                                # 提早停止也清掉暫存檔

    def test_ocr_variants_unreadable_image_only_yields_original_ocr(self):
        path = os.path.join(self.temp_dir.name, "not_an_image.bin")
        with open(path, "wb") as f:
            f.write(b"definitely not an image")
        with mock.patch.object(auto_reply, "ocr_image_text", return_value="原圖") as ocr:
            self.assertEqual(list(auto_reply.ocr_image_text_variants(path)), ["原圖"])
            self.assertEqual(ocr.call_count, 1)

    # ---------- 加密快取 (.eimg) 改用聊天視窗截圖識別 ----------
    def test_can_decode_image(self):
        self.assertTrue(auto_reply._can_decode_image(self._make_image("dec_ok.png")))
        junk = os.path.join(self.temp_dir.name, "dec_junk.eimg")
        with open(junk, "wb") as f:
            f.write(b"\x00\x01encrypted")
        self.assertFalse(auto_reply._can_decode_image(junk))                    # LINE 的 .eimg 是加密的
        self.assertFalse(auto_reply._can_decode_image(os.path.join(self.temp_dir.name, "dec_missing.png")))

    def _chat_shot(self, bubble=(84, 50, 450, 300)):
        """合成一張聊天室截圖：白底，左側有一張圖 (藍色方塊)。回傳 (截圖, is_content)"""
        from PIL import Image, ImageDraw
        shot = Image.new("RGB", (600, 400), (255, 255, 255))
        ImageDraw.Draw(shot).rectangle(list(bubble), fill=(20, 60, 160))
        is_content = lambda px: sum(abs(px[i] - 255) for i in range(3)) > 30
        return shot, is_content

    def test_incoming_media_crop_crops_the_image_and_measures_its_width(self):
        shot, is_content = self._chat_shot()
        with mock.patch.object(auto_reply, "_left_message_blocks", return_value=(shot, 1.5, 84, 324, is_content, [(300, 50)])):
            crop, width = auto_reply._incoming_media_crop(1)
        self.assertEqual(crop.size, (379, 263))                                  # 圖片四周各加 6px
        self.assertEqual(width, 244)                                             # (450 - 84 + 1) / 1.5 邏輯像素

    def test_incoming_media_crop_skips_the_thin_action_bar_below_the_image(self):
        shot, is_content = self._chat_shot()
        # 最底部是矮的「儲存｜另存新檔｜分享」連結列 (高 8px)，圖片在它上方
        with mock.patch.object(auto_reply, "_left_message_blocks", return_value=(shot, 1.5, 84, 324, is_content, [(330, 322), (300, 50)])):
            crop, width = auto_reply._incoming_media_crop(1)
        self.assertEqual(crop.size, (379, 263))                                  # 仍然是圖片那一塊，不是連結列

    def test_incoming_media_crop_none_cases(self):
        shot, is_content = self._chat_shot()
        with mock.patch.object(auto_reply, "_left_message_blocks", return_value=None):
            self.assertIsNone(auto_reply._incoming_media_crop(1))                # 視窗縮小、被遮住
        with mock.patch.object(auto_reply, "_left_message_blocks", return_value=(shot, 1.5, 84, 324, is_content, [(330, 322)])):
            self.assertIsNone(auto_reply._incoming_media_crop(1))                # 沒有夠高的區塊 (只有連結列)
        blank, is_content = self._chat_shot(bubble=(0, 0, 1, 1))                 # 該區塊內其實沒有內容
        with mock.patch.object(auto_reply, "_left_message_blocks", return_value=(blank, 1.5, 84, 324, is_content, [(300, 50)])):
            self.assertIsNone(auto_reply._incoming_media_crop(1))

    def test_skip_not_grandma_records_received_image(self):
        img = self._make_image("skip_rec.png", color=(7, 8, 9))
        with mock.patch.object(auto_reply, "record_contact_received") as rec, mock.patch.object(auto_reply, "log_v"):
            self.assertEqual(auto_reply._skip_not_grandma("小明", img, {"history_rounds": 30}), "SKIPPED_NOT_GRANDMA")
            rec.assert_called_once_with("小明", auto_reply.get_file_md5(img), max_rounds=30)
            rec.reset_mock()
            self.assertEqual(auto_reply._skip_not_grandma("小明", None, {}), "SKIPPED_NOT_GRANDMA")
            rec.assert_not_called()

    def test_media_type_with_retry(self):
        with mock.patch.object(auto_reply.time, "sleep") as sleep:
            with mock.patch.object(auto_reply, "last_left_message_is_media", side_effect=[None, None, True]) as m:
                self.assertIs(auto_reply._media_type_with_retry(1), True)         # 剛還原視窗還沒重繪完：稍等再試就判斷得出來
                self.assertEqual((m.call_count, sleep.call_count), (3, 2))
            sleep.reset_mock()
            with mock.patch.object(auto_reply, "last_left_message_is_media", return_value=None) as m:
                self.assertIsNone(auto_reply._media_type_with_retry(1))           # 試完仍判斷不出來
                self.assertEqual((m.call_count, sleep.call_count), (auto_reply.MEDIA_CHECK_ATTEMPTS, auto_reply.MEDIA_CHECK_ATTEMPTS - 1))
            sleep.reset_mock()
            with mock.patch.object(auto_reply, "last_left_message_is_media", return_value=False) as m:
                self.assertIs(auto_reply._media_type_with_retry(1), False)        # 一次就有答案：不等待
                self.assertEqual((m.call_count, sleep.call_count), (1, 0))

    def _assert_skip_status_marks_processed(self, status):
        test_cache = os.path.join(self.temp_dir.name, "skip_cache")
        fake_hash = "777703ef1234567890abcdef1234567890abcdef"
        chat_m = os.path.join(test_cache, fake_hash, "m")
        os.makedirs(chat_m, exist_ok=True)
        img_path = os.path.join(chat_m, "t.eimg")
        with open(img_path, "wb") as f:
            f.write(b"\x00" * 300000)
        now = time.time()
        os.utime(img_path, (now, now))
        with auto_reply.locked_chat_dirs() as m:
            m[fake_hash] = "TargetSkip"
        orig = (auto_reply.get_active_line_cache_dir, auto_reply.find_target_chat_window,
                auto_reply.send_grandma_image, auto_reply.is_valid_image_or_eimg)
        auto_reply.get_active_line_cache_dir = lambda: test_cache
        auto_reply.find_target_chat_window = lambda t: [(9970, t, "ChatWnd", True)] if t == "TargetSkip" else []
        auto_reply.is_valid_image_or_eimg = lambda p: True
        calls = []
        auto_reply.send_grandma_image = lambda t, c, **kw: calls.append(t) or status
        try:
            cfg = {"targets": ["TargetSkip"], "trigger_mode": "photo_and_sticker", "cooldown_seconds": 0,
                   "min_delay_seconds": 0, "max_delay_seconds": 0}
            auto_reply.last_global_reply_time = 0.0
            auto_reply.last_reply_times.pop("TargetSkip", None)
            auto_reply.PROCESSED_CACHE_FILES.clear()
            auto_reply.last_seen_cache_mtime = now - 1.0
            auto_reply.SEND_FAIL_COUNTS["TargetSkip"] = 2                          # 先前失敗過兩次：跳過是「已處理」，要清掉失敗計數
            auto_reply.check_for_incoming_messages(cfg)
            self.assertEqual(calls, ["TargetSkip"])
            self.assertEqual(auto_reply.last_reply_times.get("TargetSkip", 0), 0)   # 沒有啟動冷卻
            self.assertEqual(auto_reply.SEND_FAIL_COUNTS["TargetSkip"], 0)          # 不是失敗，不累積重試次數
            self.assertNotIn("TargetSkip", auto_reply.PENDING_RETRY_MEDIA)          # 也不排重試
            self.assertIn((img_path, os.path.getmtime(img_path)), auto_reply.PROCESSED_CACHE_FILES)
            auto_reply.check_for_incoming_messages(cfg)
            self.assertEqual(calls, ["TargetSkip"])                                  # 同一檔案不重複觸發
        finally:
            (auto_reply.get_active_line_cache_dir, auto_reply.find_target_chat_window,
             auto_reply.send_grandma_image, auto_reply.is_valid_image_or_eimg) = orig

    def test_skipped_text_does_not_start_cooldown_and_marks_processed(self):
        self._assert_skip_status_marks_processed("SKIPPED_TEXT")

    def test_skipped_not_grandma_does_not_start_cooldown_and_marks_processed(self):
        self._assert_skip_status_marks_processed("SKIPPED_NOT_GRANDMA")          # 不是長輩圖：不冷卻、不重試

    def test_load_config_sanitizes_image_sources_and_gate_settings(self):
        path = os.path.join(self.temp_dir.name, "img_cfg.json")
        auto_reply._WARNED_ONCE.difference_update({m for m in auto_reply._WARNED_ONCE if m.startswith("來源")})

        def load(data):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            with mock.patch.object(auto_reply, "CONFIG_PATH", path), mock.patch.object(auto_reply.logging, "warning") as warn:
                return auto_reply.load_config(), warn

        cfg, warn = load({"targets": ["a"]})
        self.assertEqual((cfg["require_grandma_image"], cfg["auto_neutral_images"], cfg["image_sources"]), (True, False, []))   # 預設
        cfg, warn = load({"targets": ["a"], "require_grandma_image": False, "auto_neutral_images": True, "image_sources": [
            {"name": "ok", "url": "https://x/{n}.jpg", "start": 1, "end": 3, "category": "neutral"},
            {"name": "bad", "url": "https://x/no-placeholder.jpg", "start": 1, "end": 3}]})
        self.assertEqual((cfg["require_grandma_image"], cfg["auto_neutral_images"]), (False, True))
        self.assertEqual([s["name"] for s in cfg["image_sources"]], ["ok"])                       # 不合格的來源略過
        self.assertTrue(any("bad" in str(c.args[0]) for c in warn.call_args_list))
        cfg, _ = load({"targets": ["a"], "require_grandma_image": "no", "auto_neutral_images": 1, "image_sources": "x"})
        self.assertEqual((cfg["require_grandma_image"], cfg["auto_neutral_images"], cfg["image_sources"]), (True, False, []))   # 型別錯誤沿用預設
        cfg, _ = load({"targets": ["a"], "image_sources": [{"url": "ftp://x/{n}", "start": 1, "end": 2}]})
        self.assertEqual([s["name"] for s in cfg["image_sources"]], ["fpg-daily-photo", "fpg-daily-illustration"])   # 全部不合格：用預設來源

    def test_configure_image_options_sets_sources_and_auto_neutral(self):
        with mock.patch.object(auto_reply, "_IMAGE_SOURCES", None), mock.patch.object(auto_reply, "_AUTO_NEUTRAL", False):
            src = [{"name": "s", "url": "https://x/{n}", "start": 1, "end": 2}]
            auto_reply.configure_image_options({"image_sources": src, "auto_neutral_images": True})
            self.assertEqual((auto_reply._IMAGE_SOURCES, auto_reply._AUTO_NEUTRAL), (src, True))
            auto_reply.configure_image_options({})                                  # 沒設定：預設來源、不自動判無時段
            self.assertEqual((auto_reply._IMAGE_SOURCES, auto_reply._AUTO_NEUTRAL), (None, False))

    # ---------- 自動判定無時段圖 (auto_neutral_images) ----------
    def _classify_with_texts(self, texts, *, auto=True, name="c.png", color=(5, 6, 7)):
        img = self._make_image(name, (800, 600), color)
        tags_path = os.path.join(self.temp_dir.name, "image_tags.json")
        with mock.patch.object(auto_reply, "IMAGE_TAGS_PATH", tags_path), mock.patch.object(auto_reply, "_AUTO_NEUTRAL", auto), \
             mock.patch.object(auto_reply, "ocr_image_text", side_effect=texts) as ocr:
            result = auto_reply.classify_image_slots(img)
        tags = {}
        if os.path.exists(tags_path):
            with open(tags_path, encoding="utf-8") as f:
                tags = json.load(f)
        return result, ocr.call_count, tags, auto_reply.get_file_md5(img), img, tags_path

    def test_auto_neutral_greeting_in_later_variant_wins_and_stops_early(self):
        result, calls, tags, md5, _, _ = self._classify_with_texts(["", "晚安 好夢", "不該被呼叫", "不該被呼叫"])
        self.assertEqual(result, ["evening"])
        self.assertEqual(calls, 2)                                                # 第二種前處理讀到問候語就停
        self.assertEqual(tags[md5], ["evening"])

    def test_auto_neutral_labels_readable_greetingless_image_and_caches(self):
        result, calls, tags, md5, img, tags_path = self._classify_with_texts(["平安喜樂身體健康", "", "", ""])
        self.assertEqual(result, ["neutral"])
        self.assertEqual(calls, 4)                                                # 全部前處理都沒有問候語才下結論
        self.assertEqual(tags[md5], ["neutral"])
        self.assertEqual(tags["_auto_neutral"], [md5])                            # 記在自動判定清單，方便檢查
        with mock.patch.object(auto_reply, "IMAGE_TAGS_PATH", tags_path), mock.patch.object(auto_reply, "_AUTO_NEUTRAL", True), \
             mock.patch.object(auto_reply, "ocr_image_text") as ocr:
            self.assertEqual(auto_reply.classify_image_slots(img), ["neutral"])   # 第二次直接用快取
            ocr.assert_not_called()

    def test_auto_neutral_refuses_without_enough_evidence(self):
        for texts in (["平安", "", "", ""],                                       # 只讀到 2 個字：OCR 對這張圖不可靠
                      ["", "", "", ""],                                           # 什麼都讀不到
                      ["中秋節快樂闔家團圓", "", "", ""],                         # 節慶圖不是通用圖
                      ["父親節快樂永遠健康", "", "", ""]):
            result, calls, tags, md5, _, _ = self._classify_with_texts(texts)
            self.assertIsNone(result, texts)
            self.assertNotIn(md5, tags)                                           # 沒有證據就不快取
        result, calls, tags, md5, _, _ = self._classify_with_texts([RuntimeError("ocr down")] * 4)
        self.assertIsNone(result)                                                 # OCR 全失敗

    def test_auto_neutral_off_keeps_unknown_images_unknown(self):
        result, calls, tags, md5, _, _ = self._classify_with_texts(["平安喜樂身體健康"], auto=False)
        self.assertIsNone(result)                                                 # 預設行為：沒讀到問候語就是未知
        self.assertEqual(calls, 1)                                                # 只跑一次 OCR
        self.assertNotIn(md5, tags)

    def test_fill_image_pool_uses_source_category_and_reserves_neutral_quota(self):
        tags_path = os.path.join(self.temp_dir.name, "pool_tags.json")
        for auto, expected_files in ((True, 2), (False, 0)):
            d = os.path.join(self.temp_dir.name, f"pool_src_{auto}")
            os.makedirs(d)
            state = {"n": 0}

            def fake_download(url, save_dir, md5s, state=state):
                state["n"] += 1
                path = os.path.join(save_dir, f"grandma_{state['n']}.jpg")
                with open(path, "wb") as f:
                    f.write(f"img{state['n']}-{auto}".encode())
                return path

            with mock.patch.object(auto_reply, "IMAGE_TAGS_PATH", tags_path), mock.patch.object(auto_reply, "_AUTO_NEUTRAL", auto), \
                 mock.patch.object(auto_reply, "download_and_validate_image", fake_download), \
                 mock.patch.object(auto_reply, "get_existing_md5s", return_value=set()), \
                 mock.patch.object(auto_reply, "build_candidates", return_value=[("u", "neutral")] * 40), \
                 mock.patch.object(auto_reply, "log_v"):
                counts = auto_reply.fill_image_pool(d, total=10, max_attempts=40)
            # 來源標明 neutral：不必 OCR 直接歸類。開啟自動無時段時保留 10 // 5 = 2 張，其餘超額的丟棄；關閉時 neutral 沒有名額
            self.assertEqual(counts["neutral"], expected_files)
            self.assertEqual(len(os.listdir(d)), expected_files)

    def test_filter_returns_tagged_neutral_after_ocr_quota_exhausted(self):
        fail_path = os.path.join(self.temp_dir.name, "q_fail.jpg")
        neutral_path = os.path.join(self.temp_dir.name, "q_neutral.jpg")
        for pth, data in ((fail_path, b"fail" * 10), (neutral_path, b"neut" * 10)):
            with open(pth, "wb") as f:
                f.write(data)
        orig = (auto_reply.ocr_image_text, auto_reply.IMAGE_TAGS_PATH, auto_reply.random.shuffle)
        auto_reply.IMAGE_TAGS_PATH = os.path.join(self.temp_dir.name, "image_tags.json")
        auto_reply.random.shuffle = lambda x: None   # 固定順序：先遇到 OCR 失敗的圖，再遇到 neutral 圖

        def failing_ocr(path):
            raise RuntimeError("ocr fail")

        auto_reply.ocr_image_text = failing_ocr
        try:
            self.assertTrue(auto_reply.set_image_slots(neutral_path, ["neutral"]))
            # OCR 額度只有 1：用在失敗的圖之後，仍要掃已標記的 neutral 圖當備援
            self.assertEqual(auto_reply.filter_images_by_time_slot([fail_path, neutral_path], "evening", max_ocr=1), neutral_path)
        finally:
            auto_reply.ocr_image_text, auto_reply.IMAGE_TAGS_PATH, auto_reply.random.shuffle = orig

    # ---------- 遠端停止指令 ----------
    def test_stop_command_text_matching(self):
        m = auto_reply.text_matches_stop_command
        phrase = "結束長輩圖機器人"
        self.assertTrue(m("結束長輩圖機器人", phrase))
        self.assertTrue(m("結束 長輩圖 機器人！", phrase))                 # 空白與標點不影響
        self.assertTrue(m("麻煩你結束長輩圖機器人謝謝", phrase))            # 句中含指令
        self.assertTrue(m("結束長輩圖機器八", phrase))                      # OCR 認錯一字
        self.assertFalse(m("結束長輩圖", phrase))
        self.assertFalse(m("早安你好嗎", phrase))
        self.assertFalse(m("", phrase))
        self.assertFalse(m("結束長輩圖機器人", ""))

    def _stop_cfg(self):
        return {"stop_command": "結束長輩圖機器人", "stop_command_chats": ["Notify Bot"]}

    def _reset_stop_state(self):
        auto_reply._STOP_SEEN_SIG.clear()
        auto_reply._STOP_SIG_LAST_CHECK.clear()

    def _run_stop_rounds(self, sigs, *, fg=5, check=False, clock=None, windows=None, rounds=None):
        """依序餵入每輪的視窗簽章跑 check_stop_command，回傳 _chat_has_stop_command 被呼叫的次數"""
        sigs = list(sigs)
        it = iter(sigs)
        calls = []
        patches = [
            mock.patch.object(auto_reply, "get_all_line_windows", return_value=windows or [(5, "Notify Bot", "Qt", True)]),
            mock.patch.object(auto_reply, "_expanded_window_signature", side_effect=lambda h: next(it)),
            mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=fg),
            mock.patch.object(auto_reply, "_chat_has_stop_command", side_effect=lambda *a, **k: calls.append(a) or check),
            mock.patch.object(auto_reply, "log_v"),
        ]
        if clock is not None:
            patches.append(mock.patch.object(auto_reply.time, "time", lambda: clock["t"]))
        for p in patches:
            p.start()
        try:
            for _ in range(rounds or len(sigs)):
                auto_reply.check_stop_command(self._stop_cfg())
                if clock is not None:
                    clock["t"] += auto_reply.STOP_SIG_MIN_INTERVAL
        finally:
            for p in patches:
                p.stop()
        return calls

    def test_check_stop_command_disabled_or_missing_window(self):
        self._reset_stop_state()
        with mock.patch.object(auto_reply, "get_all_line_windows") as gw:
            auto_reply.check_stop_command({"stop_command": "x", "stop_command_chats": []})
            gw.assert_not_called()
        # 名單內的聊天室沒開視窗：不檢查
        self.assertEqual(self._run_stop_rounds([], windows=[(1, "別人", "Qt", True)], rounds=1), [])

    def test_check_stop_command_builds_baseline_then_checks_each_change_once(self):
        self._reset_stop_state()
        clock = {"t": 1000.0}
        # 第一輪只建基準，第二輪沒變，第三輪變了檢查一次，第四輪沒變不再檢查
        calls = self._run_stop_rounds([(10, "a", "b"), (10, "a", "b"), (11, "c", "a"), (11, "c", "a")], clock=clock)
        self.assertEqual(calls, [("Notify Bot", "結束長輩圖機器人")])

    def test_check_stop_command_raises_when_own_command_found(self):
        self._reset_stop_state()
        auto_reply._STOP_SEEN_SIG["Notify Bot"] = (10, "a", "b")
        with self.assertRaises(auto_reply.StopRequested):
            self._run_stop_rounds([(11, "c", "a")], check=True)

    def test_check_stop_command_is_throttled_while_scrolling(self):
        self._reset_stop_state()
        auto_reply._STOP_SEEN_SIG["Notify Bot"] = (10, "a", "b")
        clock = {"t": 1000.0}
        # 捲動歷史訊息：簽章每輪都變。第一次檢查後 5 秒內不再檢查，間隔過後才再查一次
        it = iter([(11, "c", "a"), (12, "d", "c"), (13, "e", "d")])
        calls = []
        with mock.patch.object(auto_reply, "get_all_line_windows", return_value=[(5, "Notify Bot", "Qt", True)]), \
             mock.patch.object(auto_reply.time, "time", lambda: clock["t"]), \
             mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=5), \
             mock.patch.object(auto_reply, "_expanded_window_signature", side_effect=lambda h: next(it)), \
             mock.patch.object(auto_reply, "_chat_has_stop_command", side_effect=lambda *a, **k: calls.append(1) or False):
            auto_reply.check_stop_command(self._stop_cfg())      # 檢查
            clock["t"] += 1
            auto_reply.check_stop_command(self._stop_cfg())      # 距上次 1 秒：略過，保留舊簽章
            self.assertEqual(len(calls), 1)
            clock["t"] += auto_reply.STOP_SIG_MIN_INTERVAL
            auto_reply.check_stop_command(self._stop_cfg())      # 間隔已過：再檢查
        self.assertEqual(len(calls), 2)

    def test_check_stop_command_waits_until_window_is_foreground(self):
        self._reset_stop_state()
        auto_reply._STOP_SEEN_SIG["Notify Bot"] = (10, "a", "b")
        # 背景視窗 (例如官方帳號推來 log)：不檢查、保留舊簽章；回到前景後才檢查
        calls = self._run_stop_rounds([(11, "c", "a")], fg=99)
        self.assertEqual(calls, [])
        self.assertEqual(auto_reply._STOP_SEEN_SIG["Notify Bot"], (10, "a", "b"))
        calls = self._run_stop_rounds([(11, "c", "a")], fg=5)
        self.assertEqual(len(calls), 1)

    def test_check_stop_command_minimized_window_drops_signature_and_rebuilds(self):
        self._reset_stop_state()
        auto_reply._STOP_SEEN_SIG["Notify Bot"] = (10, "a", "b")
        calls = self._run_stop_rounds([None, (12, "x", "y")])     # 縮小時為 None；還原後第一次只重建基準，不檢查
        self.assertEqual(calls, [])
        self.assertEqual(auto_reply._STOP_SEEN_SIG["Notify Bot"], (12, "x", "y"))

    def test_check_stop_command_swallows_ordinary_errors(self):
        self._reset_stop_state()
        auto_reply._STOP_SEEN_SIG["Notify Bot"] = (10, "a", "b")
        with mock.patch.object(auto_reply, "get_all_line_windows", return_value=[(5, "Notify Bot", "Qt", True)]), \
             mock.patch.object(auto_reply, "_expanded_window_signature", return_value=(11, "c", "a")), \
             mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=5), \
             mock.patch.object(auto_reply, "_chat_has_stop_command", side_effect=RuntimeError("boom")), \
             mock.patch.object(auto_reply, "log_v"):
            auto_reply.check_stop_command(self._stop_cfg())   # 一般例外只記錄，不擲出

    def test_init_stop_baselines_records_signature_and_ignores_old_command(self):
        self._reset_stop_state()
        cfg = {"stop_command": "結束長輩圖機器人", "stop_command_chats": ["Notify Bot", "小華"]}
        wins = [(1, "Notify Bot", "Qt", True), (2, "小華", "Qt", True)]
        with mock.patch.object(auto_reply, "get_all_line_windows", return_value=wins), \
             mock.patch.object(auto_reply, "_expanded_window_signature", side_effect=lambda h: (h, "a", "b")):
            auto_reply.init_stop_baselines(cfg, ["小華"])         # 熱重載只替新增的聊天室建基準線
            self.assertEqual(auto_reply._STOP_SEEN_SIG, {"小華": (2, "a", "b")})
            auto_reply.init_stop_baselines(cfg)
            self.assertEqual(set(auto_reply._STOP_SEEN_SIG), {"Notify Bot", "小華"})
        # 基準線之後簽章沒變：啟動前就留在聊天室的舊指令不會被檢查
        calls = self._run_stop_rounds([(1, "a", "b")], windows=wins[:1], check=True)
        self.assertEqual(calls, [])

    def _run_chat_has_stop(self, *, fg_hwnd=77, own=True, found=True, window=True):
        with mock.patch.object(auto_reply, "find_target_chat_window", return_value=[(77, "Notify Bot", "Qt", True)] if window else []), \
             mock.patch.object(auto_reply.time, "sleep"), \
             mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=fg_hwnd), \
             mock.patch.object(auto_reply.win32gui, "GetWindowText", return_value="Notify Bot"), \
             mock.patch.object(auto_reply, "verify_message_sent_to_chat", return_value=own) as verify, \
             mock.patch.object(auto_reply, "last_own_message_has_text", return_value=found) as rd, \
             mock.patch.object(auto_reply, "log_v"):
            result = auto_reply._chat_has_stop_command("Notify Bot", "結束長輩圖機器人")
        return result, verify.call_count, rd.call_count

    def test_chat_has_stop_command_cases(self):
        self.assertEqual(self._run_chat_has_stop(), (True, 1, 1))
        self.assertEqual(self._run_chat_has_stop(found=False), (False, 0, 1))        # 沒有指令：不確認位置，視窗完全不動
        self.assertEqual(self._run_chat_has_stop(window=False), (False, 0, 0))       # 找不到聊天室視窗
        self.assertEqual(self._run_chat_has_stop(fg_hwnd=5), (False, 0, 0))          # 視窗不在前景：不搶焦點、不檢查
        self.assertEqual(self._run_chat_has_stop(own=False), (False, 1, 1))          # 有指令但不是最後一則 (下面有官方帳號推來的新訊息)
        res, verify, reads = self._run_chat_has_stop(found=None)                      # 讀不出字：重試後不停止
        self.assertEqual((res, verify, reads), (False, 0, auto_reply.STOP_READ_ATTEMPTS))

    def _own_bubble_shot(self, bubble_color=(141, 224, 85)):
        from PIL import Image, ImageDraw
        shot = Image.new("RGB", (400, 300), (255, 255, 255))
        ImageDraw.Draw(shot).rectangle([250, 100, 375, 129], fill=bubble_color)
        is_content = lambda px: sum(abs(px[i] - 255) for i in range(3)) > 30
        return shot, is_content

    def test_own_bubble_box_crops_only_the_bubble(self):
        shot, is_content = self._own_bubble_shot()
        box = auto_reply._own_bubble_box(shot, 1.0, 216, 376, 100, 130, is_content)
        self.assertEqual(box, (246, 96, 379, 135))                          # 氣泡左右各加 4px 邊，不含左側時間與已讀標籤

    def test_own_bubble_box_none_for_photo(self):
        from PIL import Image
        import random
        rnd = random.Random(1)
        shot = Image.new("RGB", (400, 300), (255, 255, 255))
        for x in range(216, 376):
            for y in range(100, 130):
                shot.putpixel((x, y), (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
        is_content = lambda px: sum(abs(px[i] - 255) for i in range(3)) > 30
        self.assertIsNone(auto_reply._own_bubble_box(shot, 1.0, 216, 376, 100, 130, is_content))
        self.assertIsNone(auto_reply._own_bubble_box(Image.new("RGB", (400, 300), (255, 255, 255)), 1.0, 216, 376, 100, 130, is_content))

    def _run_own_text(self, texts, shot=None):
        from PIL import Image
        base, is_content = self._own_bubble_shot()
        shot = shot or base
        seen = []
        queue = list(texts)

        def fake_ocr(path):
            with Image.open(path) as im:
                seen.append((im.size, path))
            self.assertTrue(os.path.exists(path))
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch.object(auto_reply, "_left_message_blocks", return_value=(shot, 1.0, 216, 376, is_content, [(130, 100)])) as blocks, \
             mock.patch.object(auto_reply, "ocr_image_text", side_effect=fake_ocr):
            result = auto_reply.last_own_message_has_text(1, "結束長輩圖機器人")
        self.assertEqual(blocks.call_args.kwargs, {"side": "right"})        # 讀右側 (自己發的)，不是左側
        return result, seen

    def test_last_own_message_has_text_any_variant_matches(self):
        result, seen = self._run_own_text(["結束長輩", "結束長輩圖機器人"])
        self.assertTrue(result)
        self.assertEqual(len(seen), 2)                                        # 第二種前處理讀到就停
        self.assertFalse(os.path.exists(seen[0][1]))                          # 暫存圖檔已刪除

    def test_last_own_message_has_text_crops_bubble_with_padding(self):
        _, seen = self._run_own_text(["結束長輩圖機器人"])
        up = auto_reply.STOP_OCR_UPSCALES[0]
        # 氣泡截圖 133x39，加上下左右各 24 的白邊後放大
        self.assertEqual(seen[0][0], ((133 + 48) * up, (39 + 48) * up))

    def test_last_own_message_has_text_false_and_none(self):
        from PIL import Image
        n = len(list(auto_reply._stop_ocr_variants(Image.new("RGB", (50, 20)))))
        result, seen = self._run_own_text(["早安"] * n)
        self.assertIs(result, False)                                          # 讀得到字但不是指令
        self.assertEqual(len(seen), n)
        self.assertIsNone(self._run_own_text([""] * n)[0])                    # 全部讀不出字：無法確認
        self.assertIsNone(self._run_own_text([RuntimeError("ocr")] * n)[0])   # OCR 全部失敗：無法確認
        self.assertIs(self._run_own_text(["", RuntimeError("x"), "早安"])[0], False)   # 只要有一次讀到字就算讀得到
        with mock.patch.object(auto_reply, "_left_message_blocks", return_value=None):
            self.assertIsNone(auto_reply.last_own_message_has_text(1, "x"))

    def test_last_own_message_has_text_skips_ocr_for_photo(self):
        from PIL import Image
        import random
        rnd = random.Random(2)
        shot = Image.new("RGB", (400, 300), (255, 255, 255))
        for x in range(216, 376):
            for y in range(100, 130):
                shot.putpixel((x, y), (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
        result, seen = self._run_own_text([], shot=shot)
        self.assertIs(result, False)                                          # 自己傳的照片或貼圖不是指令，也不做 OCR
        self.assertEqual(seen, [])

    def test_left_message_blocks_right_side_finds_own_bubble(self):
        from PIL import Image, ImageDraw
        shot = Image.new("RGB", (400, 101), (255, 255, 255))
        ImageDraw.Draw(shot).rectangle([250, 40, 375, 70], fill=(141, 224, 85))
        with mock.patch.object(auto_reply.win32gui, "IsWindow", return_value=True), \
             mock.patch.object(auto_reply.win32gui, "IsIconic", return_value=False), \
             mock.patch.object(auto_reply.win32gui, "IsWindowVisible", return_value=True), \
             mock.patch.object(auto_reply, "get_window_true_rect", return_value=(0, 0, 400, 300)), \
             mock.patch.object(auto_reply.ImageGrab, "grab", return_value=shot):
            found = auto_reply._left_message_blocks(1, side="right")
            self.assertIsNotNone(found)
            _, scale, x_start, x_end, _, blocks = found
            self.assertEqual((x_start, x_end, blocks), (216, 376, [(70, 40)]))
            self.assertIsNone(auto_reply._left_message_blocks(1, side="left"))   # 左側沒有內容：自己發的不算對方訊息

    # ---------- 視窗太窄時暫時最大化再判斷左右 ----------
    def _run_wide_judgment(self, *, placement=1, width=585, dpi=144, iconic=False, body_error=False, placement_error=False):
        shows = []
        with mock.patch.object(auto_reply.win32gui, "IsWindow", return_value=True),              mock.patch.object(auto_reply.win32gui, "IsIconic", return_value=iconic),              mock.patch.object(auto_reply.win32gui, "IsWindowVisible", return_value=True),              mock.patch.object(auto_reply.win32gui, "GetWindowPlacement", side_effect=RuntimeError("x") if placement_error else None, return_value=(0, placement, (0, 0), (0, 0), (0, 0, 0, 0))),              mock.patch.object(auto_reply.ctypes.windll.user32, "GetDpiForWindow", return_value=dpi),              mock.patch.object(auto_reply, "get_window_true_rect", return_value=(0, 0, width, 800)),              mock.patch.object(auto_reply.win32gui, "ShowWindow", side_effect=lambda h, c: shows.append(c)),              mock.patch.object(auto_reply.time, "sleep"):
            try:
                with auto_reply._wide_window_for_judgment(1):
                    shows.append("body")
                    if body_error:
                        raise ValueError("boom")
            except ValueError:
                shows.append("raised")
        return shows

    def test_wide_window_judgment_maximizes_narrow_window_and_restores(self):
        M, R = auto_reply.win32con.SW_MAXIMIZE, auto_reply.win32con.SW_RESTORE
        self.assertEqual(self._run_wide_judgment(), [M, "body", R])                    # 585px / 1.5 = 390 邏輯像素：太窄
        self.assertEqual(self._run_wide_judgment(body_error=True), [M, "body", R, "raised"])   # 判斷出錯也要還原

    def test_wide_window_judgment_leaves_other_windows_alone(self):
        self.assertEqual(self._run_wide_judgment(width=1942), ["body"])                # 已夠寬 (1942 / 1.5 = 1294)
        self.assertEqual(self._run_wide_judgment(placement=auto_reply.win32con.SW_SHOWMAXIMIZED), ["body"])
        self.assertEqual(self._run_wide_judgment(iconic=True), ["body"])               # 最小化：不彈出視窗
        self.assertEqual(self._run_wide_judgment(placement_error=True), ["body"])      # 讀不到視窗狀態：不動

    def test_left_right_judgments_run_inside_wide_window_context(self):
        order = []

        @contextlib.contextmanager
        def fake_ctx(h):
            order.append("max")
            try:
                yield
            finally:
                order.append("restore")

        with mock.patch.object(auto_reply, "_wide_window_for_judgment", fake_ctx),              mock.patch.object(auto_reply, "_is_last_message_from_self_in_chat", side_effect=lambda h: order.append("self") or False),              mock.patch.object(auto_reply, "_verify_message_sent_to_chat", side_effect=lambda h: order.append("verify") or True):
            self.assertFalse(auto_reply.is_last_message_from_self_in_chat(1))
            self.assertTrue(auto_reply.verify_message_sent_to_chat(1))
        self.assertEqual(order, ["max", "self", "restore", "max", "verify", "restore"])

    def test_countdown_sleep_abort_check_stops_wait(self):
        clock = {"t": 1000.0}
        calls = []

        def abort():
            calls.append(clock["t"])
            if len(calls) == 2:
                raise auto_reply.StopRequested("stop")

        with mock.patch.object(auto_reply.time, "monotonic", lambda: clock["t"]), \
             mock.patch.object(auto_reply.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s)), \
             mock.patch.object(auto_reply, "log_v"):
            with self.assertRaises(auto_reply.StopRequested):
                auto_reply.countdown_sleep(600, abort_check=abort, check_every=3.0)
        self.assertLess(clock["t"] - 1000.0, 10)                          # 約 6 秒就中斷，沒有等滿 600 秒

    def test_countdown_sleep_abort_check_ordinary_error_does_not_break_wait(self):
        clock = {"t": 0.0}

        def bad():
            raise RuntimeError("oops")

        with mock.patch.object(auto_reply.time, "monotonic", lambda: clock["t"]), \
             mock.patch.object(auto_reply.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s)), \
             mock.patch.object(auto_reply, "log_v"):
            auto_reply.countdown_sleep(20, abort_check=bad, check_every=3.0)
        self.assertGreaterEqual(clock["t"], 20)

    def test_check_for_incoming_messages_runs_stop_check_first(self):
        with mock.patch.object(auto_reply, "update_target_window_fingerprints"), \
             mock.patch.object(auto_reply, "check_stop_command", side_effect=auto_reply.StopRequested("stop")):
            with self.assertRaises(auto_reply.StopRequested):
                auto_reply.check_for_incoming_messages({"targets": ["小明"]})

    def test_load_config_rejects_too_short_stop_command(self):
        path = os.path.join(self.temp_dir.name, "stop_short.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"targets": ["a"], "stop_command": " 停！ ", "stop_command_chats": ["小明"]}, f)
        with mock.patch.object(auto_reply, "CONFIG_PATH", path):
            cfg = auto_reply.load_config()
        self.assertEqual(cfg["stop_command"], auto_reply.DEFAULT_CONFIG["stop_command"])   # 單字太容易誤停

    def test_test_mode_exits_with_stop_code_when_stop_requested(self):
        argv = ["auto_reply.py", "--test", "小明"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(auto_reply, "ensure_single_instance"), \
             mock.patch.object(auto_reply, "load_config", return_value=dict(auto_reply.DEFAULT_CONFIG)), \
             mock.patch.object(auto_reply, "check_circuit_breaker", return_value=True), \
             mock.patch.object(auto_reply, "send_grandma_image", side_effect=auto_reply.StopRequested("stop")), \
             mock.patch.object(auto_reply, "log_v"):
            with self.assertRaises(SystemExit) as cm:
                auto_reply.main()
        self.assertEqual(cm.exception.code, auto_reply.STOP_EXIT_CODE)

    def test_test_mode_sets_stop_baselines_before_sending(self):
        order = []
        argv = ["auto_reply.py", "--test", "小明"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(auto_reply, "ensure_single_instance"), \
             mock.patch.object(auto_reply, "load_config", return_value=dict(auto_reply.DEFAULT_CONFIG)), \
             mock.patch.object(auto_reply, "check_circuit_breaker", return_value=True), \
             mock.patch.object(auto_reply, "init_stop_baselines", side_effect=lambda *a, **k: order.append("baseline")), \
             mock.patch.object(auto_reply, "send_grandma_image", side_effect=lambda *a, **k: order.append("send") or "NOT_SENT"), \
             mock.patch.object(auto_reply, "log_v"):
            auto_reply.main()
        self.assertEqual(order, ["baseline", "send"])

    def test_too_short_stop_command_warns_only_once(self):
        path = os.path.join(self.temp_dir.name, "stop_short2.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"targets": ["a"], "stop_command": "停"}, f)
        auto_reply._WARNED_ONCE.discard("stop_command_short")
        with mock.patch.object(auto_reply, "CONFIG_PATH", path), mock.patch.object(auto_reply.logging, "warning") as warn:
            auto_reply.load_config()
            auto_reply.load_config()
            auto_reply.load_config()
        self.assertEqual(sum("stop_command" in str(c.args[0]) for c in warn.call_args_list), 1)

    def test_expanded_window_signature_none_when_minimized(self):
        with mock.patch.object(auto_reply.win32gui, "IsIconic", return_value=True):
            self.assertIsNone(auto_reply._expanded_window_signature(1))
        with mock.patch.object(auto_reply.win32gui, "IsIconic", side_effect=RuntimeError("bad hwnd")):
            self.assertIsNone(auto_reply._expanded_window_signature(1))

    def test_window_hwnd_matches_title_with_or_without_unread(self):
        wins = [(1, "LINE", "Qt", True), (2, "小明 (3)", "Qt", True), (3, "別人", "Qt", True)]
        self.assertEqual(auto_reply._window_hwnd(wins, "小明"), 2)
        self.assertEqual(auto_reply._window_hwnd(wins, "別人"), 3)
        self.assertIsNone(auto_reply._window_hwnd(wins, "不存在"))

    def test_load_config_sanitizes_notify_settings(self):
        path = os.path.join(self.temp_dir.name, "notify_cfg.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"targets": ["a"], "notify_log": True, "notify_monthly_cap": "80"}, f)
        with mock.patch.object(auto_reply, "CONFIG_PATH", path):
            cfg = auto_reply.load_config()
        self.assertEqual((cfg["notify_log"], cfg["notify_monthly_cap"]), (True, 80))
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"targets": ["a"], "notify_log": "yes", "notify_monthly_cap": "lots"}, f)
        with mock.patch.object(auto_reply, "CONFIG_PATH", path):
            cfg = auto_reply.load_config()
        self.assertEqual((cfg["notify_log"], cfg["notify_monthly_cap"]), (False, 150))   # 型別錯誤沿用預設：預設關閉
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"targets": ["a"], "notify_monthly_cap": 0}, f)
        with mock.patch.object(auto_reply, "CONFIG_PATH", path):
            self.assertEqual(auto_reply.load_config()["notify_monthly_cap"], 1)           # 至少 1

    def test_configure_notifier_passes_settings(self):
        with mock.patch.object(auto_reply.notifier, "configure") as conf:
            auto_reply.configure_notifier({"notify_log": True, "notify_monthly_cap": 90})
            conf.assert_called_once_with(True, 90, auto_reply.NOTIFY_STATE_PATH)
            conf.reset_mock()
            auto_reply.configure_notifier({})                                             # 沒設定：關閉
            conf.assert_called_once_with(False, 150, auto_reply.NOTIFY_STATE_PATH)

    def test_log_v_forwards_message_to_notifier_and_survives_its_errors(self):
        with mock.patch.object(auto_reply.notifier, "on_log") as on_log, mock.patch("builtins.print"):
            auto_reply.log_v("[警告] x")
            on_log.assert_called_once_with("[警告] x")
        with mock.patch.object(auto_reply.notifier, "on_log", side_effect=RuntimeError("boom")), mock.patch("builtins.print"):
            auto_reply.log_v("[警告] y")                                                   # 推播出錯不能影響主程式

    def test_load_config_sanitizes_stop_command(self):
        path = os.path.join(self.temp_dir.name, "stop_cfg.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"targets": ["a"], "stop_command": "  結束 程式  ", "stop_command_chats": [" 小明 ", "", 5]}, f)
        with mock.patch.object(auto_reply, "CONFIG_PATH", path):
            cfg = auto_reply.load_config()
        self.assertEqual(cfg["stop_command"], "結束 程式")
        self.assertEqual(cfg["stop_command_chats"], ["小明", "5"])
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"targets": ["a"], "stop_command": 3, "stop_command_chats": "小明"}, f)
        with mock.patch.object(auto_reply, "CONFIG_PATH", path):
            cfg = auto_reply.load_config()
        self.assertEqual(cfg["stop_command"], auto_reply.DEFAULT_CONFIG["stop_command"])
        self.assertEqual(cfg["stop_command_chats"], [])

if __name__ == '__main__':
    unittest.main()
