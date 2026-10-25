from datetime import datetime

import numpy as np
from PIL import Image

from grandma.library import Library, phash, time_keyword


def noise_image(seed):
    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 255, (32, 32, 3), dtype=np.uint8)
    return Image.fromarray(arr).resize((256, 256), Image.NEAREST)


def make_library(tmp_path, n, keyword=None):
    lib = Library(tmp_path / "test.db", tmp_path / "library")
    images = []
    for i in range(n):
        img = noise_image(i)
        name = f"{i}.jpg"
        img.save(lib.library_dir / name)
        lib.add(name, phash(img), keyword)
        images.append(img)
    return lib, images


def test_never_picks_the_received_image(tmp_path):
    lib, images = make_library(tmp_path, 3)
    for _ in range(30):
        assert lib.pick_different(images[0]).name != "0.jpg"


def test_excludes_recently_sent(tmp_path):
    lib, images = make_library(tmp_path, 3)
    lib.mark_sent(lib.library_dir / "1.jpg")
    for _ in range(30):
        assert lib.pick_different(images[0], recent_n=5).name == "2.jpg"


def test_relaxes_recent_rule_when_library_is_small(tmp_path):
    lib, images = make_library(tmp_path, 2)
    lib.mark_sent(lib.library_dir / "1.jpg")
    assert lib.pick_different(images[0]).name == "1.jpg"


def test_empty_library_returns_none(tmp_path):
    lib = Library(tmp_path / "test.db", tmp_path / "library")
    assert lib.pick_different(noise_image(0)) is None


def test_sent_today(tmp_path):
    lib, _ = make_library(tmp_path, 2)
    lib.mark_sent(lib.library_dir / "0.jpg", now=datetime(2026, 1, 1, 23, 0))
    lib.mark_sent(lib.library_dir / "1.jpg", now=datetime(2026, 1, 2, 8, 0))
    assert lib.sent_today(now=datetime(2026, 1, 2, 9, 0)) == 1


def test_time_keyword():
    assert time_keyword(7) == "早安"
    assert time_keyword(12) == "午安"
    assert time_keyword(22) == "晚安"
    assert time_keyword(16) is None
