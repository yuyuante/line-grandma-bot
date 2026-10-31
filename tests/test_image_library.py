import json
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import image_kind as ik
import image_library as il

K = ik.ImageKind


def _write(path, text):
    """寫一個檔案，內容就是它的「OCR 文字」，讓假 OCR 直接讀回來 (內容不同 MD5 就不同)"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def read_ocr(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "images")
        os.makedirs(self.dir)
        self.index_path = os.path.join(self.tmp.name, "kinds.json")
        self.ocr_calls = []

        def counting_ocr(path):
            self.ocr_calls.append(os.path.basename(path))
            return read_ocr(path)

        self.index = il.KindIndex(self.index_path, counting_ocr)

    def tearDown(self):
        self.tmp.cleanup()

    def img(self, name, text):
        return _write(os.path.join(self.dir, name), text)


class TestKindIndex(_Base):
    def test_classifies_and_caches_by_content(self):
        path = self.img("a.jpg", "FORMOSA!早安悠然自得喜樂相随")
        kind, text = self.index.classify(path)
        self.assertEqual(kind, K("greeting", ("morning",)))
        self.assertEqual(text, "早安悠然自得喜樂相隨")                                # 去浮水印、簡轉繁
        self.index.classify(path)
        self.assertEqual(self.ocr_calls, ["a.jpg"])                                    # 第二次不再 OCR
        copy = self.img("renamed.jpg", "FORMOSA!早安悠然自得喜樂相随")
        self.index.classify(copy)
        self.assertEqual(self.ocr_calls, ["a.jpg"])                                    # 內容相同 (MD5 相同)，改名也命中快取

    def test_unrecognised_is_cached_as_none(self):
        path = self.img("u.jpg", "FORMOSA!")
        self.assertEqual(self.index.classify(path), (None, ""))
        self.index.classify(path)
        self.assertEqual(self.ocr_calls, ["u.jpg"])                                    # 認不出來也只辨識一次

    def test_ocr_failure_propagates_and_is_not_cached(self):
        path = self.img("e.jpg", "中秋節快樂")
        boom = il.KindIndex(self.index_path, lambda p: (_ for _ in ()).throw(RuntimeError("ocr down")))
        with self.assertRaises(RuntimeError):
            boom.classify(path)
        kind, _ = self.index.classify(path)                                            # 恢復後可以正常辨識
        self.assertEqual(kind, K("festival", (), "中秋節"))

    def test_persists_across_instances_and_survives_a_corrupt_file(self):
        path = self.img("p.jpg", "晚安好眠")
        self.index.classify(path)
        again = il.KindIndex(self.index_path, lambda p: self.fail("should use the saved result"))
        self.assertEqual(again.classify(path)[0], K("greeting", ("evening",)))
        with open(self.index_path, "w", encoding="utf-8") as f:
            f.write("{not json")
        fresh = il.KindIndex(self.index_path, read_ocr)
        self.assertEqual(fresh.classify(path)[0], K("greeting", ("evening",)))          # 壞檔當成沒有，重新辨識

    def test_forget(self):
        path = self.img("f.jpg", "晚安好眠")
        self.index.classify(path)
        self.index.forget(il.file_md5(path))
        self.index.classify(path)
        self.assertEqual(self.ocr_calls, ["f.jpg", "f.jpg"])

    def test_missing_file(self):
        self.assertEqual(self.index.classify(os.path.join(self.dir, "nope.jpg")), (None, ""))


class TestScanLibrary(_Base):
    def test_only_recognised_images_are_returned(self):
        self.img("a.jpg", "早安平安")
        self.img("b.png", "中秋節快樂")
        self.img("c.jpg", "FORMOSA!")                                                  # 認不出來：不在結果裡
        self.img("notes.txt", "早安")                                                  # 不是圖片
        library = il.scan_library(self.dir, self.index)
        self.assertEqual(sorted(os.path.basename(i.path) for i in library), ["a.jpg", "b.png"])
        self.assertEqual({i.kind.category for i in library}, {"greeting", "festival"})

    def test_ocr_failure_skips_that_image_only(self):
        self.img("ok.jpg", "早安")
        self.img("bad.jpg", "晚安")
        logs = []

        def flaky(path):
            if path.endswith("bad.jpg"):
                raise RuntimeError("ocr down")
            return read_ocr(path)

        library = il.scan_library(self.dir, il.KindIndex(self.index_path, flaky), log=logs.append)
        self.assertEqual([os.path.basename(i.path) for i in library], ["ok.jpg"])
        self.assertEqual(len(logs), 1)

    def test_missing_directory_is_empty(self):
        self.assertEqual(il.scan_library(os.path.join(self.tmp.name, "nope"), self.index), [])


def L(name, text, kind=None):
    kind = kind or ik.classify_text(text)
    return il.LibraryImage(name, name + "-md5", kind, il.clean_text(text))


class TestTextSimilarity(unittest.TestCase):
    def test_same_image_read_slightly_differently(self):
        self.assertTrue(il.texts_similar("FORMOSA!早安悠然自得喜樂相隨", "早安悠然自得喜樂相随"))
        self.assertTrue(il.texts_similar("早安悠然自得喜樂相隨", "早安悠然自得喜樂相陪"))      # 認錯一個字
        self.assertTrue(il.texts_similar("早安悠然自得喜樂相隨", "早安悠然白得喜樂相隨"))      # 錯字在中間：連續相同的片段都很短，要靠整段相似度

    def test_line_ui_text_from_the_chat_screenshot_is_ignored(self):
        # 縮圖截圖會一併截到圖片下方的操作列與時間，它們不是圖上的字 (實測：這曾讓同一張圖被當成不同張)
        screenshot = "平安喜樂聖誕快樂FORMOSA!儲存|另存新檔|分享|傳送至Keep筆記下午10:50"
        self.assertEqual(il.clean_text(screenshot).replace("|", ""), "平安喜樂聖誕快樂")
        self.assertTrue(il.texts_similar(screenshot, "平安喜樂聖誕快樂"))
        self.assertFalse(il.texts_similar(screenshot, "聖誕節快樂歡樂滿屋"))                 # 另一張聖誕圖

    def test_shorter_text_mostly_contained_in_the_longer_one(self):
        self.assertTrue(il.texts_similar("平安喜樂聖誕快樂", "FORMOSA!南投平安喜樂聖誕快樂祝福您"))     # 縮圖少讀了幾行
        self.assertFalse(il.texts_similar("平安喜樂聖誕快樂", "平安喜樂事事順利心想事成"))            # 只有開頭幾個字相同

    def test_ui_words_are_removed_from_both_sides_so_comparison_stays_consistent(self):
        self.assertTrue(il.texts_similar("認同請分享幸福快樂每一天", "認同請分享幸福快樂每一天"))

    def test_different_images_and_short_texts(self):
        self.assertFalse(il.texts_similar("早安悠然自得喜樂相隨", "早安珍惜每個緣分感恩相遇"))
        self.assertFalse(il.texts_similar("早安", "早安"))                                # 太短不可靠，不當成同一張
        self.assertFalse(il.texts_similar("", "早安悠然自得喜樂相隨"))
        self.assertFalse(il.texts_similar(None, None))


class TestCandidatePools(unittest.TestCase):
    def setUp(self):
        self.morning = L("m", "早安平安喜樂")
        self.evening = L("e", "晚安好眠")
        self.blessing = L("b", "幸福快樂")
        self.care = L("c", "雨天路滑注意安全")
        self.inspire = L("i", "人生要學會放下")
        self.mid_autumn = L("ma", "中秋節快樂")
        self.dragon = L("d", "端午節快樂")
        self.library = [self.morning, self.evening, self.blessing, self.care, self.inspire, self.mid_autumn, self.dragon]

    def pools(self, incoming_text, slot="morning", time_aware=True):
        return il.candidate_pools(ik.classify_text(incoming_text), slot, time_aware)

    def pick(self, pool_index, incoming_text, slot="morning", time_aware=True, **kw):
        pool = self.pools(incoming_text, slot, time_aware)[pool_index]
        return il.pick_from_pool(self.library, pool, incoming_text, **kw)

    def test_festival_replies_with_the_same_festival_only(self):
        self.assertEqual(len(self.pools("中秋節快樂")), 1)                              # 沒有退而求其次：回錯節日比不回更糟
        self.assertIs(self.pick(0, "中秋節快樂 闔家團圓"), self.mid_autumn)
        self.assertIsNone(il.pick_from_pool(self.library, self.pools("父親節快樂")[0]))   # 沒有父親節的圖

    def test_greeting_uses_current_time_slot_then_neutral(self):
        self.assertIs(self.pick(0, "早安你好嗎", slot="morning"), self.morning)
        self.assertIs(self.pick(0, "早安你好嗎", slot="evening"), self.evening)            # 對方傳早安，現在是晚上：回晚安
        self.assertIsNone(self.pick(0, "早安你好嗎", slot="noon"))                        # 沒有午安的圖
        neutral = {self.pick(1, "早安你好嗎", slot="noon", rng=random.Random(n)).kind.category for n in range(20)}
        self.assertEqual(neutral, {"blessing", "care", "inspire"})                        # 退而用無時段圖，節日圖不會被挑到

    def test_greeting_ignores_slot_when_time_awareness_is_off(self):
        kinds = {self.pick(0, "早安", slot="noon", time_aware=False, rng=random.Random(n)).kind.slots for n in range(20)}
        self.assertEqual(kinds, {("morning",), ("evening",)})

    def test_other_natures_reply_with_the_same_nature_first(self):
        self.assertIs(self.pick(0, "雨天路滑請慢行注意安全"), self.care)
        self.assertIs(self.pick(0, "福氣自來幸福快樂"), self.blessing)
        self.assertIs(self.pick(0, "懂得珍惜知足常樂"), self.inspire)

    def test_other_natures_fall_back_to_time_slot_greeting_then_neutral(self):
        pools = self.pools("雨天路滑請慢行注意安全", slot="evening")
        self.assertEqual(len(pools), 3)
        self.assertIs(il.pick_from_pool([self.morning, self.evening], pools[1]), self.evening)
        self.assertEqual(il.pick_from_pool([self.blessing], pools[2]), self.blessing)

    def test_incoming_image_itself_is_never_picked(self):
        same_as_incoming = L("same", "雨天路滑請慢行注意安全多留心")
        picked = il.pick_from_pool([same_as_incoming], self.pools("雨天路滑請慢行注意安全多留心")[0], "FORMOSA!雨天路滑請慢行注意安全多留心")
        self.assertIsNone(picked)

    def test_forbidden_md5s_are_excluded(self):
        self.assertIsNone(self.pick(0, "雨天路滑請慢行注意安全", forbidden_md5s={self.care.md5}))
        self.assertIs(self.pick(0, "雨天路滑請慢行注意安全", forbidden_md5s={"other"}), self.care)


class TestFetchMatching(_Base):
    def downloader(self, texts):
        """假的下載：依序把 texts 寫成檔案；文字為 None 代表下載失敗"""
        queue = list(texts)
        state = {"downloads": 0}

        def download(url, save_dir, md5s):
            text = queue.pop(0)
            if text is None:
                return None
            state["downloads"] += 1
            return _write(os.path.join(save_dir, f"grandma_{state['downloads']}_{len(queue)}.jpg"), text)

        return download, state

    def fetch(self, texts, predicate, **kw):
        download, state = self.downloader(texts)
        candidates = [(f"http://x/{i}", None) for i in range(len(texts))]
        kw.setdefault("max_downloads", 40)
        result = il.fetch_matching(predicate, self.dir, self.index, candidates, download, **kw)
        return result, state

    def festival(self, name):
        return lambda img: img.kind.category == "festival" and img.kind.festival == name

    def files(self):
        return sorted(os.listdir(self.dir))

    def test_stops_at_the_first_match_and_keeps_other_recognised_images(self):
        result, state = self.fetch(["晚安好眠", "父親節快樂", "中秋節快樂團圓", "不會下載到這張"], self.festival("中秋節"))
        self.assertEqual(result.kind, K("festival", (), "中秋節"))
        self.assertEqual(state["downloads"], 3)                                        # 找到就停
        self.assertEqual(len(self.files()), 3)                                         # 辨識得出但不符合的留作庫存

    def test_unrecognised_images_are_deleted(self):
        result, _ = self.fetch(["FORMOSA!", "", "中秋節快樂團圓"], self.festival("中秋節"))
        self.assertIsNotNone(result)
        self.assertEqual(len(self.files()), 1)                                         # 認不出來的都刪了，只剩符合的
        self.assertEqual(self.index._load().get(il.file_md5(os.path.join(self.dir, self.files()[0])))["kind"]["festival"], "中秋節")

    def test_gives_up_after_max_downloads(self):
        result, state = self.fetch(["晚安好眠"] * 1 + ["早安好眠", "晚安平安", "早安你好", "晚安你好"], self.festival("中秋節"), max_downloads=3)
        self.assertIsNone(result)
        self.assertEqual(state["downloads"], 3)

    def test_gives_up_after_consecutive_failures(self):
        result, state = self.fetch([None] * 5 + ["中秋節快樂團圓"], self.festival("中秋節"), max_consecutive_fails=3)
        self.assertIsNone(result)
        self.assertEqual(state["downloads"], 0)
        # 失敗不是連續的就不算
        result, _ = self.fetch([None, None, "晚安好眠", None, None, "中秋節快樂團圓"], self.festival("中秋節"), max_consecutive_fails=3)
        self.assertIsNotNone(result)

    def test_does_not_return_the_image_the_friend_sent(self):
        result, _ = self.fetch(["中秋節快樂闔家團圓", "中秋節快樂月圓人團圓好運"], self.festival("中秋節"), incoming_text="中秋節快樂闔家團圓")
        self.assertIsNotNone(result)
        self.assertEqual(result.text, "中秋節快樂月圓人團圓好運")                         # 和對方相同的那張被跳過

    def test_forbidden_md5_is_skipped(self):
        first = "中秋節快樂闔家團圓"
        forbidden = il.file_md5(_write(os.path.join(self.tmp.name, "tmp.jpg"), first))
        result, _ = self.fetch([first, "中秋節快樂月圓人團圓好運"], self.festival("中秋節"), forbidden_md5s={forbidden})
        self.assertEqual(result.text, "中秋節快樂月圓人團圓好運")

    def test_pool_cap_removes_extra_non_matching_images(self):
        for i in range(3):
            self.img(f"existing{i}.jpg", f"晚安存貨{i}")
        result, _ = self.fetch(["晚安好眠", "晚安平安"], self.festival("中秋節"), max_pool=3)
        self.assertIsNone(result)
        self.assertEqual(len(self.files()), 3)                                         # 庫存已滿：新抓到的不符合的圖不留

    def test_ocr_error_on_a_download_discards_that_image(self):
        def flaky(path):
            if "boom" in read_ocr(path):
                raise RuntimeError("ocr down")
            return read_ocr(path)

        index = il.KindIndex(self.index_path, flaky)
        download, _ = self.downloader(["boom", "中秋節快樂團圓"])
        result = il.fetch_matching(self.festival("中秋節"), self.dir, index, [("u1", None), ("u2", None)], download)
        self.assertIsNotNone(result)
        self.assertEqual(len(self.files()), 1)                                         # 辨識出錯 = 辨識不出來，捨棄


if __name__ == "__main__":
    unittest.main()
