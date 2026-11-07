import contextlib
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_reply
import image_kind as ik
import image_library as il
import reply_budget

K = ik.ImageKind
NL = chr(10)
CHAT = 77
MID_AUTUMN = K("festival", (), "中秋節")
MORNING = K("greeting", ("morning",))
BIRTHDAY = K("festival", (), "生日")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "images")
        os.makedirs(self.dir)
        self.counts_path = os.path.join(self.tmp.name, "reply_counts.json")
        self.ocr_calls = []
        self.texts = {}                         # 路徑 -> OCR 文字；PIL 圖片用 self.crop_text
        self.crop_text = ""

        def fake_ocr(image):
            self.ocr_calls.append(image)
            if isinstance(image, str):
                if image in self.texts:
                    value = self.texts[image]
                    if isinstance(value, Exception):
                        raise value
                    return value
                with open(image, "rb") as f:                      # 沒指定就讀檔案內容 (文字檔當成「圖」)
                    return f.read().decode("utf-8", errors="ignore")
            return self.crop_text

        self.clock = [1000.0]                                  # 假時鐘：sleep 只推進時間，不真的等
        self.sleeps = []

        def fake_sleep(seconds):
            self.sleeps.append(seconds)
            self.clock[0] += seconds

        self.stack = contextlib.ExitStack()
        self.stack.enter_context(mock.patch.object(auto_reply.time, "time", lambda: self.clock[0]))
        self.stack.enter_context(mock.patch.object(auto_reply.time, "sleep", fake_sleep))
        self.stack.enter_context(mock.patch.object(auto_reply, "ocr_text", fake_ocr))
        self.stack.enter_context(mock.patch.object(auto_reply, "REPLY_COUNTS_PATH", self.counts_path))
        self.stack.enter_context(mock.patch.object(auto_reply, "KIND_INDEX",
                                                   il.KindIndex(os.path.join(self.tmp.name, "kinds.json"), fake_ocr)))
        self.log = self.stack.enter_context(mock.patch.object(auto_reply, "log_v"))
        auto_reply._FILE_KIND_CACHE.clear()
        auto_reply._LAST_OPEN_ATTEMPT.clear()
        auto_reply._DUPLICATE_WINDOW_SEEN.clear()
        auto_reply._TEXT_SEEN_SIG.clear()
        auto_reply._TEXT_LAST_TRIGGER.clear()
        auto_reply._BACKGROUND_WINDOWS.clear()

    def tearDown(self):
        self.stack.close()
        auto_reply._FILE_KIND_CACHE.clear()
        self.tmp.cleanup()

    def real_image(self, name, size=(400, 400), color=(10, 20, 30)):
        from PIL import Image
        path = os.path.join(self.tmp.name, name)
        Image.new("RGB", size, color).save(path)
        return path

    def text_image(self, name, text):
        """圖庫裡的「圖」：內容就是它的 OCR 文字"""
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path


class TestRecognizeFile(_Base):
    def test_recognises_kind_and_caches_by_content(self):
        img = self.real_image("a.png")
        self.texts[img] = "FORMOSA!早安悠然自得"
        kind, text = auto_reply._recognize_file(img)
        self.assertEqual((kind, text), (MORNING, "早安悠然自得"))
        auto_reply._recognize_file(img)
        self.assertEqual(self.ocr_calls, [img])                                          # 同一張圖只辨識一次
        self.assertTrue(auto_reply.is_authentic_grandma_image(img))

    def test_small_unreadable_and_unrecognised_images_are_rejected(self):
        small = self.real_image("small.png", (200, 300), (1, 1, 1))                        # 短邊 < 250：貼圖或縮圖
        self.assertEqual(auto_reply._recognize_file(small), (None, ""))
        self.assertEqual(self.ocr_calls, [])                                               # 太小不浪費 OCR
        edge = self.real_image("edge.png", (250, 400), (2, 2, 2))
        self.texts[edge] = "平安喜樂"
        self.assertIsNotNone(auto_reply._recognize_file(edge)[0])                          # 剛好 250 算數
        junk = os.path.join(self.tmp.name, "junk.bin")
        with open(junk, "wb") as f:
            f.write(b"junk")
        self.assertEqual(auto_reply._recognize_file(junk), (None, ""))
        self.assertEqual(auto_reply._recognize_file(os.path.join(self.tmp.name, "missing.png")), (None, ""))
        photo = self.real_image("photo.png", (400, 400), (90, 90, 90))
        self.texts[photo] = "FORMOSA!"                                                    # 只有浮水印：辨識不出性質
        self.assertEqual(auto_reply._recognize_file(photo)[0], None)
        self.assertFalse(auto_reply.is_authentic_grandma_image(photo))

    def test_ocr_failure_is_treated_as_unrecognised(self):
        img = self.real_image("boom.png", color=(5, 5, 5))
        self.texts[img] = RuntimeError("ocr down")
        self.assertEqual(auto_reply._recognize_file(img), (None, ""))                      # Fail-Closed


class TestRecognizeScreenshot(_Base):
    def run_shot(self, crop_result, text="", ocr_error=None, row=True):
        from PIL import Image
        self.crop_text = text
        crop = (Image.new("RGB", (300, 200), (255, 255, 255)), crop_result, row) if crop_result is not None else None
        with mock.patch.object(auto_reply, "_incoming_media_info", return_value=crop):
            if ocr_error:
                with mock.patch.object(auto_reply, "ocr_text", side_effect=ocr_error):
                    return auto_reply._recognize_chat_screenshot(CHAT)
            return auto_reply._recognize_chat_screenshot(CHAT)

    def test_cases(self):
        self.assertEqual(self.run_shot(300, "中秋節快樂 闔家團圓"), (True, MID_AUTUMN, "中秋節快樂闔家團圓"))
        self.assertEqual(self.run_shot(300, "FORMOSA!"), (False, None, ""))               # 讀得到字但辨識不出性質：捨棄
        self.assertEqual(self.run_shot(300, ""), (False, None, ""))                         # 一般照片，沒有字
        self.ocr_calls.clear()
        self.assertEqual(self.run_shot(150, "早安", row=False), (False, None, ""))         # 下面沒有「另存新檔」那一列：貼圖
        self.assertEqual(self.ocr_calls, [])                                               # 貼圖不做 OCR
        self.assertEqual(self.run_shot(None), (None, None, ""))                            # 暫時截不到
        self.assertEqual(self.run_shot(300, ocr_error=RuntimeError("down")), (None, None, ""))   # OCR 暫時不能用：等下次

    def test_a_narrow_photo_is_still_a_photo_because_it_has_the_save_as_row(self):
        # 照片下方有「儲存｜另存新檔｜分享」那一列，貼圖沒有：縮圖很窄 (直幅、小張) 的照片不能被當成貼圖
        self.assertEqual(self.run_shot(150, "早安 平安喜樂", row=True), (True, ik.classify_text("早安 平安喜樂"), "早安平安喜樂"))
        self.assertEqual(self.run_shot(150, "FORMOSA!", row=True), (False, None, ""))      # 是照片，但辨識不出性質：照舊捨棄
        self.ocr_calls.clear()
        self.assertEqual(self.run_shot(150, "早安", row=False), (False, None, ""))         # 又窄又沒有那一列：貼圖
        # 真機 (2026-11-01)：貼圖旁邊的時間戳記把量到的寬度撐到 327，舊規則當成照片；只看那一列就不會被騙
        self.assertEqual(self.run_shot(327, "早安 平安喜樂", row=False), (False, None, ""))
        self.assertEqual(self.ocr_calls, [])


class TestStickerText(_Base):
    """一對一：最底部是貼圖、上面寫著短問候或祝福 (例如真機的「早安安」貼圖) 也回；其他貼圖照舊略過。
    貼圖不會寫進聊天室的快取目錄，所以是在「視窗列表有變化」的文字觸發流程裡讀 (recognize_incoming_sticker)"""

    def run_sticker(self, texts, bottom=((True, "crop"),), max_chars=30, ocr_error=None, front=True):
        """texts：依序給每次 OCR 讀到的字；bottom：依序給每次偵測到的 (狀態, 是否有裁切圖)"""
        from PIL import Image
        reads = iter(texts)
        states = iter(bottom)
        self.ocr_inputs = []
        self.order = []
        self.naps = []

        def fake_ocr(image):
            self.ocr_inputs.append(image.size)
            self.order.append("ocr")
            if ocr_error:
                raise ocr_error
            return next(reads, "")

        def fake_bottom(hwnd):
            self.order.append("detect")
            state, has_crop = next(states, bottom[-1])
            return state, (Image.new("RGB", (300, 200), (255, 255, 255)) if has_crop else None)

        with mock.patch.object(auto_reply.win32gui, "IsWindow", return_value=True), \
             mock.patch.object(auto_reply.win32gui, "IsIconic", return_value=False), \
             mock.patch.object(auto_reply.win32gui, "IsWindowVisible", return_value=True), \
             mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=CHAT if front else CHAT + 1), \
             mock.patch.object(auto_reply, "_wait_until_chat_settled", side_effect=lambda h: self.order.append("settle") or True), \
             mock.patch.object(auto_reply, "_bottom_sticker", side_effect=fake_bottom), \
             mock.patch.object(auto_reply.time, "sleep", side_effect=self.naps.append), \
             mock.patch.object(auto_reply, "ocr_text", side_effect=fake_ocr):
            return auto_reply.recognize_incoming_sticker(CHAT, max_chars)

    def logged(self):
        return NL.join(str(c.args[0]) for c in self.log.call_args_list)

    def test_a_sticker_that_says_good_morning_is_recognised(self):
        state, kind, text = self.run_sticker(["早安安"])
        self.assertEqual((state, text), (True, "早安安"))
        self.assertEqual(kind, ik.classify_message("早安安"))
        self.assertIn("貼圖文字識別", self.logged())
        self.assertEqual(self.ocr_inputs, [(300, 200)])                             # 原圖就讀對：不再讀第二次

    def test_a_sticker_that_read_nothing_is_not_doubled_on_the_first_look_only_on_the_second(self):
        # 審查：沒字的貼圖原本會「讀原圖、放大讀、等 1.5 秒、再讀原圖、再放大讀」共四次；第一輪不放大，放大留給第二輪
        state, _, text = self.run_sticker(["", "", "早安安"])
        self.assertEqual((state, text), (True, "早安安"))
        self.assertEqual(self.ocr_inputs, [(300, 200), (300, 200), (600, 400)])
        self.run_sticker(["", "", ""])
        self.assertEqual(self.ocr_inputs, [(300, 200), (300, 200), (600, 400)])                  # 沒字：共三次，不是四次

    def test_a_short_text_is_doubled_at_once_on_the_first_look(self):
        state, _, text = self.run_sticker(["", ])                                               # 對照：讀到短字 (非問候) 才在第一輪放大
        self.assertEqual(state, False)
        self.run_sticker(["OK", "早安安"])
        self.assertEqual(self.ocr_inputs, [(300, 200), (600, 400)])

    def test_a_short_non_greeting_gets_the_doubled_read_too_but_a_long_text_does_not(self):
        self.run_sticker(["OK啦", "OK啦"])
        self.assertEqual(len(self.ocr_inputs), 2)                                   # 短字：可能是小字，再讀一次
        long_text = "早安" + "你好" * 20
        self.run_sticker([long_text, long_text])
        self.assertEqual(len(self.ocr_inputs), 1)                                   # 審查：已經太長就不必再讀 (省一次 OCR)

    def test_other_stickers_are_not_replied_to(self):
        self.assertEqual(self.run_sticker(["OK啦", "OK啦"]), (False, None, "OK啦"))
        self.assertEqual(self.run_sticker(["哈哈哈", "哈哈哈"])[0], False)

    def test_the_same_limits_as_text_messages_apply(self):
        long_text = "早安" + "你好" * 20
        self.assertEqual(self.run_sticker([long_text])[0], False)                    # 超過 30 字
        self.assertEqual(self.run_sticker(["早安安安"], max_chars=3)[0], False)       # 上限來自 text_reply_max_chars
        self.assertEqual(self.run_sticker(["生日", "生日"])[0], False)                # 只是提到生日、沒有祝賀語
        state, kind, _ = self.run_sticker(["生日快樂"])
        self.assertTrue(state)
        self.assertTrue(ik.is_birthday(kind))                                        # 生日祝賀：之後只回「謝謝」

    def test_the_time_next_to_the_sticker_is_cleaned_out_of_the_text(self):
        self.assertEqual(self.run_sticker(["早安安 上午 6:20"])[0::2], (True, "早安安"))
        self.assertEqual(self.run_sticker(["早安安 上午6:20"])[0::2], (True, "早安安"))

    def test_text_recognition_settles_by_default_and_skips_it_when_told_the_screen_was_just_settled(self):
        from PIL import Image
        crop = Image.new("RGB", (500, 60), (255, 255, 255))
        for settle, expected in ((True, 1), (False, 0), (None, 0)):
            calls = []
            kwargs = {} if settle is None else {"settle": settle}
            if settle is None:
                kwargs = {"settle": False}
            with mock.patch.object(auto_reply.win32gui, "IsWindow", return_value=True), \
                 mock.patch.object(auto_reply.win32gui, "IsIconic", return_value=False), \
                 mock.patch.object(auto_reply.win32gui, "IsWindowVisible", return_value=True), \
                 mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=CHAT), \
                 mock.patch.object(auto_reply, "_wait_until_chat_settled", side_effect=lambda h: calls.append(1)), \
                 mock.patch.object(auto_reply, "_incoming_text_crop", return_value=crop), \
                 mock.patch.object(auto_reply, "ocr_text", return_value="早安"):
                auto_reply.recognize_incoming_text(CHAT, 30, **kwargs)
            self.assertEqual(len(calls), expected, settle)
        calls = []
        with mock.patch.object(auto_reply.win32gui, "IsWindow", return_value=True), \
             mock.patch.object(auto_reply.win32gui, "IsIconic", return_value=False), \
             mock.patch.object(auto_reply.win32gui, "IsWindowVisible", return_value=True), \
             mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=CHAT), \
             mock.patch.object(auto_reply, "_wait_until_chat_settled", side_effect=lambda h: calls.append(1)), \
             mock.patch.object(auto_reply, "_incoming_text_crop", return_value=crop), \
             mock.patch.object(auto_reply, "ocr_text", return_value="早安"):
            auto_reply.recognize_incoming_text(CHAT, 30)                                 # 不傳旗標：預設要等 (原本的行為)
        self.assertEqual(len(calls), 1)

    def test_garbled_time_leftovers_do_not_break_a_greeting(self):
        state, kind, _ = self.run_sticker(["早安安 F午6:20"])
        self.assertTrue(state)
        self.assertEqual(kind, ik.classify_message("早安安"))

    def test_an_ocr_failure_means_ask_again_later_not_not_a_greeting(self):
        self.assertEqual(self.run_sticker(["早安安"], ocr_error=RuntimeError("down")), (None, None, ""))

    def test_nothing_is_read_while_the_window_is_not_in_front(self):
        self.assertEqual(self.run_sticker(["早安安"], front=False), (None, None, ""))
        self.assertEqual(self.ocr_inputs, [])

    def test_the_screen_is_settled_and_the_sticker_detected_before_it_is_read(self):
        self.run_sticker(["早安安"])
        self.assertEqual(self.order, ["settle", "detect", "ocr"])

    def test_it_never_falls_back_to_reading_a_text_bubble(self):
        # 審查：最底部已經不是貼圖 (或暫時判斷不出) 時回傳「稍後再說」，不能中途改讀文字氣泡、紀錄卻寫貼圖
        for bottom in (((False, False),), ((None, False),)):
            self.assertEqual(self.run_sticker(["早安"], bottom=bottom), (None, None, ""))
            self.assertEqual(self.ocr_inputs, [])

    def test_a_sticker_with_no_text_at_all_is_looked_at_again_once_in_case_it_was_still_loading(self):
        # 審查：列表一變就讀，貼圖圖片可能還沒載入、讀不到任何字；基準線又會前進，所以要再看一次
        state, _, text = self.run_sticker(["", "", "早安安"])
        self.assertEqual((state, text), (True, "早安安"))
        self.assertEqual(self.naps, [auto_reply.STICKER_LOAD_RETRY_DELAY])
        self.assertEqual(self.order.count("detect"), 2)                              # 第二次是重新截圖偵測，不是沿用舊的

    def test_the_second_look_happens_only_once_and_only_when_nothing_was_read(self):
        self.assertEqual(self.run_sticker(["", "", "", ""])[0], False)
        self.assertEqual(self.order.count("detect"), 2)                              # 讀兩次還是沒字就放棄
        self.run_sticker(["OK啦", "OK啦"])
        self.assertEqual(self.order.count("detect"), 1)                              # 有讀到字 (只是不是問候)：不再看第二次
        self.assertEqual(self.naps, [])

    def test_if_the_sticker_is_gone_on_the_second_look_it_asks_again_later(self):
        self.assertEqual(self.run_sticker(["", ""], bottom=((True, True), (False, False)))[0], None)

    def photo_loading(self, rows, wait=auto_reply.STICKER_LOAD_WAIT_SECONDS):
        """rows：依序給每次偵測 (_incoming_media_info) 的「有沒有另存新檔那一列」；None 代表抓不到"""
        from PIL import Image
        crop = Image.new("RGB", (300, 200), (255, 255, 255))
        seq = iter(rows)
        naps = []
        calls = {"n": 0}

        def info(h):
            calls["n"] += 1
            r = next(seq, rows[-1])
            return None if r is None else (crop, 150, r)

        with mock.patch.object(auto_reply, "last_left_message_is_media", return_value=True), \
             mock.patch.object(auto_reply, "_incoming_media_info", side_effect=info), \
             mock.patch.object(auto_reply.time, "sleep", side_effect=lambda seconds: (naps.append(seconds), self.clock.__setitem__(0, self.clock[0] + seconds))):
            state, got = auto_reply._bottom_sticker(CHAT, wait_seconds=wait)       # sleep 要推進假時鐘，否則等待迴圈永遠等不到截止時間
        return state, got, naps, calls["n"]

    def test_a_photo_that_finishes_loading_while_waiting_is_a_photo_not_a_sticker(self):
        # 審查：載入中的照片也沒有「另存新檔」那一列，不等就會被當成貼圖、讀字、甚至回覆
        state, got, naps, calls = self.photo_loading([False, False, True])
        self.assertEqual((state, got), (False, None))
        self.assertEqual(naps, [auto_reply.IMAGE_LOAD_POLL_SECONDS] * 2)

    def test_a_real_sticker_waits_for_the_full_time_and_is_still_a_sticker(self):
        self.clock[0] = 1000.0
        state, got, naps, calls = self.photo_loading([False])
        self.assertTrue(state)
        self.assertIsNotNone(got)
        self.assertGreaterEqual(sum(naps), auto_reply.STICKER_LOAD_WAIT_SECONDS)

    def test_no_waiting_when_not_asked_to(self):
        state, _, naps, calls = self.photo_loading([False], wait=0.0)
        self.assertTrue(state)
        self.assertEqual((naps, calls), ([], 1))

    def test_a_row_that_is_already_there_needs_no_waiting(self):
        state, _, naps, _ = self.photo_loading([True])
        self.assertFalse(state)
        self.assertEqual(naps, [])

    def test_the_picture_disappearing_while_waiting_means_ask_again_later(self):
        state, got, _, _ = self.photo_loading([False, None])
        self.assertEqual((state, got), (None, None))
        # 突變只靠外層的例外處理也會得到同樣的 None：要確認「不是靠例外」，所以這一輪不能留下任何紀錄
        self.log.reset_mock()
        self.photo_loading([False, None])
        self.assertNotIn("貼圖偵測", self.logged())

    def test_a_detection_error_leaves_a_log_line_and_asks_again_later(self):
        with mock.patch.object(auto_reply, "last_left_message_is_media", side_effect=RuntimeError("dpi")):
            self.assertEqual(auto_reply._bottom_sticker(CHAT), (None, None))
        self.assertIn("dpi", self.logged())
        self.log.reset_mock()
        with mock.patch.object(auto_reply, "_wait_until_chat_settled", side_effect=RuntimeError("grab")), \
             mock.patch.object(auto_reply.win32gui, "IsWindow", return_value=True), \
             mock.patch.object(auto_reply.win32gui, "IsIconic", return_value=False), \
             mock.patch.object(auto_reply.win32gui, "IsWindowVisible", return_value=True), \
             mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=CHAT):
            self.assertEqual(auto_reply.recognize_incoming_sticker(CHAT), (None, None, ""))
        self.assertIn("grab", self.logged())

    def test_the_peek_waits_for_the_thumbnail_but_the_later_looks_do_not(self):
        seen = []
        with mock.patch.object(auto_reply, "find_target_chat_window", return_value=[(CHAT, "小明", "Qt", True)]), \
             mock.patch.object(auto_reply.win32gui, "IsIconic", return_value=False), \
             mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=CHAT), \
             mock.patch.object(auto_reply, "activate_window"), \
             mock.patch.object(auto_reply, "_wait_until_chat_settled"), \
             mock.patch.object(auto_reply, "is_last_message_from_self_in_chat", return_value=False), \
             mock.patch.object(auto_reply, "_bottom_sticker", side_effect=lambda h, wait_seconds=0.0: seen.append(wait_seconds) or (False, None)), \
             mock.patch.object(auto_reply, "recognize_incoming_text", return_value=(True, MORNING, "早安")), \
             mock.patch.object(auto_reply, "_minimize_if_configured"):
            auto_reply._peek_incoming_text("小明", {})
        self.assertEqual(seen, [auto_reply.STICKER_LOAD_WAIT_SECONDS])

    def test_bottom_sticker_detection(self):
        from PIL import Image
        crop = Image.new("RGB", (300, 200), (255, 255, 255))
        cases = ((True, (crop, 150, False), True),       # 不是文字氣泡、沒有「另存新檔」那一列：貼圖
                 (True, (crop, 150, True), False),       # 有那一列：照片
                 (False, (crop, 150, False), False),     # 文字氣泡
                 (None, (crop, 150, False), None),       # 判斷不出：不是「不是貼圖」
                 (True, None, None))                     # 抓不到圖片資訊：同上
        for media, info, expected in cases:
            with mock.patch.object(auto_reply, "last_left_message_is_media", return_value=media), \
                 mock.patch.object(auto_reply, "_incoming_media_info", return_value=info):
                state, got = auto_reply._bottom_sticker(CHAT)
                self.assertEqual(state, expected, (media, info))
                self.assertEqual(got is not None, expected is True)                  # 只有確定是貼圖才附裁切圖
        with mock.patch.object(auto_reply, "last_left_message_is_media", side_effect=RuntimeError("boom")):
            self.assertEqual(auto_reply._bottom_sticker(CHAT), (None, None))         # 任何錯誤都是「暫時判斷不出」，不能當成不是貼圖

    def test_sticker_text_is_allowed_only_for_one_to_one_chats_with_the_option_on_and_not_in_photo_only_mode(self):
        self.assertTrue(auto_reply._sticker_text_allowed({}, "小明"))
        self.assertFalse(auto_reply._sticker_text_allowed({"reply_to_sticker_text": False}, "小明"))
        self.assertFalse(auto_reply._sticker_text_allowed({"trigger_mode": "photo_only"}, "小明"))    # 「只回照片」
        self.assertTrue(auto_reply._sticker_text_allowed({"trigger_mode": "all"}, "小明"))
        auto_reply.set_group_targets(["小明"])
        self.addCleanup(auto_reply.set_group_targets, [])
        self.assertFalse(auto_reply._sticker_text_allowed({}, "小明"))

    def test_text_and_sticker_share_one_greeting_reader(self):
        from PIL import Image
        crop = Image.new("RGB", (300, 60), (255, 255, 255))
        reads = iter(["", "早安"])
        with mock.patch.object(auto_reply, "ocr_text", side_effect=lambda i: next(reads, "")):
            self.assertEqual(auto_reply._read_greeting(crop, 30, retry_when_empty=True), (ik.classify_message("早安"), "早安"))
        reads = iter(["", "早安"])
        with mock.patch.object(auto_reply, "ocr_text", side_effect=lambda i: next(reads, "")):
            self.assertEqual(auto_reply._read_greeting(crop, 30)[0], None)             # 文字：沒字不再讀第二次 (原本的行為)


