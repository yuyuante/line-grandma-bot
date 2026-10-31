"""
每個聊天室每天最多回覆幾次。以本機日期計算 (午夜 00:00 重置)，存在 JSON 檔，重啟程式不會歸零。
只計「實際送出」的回覆：因為不是長輩圖、辨識不出來而略過的，不算。
"""
import json
import os
import tempfile
import threading
import time

DEFAULT_MAX_PER_DAY = 3
_lock = threading.Lock()


def _today(now=None):
    return time.strftime("%Y-%m-%d", time.localtime(now if now is not None else time.time()))


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path, data):
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def count_today(path, chat, now=None):
    """該聊天室今天已經回覆的次數"""
    with _lock:
        entry = _load(path).get(chat)
    if isinstance(entry, dict) and entry.get("date") == _today(now) and isinstance(entry.get("count"), int):
        return entry["count"]
    return 0


def can_reply(path, chat, max_per_day, now=None):
    """今天還能不能回覆。max_per_day 為 0 或負數表示不限次數"""
    return max_per_day <= 0 or count_today(path, chat, now) < max_per_day


def record_reply(path, chat, now=None):
    """記下一次實際送出的回覆，回傳今天的累計次數"""
    with _lock:
        data = _load(path)
        today = _today(now)
        entry = data.get(chat)
        count = entry["count"] if isinstance(entry, dict) and entry.get("date") == today and isinstance(entry.get("count"), int) else 0
        data = {name: e for name, e in data.items()
                if isinstance(e, dict) and e.get("date") == today}       # 順便清掉過期的舊紀錄
        data[chat] = {"date": today, "count": count + 1}
        _save(path, data)
        return count + 1
