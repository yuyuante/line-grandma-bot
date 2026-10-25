"""長輩圖圖庫：存放圖片的 pHash，挑出和收到的圖不一樣的那一張。"""
import random
import sqlite3
from datetime import datetime
from pathlib import Path

import imagehash

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY,
    filename TEXT UNIQUE NOT NULL,
    phash TEXT NOT NULL,
    keyword TEXT,
    score REAL,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sent_log (
    id INTEGER PRIMARY KEY,
    image_id INTEGER NOT NULL REFERENCES images(id),
    sent_at TEXT NOT NULL
);
"""

# (開始小時, 結束小時, 關鍵字)：這個時段優先挑關鍵字符合的圖
TIME_KEYWORDS = [(4, 11, "早安"), (11, 14, "午安"), (18, 24, "晚安"), (0, 4, "晚安")]
TIME_WEIGHT = 3


def phash(img):
    return imagehash.phash(img.convert("RGB"))


def time_keyword(hour):
    for start, end, kw in TIME_KEYWORDS:
        if start <= hour < end:
            return kw
    return None


class Library:
    def __init__(self, db_path, library_dir):
        self.library_dir = Path(library_dir)
        self.library_dir.mkdir(parents=True, exist_ok=True)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(SCHEMA)

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]

    def has_similar(self, h, max_distance) -> bool:
        for (stored,) in self.conn.execute("SELECT phash FROM images"):
            if imagehash.hex_to_hash(stored) - h <= max_distance:
                return True
        return False

    def add(self, filename, h, keyword=None, score=None):
        self.conn.execute(
            "INSERT INTO images (filename, phash, keyword, score, added_at) VALUES (?, ?, ?, ?, ?)",
            (filename, str(h), keyword, score, datetime.now().isoformat()),
        )
        self.conn.commit()

    def pick_different(self, received, min_distance=12, recent_n=30, now=None):
        """挑一張和 received 差距夠大、最近沒傳過的圖，回傳路徑；圖庫沒圖時回傳 None。"""
        now = now or datetime.now()
        rh = phash(received)
        recent = {
            r[0]
            for r in self.conn.execute("SELECT image_id FROM sent_log ORDER BY id DESC LIMIT ?", (recent_n,))
        }
        different = [
            (iid, filename, keyword)
            for iid, filename, stored, keyword in self.conn.execute(
                "SELECT id, filename, phash, keyword FROM images"
            )
            if imagehash.hex_to_hash(stored) - rh >= min_distance and (self.library_dir / filename).exists()
        ]
        candidates = [c for c in different if c[0] not in recent] or different  # 圖庫太小時放寬
        if not candidates:
            return None

        kw = time_keyword(now.hour)
        weights = [TIME_WEIGHT if kw and kw in (c[2] or "") else 1 for c in candidates]
        _, filename, _ = random.choices(candidates, weights=weights)[0]
        return self.library_dir / filename

    def mark_sent(self, path, now=None):
        now = now or datetime.now()
        row = self.conn.execute("SELECT id FROM images WHERE filename = ?", (Path(path).name,)).fetchone()
        if row:
            self.conn.execute("INSERT INTO sent_log (image_id, sent_at) VALUES (?, ?)", (row[0], now.isoformat()))
            self.conn.commit()

    def sent_today(self, now=None) -> int:
        now = now or datetime.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        return self.conn.execute("SELECT COUNT(*) FROM sent_log WHERE sent_at >= ?", (start,)).fetchone()[0]
