"""
圖庫的性質索引與挑圖：記住每張圖的性質 (辨識一次就快取)，依「對方傳來的圖」挑一張同性質但不同張的圖，
沒有庫存時立即上網抓、邊抓邊辨識，抓到符合的就停。

挑圖規則 (依序用第一個有圖的池子)：
- 對方傳節日圖 -> 同一個節日 (中秋節回中秋節)
- 對方傳時段問候 -> 現在這個時段的問候圖 (早/午/晚安)；沒有就用無時段關聯的圖 (祝福、叮嚀、心靈雞湯)
- 對方傳平安叮嚀 / 祝福鼓勵 / 心靈雞湯 -> 同性質；真的沒有，才退而用現在時段的問候圖
- 一律排除和對方那張相同的圖 (比對 OCR 文字相似度，因為對方的原圖是加密的拿不到檔案)
- 辨識不出性質的圖一律捨棄：不入庫、不回覆
"""
import difflib
import glob
import hashlib
import json
import os
import random
import re
import tempfile
import threading
from dataclasses import dataclass

import image_kind

TEXT_KEEP = 200                 # 索引裡保留的文字長度 (拿來比對兩張圖是不是同一張)
SIMILAR_RATIO = 0.85            # 兩張圖的文字相似度達此值就視為同一張圖
SIMILAR_MIN_CHARS = 6           # 文字太短不可靠，不拿來判斷相同
SIMILAR_CONTAINED = 0.85        # 較短那段文字有這個比例連續出現在較長那段裡，也視為同一張圖
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
_WATERMARK = re.compile(r"FORMOSA!?", re.IGNORECASE)     # 每張圖都有的浮水印，不參與比對
# 聊天室截圖會一併截到圖片下方的操作列與時間，這些是 LINE 的介面文字，不是圖上的字
_UI_TEXT = re.compile(r"另存新檔|傳送至Keep筆記|傳送至Keep|儲存|分享|[上下]午\d{1,2}:\d{2}|\d{1,2}:\d{2}")


def clean_text(text):
    """索引與比對用的文字：去空白、簡轉繁、去浮水印與 LINE 介面文字 (操作列、時間)、截短"""
    text = _WATERMARK.sub("", image_kind.normalize_text(text))
    return _UI_TEXT.sub("", text)[:TEXT_KEEP]


def texts_similar(a, b):
    """
    兩段 OCR 文字是不是同一張圖：整段相似度夠高 (容許 OCR 認錯幾個字)，或是較短那段的絕大部分連續出現在較長那段裡
    (縮圖 OCR 可能少讀幾行)。清理後任一段不到 SIMILAR_MIN_CHARS 字就不可靠，一律視為不同。
    """
    a, b = clean_text(a), clean_text(b)
    shortest = min(len(a), len(b))
    if shortest < SIMILAR_MIN_CHARS:
        return False
    matcher = difflib.SequenceMatcher(None, a, b)
    if matcher.ratio() >= SIMILAR_RATIO:
        return True
    return matcher.find_longest_match(0, len(a), 0, len(b)).size >= SIMILAR_CONTAINED * shortest


def file_md5(path):
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except OSError:
        return None


@dataclass(frozen=True)
class LibraryImage:
    path: str
    md5: str
    kind: image_kind.ImageKind
    text: str


class KindIndex:
    """圖片性質索引：MD5 -> 辨識結果，存成 JSON 檔，同一張圖只辨識一次。ocr 是 路徑 -> 文字 的函式。"""

    def __init__(self, path, ocr):
        self.path = path
        self._ocr = ocr
        self._data = None
        self._lock = threading.RLock()

    def _load(self):
        if self._data is None:
            data = {}
            try:
                with open(self.path, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, ValueError):
                pass
            self._data = data
        return self._data

    def _save(self):
        directory = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def classify(self, path, md5=None):
        """
        回傳 (性質或 None, 清理過的文字)。快取命中不再 OCR。OCR 本身出錯會擲出例外 (暫時性問題，不快取)。
        文字讀不出來或認不出性質回傳 (None, 文字)，這個結果也會快取，呼叫端應捨棄該圖。
        """
        md5 = md5 or file_md5(path)
        if not md5:
            return None, ""
        with self._lock:
            entry = self._load().get(md5)
            if isinstance(entry, dict):
                kind = image_kind.ImageKind.from_dict(entry["kind"]) if entry.get("kind") else None
                return kind, entry.get("text", "")
        text = self._ocr(path)
        kind = image_kind.classify_text(text)
        cleaned = clean_text(text)
        with self._lock:
            self._load()[md5] = {"kind": kind.to_dict() if kind else None, "text": cleaned}
            self._save()
        return kind, cleaned

    def forget(self, md5):
        with self._lock:
            if self._load().pop(md5, None) is not None:
                self._save()