class TestRecognizeIncomingText(_Base):
    def run_text(self, ocr, front=True, crop="ok", max_chars=30, iconic=False):
        from PIL import Image
        self.ocr_mock = mock.Mock(side_effect=ocr if isinstance(ocr, list) else [ocr] * 4)
        image = Image.new("RGB", (300, 60), (230, 230, 230)) if crop == "ok" else None
        patches = [
            mock.patch.object(auto_reply, "_incoming_text_crop", return_value=image),
            mock.patch.object(auto_reply, "_wait_until_chat_settled", return_value=True),
            mock.patch.object(auto_reply.win32gui, "IsWindow", return_value=True),
            mock.patch.object(auto_reply.win32gui, "IsIconic", return_value=iconic),
            mock.patch.object(auto_reply.win32gui, "IsWindowVisible", return_value=True),
            mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=CHAT if front else CHAT + 1),
            mock.patch.object(auto_reply, "ocr_text", self.ocr_mock),
        ]
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            return auto_reply.recognize_incoming_text(CHAT, max_chars)

    def test_the_screen_is_awaited_before_it_is_read(self):
        # 真機 (2026-11-01)：is_last_message_from_self_in_chat 暫時最大化再還原視窗，還原後立刻讀會讀不到，等 1.5 秒才讀得到
        order = []
        with mock.patch.object(auto_reply, "_wait_until_chat_settled", side_effect=lambda h: order.append("settle") or True), \
             mock.patch.object(auto_reply, "_incoming_text_crop", side_effect=lambda h: order.append("crop")), \
             mock.patch.object(auto_reply.win32gui, "IsWindow", return_value=True), mock.patch.object(auto_reply.win32gui, "IsIconic", return_value=False), \
             mock.patch.object(auto_reply.win32gui, "IsWindowVisible", return_value=True), \
             mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=CHAT):
            auto_reply.recognize_incoming_text(CHAT, 30)
        self.assertEqual(order, ["settle", "crop"])

    def test_nothing_is_awaited_when_the_window_is_not_in_front(self):
        with mock.patch.object(auto_reply, "_wait_until_chat_settled") as settle, \
             mock.patch.object(auto_reply.win32gui, "IsWindow", return_value=True), mock.patch.object(auto_reply.win32gui, "IsIconic", return_value=False), \
             mock.patch.object(auto_reply.win32gui, "IsWindowVisible", return_value=True), \
             mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=CHAT + 1):
            self.assertEqual(auto_reply.recognize_incoming_text(CHAT, 30), (None, None, ""))
        settle.assert_not_called()

    def test_short_greeting_is_recognised_and_ui_text_is_cleaned(self):
        state, kind, text = self.run_text("早安！ 上午 12:05")                              # 氣泡右外側的時間會一併被截到
        self.assertEqual((state, kind), (True, ik.classify_message("早安")))
        self.assertNotIn("12:05", text)
        self.assertEqual(self.run_text("中秋節快樂")[:2], (True, MID_AUTUMN))

    def test_ordinary_chat_long_text_and_campaign_text_are_not_answered(self):
        self.assertEqual(self.run_text("今天吃什麼")[:2], (False, None))
        self.assertEqual(self.run_text("早安，我今天早上要去市場買菜然後再去醫院看醫生順便繳水電費還要去郵局寄包裹")[:2], (False, None))
        self.assertEqual(self.run_text("早安 請支持我們的議員")[:2], (False, None))
        self.assertEqual(self.run_text("")[:2], (False, None))

    def test_short_text_that_was_not_recognised_is_read_a_second_time_at_double_size(self):
        self.assertEqual(self.run_text(["平", "平安喜樂"])[:2], (True, ik.classify_message("平安喜樂")))       # 小字第一次讀漏，放大後讀到
        self.assertEqual(self.ocr_mock.call_count, 2)
        self.assertEqual(self.ocr_mock.call_args.args[0].size, (600, 120))                                      # 第二次是放大一倍的截圖
        self.assertEqual(self.run_text(["今天吃什麼", "還是不是問候"])[:2], (False, None))
        self.assertEqual(self.ocr_mock.call_count, 2)

    def test_text_that_is_empty_or_too_long_is_not_read_again(self):
        self.run_text("")
        self.assertEqual(self.ocr_mock.call_count, 1)
        self.run_text("早安，我今天早上要去市場買菜然後再去醫院看醫生順便繳水電費還要去郵局寄包裹")
        self.assertEqual(self.ocr_mock.call_count, 1)

    def test_cannot_tell_when_the_window_is_not_in_front_or_the_screen_cannot_be_read(self):
        self.assertEqual(self.run_text("早安", front=False), (None, None, ""))
        self.assertEqual(self.ocr_mock.call_count, 0)                                          # 視窗不在前景時連 OCR 都不做
        self.assertEqual(self.run_text("早安", iconic=True), (None, None, ""))
        self.assertEqual(self.run_text("早安", crop=None), (None, None, ""))                    # 截不到
        self.assertEqual(self.run_text([RuntimeError("ocr down")]), (None, None, ""))          # OCR 暫時不能用：等下次

    def test_custom_length_limit(self):
        self.assertEqual(self.run_text("早安祝你有美好的一天", max_chars=5)[:2], (False, None))
        self.assertEqual(self.run_text("早安祝你有美好的一天", max_chars=20)[0], True)


class TestMediaInfoActionRow(_Base):
    """_incoming_media_info 的第三個值：圖片正下方緊貼著一條矮矮的連結列 = 照片"""

    def info(self, blocks, content_width=100):
        from PIL import Image, ImageDraw
        shot = Image.new("RGB", (400, 400), (255, 255, 255))
        draw = ImageDraw.Draw(shot)
        for bottom, top in blocks:
            draw.rectangle([90, top, 90 + content_width, bottom], fill=(10, 90, 160))
        is_content = lambda px: sum(abs(a - b) for a, b in zip(px, (255, 255, 255))) > 30
        found = (shot, 1.0, 84, 324, is_content, blocks)
        with mock.patch.object(auto_reply, "_left_message_blocks", return_value=found):
            return auto_reply._incoming_media_info(CHAT)

    def test_photo_with_the_link_row_below_it(self):
        crop, width, row = self.info([(310, 302), (290, 60)])            # 連結列 (8px 高) 緊貼在圖片 (230px 高) 下方
        self.assertTrue(row)
        self.assertGreaterEqual(width, 100)

    def test_sticker_or_loading_placeholder_has_no_link_row(self):
        self.assertFalse(self.info([(290, 60)])[2])                       # 底部就是那塊圖，下面什麼都沒有
        self.assertFalse(self.info([(290, 60), (150, 100)])[2])           # 圖片上方還有別的訊息，但圖片下面沒有連結列

    def test_a_tall_block_below_or_a_far_away_row_is_not_a_link_row(self):
        self.assertFalse(self.info([(400, 330), (290, 60)])[2])           # 下面是另一則高訊息，不是連結列 (太高)
        self.assertFalse(self.info([(390, 382), (290, 60)])[2])           # 矮但離圖片太遠 (不緊貼)

    def test_the_two_value_wrapper_still_works(self):
        crop, width = self.info.__func__(self, [(310, 302), (290, 60)])[:2], 0
        self.assertIsNotNone(crop)


class TestRecognizeScreenshotWaitsForLoading(_Base):
    def shots(self, widths, text="中秋節快樂", rows=None):
        from PIL import Image
        self.crop_text = text
        crop = Image.new("RGB", (300, 200), (255, 255, 255))
        rows = rows or [w is not None and w >= 200 for w in widths]
        results = iter([None if w is None else (crop, w, r) for w, r in zip(widths, rows)])      # 預設：寬 (>= 200) 代表圖載入完成、那一列已出現
        last = {"v": None}

        def next_shot(hwnd):
            try:
                last["v"] = next(results)
            except StopIteration:
                pass
            return last["v"]

        with mock.patch.object(auto_reply, "_incoming_media_info", side_effect=next_shot) as m:
            result = auto_reply._recognize_chat_screenshot(CHAT)
        return result, m.call_count

    def test_waits_for_the_grey_placeholder_to_become_the_image(self):
        result, calls = self.shots([60, 60, 60, 300])                                       # 先是灰色小方塊，三秒後圖載入完成
        self.assertEqual(result, (True, MID_AUTUMN, "中秋節快樂"))
        self.assertEqual((calls, self.sleeps), (4, [1.0, 1.0, 1.0]))

    def test_a_narrow_image_with_the_link_row_needs_no_waiting(self):
        result, calls = self.shots([150], rows=[True])                                        # 已經有「另存新檔」那一列：圖已經載入完成
        self.assertEqual((result[0], calls, self.sleeps), (True, 1, []))

    def test_stays_small_means_sticker_after_the_full_wait(self):
        result, calls = self.shots([60])
        self.assertEqual(result, (False, None, ""))
        self.assertGreaterEqual(sum(self.sleeps), auto_reply.IMAGE_LOAD_WAIT_SECONDS)       # 等滿 8 秒才下結論
        self.assertEqual(self.ocr_calls, [])                                                # 貼圖不做 OCR

    def test_no_wait_when_the_image_is_already_loaded(self):
        result, calls = self.shots([300])
        self.assertEqual((result[0], calls, self.sleeps), (True, 1, []))

    def test_nothing_captured_stays_undecided_after_the_wait(self):
        result, _ = self.shots([None])
        self.assertEqual(result, (None, None, ""))
        self.assertGreaterEqual(sum(self.sleeps), auto_reply.IMAGE_LOAD_WAIT_SECONDS)


class TestMediaCheckWhileLoading(_Base):
    def run_check(self, results, expect_media):
        with mock.patch.object(auto_reply, "last_left_message_is_media", side_effect=results) as m, \
             mock.patch.object(auto_reply, "_wait_until_chat_settled", return_value=True):
            return auto_reply._media_type_with_retry(CHAT, expect_media=expect_media), m.call_count

    def test_the_screen_is_awaited_before_the_media_check(self):
        order = []
        with mock.patch.object(auto_reply, "_wait_until_chat_settled", side_effect=lambda h: order.append("settle") or True), \
             mock.patch.object(auto_reply, "last_left_message_is_media", side_effect=lambda h: order.append("check") or False):
            auto_reply._media_type_with_retry(CHAT)
        self.assertEqual(order, ["settle", "check"])

    def test_image_triggered_waits_through_the_text_looking_placeholder(self):
        value, calls = self.run_check([False, False, False, True], expect_media=True)       # 灰色佔位方塊先被看成文字
        self.assertEqual((value, calls, self.sleeps), (True, 4, [1.0, 1.0, 1.0]))

    def test_image_triggered_gives_up_as_text_after_the_full_wait(self):
        value, calls = self.run_check([False] * 20, expect_media=True)
        self.assertEqual((value, calls), (False, 9))                                         # 等滿 8 秒還是文字：真的是文字
        self.assertEqual(len(self.sleeps), 8)

    def test_not_image_triggered_trusts_text_immediately(self):
        value, calls = self.run_check([False, True], expect_media=False)
        self.assertEqual((value, calls, self.sleeps), (False, 1, []))                       # trigger_mode=all 等：文字就是文字


class TestRecognizeIncoming(_Base):
    def recognize(self, path, *, hwnd=CHAT, iconic=False, visible=True, fg=CHAT, shot=(True, MORNING, "t"), win_error=False):
        with mock.patch.object(auto_reply.win32gui, "IsWindow", side_effect=RuntimeError("bad") if win_error else None, return_value=True), \
             mock.patch.object(auto_reply.win32gui, "IsIconic", return_value=iconic), \
             mock.patch.object(auto_reply.win32gui, "IsWindowVisible", return_value=visible), \
             mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=fg), \
             mock.patch.object(auto_reply, "_recognize_chat_screenshot", return_value=shot) as shot_mock:
            return auto_reply.recognize_incoming_image(path, hwnd), shot_mock.call_count

    def test_routing(self):
        plain = self.real_image("plain.png", color=(3, 3, 3))
        self.texts[plain] = "晚安好眠"
        encrypted = os.path.join(self.tmp.name, "enc.eimg")
        with open(encrypted, "wb") as f:
            f.write(b"\x00encrypted")
        self.assertEqual(self.recognize(None), ((False, None, ""), 0))                      # 沒有觸發檔 (只傳文字)
        (state, kind, text), shots = self.recognize(plain)
        self.assertEqual((state, kind.slots, shots), (True, ("evening",), 0))               # 一般圖檔：直接辨識檔案
        self.assertEqual(self.recognize(encrypted), ((True, MORNING, "t"), 1))              # 加密快取且視窗在前景：截圖
        self.assertEqual(self.recognize(encrypted, shot=(None, None, "")), ((None, None, ""), 1))
        for kw in ({"fg": 5}, {"iconic": True}, {"visible": False}, {"hwnd": None}, {"win_error": True}):
            self.assertEqual(self.recognize(encrypted, **kw), ((None, None, ""), 0), kw)    # 視窗不可靠：延後，不截圖


class TestChooseReplyImage(_Base):
    def setUp(self):
        super().setUp()
        self.stack.enter_context(mock.patch.object(auto_reply, "_forbidden_md5s", return_value=set()))
        self.slot = "morning"
        self.stack.enter_context(mock.patch.object(auto_reply, "current_time_slot", side_effect=lambda: self.slot))
        self.cfg = {"images_dir": self.dir, "auto_crawl_images": True, "time_aware_images": True}
        self.fetch = self.stack.enter_context(mock.patch.object(auto_reply.image_library, "fetch_matching", return_value=None))
        self.festival_urls = self.stack.enter_context(
            mock.patch.object(auto_reply.crawl_images, "fetch_festival_candidates", return_value=[("https://f/1", None)]))
        self.generic = self.stack.enter_context(mock.patch.object(auto_reply, "build_candidates", return_value=[("https://g/1", None)]))

    def choose(self, kind, text="", **cfg):
        return auto_reply.choose_reply_image("小明", kind, text, dict(self.cfg, **cfg))

    def test_festival_gets_the_same_festival_but_not_their_image(self):
        mine = self.text_image("m1.jpg", "中秋節快樂月圓人團圓好運")
        self.text_image("m2.jpg", "中秋節快樂闔家團圓")                                      # 和對方那張相同
        self.text_image("f.jpg", "父親節快樂")
        self.text_image("g.jpg", "早安平安")
        self.assertEqual(self.choose(MID_AUTUMN, "FORMOSA!中秋節快樂闔家團圓"), mine)
        self.fetch.assert_not_called()

    def test_friends_image_is_excluded_even_when_the_screenshot_text_has_line_ui_words(self):
        same = self.text_image("same.jpg", "平安喜樂聖誕快樂")
        other = self.text_image("other.jpg", "聖誕節快樂歡樂滿屋")
        incoming = "平安喜樂聖誕快樂儲存另存新檔分享傳送至Keep筆記下午10:50"               # 真機實測的截圖文字
        for _ in range(10):
            self.assertEqual(self.choose(K("festival", (), "聖誕節"), incoming), other)    # 絕不會回對方傳的那一張

    def test_festival_without_stock_fetches_festival_images_with_doubled_limit(self):
        self.text_image("g.jpg", "早安平安")
        fetched = il.LibraryImage("new.jpg", "md5", MID_AUTUMN, "中秋節快樂")
        self.fetch.return_value = fetched
        self.assertEqual(self.choose(MID_AUTUMN, "中秋節快樂團圓", instant_crawl_max_downloads=30), "new.jpg")
        args, kwargs = self.fetch.call_args
        self.assertEqual(args[3], [("https://f/1", None)])                                   # 用節慶圖清單，不是一般來源
        self.assertEqual(kwargs["max_downloads"], 60)                                       # 節日圖的抓圖上限加倍
        self.assertTrue(args[0](fetched))                                                    # 篩選條件：同一個節日
        self.assertFalse(args[0](il.LibraryImage("x", "m", K("festival", (), "父親節"), "")))
        self.generic.assert_not_called()

    def test_festival_never_falls_back_to_another_nature(self):
        self.text_image("g.jpg", "早安平安")
        self.text_image("b.jpg", "幸福快樂")
        self.assertIsNone(self.choose(MID_AUTUMN, "中秋節快樂"))                               # 回錯節日比不回更糟
        self.assertEqual(self.fetch.call_count, 1)

    def test_greeting_uses_current_slot_then_neutral(self):
        self.slot = "evening"
        night = self.text_image("n.jpg", "晚安好眠")
        self.text_image("m.jpg", "早安平安")
        self.assertEqual(self.choose(MORNING, "早安你好嗎"), night)                           # 對方傳早安，現在是晚上：回晚安
        self.slot = "noon"
        picked = self.choose(MORNING, "早安你好嗎")                                           # 沒有午安：抓不到，改用無時段圖
        self.assertIsNone(picked)                                                          # 圖庫只有晚安和早安，沒有無時段圖
        self.assertEqual(self.fetch.call_count, 2)                                          # 午安池、無時段池都試著上網抓
        self.text_image("b.jpg", "幸福快樂")
        self.fetch.reset_mock()
        self.assertEqual(os.path.basename(self.choose(MORNING, "早安你好嗎")), "b.jpg")        # 有無時段圖：不必上網
        self.fetch.assert_called_once()                                                    # 只為午安池抓了一次，沒抓到才退而用無時段圖

    def test_fetch_for_greeting_uses_generic_sources(self):
        self.slot = "evening"
        self.choose(MORNING, "早安你好嗎")
        self.generic.assert_called()
        self.festival_urls.assert_not_called()

    def test_other_natures_prefer_the_same_nature(self):
        care = self.text_image("c.jpg", "雨天路滑注意安全")
        self.text_image("b.jpg", "幸福快樂")
        self.assertEqual(self.choose(K("care"), "天冷注意保暖小心感冒"), care)

    def test_auto_crawl_off_never_fetches(self):
        self.assertIsNone(self.choose(MID_AUTUMN, "中秋節快樂", auto_crawl_images=False))
        self.fetch.assert_not_called()

    def test_forbidden_images_are_skipped(self):
        path = self.text_image("c.jpg", "雨天路滑注意安全")
        md5 = il.file_md5(path)
        with mock.patch.object(auto_reply, "_forbidden_md5s", return_value={md5}):
            self.assertIsNone(self.choose(K("care"), "天冷注意保暖"))

    def test_time_awareness_off_ignores_slot(self):
        self.slot = "noon"
        morning = self.text_image("m.jpg", "早安平安")
        self.assertEqual(self.choose(MORNING, "早安你好嗎", time_aware_images=False), morning)

    def test_unrecognised_library_images_are_never_chosen(self):
        self.text_image("u.jpg", "FORMOSA!")
        self.assertIsNone(self.choose(K("care"), "天冷注意保暖", auto_crawl_images=False))


