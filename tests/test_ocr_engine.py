import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ocr_engine


class _FakeRapidOCR:
    instances = 0

    def __init__(self):
        type(self).instances += 1
        self.received = []
        self.result = [[[0, 0, 1, 1], "早安 Good", 0.9], [[0, 0, 1, 1], "Morning 平安", 0.8]]
        self.error = None

    def __call__(self, source):
        self.received.append(source)
        if self.error:
            raise self.error
        return self.result, [0.1]


def _install(fake_cls=_FakeRapidOCR):
    module = types.SimpleNamespace(RapidOCR=fake_cls)
    return mock.patch.dict(sys.modules, {"rapidocr_onnxruntime": module})


class TestOcrEngine(unittest.TestCase):
    def setUp(self):
        ocr_engine._engine = None
        ocr_engine._engine_error = None
        _FakeRapidOCR.instances = 0

    def tearDown(self):
        ocr_engine._engine = None
        ocr_engine._engine_error = None

    def test_joins_text_blocks_without_spaces(self):
        with _install():
            self.assertEqual(ocr_engine.read_text("a.jpg"), "早安GoodMorning平安")      # 與 Windows OCR 的輸出格式一致
            self.assertTrue(ocr_engine.is_available())
            self.assertIsNone(ocr_engine.unavailable_reason())

    def test_empty_result_is_empty_text(self):
        with _install():
            ocr_engine._get_engine().result = None                                      # RapidOCR 沒找到任何文字
            self.assertEqual(ocr_engine.read_text("a.jpg"), "")

    def test_engine_is_loaded_once(self):
        with _install():
            ocr_engine.read_text("a.jpg")
            ocr_engine.read_text("b.jpg")
            self.assertEqual(_FakeRapidOCR.instances, 1)

    def test_pil_image_is_converted_to_bgr_array(self):
        from PIL import Image
        img = Image.new("RGB", (4, 2), (10, 20, 30))
        with _install():
            ocr_engine.read_text(img)
            received = ocr_engine._get_engine().received[0]
        self.assertEqual(received.shape, (2, 4, 3))
        self.assertEqual(tuple(received[0, 0]), (30, 20, 10))                           # RGB 轉成 OpenCV 的 BGR

    def test_path_is_passed_through_unchanged(self):
        with _install():
            ocr_engine.read_text("some/path.jpg")
            self.assertEqual(ocr_engine._get_engine().received, ["some/path.jpg"])

    def test_falls_back_when_rapidocr_is_not_installed(self):
        calls = []
        with mock.patch.dict(sys.modules, {"rapidocr_onnxruntime": None}):          # import 會失敗
            self.assertEqual(ocr_engine.read_text("a.jpg", fallback=lambda p: calls.append(p) or "後備"), "後備")
            self.assertFalse(ocr_engine.is_available())
            self.assertIn("rapidocr_onnxruntime", ocr_engine.unavailable_reason())
            with self.assertRaises(RuntimeError):
                ocr_engine.read_text("a.jpg")                                           # 沒有後備就明確失敗
        self.assertEqual(calls, ["a.jpg"])

    def test_failure_to_load_is_remembered_and_not_retried(self):
        class Broken:
            attempts = 0

            def __init__(self):
                type(self).attempts += 1
                raise OSError("model missing")

        with _install(Broken):
            self.assertFalse(ocr_engine.is_available())
            self.assertFalse(ocr_engine.is_available())
            ocr_engine.read_text("a.jpg", fallback=lambda p: "x")
            self.assertEqual(Broken.attempts, 1)

    def test_runtime_error_uses_fallback_or_raises(self):
        with _install():
            ocr_engine._get_engine().error = ValueError("bad image")
            self.assertEqual(ocr_engine.read_text("a.jpg", fallback=lambda p: "後備"), "後備")
            with self.assertRaises(ValueError):
                ocr_engine.read_text("a.jpg")                                           # 沒有後備：把原本的錯誤擲出


if __name__ == "__main__":
    unittest.main()
