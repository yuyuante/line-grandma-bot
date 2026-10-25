"""用真實樣本檢查 CLIP 判斷的準確度。

把長輩圖放進 tests/samples/grandma/，一般照片、截圖、貼圖放進 tests/samples/other/，
然後執行 pytest tests/test_classifier.py -s，看每張的分數來調整 config.yaml 的 clip_threshold。
"""
from pathlib import Path

import pytest
from PIL import Image

pytest.importorskip("open_clip")

from grandma import classifier  # noqa: E402

SAMPLES = Path(__file__).parent / "samples"
THRESHOLD = 0.6
MIN_ACCURACY = 0.85


def load_samples(name):
    folder = SAMPLES / name
    if not folder.is_dir():
        return []
    return [p for p in sorted(folder.iterdir()) if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]


@pytest.mark.skipif(not load_samples("grandma") or not load_samples("other"), reason="沒有樣本圖")
def test_accuracy():
    correct = total = 0
    for label, expected in (("grandma", True), ("other", False)):
        for path in load_samples(label):
            ok, score = classifier.is_grandma_image(Image.open(path), THRESHOLD)
            print(f"{'✓' if ok == expected else '✗'} {score:.2f} {label}/{path.name}")
            correct += ok == expected
            total += 1
    assert correct / total >= MIN_ACCURACY