class TestForbiddenMd5s(_Base):
    def test_sent_and_received_within_rounds(self):
        history = {"小明": {"sent": ["s1", "s2", "s3"], "received": ["r1", "r2", "r3"]}}
        with mock.patch.object(auto_reply, "load_contact_history", return_value=history):
            self.assertEqual(auto_reply._forbidden_md5s("小明", {"history_rounds": 2}), {"s2", "s3", "r2", "r3"})
            self.assertEqual(auto_reply._forbidden_md5s("別人", {}), set())
        with mock.patch.object(auto_reply, "load_contact_history", return_value={"小明": "bad"}):
            self.assertEqual(auto_reply._forbidden_md5s("小明", {}), set())


class TestIngestDownloaded(_Base):
    def test_unrecognised_download_is_discarded_and_not_counted(self):
        keep = self.text_image("keep.jpg", "中秋節快樂")
        drop = self.text_image("drop.jpg", "FORMOSA!")
        self.assertIs(auto_reply._ingest_downloaded(keep, None), True)
        self.assertIs(auto_reply._ingest_downloaded(drop, None), False)                    # False：爬蟲不計入張數
        self.assertTrue(os.path.exists(keep))
        self.assertFalse(os.path.exists(drop))                                             # 辨識不出來的當場刪掉

    def test_ocr_error_counts_as_unrecognised(self):
        path = self.text_image("err.jpg", "中秋節快樂")
        self.texts[path] = RuntimeError("ocr down")
        self.assertIs(auto_reply._ingest_downloaded(path, None), False)
        self.assertFalse(os.path.exists(path))


class TestCampaignImages(_Base):
    def logged(self):
        return NL.join(str(c.args[0]) for c in self.log.call_args_list)

    def test_skip_reason_is_reported(self):
        with mock.patch.object(auto_reply, "record_contact_received"):
            self.assertEqual(auto_reply._skip_not_grandma("小明", None, {}, "3號議員候選人請支持"), "SKIPPED_NOT_GRANDMA")
            self.assertIn("[競選圖略過]", self.logged())
            self.log.reset_mock()
            auto_reply._skip_not_grandma("小明", None, {}, "今天去爬山拍照")
            self.assertIn("[非長輩圖略過]", self.logged())                                  # 一般的非長輩圖照舊
            self.assertNotIn("競選", self.logged())

    def test_library_never_contains_campaign_images(self):
        self.text_image("ok.jpg", "早安平安喜樂")
        self.text_image("poster.jpg", "3號議員候選人全力衝刺請支持")
        library = il.scan_library(self.dir, auto_reply.KIND_INDEX)
        self.assertEqual([os.path.basename(i.path) for i in library], ["ok.jpg"])

    def test_downloaded_campaign_images_are_discarded(self):
        poster = self.text_image("poster.jpg", "市長候選人請投票")
        self.assertIs(auto_reply._ingest_downloaded(poster, None), False)
        self.assertFalse(os.path.exists(poster))