def list_image_files(images_dir):
    files = []
    for name in sorted(os.listdir(images_dir)) if os.path.isdir(images_dir) else []:
        if name.lower().endswith(IMAGE_EXTENSIONS):
            files.append(os.path.join(images_dir, name))
    return files


def scan_library(images_dir, index, log=None):
    """
    掃描圖庫，回傳辨識得出性質的圖 (list[LibraryImage])。辨識不出來的圖不會出現在結果裡 (也就不會被挑到)。
    OCR 暫時出錯的圖這次略過，下次再試。
    """
    library = []
    for path in list_image_files(images_dir):
        md5 = file_md5(path)
        if not md5:
            continue
        try:
            kind, text = index.classify(path, md5)
        except Exception as e:
            if log:
                log(f"[圖庫] 辨識 {os.path.basename(path)} 失敗，這次略過: {e}")
            continue
        if kind is not None:
            library.append(LibraryImage(path, md5, kind, text))
    return library


def candidate_pools(incoming_kind, slot, time_aware=True):
    """
    依對方傳來的圖的性質，回傳挑圖用的篩選條件清單 (依優先順序，前面的池子沒圖才用後面的)。
    每個條件是 LibraryImage -> bool。
    """
    def slot_greeting(img):
        k = img.kind
        return k.category == "greeting" and bool(k.slots) and (not time_aware or slot in k.slots)

    def time_neutral(img):
        return img.kind.is_time_neutral

    category = incoming_kind.category
    if category == "festival":
        return [lambda img: img.kind.category == "festival" and img.kind.festival == incoming_kind.festival]
    if category == "greeting":
        return [slot_greeting, time_neutral]
    return [lambda img: img.kind.category == category, slot_greeting, time_neutral]


def pick_from_pool(library, predicate, incoming_text="", forbidden_md5s=(), rng=random):
    """從圖庫挑一張符合條件、不是對方那張、也不在百輪禁用名單裡的圖；沒有回傳 None"""
    forbidden = set(forbidden_md5s)
    options = [img for img in library
               if predicate(img) and img.md5 not in forbidden and not texts_similar(img.text, incoming_text)]
    return rng.choice(options) if options else None


def fetch_matching(predicate, save_dir, index, candidates, download, incoming_text="", forbidden_md5s=(),
                   max_downloads=40, max_pool=120, max_consecutive_fails=8, log=None):
    """
    沒有庫存時立即上網抓：依序下載候選圖並辨識。辨識不出來的立刻刪掉；符合條件 (且不是對方那張) 的就停止並回傳；
    辨識得出但不符合的先留著當庫存 (圖庫超過 max_pool 張就刪掉)。
    candidates: (網址, 來源類別) 的可迭代；download(url, save_dir, 已有的md5集合) -> 存檔路徑或 None。
    回傳 LibraryImage 或 None。
    """
    forbidden = set(forbidden_md5s)
    known_md5s = {file_md5(p) for p in list_image_files(save_dir)}
    downloads = 0
    fails = 0
    for url, _source_category in candidates:
        if downloads >= max_downloads:
            break
        if fails >= max_consecutive_fails:
            if log:
                log(" -> [網路保護] 連續下載失敗次數過多，停止立即抓圖。")
            break
        saved = download(url, save_dir, known_md5s)
        if not saved:
            fails += 1
            continue
        fails = 0
        downloads += 1
        md5 = file_md5(saved)
        try:
            kind, text = index.classify(saved, md5)
        except Exception:
            kind, text = None, ""
        if kind is None:
            _remove(saved)
            if md5:
                index.forget(md5)
            continue
        image = LibraryImage(saved, md5, kind, text)
        if predicate(image) and md5 not in forbidden and not texts_similar(text, incoming_text):
            return image
        if len(list_image_files(save_dir)) > max_pool:
            _remove(saved)
    return None


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass
