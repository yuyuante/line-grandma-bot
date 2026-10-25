"""從聊天室截圖中找出對方最新傳來的圖片。"""
import cv2
import numpy as np

MIN_IMAGE_SIDE = 100
MIN_COLORS = 300  # 文字泡泡只有少數幾種顏色，圖片會有很多種


def find_latest_image(area: np.ndarray):
    """在聊天區域截圖（RGB）裡找出最下方、對方那一側（左邊）的圖片，回傳 (x, y, w, h) 或 None。"""
    bg = np.median(area[:, -5:].reshape(-1, 3), axis=0)  # 最右邊一條通常是聊天背景
    mask = (np.abs(area.astype(int) - bg).sum(axis=2) > 40).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    width = area.shape[1]
    best = None
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w < MIN_IMAGE_SIDE or h < MIN_IMAGE_SIDE or x > width * 0.5:
            continue
        crop = area[y : y + h, x : x + w] >> 4  # 每個顏色通道量化成 16 階
        colors = np.unique(crop.reshape(-1, 3), axis=0)
        if len(colors) < MIN_COLORS:
            continue
        if best is None or y + h > best[1] + best[3]:
            best = (x, y, w, h)
    return best
