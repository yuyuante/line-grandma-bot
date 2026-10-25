"""讀取 config.yaml，補上預設值，並把相對路徑轉成以設定檔所在資料夾為準。"""
from pathlib import Path

import yaml

DEFAULTS = {
    "friend_name": "",
    "dry_run": True,
    "delay_range_sec": [60, 480],
    "daily_limit": 5,
    "quiet_hours": [23, 7],
    "clip_threshold": 0.6,
    "phash_min_distance": 12,
    "recent_exclude": 30,
    "user_idle_sec": 60,
    "poll_interval_sec": 3,
    "photo_keywords": ["傳送了照片", "傳送了圖片", "sent a photo"],
    "line_app_id_contains": "line",
    "library_dir": "data/library",
    "db_path": "data/grandma.db",
    "templates_dir": "templates",
    "debug_dir": "data/debug",
    "ui": {
        "main_window_title": "LINE",
        "match_threshold": 0.8,
        "open_chat_keys": ["enter"],
        "send_keys": ["enter"],
        "close_keys": ["esc"],
        "minimize_after": True,
        "chat_header_px": 70,
        "chat_left_px": 0,
        "input_margin_px": 20,
    },
    "crawler": {
        "keywords": ["早安圖", "晚安圖", "平安喜樂 圖"],
        "min_side": 300,
        "dup_distance": 6,
    },
}

PATH_KEYS = ("library_dir", "db_path", "templates_dir", "debug_dir")


def load_config(path="config.yaml") -> dict:
    path = Path(path).resolve()
    with open(path, encoding="utf-8") as f:
        user = yaml.safe_load(f) or {}

    cfg = {**DEFAULTS, **user}
    for section in ("ui", "crawler"):
        cfg[section] = {**DEFAULTS[section], **(user.get(section) or {})}

    for key in PATH_KEYS:
        p = Path(cfg[key])
        cfg[key] = p if p.is_absolute() else path.parent / p
    cfg["base_dir"] = path.parent
    return cfg
