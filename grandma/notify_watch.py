"""監看 Windows 通知資料庫，找出指定好友傳照片的 LINE 通知。

只讀通知，不會打開聊天室，所以對方不會看到已讀。
"""
import argparse
import logging
import os
import shutil
import sqlite3
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_DB = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Notifications\wpndatabase.db")

QUERY = """
SELECT n.Id, n.Payload, h.PrimaryId
FROM Notification n JOIN NotificationHandler h ON n.HandlerId = h.RecordId
WHERE n.Id > ? AND n.Type = 'toast'
ORDER BY n.Id
"""


@dataclass
class Toast:
    id: int
    app_id: str
    texts: list


def parse_toast(payload) -> list:
    """從 toast XML 取出所有 <text> 內容，第一個通常是寄件人。"""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, TypeError):
        return []
    return [t.text.strip() for t in root.iter("text") if t.text and t.text.strip()]


def is_photo_from(texts, friend_name, keywords) -> bool:
    if not texts or texts[0] != friend_name:
        return False
    body = " ".join(texts[1:])
    return any(k in body for k in keywords)


class NotifyWatcher:
    def __init__(self, friend_name, keywords, app_id_contains="line", db_path=DEFAULT_DB):
        self.friend_name = friend_name
        self.keywords = keywords
        self.app_id_contains = app_id_contains.lower()
        self.db_path = str(db_path)
        # 從現在開始看，不處理啟動前就存在的舊通知
        self.last_seen_id = self._max_id()

    def _connect(self, path, readonly=True):
        if readonly:
            return sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=2)
        return sqlite3.connect(path, timeout=2)

    def _fetch(self, sql, params=()):
        try:
            with self._connect(self.db_path) as conn:
                return conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # Windows 鎖住資料庫時，複製一份出來讀
            with tempfile.TemporaryDirectory() as tmp:
                copy = os.path.join(tmp, "wpn.db")
                for suffix in ("", "-wal", "-shm"):
                    if os.path.exists(self.db_path + suffix):
                        shutil.copy2(self.db_path + suffix, copy + suffix)
                conn = self._connect(copy, readonly=False)
                try:
                    return conn.execute(sql, params).fetchall()
                finally:
                    conn.close()

    def _max_id(self) -> int:
        rows = self._fetch("SELECT MAX(Id) FROM Notification")
        return rows[0][0] or 0

    def new_line_toasts(self) -> list:
        rows = self._fetch(QUERY, (self.last_seen_id,))
        toasts = []
        for nid, payload, app_id in rows:
            self.last_seen_id = max(self.last_seen_id, nid)
            if self.app_id_contains in (app_id or "").lower():
                toasts.append(Toast(nid, app_id, parse_toast(payload)))
        return toasts

    def poll(self) -> int:
        """回傳這次檢查到幾則「好友傳照片」的通知。"""
        count = 0
        for toast in self.new_line_toasts():
            if is_photo_from(toast.texts, self.friend_name, self.keywords):
                log.info("收到照片通知 (id=%s)", toast.id)
                count += 1
        return count


def main():
    from grandma.config import load_config

    parser = argparse.ArgumentParser(description="單獨測試通知監看")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--verbose", action="store_true", help="印出所有 LINE 通知的 App ID 和內容")
    args = parser.parse_args()
    cfg = load_config(args.config)

    watcher = NotifyWatcher(cfg["friend_name"], cfg["photo_keywords"], cfg["line_app_id_contains"])
    print(f"開始監看「{cfg['friend_name']}」的照片通知，Ctrl+C 結束")
    while True:
        for toast in watcher.new_line_toasts():
            matched = is_photo_from(toast.texts, cfg["friend_name"], cfg["photo_keywords"])
            sender = toast.texts[0] if toast.texts else "?"
            print(f"[{'照片!' if matched else '忽略'}] 寄件人={sender}")
            if args.verbose:
                print(f"    app_id={toast.app_id}\n    texts={toast.texts}")
        time.sleep(cfg["poll_interval_sec"])


if __name__ == "__main__":
    main()
