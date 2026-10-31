import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import crawl_images


class TestCrawlSources(unittest.TestCase):
    def test_missing_or_empty_sources_use_defaults_as_copies(self):
        for value in (None, [], "x", 5, {}):
            srcs, skipped = crawl_images.normalize_sources(value)
            self.assertEqual([s["name"] for s in srcs], ["fpg-daily-photo", "fpg-daily-illustration"])
            self.assertEqual(skipped, [])
        srcs, _ = crawl_images.normalize_sources(None)
        srcs[0]["start"] = 99999                                                   # 改動回傳值不能影響預設
        self.assertEqual(crawl_images.DEFAULT_IMAGE_SOURCES[0]["start"], 100)

    def test_valid_source_gets_defaults_for_optional_fields(self):
        srcs, skipped = crawl_images.normalize_sources([{"url": "https://x/{n}.jpg", "start": "1", "end": 5}])
        self.assertEqual(skipped, [])
        self.assertEqual(srcs, [{"name": "#1", "url": "https://x/{n}.jpg", "start": 1, "end": 5, "category": None, "pad": 0}])
        srcs, _ = crawl_images.normalize_sources([{"name": "n", "url": "http://x/{n}", "start": 1, "end": 1, "category": "evening", "pad": 3}])
        self.assertEqual((srcs[0]["category"], srcs[0]["pad"]), ("evening", 3))

    def test_invalid_sources_are_skipped_with_reasons(self):
        good = {"name": "good", "url": "https://x/{n}.jpg", "start": 1, "end": 2}
        bad = [
            {"name": "no-placeholder", "url": "https://x/a.jpg", "start": 1, "end": 2},
            {"name": "not-http", "url": "file:///{n}", "start": 1, "end": 2},
            {"name": "reversed", "url": "https://x/{n}", "start": 5, "end": 1},
            {"name": "too-many", "url": "https://x/{n}", "start": 1, "end": crawl_images.MAX_SOURCE_SPAN + 1},
            {"name": "bad-category", "url": "https://x/{n}", "start": 1, "end": 2, "category": "noon-ish"},
            {"name": "missing-range", "url": "https://x/{n}"},
            {"name": "not-numbers", "url": "https://x/{n}", "start": "a", "end": "b"},
            "just a string",
        ]
        srcs, skipped = crawl_images.normalize_sources([good] + bad)
        self.assertEqual([s["name"] for s in srcs], ["good"])
        self.assertEqual(len(skipped), len(bad))
        self.assertTrue(any("no-placeholder" in m for m in skipped))
        # 全部不合格時退回預設來源，不會變成沒有來源
        srcs, skipped = crawl_images.normalize_sources(bad)
        self.assertEqual([s["name"] for s in srcs], ["fpg-daily-photo", "fpg-daily-illustration"])
        self.assertEqual(len(skipped), len(bad))

    def test_build_candidates_expands_ranges_padding_and_category(self):
        sources = [
            {"name": "a", "url": "https://a/{n}.jpg", "start": 1, "end": 3, "pad": 3, "category": "neutral"},
            {"name": "b", "url": "https://b/{n}.jpg", "start": 10, "end": 11},
        ]
        cands = crawl_images.build_candidates(sources)
        self.assertEqual(sorted(cands, key=lambda c: c[0]), [
            ("https://a/001.jpg", "neutral"), ("https://a/002.jpg", "neutral"), ("https://a/003.jpg", "neutral"),
            ("https://b/10.jpg", None), ("https://b/11.jpg", None)])
        urls = crawl_images.build_candidate_urls(sources)                           # 舊介面：只有網址
        self.assertEqual(sorted(urls), sorted(c[0] for c in cands))

    def test_default_candidates_cover_both_fpg_galleries(self):
        cands = crawl_images.build_candidates()
        self.assertEqual(len(cands), (749 - 100 + 1) + (244 - 30 + 1))
        self.assertTrue(all(c[1] is None for c in cands))                           # 預設來源不標時段，靠 OCR 判斷
        self.assertTrue(any("daily-photo-100.jpg" in c[0] for c in cands))
        self.assertTrue(any("daily-illustration-244.jpg" in c[0] for c in cands))

    def _crawl(self, on_saved, count=2):
        saved_paths = []
        n = {"i": 0}

        def fake_download(url, save_dir, md5s):
            n["i"] += 1
            path = os.path.join(save_dir, f"grandma_{n['i']}.jpg")
            saved_paths.append(path)
            return path

        sources = [{"name": "s", "url": "https://x/{n}.jpg", "start": 1, "end": 20, "category": "neutral"}]
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(crawl_images, "download_and_validate_image", fake_download), \
             mock.patch.object(crawl_images, "get_existing_md5s", return_value=set()), \
             mock.patch.object(crawl_images.time, "sleep"), mock.patch.object(crawl_images, "log_crawl"):
            got = crawl_images.crawl_grandma_images(target_count=count, save_dir=d, sources=sources, on_saved=on_saved)
        return got, saved_paths

    def test_crawl_reports_each_saved_image_with_its_source_category(self):
        calls = []
        got, paths = self._crawl(lambda path, cat: calls.append((path, cat)))
        self.assertEqual(got, 2)
        self.assertEqual(calls, [(p, "neutral") for p in paths])

    def test_crawl_continues_when_on_saved_fails(self):
        def boom(path, cat):
            raise RuntimeError("tagging failed")

        got, _ = self._crawl(boom, count=3)
        self.assertEqual(got, 3)                                                    # 記錄類別失敗不影響下載

    def test_on_saved_false_means_discarded_and_not_counted(self):
        decisions = iter([False, True, False, True])
        calls = []
        got, paths = self._crawl(lambda path, cat: calls.append(path) or next(decisions), count=2)
        self.assertEqual(got, 2)                                                    # 兩張被捨棄的不算
        self.assertEqual(len(calls), 4)

    def test_fetch_festival_candidates_parses_listing_and_caches(self):
        html = ('<a data-original="sticker/festival-illustration/festival-illustration-051.jpg"></a>'
                '<a data-original="sticker/festival-photo/festival-photo-036.jpg"></a>'
                '<a data-original="sticker/festival-short/festival-short-002.jpg"></a>'          # 影片：不收
                '<a data-original="sticker/daily-photo/daily-photo-001.jpg"></a>'                # 不是節慶：不收
                '<a data-original="sticker/festival-illustration/festival-illustration-051.jpg"></a>')   # 重複
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = html.encode("utf-8")
        crawl_images._festival_cache.update({"time": 0.0, "urls": []})
        with mock.patch.object(crawl_images.urllib.request, "urlopen", return_value=resp) as opener:
            first = crawl_images.fetch_festival_candidates(now=1000.0)
            second = crawl_images.fetch_festival_candidates(now=1500.0)                         # 一小時內：用快取
        self.assertEqual(sorted(u for u, _ in first), [
            "https://sticker.fpg.com.tw/sticker/festival-illustration/festival-illustration-051.jpg",
            "https://sticker.fpg.com.tw/sticker/festival-photo/festival-photo-036.jpg"])
        self.assertEqual(len(second), 2)
        self.assertEqual(opener.call_count, 1)
        self.assertTrue(all(cat is None for _, cat in first))
        with mock.patch.object(crawl_images.urllib.request, "urlopen", return_value=resp) as opener:
            crawl_images.fetch_festival_candidates(now=1000.0 + crawl_images.FESTIVAL_CACHE_SECONDS + 1)
            self.assertEqual(opener.call_count, 1)                                                # 過期後重新抓

    def test_fetch_festival_candidates_failure_returns_empty(self):
        crawl_images._festival_cache.update({"time": 0.0, "urls": []})
        with mock.patch.object(crawl_images.urllib.request, "urlopen", side_effect=OSError("offline")),              mock.patch.object(crawl_images, "log_crawl"):
            self.assertEqual(crawl_images.fetch_festival_candidates(now=5.0), [])

    def test_crawl_without_callback_still_works(self):
        got, _ = self._crawl(None)
        self.assertEqual(got, 2)


if __name__ == "__main__":
    unittest.main()