class TestSendFlow(_Base):
    """把 send_grandma_image 一路跑到送出，其餘安全檢查都當作通過"""

    def setUp(self):
        super().setUp()
        self.image = self.real_image("reply.jpg", color=(9, 9, 9))
        self.incoming = os.path.join(self.tmp.name, "incoming.eimg")
        with open(self.incoming, "wb") as f:
            f.write(b"\x00enc")
        self.slot = "morning"

    def run_flow(self, *, recognize=((True, MID_AUTUMN, "中秋節快樂"),), choose=None, attach=True, config=None,
                 triggered=True, slots=None, find_results=None, opener=False, title="小明",
                 text_trigger=False, text_result=(True, MORNING, "早安"), media_result=True, target="小明", self_last=False,
                 send_text=True, peek=(True, MORNING, "早安"), sticker=False):
        mocks = {}
        minimized = []

        def fake_peek(target_name, config, info=None):
            if info is not None and sticker is True:
                info["sticker"] = True                  # 真的「看一眼」會回報最底部是貼圖
            return peek

        patches = {
            "recognize": mock.patch.object(auto_reply, "recognize_incoming_image", side_effect=list(recognize)),
            "choose": mock.patch.object(auto_reply, "choose_reply_image", side_effect=choose if choose is not None else [self.image]),
            "attach": mock.patch.object(auto_reply, "send_image_via_attach_dialog", return_value=attach),
            "send_text": mock.patch.object(auto_reply, "send_text_via_input", return_value=send_text),
            "slot": mock.patch.object(auto_reply, "current_time_slot", side_effect=slots or (lambda: self.slot)),
            "contact_received": mock.patch.object(auto_reply, "record_contact_received"),
            "find": mock.patch.object(auto_reply, "find_target_chat_window",
                                      **({"side_effect": list(find_results)} if find_results is not None
                                         else {"return_value": [(CHAT, "小明", "Qt", True)]})),
            "opener": mock.patch.object(auto_reply, "open_missing_chat_window", return_value=opener),
            "harvest": mock.patch.object(auto_reply, "harvest_line_incoming_images"),
            "get_images": mock.patch.object(auto_reply, "get_images", return_value=[]),
            "countdown": mock.patch.object(auto_reply, "countdown_sleep"),
            "iconic": mock.patch.object(auto_reply.win32gui, "IsIconic", return_value=False),
            "fg": mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=CHAT),
            "title": mock.patch.object(auto_reply.win32gui, "GetWindowText", return_value=title),
            "activate": mock.patch.object(auto_reply, "activate_window"),
            "focus": mock.patch.object(auto_reply, "focus_chat_input", return_value=True),
            "self": mock.patch.object(auto_reply, "is_last_message_from_self_in_chat", return_value=self_last),
            "media": mock.patch.object(auto_reply, "_media_type_with_retry", return_value=media_result),
            "recognize_text": mock.patch.object(auto_reply, "recognize_incoming_text", return_value=text_result),
            "peek": mock.patch.object(auto_reply, "_peek_incoming_text", side_effect=fake_peek),
            "sticker": mock.patch.object(auto_reply, "_bottom_sticker", return_value=(sticker, None)),
            "recognize_sticker": mock.patch.object(auto_reply, "recognize_incoming_sticker", return_value=text_result),
            "cache_dir": mock.patch.object(auto_reply, "get_active_line_cache_dir", return_value="cache"),
            "cache_files": mock.patch.object(auto_reply, "get_all_cache_files", return_value={}),
            "chat_map": mock.patch.object(auto_reply, "load_chat_dirs_map", return_value={}),
            "verify": mock.patch.object(auto_reply, "verify_message_sent_to_chat", return_value=True),
            "reply_sent": mock.patch.object(auto_reply, "record_reply_sent"),
            "contact_sent": mock.patch.object(auto_reply, "record_contact_sent"),
            "retire": mock.patch.object(auto_reply, "retire_image"),
            "minimize": mock.patch.object(auto_reply, "_minimize_if_configured", side_effect=lambda h, c, log_target=None: minimized.append(h)),
            "notify": mock.patch.object(auto_reply.notifier, "notify"),
            "sleep": mock.patch.object(auto_reply.time, "sleep"),
        }
        with contextlib.ExitStack() as stack:
            for name, patcher in patches.items():
                mocks[name] = stack.enter_context(patcher)
            cfg = {"min_delay_seconds": 1, "max_delay_seconds": 1}
            cfg.update(config or {})
            result = auto_reply.send_grandma_image(target, cfg, triggered_file=self.incoming if triggered else None, text_trigger=text_trigger)
        return result, mocks, minimized

    def today(self):
        return reply_budget.count_today(self.counts_path, "小明")

    def test_successful_reply_is_counted_toward_the_daily_limit(self):
        result, m, _ = self.run_flow()
        self.assertEqual(result, "SENT_CONFIRMED")
        self.assertEqual(self.today(), 1)
        args = m["choose"].call_args.args
        self.assertEqual((args[1], args[2]), (MID_AUTUMN, "中秋節快樂"))                      # 把對方圖的性質與文字交給挑圖
        m["attach"].assert_called_once_with(CHAT, self.image)
        m["media"].assert_called_once_with(CHAT, expect_media=True)                           # 被圖片快取觸發：最後一則一定是圖，圖還沒載入完要等

    def test_at_most_three_replies_per_day(self):
        for expected in (1, 2, 3):
            self.assertEqual(self.run_flow()[0], "SENT_CONFIRMED")
            self.assertEqual(self.today(), expected)
        result, m, _ = self.run_flow()
        self.assertEqual(result, "SKIPPED_DAILY_LIMIT")                                       # 第四次不回
        m["recognize"].assert_not_called()                                                    # 連 OCR、視窗都不用碰
        m["choose"].assert_not_called()
        m["attach"].assert_not_called()
        self.assertEqual(self.today(), 3)

    def test_daily_limit_is_configurable_and_zero_means_unlimited(self):
        self.assertEqual(self.run_flow(config={"max_replies_per_day": 1})[0], "SENT_CONFIRMED")
        self.assertEqual(self.run_flow(config={"max_replies_per_day": 1})[0], "SKIPPED_DAILY_LIMIT")
        for _ in range(5):
            self.assertEqual(self.run_flow(config={"max_replies_per_day": 0})[0], "SENT_CONFIRMED")

    def text_flow(self, **kw):
        kw.setdefault("triggered", False)
        kw.setdefault("media_result", False)               # 最底部是文字氣泡
        return self.run_flow(text_trigger=True, **kw)

    def assert_thanks_only(self, result, m, text="謝謝"):
        self.assertEqual(result, "SENT_CONFIRMED")
        m["send_text"].assert_called_once_with(CHAT, text)
        m["attach"].assert_not_called()                                                        # 只回文字，不回圖
        m["choose"].assert_not_called()
        m["contact_sent"].assert_not_called()                                                  # 沒有圖，沒有百輪記憶要記
        m["retire"].assert_not_called()
        self.assertEqual(self.today(), 1)                                                      # 但算一次回覆 (與其他回覆共用每日上限)

    def test_birthday_picture_gets_only_a_thank_you(self):
        result, m, _ = self.run_flow(recognize=[(True, BIRTHDAY, "生日快樂")])
        self.assert_thanks_only(result, m)
        m["notify"].assert_called_once()
        self.assertIn("謝謝", m["notify"].call_args.args[0])

    def test_birthday_text_gets_only_a_thank_you(self):
        result, m, _ = self.text_flow(text_result=(True, BIRTHDAY, "生日快樂"))
        self.assert_thanks_only(result, m)

    def test_a_birthday_picture_without_a_wish_phrase_is_not_thanked(self):
        # 圖上只有「生日」(沒有「生日快樂」之類)：照一般節日圖回覆，不回謝謝
        result, m, _ = self.run_flow(recognize=[(True, BIRTHDAY, "生日蛋糕")])
        self.assertEqual(result, "SENT_CONFIRMED")
        m["attach"].assert_called_once()
        m["send_text"].assert_not_called()

    def test_birthday_picture_recognised_late_from_the_window_screenshot_also_gets_a_thank_you(self):
        result, m, _ = self.run_flow(recognize=[(None, None, ""), (True, BIRTHDAY, "生日快樂")])      # 視窗不在前景：先延後，帶到前景再辨識
        self.assert_thanks_only(result, m)

    def test_the_thank_you_text_comes_from_the_config(self):
        result, m, _ = self.run_flow(recognize=[(True, BIRTHDAY, "生日快樂")], config={"birthday_reply": "謝謝你的祝福"})
        self.assert_thanks_only(result, m, "謝謝你的祝福")

    def test_other_festivals_and_greetings_still_get_pictures(self):
        result, m, _ = self.run_flow()                                                         # 中秋節
        self.assertEqual(result, "SENT_CONFIRMED")
        m["attach"].assert_called_once()
        m["send_text"].assert_not_called()
        result, m, _ = self.text_flow(text_result=(True, MORNING, "早安"))
        m["attach"].assert_called_once()
        m["send_text"].assert_not_called()

    def test_blank_birthday_reply_keeps_the_old_behaviour(self):
        result, m, _ = self.run_flow(recognize=[(True, BIRTHDAY, "生日快樂")], config={"birthday_reply": ""})
        self.assertEqual(result, "SENT_CONFIRMED")
        m["attach"].assert_called_once()                                                       # 回一張生日圖
        m["send_text"].assert_not_called()

    def test_groups_keep_getting_a_birthday_picture(self):
        auto_reply.set_group_targets(["小明"])
        self.addCleanup(auto_reply.set_group_targets, [])
        result, m, _ = self.run_flow(recognize=[(True, BIRTHDAY, "生日快樂")])
        self.assertEqual(result, "SENT_CONFIRMED")
        m["attach"].assert_called_once()
        m["send_text"].assert_not_called()                                                     # 群組的生日祝福常常是祝別人，不回謝謝

    def test_birthday_thanks_shares_the_daily_limit(self):
        for _ in range(2):
            self.assertEqual(self.run_flow()[0], "SENT_CONFIRMED")
        self.assertEqual(self.run_flow(recognize=[(True, BIRTHDAY, "生日快樂")])[0], "SENT_CONFIRMED")
        self.assertEqual(self.today(), 3)
        result, m, _ = self.run_flow(recognize=[(True, BIRTHDAY, "生日快樂")])
        self.assertEqual(result, "SKIPPED_DAILY_LIMIT")                                        # 第 4 次 (謝謝也算) 不回
        m["send_text"].assert_not_called()

    def test_failed_text_send_is_retried_and_leaves_no_trace(self):
        result, m, minimized = self.run_flow(recognize=[(True, BIRTHDAY, "生日快樂")], send_text=False)
        self.assertEqual(result, "NOT_SENT")
        self.assertEqual(self.today(), 0)
        m["contact_sent"].assert_not_called()
        self.assertEqual(minimized, [CHAT])

    def test_birthday_text_still_gets_thanks_when_text_replies_are_off_but_other_text_does_not(self):
        result, m, _ = self.text_flow(text_result=(True, BIRTHDAY, "生日快樂"), peek=(True, BIRTHDAY, "生日快樂"), config={"reply_to_text": False})
        self.assert_thanks_only(result, m)
        result, m, _ = self.text_flow(text_result=(True, MORNING, "早安"), config={"reply_to_text": False})
        self.assertEqual(result, "SKIPPED_TEXT")
        m["attach"].assert_not_called()
        m["send_text"].assert_not_called()

    def test_text_flow_is_not_entered_when_both_text_replies_and_birthday_thanks_are_off(self):
        result, m, _ = self.text_flow(config={"reply_to_text": False, "birthday_reply": "", "reply_to_sticker_text": False}, recognize=[(False, None, "")])
        self.assertEqual(result, "SKIPPED_NOT_GRANDMA")
        m["recognize_text"].assert_not_called()

    def test_short_greeting_text_gets_a_picture_of_the_same_kind(self):
        result, m, _ = self.text_flow(text_result=(True, MID_AUTUMN, "中秋節快樂"))
        self.assertEqual(result, "SENT_CONFIRMED")
        args = m["choose"].call_args.args
        self.assertEqual((args[1], args[2]), (MID_AUTUMN, "中秋節快樂"))                       # 把文字的性質交給挑圖，跟圖片同一套規則
        m["attach"].assert_called_once_with(CHAT, self.image)
        m["recognize"].assert_not_called()                                                     # 沒有圖可辨識
        m["recognize_text"].assert_called_once()
        m["media"].assert_called_once_with(CHAT, expect_media=False)
        self.assertEqual(self.today(), 1)

    def test_text_and_picture_replies_share_the_same_daily_limit(self):
        self.assertEqual(self.text_flow()[0], "SENT_CONFIRMED")
        self.assertEqual(self.run_flow()[0], "SENT_CONFIRMED")                                  # 圖片
        self.assertEqual(self.text_flow()[0], "SENT_CONFIRMED")
        self.assertEqual(self.today(), 3)
        result, m, _ = self.text_flow()
        self.assertEqual(result, "SKIPPED_DAILY_LIMIT")                                         # 第 4 次 (不論圖或文字) 都不回
        m["recognize_text"].assert_not_called()
        m["attach"].assert_not_called()
        self.assertEqual(self.run_flow()[0], "SKIPPED_DAILY_LIMIT")

    def test_text_that_is_not_a_short_greeting_is_skipped_and_not_counted(self):
        result, m, minimized = self.text_flow(text_result=(False, None, "今天吃什麼"))
        self.assertEqual(result, "SKIPPED_TEXT")
        m["choose"].assert_not_called()
        m["attach"].assert_not_called()
        self.assertEqual(self.today(), 0)
        self.assertEqual(minimized, [CHAT])                                                     # 看完就縮回去，不留在前景
        self.assertIn("不是短問候或祝福", NL.join(str(c.args[0]) for c in self.log.call_args_list))

    def test_text_that_cannot_be_read_yet_is_retried_later(self):
        result, m, minimized = self.text_flow(text_result=(None, None, ""))
        self.assertEqual(result, "NOT_SENT")
        m["attach"].assert_not_called()
        self.assertEqual(self.today(), 0)

    def test_a_text_trigger_whose_last_message_is_really_a_picture_is_not_treated_as_text(self):
        result, m, _ = self.text_flow(media_result=True)
        self.assertEqual(result, "SKIPPED_TEXT")
        m["recognize_text"].assert_not_called()
        m["attach"].assert_not_called()

    def test_text_trigger_still_verifies_the_message_type_in_trigger_mode_all(self):
        result, m, _ = self.text_flow(config={"trigger_mode": "all"}, media_result=True)       # all 模式不套用「文字略過」，但文字流程自己要確認
        self.assertEqual(result, "SKIPPED_TEXT")
        m["recognize_text"].assert_not_called()
        result, m, _ = self.text_flow(config={"trigger_mode": "all"})
        self.assertEqual(result, "SENT_CONFIRMED")

    def test_text_replies_never_send_when_the_last_message_is_our_own(self):
        result, m, _ = self.text_flow(self_last=True)
        self.assertEqual(result, "NOT_SENT")
        m["attach"].assert_not_called()

    def test_a_non_greeting_text_is_dropped_before_the_random_wait_and_before_anything_else(self):
        # 審查發現：以前每則聊天文字都先睡 5~900 秒 (單執行緒，擋住所有好友) 才發現不是問候
        result, m, _ = self.text_flow(peek=(False, None, "今天吃什麼"))
        self.assertEqual(result, "SKIPPED_TEXT")
        m["peek"].assert_called_once()
        m["countdown"].assert_not_called()                                                     # 沒有睡
        m["get_images"].assert_not_called()                                                    # 也沒有為它去補圖庫、上網
        m["choose"].assert_not_called()
        m["attach"].assert_not_called()
        self.assertEqual(self.today(), 0)

    def test_text_that_cannot_be_peeked_yet_is_retried_without_waiting(self):
        result, m, _ = self.text_flow(peek=(None, None, ""))
        self.assertEqual(result, "NOT_SENT")
        m["countdown"].assert_not_called()

    def test_a_greeting_text_passes_the_peek_and_then_goes_through_the_whole_flow_with_the_wait(self):
        result, m, _ = self.text_flow()
        self.assertEqual(result, "SENT_CONFIRMED")
        m["peek"].assert_called_once()
        m["countdown"].assert_called_once()                                                    # 通過才做「像真人」的隨機等待
        m["recognize_text"].assert_called_once()                                               # 等完、視窗在前景時再確認一次 (期間可能有新訊息)

    def test_image_triggers_never_peek(self):
        _, m, _ = self.run_flow()
        m["peek"].assert_not_called()

    def test_text_trigger_needs_no_bound_channel(self):
        # 對方只傳文字、從沒傳過圖：chat_dirs.json 沒有他，文字照樣能回
        result, m, _ = self.text_flow()
        self.assertEqual(result, "SENT_CONFIRMED")
        self.assertEqual(m["chat_map"].return_value, {})

    def test_switch_off_and_requirements_keep_the_old_behaviour(self):
        result, m, _ = self.text_flow(config={"reply_to_text": False, "birthday_reply": "", "reply_to_sticker_text": False}, recognize=[(False, None, "")])
        self.assertEqual(result, "SKIPPED_NOT_GRANDMA")                                        # 關掉：沒有觸發檔 = 不是長輩圖
        m["recognize_text"].assert_not_called()
        result, m, _ = self.text_flow(config={"require_grandma_image": False})
        m["recognize_text"].assert_not_called()                                                 # 沒有識別就沒有「性質」可言，不走文字流程

    def test_groups_never_use_the_text_flow(self):
        auto_reply.set_group_targets(["小明"])
        self.addCleanup(auto_reply.set_group_targets, [])
        result, m, _ = self.text_flow(recognize=[(False, None, "")])
        self.assertEqual(result, "SKIPPED_NOT_GRANDMA")
        m["recognize_text"].assert_not_called()
        m["attach"].assert_not_called()

    def test_text_length_limit_comes_from_the_config(self):
        _, m, _ = self.text_flow(config={"text_reply_max_chars": 12})
        self.assertEqual(m["recognize_text"].call_args.args[1], 12)

    def test_a_group_uses_its_own_daily_limit_which_defaults_to_one(self):
        auto_reply.set_group_targets(["小明"])
        self.addCleanup(auto_reply.set_group_targets, [])
        self.assertEqual(self.run_flow(config={"max_replies_per_day": 3})[0], "SENT_CONFIRMED")
        result, m, _ = self.run_flow(config={"max_replies_per_day": 3})                       # 1:1 的上限 3 對群組不適用
        self.assertEqual(result, "SKIPPED_DAILY_LIMIT")
        m["recognize"].assert_not_called()
        self.assertEqual(self.run_flow(config={"group_max_replies_per_day": 2})[0], "SENT_CONFIRMED")
        self.assertEqual(self.run_flow(config={"group_max_replies_per_day": 0})[0], "SENT_CONFIRMED")   # 0 = 不限

    def test_one_to_one_chat_keeps_its_own_limit_even_when_a_group_exists(self):
        auto_reply.set_group_targets(["別的群組"])
        self.addCleanup(auto_reply.set_group_targets, [])
        for _ in range(3):
            self.assertEqual(self.run_flow()[0], "SENT_CONFIRMED")
        self.assertEqual(self.run_flow()[0], "SKIPPED_DAILY_LIMIT")

    def test_groups_are_always_recognised_even_when_recognition_is_switched_off(self):
        auto_reply.set_group_targets(["小明"])
        self.addCleanup(auto_reply.set_group_targets, [])
        result, m, _ = self.run_flow(config={"require_grandma_image": False}, recognize=[(False, None, "")])
        self.assertEqual(result, "SKIPPED_NOT_GRANDMA")                                     # 貼圖或辨識不出的：群組不回
        m["attach"].assert_not_called()
        auto_reply.set_group_targets([])
        result, m, _ = self.run_flow(config={"require_grandma_image": False}, recognize=[(False, None, "")])
        self.assertEqual(result, "SENT_CONFIRMED")                                          # 1:1：關掉識別就是舊行為，什麼都回
        m["recognize"].assert_not_called()

    def sticker_flow(self, **kw):
        """最底部是貼圖的文字觸發流程 (視窗列表有變化)：看一眼通過、最後一則是媒體、而且是貼圖"""
        kw.setdefault("sticker", True)
        kw.setdefault("media_result", True)
        kw.setdefault("text_result", (True, MORNING, "早安安"))
        kw.setdefault("peek", (True, MORNING, "早安安"))
        return self.text_flow(**kw)

    def test_a_sticker_good_morning_gets_the_matching_image(self):
        result, m, _ = self.sticker_flow()
        self.assertEqual(result, "SENT_CONFIRMED")
        self.assertEqual(m["choose"].call_args.args[1], MORNING)
        m["attach"].assert_called_once_with(CHAT, self.image)
        m["recognize_sticker"].assert_called_once()                                              # 讀的是貼圖，不是文字氣泡
        m["recognize_text"].assert_not_called()
        self.assertIn("貼圖識別", NL.join(str(c.args[0]) for c in self.log.call_args_list))

    def test_the_sticker_text_uses_the_text_length_limit_from_the_config(self):
        _, m, _ = self.sticker_flow(config={"text_reply_max_chars": 9})
        self.assertEqual(m["recognize_sticker"].call_args.args[1], 9)

    def test_with_the_option_off_a_sticker_is_skipped_like_any_picture(self):
        result, m, _ = self.sticker_flow(config={"reply_to_sticker_text": False})
        self.assertEqual(result, "SKIPPED_TEXT")
        m["recognize_sticker"].assert_not_called()
        m["recognize_text"].assert_not_called()
        m["attach"].assert_not_called()
        self.assertIn("圖片或貼圖，不是文字", NL.join(str(c.args[0]) for c in self.log.call_args_list))

    def test_photo_only_mode_never_replies_to_a_sticker(self):
        result, m, _ = self.sticker_flow(config={"trigger_mode": "photo_only"})
        self.assertEqual(result, "SKIPPED_TEXT")
        m["recognize_sticker"].assert_not_called()
        m["attach"].assert_not_called()

    def test_a_photo_at_the_bottom_is_still_left_to_the_cache_path(self):
        result, m, _ = self.sticker_flow(sticker=False)                                         # 最底部是媒體，但不是貼圖 (是照片)
        self.assertEqual(result, "SKIPPED_TEXT")
        m["recognize_sticker"].assert_not_called()
        m["attach"].assert_not_called()

    def test_when_it_cannot_tell_a_sticker_from_a_photo_it_retries_instead_of_skipping_for_good(self):
        # 審查：暫時性的失敗不能被當成「不是貼圖」而永久略過 (SKIPPED_TEXT 會讓基準線前進)
        result, m, _ = self.sticker_flow(sticker=None)
        self.assertEqual(result, "NOT_SENT")
        m["attach"].assert_not_called()
        self.assertIn("貼圖待確認", NL.join(str(c.args[0]) for c in self.log.call_args_list))

    def test_a_sticker_without_a_greeting_is_skipped_with_its_own_log_line(self):
        result, m, _ = self.sticker_flow(peek=(False, None, "OK啦"))
        self.assertEqual(result, "SKIPPED_TEXT")
        m["attach"].assert_not_called()
        logged = NL.join(str(c.args[0]) for c in self.log.call_args_list)
        self.assertNotIn("最後一則文字不是短問候", logged)

    def test_a_sticker_that_cannot_be_read_yet_is_retried_later(self):
        result, m, _ = self.sticker_flow(text_result=(None, None, ""))
        self.assertEqual(result, "NOT_SENT")
        m["attach"].assert_not_called()
        self.assertIn("貼圖", NL.join(str(c.args[0]) for c in self.log.call_args_list))

    def test_a_sticker_without_a_greeting_after_the_wait_is_skipped_as_a_sticker(self):
        result, m, _ = self.sticker_flow(text_result=(False, None, "OK啦"))
        self.assertEqual(result, "SKIPPED_TEXT")
        m["attach"].assert_not_called()
        logged = NL.join(str(c.args[0]) for c in self.log.call_args_list)
        self.assertIn("貼圖略過", logged)
        self.assertNotIn("最後一則文字不是短問候", logged)

    def test_a_sticker_still_gets_a_reply_when_text_replies_are_switched_off(self):
        # reply_to_text 管文字訊息，reply_to_sticker_text 管貼圖：各自獨立
        result, m, _ = self.sticker_flow(config={"reply_to_text": False, "birthday_reply": "", "reply_to_sticker_text": True})
        self.assertEqual(result, "SENT_CONFIRMED")
        m["attach"].assert_called_once()
        m["recognize_sticker"].assert_called_once()
        m["recognize"].assert_not_called()                                                        # 不是一般圖片的辨識 (沒有快取檔可辨識)

    def test_a_plain_text_greeting_is_dropped_before_the_wait_when_only_the_sticker_option_is_on(self):
        # 審查：只開貼圖設定時，文字問候不能先通過「看一眼」、睡上最多 900 秒才略過 (主迴圈單執行緒，會卡住其他好友)
        result, m, _ = self.text_flow(config={"reply_to_text": False, "birthday_reply": "", "reply_to_sticker_text": True},
                                      peek=(True, MORNING, "早安"))
        self.assertEqual(result, "SKIPPED_TEXT")
        m["countdown"].assert_not_called()                                                        # 沒有睡
        m["get_images"].assert_not_called()                                                       # 也沒有去補圖庫
        m["attach"].assert_not_called()
        self.assertIn("reply_to_text 已關閉", NL.join(str(c.args[0]) for c in self.log.call_args_list))

    def test_a_text_birthday_wish_still_gets_thanks_when_only_the_sticker_option_is_on(self):
        wish = ik.classify_message("生日快樂")
        result, m, _ = self.text_flow(config={"reply_to_text": False, "birthday_reply": "謝謝", "reply_to_sticker_text": True},
                                      peek=(True, wish, "生日快樂"), text_result=(True, wish, "生日快樂"))
        self.assertEqual(result, "SENT_CONFIRMED")
        m["send_text"].assert_called_once()
        m["attach"].assert_not_called()

    def test_a_sticker_birthday_wish_only_gets_a_thank_you(self):
        wish = ik.classify_message("生日快樂")
        result, m, _ = self.sticker_flow(text_result=(True, wish, "生日快樂"), peek=(True, wish, "生日快樂"), config={"birthday_reply": "謝謝"})
        self.assertEqual(result, "SENT_CONFIRMED")
        m["send_text"].assert_called_once()
        m["choose"].assert_not_called()
        m["attach"].assert_not_called()

    def test_a_group_never_enters_the_sticker_flow(self):
        auto_reply.set_group_targets(["小明"])
        self.addCleanup(auto_reply.set_group_targets, [])
        result, m, _ = self.sticker_flow(recognize=[(False, None, "")])
        self.assertEqual(result, "SKIPPED_NOT_GRANDMA")
        m["recognize_sticker"].assert_not_called()
        m["recognize_text"].assert_not_called()
        m["attach"].assert_not_called()

    def test_group_window_title_with_member_count_may_send_but_a_one_to_one_one_may_not(self):
        result, m, _ = self.run_flow(title="小明 (477)")                                       # 1:1 前景標題帶括號 = 群組冒充，擋下
        self.assertEqual(result, "NOT_SENT")
        m["attach"].assert_not_called()
        auto_reply.set_group_targets(["小明"])
        self.addCleanup(auto_reply.set_group_targets, [])
        result, m, _ = self.run_flow(title="小明 (477)")
        self.assertEqual(result, "SENT_CONFIRMED")
        m["attach"].assert_called_once()

    def test_group_send_is_refused_when_the_foreground_title_is_another_chat(self):
        auto_reply.set_group_targets(["小明"])
        self.addCleanup(auto_reply.set_group_targets, [])
        for title in ("別的群組 (12)", "小明2 (477)", "小明 (x)"):
            result, m, _ = self.run_flow(title=title)
            self.assertEqual(result, "NOT_SENT", title)
            m["attach"].assert_not_called()

    def test_skipped_and_failed_replies_do_not_count(self):
        result, m, _ = self.run_flow(recognize=[(False, None, "")])                           # 不是長輩圖 / 辨識不出來
        self.assertEqual(result, "SKIPPED_NOT_GRANDMA")
        m["choose"].assert_not_called()
        m["contact_received"].assert_called_once()
        result, _, _ = self.run_flow(choose=[None])                                          # 找不到回覆圖
        self.assertEqual(result, "SKIPPED_NO_IMAGE")
        result, _, _ = self.run_flow(attach=False)                                           # 送出失敗
        self.assertEqual(result, "NOT_SENT")
        self.assertEqual(self.today(), 0)

    def test_closed_chat_window_is_reopened_before_sending(self):
        window = [(CHAT, "小明", "Qt", True)]
        # 依序：辨識前的提前查詢 (視窗不見)、找視窗 (不見)、開窗成功後再找 (出現)
        result, m, _ = self.run_flow(find_results=[[], [], window], opener=True)
        self.assertEqual(result, "SENT_CONFIRMED")
        m["opener"].assert_called_once_with("小明", mock.ANY)
        self.assertEqual(self.today(), 1)

    def test_window_that_cannot_be_reopened_means_nothing_is_sent(self):
        result, m, _ = self.run_flow(find_results=[[], [], []], opener=False)
        self.assertEqual(result, "NOT_SENT")                                                # 維持原本的保守行為：不送、走重試
        m["attach"].assert_not_called()
        self.assertEqual(self.today(), 0)

    def test_existing_window_is_never_reopened(self):
        result, m, _ = self.run_flow()
        self.assertEqual(result, "SENT_CONFIRMED")
        m["opener"].assert_not_called()

    def test_no_image_to_send_does_not_open_any_window(self):
        result, m, _ = self.run_flow(choose=[None], find_results=[[]])
        self.assertEqual(result, "SKIPPED_NO_IMAGE")
        m["opener"].assert_not_called()                                                     # 沒有要回的圖就不必動主視窗

    def test_campaign_image_is_skipped_with_its_own_log_and_not_counted(self):
        result, m, _ = self.run_flow(recognize=[(False, None, "3號議員候選人全力衝刺")])
        self.assertEqual(result, "SKIPPED_NOT_GRANDMA")
        self.assertIn("[競選圖略過]", NL.join(str(c.args[0]) for c in self.log.call_args_list))
        m["choose"].assert_not_called()
        m["attach"].assert_not_called()
        self.assertEqual(self.today(), 0)

    def test_campaign_image_found_after_deferred_recognition(self):
        result, m, minimized = self.run_flow(recognize=[(None, None, ""), (False, None, "請投票給5號")])
        self.assertEqual((result, minimized), ("SKIPPED_NOT_GRANDMA", [CHAT]))
        self.assertIn("[競選圖略過]", NL.join(str(c.args[0]) for c in self.log.call_args_list))
        m["choose"].assert_not_called()
        self.assertEqual(self.today(), 0)

    def test_no_image_stops_before_touching_the_window(self):
        result, m, minimized = self.run_flow(choose=[None])
        self.assertEqual(result, "SKIPPED_NO_IMAGE")
        m["countdown"].assert_not_called()                                                    # 不必白等隨機延遲
        m["activate"].assert_not_called()
        self.assertEqual(minimized, [])

    def test_deferred_recognition_then_choice_with_window_in_foreground(self):
        result, m, _ = self.run_flow(recognize=[(None, None, ""), (True, K("care"), "天冷注意保暖")])
        self.assertEqual(result, "SENT_CONFIRMED")
        self.assertEqual(m["recognize"].call_count, 2)
        self.assertEqual(m["recognize"].call_args_list[1].args, (self.incoming, CHAT))        # 第二次是在視窗前景時問
        self.assertEqual(m["choose"].call_count, 1)                                          # 性質辨識出來之後才挑一次
        self.assertEqual(m["choose"].call_args.args[1], K("care"))
        self.assertEqual(self.today(), 1)

    def test_deferred_recognition_failures(self):
        result, m, minimized = self.run_flow(recognize=[(None, None, ""), (None, None, "")])
        self.assertEqual((result, minimized), ("NOT_SENT", [CHAT]))                           # 還是判斷不出來：走重試
        m["choose"].assert_not_called()
        result, m, minimized = self.run_flow(recognize=[(None, None, ""), (False, None, "")])
        self.assertEqual((result, minimized), ("SKIPPED_NOT_GRANDMA", [CHAT]))
        result, m, minimized = self.run_flow(recognize=[(None, None, ""), (True, K("care"), "t")], choose=[None])
        self.assertEqual((result, minimized), ("SKIPPED_NO_IMAGE", [CHAT]))                   # 辨識得出但沒有合適的回覆圖
        self.assertEqual(self.today(), 0)

    def test_time_slot_change_during_the_delay_rechooses_non_festival_replies(self):
        other = self.real_image("other.jpg", color=(8, 8, 8))
        slots = iter(["morning", "noon", "noon", "noon"])                            # 第一次是挑圖時的時段，之後是送出前
        result, m, _ = self.run_flow(recognize=[(True, MORNING, "早安")], choose=[self.image, other],
                                     slots=lambda: next(slots))
        self.assertEqual(result, "SENT_CONFIRMED")
        self.assertEqual(m["choose"].call_count, 2)                                          # 10:55 挑的早安圖，11:10 才送出：重挑
        m["attach"].assert_called_once_with(CHAT, other)

    def test_festival_replies_do_not_depend_on_the_time_slot(self):
        slots = iter(["morning", "evening", "evening", "evening"])
        result, m, _ = self.run_flow(slots=lambda: next(slots))
        self.assertEqual(result, "SENT_CONFIRMED")
        self.assertEqual(m["choose"].call_count, 1)                                          # 節日圖不看時段，不重挑

    def test_recognition_can_be_turned_off(self):
        result, m, _ = self.run_flow(config={"require_grandma_image": False}, triggered=False)
        self.assertEqual(result, "SENT_CONFIRMED")                                            # 舊行為：不辨識，照時段回問候圖
        m["recognize"].assert_not_called()
        self.assertEqual(m["choose"].call_args.args[1], K("greeting"))
        m["media"].assert_called_once_with(CHAT, expect_media=False)                          # 沒有圖片觸發 (例如文字觸發)：文字就是文字


