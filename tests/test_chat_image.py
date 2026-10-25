import numpy as np

from grandma.chat_image import find_latest_image

BG = (140, 170, 200)


def blank(h=800, w=600):
    area = np.zeros((h, w, 3), dtype=np.uint8)
    area[:] = BG
    return area


def paste_photo(area, x, y, w, h, seed=0):
    rng = np.random.default_rng(seed)
    area[y : y + h, x : x + w] = rng.integers(0, 255, (h, w, 3), dtype=np.uint8)


def paste_text_bubble(area, x, y, w, h):
    area[y : y + h, x : x + w] = 255
    area[y + 20 : y + h - 20 : 12, x + 15 : x + w - 15] = 30  # 幾行黑色「文字」


def test_finds_bottom_left_photo():
    area = blank()
    paste_photo(area, 60, 50, 200, 200, seed=1)
    paste_photo(area, 60, 400, 220, 180, seed=2)  # 最新的一張
    paste_text_bubble(area, 60, 620, 300, 120)  # 圖片之後的文字訊息
    assert find_latest_image(area) == (60, 400, 220, 180)


def test_ignores_own_photo_on_right():
    area = blank()
    paste_photo(area, 60, 100, 200, 200, seed=1)
    paste_photo(area, 350, 500, 200, 200, seed=2)  # 自己傳的，在右邊
    assert find_latest_image(area) == (60, 100, 200, 200)


def test_no_photo():
    area = blank()
    paste_text_bubble(area, 60, 300, 300, 150)
    assert find_latest_image(area) is None
