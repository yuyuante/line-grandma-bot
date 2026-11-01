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


class TestRecognizeIncomingText(_Base):
    def run_text(self, ocr, front=True, crop="ok", max_chars=30, iconic=False):
        from PIL import Image
        self.ocr_mock = mock.Mock(side_effect=ocr if isinstance(ocr, list) else [ocr] * 4)
        image = Image.new("RGB", (300, 60), (230, 230, 230)) if crop == "ok" else None
        patches = [
            mock.patch.object(auto_reply, "_incoming_text_crop", return_value=image),
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
        with mock.patch.object(auto_reply, "last_left_message_is_media", side_effect=results) as m:
            return auto_reply._media_type_with_retry(CHAT, expect_media=expect_media), m.call_count

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
                 send_text=True):
        mocks = {}
        minimized = []
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
        result, m, _ = self.text_flow(text_result=(True, BIRTHDAY, "生日快樂"), config={"reply_to_text": False})
        self.assert_thanks_only(result, m)
        result, m, _ = self.text_flow(text_result=(True, MORNING, "早安"), config={"reply_to_text": False})
        self.assertEqual(result, "SKIPPED_TEXT")
        m["attach"].assert_not_called()
        m["send_text"].assert_not_called()

    def test_text_flow_is_not_entered_when_both_text_replies_and_birthday_thanks_are_off(self):
        result, m, _ = self.text_flow(config={"reply_to_text": False, "birthday_reply": ""}, recognize=[(False, None, "")])
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

    def test_text_trigger_needs_no_bound_channel(self):
        # 對方只傳文字、從沒傳過圖：chat_dirs.json 沒有他，文字照樣能回
        result, m, _ = self.text_flow()
        self.assertEqual(result, "SENT_CONFIRMED")
        self.assertEqual(m["chat_map"].return_value, {})

    def test_switch_off_and_requirements_keep_the_old_behaviour(self):
        result, m, _ = self.text_flow(config={"reply_to_text": False, "birthday_reply": ""}, recognize=[(False, None, "")])
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


class TestDetectionReopensTheWindow(_Base):
    """偵測階段：頻道已綁定、但好友的獨立視窗不見了，不再直接丟掉事件，先試著把視窗開回來"""

    def run_detection(self, window_open, opener_result, config_extra=None, group=False, photo=True, last_reply_ago=None, windows=(), size=300000):
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
        self.assertEqual(self.run_detection(config_extra={"reply_to_text": False, "birthday_reply": ""}), [])
        self.assertEqual(auto_reply.LAST_PROCESSED_UNREAD["小明"], 2)                           # 同一則文字不會一直被看

    def test_birthday_thanks_alone_is_enough_to_trigger_a_text_check(self):
        self.assertEqual(len(self.run_detection(config_extra={"reply_to_text": False, "birthday_reply": "謝謝"})), 1)
        self.assertEqual(self.run_detection(config_extra={"reply_to_text": False, "birthday_reply": ""}), [])

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