class TestStartup(_Base):
    def test_report_ocr_engine(self):
        logs = []
        with mock.patch.object(auto_reply, "log_v", side_effect=logs.append):
            with mock.patch.object(auto_reply.ocr_engine, "is_available", return_value=True):
                self.assertTrue(auto_reply.report_ocr_engine())
            with mock.patch.object(auto_reply.ocr_engine, "is_available", return_value=False),                  mock.patch.object(auto_reply.ocr_engine, "unavailable_reason", return_value="ModuleNotFoundError: x"):
                self.assertFalse(auto_reply.report_ocr_engine())
        self.assertIn("RapidOCR", logs[0])
        self.assertIn("pip install rapidocr-onnxruntime", logs[1])                              # 沒裝要明確告訴使用者怎麼裝
        self.assertIn("辨識率大幅下降", logs[1])

    def test_warm_up_scans_the_library_in_the_background_and_reports_counts(self):
        self.text_image("a.jpg", "早安平安")
        self.text_image("b.jpg", "中秋節快樂")
        self.text_image("c.jpg", "FORMOSA!")
        logs = []

        class Inline:
            def __init__(self, target, name=None, daemon=None):
                self.target, self.daemon = target, daemon

            def start(self):
                self.target()

        with mock.patch.object(auto_reply, "log_v", side_effect=logs.append),              mock.patch.object(auto_reply.threading, "Thread", Inline):
            auto_reply.warm_up_image_library({"images_dir": self.dir, "auto_crawl_images": False})
        self.assertTrue(any("可用 2 張" in l and "節日 1" in l and "時段問候 1" in l for l in logs), logs)   # 辨識不出的那張不算

    def test_warm_up_tops_up_the_library_from_the_web_before_scanning(self):
        # 圖庫不進版控：剛下載的專案是空的，啟動時要先上網補到 min_image_pool 張，再辨識
        logs = []
        calls = []

        class Inline:
            def __init__(self, target, name=None, daemon=None):
                self.target = target

            def start(self):
                self.target()

        def fake_get_images(images_dir, auto_replenish=True, min_pool=10):
            calls.append((images_dir, auto_replenish, min_pool))
            self.text_image("downloaded.jpg", "早安平安")                                     # 模擬剛抓下來的圖
            return []

        with mock.patch.object(auto_reply, "log_v", side_effect=logs.append), mock.patch.object(auto_reply.threading, "Thread", Inline), \
             mock.patch.object(auto_reply, "get_images", side_effect=fake_get_images):
            auto_reply.warm_up_image_library({"images_dir": self.dir, "min_image_pool": 7})
        self.assertEqual(calls, [(self.dir, True, 7)])
        self.assertTrue(any("可用 1 張" in l for l in logs), logs)                           # 補完之後才辨識，剛抓的圖算進去

    def test_warm_up_does_not_go_online_when_auto_crawl_is_off(self):
        calls = []

        class Inline:
            def __init__(self, target, name=None, daemon=None):
                self.target = target

            def start(self):
                self.target()

        with mock.patch.object(auto_reply, "log_v"), mock.patch.object(auto_reply.threading, "Thread", Inline), \
             mock.patch.object(auto_reply, "get_images", side_effect=lambda d, auto_replenish=True, min_pool=10: calls.append(auto_replenish) or []):
            auto_reply.warm_up_image_library({"images_dir": self.dir, "auto_crawl_images": False})
        self.assertEqual(calls, [False])

    def test_two_replenish_attempts_do_not_download_twice(self):
        downloads = []

        def fake_crawl(needed, abs_dir):
            downloads.append(needed)
            for i in range(needed):
                self.text_image(f"dl{i}.jpg", "早安平安")

        with mock.patch.object(auto_reply, "log_v"), mock.patch.object(auto_reply, "crawl_images_now", side_effect=fake_crawl):
            first = auto_reply.get_images(self.dir, auto_replenish=True, min_pool=3)
            second = auto_reply.get_images(self.dir, auto_replenish=True, min_pool=3)         # 啟動補庫存之後送圖又檢查一次
        self.assertEqual(downloads, [3])
        self.assertEqual((len(first), len(second)), (3, 3))

    def test_two_threads_replenishing_at_the_same_time_download_only_once(self):
        # 啟動時的背景補庫存還在下載，第一張圖就來了：送圖前的補庫存要等它做完，不能再抓一次
        import threading as real_threading
        downloads = []
        started = real_threading.Event()
        release = real_threading.Event()

        def slow_crawl(needed, abs_dir):
            downloads.append(needed)
            started.set()
            release.wait(5)
            for i in range(needed):
                self.text_image(f"dl{i}.jpg", "早安平安")

        results = {}

        def call(name):
            results[name] = len(auto_reply.get_images(self.dir, auto_replenish=True, min_pool=3))

        with mock.patch.object(auto_reply, "log_v"), mock.patch.object(auto_reply, "crawl_images_now", side_effect=slow_crawl):
            a = real_threading.Thread(target=call, args=("a",))
            a.start()
            self.assertTrue(started.wait(5))
            b = real_threading.Thread(target=call, args=("b",))
            b.start()                                                                        # b 已經掃過圖庫 (還是空的)，卡在鎖上
            time.sleep(0.3)
            release.set()
            a.join(10)
            b.join(10)
        self.assertEqual(downloads, [3])                                                      # b 等到鎖之後重新檢查，發現夠了
        self.assertEqual(results, {"a": 3, "b": 3})

    def test_warm_up_failure_is_reported_not_raised(self):
        logs = []

        class Inline:
            def __init__(self, target, name=None, daemon=None):
                self.target = target

            def start(self):
                self.target()

        with mock.patch.object(auto_reply, "log_v", side_effect=logs.append),              mock.patch.object(auto_reply.threading, "Thread", Inline),              mock.patch.object(auto_reply.image_library, "scan_library", side_effect=RuntimeError("disk")):
            auto_reply.warm_up_image_library({"images_dir": self.dir, "auto_crawl_images": False})
        self.assertTrue(any("預先辨識圖庫失敗" in l for l in logs))


class TestWaitUntilChatSettled(_Base):
    def settle(self, images, timeout=3.0):
        from PIL import Image
        seq = iter(images)
        last = {"v": None}

        def sig(hwnd):
            try:
                v = next(seq)
            except StopIteration:
                v = last["v"]
            if isinstance(v, Exception):
                raise v
            last["v"] = v
            return v

        with mock.patch.object(auto_reply, "_chat_signature", side_effect=sig) as m:
            return auto_reply._wait_until_chat_settled(CHAT, timeout=timeout, interval=0.3), m.call_count

    def img(self, level, noise=0):
        from PIL import Image
        im = Image.new("L", (64, 64), level)
        if noise:
            im.putpixel((3, 3), min(255, level + noise))
        return im

    def test_two_identical_screens_in_a_row_are_settled(self):
        self.assertEqual(self.settle([self.img(200), self.img(200)]), (True, 2))

    def test_it_keeps_waiting_while_the_screen_is_still_changing(self):
        ok, calls = self.settle([self.img(250), self.img(100), self.img(180), self.img(180)])
        self.assertEqual((ok, calls), (True, 4))
        self.assertAlmostEqual(sum(self.sleeps), 0.9)                                          # 等了三輪 (用假時鐘，沒有真的睡)

    def test_a_blinking_caret_or_tiny_noise_does_not_count_as_changing(self):
        self.assertEqual(self.settle([self.img(200), self.img(200, noise=200)]), (True, 2))     # 只有一個像素不同

    def test_a_screen_that_never_stops_changing_gives_up_after_the_timeout(self):
        ok, calls = self.settle([self.img(i * 20 % 250) for i in range(100)], timeout=1.0)
        self.assertFalse(ok)
        self.assertLess(calls, 10)

    def test_a_failed_capture_means_not_settled_and_never_raises(self):
        self.assertEqual(self.settle([RuntimeError("no screen")]), (False, 1))


class TestPeekIncomingText(_Base):
    def peek(self, *, found=True, iconic=False, front=True, own_last=False, recognized=(True, MORNING, "早安"), explode=False):
        window = [(CHAT, "小明", "Qt", True)] if found else []
        recognize = mock.Mock(side_effect=RuntimeError("boom")) if explode else mock.Mock(return_value=recognized)
        with mock.patch.object(auto_reply, "find_target_chat_window", return_value=window), \
             mock.patch.object(auto_reply.win32gui, "IsIconic", return_value=iconic), \
             mock.patch.object(auto_reply.win32gui, "ShowWindow") as show, \
             mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=CHAT if front else CHAT + 1), \
             mock.patch.object(auto_reply, "activate_window"), \
             mock.patch.object(auto_reply, "is_last_message_from_self_in_chat", return_value=own_last), \
             mock.patch.object(auto_reply, "recognize_incoming_text", recognize), \
             mock.patch.object(auto_reply, "_minimize_if_configured") as minimize:
            try:
                result = auto_reply._peek_incoming_text("小明", {"text_reply_max_chars": 12, "reply_to_sticker_text": False})
            except RuntimeError:
                result = "raised"
        return result, recognize, minimize, show

    def test_a_greeting_is_recognised_and_the_window_is_put_back(self):
        result, recognize, minimize, _ = self.peek()
        self.assertEqual(result, (True, MORNING, "早安"))
        recognize.assert_called_once_with(CHAT, 12, settle=True)                                # 字數上限來自設定；沒有貼圖偵測時由文字辨識自己等畫面穩定
        minimize.assert_called_once()

    def peek_sticker(self, cfg, group=False, bottom=(True, None), sticker_result=(True, MORNING, "早安安")):
        auto_reply.set_group_targets(["小明"] if group else [])
        self.addCleanup(auto_reply.set_group_targets, [])
        info = {}
        with mock.patch.object(auto_reply, "find_target_chat_window", return_value=[(CHAT, "小明", "Qt", True)]), \
             mock.patch.object(auto_reply.win32gui, "IsIconic", return_value=False), \
             mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=CHAT), \
             mock.patch.object(auto_reply, "activate_window"), \
             mock.patch.object(auto_reply, "_wait_until_chat_settled"), \
             mock.patch.object(auto_reply, "is_last_message_from_self_in_chat", return_value=False), \
             mock.patch.object(auto_reply, "_bottom_sticker", return_value=bottom), \
             mock.patch.object(auto_reply, "recognize_incoming_sticker", return_value=sticker_result) as sticker, \
             mock.patch.object(auto_reply, "recognize_incoming_text", return_value=(True, MORNING, "早安")) as text, \
             mock.patch.object(auto_reply, "_minimize_if_configured"):
            result = auto_reply._peek_incoming_text("小明", dict(cfg), info)
        return result, info, sticker, text

    def test_a_bottom_sticker_is_read_as_a_sticker_and_reported_to_the_caller(self):
        result, info, sticker, text = self.peek_sticker({})
        self.assertEqual(result, (True, MORNING, "早安安"))
        self.assertEqual(info, {"sticker": True})
        sticker.assert_called_once()
        text.assert_not_called()

    def test_the_screen_is_settled_before_the_peek_decides_whether_it_is_a_sticker(self):
        # 視窗剛被帶到前景，LINE 還在重排；立刻偵測會把還沒畫好的畫面當成「不是貼圖」或判斷不出
        order = []
        with mock.patch.object(auto_reply, "find_target_chat_window", return_value=[(CHAT, "小明", "Qt", True)]), \
             mock.patch.object(auto_reply.win32gui, "IsIconic", return_value=False), \
             mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=CHAT), \
             mock.patch.object(auto_reply, "activate_window"), \
             mock.patch.object(auto_reply, "_wait_until_chat_settled", side_effect=lambda h: order.append("settle") or True), \
             mock.patch.object(auto_reply, "is_last_message_from_self_in_chat", return_value=False), \
             mock.patch.object(auto_reply, "_bottom_sticker", side_effect=lambda h, wait_seconds=0.0: order.append("detect") or (False, None)), \
             mock.patch.object(auto_reply, "recognize_incoming_text", side_effect=lambda *a, **k: order.append(("text", k.get("settle"))) or (True, MORNING, "早安")), \
             mock.patch.object(auto_reply, "_minimize_if_configured"):
            auto_reply._peek_incoming_text("小明", {})
        # 先等穩定再偵測；偵測完讀文字時不必再等一次 (settle=False)，一般文字問候不多付一次等待
        self.assertEqual(order, ["settle", "detect", ("text", False)])

    def test_a_bottom_text_message_is_read_as_text(self):
        result, info, sticker, text = self.peek_sticker({}, bottom=(False, None))
        self.assertEqual(result, (True, MORNING, "早安"))
        self.assertEqual(info, {})
        sticker.assert_not_called()
        text.assert_called_once()

    def test_when_it_cannot_tell_if_it_is_a_sticker_it_asks_again_later(self):
        result, info, sticker, text = self.peek_sticker({}, bottom=(None, None))
        self.assertEqual(result, (None, None, ""))                                        # 稍後重試，不是「不是貼圖」
        sticker.assert_not_called()
        text.assert_not_called()

    def test_stickers_are_never_looked_for_when_the_option_is_off_in_a_group_or_in_photo_only_mode(self):
        for cfg, group in (({"reply_to_sticker_text": False}, False), ({}, True), ({"trigger_mode": "photo_only"}, False)):
            _, info, sticker, text = self.peek_sticker(cfg, group=group, bottom=(True, None))
            sticker.assert_not_called()
            text.assert_called_once()
            self.assertEqual(info, {}, (cfg, group))

    def test_other_text_is_a_no(self):
        self.assertEqual(self.peek(recognized=(False, None, "今天吃什麼"))[0][0], False)

    def test_a_minimised_window_is_restored_first(self):
        _, _, _, show = self.peek(iconic=True)
        show.assert_called_once()

    def test_without_the_window_or_the_foreground_it_cannot_tell_and_never_reads_the_screen(self):
        result, recognize, _, _ = self.peek(found=False)
        self.assertEqual(result[0], None)
        recognize.assert_not_called()
        result, recognize, minimize, _ = self.peek(front=False)
        self.assertEqual(result[0], None)
        recognize.assert_not_called()                                                           # 視窗不在前景時截到的是別人的畫面
        minimize.assert_called_once()                                                           # 但視窗一樣縮回去

    def test_when_the_last_message_is_our_own_there_is_nothing_to_answer(self):
        result, recognize, _, _ = self.peek(own_last=True)
        self.assertEqual(result[0], False)
        recognize.assert_not_called()

    def test_the_window_is_always_put_back_even_when_reading_blows_up(self):
        result, _, minimize, _ = self.peek(explode=True)
        self.assertEqual(result, "raised")
        minimize.assert_called_once()


class TestDetectionReopensTheWindow(_Base):
    """偵測階段：頻道已綁定、但好友的獨立視窗不見了，不再直接丟掉事件，先試著把視窗開回來"""

    def run_detection(self, window_open, opener_result, config_extra=None, group=False, photo=True, last_reply_ago=None, windows=(), size=300000, replies_today=0):
        cache = os.path.join(self.tmp.name, "cache")
        fake_hash = "888803ef1234567890abcdef1234567890abcdef"
        chat_m = os.path.join(cache, fake_hash, "m")
        os.makedirs(chat_m, exist_ok=True)
        img = os.path.join(chat_m, "t.eimg")
        with open(img, "wb") as f:
            f.write(b"\x00" * size)
        now = time.time()
        os.utime(img, (now, now))
        sent = []
        state = {"open": window_open}
        window = [(CHAT, "小明", "Qt", True)]

        def open_it(name, config):
            state["open"] = bool(opener_result)                     # 開窗成功之後，視窗就找得到了
            return opener_result

        cfg = {"targets": ["小明"], "trigger_mode": "photo_and_sticker", "cooldown_seconds": 0, "min_delay_seconds": 0, "max_delay_seconds": 0}
        cfg.update(config_extra or {})
        with mock.patch.object(auto_reply, "CHAT_MAP_PATH", os.path.join(self.tmp.name, "chat_dirs.json")):
            with auto_reply.locked_chat_dirs() as m:
                m[fake_hash] = "小明"
            with mock.patch.object(auto_reply, "get_active_line_cache_dir", return_value=cache), \
                 mock.patch.object(auto_reply, "find_target_chat_window", side_effect=lambda t: window if state["open"] else []), \
                 mock.patch.object(auto_reply, "is_valid_image_or_eimg", return_value=True), \
                 mock.patch.object(auto_reply, "is_likely_photo", return_value=photo), \
                 mock.patch.object(auto_reply, "get_all_line_windows", return_value=list(windows)), \
                 mock.patch.object(auto_reply, "open_missing_chat_window", side_effect=open_it) as opener, \
                 mock.patch.object(auto_reply, "send_grandma_image", side_effect=lambda t, c, **kw: sent.append(t) or "SENT_CONFIRMED"), \
                 mock.patch.object(auto_reply, "check_circuit_breaker", return_value=True):
                auto_reply.last_global_reply_time = 0.0
                auto_reply.last_reply_times.pop("小明", None)
                if last_reply_ago is not None:
                    auto_reply.last_reply_times["小明"] = now - last_reply_ago
                for _ in range(replies_today):
                    reply_budget.record_reply(self.counts_path, "小明")
                auto_reply.PROCESSED_CACHE_FILES.clear()
                auto_reply.last_seen_cache_mtime = now - 1.0
                auto_reply.set_group_targets(["小明"] if group else [])
                try:
                    auto_reply.check_for_incoming_messages(cfg)
                finally:
                    auto_reply.set_group_targets([])
        return sent, opener

    def test_missing_window_is_reopened_and_the_reply_goes_ahead(self):
        sent, opener = self.run_detection(window_open=False, opener_result=True)
        self.assertEqual(sent, ["小明"])
        opener.assert_called_once_with("小明", mock.ANY)

    def test_the_main_window_is_not_hijacked_for_a_message_that_would_never_be_answered(self):
        # 審查發現：以前沒視窗就先開窗，才檢查上限與冷卻；對一張不會回的圖也會搶你的主視窗、在搜尋欄打字
        sent, opener = self.run_detection(window_open=False, opener_result=True, replies_today=3)       # 今天已回 3 次 (上限)
        self.assertEqual((sent, opener.call_count), ([], 0))
        sent, opener = self.run_detection(window_open=False, opener_result=True, last_reply_ago=5, config_extra={"cooldown_seconds": 30})
        self.assertEqual((sent, opener.call_count), ([], 0))                                           # 還在冷卻
        if os.path.exists(self.counts_path):
            os.remove(self.counts_path)                                                                # 清掉前面幾次記下的回覆次數
        sent, opener = self.run_detection(window_open=False, opener_result=True, replies_today=2)
        self.assertEqual((sent, opener.call_count), (["小明"], 1))                                      # 還有額度、不在冷卻：照常開窗

    def test_groups_use_their_own_limit_and_cooldown_before_opening(self):
        sent, opener = self.run_detection(window_open=False, opener_result=True, group=True, replies_today=1)      # 群組上限預設 1 次
        self.assertEqual(opener.call_count, 0)
        sent, opener = self.run_detection(window_open=False, opener_result=True, group=True, last_reply_ago=120)   # 群組冷卻 600 秒
        self.assertEqual(opener.call_count, 0)
        if os.path.exists(self.counts_path):
            os.remove(self.counts_path)
        sent, opener = self.run_detection(window_open=False, opener_result=True, group=False, replies_today=1, last_reply_ago=120)
        self.assertEqual(opener.call_count, 1)                                                                     # 同樣狀況一對一 (上限 3、冷卻 0) 照開

    def test_opening_the_window_is_attempted_at_most_once_a_minute_per_contact(self):
        _, opener = self.run_detection(window_open=False, opener_result=False)                          # 開不出來 (例如視窗名稱對不上)
        self.assertEqual(opener.call_count, 1)
        _, opener = self.run_detection(window_open=False, opener_result=False)                          # 馬上又來一個事件：不再搶主視窗
        self.assertEqual(opener.call_count, 0)
        self.clock[0] += auto_reply.OPEN_ATTEMPT_MIN_INTERVAL + 1
        _, opener = self.run_detection(window_open=False, opener_result=False)
        self.assertEqual(opener.call_count, 1)                                                          # 一分鐘後可以再試

    def test_window_that_cannot_be_reopened_still_drops_the_event(self):
        sent, opener = self.run_detection(window_open=False, opener_result=False)
        self.assertEqual(sent, [])                                                          # 維持原本的安全略過
        self.assertIn("桌面無該好友專屬視窗", NL.join(str(c.args[0]) for c in self.log.call_args_list))
        opener.assert_called_once()

    def test_group_never_judges_photo_or_sticker_by_cache_file_size(self):
        # 真機 (2026-11-01)：理財幼幼班_Z 傳來的照片，快取檔只有 13KB 與 7KB，被當成貼圖略過。
        # 快取檔大小和是不是照片沒有可靠的關係 (Notify Bot 的 38 張照片：最小 20KB、中位數 112KB)，一律交給視窗截圖裡
        # 「另存新檔」那一列辨識 (照片有、貼圖沒有)
        for size in (2000, 7000, 13000, 60000, 300000):
            sent, _ = self.run_detection(window_open=True, opener_result=True, group=True, size=size)
            self.assertEqual(sent, ["小明"], size)
            sent, _ = self.run_detection(window_open=True, opener_result=True, group=False, size=size)
            self.assertEqual(sent, ["小明"], size)

    def test_group_cooldown_is_much_longer_than_the_normal_one(self):
        sent, _ = self.run_detection(window_open=True, opener_result=True, group=True, last_reply_ago=120)
        self.assertEqual(sent, [])                                                          # 2 分鐘前才回過：群組還在冷卻
        sent, _ = self.run_detection(window_open=True, opener_result=True, group=True, last_reply_ago=601)
        self.assertEqual(sent, ["小明"])
        sent, _ = self.run_detection(window_open=True, opener_result=True, group=False, last_reply_ago=120)
        self.assertEqual(sent, ["小明"])                                                    # 1:1 用 cooldown_seconds，不受群組冷卻影響
        sent, _ = self.run_detection(window_open=True, opener_result=True, group=True, last_reply_ago=120,
                                     config_extra={"group_cooldown_seconds": 60})
        self.assertEqual(sent, ["小明"])

    def test_group_member_count_in_the_title_never_triggers_the_unread_path(self):
        titled = [(CHAT, "小明 (477)", "Qt", True)]
        auto_reply.LAST_PROCESSED_UNREAD["小明"] = 476                                       # 成員數 477 看起來像「未讀數增加」
        self.addCleanup(auto_reply.LAST_PROCESSED_UNREAD.pop, "小明", None)
        self.run_detection(window_open=True, opener_result=True, group=False, windows=titled)
        self.assertIn("未讀數增加", NL.join(str(c.args[0]) for c in self.log.call_args_list))      # 1:1：照舊用未讀數觸發
        self.log.reset_mock()
        auto_reply.LAST_PROCESSED_UNREAD["小明"] = 476
        sent, _ = self.run_detection(window_open=True, opener_result=True, group=True, windows=titled)
        self.assertEqual(sent, ["小明"])                                                    # 照樣由快取新圖偵測觸發
        self.assertNotIn("未讀數增加", NL.join(str(c.args[0]) for c in self.log.call_args_list))   # 但不是靠成員數

    def test_present_window_does_not_trigger_the_opener(self):
        sent, opener = self.run_detection(window_open=True, opener_result=True)
        self.assertEqual(sent, ["小明"])
        opener.assert_not_called()


