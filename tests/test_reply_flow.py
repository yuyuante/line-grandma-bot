import contextlib
import json
import os
import sys
import tempfile
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
    def run_shot(self, crop_result, text="", ocr_error=None):
        from PIL import Image
        self.crop_text = text
        crop = (Image.new("RGB", (300, 200), (255, 255, 255)), crop_result) if crop_result is not None else None
        with mock.patch.object(auto_reply, "_incoming_media_crop", return_value=crop):
            if ocr_error:
                with mock.patch.object(auto_reply, "ocr_text", side_effect=ocr_error):
                    return auto_reply._recognize_chat_screenshot(CHAT)
            return auto_reply._recognize_chat_screenshot(CHAT)

    def test_cases(self):
        self.assertEqual(self.run_shot(300, "中秋節快樂 闔家團圓"), (True, MID_AUTUMN, "中秋節快樂闔家團圓"))
        self.assertEqual(self.run_shot(300, "FORMOSA!"), (False, None, ""))               # 讀得到字但辨識不出性質：捨棄
        self.assertEqual(self.run_shot(300, ""), (False, None, ""))                         # 一般照片，沒有字
        self.ocr_calls.clear()
        self.assertEqual(self.run_shot(150, "早安"), (False, None, ""))                    # 內容太窄：貼圖
        self.assertEqual(self.ocr_calls, [])                                               # 貼圖不做 OCR
        self.assertEqual(self.run_shot(None), (None, None, ""))                            # 暫時截不到
        self.assertEqual(self.run_shot(300, ocr_error=RuntimeError("down")), (None, None, ""))   # OCR 暫時不能用：等下次


class TestRecognizeScreenshotWaitsForLoading(_Base):
    def shots(self, widths, text="中秋節快樂"):
        from PIL import Image
        self.crop_text = text
        crop = Image.new("RGB", (300, 200), (255, 255, 255))
        results = iter([None if w is None else (crop, w) for w in widths])
        last = {"v": None}

        def next_shot(hwnd):
            try:
                last["v"] = next(results)
            except StopIteration:
                pass
            return last["v"]

        with mock.patch.object(auto_reply, "_incoming_media_crop", side_effect=next_shot) as m:
            result = auto_reply._recognize_chat_screenshot(CHAT)
        return result, m.call_count

    def test_waits_for_the_grey_placeholder_to_become_the_image(self):
        result, calls = self.shots([60, 60, 60, 300])                                       # 先是灰色小方塊，三秒後圖載入完成
        self.assertEqual(result, (True, MID_AUTUMN, "中秋節快樂"))
        self.assertEqual((calls, self.sleeps), (4, [1.0, 1.0, 1.0]))

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
                 triggered=True, slots=None):
        mocks = {}
        minimized = []
        patches = {
            "recognize": mock.patch.object(auto_reply, "recognize_incoming_image", side_effect=list(recognize)),
            "choose": mock.patch.object(auto_reply, "choose_reply_image", side_effect=choose if choose is not None else [self.image]),
            "attach": mock.patch.object(auto_reply, "send_image_via_attach_dialog", return_value=attach),
            "slot": mock.patch.object(auto_reply, "current_time_slot", side_effect=slots or (lambda: self.slot)),
            "contact_received": mock.patch.object(auto_reply, "record_contact_received"),
            "find": mock.patch.object(auto_reply, "find_target_chat_window", return_value=[(CHAT, "小明", "Qt", True)]),
            "harvest": mock.patch.object(auto_reply, "harvest_line_incoming_images"),
            "get_images": mock.patch.object(auto_reply, "get_images", return_value=[]),
            "countdown": mock.patch.object(auto_reply, "countdown_sleep"),
            "iconic": mock.patch.object(auto_reply.win32gui, "IsIconic", return_value=False),
            "fg": mock.patch.object(auto_reply.win32gui, "GetForegroundWindow", return_value=CHAT),
            "title": mock.patch.object(auto_reply.win32gui, "GetWindowText", return_value="小明"),
            "activate": mock.patch.object(auto_reply, "activate_window"),
            "focus": mock.patch.object(auto_reply, "focus_chat_input", return_value=True),
            "self": mock.patch.object(auto_reply, "is_last_message_from_self_in_chat", return_value=False),
            "media": mock.patch.object(auto_reply, "_media_type_with_retry", return_value=True),
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
            result = auto_reply.send_grandma_image("小明", cfg, triggered_file=self.incoming if triggered else None)
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
            auto_reply.warm_up_image_library({"images_dir": self.dir})
        self.assertTrue(any("可用 2 張" in l and "節日 1" in l and "時段問候 1" in l for l in logs), logs)   # 辨識不出的那張不算

    def test_warm_up_failure_is_reported_not_raised(self):
        logs = []

        class Inline:
            def __init__(self, target, name=None, daemon=None):
                self.target = target

            def start(self):
                self.target()

        with mock.patch.object(auto_reply, "log_v", side_effect=logs.append),              mock.patch.object(auto_reply.threading, "Thread", Inline),              mock.patch.object(auto_reply.image_library, "scan_library", side_effect=RuntimeError("disk")):
            auto_reply.warm_up_image_library({"images_dir": self.dir})
        self.assertTrue(any("預先辨識圖庫失敗" in l for l in logs))


class TestConfigKeys(_Base):
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