class TestNotifyFallback(_Base):
    """推播額度用完時，改從使用者自己的帳號把通知送進 Notify Bot 聊天室 (視窗要已開著)"""
    PHRASE = "結束長輩圖機器人"

    def setUp(self):
        super().setUp()
        with auto_reply._FALLBACK_LOCK:
            auto_reply._FALLBACK.update({"chat": "Notify Bot", "pending": [], "last": 0.0, "window_checked": 0.0, "failures": 0, "open_tried": -1e9})
        self.addCleanup(auto_reply._FALLBACK.update, {"chat": "", "pending": [], "last": 0.0, "window_checked": 0.0, "failures": 0, "open_tried": -1e9})
        self.addCleanup(setattr, auto_reply.notifier, "fallback_sink", None)
        self.addCleanup(setattr, auto_reply.notifier, "fallback_busy", False)
        self.opener = mock.patch.object(auto_reply, "open_missing_chat_window", return_value=False).start()    # 預設開不起來 (不碰真的 LINE)
        self.addCleanup(mock.patch.stopall)

    def flush(self, window=True, foreground=CHAT, title="Notify Bot", sent=True, iconic=False, send_error=None):
        busy_during_send = []

        def fake_send(hwnd, text):
            busy_during_send.append(auto_reply.notifier.fallback_busy)
            if send_error:
                raise send_error
            self.sent_text = text
            return sent

        self.sent_text = None
        with mock.patch.object(auto_reply, "find_target_chat_window", return_value=[(CHAT, "Notify Bot", "Qt", True)] if window else []), \
             mock.patch.object(auto_reply.win32gui, "IsIconic", return_value=iconic), \
             mock.patch.object(auto_reply.win32gui, "ShowWindow") as show, \
             mock.patch.object(auto_reply, "activate_window"), \
             mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=foreground), \
             mock.patch.object(auto_reply.win32gui, "GetWindowText", return_value=title), \
             mock.patch.object(auto_reply, "send_text_via_input", side_effect=fake_send) as send, \
             mock.patch.object(auto_reply, "_minimize_if_configured") as put_back:
            auto_reply.flush_fallback_notices({"stop_command": self.PHRASE})
        return send, put_back, show, busy_during_send

    # --- 暫存 ---
    def test_notices_are_queued_when_the_fallback_is_on(self):
        auto_reply.queue_fallback_notice("[10:00:00] a")
        self.assertEqual(auto_reply._FALLBACK["pending"], ["[10:00:00] a"])

    def test_notices_are_dropped_when_the_fallback_is_off(self):
        auto_reply._FALLBACK["chat"] = ""
        auto_reply.queue_fallback_notice("a")
        self.assertEqual(auto_reply._FALLBACK["pending"], [])

    def test_the_queue_keeps_only_the_newest_notices(self):
        for n in range(auto_reply.FALLBACK_MAX_PENDING + 5):
            auto_reply.queue_fallback_notice(f"n{n}")
        pending = auto_reply._FALLBACK["pending"]
        self.assertEqual(len(pending), auto_reply.FALLBACK_MAX_PENDING)
        self.assertEqual(pending[-1], f"n{auto_reply.FALLBACK_MAX_PENDING + 4}")
        self.assertEqual(pending[0], "n5")

    def test_configure_wires_the_sink_and_an_empty_name_turns_it_off(self):
        with mock.patch.object(auto_reply.notifier, "configure"):
            auto_reply.configure_notifier({"notify_fallback_chat": "Notify Bot"})
            self.assertIs(auto_reply.notifier.fallback_sink, auto_reply.queue_fallback_notice)
            auto_reply.queue_fallback_notice("x")
            auto_reply.configure_notifier({"notify_fallback_chat": ""})
        self.assertIsNone(auto_reply.notifier.fallback_sink)
        self.assertEqual(auto_reply._FALLBACK["pending"], [])                 # 關掉就清空，不留舊通知

    # --- 合併成一則 ---
    def test_lines_are_merged_into_one_message_with_newlines_flattened(self):
        text, used = auto_reply._fallback_message(["[10:00:00] 第一行\n第二行", "[10:01:00] b"], self.PHRASE)
        self.assertEqual(used, 2)
        self.assertEqual(text, "📝[備援] [10:00:00] 第一行 ／ 第二行 ｜ [10:01:00] b")

    def test_the_stop_phrase_is_always_replaced_so_the_bot_cannot_stop_itself(self):
        line = "[10:00:00] 🟢 已啟動\n停止指令：在 Notify Bot 自己發「結束長輩圖機器人」"
        text, _ = auto_reply._fallback_message([line], self.PHRASE)
        self.assertNotIn(self.PHRASE, text)
        self.assertFalse(auto_reply.text_matches_stop_command(text, self.PHRASE))

    # 停止指令的三層防護各自單獨測 (互相掩護時，拿掉任何一層結果都一樣，測試就看不出來)
    def test_layer_one_the_literal_phrase_is_replaced_even_if_the_matcher_says_no(self):
        with mock.patch.object(auto_reply, "text_matches_stop_command", return_value=False):
            text, _ = auto_reply._fallback_message(["[10:00:00] 自己發「結束長輩圖機器人」"], self.PHRASE)
        self.assertNotIn(self.PHRASE, text)
        self.assertIn("（停止指令）", text)

    def test_layer_two_a_line_the_matcher_still_flags_is_replaced_by_itself(self):
        def flags(text, phrase):
            return "可疑" in text and "省略" not in text
        with mock.patch.object(auto_reply, "text_matches_stop_command", side_effect=flags):
            text, used = auto_reply._fallback_message(["[10:00:00] 可疑內容", "[10:00:01] 正常"], self.PHRASE)
        self.assertNotIn("可疑", text)
        self.assertIn("正常", text)                                              # 只換掉有問題的那一則
        self.assertEqual(used, 2)

    def test_layer_three_a_merged_text_the_matcher_flags_is_replaced_as_a_whole(self):
        def flags(text, phrase):
            return text.startswith("📝[備援]") and "｜" in text                    # 只有合併後的整段才被抓到
        with mock.patch.object(auto_reply, "text_matches_stop_command", side_effect=flags):
            text, used = auto_reply._fallback_message(["[10:00:00] a", "[10:00:01] b"], self.PHRASE)
        self.assertNotIn("[10:00:00] a", text)
        self.assertIn("省略", text)
        self.assertEqual(used, 2)

    def test_the_main_loop_calls_the_flush_before_each_detection_pass(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "auto_reply.py")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        loop = source[source.index("def main("):]
        self.assertIn("flush_fallback_notices(config)", loop)
        self.assertLess(loop.index("flush_fallback_notices(config)"), loop.index("check_for_incoming_messages(config)"))

    def test_a_group_chat_as_the_fallback_target_turns_the_fallback_off(self):
        auto_reply.set_group_targets(["Notify Bot"])
        self.addCleanup(auto_reply.set_group_targets, [])
        auto_reply._WARNED_ONCE.discard("fallback_group")
        self.addCleanup(auto_reply._WARNED_ONCE.discard, "fallback_group")
        with mock.patch.object(auto_reply.notifier, "configure"), mock.patch.object(auto_reply.logging, "warning") as warn:
            auto_reply.configure_notifier({"notify_fallback_chat": "Notify Bot"})
            auto_reply.configure_notifier({"notify_fallback_chat": "Notify Bot"})
        self.assertIsNone(auto_reply.notifier.fallback_sink)
        self.assertEqual(auto_reply._FALLBACK["chat"], "")
        warn.assert_called_once()                                                # 警告只印一次

    def test_notices_trimmed_away_during_a_send_do_not_remove_unsent_ones(self):
        for n in range(auto_reply.FALLBACK_MAX_PENDING):
            auto_reply.queue_fallback_notice(f"[10:00:00] n{n}")

        sent_texts = []

        def send_and_meanwhile_add_more(hwnd, text):
            sent_texts.append(text)
            for k in range(3):
                auto_reply.queue_fallback_notice(f"[10:01:00] new{k}")       # 背景執行緒在送出期間新增，從前面裁掉最舊的
            return True

        with mock.patch.object(auto_reply, "find_target_chat_window", return_value=[(CHAT, "Notify Bot", "Qt", True)]), \
             mock.patch.object(auto_reply.win32gui, "IsIconic", return_value=False), \
             mock.patch.object(auto_reply, "activate_window"), \
             mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=CHAT), \
             mock.patch.object(auto_reply.win32gui, "GetWindowText", return_value="Notify Bot"), \
             mock.patch.object(auto_reply, "send_text_via_input", side_effect=send_and_meanwhile_add_more), \
             mock.patch.object(auto_reply, "_minimize_if_configured"):
            auto_reply.flush_fallback_notices({"stop_command": self.PHRASE})
        left = auto_reply._FALLBACK["pending"]
        self.assertEqual([x for x in left if x.startswith("[10:01:00]")], [f"[10:01:00] new{k}" for k in range(3)])   # 新增的沒被誤刪
        old_left = [x for x in left if x.startswith("[10:00:00]")]
        sent_count = sent_texts[0].count(" ｜ ") + 1                             # 從實際送出的訊息算出送了幾則 (不能用剩下幾則反推)
        self.assertGreater(sent_count, 3)
        # 送出的是 n0..n(sent_count-1)，所以沒送的 n(sent_count)..n199 一則都不能少 (位置刪除會多刪掉被前面裁掉的那幾則)
        self.assertEqual(old_left, [f"[10:00:00] n{k}" for k in range(sent_count, auto_reply.FALLBACK_MAX_PENDING)])

    def test_a_notice_that_still_looks_like_the_stop_command_is_replaced_entirely(self):
        with mock.patch.object(auto_reply, "text_matches_stop_command", return_value=True):
            text, used = auto_reply._fallback_message(["[10:00:00] 隨便"], self.PHRASE)
        self.assertNotIn("隨便", text)
        self.assertEqual(used, 1)

    def test_a_long_backlog_is_split_across_messages(self):
        lines = [f"[10:00:{n % 60:02d}] " + "字" * 100 for n in range(30)]
        text, used = auto_reply._fallback_message(lines, self.PHRASE)
        self.assertLessEqual(len(text), auto_reply.FALLBACK_MAX_CHARS + 20)
        self.assertTrue(0 < used < 30)

    def test_one_oversized_line_is_still_sent_truncated(self):
        text, used = auto_reply._fallback_message(["字" * 5000], self.PHRASE)
        self.assertEqual(used, 1)
        self.assertLessEqual(len(text), auto_reply.FALLBACK_MAX_CHARS + 20)

    # --- 送進聊天室 ---
    def test_nothing_pending_means_nothing_is_sent(self):
        send, put_back, _, _ = self.flush()
        send.assert_not_called()
        put_back.assert_not_called()

    def test_pending_notices_are_sent_and_then_cleared(self):
        auto_reply.queue_fallback_notice("[10:00:00] a")
        auto_reply.queue_fallback_notice("[10:00:01] b")
        send, put_back, _, busy = self.flush()
        send.assert_called_once()
        self.assertIn("[10:00:00] a", self.sent_text)
        self.assertEqual(auto_reply._FALLBACK["pending"], [])
        put_back.assert_called_once()
        self.assertEqual(busy, [True])                                         # 送出期間產生的通知不再排進備援
        self.assertFalse(auto_reply.notifier.fallback_busy)

    def test_a_failed_send_keeps_the_notices_and_waits_for_the_next_interval(self):
        auto_reply.queue_fallback_notice("[10:00:00] a")
        self.flush(sent=False)
        self.assertEqual(auto_reply._FALLBACK["pending"], ["[10:00:00] a"])
        send, _, _, _ = self.flush(sent=True)
        send.assert_not_called()                                               # 間隔內不重試
        self.clock[0] += auto_reply.FALLBACK_MIN_INTERVAL
        send, _, _, _ = self.flush(sent=True)
        send.assert_not_called()                                               # 失敗之後間隔拉長：一般間隔過了也還不重試
        self.clock[0] += auto_reply.FALLBACK_FAILURE_INTERVAL - auto_reply.FALLBACK_MIN_INTERVAL
        send, _, _, _ = self.flush(sent=True)
        send.assert_called_once()
        self.assertEqual(auto_reply._FALLBACK["pending"], [])
        self.assertEqual(auto_reply._FALLBACK["failures"], 0)                  # 成功就歸零

    def test_only_the_notices_that_were_sent_are_removed(self):
        for n in range(30):
            auto_reply.queue_fallback_notice(f"[10:00:{n:02d}] " + "字" * 100)
        self.flush()
        left = len(auto_reply._FALLBACK["pending"])
        self.assertTrue(0 < left < 30)

    def test_a_closed_chat_window_sends_nothing_and_does_not_start_the_interval(self):
        auto_reply.queue_fallback_notice("a")
        send, put_back, _, _ = self.flush(window=False)
        send.assert_not_called()
        put_back.assert_not_called()
        self.assertEqual(auto_reply._FALLBACK["last"], 0.0)                    # 之後視窗一開就能馬上送
        self.assertEqual(auto_reply._FALLBACK["pending"], ["a"])

    def test_it_never_sends_when_another_window_is_in_front(self):
        auto_reply.queue_fallback_notice("a")
        send, put_back, _, _ = self.flush(foreground=CHAT + 1)
        send.assert_not_called()
        put_back.assert_called_once()                                          # 但視窗照常放回去
        self.assertEqual(auto_reply._FALLBACK["pending"], ["a"])

    def test_it_never_sends_when_the_window_title_is_not_the_chat(self):
        auto_reply.queue_fallback_notice("a")
        send, _, _, _ = self.flush(title="某個好友")
        send.assert_not_called()

    def test_a_minimised_window_is_restored_first(self):
        auto_reply.queue_fallback_notice("a")
        _, _, show, _ = self.flush(iconic=True)
        show.assert_called_once_with(CHAT, auto_reply.win32con.SW_RESTORE)

    def test_an_exception_while_sending_still_puts_the_window_back_and_clears_busy(self):
        auto_reply.queue_fallback_notice("a")
        _, put_back, _, _ = self.flush(send_error=RuntimeError("boom"))
        put_back.assert_called_once()
        self.assertFalse(auto_reply.notifier.fallback_busy)
        self.assertEqual(auto_reply._FALLBACK["pending"], ["a"])

    # --- 第二輪審查的修正 ---
    def test_a_closed_window_is_only_looked_for_every_30_seconds(self):
        auto_reply.queue_fallback_notice("a")
        with mock.patch.object(auto_reply, "find_target_chat_window", return_value=[]) as find:
            auto_reply.flush_fallback_notices({})
            auto_reply.flush_fallback_notices({})                              # 找視窗要掃行程與視窗列表，不必每輪都做
            self.assertEqual(find.call_count, 1)
            self.clock[0] += auto_reply.FALLBACK_WINDOW_RECHECK
            auto_reply.flush_fallback_notices({})
            self.assertEqual(find.call_count, 2)

    # --- 視窗沒開：自動開啟，並且留著 ---
    def test_a_missing_window_is_opened_automatically_and_kept_open(self):
        auto_reply.queue_fallback_notice("a")
        self.opener.return_value = True
        win = [(CHAT, "Notify Bot", "Qt", True)]
        found = iter([[], win])                                               # 第一次找沒有，開了之後找得到
        auto_reply._BOT_OPENED_HWNDS[CHAT] = (auto_reply.time.time(), "Notify Bot")     # open_missing_chat_window 會登記它
        self.addCleanup(auto_reply._BOT_OPENED_HWNDS.clear)
        with mock.patch.object(auto_reply, "find_target_chat_window", side_effect=lambda n: next(found)),              mock.patch.object(auto_reply.win32gui, "IsIconic", return_value=False),              mock.patch.object(auto_reply, "activate_window"),              mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=CHAT),              mock.patch.object(auto_reply.win32gui, "GetWindowText", return_value="Notify Bot"),              mock.patch.object(auto_reply, "send_text_via_input", return_value=True) as send,              mock.patch.object(auto_reply, "_minimize_if_configured"):
            auto_reply.flush_fallback_notices({"stop_command": self.PHRASE})
        self.opener.assert_called_once()
        send.assert_called_once()
        self.assertNotIn(CHAT, auto_reply._BOT_OPENED_HWNDS)                  # 不登記成「用完要關」：停止指令偵測也要這個視窗
        self.assertEqual(auto_reply._FALLBACK["pending"], [])

    def test_auto_open_off_never_opens_the_window(self):
        auto_reply.queue_fallback_notice("a")
        with mock.patch.object(auto_reply, "find_target_chat_window", return_value=[]):
            auto_reply.flush_fallback_notices({"auto_open_chat_window": False})
        self.opener.assert_not_called()
        self.assertEqual(auto_reply._FALLBACK["pending"], ["a"])             # 通知留著，之後視窗開了再送

    def test_a_window_that_cannot_be_opened_keeps_the_notices_and_rechecks_later(self):
        auto_reply.queue_fallback_notice("a")
        with mock.patch.object(auto_reply, "find_target_chat_window", return_value=[]):
            auto_reply.flush_fallback_notices({})
            auto_reply.flush_fallback_notices({})                              # 還沒到重找的時間：不會再開一次
        self.opener.assert_called_once()
        self.assertEqual(auto_reply._FALLBACK["pending"], ["a"])
        self.assertGreater(auto_reply._FALLBACK["window_checked"], 0)

    def test_the_final_flush_at_exit_never_opens_a_window(self):
        auto_reply.queue_fallback_notice("a")
        with mock.patch.object(auto_reply, "find_target_chat_window", return_value=[]):
            auto_reply.flush_fallback_notices({}, force=True)
        self.opener.assert_not_called()

    def test_a_failed_open_is_not_retried_before_the_retry_interval(self):
        auto_reply.queue_fallback_notice("a")
        with mock.patch.object(auto_reply, "find_target_chat_window", return_value=[]):
            auto_reply.flush_fallback_notices({})
            auto_reply._FALLBACK["window_checked"] = 0.0                      # 重找視窗的時間到了，但開窗的重試間隔還沒到
            auto_reply.flush_fallback_notices({})
        self.opener.assert_called_once()

    def test_the_final_flush_at_exit_never_opens_a_window(self):
        auto_reply.queue_fallback_notice("a")
        with mock.patch.object(auto_reply, "find_target_chat_window", return_value=[]):
            auto_reply.flush_fallback_notices({}, force=True)
        self.opener.assert_not_called()

    def test_a_failed_open_is_not_retried_before_the_retry_interval(self):
        auto_reply.queue_fallback_notice("a")
        with mock.patch.object(auto_reply, "find_target_chat_window", return_value=[]):
            auto_reply.flush_fallback_notices({})
            auto_reply._FALLBACK["window_checked"] = 0.0                      # 重找視窗的時間到了，但開窗的重試間隔還沒到
            auto_reply.flush_fallback_notices({})
        self.opener.assert_called_once()

    def test_an_already_open_window_is_never_reopened(self):
        auto_reply.queue_fallback_notice("a")
        self.flush()
        self.opener.assert_not_called()

    def test_finding_the_window_resets_the_recheck_timer(self):
        auto_reply.queue_fallback_notice("a")
        with mock.patch.object(auto_reply, "find_target_chat_window", return_value=[]):
            auto_reply.flush_fallback_notices({})
        self.assertGreater(auto_reply._FALLBACK["window_checked"], 0)
        self.clock[0] += auto_reply.FALLBACK_WINDOW_RECHECK
        self.flush()
        self.assertEqual(auto_reply._FALLBACK["window_checked"], 0.0)

    def test_after_a_failure_the_retry_is_slower_and_the_text_loses_its_emoji(self):
        auto_reply.queue_fallback_notice("[10:00:00] ⚠️ 警告 📨 內容")
        self.flush(sent=False)
        self.assertIn("📝", self.sent_text)                                    # 第一次照原樣送
        self.assertEqual(auto_reply._FALLBACK["failures"], 1)
        self.clock[0] += auto_reply.FALLBACK_FAILURE_INTERVAL
        self.flush(sent=False)
        self.assertEqual(auto_reply._FALLBACK["failures"], 2)
        for emoji in ("📝", "⚠", "📨", "️"):
            self.assertNotIn(emoji, self.sent_text)                            # 讀回比對失敗時改送沒有表情符號的版本
        self.assertIn("警告", self.sent_text)
        self.assertLess(auto_reply._FALLBACK["last"], self.clock[0] + auto_reply.FALLBACK_FAILURE_INTERVAL)

    def test_a_successful_send_goes_back_to_the_normal_text(self):
        auto_reply.queue_fallback_notice("[10:00:00] 📨 a")
        self.flush(sent=False)
        self.clock[0] += auto_reply.FALLBACK_FAILURE_INTERVAL
        self.flush(sent=True)
        self.assertEqual(auto_reply._FALLBACK["failures"], 0)
        auto_reply.queue_fallback_notice("[10:05:00] 📨 b")
        self.clock[0] += auto_reply.FALLBACK_MIN_INTERVAL
        self.flush(sent=True)
        self.assertIn("📝", self.sent_text)

    def test_emoji_between_the_characters_of_the_stop_phrase_cannot_rebuild_it(self):
        sneaky = "停止😀指令" if False else "結束😀長輩圖機器人"
        text, _ = auto_reply._fallback_message([f"[10:00:00] {sneaky}"], self.PHRASE, plain=True)
        self.assertFalse(auto_reply.text_matches_stop_command(text, self.PHRASE))
        self.assertNotIn(self.PHRASE, text)

    def test_a_short_stop_phrase_that_overlaps_the_placeholder_wording_never_survives(self):
        # 停止指令剛好是「停止指令」(4 字，合法)：括號裡的「停止指令」與「省略」說明文字本身都會中
        for phrase in ("停止指令", "備援", "字樣"):
            text, used = auto_reply._fallback_message(["[10:00:00] 內容含停止指令與備援字樣", "[10:01:00] 另一則"], phrase)
            self.assertFalse(auto_reply.text_matches_stop_command(text, phrase), (phrase, text))
            self.assertTrue(text.strip())
            self.assertEqual(used, 2)

    def test_the_replacement_text_itself_is_checked_against_the_phrase(self):
        # 停止指令剛好是「停止指令」：括號裡的「停止指令」會中，要改用不含它的替代文字，而且其餘內容保留 (不是整則被省略)
        text, _ = auto_reply._fallback_message(["[10:00:00] 內容含停止指令字樣"], "停止指令")
        self.assertIn("內容含（已省略）字樣", text)

    def test_the_omitted_wording_itself_is_checked_against_the_phrase(self):
        def flags(text, phrase):
            if text.startswith("📝[備援]"):
                return False                                                   # 讓最後的整段檢查「失明」，單獨驗證省略說明文字這一層
            return phrase in text or "可疑" in text
        with mock.patch.object(auto_reply, "text_matches_stop_command", side_effect=flags):
            text, _ = auto_reply._fallback_message(["[10:00:00] 可疑內容"], "省略")      # 「省略」同時出現在預設的省略說明裡
        self.assertNotIn("省略", text)
        self.assertNotIn("可疑", text)

    def test_the_shared_state_is_read_while_holding_the_lock(self):
        auto_reply.queue_fallback_notice("a")
        events = []

        class Spy:
            def __enter__(self_inner):
                events.append("lock")

            def __exit__(self_inner, *exc):
                return False

        with mock.patch.object(auto_reply, "_FALLBACK_LOCK", Spy()), \
             mock.patch.object(auto_reply, "find_target_chat_window", side_effect=lambda c: events.append("find") or []):
            auto_reply.flush_fallback_notices({})
        self.assertEqual(events[:2], ["lock", "find"])                         # 先在鎖裡讀 chat 與待送清單，才去找視窗

    def test_the_exit_flush_ignores_the_intervals_but_not_the_window_checks(self):
        auto_reply.queue_fallback_notice("[10:00:00] 🔴 長輩圖機器人已停止")
        auto_reply._FALLBACK["last"] = self.clock[0]                           # 剛送過：一般情況要等 5 分鐘
        auto_reply._FALLBACK["window_checked"] = self.clock[0]
        with mock.patch.object(auto_reply, "find_target_chat_window", return_value=[(CHAT, "Notify Bot", "Qt", True)]), \
             mock.patch.object(auto_reply.win32gui, "IsIconic", return_value=False), \
             mock.patch.object(auto_reply, "activate_window"), \
             mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=CHAT), \
             mock.patch.object(auto_reply.win32gui, "GetWindowText", return_value="Notify Bot"), \
             mock.patch.object(auto_reply, "send_text_via_input", return_value=True) as send, \
             mock.patch.object(auto_reply, "_minimize_if_configured"):
            auto_reply.flush_fallback_notices_at_exit({})
        send.assert_called_once()
        self.assertEqual(auto_reply._FALLBACK["pending"], [])

    def test_the_exit_flush_never_raises(self):
        auto_reply.queue_fallback_notice("a")
        with mock.patch.object(auto_reply, "find_target_chat_window", side_effect=RuntimeError("boom")):
            auto_reply.flush_fallback_notices_at_exit({})

    def test_both_exit_paths_flush_the_fallback(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "auto_reply.py")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        tail = source[source.index("except KeyboardInterrupt:"):]
        self.assertGreaterEqual(tail.count("flush_fallback_notices_at_exit(config)"), 2)       # Ctrl+C 與遠端停止

    def test_nothing_is_sent_when_the_pending_list_empties_before_the_copy(self):
        # 檢查與複製要在同一個鎖裡：清空後不能送出只有標頭的空訊息
        text, used = auto_reply._fallback_message([], self.PHRASE)
        self.assertEqual(used, 0)
        auto_reply._FALLBACK["pending"].clear()
        send, _, _, _ = self.flush()
        send.assert_not_called()

    def test_the_exception_line_never_carries_an_alert_tag(self):
        # 帶警告標籤的行會再被推播/備援，非同步送出時 fallback_busy 可能已清掉，會變成自己送自己
        auto_reply.queue_fallback_notice("a")
        self.flush(send_error=RuntimeError("boom"))
        lines = [str(c.args[0]).strip() for c in self.log.call_args_list if c.args]
        crash_lines = [x for x in lines if "boom" in x]
        self.assertTrue(crash_lines)
        self.assertFalse(any(x.startswith(auto_reply.notifier.ALERT_TAGS) for x in crash_lines))

    def test_a_group_chat_is_never_used_as_the_fallback(self):
        auto_reply.queue_fallback_notice("a")
        auto_reply.set_group_targets(["Notify Bot"])
        self.addCleanup(auto_reply.set_group_targets, [])
        send, _, _, _ = self.flush()
        send.assert_not_called()

    def test_the_setting_is_validated_and_in_the_example_config(self):
        import json
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "config.example.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["notify_fallback_chat"], "Notify Bot")
        self.assertEqual(auto_reply.DEFAULT_CONFIG["notify_fallback_chat"], "Notify Bot")


class TestChannelLabel(_Base):
    """紀錄裡的快取頻道代號 (LINE 自己產生的雜湊) 光看分不出是誰，每一行都要一起帶出聊天室名稱"""
    BOUND = "b1acf4e0" + "0" * 32
    OTHER = "3436b478" + "f" * 32

    def label(self, chat_hash, mapping=None):
        with mock.patch.object(auto_reply, "load_chat_dirs_map", return_value={self.BOUND: "林如婷"} if mapping is None else mapping):
            return auto_reply.channel_label(chat_hash)

    def test_a_bound_channel_shows_its_chat_name(self):
        self.assertEqual(self.label(self.BOUND), "「b1acf4e0...」(林如婷)")

    def test_an_unbound_channel_says_so(self):
        self.assertEqual(self.label(self.OTHER), "「3436b478...」(未綁定)")

    def test_the_hash_match_ignores_case(self):
        self.assertEqual(self.label(self.BOUND.upper()), "「B1ACF4E0...」(林如婷)")

    def test_missing_hash_or_a_broken_map_never_raises(self):
        self.assertEqual(self.label(None), "「...」(未綁定)")
        with mock.patch.object(auto_reply, "load_chat_dirs_map", side_effect=OSError("disk")):
            self.assertEqual(auto_reply.channel_label(self.OTHER), "「3436b478...」(未綁定)")

    def test_no_log_line_prints_a_bare_channel_hash(self):
        # 防止日後又寫回「快取頻道「{ch[:8]}...」」這種看不出是誰的寫法
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "auto_reply.py")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        self.assertNotIn("快取頻道「", source)
        self.assertGreaterEqual(source.count("channel_label("), 12)


class TestClickTargetCheck(_Base):
    """真機 (2026-11-01 19:34)：視窗被放到最底層，焦點對了卻被終端機蓋住，點輸入框點到了上面的終端機，焦點被搶走"""

    def click_attempt(self, tops):
        """tops: 依序回傳「輸入框座標最上面的視窗」"""
        seq = iter(tops)
        stack = contextlib.ExitStack()
        with stack:
            stack.enter_context(mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=CHAT))
            stack.enter_context(mock.patch.object(auto_reply.win32gui, "GetWindowText", return_value="小明"))
            stack.enter_context(mock.patch.object(auto_reply.win32gui, "GetWindowRect", return_value=(0, 0, 400, 600)))
            stack.enter_context(mock.patch.object(auto_reply, "get_system_dpi_scale", return_value=1.0))
            stack.enter_context(mock.patch.object(auto_reply, "_root_window_at", side_effect=lambda x, y: next(seq)))
            raised = stack.enter_context(mock.patch.object(auto_reply, "_raise_to_top"))
            click = stack.enter_context(mock.patch.object(auto_reply.pyautogui, "click"))
            ok = auto_reply.focus_chat_input(CHAT, target_name="小明")
        return ok, click, raised

    def test_it_clicks_when_the_target_is_on_top(self):
        ok, click, raised = self.click_attempt([CHAT])
        self.assertTrue(ok)
        click.assert_called_once()
        raised.assert_not_called()

    def test_a_covered_window_is_raised_and_then_clicked(self):
        ok, click, raised = self.click_attempt([CHAT + 1, CHAT])
        self.assertTrue(ok)
        raised.assert_called_once_with(CHAT)
        click.assert_called_once()

    def test_it_never_clicks_when_another_window_stays_on_top(self):
        ok, click, raised = self.click_attempt([CHAT + 1, CHAT + 1])
        self.assertFalse(ok)
        click.assert_not_called()
        self.assertIn("禁止點擊", NL.join(str(c.args[0]) for c in self.log.call_args_list))

    def test_unreadable_top_window_is_treated_as_not_ours(self):
        ok, click, _ = self.click_attempt([None, None])
        self.assertFalse(ok)
        click.assert_not_called()

    def test_raise_to_top_uses_hwnd_top_without_activating(self):
        with mock.patch.object(auto_reply.win32gui, "SetWindowPos") as pos:
            auto_reply._raise_to_top(CHAT)
        flags = auto_reply.win32con.SWP_NOMOVE | auto_reply.win32con.SWP_NOSIZE | auto_reply.win32con.SWP_NOACTIVATE
        pos.assert_called_once_with(CHAT, auto_reply.win32con.HWND_TOP, 0, 0, 0, 0, flags)

    def test_raise_to_top_never_raises(self):
        with mock.patch.object(auto_reply.win32gui, "SetWindowPos", side_effect=RuntimeError("boom")):
            auto_reply._raise_to_top(CHAT)

    def test_activating_a_window_also_raises_it(self):
        with mock.patch.object(auto_reply.win32gui, "IsIconic", return_value=False), \
             mock.patch.object(auto_reply.win32gui, "ShowWindow"), \
             mock.patch.object(auto_reply.win32gui, "SetForegroundWindow"), \
             mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=CHAT), \
             mock.patch.object(auto_reply.pyautogui, "press"), \
             mock.patch.object(auto_reply, "_raise_to_top") as raised:
            self.assertTrue(auto_reply.activate_window(CHAT))
        raised.assert_called_once_with(CHAT)

    def test_root_window_lookup_never_raises(self):
        with mock.patch.object(auto_reply.ctypes, "windll", mock.Mock(user32=mock.Mock(WindowFromPoint=mock.Mock(side_effect=OSError("x"))))):
            self.assertIsNone(auto_reply._root_window_at(1, 1))


class TestForceForeground(_Base):
    """真機 (2026-11-01)：展開、放在最底層的視窗，使用者正在別的視窗打字時一般的 SetForegroundWindow 會被前景鎖擋下"""

    def run_activate(self, fg_after_normal, fg_after_attach=None):
        fg = {"v": CHAT + 1}
        calls = []
        sets = {"n": 0}

        def set_fg(h):
            sets["n"] += 1
            calls.append("set")
            fg["v"] = fg_after_normal if sets["n"] == 1 else (fg_after_attach if fg_after_attach is not None else fg["v"])

        def show(h, cmd):
            calls.append(cmd)
            if cmd == auto_reply.win32con.SW_RESTORE and "min" in calls:
                fg["v"] = CHAT

        def show_wrapper(h, cmd):
            if cmd == auto_reply.win32con.SW_MINIMIZE:
                calls.append("min")
                return
            show(h, cmd)

        user32 = mock.Mock()
        user32.AttachThreadInput.return_value = 1
        with mock.patch.object(auto_reply.win32gui, "IsIconic", return_value=False), \
             mock.patch.object(auto_reply.win32gui, "ShowWindow", side_effect=show_wrapper), \
             mock.patch.object(auto_reply.win32gui, "SetForegroundWindow", side_effect=set_fg), \
             mock.patch.object(auto_reply.win32gui, "BringWindowToTop"), \
             mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", side_effect=lambda: fg["v"]), \
             mock.patch.object(auto_reply.win32process, "GetWindowThreadProcessId", return_value=(111, 222)), \
             mock.patch.object(auto_reply.ctypes, "windll", mock.Mock(user32=user32, kernel32=mock.Mock(GetCurrentThreadId=lambda: 999))), \
             mock.patch.object(auto_reply.pyautogui, "press"):
            result = auto_reply.activate_window(CHAT)
        return result, calls, user32, fg["v"]

    def test_normal_switch_needs_no_fallback(self):
        _, calls, user32, fg = self.run_activate(fg_after_normal=CHAT)
        self.assertEqual(fg, CHAT)
        self.assertEqual(calls.count("set"), 1)
        user32.AttachThreadInput.assert_not_called()

    def test_input_thread_attach_is_the_first_fallback_and_is_always_detached(self):
        _, calls, user32, fg = self.run_activate(fg_after_normal=CHAT + 1, fg_after_attach=CHAT)
        self.assertEqual(fg, CHAT)
        self.assertNotIn("min", calls)
        self.assertEqual([c.args[2] for c in user32.AttachThreadInput.call_args_list], [True, False])

    def test_minimise_and_restore_is_the_last_resort(self):
        _, calls, user32, fg = self.run_activate(fg_after_normal=CHAT + 1, fg_after_attach=CHAT + 1)
        self.assertIn("min", calls)
        self.assertEqual(fg, CHAT)                                              # 還原一定會啟用視窗
        self.assertEqual([c.args[2] for c in user32.AttachThreadInput.call_args_list], [True, False])

    def test_it_never_raises(self):
        with mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", side_effect=RuntimeError("boom")):
            self.assertFalse(auto_reply._force_foreground(CHAT))


class TestExpandedWindowSignature(_Base):
    """真機 (2026-11-01)：列表已滿時新訊息把舊的擠出去，項目數與最底部位置不變，簽章要靠 runtime ID 才看得出新訊息"""

    class FakeItem:
        def __init__(self, rect, rid):
            self.BoundingRectangle = type("R", (), dict(left=rect[0], top=rect[1], right=rect[2], bottom=rect[3]))()
            self._rid = rid

        def GetRuntimeId(self):
            if isinstance(self._rid, Exception):
                raise self._rid
            return self._rid

    def signature(self, items):
        import sys
        import types
        view = mock.Mock()
        view.Exists.return_value = True
        view.GetChildren.return_value = items
        root = mock.Mock()
        root.ListControl.return_value = view
        fake = types.SimpleNamespace(ControlFromHandle=lambda h: root)
        with mock.patch.dict(sys.modules, {"uiautomation": fake}), \
             mock.patch.object(auto_reply.win32gui, "IsIconic", return_value=False), \
             mock.patch.object(auto_reply.win32gui, "IsWindowVisible", return_value=True):
            return auto_reply._expanded_window_signature(CHAT)

    def items(self, ids):
        return [self.FakeItem((0, 100 * i, 500, 100 * i + 90), rid) for i, rid in enumerate(ids)]

    def test_a_new_message_in_a_full_list_changes_the_signature_even_if_count_and_positions_stay_the_same(self):
        before = self.signature(self.items([(1,), (2,), (3,)]))
        after = self.signature(self.items([(2,), (3,), (4,)]))                  # 舊的擠出去、新的進來：數量與位置都一樣
        self.assertNotEqual(before, after)

    def test_the_same_list_gives_the_same_signature(self):
        self.assertEqual(self.signature(self.items([(1,), (2,)])), self.signature(self.items([(1,), (2,)])))

    def test_a_missing_runtime_id_does_not_break_the_signature(self):
        sig = self.signature(self.items([RuntimeError("no id"), RuntimeError("no id")]))
        self.assertIsNotNone(sig)

    def test_a_single_item_list_works(self):
        self.assertIsNotNone(self.signature(self.items([(1,)])))

    def test_empty_list_or_missing_view_is_none(self):
        self.assertIsNone(self.signature([]))


class TestTextWindowWatch(_Base):
    """真機 (2026-11-01)：一對一獨立視窗不論縮小或展開標題都沒有未讀數，文字只能靠展開視窗的訊息列表變化偵測"""
    WINDOWS = [(CHAT, "小明", "Qt", True)]

    def watch(self, sig, foreground=0, target="小明", config=None, windows=None, covered=False):
        with mock.patch.object(auto_reply, "_expanded_window_signature", return_value=sig), \
             mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=foreground), \
             mock.patch.object(auto_reply.win32gui, "GetWindowRect", return_value=(0, 0, 400, 600)), \
             mock.patch.object(auto_reply, "_root_window_at", return_value=CHAT + 1 if covered else CHAT):
            return auto_reply.text_window_changed(self.WINDOWS if windows is None else windows, target, config or {})

    def test_first_look_only_records_the_baseline(self):
        self.assertFalse(self.watch((3, "a", "b")))
        self.assertEqual(auto_reply._TEXT_SEEN_SIG["小明"], (3, "a", "b"))

    def test_a_changed_list_in_the_background_triggers_but_keeps_the_old_baseline(self):
        self.watch((3, "a", "b"))
        self.assertTrue(self.watch((4, "c", "a")))
        self.assertEqual(auto_reply._TEXT_SEEN_SIG["小明"], (3, "a", "b"))      # 沒處理完前不更新：失敗還能再試
        self.assertIn(CHAT, auto_reply._BACKGROUND_WINDOWS)

    def test_an_unchanged_list_never_triggers(self):
        self.watch((3, "a", "b"))
        self.assertFalse(self.watch((3, "a", "b")))

    def test_a_window_in_front_updates_the_baseline_without_triggering(self):
        self.watch((3, "a", "b"))
        self.assertFalse(self.watch((4, "c", "a"), foreground=CHAT))            # 你正在看或在捲動
        self.assertEqual(auto_reply._TEXT_SEEN_SIG["小明"], (4, "c", "a"))
        self.assertFalse(self.watch((4, "c", "a")))

    def test_a_foreground_window_that_is_covered_does_not_count_as_being_viewed(self):
        # 回覆完視窗被放到最底層但仍是前景視窗：你看不到它，之後的新文字不能被當成「你正在看」而漏掉
        self.watch((3, "a", "b"))
        self.assertTrue(self.watch((4, "c", "a"), foreground=CHAT, covered=True))

    def test_an_unreadable_window_position_counts_as_being_viewed(self):
        self.watch((3, "a", "b"))
        with mock.patch.object(auto_reply, "_expanded_window_signature", return_value=(4, "c", "a")), \
             mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=CHAT), \
             mock.patch.object(auto_reply.win32gui, "GetWindowRect", side_effect=OSError("gone")):
            self.assertFalse(auto_reply.text_window_changed(self.WINDOWS, "小明", {}))

    def test_triggers_are_spaced_by_the_minimum_interval(self):
        self.watch((3, "a", "b"))
        self.assertTrue(self.watch((4, "c", "a")))
        self.assertFalse(self.watch((5, "d", "c")))                              # 5 秒內不再觸發
        self.clock[0] += auto_reply.TEXT_WATCH_MIN_INTERVAL
        self.assertTrue(self.watch((5, "d", "c")))

    def test_a_minimised_or_missing_window_clears_the_baseline(self):
        self.watch((3, "a", "b"))
        self.assertFalse(self.watch(None))                                       # 縮小 (簽章讀不到)
        self.assertNotIn("小明", auto_reply._TEXT_SEEN_SIG)
        self.assertFalse(self.watch((4, "c", "a")))                              # 展開後重新建立基準，不把舊訊息當新的
        self.assertFalse(self.watch((4, "c", "a"), windows=[]))                  # 視窗沒開

    def test_stop_command_chats_are_never_watched(self):
        cfg = {"stop_command_chats": ["小明"]}
        self.watch((3, "a", "b"), config=cfg)
        self.assertFalse(self.watch((4, "c", "a"), config=cfg))                  # 官方帳號的推播會讓它一直變
        self.assertNotIn("小明", auto_reply._TEXT_SEEN_SIG)

    def test_rebaseline_records_the_current_signature_or_clears_it(self):
        with mock.patch.object(auto_reply, "get_all_line_windows", return_value=self.WINDOWS), \
             mock.patch.object(auto_reply, "_expanded_window_signature", return_value=(9, "x", "y")):
            auto_reply.text_window_rebaseline("小明")
        self.assertEqual(auto_reply._TEXT_SEEN_SIG["小明"], (9, "x", "y"))
        with mock.patch.object(auto_reply, "get_all_line_windows", return_value=self.WINDOWS), \
             mock.patch.object(auto_reply, "_expanded_window_signature", return_value=None):
            auto_reply.text_window_rebaseline("小明")
        self.assertNotIn("小明", auto_reply._TEXT_SEEN_SIG)

    def test_watched_windows_go_to_the_back_instead_of_being_minimised(self):
        auto_reply._BACKGROUND_WINDOWS.add(CHAT)
        with mock.patch.object(auto_reply, "_send_to_background") as back, mock.patch.object(auto_reply.win32gui, "ShowWindow") as show:
            auto_reply._minimize_if_configured(CHAT, {"close_window_after_send": True})
        back.assert_called_once_with(CHAT)
        show.assert_not_called()

    def test_other_windows_are_still_minimised(self):
        with mock.patch.object(auto_reply, "_send_to_background") as back, mock.patch.object(auto_reply.win32gui, "ShowWindow") as show:
            auto_reply._minimize_if_configured(CHAT, {"close_window_after_send": True})
        show.assert_called_once_with(CHAT, auto_reply.win32con.SW_MINIMIZE)
        back.assert_not_called()

    def test_with_close_window_after_send_off_nothing_is_moved(self):
        auto_reply._BACKGROUND_WINDOWS.add(CHAT)
        with mock.patch.object(auto_reply, "_send_to_background") as back, mock.patch.object(auto_reply.win32gui, "ShowWindow") as show:
            auto_reply._minimize_if_configured(CHAT, {"close_window_after_send": False})
        back.assert_not_called()
        show.assert_not_called()

    def test_send_to_background_puts_the_window_at_the_bottom_without_activating(self):
        with mock.patch.object(auto_reply.win32gui, "SetWindowPos") as pos:
            auto_reply._send_to_background(CHAT)
        flags = auto_reply.win32con.SWP_NOMOVE | auto_reply.win32con.SWP_NOSIZE | auto_reply.win32con.SWP_NOACTIVATE
        pos.assert_called_once_with(CHAT, auto_reply.win32con.HWND_BOTTOM, 0, 0, 0, 0, flags)


class TestTextWindowWatchInTheLoop(_Base):
    """偵測階段：標題沒有未讀數，但展開視窗的列表變了 -> 文字觸發"""

    def run_watch(self, changed=True, rebaseline_expected=None, send_result="SKIPPED_TEXT", extra=None, group=False, last_reply_ago=None):
        sent = []
        cfg = {"targets": ["小明"], "trigger_mode": "photo_and_sticker", "cooldown_seconds": 30, "min_delay_seconds": 0, "max_delay_seconds": 0}
        cfg.update(extra or {})
        windows = [(CHAT, "小明", "Qt", True)]
        cache = os.path.join(self.tmp.name, "cache")
        os.makedirs(cache, exist_ok=True)

        def fake_send(target, config, **kw):
            sent.append((target, kw))
            return send_result

        with mock.patch.object(auto_reply, "CHAT_MAP_PATH", os.path.join(self.tmp.name, "chat_dirs.json")), \
             mock.patch.object(auto_reply, "get_active_line_cache_dir", return_value=cache), \
             mock.patch.object(auto_reply, "get_all_line_windows", return_value=windows), \
             mock.patch.object(auto_reply, "find_target_chat_window", return_value=windows), \
             mock.patch.object(auto_reply, "update_target_window_fingerprints"), mock.patch.object(auto_reply, "check_stop_command"), \
             mock.patch.object(auto_reply, "save_circuit_breaker_state"), mock.patch.object(auto_reply, "check_circuit_breaker", return_value=True), \
             mock.patch.object(auto_reply, "text_window_changed", return_value=changed) as watch, \
             mock.patch.object(auto_reply, "text_window_rebaseline") as rebase, \
             mock.patch.object(auto_reply, "send_grandma_image", side_effect=fake_send):
            auto_reply.last_global_reply_time = 0.0
            for d in (auto_reply.LAST_SENT_TIME_BY_TARGET, auto_reply.PENDING_RETRY_MEDIA, auto_reply.SEND_FAIL_COUNTS, auto_reply.last_reply_times):
                d.pop("小明", None)
            if last_reply_ago is not None:
                auto_reply.last_reply_times["小明"] = self.clock[0] - last_reply_ago
            auto_reply.LAST_PROCESSED_UNREAD["小明"] = 0
            auto_reply.PROCESSED_CACHE_FILES.clear()
            auto_reply.last_seen_cache_mtime = self.clock[0] + 1000
            auto_reply.set_group_targets(["小明"] if group else [])
            try:
                auto_reply.check_for_incoming_messages(cfg)
            finally:
                auto_reply.set_group_targets([])
        self.addCleanup(auto_reply.LAST_PROCESSED_UNREAD.pop, "小明", None)
        self.addCleanup(auto_reply.last_reply_times.pop, "小明", None)
        return sent, watch, rebase

    def test_a_changed_list_triggers_a_text_check(self):
        sent, _, _ = self.run_watch()
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0][1]["text_trigger"])
        self.assertIn("訊息列表有變化", NL.join(str(c.args[0]) for c in self.log.call_args_list))

    def test_an_unchanged_list_does_nothing(self):
        sent, _, rebase = self.run_watch(changed=False)
        self.assertEqual(sent, [])
        rebase.assert_not_called()

    def test_the_baseline_is_reset_after_the_check(self):
        for result in ("SKIPPED_TEXT", "SENT_CONFIRMED"):
            _, _, rebase = self.run_watch(send_result=result)
            rebase.assert_called_once_with("小明")

    def test_a_failed_send_keeps_the_old_baseline_so_it_is_retried(self):
        _, _, rebase = self.run_watch(send_result="NOT_SENT")
        rebase.assert_not_called()

    def test_groups_are_never_watched(self):
        sent, watch, _ = self.run_watch(group=True)
        self.assertEqual(sent, [])
        watch.assert_not_called()

    def test_a_change_inside_the_per_chat_cooldown_does_not_fire(self):
        sent, _, _ = self.run_watch(last_reply_ago=10)                          # 冷卻 30 秒，10 秒前剛回過
        self.assertEqual(sent, [])
        sent, _, _ = self.run_watch(last_reply_ago=40)
        self.assertEqual(len(sent), 1)

    def test_photo_only_mode_does_not_watch_the_list_for_stickers(self):
        # 審查：photo_only (只回照片) 時只開貼圖設定也不用看訊息列表，否則每次列表變化都白跑一次
        sent, watch, _ = self.run_watch(extra={"trigger_mode": "photo_only", "reply_to_text": False, "birthday_reply": "", "reply_to_sticker_text": True})
        self.assertEqual(sent, [])
        watch.assert_not_called()

    def test_the_sticker_option_alone_keeps_the_watching_on(self):
        # 貼圖不會寫進快取，只能靠這條「視窗列表有變化」的流程偵測：只開 reply_to_sticker_text 也要看
        sent, watch, _ = self.run_watch(extra={"reply_to_text": False, "birthday_reply": ""})
        self.assertEqual(len(sent), 1)
        watch.assert_called()

    def test_switch_off_means_no_watching(self):
        sent, watch, _ = self.run_watch(extra={"reply_to_text": False, "birthday_reply": "", "reply_to_sticker_text": False})
        self.assertEqual(sent, [])
        watch.assert_not_called()


class TestTextTrigger(_Base):
    """偵測階段：1:1 視窗標題未讀數增加、沒有新的圖片快取 -> 文字觸發 (文字訊息沒有快取檔)"""

    def run_detection(self, config_extra=None, title="小明 (2)", group=False, baseline=1, last_reply_ago=None):
        sent = []
        cfg = {"targets": ["小明"], "trigger_mode": "photo_and_sticker", "cooldown_seconds": 30, "min_delay_seconds": 0, "max_delay_seconds": 0}
        cfg.update(config_extra or {})
        windows = [(CHAT, title, "Qt", True)]
        cache = os.path.join(self.tmp.name, "cache")
        os.makedirs(cache, exist_ok=True)

        def fake_send(target, config, **kw):
            sent.append((target, kw))
            return "SKIPPED_TEXT"

        with mock.patch.object(auto_reply, "CHAT_MAP_PATH", os.path.join(self.tmp.name, "chat_dirs.json")), \
             mock.patch.object(auto_reply, "get_active_line_cache_dir", return_value=cache), \
             mock.patch.object(auto_reply, "get_all_line_windows", return_value=windows), \
             mock.patch.object(auto_reply, "find_target_chat_window", return_value=windows), \
             mock.patch.object(auto_reply, "update_target_window_fingerprints"), mock.patch.object(auto_reply, "check_stop_command"), \
             mock.patch.object(auto_reply, "save_circuit_breaker_state"), mock.patch.object(auto_reply, "check_circuit_breaker", return_value=True), \
             mock.patch.object(auto_reply, "send_grandma_image", side_effect=fake_send):
            auto_reply.last_global_reply_time = 0.0
            auto_reply.LAST_SENT_TIME_BY_TARGET.pop("小明", None)
            auto_reply.PENDING_RETRY_MEDIA.pop("小明", None)
            auto_reply.SEND_FAIL_COUNTS.pop("小明", None)
            auto_reply.last_reply_times.pop("小明", None)
            if last_reply_ago is not None:
                auto_reply.last_reply_times["小明"] = self.clock[0] - last_reply_ago
            auto_reply.LAST_PROCESSED_UNREAD["小明"] = baseline
            auto_reply.PROCESSED_CACHE_FILES.clear()
            auto_reply.last_seen_cache_mtime = self.clock[0] + 1000
            auto_reply.set_group_targets(["小明"] if group else [])
            try:
                auto_reply.check_for_incoming_messages(cfg)
            finally:
                auto_reply.set_group_targets([])
        self.addCleanup(auto_reply.LAST_PROCESSED_UNREAD.pop, "小明", None)
        self.addCleanup(auto_reply.last_reply_times.pop, "小明", None)
        return sent

    def test_unread_increase_without_media_triggers_a_text_check(self):
        sent = self.run_detection()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "小明")
        self.assertTrue(sent[0][1]["text_trigger"])
        self.assertIsNone(sent[0][1]["triggered_file"])
        self.assertIn("文字訊息觸發", NL.join(str(c.args[0]) for c in self.log.call_args_list))

    def test_no_unread_increase_means_no_trigger(self):
        self.assertEqual(self.run_detection(baseline=2), [])                                    # 未讀數沒變
        self.assertEqual(self.run_detection(title="小明"), [])                                  # 沒有未讀數

    def test_switch_off_keeps_the_old_silence_and_advances_the_baseline(self):
        self.assertEqual(self.run_detection(config_extra={"reply_to_text": False, "birthday_reply": "", "reply_to_sticker_text": False}), [])
        self.assertEqual(auto_reply.LAST_PROCESSED_UNREAD["小明"], 2)                           # 同一則文字不會一直被看

    def test_photo_only_mode_with_only_the_sticker_option_never_triggers_a_text_check(self):
        extra = {"trigger_mode": "photo_only", "reply_to_text": False, "birthday_reply": "", "reply_to_sticker_text": True}
        self.assertEqual(self.run_detection(config_extra=extra), [])

    def test_the_sticker_option_alone_is_enough_to_trigger_a_text_check(self):
        self.assertEqual(len(self.run_detection(config_extra={"reply_to_text": False, "birthday_reply": ""})), 1)
        self.assertEqual(self.run_detection(config_extra={"reply_to_text": False, "birthday_reply": "", "reply_to_sticker_text": False}), [])

    def test_birthday_thanks_alone_is_enough_to_trigger_a_text_check(self):
        self.assertEqual(len(self.run_detection(config_extra={"reply_to_text": False, "birthday_reply": "謝謝"})), 1)
        self.assertEqual(self.run_detection(config_extra={"reply_to_text": False, "birthday_reply": "", "reply_to_sticker_text": False}), [])

    def test_without_grandma_recognition_there_is_no_kind_to_match_so_no_text_trigger(self):
        self.assertEqual(self.run_detection(config_extra={"require_grandma_image": False}), [])

    def test_cooldown_applies_to_text_triggers_too(self):
        self.assertEqual(self.run_detection(last_reply_ago=10), [])
        self.assertEqual(len(self.run_detection(last_reply_ago=31)), 1)

    def test_groups_are_never_triggered_by_their_member_count(self):
        self.assertEqual(self.run_detection(title="小明 (477)", group=True, baseline=476), [])

    def test_trigger_mode_all_keeps_its_own_text_path(self):
        sent = self.run_detection(config_extra={"trigger_mode": "all"})
        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0][1]["text_trigger"])                                            # all 模式：照舊，文字一律回 (不看內容)


class TestConfigTextKeys(_Base):
    def load(self, cfg):
        path = os.path.join(self.tmp.name, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        with mock.patch.object(auto_reply, "CONFIG_PATH", path):
            return auto_reply.load_config()

    def test_defaults_and_sanitising(self):
        cfg = self.load({"targets": ["a"]})
        self.assertEqual((cfg["reply_to_text"], cfg["text_reply_max_chars"]), (True, 30))
        cfg = self.load({"targets": ["a"], "reply_to_text": False, "text_reply_max_chars": "12"})
        self.assertEqual((cfg["reply_to_text"], cfg["text_reply_max_chars"]), (False, 12))
        cfg = self.load({"targets": ["a"], "reply_to_text": "no", "text_reply_max_chars": 0})
        self.assertEqual((cfg["reply_to_text"], cfg["text_reply_max_chars"]), (True, 1))        # 不是布林：沿用預設；字數最少 1
        cfg = self.load({"targets": ["a"], "text_reply_max_chars": "many"})
        self.assertEqual(cfg["text_reply_max_chars"], 30)

    def test_birthday_reply_setting(self):
        self.assertEqual(self.load({"targets": ["a"]})["birthday_reply"], "謝謝")
        self.assertEqual(self.load({"targets": ["a"], "birthday_reply": "  謝謝你  "})["birthday_reply"], "謝謝你")
        self.assertEqual(self.load({"targets": ["a"], "birthday_reply": ""})["birthday_reply"], "")           # 空字串 = 關閉
        self.assertEqual(self.load({"targets": ["a"], "birthday_reply": 5})["birthday_reply"], "謝謝")          # 不是字串：沿用預設
        long = self.load({"targets": ["a"], "birthday_reply": "謝" * 50})["birthday_reply"]
        self.assertEqual(len(long), auto_reply.BIRTHDAY_REPLY_MAX_CHARS)                                    # 這是要打進輸入框的一句話


class TestConfigKeys(_Base):
    def test_example_config_lists_every_setting_and_loads_cleanly(self):
        # 範本漏掉新設定項時，使用者複製範本就看不到那些選項
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.example.json")
        with open(path, encoding="utf-8") as f:
            example = json.load(f)
        obsolete = {"auto_neutral_images"}                        # 已被新的挑圖規則取代的舊選項
        self.assertEqual(sorted(set(auto_reply.DEFAULT_CONFIG) - obsolete - set(example)), [])
        self.assertEqual(sorted(set(example) - set(auto_reply.DEFAULT_CONFIG)), [])        # 範本也不能有程式不認得的設定項
        loaded = self.load(example)
        for key in set(auto_reply.DEFAULT_CONFIG) - obsolete - {"targets"}:
            self.assertEqual(loaded[key], example[key], key)                                # 範本的值都能被原樣接受

    def load(self, data):
        path = os.path.join(self.tmp.name, "cfg.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        with mock.patch.object(auto_reply, "CONFIG_PATH", path):
            return auto_reply.load_config()

    def test_defaults(self):
        cfg = self.load({"targets": ["a"]})
        self.assertEqual((cfg["max_replies_per_day"], cfg["max_image_pool"], cfg["instant_crawl_max_downloads"]), (3, 120, 40))

    def test_valid_values_and_lower_bounds(self):
        cfg = self.load({"targets": ["a"], "max_replies_per_day": "5", "max_image_pool": 0, "instant_crawl_max_downloads": -3})
        self.assertEqual((cfg["max_replies_per_day"], cfg["max_image_pool"], cfg["instant_crawl_max_downloads"]), (5, 1, 1))
        self.assertEqual(self.load({"targets": ["a"], "max_replies_per_day": 0})["max_replies_per_day"], 0)       # 0 = 不限次數
        self.assertEqual(self.load({"targets": ["a"], "max_replies_per_day": -2})["max_replies_per_day"], 0)

    def test_bad_types_keep_defaults(self):
        cfg = self.load({"targets": ["a"], "max_replies_per_day": "many", "max_image_pool": None})
        self.assertEqual((cfg["max_replies_per_day"], cfg["max_image_pool"]), (3, 120))


if __name__ == "__main__":
    unittest.main()
