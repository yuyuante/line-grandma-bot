# -*- coding: utf-8 -*-
"""
LINE Windows 桌面自動回覆長輩圖腳本
- 支援多位聯絡人名單 (config.json)
- 支援隨機間隔時間回覆 (min_delay_seconds ~ max_delay_seconds)，避免固定規律
- 支援隨機挑選且「不重複」長輩圖歷史記憶 (avoid_repeat_count)
- 支援觸發模式：只在對方傳照片時回覆 (trigger_mode: "photo_only" 或 "all")
- 送出圖片後自動最小化對話視窗，避免保持在前景導致持續已讀
"""

import sys
import os
import json
import time
import random
from datetime import datetime, date as _date, timedelta as _timedelta
import glob
import argparse
import hashlib
import shutil
import subprocess
import logging
import tempfile
import ctypes
import ctypes.wintypes
import threading
from io import BytesIO
import re
import contextlib
import collections
import difflib

import win32gui
import win32con
import win32process
import win32clipboard
import psutil
from PIL import Image, ImageChops, ImageGrab, ImageOps, ImageStat
import pyautogui

import notifier
import view_binder
import ocr_engine
import image_kind
import image_library
import reply_budget
import crawl_images
import chat_opener
from crawl_images import (crawl_grandma_images, clean_expired_images, retire_image,
                          download_and_validate_image, get_existing_md5s, build_candidate_urls,
                          build_candidates, normalize_sources)

VERSION = "2.16.0"

# 0. 全系統單一執行個體鎖 (Windows Named Mutex)，防止重複啟動繞過斷路器
_app_mutex_handle = None
def ensure_single_instance():
    global _app_mutex_handle
    if _app_mutex_handle is not None:
        return
    mutex_name = "Local\\LineGrandmaBot_SingleInstance_Mutex"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, mutex_name)
    last_err = ctypes.get_last_error()
    if not handle or handle == 0:
        log_v("❌ [致命錯誤] 無法建立系統互斥鎖 (CreateMutexW 失敗)！為確保系統安全，本程式立即安全退出。")
        sys.exit(1)
    ERROR_ALREADY_EXISTS = 183
    if last_err == ERROR_ALREADY_EXISTS:
        log_v("❌ [致命衝突] 偵測到已有另一個 auto_reply 程式在執行中！為防重複發送與繞過斷路器，本程式立即安全退出。")
        sys.exit(1)
    _app_mutex_handle = handle

# 1. 啟用 Windows Per-Monitor V2 DPI 感知，防止高解析度縮放 (125%/150%) 導致點擊座標偏移
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# 2. 紀錄檔：每天一個檔 (logs/auto_reply_YYYY-MM-DD.log)，內容與畫面相同，只保留 log_keep_days 天；寫檔失敗絕不影響機器人
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_FILE_PATTERN = re.compile(r"^auto_reply_(\d{4}-\d{2}-\d{2})\.log$")
_LOG_FILE_LOCK = threading.Lock()          # 啟動時的背景辨識執行緒也會寫 log，兩行不能交錯
_file_log_settings = {"enabled": False, "keep_days": 7}      # 啟動後由 main() 的 configure_file_log 打開；匯入模組與單元測試都不會碰到真的 logs/
_file_log_state = {"pruned_day": None}


def _notifier_log_line(text):
    """notifier 自己印的 [通知] 提示 (沒有權杖、被 LINE 拒絕、額度用完…) 也寫進紀錄檔"""
    _write_log_file(f"[{time.strftime('%H:%M:%S')}][v{VERSION}] {text}")


def configure_file_log(config):
    """依設定開關檔案紀錄與保留天數 (主迴圈每輪呼叫，改 config.json 即時生效)"""
    _file_log_settings["enabled"] = bool(config.get("log_to_file", True))
    try:
        _file_log_settings["keep_days"] = max(1, int(config.get("log_keep_days", 7)))
    except (TypeError, ValueError):
        _file_log_settings["keep_days"] = 7


def _prune_old_logs(keep_days, today):
    """刪掉 logs/ 底下「檔名符合 auto_reply_日期.log 且日期早於保留期限」的檔；其他檔名、子資料夾一律不碰。today 是 datetime.date"""
    try:
        names = os.listdir(LOG_DIR)
    except OSError:
        return
    oldest_kept = today - _timedelta(days=keep_days - 1)
    for name in names:
        m = LOG_FILE_PATTERN.match(name)
        path = os.path.join(LOG_DIR, name)
        if not m or not os.path.isfile(path):
            continue
        try:
            if _date.fromisoformat(m.group(1)) < oldest_kept:
                os.remove(path)
        except (ValueError, OSError):
            pass


def _write_log_file(text):
    """把一行紀錄附加到今天的紀錄檔；任何例外一律吞掉 (最多少一行，不能拖垮機器人)"""
    if not _file_log_settings["enabled"]:
        return
    try:
        with _LOG_FILE_LOCK:
            day = time.strftime("%Y-%m-%d")
            os.makedirs(LOG_DIR, exist_ok=True)
            if _file_log_state["pruned_day"] != day:
                _file_log_state["pruned_day"] = day
                _prune_old_logs(_file_log_settings["keep_days"], _date.fromisoformat(day))
            with open(os.path.join(LOG_DIR, f"auto_reply_{day}.log"), "a", encoding="utf-8") as f:
                f.write(text + "\n")
    except Exception:
        pass


def banner_print(text=""):
    """啟動橫幅：印在畫面，也寫進紀錄檔"""
    print(text)
    _write_log_file(text)


class _FileLogHandler(logging.Handler):
    """logging 模組的 WARNING 以上 (設定錯誤這類) 也寫進同一個紀錄檔"""
    def emit(self, record):
        try:
            _write_log_file(self.format(record))
        except Exception:
            pass


# 3. 設定標準日誌輸出 (包含版本號碼)
logging.basicConfig(
    level=logging.INFO,
    format=f"[%(asctime)s][v{VERSION}] %(message)s",
    datefmt="%H:%M:%S"
)
_file_handler = _FileLogHandler(level=logging.WARNING)
_file_handler.setFormatter(logging.Formatter(f"[%(asctime)s][v{VERSION}] %(message)s", datefmt="%H:%M:%S"))
logging.getLogger().addHandler(_file_handler)

ISOLATION_LOG_INTERVAL = 60.0              # 相同的 [安全隔離] 訊息最短間隔 (秒)
_isolation_last = {}                       # 訊息樣式 -> (上次印出的 monotonic 時間, 之後被略過的次數)


def throttle_isolation(msg, now=None):
    """
    [安全隔離] 訊息每個輪詢週期都可能重複 (每秒數次)，相同內容 (數字視為相同) 在間隔內只印一次。
    回傳要印出的文字，被節流就回傳 None；重新印出時註明中間略過幾次。其他訊息原樣回傳。
    """
    if not msg.lstrip("\n").startswith("[安全隔離]"):
        return msg
    now = time.monotonic() if now is None else now
    key = re.sub(r"[\d.+\-:]+", "#", msg)
    last, skipped = _isolation_last.get(key, (None, 0))
    if last is not None and now - last < ISOLATION_LOG_INTERVAL:
        _isolation_last[key] = (last, skipped + 1)
        return None
    _isolation_last[key] = (now, 0)
    return f"{msg} (其間略過 {skipped} 次相同訊息)" if skipped else msg


def log_v(msg):
    """印出帶有時間戳記與版本號碼的訊息"""
    msg = throttle_isolation(msg)
    if msg is None:
        return
    now = time.strftime("%H:%M:%S")
    prefix = ""
    if msg.startswith("\n"):
        prefix = "\n"
        msg = msg[1:]
    full_text = f"{prefix}[{now}][v{VERSION}] {msg}"
    try:
        print(full_text)
    except UnicodeEncodeError:
        try:
            enc = sys.stdout.encoding or "utf-8"
            print(full_text.encode(enc, errors="replace").decode(enc, errors="replace"))
        except Exception:
            print(full_text.encode("ascii", errors="replace").decode("ascii", errors="replace"))
    _write_log_file(full_text)           # 紀錄檔 (log_to_file)：與畫面同樣的內容
    try:
        notifier.on_log(msg)             # 警告/錯誤標籤開頭的訊息 (已啟用 notify_log 時) 同步推播到 Notify Bot
    except Exception:
        pass

# 設定檔與歷史記錄路徑
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
HISTORY_PATH = os.path.join(SCRIPT_DIR, "contact_history.json")
IMAGE_TAGS_PATH = os.path.join(SCRIPT_DIR, "image_tags.json")
CHAT_MAP_PATH = os.path.join(SCRIPT_DIR, "chat_dirs.json")
IMAGE_KINDS_PATH = os.path.join(SCRIPT_DIR, "image_kinds.json")        # 每張圖的性質 (辨識過一次就快取)
REPLY_COUNTS_PATH = os.path.join(SCRIPT_DIR, "reply_counts.json")      # 每個聊天室今天已回覆幾次
NOTIFY_STATE_PATH = os.path.join(SCRIPT_DIR, "notify_state.json")   # 本月已推播則數 (見 notifier.py)

def atomic_save_json(file_path, data):
    """使用暫存檔與原子替換 (os.replace)，防止當機或意外中斷損毀 JSON 檔案"""
    dirname, basename = os.path.split(file_path)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=dirname, delete=False, encoding="utf-8") as tf:
            temp_name = tf.name
            json.dump(data, tf, ensure_ascii=False, indent=2)
            tf.flush()
            os.fsync(tf.fileno())
        for attempt in range(3):
            try:
                os.replace(temp_name, file_path)
                return True
            except PermissionError:
                # Windows 上其他行程讀取目標檔時 os.replace 會暫時拋出 PermissionError (WinError 5/32)
                if attempt == 2:
                    raise
                time.sleep(0.1 * (attempt + 1))
    except Exception as e:
        logging.warning(f"原子寫入 {file_path} 失敗: {e}")
        if temp_name and os.path.exists(temp_name):
            try:
                os.remove(temp_name)
            except Exception:
                pass
        return False

_WARNED_ONCE = set()            # 已印過的一次性警告
STOP_COMMAND_MIN_LEN = 4        # stop_command 去除空白與標點後的最少字數，太短 (例如單字) 會讓一般對話誤停程式

DEFAULT_CONFIG = {
    "targets": [],
    "images_dir": "images",
    "trigger_mode": "photo_and_sticker", # "photo_and_sticker" (照片與貼圖都會觸發偵測；貼圖要寫著問候字才回，見 reply_to_sticker_text)、"photo_only" (只回照片) 或 "all" (文字、貼圖與照片都會觸發偵測)
    "min_delay_seconds": 5,             # 隨機延遲下限 (秒)
    "max_delay_seconds": 900,           # 隨機延遲上限 (秒，最長 15 分鐘)
    "cooldown_seconds": 30,             # 防洗版冷卻時間 (秒)
    "history_rounds": 100,              # 百輪內不重複送同一張圖或對方曾送過的圖
    "replace_after_send": True,
    "collect_incoming_images": True,
    "image_ttl_days": 7,
    "min_image_pool": 10,
    "auto_crawl_images": True,
    "close_window_after_send": True,
    "time_aware_images": True,          # 依時段挑圖：早上不送晚安圖、晚上不送早安圖 (標記為 neutral 的無時段字樣圖不受限)
    "stop_command": "結束長輩圖機器人",   # 停止指令：你在 stop_command_chats 內的聊天室自己發了含此文字的訊息，程式立即結束
    "stop_command_chats": [],            # 接受你下停止指令的聊天室名單 (預設空白 = 停用)
    "max_replies_per_day": 3,           # 每個聊天室每天最多回覆幾次 (本機午夜重置，只計實際送出的；0 = 不限次數)
    "max_image_pool": 120,              # 圖庫上限張數：即時抓圖時辨識得出但用不到的圖，超過這個數量就不留
    "instant_crawl_max_downloads": 40,  # 圖庫沒有合適的圖時，立即上網抓圖最多下載幾張 (節日圖加倍)
    "image_sources": [],                # 圖庫來源 (網址範本、編號範圍、選填時段類別，見 crawl_images.py)；空白 = 預設的 Formosa 貼圖酷每日問候攝影與插畫
    "require_grandma_image": True,      # 對方傳來的圖要先辨識為長輩圖才回覆；貼圖、生活照、無法辨識的一律不理會
    "reply_to_text": True,              # 對方傳來的「短文字訊息」(例如早安、中秋節快樂) 屬於本專案處理的性質時，也回同性質但不同張的圖 (只限一對一；讀文字要把聊天視窗帶到前景)
    "reply_to_sticker_text": True,      # 對方傳來的「貼圖」上若寫著短問候或祝福 (例如早安安)，OCR 讀出來也回同性質但不同張的圖；其他貼圖照舊略過 (只限一對一；群組一律不回貼圖)。設定檔「沒寫」這一項時 load_config 會讓它跟著 reply_to_text (關掉文字回覆的人不會被貼圖打擾)；明確寫了就照寫的
    "birthday_reply": "謝謝",           # 一對一收到「生日快樂」的圖或短文字時，只回這句話 (不回圖)；空字串 = 不特別處理，照一般節日圖回覆。群組不適用
    "text_reply_max_chars": 30,         # 文字訊息超過這麼多字 (去掉空白與標點) 就當作一般聊天，不回圖
    "auto_neutral_images": False,       # 【開發中，預設關閉】沒有符合目前時段的圖時，用 OCR 讀得到字、但沒有問候語與節慶字樣的圖代替。實測 OCR 常讀不出藝術字的問候語，開啟會把早安圖誤判成無時段
    "group_targets": [],                # 要回覆的群組名單 (名稱同 targets 的寫法，不含人數)；群組另有更保守的規則，只能手動綁定快取頻道 (--bind-view)
    "group_max_replies_per_day": 1,     # 每個群組每天最多回覆幾次 (0 = 不限次數)
    "group_cooldown_seconds": 600,      # 同一個群組兩次回覆之間至少間隔幾秒 (多人連續傳圖時只回一次)
    "close_opened_windows": True,       # 機器人自己從主視窗開出來的聊天視窗，回覆完或綁定完直接關閉 (不碰你本來就開著的視窗，也不碰文字偵測正在監看的視窗)
    "auto_bind_by_viewing": True,       # 未綁定的好友/群組傳新圖時，自動開他的視窗、點開一張照片，確認快取頻道後綁定，再把視窗放回去 (關閉則維持手動 --bind-view)
    "auto_open_chat_window": True,      # 好友的獨立聊天視窗不見時，自動從 LINE 主視窗的搜尋欄開回來 (關閉則找不到視窗就略過該則訊息)
    "log_to_file": True,                # 程式自己寫紀錄檔：logs/auto_reply_日期.log，每天一個檔，內容與畫面相同 (不含聊天內容)
    "log_keep_days": 7,                 # 紀錄檔保留幾天，更早的自動刪除
    "notify_log": False,                # 把啟動、回覆、停止與警告/錯誤推播到 LINE 官方帳號 (需 ~/.line-notify.env，見 notifier.py)
    "notify_monthly_cap": 150,          # 每月最多推播則數 (官方帳號免費方案上限 200，留一些給手動測試)
    "notify_fallback_chat": "Notify Bot"  # 推播額度用完時 (LINE 回 429 或到 notify_monthly_cap)，改從你自己的帳號把通知送進這個聊天室；視窗要已開著，沒開就只寫紀錄檔；空字串 = 不備援
}

last_reply_times = {}
_last_valid_config = None

BIRTHDAY_REPLY_MAX_CHARS = 20      # 生日回覆的字數上限：這是要「打」進輸入框的一句話，不是長篇大論


def birthday_reply_text(config, target_name=None):
    """一對一收到生日快樂時要回的那句話；空字串表示不特別處理。群組 (target_name 在 group_targets) 一律回空字串"""
    if target_name is not None and is_group_target(target_name):
        return ""
    return str(config.get("birthday_reply", DEFAULT_CONFIG["birthday_reply"]) or "").strip()


def configure_notifier(config):
    """依設定啟用或停用 Notify Bot 推播 (主迴圈每輪呼叫，改 config.json 即時生效)"""
    notifier.configure(config.get("notify_log", False), config.get("notify_monthly_cap", DEFAULT_CONFIG["notify_monthly_cap"]), NOTIFY_STATE_PATH)
    chat = str(config.get("notify_fallback_chat", DEFAULT_CONFIG["notify_fallback_chat"]) or "").strip()
    if chat and is_group_target(chat):
        if "fallback_group" not in _WARNED_ONCE:
            _WARNED_ONCE.add("fallback_group")
            logging.warning(f"notify_fallback_chat「{chat}」同時是群組，不能當備援聊天室，備援已關閉。")
        chat = ""
    with _FALLBACK_LOCK:
        _FALLBACK["chat"] = chat
        if not chat:
            _FALLBACK["pending"].clear()
            _FALLBACK["failures"] = 0
    notifier.fallback_sink = queue_fallback_notice if chat else None


FALLBACK_MIN_INTERVAL = 300.0    # 備援訊息兩次嘗試送進聊天室的最短間隔 (秒)：累積幾則合併成一則，不要一直打擾你
FALLBACK_FAILURE_INTERVAL = 900.0   # 送不進去 (輸入框有草稿、焦點被搶走) 之後拉長到這個間隔再試，不要每 5 分鐘打擾一次
FALLBACK_OPEN_RETRY = 300.0      # 備援聊天室自動開窗，失敗後最短隔多久才再試 (每次都要動到主視窗)
FALLBACK_WINDOW_RECHECK = 30.0   # 聊天室視窗沒開時，多久才再找一次 (找視窗要掃行程與視窗列表，不必每輪都做)
FALLBACK_MAX_PENDING = 200       # 最多暫存幾則 (超過丟最舊的，完整紀錄仍在 logs/ 檔案)
FALLBACK_MAX_CHARS = 1200        # 合併後單則訊息字數上限
_FALLBACK = {"chat": "", "pending": [], "last": 0.0, "window_checked": 0.0, "failures": 0}
_FALLBACK_LOCK = threading.Lock()
_EMOJI_RE = re.compile(r"[\U00010000-\U0010ffff\u2600-\u27bf\ufe0f\u200d]")      # 表情符號、符號區 (警告三角形、打勾等) 與變體選擇符：輸入框可能改寫它們，造成讀回比對不一致


def queue_fallback_notice(text):
    """推播額度用完時，notifier 把通知交到這裡暫存 (背景執行緒呼叫)；備援沒開就丟掉"""
    with _FALLBACK_LOCK:
        if not _FALLBACK["chat"]:
            return
        _FALLBACK["pending"].append(str(text))
        del _FALLBACK["pending"][:-FALLBACK_MAX_PENDING]


def _first_safe(candidates, phrase):
    """候選文字裡第一個「不像停止指令」的；全部都像就用最後一個 (最短、最不可能含任何指令的省略號)"""
    for candidate in candidates:
        if not phrase or not text_matches_stop_command(candidate, phrase):
            return candidate
    return candidates[-1]


def _fallback_message(lines, stop_phrase, plain=False):
    """
    把暫存的通知合併成一則，回傳 (文字, 用掉幾則)。換行改成「／」。
    **停止指令那句話一律換掉**：停止指令是「你自己發的訊息裡含那句話就停」，備援訊息正是從你的帳號送出，
    通知文字 (例如啟動說明) 裡若帶著那句話，機器人會把自己停掉。換掉用的文字本身也要不像停止指令
    (停止指令若剛好是「停止指令」四個字，括號裡的「停止指令」就還是會中)，所以每個替代文字都逐一核對。
    plain 為 True 時去掉表情符號與變體選擇符 (前一次因為讀回不一致送不進去時改用)；去除要在核對之前做
    (去掉夾在中間的符號，才不會拼出停止指令)。
    """
    phrase = (stop_phrase or "").strip()
    repl = _first_safe(["（停止指令）", "（已省略）", "…"], phrase)
    omitted = _first_safe(["（這則通知含停止指令字樣，已省略）", "（已省略）", "…"], phrase)

    def safe(line):
        line = str(line)
        if plain:
            line = re.sub(r"[ \t]{2,}", " ", _EMOJI_RE.sub("", line))
        line = re.sub(r"\s*[\r\n]+\s*", " ／ ", line).strip()
        if phrase:
            line = line.replace(phrase, repl)
            if text_matches_stop_command(line, phrase):
                return omitted
        return line

    header = "[備援] " if plain else "📝[備援] "
    picked, size = [], len(header)
    for line in lines:
        piece = safe(line)
        if picked and size + len(piece) + 3 > FALLBACK_MAX_CHARS:
            break
        picked.append(piece[:FALLBACK_MAX_CHARS])
        size += len(piece) + 3
    text = header + " ｜ ".join(picked)
    if phrase and text_matches_stop_command(text, phrase):
        text = _first_safe([header + "（通知內容含停止指令字樣，已省略）", "（已省略）", "…"], phrase)
    return text, len(picked)


def flush_fallback_notices(config, force=False):
    """
    推播額度用完後，把暫存的通知從你自己的帳號送進 notify_fallback_chat (主迴圈每輪呼叫)。
    聊天室視窗沒開時照 auto_open_chat_window 自動從主視窗開出來 (開出來的不登記成「用完要關」，會一直留著，最小化)；
    最多每 FALLBACK_MIN_INTERVAL 秒送一則合併的訊息；送完照 close_window_after_send 放回去。送不進去 (輸入框有草稿、焦點被搶走、讀回不一致) 就保留，
    間隔拉長到 FALLBACK_FAILURE_INTERVAL 再試，而且改送去掉表情符號的版本。
    force 為 True (程式結束前最後一次) 時不看間隔，把最後的通知 (例如「已停止」) 盡量送出去。
    """
    with _FALLBACK_LOCK:
        chat = _FALLBACK["chat"]
        lines = list(_FALLBACK["pending"])
    if not chat or not lines or is_group_target(chat):
        return
    now = time.time()
    if not force and (now - _FALLBACK["last"] < FALLBACK_MIN_INTERVAL or now - _FALLBACK["window_checked"] < FALLBACK_WINDOW_RECHECK):
        return
    matched = find_target_chat_window(chat)
    if (not matched and not force and config.get("auto_open_chat_window", True)       # 結束前的最後一次不再去動主視窗
            and now - _FALLBACK.get("open_tried", -1e9) >= FALLBACK_OPEN_RETRY):
        _FALLBACK["open_tried"] = now
        opened = open_missing_chat_window(chat, config)
    else:
        opened = False
    if opened:
        matched = find_target_chat_window(chat)
        for w in matched:
            _BOT_OPENED_HWNDS.pop(w[0], None)           # 備援聊天室要一直留著 (停止指令偵測也要有這個視窗)，不登記成「用完要關」
    if not matched:
        _FALLBACK["window_checked"] = now               # 視窗沒開、也開不起來：通知只留在 logs/ 檔案，稍後才再找
        return
    _FALLBACK["window_checked"] = 0.0
    _FALLBACK["last"] = now
    text, used = _fallback_message(lines, config.get("stop_command", ""), plain=_FALLBACK["failures"] > 0)
    hwnd = matched[0][0]
    sent = False
    notifier.fallback_busy = True
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.3)
        activate_window(hwnd)
        time.sleep(0.4)
        if win32gui.GetForegroundWindow() == hwnd and title_matches_target(win32gui.GetWindowText(hwnd), chat):
            sent = send_text_via_input(hwnd, text)
    except Exception as e:
        log_v(f" -> [通知備援] 送進「{chat}」時發生例外: {e}")           # 刻意不用 [警告] 標籤：那種行會再被推播/備援
    finally:
        _minimize_if_configured(hwnd, config)
        notifier.fallback_busy = False
    if sent:
        _FALLBACK["failures"] = 0
        with _FALLBACK_LOCK:
            for item in lines[:used]:
                try:
                    _FALLBACK["pending"].remove(item)
                except ValueError:
                    pass                                    # 送出期間已被裁掉的舊通知
        log_v(f" -> [通知備援] 推播額度已用完，已改從你的帳號把 {used} 則通知送進「{chat}」。")
    else:
        _FALLBACK["failures"] += 1
        _FALLBACK["last"] = now - FALLBACK_MIN_INTERVAL + FALLBACK_FAILURE_INTERVAL
        log_v(f" -> [通知備援] 暫時送不進「{chat}」，{int(FALLBACK_FAILURE_INTERVAL)} 秒後再試 (已暫存 {len(lines)} 則)。")


def flush_fallback_notices_at_exit(config):
    """程式結束前最後一次把備援暫存的通知 (例如「已停止」) 送出去；任何錯誤都不影響結束流程"""
    try:
        flush_fallback_notices(config, force=True)
    except Exception:
        pass

def load_config():
    global _last_valid_config
    if not os.path.exists(CONFIG_PATH):
        atomic_save_json(CONFIG_PATH, DEFAULT_CONFIG)
        _last_valid_config = DEFAULT_CONFIG.copy()
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError("config.json 格式錯誤：根節點必須是物件 (dict)")

        # 嚴格 Schema 與型別驗證
        sanitized = DEFAULT_CONFIG.copy()
        if "targets" in cfg:
            if isinstance(cfg["targets"], list):
                sanitized["targets"] = [str(t).strip() for t in cfg["targets"] if str(t).strip()]
            else:
                logging.warning("config.json 中的 'targets' 必須是清單格式，暫時設為空名單。")
                sanitized["targets"] = []
        else:
            sanitized["targets"] = []

        groups = []
        if isinstance(cfg.get("group_targets"), list):
            groups = [str(t).strip() for t in cfg["group_targets"] if str(t).strip()]
        sanitized["group_targets"] = groups
        for k, minimum in (("group_max_replies_per_day", 0), ("group_cooldown_seconds", 5)):
            if k in cfg:
                try:
                    sanitized[k] = max(minimum, int(cfg[k]))
                except Exception:
                    pass
        known = {t.lower() for t in sanitized["targets"]}
        sanitized["targets"] = sanitized["targets"] + [g for g in groups if g.lower() not in known]    # 群組也是監控對象，其餘流程共用
        set_group_targets(groups)

        if cfg.get("trigger_mode") in ("photo_and_sticker", "photo_only", "all"):
            sanitized["trigger_mode"] = cfg["trigger_mode"]
        try:
            min_d = max(1, int(cfg.get("min_delay_seconds", 5)))
            max_d = max(min_d, int(cfg.get("max_delay_seconds", 900)))
            sanitized["min_delay_seconds"] = min_d
            sanitized["max_delay_seconds"] = max_d
        except Exception:
            pass
        try:
            sanitized["cooldown_seconds"] = max(5, int(cfg.get("cooldown_seconds", 30)))
        except Exception:
            pass
        for k in ("history_rounds", "image_ttl_days", "min_image_pool"):
            if k in cfg:
                try:
                    sanitized[k] = max(1, int(cfg[k]))
                except Exception:
                    pass
        for k in ("replace_after_send", "collect_incoming_images", "auto_crawl_images", "close_window_after_send", "time_aware_images",
                  "require_grandma_image", "auto_neutral_images", "auto_open_chat_window", "close_opened_windows", "auto_bind_by_viewing", "reply_to_text", "reply_to_sticker_text", "log_to_file"):
            if k in cfg and isinstance(cfg[k], bool):
                sanitized[k] = cfg[k]
        if not isinstance(cfg.get("reply_to_sticker_text"), bool):
            # 設定檔沒寫 (或型別錯誤) 時，貼圖跟著文字回覆：原本為了「不想被打擾」把 reply_to_text 關掉的人，升級後視窗不會又被帶到前景
            sanitized["reply_to_sticker_text"] = sanitized.get("reply_to_text", True)
        if isinstance(cfg.get("birthday_reply"), str):
            sanitized["birthday_reply"] = cfg["birthday_reply"].strip()[:BIRTHDAY_REPLY_MAX_CHARS]
        if isinstance(cfg.get("stop_command"), str) and cfg["stop_command"].strip():
            if len(re.sub(r"[\W_]+", "", cfg["stop_command"])) >= STOP_COMMAND_MIN_LEN:
                sanitized["stop_command"] = cfg["stop_command"].strip()
            else:
                if "stop_command_short" not in _WARNED_ONCE:
                    _WARNED_ONCE.add("stop_command_short")   # 主迴圈每秒讀一次設定，只警告一次
                    logging.warning(f"stop_command 太短 (少於 {STOP_COMMAND_MIN_LEN} 字)，容易誤停，改用預設值。")
        if isinstance(cfg.get("image_sources"), list):
            if cfg["image_sources"]:
                srcs, skipped = normalize_sources(cfg["image_sources"])
                sanitized["image_sources"] = srcs
                for msg in skipped:
                    if msg not in _WARNED_ONCE:
                        _WARNED_ONCE.add(msg)            # 主迴圈每秒讀一次設定，同一則警告只印一次
                        logging.warning(f"image_sources {msg}")
        for k, minimum in (("max_replies_per_day", 0), ("max_image_pool", 1), ("instant_crawl_max_downloads", 1), ("text_reply_max_chars", 1), ("log_keep_days", 1)):
            if k in cfg:
                try:
                    sanitized[k] = max(minimum, int(cfg[k]))
                except Exception:
                    pass
        if isinstance(cfg.get("notify_log"), bool):
            sanitized["notify_log"] = cfg["notify_log"]
        try:
            sanitized["notify_monthly_cap"] = max(1, int(cfg.get("notify_monthly_cap", DEFAULT_CONFIG["notify_monthly_cap"])))
        except Exception:
            pass
        if isinstance(cfg.get("notify_fallback_chat"), str):
            sanitized["notify_fallback_chat"] = cfg["notify_fallback_chat"].strip()
        if isinstance(cfg.get("stop_command_chats"), list):
            sanitized["stop_command_chats"] = [str(t).strip() for t in cfg["stop_command_chats"] if str(t).strip()]
        if "images_dir" in cfg and isinstance(cfg["images_dir"], str):
            candidate_raw = cfg["images_dir"].strip()
            if candidate_raw and candidate_raw not in (".", ".."):
                try:
                    real_script_dir = os.path.realpath(SCRIPT_DIR)
                    resolved_img = os.path.realpath(os.path.join(real_script_dir, candidate_raw))
                    common = os.path.commonpath([real_script_dir, resolved_img])
                    if common == real_script_dir and resolved_img != real_script_dir:
                        sanitized["images_dir"] = os.path.relpath(resolved_img, real_script_dir)
                    else:
                        logging.warning(f"images_dir 試圖逃逸出專案目錄或指向根目錄 ({candidate_raw})，使用預設 'images'。")
                except (ValueError, Exception) as e:
                    logging.warning(f"images_dir 路徑無效 ({candidate_raw}): {e}，使用預設 'images'。")
        _last_valid_config = sanitized.copy()
        return sanitized
    except Exception as e:
        logging.warning(f"讀取 config.json 失敗 ({e})。")
        if _last_valid_config is not None:
            logging.warning("沿用上一份有效的設定檔內容，防止回退至非預期目標。")
            return _last_valid_config.copy()
        fallback = DEFAULT_CONFIG.copy()
        fallback["targets"] = []
        return fallback

def get_file_md5(file_path):
    """計算檔案 MD5 數位指紋"""
    try:
        with open(file_path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except Exception:
        return None

_LAST_VALID_CONTACT_HISTORY = {}

def load_contact_history():
    """讀取各聯絡人的百輪收發歷史紀錄 (具備 emergency_copy 備份載入、記憶體回退與節點級容錯保護)"""
    global _LAST_VALID_CONTACT_HISTORY
    em_path = HISTORY_PATH + ".emergency_copy"
    target_file = HISTORY_PATH
    if not os.path.exists(target_file) and os.path.exists(em_path):
        target_file = em_path

    if not os.path.exists(target_file):
        return _LAST_VALID_CONTACT_HISTORY.copy() if _LAST_VALID_CONTACT_HISTORY else {}
    try:
        with open(target_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            if target_file != em_path and os.path.exists(em_path):
                try:
                    with open(em_path, "r", encoding="utf-8") as f_em:
                        data = json.load(f_em)
                except Exception:
                    pass
            if not isinstance(data, dict):
                logging.warning("contact_history.json 格式異常 (根節點非 dict)，沿用上一份有效歷史！")
                return _LAST_VALID_CONTACT_HISTORY.copy() if _LAST_VALID_CONTACT_HISTORY else {}
        normalized = {}
        for contact, entry in data.items():
            if not isinstance(contact, str) or not contact.strip():
                continue
            fallback_c = _LAST_VALID_CONTACT_HISTORY.get(contact, {})
            if not isinstance(entry, dict):
                if isinstance(fallback_c, dict) and fallback_c:
                    normalized[contact] = fallback_c.copy()
                else:
                    normalized[contact] = {"sent": [], "received": []}
                continue
            sent_src = entry.get("sent")
            recv_src = entry.get("received")
            if isinstance(sent_src, list):
                sent = [str(x) for x in sent_src if isinstance(x, (str, int))]
            else:
                sent = fallback_c.get("sent", []).copy() if isinstance(fallback_c.get("sent"), list) else []
            if isinstance(recv_src, list):
                received = [str(x) for x in recv_src if isinstance(x, (str, int))]
            else:
                received = fallback_c.get("received", []).copy() if isinstance(fallback_c.get("received"), list) else []
            normalized[contact] = {"sent": sent, "received": received}
        _LAST_VALID_CONTACT_HISTORY = normalized.copy()
        return normalized
    except Exception as e:
        if target_file != em_path and os.path.exists(em_path):
            try:
                with open(em_path, "r", encoding="utf-8") as f_em:
                    data = json.load(f_em)
                if isinstance(data, dict):
                    normalized = {}
                    for contact, entry in data.items():
                        if isinstance(contact, str) and isinstance(entry, dict):
                            sent = [str(x) for x in entry.get("sent", []) if isinstance(x, (str, int))] if isinstance(entry.get("sent"), list) else []
                            received = [str(x) for x in entry.get("received", []) if isinstance(x, (str, int))] if isinstance(entry.get("received"), list) else []
                            normalized[contact] = {"sent": sent, "received": received}
                    _LAST_VALID_CONTACT_HISTORY = normalized.copy()
                    return normalized
            except Exception:
                pass
        logging.warning(f"讀取 contact_history.json 失敗: {e}，沿用上一份有效歷史以防重複發送！")
        return _LAST_VALID_CONTACT_HISTORY.copy() if _LAST_VALID_CONTACT_HISTORY else {}

def save_contact_history(history):
    """原子持久化儲存各聯絡人的百輪收發歷史紀錄，寫入成功時同步備份應急複本，寫入失敗時保全應急複本"""
    global _LAST_VALID_CONTACT_HISTORY
    _LAST_VALID_CONTACT_HISTORY = history.copy()
    success = atomic_save_json(HISTORY_PATH, history)
    em_path = HISTORY_PATH + ".emergency_copy"
    try:
        # 主檔寫入成功或失敗皆確保應急副本與最新記憶體歷史保持一致，杜絕主檔損毀時載入過期副本遺失去重紀錄
        atomic_save_json(em_path, history)
    except Exception:
        pass
    if not success:
        log_v("[警告] contact_history.json 寫入失敗，但已保全應急複本以確保防重複記憶不丟失！")
    return success

def record_contact_sent(target_name, md5_hash, max_rounds=100):
    if not md5_hash:
        return
    history = load_contact_history()
    if target_name not in history or not isinstance(history.get(target_name), dict):
        history[target_name] = {"sent": [], "received": []}
    if not isinstance(history[target_name].get("sent"), list):
        history[target_name]["sent"] = []
    if not isinstance(history[target_name].get("received"), list):
        history[target_name]["received"] = []
    h_str = str(md5_hash)
    if h_str not in history[target_name]["sent"]:
        history[target_name]["sent"].append(h_str)
        history[target_name]["sent"] = history[target_name]["sent"][-max_rounds:]
        save_contact_history(history)

def record_contact_received(target_name, md5_hash, max_rounds=100):
    if not md5_hash:
        return
    history = load_contact_history()
    if target_name not in history or not isinstance(history.get(target_name), dict):
        history[target_name] = {"sent": [], "received": []}
    if not isinstance(history[target_name].get("sent"), list):
        history[target_name]["sent"] = []
    if not isinstance(history[target_name].get("received"), list):
        history[target_name]["received"] = []
    h_str = str(md5_hash)
    if h_str not in history[target_name]["received"]:
        history[target_name]["received"].append(h_str)
        history[target_name]["received"] = history[target_name]["received"][-max_rounds:]
        save_contact_history(history)

def _scan_images(abs_dir):
    """掃描指定目錄下所有支援格式之圖片並去重回傳"""
    extensions = ("*.jpg", "*.jpeg", "*.png", "*.bmp", "*.webp")
    files = []
    for ext in extensions:
        files.extend(glob.glob(os.path.join(abs_dir, ext)))
        files.extend(glob.glob(os.path.join(abs_dir, ext.upper())))
    return list(set(files))

_REPLENISH_LOCK = threading.Lock()      # 啟動時的背景補庫存與送圖時的補庫存不要同時連網下載


def get_images(images_dir, auto_replenish=True, min_pool=10):
    if not os.path.isabs(images_dir):
        abs_dir = os.path.join(SCRIPT_DIR, images_dir)
    else:
        abs_dir = images_dir
    if not os.path.exists(abs_dir):
        os.makedirs(abs_dir, exist_ok=True)
    images = _scan_images(abs_dir)

    # 自動從網路補充庫存
    if auto_replenish and len(images) < min_pool:
        with _REPLENISH_LOCK:
            images = _scan_images(abs_dir)                  # 等鎖的時候別人可能已經補好了
            if len(images) < min_pool:
                needed = min_pool - len(images)
                log_v(f"[庫存提示] 目前長輩圖庫存僅 {len(images)} 張 (低於設定值 {min_pool})，自動連網下載補充 {needed} 張備用...")
                try:
                    crawl_images_now(needed, abs_dir)
                    images = _scan_images(abs_dir)
                except Exception as e:
                    log_v(f"[警告] 自動補充長輩圖失敗: {e}")

    return images

def ocr_image_text(image_path):
    """使用 Windows 內建 OCR 辨識圖片文字 (去除空白與換行)；失敗時擲出例外"""
    abs_p = os.path.abspath(image_path)
    ps_script = """
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
[Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = [System.WindowsRuntimeSystemExtensions].GetMethods() | ? { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.IsGenericMethod } | Select-Object -First 1

function Await($winRtOp, $resultType) {
    $m = $asTaskGeneric.MakeGenericMethod($resultType)
    $t = $m.Invoke($null, @($winRtOp))
    return $t.GetAwaiter().GetResult()
}

try {
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($env:LGB_OCR_IMG)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

    $lang = [Windows.Globalization.Language]::new("zh-Hant")
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
    if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
    $res = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    foreach ($line in $res.Lines) {
        Write-Output $line.Text
    }
} catch {}
"""
    cmd = ["powershell", "-NoProfile", "-Command", ps_script]
    ocr_env = os.environ.copy()
    ocr_env["LGB_OCR_IMG"] = abs_p
    proc = subprocess.run(cmd, capture_output=True, timeout=8, env=ocr_env)
    return proc.stdout.decode("utf-8", errors="ignore").replace(" ", "").replace("\r", "").replace("\n", "")

_IMAGE_SOURCES = None           # config 的 image_sources (None = 預設來源)
_AUTO_NEUTRAL = False           # config 的 auto_neutral_images

def configure_image_options(config):
    """依設定更新圖庫來源與無時段圖自動替代 (主迴圈每輪呼叫，改 config.json 即時生效)"""
    global _IMAGE_SOURCES, _AUTO_NEUTRAL
    _IMAGE_SOURCES = config.get("image_sources") or None
    _AUTO_NEUTRAL = bool(config.get("auto_neutral_images", False))

def _tag_source_category(path, category):
    """來源標明了時段的圖，下載後直接寫入時段標記，不必再靠 OCR 猜"""
    if category:
        set_image_slots(path, [category])

def _ingest_downloaded(path, category):
    """下載後立即辨識性質：辨識不出來的圖直接捨棄 (不入庫，回傳 False 讓爬蟲不計入張數)；辨識得出的留下"""
    _tag_source_category(path, category)
    try:
        kind, _ = KIND_INDEX.classify(path)
    except Exception:
        kind = None
    if kind is None:
        try:
            os.remove(path)
        except OSError:
            pass
        log_v(f" -> [辨識不出來捨棄] {os.path.basename(path)} 辨識不出性質，已捨棄。")
        return False
    return True

def crawl_images_now(target_count, save_dir):
    """依設定的來源下載新長輩圖；辨識不出性質的圖當場捨棄"""
    return crawl_grandma_images(target_count=target_count, save_dir=save_dir, sources=_IMAGE_SOURCES, on_saved=_ingest_downloaded)

OCR_VARIANT_MAX_SIDE = 1600     # 前處理後的圖最長邊上限 (太大辨識很慢)
OCR_VARIANT_MIN_SIDE = 700      # 最長邊低於此值就放大 2 倍 (小字放大後較容易辨識)

def ocr_image_text_variants(image_path):
    """
    依序產生同一張圖用不同前處理辨識出的文字：原圖、灰階拉對比、反相 (深色底白字)、二值化。
    Windows OCR 對藝術字很挑剔，同一張圖不同前處理會讀到不同的字 (停止指令的 OCR 也是同樣做法)。
    這是產生器，呼叫端找到想要的字就可以停止，不必跑完 (每次辨識都要啟動 PowerShell，約數秒)。
    任何一種辨識失敗就產生空字串。
    """
    try:
        yield ocr_image_text(image_path)
    except Exception:
        yield ""
    try:
        with Image.open(image_path) as im:
            gray = ImageOps.autocontrast(im.convert("L"))
    except Exception:
        return
    w, h = gray.size
    longest = max(w, h)
    if longest > OCR_VARIANT_MAX_SIDE:
        ratio = OCR_VARIANT_MAX_SIDE / longest
        gray = gray.resize((max(1, int(w * ratio)), max(1, int(h * ratio))), Image.LANCZOS)
    elif longest < OCR_VARIANT_MIN_SIDE:
        gray = gray.resize((w * 2, h * 2), Image.LANCZOS)
    for variant in (gray, ImageOps.invert(gray), gray.point(lambda v: 0 if v < 140 else 255)):
        fd, tmp = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            variant.save(tmp)
            yield ocr_image_text(tmp)
        except Exception:
            yield ""
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

def _fallback_ocr(image):
    """RapidOCR 不能用時的後備：Windows 內建 OCR 多種前處理的文字串在一起 (辨識率低很多)。image 是檔案路徑或 PIL 圖片"""
    if isinstance(image, str):
        return "".join(ocr_image_text_variants(image))
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        image.save(tmp)
        return "".join(ocr_image_text_variants(tmp))
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

def ocr_text(image):
    """辨識圖片上的文字：RapidOCR 優先，沒安裝時退回 Windows 內建 OCR。image 是檔案路徑或 PIL 圖片"""
    return ocr_engine.read_text(image, fallback=_fallback_ocr)

KIND_INDEX = image_library.KindIndex(IMAGE_KINDS_PATH, ocr_text)   # 圖庫的性質索引

GRANDMA_MIN_SIDE = 250          # 邊長低於此值的圖 (LINE 貼圖、縮圖) 不是長輩圖
_FILE_KIND_CACHE = {}           # {圖片 MD5: (性質或 None, 文字)}，同一張圖在同一次執行只辨識一次

def _recognize_file(image_path):
    """
    辨識一個可以開啟的圖片檔，回傳 (性質或 None, 清理過的文字)。邊長小於 GRANDMA_MIN_SIDE (貼圖、縮圖)、
    開不了、OCR 失敗、認不出性質都回傳 (None, "")。注意：LINE 的快取檔 (.eimg) 是加密的，Pillow 打不開。
    """
    try:
        md5 = get_file_md5(image_path)
        if md5 in _FILE_KIND_CACHE:
            return _FILE_KIND_CACHE[md5]
        with Image.open(image_path) as img:
            width, height = img.size
        result = (None, "")
        if min(width, height) >= GRANDMA_MIN_SIDE:
            text = ocr_text(image_path)
            result = (image_kind.classify_text(text), image_library.clean_text(text))
        if md5:
            _FILE_KIND_CACHE[md5] = result
        return result
    except Exception:
        return None, ""

def is_authentic_grandma_image(image_path):
    """圖片檔是不是辨識得出性質的長輩圖 (見 _recognize_file)"""
    return _recognize_file(image_path)[0] is not None

GRANDMA_MEDIA_CROP_WIDTH = 420  # 截圖時從左側取樣帶起算的最大寬度 (邏輯像素)，涵蓋圖片縮圖又不會取到太多空白

def _can_decode_image(path):
    """Pillow 打不打得開這個檔案 (LINE 快取的 .eimg 是加密的，打不開)"""
    try:
        with Image.open(path):
            return True
    except Exception:
        return False

def _incoming_media_info(chat_hwnd):
    """
    截取聊天室最底部左側 (對方) 圖片訊息的縮圖區域，回傳 (截圖, 圖片內容寬度 (邏輯像素), 下方有沒有「儲存｜另存新檔｜分享」連結列)；
    視窗縮小、被遮住、太窄或找不到夠高的區塊回傳 None。
    照片下方一定有那一列連結，貼圖沒有，所以它是「照片 (相對於貼圖)」最可靠的記號；縮圖還在載入時 (灰色小方塊) 則兩者都還沒有。
    最底部若是連結列 (很矮)，圖片在它上方的區塊，所以在最底部兩個區塊裡挑第一個夠高的。
    """
    found = _left_message_blocks(chat_hwnd, side="left")
    if found is None:
        return None
    shot, scale, x_start, x_end, is_content, blocks = found
    media = next((b for b in blocks[:2] if int((b[0] - b[1] + 1) / scale) >= MEDIA_STICKER_MIN_HEIGHT), None)
    if media is None:
        return None
    bottom, top = media
    right = min(shot.width, x_start + int(GRANDMA_MEDIA_CROP_WIDTH * scale))
    xs = [x for x in range(x_start, right, 2) for y in range(top, bottom + 1, 8) if is_content(shot.getpixel((x, y)))]
    if not xs:
        return None
    pad = int(4 * scale)
    box = (max(0, min(xs) - pad), max(0, top - pad), min(shot.width, max(xs) + pad + 1), min(shot.height, bottom + pad + 1))
    has_action_row = False
    if len(blocks) >= 2 and media == blocks[1]:
        row = blocks[0]
        gap = int((row[1] - media[0]) / scale)
        has_action_row = int((row[0] - row[1] + 1) / scale) < MEDIA_NOISE_MAX_HEIGHT and gap <= MEDIA_ACTION_GAP_MAX
    return shot.crop(box), int((max(xs) - min(xs) + 1) / scale), has_action_row

IMAGE_LOAD_WAIT_SECONDS = 8.0   # 圖片縮圖還在載入時 (灰色佔位方塊) 最多等幾秒；等不到變大才當成貼圖或文字
IMAGE_LOAD_POLL_SECONDS = 1.0

def _read_greeting(crop, max_chars, retry_when_empty=False):
    """
    讀裁切圖上的字再分類，回傳 (性質 或 None, 文字)。文字訊息與貼圖共用。
    先讀原圖 (真機與模擬實測原圖最準，多種前處理反而更差)；讀不出性質時，只有「讀到短字」(可能是小字) 才放大 2 倍再讀一次，
    已經太長或根本沒字就不必 (retry_when_empty 為 True 時，沒字也再讀：貼圖的字可能小到原圖讀不到)。OCR 丟例外就讓它往上丟。
    """
    text = image_library.clean_text(ocr_text(crop))
    kind = image_kind.classify_message(text, max_chars)
    length = image_kind.message_length(text)
    if kind is None and (0 < length <= max_chars or (retry_when_empty and length == 0)):
        bigger = image_library.clean_text(ocr_text(crop.resize((crop.width * 2, crop.height * 2), Image.LANCZOS)))
        retry_kind = image_kind.classify_message(bigger, max_chars)
        if retry_kind is not None:
            return retry_kind, bigger
    return kind, text


def _recognize_sticker_text(crop, max_chars=image_kind.TEXT_MESSAGE_MAX_CHARS, retry_when_empty=True):
    """
    貼圖：讀裁切圖上的字，是短問候或祝福 (例如貼圖上寫著「早安安」) 才算，回傳 (狀態, 性質, 文字)：True、False、
    None (OCR 暫時不能用：稍後再辨識，不當成「不是問候」)。比照文字訊息的規則 (image_kind.classify_message)：max_chars 字內、
    競選字不算、生日要有祝賀語才算，所以一般貼圖 (沒字、寫「OK啦」「哈哈哈」之類) 一律不回。
    裁切圖會連旁邊的時間 (「上午 6:20」) 一起框進來，clean_text 會去掉讀得出的時間；讀歪的時間殘渣不含問候關鍵字，不影響分類。
    """
    try:
        kind, text = _read_greeting(crop, max_chars, retry_when_empty=retry_when_empty)
    except Exception:
        return None, None, ""
    if kind is not None:
        log_v(f" -> [貼圖文字識別] 貼圖上的字「{text}」，性質：{kind.label}。")
        return True, kind, text
    return False, None, text


def _recognize_chat_screenshot(chat_hwnd):
    """
    用聊天室截圖辨識最底部左側那張圖，回傳 (狀態, 性質, 文字)：
    狀態 True (辨識得出性質)、False (貼圖，或辨識不出來)、None (暫時無法判斷：截不到圖、OCR 暫時不能用)。
    這條是「快取出現新圖」的路徑，只會有照片：貼圖不會寫進聊天室的快取目錄 (LINE 把貼圖存在 Data\\Sticker，依貼圖編號存，
    與聊天室無關；真機實測 2026-11-02)，所以貼圖上的問候字是在文字觸發流程裡讀的 (見 recognize_incoming_text 的 allow_sticker_text)。
    只看一件事：圖片正下方有沒有「儲存｜另存新檔｜分享」那一列連結。照片一定有，貼圖沒有。
    不能看縮圖寬度：真機實測貼圖旁邊的時間戳記會把量到的「內容寬度」撐到 327 (舊規則當成照片)，
    而直幅或小張的照片只有 183 (舊規則當成貼圖)。
    圖片還在載入時 (LINE 先顯示一個灰色小方塊) 那一列還沒出現，所以每秒重新截圖，最多等 IMAGE_LOAD_WAIT_SECONDS 秒，
    等滿了還是沒有那一列才判定是貼圖。
    """
    deadline = time.time() + IMAGE_LOAD_WAIT_SECONDS
    while True:
        found = _incoming_media_info(chat_hwnd)
        if (found is not None and found[2]) or time.time() >= deadline:
            break
        time.sleep(IMAGE_LOAD_POLL_SECONDS)
    if found is None:
        return None, None, ""
    crop, _width, has_action_row = found      # 寬度不參與判斷 (見上)
    if not has_action_row:
        return False, None, ""            # 下面沒有「另存新檔」那一列：貼圖 (或圖片一直沒載入完成)
    try:
        text = ocr_text(crop)
    except Exception:
        return None, None, ""
    kind = image_kind.classify_text(text)
    return kind is not None, kind, image_library.clean_text(text)

def recognize_incoming_image(triggered_file, chat_hwnd=None):
    """
    辨識對方傳來的圖，回傳 (狀態, 性質, 文字)：狀態 True (辨識得出性質，是長輩圖)、False (不是長輩圖，或辨識不出來)、
    None (現在無法判斷，要等視窗在前景再問一次)。辨識不出來的圖一律不理會。
    - 沒有觸發檔 (例如只傳文字)：False。
    - 快取檔是一般圖片：直接辨識檔案。
    - 快取檔是加密的 .eimg (LINE 電腦版的實際情況)：改截聊天視窗裡那張圖的縮圖來辨識。
      只在視窗已是前景時才截圖 (視窗被別的視窗蓋住時截到的是別人的畫面)，否則回傳 None 延後判斷。
    """
    if not triggered_file:
        return False, None, ""
    if _can_decode_image(triggered_file):
        kind, text = _recognize_file(triggered_file)
        return kind is not None, kind, text
    try:
        if _chat_in_front(chat_hwnd):
            return _recognize_chat_screenshot(chat_hwnd)
    except Exception:
        pass
    return None, None, ""

CHAT_SETTLE_TIMEOUT = 3.0       # 等聊天室畫面穩定的最長秒數 (視窗剛還原、剛最大化後 LINE 還在重新排版與捲動)
CHAT_SETTLE_INTERVAL = 0.3      # 每隔多久截一次圖比較
CHAT_SETTLE_TOLERANCE = 0.6     # 兩次截圖 (縮成 64x64 灰階) 平均每點亮度差小於此值才算「沒變」；輸入框游標閃爍只差一點點，不能算變動


def _chat_signature(chat_hwnd):
    """聊天室訊息區 (不含輸入框) 的 64x64 灰階縮圖，用來比較畫面有沒有還在變"""
    rect = get_window_true_rect(chat_hwnd)
    h = rect[3] - rect[1]
    scale = 1.0
    try:
        dpi = ctypes.windll.user32.GetDpiForWindow(chat_hwnd)
        if dpi > 0:
            scale = dpi / 96.0
    except Exception:
        pass
    top = rect[1] + int(h * 0.18)
    bottom = rect[3] - int(145 * scale)
    shot = ImageGrab.grab(bbox=(rect[0], top, rect[2], max(top + 50, bottom)), all_screens=True)
    return shot.convert("L").resize((64, 64), Image.BILINEAR)


def _wait_until_chat_settled(chat_hwnd, timeout=None, interval=None):
    """
    等聊天室畫面穩定 (連續兩次截圖幾乎一樣)；回傳是否穩定。真機實測：視窗偏窄時 is_last_message_from_self_in_chat
    會暫時最大化再還原，還原後立刻截圖會讀不到最底部那則 (等 1.5 秒就讀得到)，所以讀文字之前一定要先等畫面穩定。
    逾時、截圖失敗都回傳 False，由呼叫端照常往下 (讀不到就是「待確認，稍後重試」，不會誤回)。
    """
    timeout = CHAT_SETTLE_TIMEOUT if timeout is None else timeout
    interval = CHAT_SETTLE_INTERVAL if interval is None else interval
    deadline = time.time() + timeout
    previous = None
    while True:
        try:
            current = _chat_signature(chat_hwnd)
        except Exception:
            return False
        if previous is not None and ImageStat.Stat(ImageChops.difference(previous, current)).mean[0] < CHAT_SETTLE_TOLERANCE:
            return True
        previous = current
        if time.time() >= deadline:
            return False
        time.sleep(interval)


TEXT_CROP_RIGHT_RATIO = 0.9     # 長文字氣泡可以很寬，右界取視窗寬度的這個比例 (氣泡右外側的時間 clean_text 會去掉)

def _incoming_text_crop(chat_hwnd):
    """截取聊天室最底部左側 (對方) 的文字氣泡；視窗縮小、被遮住、太窄、找不到內容回傳 None"""
    found = _left_message_blocks(chat_hwnd, side="left")
    if found is None:
        return None
    shot, scale, x_start, _x_end, _is_content, blocks = found
    bottom, top = blocks[0]
    pad = int(4 * scale)
    left = x_start                              # 區塊取樣帶的起點 (56px)：已避開頭像 (到約 50px)，氣泡從約 64px 開始
    right = int(shot.width * TEXT_CROP_RIGHT_RATIO)
    if right - left < int(40 * scale):
        return None
    return shot.crop((left, max(0, top - pad), right, min(shot.height, bottom + pad + 1)))

def _sticker_text_allowed(config, target_name):
    """
    貼圖上寫著短問候或祝福也回：只限一對一 (群組一律只回長輩圖照片)，reply_to_sticker_text 要開著，
    而且 trigger_mode 不是 photo_only (只回照片)。
    """
    return (bool(config.get("reply_to_sticker_text", True)) and not is_group_target(target_name)
            and config.get("trigger_mode", "photo_and_sticker") != "photo_only")


def _text_trigger_enabled(config, target_name):
    """這位好友的文字、生日「謝謝」、貼圖三種觸發至少開一種 (都關著就不用看他的文字訊息)。"""
    return bool(config.get("reply_to_text", True) or birthday_reply_text(config) or _sticker_text_allowed(config, target_name))


def _chat_in_front(chat_hwnd):
    """聊天視窗存在、可見、沒最小化、而且在前景 (讀螢幕上的字之前的共同前提)。"""
    return bool(chat_hwnd is not None and win32gui.IsWindow(chat_hwnd) and not win32gui.IsIconic(chat_hwnd)
                and win32gui.IsWindowVisible(chat_hwnd) and win32gui.GetForegroundWindow() == chat_hwnd)


STICKER_LOAD_RETRY_DELAY = 1.5      # 貼圖讀到一個字都沒有時，可能圖還在載入：等這麼久再截圖讀一次 (只再讀一次)
STICKER_LOAD_WAIT_SECONDS = 4.0     # 判斷「是貼圖還是還在載入的照片」時，最多等這麼久看「另存新檔」那一列有沒有出現 (照片載入完成才有)


def _bottom_sticker(chat_hwnd, wait_seconds=0.0):
    """
    最底部左側 (對方) 那一則是不是貼圖，回傳 (狀態, 裁切圖)：True (是貼圖，附裁切圖)、False (不是)、None (暫時判斷不出，稍後再說)。
    貼圖的條件：不是文字氣泡 (last_left_message_is_media 為 True)，而且圖片正下方沒有「儲存｜另存新檔｜分享」那一列連結 (照片才有)。
    照片還在載入時 (灰色小方塊) 也沒有那一列，會被誤當成貼圖，所以 wait_seconds 大於 0 時，沒看到那一列會每秒重看、最多等這麼久，
    等到那一列出現就是照片 (交給快取路徑，不能在這裡回覆)。貼圖永遠不會有那一列，所以只有真的貼圖會等滿。
    暫時性的失敗 (截不到圖、判斷不出、例外) 回傳 None，不能當成「不是貼圖」而永久略過；例外會留一行紀錄。
    """
    try:
        media = last_left_message_is_media(chat_hwnd)
        if media is None:
            return None, None
        if media is False:
            return False, None
        info = _incoming_media_info(chat_hwnd)
        if info is None:
            return None, None
        deadline = time.time() + wait_seconds
        while not info[2] and time.time() < deadline:
            time.sleep(IMAGE_LOAD_POLL_SECONDS)
            info = _incoming_media_info(chat_hwnd)
            if info is None:
                return None, None               # 等的時候圖片不見了 (被新訊息推走)：稍後再說
        if info[2]:
            return False, None                  # 有「另存新檔」那一列：照片，交給快取路徑
        return True, info[0]
    except Exception as e:
        log_v(f" -> [貼圖偵測] 暫時判斷不出最底部是不是貼圖：{e}")
        return None, None


_TIME_RESIDUE = re.compile(r"""[上下午時0-9:：\sA-Za-z.,;'"|!?_\-]""")


def _nothing_readable(text):
    """
    OCR 讀到的字去掉「讀歪的時間殘渣」(上、下、午、數字、冒號、英文字母與符號) 之後還剩不剩東西。
    貼圖的裁切圖一定把旁邊的「上午 6:20」框進來，clean_text 只去得掉讀對的時間；讀歪的 (例如「E午620」) 會留下來，
    所以光看「字串是不是空的」，沒字或還沒載入的貼圖幾乎都不會被判成沒字。
    """
    return not _TIME_RESIDUE.sub("", text or "")


def recognize_incoming_sticker(chat_hwnd, max_chars=image_kind.TEXT_MESSAGE_MAX_CHARS, first_crop=None):
    """
    讀最底部那張貼圖上的字，回傳 (狀態, 性質, 文字)，同 recognize_incoming_text。只在視窗是前景時截圖。
    first_crop 是呼叫端剛偵測到的貼圖裁切圖 (「看一眼」剛等過畫面穩定、偵測過，不必再來一次)，給了就直接拿來讀第一次。
    一次取得貼圖的裁切圖就用同一張讀 (不會中途變成讀文字氣泡)；最底部已經不是貼圖，或暫時判斷不出，回傳 None (稍後再說)。
    第一次讀不出問候、而且「去掉時間殘渣後沒有任何字」時，貼圖可能還在載入：等 STICKER_LOAD_RETRY_DELAY 秒、重新等畫面穩定、
    重新截圖偵測，再讀一次 (這次沒字也放大 2 倍讀)。其餘情形 (讀到字但不是問候) 就是一般貼圖，不再讀第二次。
    貼圖不會寫進聊天室的快取目錄，所以是靠「視窗列表有變化」的文字觸發流程偵測的。
    """
    try:
        if not _chat_in_front(chat_hwnd):
            return None, None, ""
        crop = first_crop
        if crop is None:
            _wait_until_chat_settled(chat_hwnd)
            state, crop = _bottom_sticker(chat_hwnd)
            if state is not True:
                return None, None, ""
        # 第一步：沒字不放大 (放大留給等一下重新截圖的第二步，免得同一個沒字的貼圖連讀四次)
        first = _recognize_sticker_text(crop, max_chars, retry_when_empty=False)
        if first[0] is not False or not _nothing_readable(first[2]):
            return first
        # 第二步：什麼都沒讀到，等一下重新截圖再讀一次
        time.sleep(STICKER_LOAD_RETRY_DELAY)
        _wait_until_chat_settled(chat_hwnd)
        state, crop = _bottom_sticker(chat_hwnd)
        if state is not True:
            return None, None, ""
        return _recognize_sticker_text(crop, max_chars, retry_when_empty=True)
    except Exception as e:
        log_v(f" -> [貼圖偵測] 讀取貼圖時發生例外：{e}")
        return None, None, ""


def recognize_incoming_text(chat_hwnd, max_chars=image_kind.TEXT_MESSAGE_MAX_CHARS, settle=True):
    """
    讀對方傳來的最後一則文字訊息，回傳 (狀態, 性質, 文字)：True (短訊息且辨識得出性質)、
    False (太長、不是問候或祝福、競選相關、讀不到字)、None (暫時無法判斷：視窗不在前景、截圖失敗、OCR 暫時不能用)。
    只在視窗是前景時截圖 (視窗被別的視窗蓋住時截到的是別人的畫面)。
    """
    try:
        if not _chat_in_front(chat_hwnd):
            return None, None, ""
        if settle:
            _wait_until_chat_settled(chat_hwnd)          # 視窗剛還原後 LINE 還在重排，立刻讀會讀錯 (呼叫端剛等過就傳 settle=False)
        crop = _incoming_text_crop(chat_hwnd)
        if crop is None:
            return None, None, ""
        kind, text = _read_greeting(crop, max_chars)
    except Exception:
        return None, None, ""
    return kind is not None, kind, text

def _skip_not_grandma(target_name, triggered_file, config, incoming_text=""):
    """略過不是長輩圖 (或辨識不出來、或是競選圖) 的來訊：記下對方傳過這張 (百輪內不回送給本人)，回傳結果代碼"""
    if image_kind.is_campaign_text(incoming_text):
        log_v(f"[競選圖略過]「{target_name}」傳來的是候選人或競選相關的圖，不回覆。")
    else:
        log_v(f"[非長輩圖略過]「{target_name}」傳來的不是長輩圖 (或辨識不出來)，不回覆。")
    md5 = get_file_md5(triggered_file) if triggered_file else None
    if md5:
        record_contact_received(target_name, md5, max_rounds=config.get("history_rounds", 100))
    return "SKIPPED_NOT_GRANDMA"

def harvest_line_incoming_images(images_dir, sender_name=None, max_rounds=100, specific_file_path=None):
    """
    從 LINE 電腦版暫存快取目錄中，自動收納對方傳來的正統長輩圖進圖庫。
    若對方傳來的是生活照、寵物照片或雜圖，則嚴格略過不收納！
    無論是否收納，均將圖片指紋記錄在該對象的 received 歷史中，確保百輪內不回傳同張圖給本人。
    """
    if not os.path.isabs(images_dir):
        abs_dir = os.path.join(SCRIPT_DIR, images_dir)
    else:
        abs_dir = images_dir
    os.makedirs(abs_dir, exist_ok=True)

    if specific_file_path and os.path.exists(specific_file_path):
        cached_files = [specific_file_path]
    else:
        cached_files = []
    if not cached_files:
        return []

    # 取得目前圖庫已存在的 MD5
    existing_md5s = set()
    for f in os.listdir(abs_dir):
        p = os.path.join(abs_dir, f)
        if os.path.isfile(p):
            try:
                with open(p, "rb") as fp:
                    existing_md5s.add(hashlib.md5(fp.read()).hexdigest())
            except Exception:
                pass

    harvested_filenames = []
    for f in cached_files:
        try:
            with open(f, "rb") as fp:
                data = fp.read()
            m = hashlib.md5(data).hexdigest()

            # 無論是否為長輩圖，均記錄這張圖是由該對象傳送過來的 (百輪禁發給本人)
            if sender_name:
                record_contact_received(sender_name, m, max_rounds=max_rounds)

            if m in existing_md5s:
                continue

            with Image.open(f) as img:
                w, h = img.size
                fmt = img.format.lower()
                if w < 250 or h < 250:
                    continue
                ext = ".jpg" if fmt in ("jpeg", "jpg") else f".{fmt}"

            # 嚴格驗證：若不是真正的長輩圖 (生活照/寵物照/雜圖)，不收納進長輩圖庫！
            if not is_authentic_grandma_image(f):
                log_v(f" -> [非長輩圖不收] 偵測到「{sender_name or '好友'}」發送的為生活照/雜圖，不收納進長輩圖庫！")
                continue

            dest_filename = f"received_{int(os.path.getmtime(f))}_{m[:8]}{ext}"
            dest_path = os.path.join(abs_dir, dest_filename)
            shutil.copy2(f, dest_path)
            existing_md5s.add(m)
            harvested_filenames.append(dest_filename)
            log_v(f" -> [借花獻佛] 成功收納「{sender_name or '好友'}」發來的正統長輩圖: {dest_filename} (已入庫備用！)")
        except Exception:
            pass

    return harvested_filenames

def current_time_slot(now=None):
    """依本機時間回傳時段：morning (05-11 時)、noon (11-17 時)、evening (17-05 時)"""
    hour = (now or datetime.now()).hour
    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 17:
        return "noon"
    return "evening"

def _load_image_tags():
    try:
        with open(IMAGE_TAGS_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            return loaded
    except Exception:
        pass
    return {}

def is_image_tagged(image_path):
    """該圖的時段分類是否已快取 (查快取不需 OCR)"""
    md5 = get_file_md5(image_path)
    cached = _load_image_tags().get(md5) if md5 else None
    return isinstance(cached, list) and bool(cached)

def set_image_slots(image_path, slots):
    """手動標記圖片適用時段 (morning / noon / evening / neutral)，寫入 image_tags.json。回傳是否成功。"""
    valid = [x for x in slots if x in ("morning", "noon", "evening", "neutral")]
    md5 = get_file_md5(image_path)
    if not md5 or not valid or ("neutral" in valid and len(valid) > 1):
        return False
    tags = _load_image_tags()
    tags[md5] = valid
    return atomic_save_json(IMAGE_TAGS_PATH, tags)

NEUTRAL_MIN_CJK = 6             # 自動判定無時段字樣時，至少要有一種前處理讀到這麼多中文字 (證明 OCR 對這張圖有效)
FESTIVAL_KEYWORDS = ("節", "新年", "新春", "春節", "元宵", "清明", "端午", "七夕", "中秋", "重陽", "除夕", "元旦",
                     "父親", "母親", "情人", "聖誕", "萬聖", "生日", "週年", "周年", "畢業", "婚")   # 特定節日或場合的圖，不是通用圖

def _slots_from_text(text):
    """從 OCR 文字找問候語，回傳適用時段清單 (沒有回傳空清單)"""
    text = text.lower()
    slots = []
    if "早安" in text or "早上好" in text or "晨" in text or "goodmorning" in text:
        slots.append("morning")
    if "午安" in text or "中午好" in text or "下午好" in text or "goodafternoon" in text:
        slots.append("noon")
    if "晚安" in text or "晚上好" in text or "夜安" in text or "好夢" in text or "goodnight" in text or "goodevening" in text:
        slots.append("evening")
    return slots

def _cjk_count(text):
    return sum(1 for ch in text if "一" <= ch <= "鿿")

def classify_image_slots(image_path):
    """
    回傳圖片適用時段清單 (morning / noon / evening；["neutral"] 表示無時段字樣，任何時段皆可用)；
    未知回傳 None (Fail-Closed，不被挑選)。
    - 先查 image_tags.json 快取 (以 MD5 為鍵；舊版留下的空清單視為未知)。來源標明時段的圖、`--tag-image` 手動標記的圖都在這裡。
    - 未快取時以 OCR 找問候語 (早安/早上好/晨 -> morning、午安/中午好 -> noon、晚安/晚上好 -> evening)，
      命中才快取。OCR 沒找到問候語不代表沒有 (藝術字常辨識不出)，所以預設視為未知。
    - 開啟 auto_neutral_images 時：改用多種前處理逐一辨識，任何一種讀到問候語就以它為準；全部都沒有問候語，
      而且至少一種讀到 NEUTRAL_MIN_CJK 個以上中文字、沒有節慶字樣，才判定為 neutral 並快取
      (同時記在 image_tags.json 的 _auto_neutral 清單，方便檢查；要推翻就用 `--tag-image` 重新標記)。
    """
    md5 = get_file_md5(image_path)
    if not md5:
        return None
    tags = _load_image_tags()
    cached = tags.get(md5)
    valid_slots = ("morning", "noon", "evening", "neutral")
    if (isinstance(cached, list) and cached and all(x in valid_slots for x in cached)
            and not ("neutral" in cached and len(cached) > 1)):
        return cached

    def read_texts():
        if _AUTO_NEUTRAL:
            yield from ocr_image_text_variants(image_path)
        else:
            yield ocr_image_text(image_path)

    union = ""
    best_cjk = 0
    try:
        for text in read_texts():
            slots = _slots_from_text(text)
            if slots:
                tags[md5] = slots
                atomic_save_json(IMAGE_TAGS_PATH, tags)
                return slots
            union += text
            best_cjk = max(best_cjk, _cjk_count(text))
    except Exception:
        return None
    if not _AUTO_NEUTRAL or best_cjk < NEUTRAL_MIN_CJK or any(k in union for k in FESTIVAL_KEYWORDS):
        return None
    tags[md5] = ["neutral"]
    auto = tags.get("_auto_neutral")
    tags["_auto_neutral"] = (auto if isinstance(auto, list) else []) + [md5]
    atomic_save_json(IMAGE_TAGS_PATH, tags)
    return ["neutral"]

def filter_images_by_time_slot(candidates, slot, max_ocr=8):
    """
    依時段挑圖：優先回傳適用目前時段的圖；沒有時改用已標記為 neutral (無時段字樣) 的圖代替；兩者皆無回傳 None (Fail-Closed)。未知的圖不會被選。
    隨機順序走訪，最多現場 OCR max_ocr 張未快取的圖。
    """
    pool = list(candidates)
    random.shuffle(pool)
    ocr_used = 0
    neutral = None
    for img in pool:
        if not is_image_tagged(img):
            ocr_used += 1
        slots = classify_image_slots(img)
        if slots is not None:
            if slot in slots:
                return img
            if "neutral" in slots and neutral is None:
                neutral = img
        if ocr_used >= max_ocr:
            # OCR 額度用完：只繼續掃已快取的圖 (不需 OCR)，仍有機會找到符合時段的圖
            for rest in pool[pool.index(img) + 1:]:
                if not is_image_tagged(rest):
                    continue
                rest_slots = classify_image_slots(rest)
                if rest_slots is not None:
                    if slot in rest_slots:
                        return rest
                    if "neutral" in rest_slots and neutral is None:
                        neutral = rest
            break
    return neutral

def countdown_sleep(total_seconds, abort_check=None, check_every=3.0):
    """
    倒數等待：剩餘時間每跨過 60 秒的倍數、以及 30/10 秒時印出一次，讓長時間等待時看得到進度。
    abort_check：選用，每 check_every 秒呼叫一次 (用於等待期間仍能收到遠端停止指令)；
    它擲出 StopRequested 時會直接中斷等待，其他例外只記錄不中斷。
    """
    end = time.monotonic() + total_seconds
    next_check = time.monotonic() + check_every
    marks = {m for m in set(range(60, int(total_seconds) + 1, 60)) | {30, 10} if m < total_seconds}
    announced = set()
    while True:
        remain = end - time.monotonic()
        if remain <= 0:
            break
        if abort_check is not None and time.monotonic() >= next_check:
            next_check = time.monotonic() + check_every
            try:
                abort_check()
            except Exception as e:
                log_v(f"[警告] 等待期間的停止指令檢查發生例外: {e}")
        due = [m for m in marks if remain <= m and m not in announced]
        if due:
            m = max(due)
            announced.update(due)
            label = f"{m // 60} 分 {m % 60} 秒" if m >= 60 else f"{m} 秒"
            log_v(f" -> [倒數] 距離發送還有約 {label}")
        time.sleep(min(1.0, remain))

IMAGE_CATEGORIES = ("morning", "noon", "evening", "neutral")

def image_category(image_path):
    """圖庫分類：morning / noon / evening / neutral (已標記為無時段字樣)；未知回傳 None"""
    slots = classify_image_slots(image_path)
    if not slots:
        return None
    return slots[0]

def fill_image_pool(images_dir, total=30, max_attempts=250):
    """
    把圖庫補到 total 張：已標記為 neutral (無時段字樣) 的圖照單計入，其餘名額由早安、午安、晚安平均分配。
    開啟 auto_neutral_images 時，neutral 另外保留約 1/5 的名額 (讓沒有符合時段的圖時有圖可替代)。
    來源標明時段的圖直接依來源歸類；其餘靠 OCR，OCR 讀不出來的圖 (未知) 丟棄。
    既有圖片先分類計數 (超額者保留不刪)；再逐張下載，類別已滿的新圖立即丟棄。
    回傳 {類別: 張數}。
    """
    abs_dir = images_dir if os.path.isabs(images_dir) else os.path.join(SCRIPT_DIR, images_dir)
    os.makedirs(abs_dir, exist_ok=True)
    counts = {c: 0 for c in IMAGE_CATEGORIES}
    for img in _scan_images(abs_dir):
        cat = image_category(img)
        if cat:
            counts[cat] += 1
    slot_cats = [c for c in IMAGE_CATEGORIES if c != "neutral"]
    neutral_quota = max(counts["neutral"], total // 5) if _AUTO_NEUTRAL else counts["neutral"]
    base, extra = divmod(max(0, total - neutral_quota), len(slot_cats))
    quota = {c: base + (1 if i < extra else 0) for i, c in enumerate(slot_cats)}
    quota["neutral"] = neutral_quota
    log_v(f"[圖庫分配] 目前 {counts}，目標 {quota}")

    existing_md5s = get_existing_md5s(abs_dir)
    candidates = build_candidates(_IMAGE_SOURCES)[:max_attempts]
    fails = 0
    for url, source_category in candidates:
        if all(counts[c] >= quota[c] for c in IMAGE_CATEGORIES):
            break
        if fails >= 10:
            log_v("[網路保護] 連續下載失敗 10 次，停止補圖。")
            break
        saved = download_and_validate_image(url, abs_dir, existing_md5s)
        if not saved:
            fails += 1
            continue
        fails = 0
        _tag_source_category(saved, source_category)
        cat = image_category(saved)
        if cat is None or counts[cat] >= quota[cat]:
            try:
                os.remove(saved)
            except Exception:
                pass
            continue
        counts[cat] += 1
        log_v(f" -> [圖庫分配] 收納 {os.path.basename(saved)} 為「{cat}」類 ({counts[cat]}/{quota[cat]})")
    if sum(counts.values()) >= total:
        log_v(f"[圖庫分配] 完成，圖庫已達 {total} 張: {counts}")
    else:
        log_v(f"[圖庫分配] 嘗試次數用盡，目前 {sum(counts.values())}/{total} 張: {counts}，可稍後再執行一次。")
    return counts

def pick_unique_image(images, target_name, config):
    """
    從圖庫中隨機挑選一張合格且百輪不重複的長輩圖：
    - 嚴格比對各聯絡人的百輪收發 MD5 雜湊 (包含本機送出與對方傳入)
    - 確保針對該目標至少 100 輪之內絕不重複發送相同圖片
    - 若所有圖片均已進入百輪記憶，自動觸發連網爬取全新長輩圖補充
    """
    if not images:
        return None

    max_rounds = config.get("history_rounds", 100)
    history = load_contact_history()
    contact_data = history.get(target_name, {"sent": [], "received": []})
    if not isinstance(contact_data, dict):
        contact_data = {"sent": [], "received": []}
    sent_list = contact_data.get("sent", []) if isinstance(contact_data.get("sent"), list) else []
    recv_list = contact_data.get("received", []) if isinstance(contact_data.get("received"), list) else []

    # 取得百輪內禁止發給該對象的 MD5 集合
    forbidden_md5s = set(sent_list[-max_rounds:] + recv_list[-max_rounds:])

    candidates = []
    for img in images:
        m = get_file_md5(img)
        if m and m not in forbidden_md5s:
            candidates.append(img)

    # 若現有圖庫已全部在百輪記憶中，自動觸發連網爬取全新長輩圖
    if not candidates:
        log_v(f"\n[百輪保護] 目前圖庫中所有圖片均在「{target_name}」百輪記憶中 (曾送出或對方曾傳送過)！")
        log_v(f" -> 正在自動連網下載 5 張全新長輩圖，確保絕不重複...")
        target_dir = os.path.join(SCRIPT_DIR, config.get("images_dir", "images"))
        try:
            crawl_images_now(5, target_dir)
            fresh_images = get_images(config.get("images_dir", "images"), auto_replenish=False)
            for img in fresh_images:
                m = get_file_md5(img)
                if m and m not in forbidden_md5s:
                    candidates.append(img)
        except Exception as e:
            log_v(f"[警告] 連網補充長輩圖失敗: {e}")
            return None
        if not candidates:
            log_v(f"[百輪保護安全攔截] 目前圖庫所有長輩圖皆在「{target_name}」百輪記憶中，且連網補充未獲取新圖，遵循 Fail-Closed 拒絕發送！")
            return None


    if config.get("time_aware_images", False):
        slot = current_time_slot()
        selected = filter_images_by_time_slot(candidates, slot)
        if selected is None:
            log_v(f"[時段保護] 找不到適合目前時段 ({slot}) 或無時段字樣的長輩圖，為避免送錯問候語，本次不發送。")
        return selected

    selected = random.choice(candidates)
    return selected

ATTACH_DIALOG_TIMEOUT = 8.0         # 按下附加檔案按鈕後，等檔案對話框出現的秒數
ATTACH_DIALOG_CLOSE_TIMEOUT = 8.0   # 按 Enter 後，等檔案對話框關閉 (LINE 接手送出) 的秒數

def _find_attach_button_center(chat_hwnd):
    """
    用 UIA 找輸入區最左邊的按鈕 (迴紋針，附加檔案)，回傳螢幕座標 (x, y)；找不到回傳 None。
    LINE 電腦版目前不接受貼上圖片 (CF_DIB、PNG、檔案清單三種剪貼簿格式都試過，輸入框都沒有反應)，
    但這個按鈕選檔後會直接送出。
    """
    try:
        import uiautomation as auto
        panel = auto.ControlFromHandle(chat_hwnd).GroupControl(ClassName="MessageInputPanel")
        if not panel.Exists(1, 0.2):
            return None
        buttons = []

        def walk(ctrl, depth=0):
            if depth > 3:
                return
            for child in ctrl.GetChildren():
                if child.ClassName == "LcButton":
                    buttons.append(child)
                walk(child, depth + 1)

        walk(panel)
        if not buttons:
            return None
        r = min(buttons, key=lambda b: b.BoundingRectangle.left).BoundingRectangle
        return (r.left + r.right) // 2, (r.top + r.bottom) // 2
    except Exception:
        return None

def _find_open_dialog(timeout=ATTACH_DIALOG_TIMEOUT):
    """等 LINE 的「開啟」檔案對話框 (標準對話框，視窗類別 #32770) 出現，回傳視窗控制代碼；逾時回傳 None"""
    deadline = time.time() + timeout
    while True:
        line_pids = get_line_pids()
        found = []

        def enum_cb(hwnd, _):
            if win32gui.IsWindowVisible(hwnd) and win32gui.GetClassName(hwnd) == "#32770":
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid in line_pids:
                    found.append(hwnd)
            return True

        try:
            win32gui.EnumWindows(enum_cb, None)
        except Exception:
            pass
        if found:
            return found[0]
        if time.time() >= deadline:
            return None
        time.sleep(0.2)

def _set_dialog_filename(dialog_hwnd, path):
    """
    用 UIA 直接設定檔案對話框的檔名欄 (id 1148)，不經鍵盤輸入，所以不受輸入法影響
    (注音輸入法會把路徑打成注音符號)。設完讀回來比對 (Windows 路徑不分大小寫)，一致才回傳 True。
    """
    try:
        import uiautomation as auto
        edit = auto.ControlFromHandle(dialog_hwnd).EditControl(AutomationId="1148")
        if not edit.Exists(2, 0.2):
            return False
        pattern = edit.GetValuePattern()
        pattern.SetValue(path)
        time.sleep(0.3)
        return os.path.normcase(os.path.normpath(pattern.Value)) == os.path.normcase(os.path.normpath(path))
    except Exception:
        return False

def _close_dialog(dialog_hwnd):
    """關掉檔案對話框 (相當於按取消)，不會送出任何東西"""
    try:
        win32gui.PostMessage(dialog_hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception:
        pass

def send_image_via_attach_dialog(chat_hwnd, image_path):
    """
    透過輸入區的附加檔案按鈕送出圖片：點按鈕 -> 檔案對話框 -> UIA 填入完整路徑 -> Enter 開啟，LINE 接手直接送出。
    回傳 True 表示已按下 Enter 且對話框已關閉 (圖片視為已送出)；任何一步沒把握都關掉對話框並回傳 False (什麼都沒送)。
    每一步前都核驗前景視窗，避免點到別的視窗。
    """
    path = os.path.abspath(image_path)
    if not os.path.isfile(path):
        log_v(f"[錯誤] 要送出的圖片不存在: {path}")
        return False
    center = _find_attach_button_center(chat_hwnd)
    if center is None:
        log_v("[安全攔截] 找不到輸入區的附加檔案按鈕，終止發送。")
        return False
    if win32gui.GetForegroundWindow() != chat_hwnd:
        log_v("[安全攔截] 點附加檔案按鈕前焦點已被移轉至其他視窗，終止發送。")
        return False
    pyautogui.click(center[0], center[1])
    dialog = _find_open_dialog()
    if dialog is None:
        log_v("[安全攔截] 點了附加檔案按鈕但沒有出現檔案對話框，終止發送。")
        return False
    if not _set_dialog_filename(dialog, path):
        log_v("[安全攔截] 無法把圖片路徑填入檔案對話框，取消發送。")
        _close_dialog(dialog)
        return False
    if win32gui.GetForegroundWindow() != dialog:
        log_v("[安全攔截] 按 Enter 前檔案對話框不在前景，取消發送。")
        _close_dialog(dialog)
        return False
    pyautogui.press("enter")
    deadline = time.time() + ATTACH_DIALOG_CLOSE_TIMEOUT
    while win32gui.IsWindow(dialog) and win32gui.IsWindowVisible(dialog):
        if time.time() >= deadline:
            log_v("[安全攔截] 按 Enter 後檔案對話框沒有關閉，取消發送。")
            _close_dialog(dialog)
            return False
        time.sleep(0.2)
    return True
TEXT_SEND_CONFIRM_TIMEOUT = 6.0     # 按 Enter 後等輸入框清空 (LINE 收下並送出) 的秒數；LINE 偶爾反應慢，等太短會把「慢」當成「沒送出」而重送


def _find_message_input(chat_hwnd):
    """聊天視窗底部的訊息輸入框 (UIA EditControl，類別 AutoSuggestTextArea)；找不到回傳 None"""
    try:
        import uiautomation as auto
        edit = auto.ControlFromHandle(chat_hwnd).EditControl(ClassName="AutoSuggestTextArea")
        return edit if edit.Exists(2, 0.2) else None
    except Exception:
        return None


def send_text_via_input(chat_hwnd, text):
    """
    把一句文字送進聊天室：用 UIA 直接寫入輸入框 (不經剪貼簿與鍵盤輸入，所以不受輸入法影響、不動到剪貼簿)，讀回來比對後按 Enter。
    回傳 True 表示已按 Enter 且輸入框已清空 (LINE 已收下)；按 Enter 之前任何一步沒把握都清掉自己寫的字並回傳 False：
    輸入框裡本來就有尚未送出的草稿 (絕不覆蓋、也不跟草稿一起送出)、寫入後讀回不一致、按 Enter 前焦點被轉走。
    **按了 Enter 之後**：輸入框清空 -> True；讀取輸入框時發生例外 (無法確認有沒有送出) -> 也回傳 True (視為已送出，絕不重送，
    與圖片「按了 Enter 就算送出」同一個原則，寧可少送一次也不重複送「謝謝」)；只有等滿時間、確定輸入框裡還留著我們的字，
    才清掉並回傳 False (確定沒送出，可以安全重試)。
    """
    if not text or not text.strip():
        return False
    edit = _find_message_input(chat_hwnd)
    if edit is None:
        log_v("[安全攔截] 找不到訊息輸入框，終止發送。")
        return False
    pressed = False
    try:
        pattern = edit.GetValuePattern()
        if (pattern.Value or "").strip():
            log_v("[安全攔截] 輸入框裡有尚未送出的文字 (草稿)，為避免覆蓋或連同草稿一起送出，終止發送。")
            return False
        if win32gui.GetForegroundWindow() != chat_hwnd:
            log_v("[安全攔截] 寫入文字前焦點已被移轉至其他視窗，終止發送。")
            return False
        pattern.SetValue(text)
        time.sleep(0.3)
        if (pattern.Value or "") != text:
            log_v("[安全攔截] 文字寫入輸入框後讀回不一致，取消發送。")
            pattern.SetValue("")
            return False
        if win32gui.GetForegroundWindow() != chat_hwnd:
            log_v("[安全攔截] 按 Enter 前焦點已被移轉至其他視窗，取消發送。")
            pattern.SetValue("")
            return False
        pyautogui.press("enter")
        pressed = True
        deadline = time.time() + TEXT_SEND_CONFIRM_TIMEOUT
        while time.time() < deadline:
            time.sleep(0.2)
            if not (pattern.Value or "").strip():
                return True
        log_v("[安全攔截] 按 Enter 後輸入框一直留著我們的字，判定沒送出，取消並清掉輸入框。")
        pattern.SetValue("")
        return False
    except Exception as e:
        if pressed:
            log_v(f"[警告] 按了 Enter 之後無法確認文字有沒有送出 ({e})，視為已送出，不重送。")
            return True
        log_v(f"[安全攔截] 送出文字時發生例外: {e}")
        try:
            edit.GetValuePattern().SetValue("")
        except Exception:
            pass
        return False


def get_line_pids():
    pids = set()
    for p in psutil.process_iter(['pid', 'name']):
        try:
            if p.info['name'] and 'LINE.exe' in p.info['name']:
                pids.add(p.info['pid'])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return pids

def get_all_line_windows():
    line_pids = get_line_pids()
    results = []
    def enum_cb(hwnd, _):
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid in line_pids:
            title = win32gui.GetWindowText(hwnd).strip()
            cls = win32gui.GetClassName(hwnd)
            vis = win32gui.IsWindowVisible(hwnd)
            results.append((hwnd, title, cls, vis))
        return True
    try:
        win32gui.EnumWindows(enum_cb, None)
    except Exception:
        pass
    return results

def verify_is_not_group_window_uia(hwnd):
    """透過 UI 控制項層級深度遞迴驗證是否為群組視窗 (檢驗成員列表、記事本、相簿等群組專屬控制項，杜絕同名群組冒充 1:1)"""
    try:
        import uiautomation as auto
    except ImportError:
        log_v("[安全警告] 系統未安裝 uiautomation 模組，無法執行 UIA 深度群組檢驗！遵循 Fail-Closed 安全原則攔截。")
        return False
    except Exception as e:
        log_v(f"[安全警告] 載入 uiautomation 失敗: {e}，遵循 Fail-Closed 拒絕發送。")
        return False

    try:
        if not win32gui.IsWindow(hwnd):
            # 若非真實 OS 視窗 (如無 GUI 隔離測試之 mock HWND)，放行讓邏輯測試執行
            return True
        ctrl = auto.ControlFromHandle(hwnd)
        if not ctrl:
            log_v("[UIA群組安全攔截] 無法從視窗取得 UIA 控制項根節點，遵循 Fail-Closed 拒絕發送！")
            return False
        children = ctrl.GetChildren()
        if not children:
            log_v("[UIA群組安全攔截] 視窗無可列舉之子控制項，無法確認為 1:1 視窗，遵循 Fail-Closed 拒絕發送！")
            return False

        # 多層級遞迴深度檢驗群組特徵控制項 (最多深入 3 層)，防範群組元件被封裝於內部容器
        def has_group_descendants(element, depth=0, max_depth=3):
            if not element or depth > max_depth:
                return False
            name = element.Name or ""
            if any(k in name for k in ("成員", "群組通話", "直播", "群組設定")) or re.search(r"\b(Members|Group Call|Live|Group Settings)\b", name, re.IGNORECASE):
                log_v(f"[UIA群組安全攔截] 視窗內偵測到群組特有控制項「{name}」，判定為群組視窗，嚴格拒絕發送！")
                return True
            for child in element.GetChildren():
                if has_group_descendants(child, depth + 1, max_depth):
                    return True
            return False

        if has_group_descendants(ctrl, depth=0, max_depth=3):
            return False
    except Exception as e:
        log_v(f"[安全警告] 掃描視窗控制項失敗: {e}，遵循 Fail-Closed 拒絕發送。")
        return False
    return True

GROUP_NAME_PATTERNS = r"(?i)\b(group|family|team|project|club)\b|群組|聊天室|成員|專案|公務|同學|家族"

GROUP_TARGETS = set()                      # config.json 的 group_targets (小寫)；只有這些名稱走群組規則，其餘一律維持 1:1 的嚴格規則
_GROUP_COUNT_SUFFIX = r"(?:\s*[\(\uFF08]\d+[\)\uFF09])?"      # 群組視窗標題後面的成員數，例如「名稱 (477)」


def set_group_targets(names):
    global GROUP_TARGETS
    GROUP_TARGETS = {str(n).strip().lower() for n in names if str(n).strip()}


def is_group_target(name):
    return bool(name) and name.strip().lower() in GROUP_TARGETS


def title_matches_target(title, name):
    """視窗標題是否就是這個對象：1:1 要完全相符；群組允許名稱後面接成員數 (人數不是未讀數，不可拿來當觸發依據)"""
    t = (title or "").strip()
    if is_group_target(name):
        return re.fullmatch(re.escape(name.strip()) + _GROUP_COUNT_SUFFIX, t, flags=re.IGNORECASE) is not None
    return t.lower() == (name or "").strip().lower()


_DUPLICATE_WINDOW_SEEN = {}                # {名稱 (小寫): 上次發現桌面上有重複同名視窗的時間}；重複視窗時 find 會回傳 []，開窗必定失敗
DUPLICATE_WINDOW_SKIP_SECONDS = 5.0


def find_group_chat_window(target_name):
    """尋找群組的獨立聊天視窗：標題必須是「名稱」或「名稱 (成員數)」，且桌面上只能有一個；絕不匹配 LINE 主視窗"""
    main_hwnd = find_line_main_window()
    found = []
    for hwnd, title, cls, vis in get_all_line_windows():
        if not vis or (cls != "ChatWnd" and "QWindowIcon" not in cls):
            continue
        if hwnd == main_hwnd or title.strip() == 'LINE':
            continue
        if title_matches_target(title, target_name):
            found.append((hwnd, title, cls, vis))
    if len(found) > 1:
        _DUPLICATE_WINDOW_SEEN[target_name.strip().lower()] = time.time()
        log_v(f"[致命安全攔截] 偵測到桌面上存在 {len(found)} 個符合群組「{target_name}」的視窗 (重名或重複開啟衝突)，為防誤傳，嚴格拒絕發送！")
        return []
    return found


def find_target_chat_window(target_name):
    """尋找標題精確相符或帶括號未讀數的獨立 1:1 聊天視窗 (絕不匹配 LINE 主視窗或群組視窗)；名稱列在 group_targets 時改找群組視窗"""
    if target_name.strip().lower() in ("line", "line main window", "line主視窗"):
        log_v("[安全拒絕] target 不得設定為系統主視窗關鍵字 'LINE'！")
        return []
    if is_group_target(target_name):
        return find_group_chat_window(target_name)

    main_hwnd = find_line_main_window()
    windows = get_all_line_windows()
    exact_matched = []
    unread_matched = []

    for hwnd, title, cls, vis in windows:
        # 嚴格排除隱藏視窗與非聊天室 Class (支援 LINE 原生 ChatWnd 與各版本 Qt QWindowIcon)
        if not vis or (cls != "ChatWnd" and "QWindowIcon" not in cls):
            continue
        # 嚴格排除 LINE 主視窗本身 (主視窗標題通常為 'LINE' 或其 HWND 與 main_hwnd 相符)
        if hwnd == main_hwnd or title.strip() == 'LINE':
            continue
        # 若視窗目前處於前景焦點卻仍帶有括號，100% 為群組成員數視窗 (1:1 視窗在前景時未讀數已自動清除而無括號)，嚴格排除
        if hwnd == win32gui.GetForegroundWindow() and re.search(r'[\(\uFF08]\d+\+?[\)\uFF09]', title):
            continue
        # 嚴格排除多括號群組視窗 (如 '群組名稱 (3) (1)')
        paren_matches = re.findall(r'[\(\uFF08]\d+\+?[\)\uFF09]', title)
        if len(paren_matches) > 1:
            continue
        # 嚴格排除帶有常見群組關鍵字之視窗 (如 Family, Group, 群組, 聊天室 等，杜絕無成員數標記之群組冒充 1:1 好友)
        if re.search(GROUP_NAME_PATTERNS, title):
            log_v(f"[安全攔截] 視窗標題「{title}」含有群組特徵關鍵字，嚴格拒絕作為 1:1 好友發送！")
            continue

        # 優先比對標題：先確認標題與目標相符，再進行深入核驗，大幅降低全視窗走訪之系統負載
        is_exact = (title.strip().lower() == target_name.lower())
        m = re.search(r"^(.*?)\s*[\(\uFF08](\d+\+?)[\)\uFF09]\s*$", title)
        is_unread = (m is not None and m.group(1).strip().lower() == target_name.lower())

        if not (is_exact or is_unread):
            continue

        # 僅對名稱符合之目標候選視窗執行 UIA 深度核驗 (若視窗處於最小化 Iconic 狀態，則保留至 focus_chat_input 恢復前景時核驗)
        if not win32gui.IsIconic(hwnd) and not verify_is_not_group_window_uia(hwnd):
            continue

        if is_exact:
            exact_matched.append((hwnd, title, cls, vis))
        else:
            unread_matched.append((hwnd, title, cls, vis))

    # 合併精確比對與未讀括號比對之視窗，以 HWND 進行去重
    unique_candidates = {}
    for item in exact_matched + unread_matched:
        unique_candidates[item[0]] = item

    all_matched = list(unique_candidates.values())

    # 嚴格禁止任何形式的重名或重複開啟 (即使一個已讀、一個未讀亦徹底攔截，杜絕誤傳)
    if len(all_matched) > 1:
        _DUPLICATE_WINDOW_SEEN[target_name.strip().lower()] = time.time()
        log_v(f"[致命安全攔截] 偵測到桌面上存在 {len(all_matched)} 個符合「{target_name}」的視窗 (重名或重複開啟衝突)，為防誤傳，嚴格拒絕發送！")
        return []

    return all_matched

def get_all_open_chat_windows():
    """取得桌面上所有開啟中的可見 LINE 1:1 獨立聊天視窗 (排除主視窗與群組)"""
    main_hwnd = find_line_main_window()
    windows = get_all_line_windows()
    open_chats = []
    for hwnd, title, cls, vis in windows:
        if not vis or (cls != "ChatWnd" and "QWindowIcon" not in cls):
            continue
        if hwnd == main_hwnd or title.strip() == 'LINE':
            continue
        if hwnd == win32gui.GetForegroundWindow() and re.search(r'[\(\uFF08]\d+\+?[\)\uFF09]', title):
            continue
        paren_matches = re.findall(r'[\(\uFF08]\d+\+?[\)\uFF09]', title)
        if len(paren_matches) > 1:
            continue
        if re.search(GROUP_NAME_PATTERNS, title):
            continue
        if not win32gui.IsIconic(hwnd) and not verify_is_not_group_window_uia(hwnd):
            continue
        open_chats.append((hwnd, title, cls, vis))
    return open_chats

def get_window_true_rect(hwnd):
    """取得視窗精確矩形 (使用 DWM 去除 Windows 外框陰影)"""
    try:
        from ctypes import wintypes
        dwmapi = ctypes.windll.dwmapi
        dwm_rect = wintypes.RECT()
        DWMWA_EXTENDED_FRAME_BOUNDS = 9
        hr = dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(dwm_rect), ctypes.sizeof(dwm_rect))
        if hr == 0:
            return (dwm_rect.left, dwm_rect.top, dwm_rect.right, dwm_rect.bottom)
    except Exception:
        pass
    return win32gui.GetWindowRect(hwnd)

def get_system_dpi_scale(hwnd=None):
    """計算視窗或系統目前 DPI 縮放係數 (基準 96 DPI = 1.0)"""
    try:
        if hwnd and win32gui.IsWindow(hwnd):
            dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
            if dpi > 0:
                return dpi / 96.0
        dpi = ctypes.windll.user32.GetDpiForSystem()
        if dpi > 0:
            return dpi / 96.0
    except Exception:
        pass
    return 1.0

def focus_chat_input(hwnd, target_name=None):
    """聚焦對話視窗底部的輸入訊息區域 (點擊前強制驗證前景、括號與標題，絕不誤點群組視窗)"""
    try:
        # 嚴格前景核驗：只有目標視窗處於焦點前景時才允許注入點擊，杜絕點擊其他非相關視窗
        fg_hwnd = win32gui.GetForegroundWindow()
        if fg_hwnd != hwnd:
            log_v(f"[安全攔截] 視窗 (HWND: {hwnd}) 非焦點前景，禁止注入輸入框點擊！")
            return False

        # 標題括號清除輪詢：LINE 視窗啟動後，未讀括號 (如 "TargetUser (1)") 通常需 0.2~1.5 秒由 Qt 重繪清除。
        # 輪詢等待最多 2.0 秒，若標題括號清除則確立為合法 1:1 視窗；若 2 秒後仍帶有括號 (如 "Family (4)")，100% 判定為群組成員數視窗並拒絕點擊！
        is_group = bool(target_name) and is_group_target(target_name)       # 群組標題本來就帶成員數，不等也不當成「群組冒充 1:1」
        deadline = time.time() + (0 if is_group else 2.0)
        fg_title = win32gui.GetWindowText(hwnd).strip()
        while time.time() < deadline:
            if not re.search(r"[\(\uFF08]\d+\+?[\)\uFF09]", fg_title):
                break
            time.sleep(0.15)
            fg_title = win32gui.GetWindowText(hwnd).strip()

        if not is_group and re.search(r"[\(\uFF08]\d+\+?[\)\uFF09]", fg_title):
            log_v(f"[致命安全攔截] 視窗置於前景 2 秒後標題仍帶有括號「{fg_title}」，判定為群組成員數視窗，嚴禁點擊輸入框！")
            return False

        if not is_group and re.search(GROUP_NAME_PATTERNS, fg_title):
            log_v(f"[致命安全攔截] 視窗標題「{fg_title}」含有群組特徵關鍵字，判定為群組視窗，嚴禁點擊輸入框！")
            return False

        if target_name and not title_matches_target(fg_title, target_name):
            log_v(f"[致命安全攔截] 前景視窗標題「{fg_title}」與目標對象「{target_name}」不符，嚴禁點擊輸入框！")
            return False

        # 點擊前二次前景核驗，排除極端 TOCTOU
        if win32gui.GetForegroundWindow() != hwnd:
            log_v(f"[安全攔截] 點擊瞬間視窗失焦 (非前景)，安全攔截！")
            return False

        rect = win32gui.GetWindowRect(hwnd)
        w = rect[2] - rect[0]
        h = rect[3] - rect[1]
        if w > 600:
            # 雙欄式主視窗：聊天輸入框位於右側 65% 處
            click_x = rect[0] + int(w * 0.65)
        else:
            # 獨立小視窗：聊天輸入框位於水平中央 50% 處
            click_x = rect[0] + int(w * 0.5)
        scale = get_system_dpi_scale(hwnd)
        click_y = rect[1] + h - int(75 * scale)
        top = _root_window_at(click_x, click_y)
        if top != hwnd:
            _raise_to_top(hwnd)                 # 焦點對了但被別的視窗蓋住：提到最上層再核對一次
            time.sleep(0.2)
            top = _root_window_at(click_x, click_y)
        if top != hwnd:
            log_v(f"[安全攔截] 輸入框座標最上面的視窗 (HWND: {top}) 不是「{target_name or hwnd}」(HWND: {hwnd})，為免點到別的視窗，禁止點擊！")
            return False
        pyautogui.click(click_x, click_y)
        time.sleep(0.3)
        return True
    except Exception as e:
        log_v(f"[警告] 點擊輸入框失敗: {e}")
        return False

def find_line_main_window():
    windows = get_all_line_windows()
    for hwnd, title, cls, vis in windows:
        if title == "LINE" and "QWindowIcon" in cls and vis:
            return hwnd
    for hwnd, title, cls, vis in windows:
        if title == "LINE" and vis:
            return hwnd
    return None

class LineMainUi:
    """chat_opener 用的介面：實際操作 LINE 主視窗 (UIA 寫入搜尋欄、截圖、雙擊搜尋結果)"""

    def find_chat_window(self, name):
        return find_target_chat_window(name)

    def main_window(self):
        self._main = find_line_main_window()
        return self._main

    def bring_to_front(self, hwnd):
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        activate_window(hwnd)

    def is_front(self, hwnd):
        return win32gui.GetForegroundWindow() == hwnd

    def _search_edit(self, hwnd):
        import uiautomation as auto
        edit = auto.ControlFromHandle(hwnd).EditControl(ClassName="LcTextField")
        return edit if edit.Exists(1, 0.2) else None

    def search_box_rect(self, hwnd):
        edit = self._search_edit(hwnd)
        if edit is None:
            return None
        r = edit.BoundingRectangle
        return r.left, r.top, r.right, r.bottom

    def set_search_text(self, text):
        self._search_edit(self._main).GetValuePattern().SetValue(text)

    def clear_search(self):
        edit = self._search_edit(self._main)
        if edit is not None:
            edit.GetValuePattern().SetValue("")

    def double_click(self, x, y):
        pyautogui.doubleClick(x, y)

    # --- 分頁列 (全部 / 好友 / 群組 / ...)：UIA 的預設動作對它沒有作用，只能用滑鼠點；位置與選取狀態可以從 UIA 讀 ---
    MIN_MAIN_WIDTH = 1300           # 主視窗至少這麼寬，分頁才全部排得下 (捲動歸零、沒有淡出遮罩；窄一點第一個分頁會被遮罩蓋住點不到)

    def ensure_wide(self, hwnd):
        """主視窗太窄就暫時拉寬 (位置與高度不變)，回傳原本的 (左, 上, 右, 下) 讓呼叫端還原；不需要拉寬回傳 None"""
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if right - left >= self.MIN_MAIN_WIDTH:
            return None
        win32gui.MoveWindow(hwnd, left, top, self.MIN_MAIN_WIDTH, bottom - top, True)
        time.sleep(0.8)
        return left, top, right, bottom

    def restore_rect(self, hwnd, rect):
        left, top, right, bottom = rect
        win32gui.MoveWindow(hwnd, left, top, right - left, bottom - top, True)

    @staticmethod
    def _descendants(ctrl, depth=0, max_depth=6):
        if depth > max_depth:
            return
        for child in ctrl.GetChildren():
            yield child
            yield from LineMainUi._descendants(child, depth + 1, max_depth)

    def _tab_buttons(self, hwnd):
        import uiautomation as auto
        bar = auto.ControlFromHandle(hwnd).GroupControl(ClassName="TabBar")
        if not bar.Exists(1, 0.2):
            return None, []
        return bar, [c for c in self._descendants(bar) if c.ClassName == "TabButton"]

    def selected_tab(self, hwnd):
        """目前選取的分頁索引 (被選取的分頁底下有一條 3px 高的底線)；讀不到回傳 None"""
        _, buttons = self._tab_buttons(hwnd)
        for index, button in enumerate(buttons):
            for c in self._descendants(button, max_depth=3):
                r = c.BoundingRectangle
                if c.ClassName == "LcWidget" and 0 < r.bottom - r.top <= 4 and r.right - r.left > 20:
                    return index
        return None

    def select_tab(self, hwnd, index):
        """
        切到指定分頁並驗證 (底線標記要移過去)。分頁被捲到看不見、主視窗不在前景、點擊位置不在主視窗內都不動，
        回傳是否成功。絕不在分頁列上拖曳 (分頁列屬於標題列，拖曳會拖動整個視窗)。
        """
        bar, buttons = self._tab_buttons(hwnd)
        if bar is None or index >= len(buttons) or not self.is_front(hwnd):
            return False
        view, tab = bar.BoundingRectangle, buttons[index].BoundingRectangle
        x, y = (tab.left + tab.right) // 2, (tab.top + tab.bottom) // 2
        if not (view.left <= x < view.right and view.top <= y < view.bottom):
            return False                                             # 中心點在可視區外 = 被捲到看不見；邊緣被切掉幾 px 沒關係
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if not (left <= x < right and top <= y < bottom):
            return False
        pyautogui.click(x, y)
        time.sleep(0.6)
        return self.selected_tab(hwnd) == index

    def screenshot(self, hwnd):
        rect = get_window_true_rect(hwnd)
        return ImageGrab.grab(bbox=rect, all_screens=True), (rect[0], rect[1])

def open_missing_chat_window(name, config):
    """好友的獨立聊天視窗不見了：從 LINE 主視窗的搜尋欄開回來 (auto_open_chat_window 關閉時不做)；成功回傳 True"""
    if not config.get("auto_open_chat_window", True):
        return False
    if time.time() - _DUPLICATE_WINDOW_SEEN.get(name.strip().lower(), -1e9) < DUPLICATE_WINDOW_SKIP_SECONDS:
        log_v(f" -> 「{name}」不是沒有視窗，而是桌面上有重複的同名視窗；請關掉多餘的，不去搶 LINE 主視窗開窗。")
        return False
    log_v(f" -> 找不到「{name}」的獨立聊天視窗，嘗試從 LINE 主視窗開啟...")
    before = {w[0] for w in get_all_line_windows()}         # 開窗前桌面上所有 LINE 視窗：只有「新出現」的才算我們開的
    opened = chat_opener.open_chat_window(name, LineMainUi(), log=log_v, allow_member_count=is_group_target(name))
    if opened:
        wins = find_target_chat_window(name)
        if wins and wins[0][0] not in before:
            _BOT_OPENED_HWNDS[wins[0][0]] = (time.time(), name)      # 記下：這個視窗是我們開出來的，用完可以關
    return opened

OPEN_ATTEMPT_MIN_INTERVAL = 60.0           # 同一個對象，偵測階段最短隔多久才再從主視窗開窗一次
_LAST_OPEN_ATTEMPT = {}


def _worth_opening_window(target, config, now, cooldown):
    """
    圖片快取偵測到新圖、但對象的視窗不在時，要不要去搶 LINE 主視窗把視窗開回來。只做不碰任何視窗的便宜檢查：
    今天已達回覆上限、還在冷卻、或剛嘗試開過 (每個對象每分鐘最多一次) 就不開，因為反正不會回覆，不值得打擾你的主視窗。
    (是不是貼圖、是不是長輩圖要等視窗開了才看得出來，那一步沒辦法省。)
    """
    if is_group_target(target):
        max_per_day = config.get("group_max_replies_per_day", DEFAULT_CONFIG["group_max_replies_per_day"])
        cd = config.get("group_cooldown_seconds", DEFAULT_CONFIG["group_cooldown_seconds"])
    else:
        max_per_day = config.get("max_replies_per_day", reply_budget.DEFAULT_MAX_PER_DAY)
        cd = cooldown
    if not reply_budget.can_reply(REPLY_COUNTS_PATH, target, max_per_day):
        return False
    if now - last_reply_times.get(target, 0) < cd:
        return False
    if now - _LAST_OPEN_ATTEMPT.get(target, -1e9) < OPEN_ATTEMPT_MIN_INTERVAL:
        return False
    _LAST_OPEN_ATTEMPT[target] = now
    return True


def activate_window(hwnd):
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        else:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)

        pyautogui.press('alt')
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.3)
        if win32gui.GetForegroundWindow() != hwnd:
            _force_foreground(hwnd)
        _raise_to_top(hwnd)
        return True
    except Exception as e:
        log_v(f"[警告] 前景切換失敗: {e}")
        return False


def _raise_to_top(hwnd):
    """
    把視窗提到所有一般視窗的最上層 (不搶焦點、不改大小位置)。真機實測：被放到最底層的視窗即使成了前景 (焦點對了)，
    仍可能被別的視窗蓋住，這時點輸入框點到的是上面那個視窗。
    """
    try:
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)
    except Exception:
        pass


def _root_window_at(x, y):
    """螢幕座標 (x, y) 最上面那個視窗的最上層擁有者；讀不到回傳 None"""
    try:
        user32 = ctypes.windll.user32
        point = ctypes.wintypes.POINT(int(x), int(y))
        found = user32.WindowFromPoint(point)
        return user32.GetAncestor(found, 2) or found or None      # 2 = GA_ROOT
    except Exception:
        return None


def _force_foreground(hwnd):
    """
    一般的 SetForegroundWindow 被 Windows 的前景鎖擋下時 (真機實測：展開、放在最底層的視窗，使用者正在別的視窗打字時就會失敗) 的備案：
    先附加到目前前景視窗的輸入執行緒再切換；還是不行就縮小再還原 (還原一定會啟用視窗)。回傳是否已在前景。
    每個階段的結果都寫進紀錄，方便查是哪一步被擋。
    """
    def in_front():
        try:
            return win32gui.GetForegroundWindow() == hwnd
        except Exception:
            return False

    try:
        user32 = ctypes.windll.user32
        fg = win32gui.GetForegroundWindow()
        fg_thread = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
        me = ctypes.windll.kernel32.GetCurrentThreadId()
        attached = bool(fg_thread and fg_thread != me and user32.AttachThreadInput(me, fg_thread, True))
        try:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(me, fg_thread, False)
        time.sleep(0.2)
        if in_front():
            log_v(f" -> [前景切換] 一般切換被擋，改附加輸入執行緒 (附加={attached}) 後成功。")
            return True
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        time.sleep(0.2)
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.3)
    except Exception as e:
        log_v(f"[警告] 前景切換備案發生例外: {e}")
    ok = in_front()
    log_v(f" -> [前景切換] 一般切換與附加輸入執行緒都被擋，改用縮小再還原：{'成功' if ok else '仍失敗'}。")
    return ok


_BACKGROUND_WINDOWS = set()     # 文字偵測在看的視窗 (展開、放背景)：回覆完要放回最底層，不能縮小 (縮小後 LINE 不顯示任何新訊息記號)


def _send_to_background(hwnd):
    """把視窗放到所有視窗的最底層，不縮小、不搶焦點、不改大小位置"""
    win32gui.SetWindowPos(hwnd, win32con.HWND_BOTTOM, 0, 0, 0, 0,
                          win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)


_BOT_OPENED_HWNDS = {}          # {視窗 hwnd: 開出來的時間}：機器人自己從主視窗開出來的聊天視窗 (使用者本來就開著的不會在這裡)
BOT_OPENED_MAX_AGE = 3600.0     # 登記超過這麼久就不關了 (可能已經是使用者在用，也避免 hwnd 被別的視窗重用)


def _close_if_bot_opened(hwnd, config, log_target=None):
    """
    只關「機器人自己開出來的」聊天視窗 (close_opened_windows)：本來就開著的視窗、文字偵測正在監看的視窗 (關掉就偵測不到下一則)、
    登記太久的視窗、標題已經不是登記時那個對象的視窗一律不碰。用 WM_CLOSE 關單一視窗，確認視窗真的消失了才算數。
    關了回傳 True；沒關成 (LINE 沒理、跳確認視窗) 回傳 False 並保留登記，呼叫端照常最小化。
    """
    entry = _BOT_OPENED_HWNDS.get(hwnd)
    if entry is None or not config.get("close_opened_windows", True) or hwnd in _BACKGROUND_WINDOWS:
        return False
    opened_at, name = entry
    try:
        if (time.time() - opened_at > BOT_OPENED_MAX_AGE or not win32gui.IsWindow(hwnd)
                or not title_matches_target(win32gui.GetWindowText(hwnd), name)):
            _BOT_OPENED_HWNDS.pop(hwnd, None)           # 過期、視窗不見、或 hwnd 被別的視窗重用 (標題對不上)：不動它
            return False
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        for _ in range(5):
            time.sleep(0.2)
            if not win32gui.IsWindow(hwnd):
                _BOT_OPENED_HWNDS.pop(hwnd, None)
                log_v(f" -> 已關閉機器人自己開出來的「{log_target or name}」視窗。")
                return True
        log_v(f"[警告] 關閉「{log_target or name}」視窗後視窗還在 (LINE 沒有關掉它)，改為最小化。")
        return False
    except Exception:
        return False


def _minimize_if_configured(hwnd, config, log_target=None):
    """安全將視窗最小化以維持後續未讀狀態；文字偵測在看的視窗改放到最底層 (維持展開才偵測得到下一則)"""
    if config.get("close_window_after_send", True) and hwnd:
        try:
            if hwnd in _BACKGROUND_WINDOWS:
                _send_to_background(hwnd)
                if log_target:
                    log_v(f" -> 已將「{log_target}」視窗放到背景，維持展開以偵測下一則文字。")
                return
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
            if log_target:
                log_v(f" -> 已自動將「{log_target}」視窗最小化，維持後續未讀狀態。")
        except Exception:
            pass

def _finish_with_window(hwnd, config, log_target=None):
    """
    這個視窗的事辦完了、不會再重試 (送出成功、確定略過、自動綁定結束)：機器人自己開的就關掉，其餘照 _minimize_if_configured 放回去。
    要重試的路徑 (NOT_SENT) 與「看一眼」那一步不用這個：關掉的話重試時得重新開窗。
    """
    if hwnd and _close_if_bot_opened(hwnd, config, log_target):
        return
    _minimize_if_configured(hwnd, config, log_target)


# 全域發送時間、時間窗、快取差集快照與斷路器
last_global_reply_time = 0
last_seen_cache_mtime = time.time()
LAST_PROCESSED_UNREAD = {}     # {target_name: unread_count} 嚴格未讀去重（僅在未讀數高於已處理基準時觸發）
IGNORED_CACHE_FILES = {}       # {file_path: (timestamp, mtime, size)} 本機發送快取差集黑名單 (含 TTL 清理與內容比對)
PROCESSED_CACHE_FILES = set()  # {(file_path, mtime)} 同 mtime 候選多檔案安全追蹤集合
SEND_FAIL_COUNTS = {}          # {target_name: fail_count} 暫時發送失敗次數計數器
PENDING_RETRY_MEDIA = {}       # {target_name: file_path} 發送暫時失敗待重試之媒體
_state_lock = threading.RLock() # 使用可重入鎖 (RLock) 徹底避免重入死結

CIRCUIT_BREAKER_PATH = os.path.join(SCRIPT_DIR, "circuit_breaker.json")

def load_circuit_breaker_state():
    """載入持久化的斷路器狀態，支援完整型別驗證、嚴格 Schema 檢驗與跨重啟應急鎖定"""
    state = {
        "blocked_until": {},
        "reply_timestamps": {},
        "last_sent_times": {},
        "last_processed_unread": {}
    }
    # 檢查是否存在跨重啟應急鎖定標記 (例如前次執行時磁碟寫入失敗引發的 Fail-Closed)
    emergency_lock_path = CIRCUIT_BREAKER_PATH + ".emergency_lock"
    if os.path.exists(emergency_lock_path):
        try:
            with open(emergency_lock_path, "r", encoding="utf-8") as f_em:
                em_data = json.load(f_em)
                if isinstance(em_data, dict) and em_data.get("__GLOBAL__", 0) > time.time():
                    state["blocked_until"]["__GLOBAL__"] = float(em_data["__GLOBAL__"])
                    log_v("[安全防禦生效] 偵測到跨重啟應急 Fail-Closed 鎖定標記，全域鎖定持續生效中！")
        except Exception:
            state["blocked_until"]["__GLOBAL__"] = time.time() + 1800

    if os.path.exists(CIRCUIT_BREAKER_PATH):
        try:
            with open(CIRCUIT_BREAKER_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("circuit_breaker.json 格式異常 (根節點非 dict)")

            # 嚴格 Schema 驗證：若欄位存在但內部型別損壞，整體判定為損毀並 Fail-Closed
            now = time.time()
            if "blocked_until" in data:
                if not isinstance(data["blocked_until"], dict):
                    raise ValueError("blocked_until 不是 dict")
                parsed_blocked = {}
                for k, v in data["blocked_until"].items():
                    if not isinstance(v, (int, float)):
                        raise ValueError(f"blocked_until 數值非數字: {v}")
                    if v > now and v <= now + 3600:
                        parsed_blocked[str(k)] = float(v)
                state["blocked_until"].update(parsed_blocked)

            if "reply_timestamps" in data:
                if not isinstance(data["reply_timestamps"], dict):
                    raise ValueError("reply_timestamps 不是 dict")
                parsed_replies = {}
                for k, v in data["reply_timestamps"].items():
                    if not isinstance(v, list) or not all(isinstance(t, (int, float)) for t in v):
                        raise ValueError(f"reply_timestamps 包含非數字串列: {v}")
                    parsed_replies[str(k)] = [float(t) for t in v]
                state["reply_timestamps"] = parsed_replies

            if "last_sent_times" in data:
                if not isinstance(data["last_sent_times"], dict):
                    raise ValueError("last_sent_times 不是 dict")
                state["last_sent_times"] = {
                    str(k): float(v) for k, v in data["last_sent_times"].items()
                    if isinstance(v, (int, float))
                }

            if "last_processed_unread" in data:
                if not isinstance(data["last_processed_unread"], dict):
                    raise ValueError("last_processed_unread 不是 dict")
                state["last_processed_unread"] = {
                    str(k): int(v) for k, v in data["last_processed_unread"].items()
                    if isinstance(v, int)
                }
        except Exception as e:
            log_v(f"[致命安全警告] 讀取斷路器狀態檔失敗 ({e})，遵循 Fail-Closed 原則啟動全域 30 分鐘防禦鎖定！")
            now_t = time.time()
            state["blocked_until"]["__GLOBAL__"] = now_t + 1800
    return state

_cb_initial_state = load_circuit_breaker_state()
CIRCUIT_BREAKER_BLOCKED_UNTIL = _cb_initial_state.get("blocked_until", {})
REPLY_HISTORY_TIMESTAMPS = _cb_initial_state.get("reply_timestamps", {})
REPLY_TIMESTAMPS = REPLY_HISTORY_TIMESTAMPS
LAST_SENT_TIME_BY_TARGET = _cb_initial_state.get("last_sent_times", {})
LAST_PROCESSED_UNREAD = _cb_initial_state.get("last_processed_unread", {})

def save_circuit_breaker_state():
    """原子持久化儲存斷路器狀態 (寫入失敗時觸發跨重啟 Fail-Closed 應急鎖定，並回傳是否成功)"""
    with _state_lock:
        state = {
            "blocked_until": CIRCUIT_BREAKER_BLOCKED_UNTIL,
            "reply_timestamps": REPLY_TIMESTAMPS,
            "last_sent_times": LAST_SENT_TIME_BY_TARGET,
            "last_processed_unread": LAST_PROCESSED_UNREAD,
        }
        success = False
        try:
            success = atomic_save_json(CIRCUIT_BREAKER_PATH, state)
        except Exception:
            success = False
        if not success:
            log_v("[斷路器持久化警告] 儲存狀態失敗，啟動全域 30 分鐘 Fail-Closed 鎖定並持久化跨重啟標記！")
            now_lock = time.time() + 1800
            CIRCUIT_BREAKER_BLOCKED_UNTIL["__GLOBAL__"] = now_lock
            try:
                emergency_lock_path = CIRCUIT_BREAKER_PATH + ".emergency_lock"
                atomic_save_json(emergency_lock_path, {"__GLOBAL__": now_lock})
            except Exception:
                pass
        return success


def get_active_line_cache_dir():
    cache_base = os.path.expandvars(r"%LOCALAPPDATA%\LINE\Cache")
    if not os.path.exists(cache_base):
        return None
    subdirs = []
    for d in os.listdir(cache_base):
        p = os.path.join(cache_base, d)
        if os.path.isdir(p) and len(d) == 40:
            try:
                subdirs.append((os.path.getmtime(p), p))
            except Exception:
                pass
    if not subdirs:
        return cache_base
    subdirs.sort(key=lambda x: x[0], reverse=True)
    newest_dir = subdirs[0][1]
    # 智慧辨識：檢查是否為雙層結構 (目錄內是否還有 40 字元的子目錄)
    has_nested_hash = False
    try:
        for sub in os.listdir(newest_dir):
            if len(sub) == 40 and os.path.isdir(os.path.join(newest_dir, sub)):
                has_nested_hash = True
                break
    except Exception:
        pass
    if has_nested_hash:
        return newest_dir  # 雙層結構：返回最新帳號目錄
    else:
        return cache_base  # 單層結構：返回 Cache 根目錄以涵蓋全部聊天室！

def extract_chat_hash_from_path(file_path):
    r"""
    從快取路徑精確提取對話目錄 Hash (40 位元雜湊字串)：
    相對於 Cache 根目錄，若結構為 Cache\<account_hash>\<chat_hash>\... 則取第二層 chat_hash；
    若結構為 Cache\<chat_hash>\... 則直接取該 chat_hash。
    """
    cache_base = os.path.expandvars(r"%LOCALAPPDATA%\LINE\Cache")
    if file_path.lower().startswith(cache_base.lower()):
        rel = os.path.relpath(file_path, cache_base)
        parts = os.path.normpath(rel).split(os.sep)
        hashes = [p for p in parts if len(p) == 40 and re.fullmatch(r"[0-9a-fA-F]{40}", p)]
        if len(hashes) >= 2:
            return hashes[1]
        elif len(hashes) == 1:
            # Cache\<account_hash>\tmp\... 只含帳號層 Hash，不屬於任何對話頻道 (例如 tmp\messageImage_*.jpg)
            if len(parts) >= 2 and parts[1].lower() == "tmp":
                return None
            return hashes[0]
    norm_parts = os.path.normpath(file_path).split(os.sep)
    hashes = [p for p in norm_parts if len(p) == 40 and re.fullmatch(r"[0-9a-fA-F]{40}", p)]
    return hashes[-1] if hashes else None

def parse_unread_count(raw_val):
    """
    統一解析 LINE 視窗標題未讀訊息數：
    - 若為 '99+' 等溢位標記，統一映射為邊緣數值 100
    - 一般數字字串轉為對應整數
    - 確保全系統邊緣觸發與基準線更新 100% 嚴格一致，徹底杜絕 99+ 冷卻後無窮迴圈重複發送
    """
    if not raw_val:
        return 0
    s = str(raw_val).strip()
    if s.endswith("+"):
        return 100
    try:
        return int(s)
    except Exception:
        return 0

def load_chat_dirs_map():
    """安全讀取並過濾 chat_dirs.json，嚴格驗證 {40位hex雜湊: 非空聯絡人字串}，防禦非預期型別"""
    if not os.path.exists(CHAT_MAP_PATH):
        return {}
    try:
        with open(CHAT_MAP_PATH, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        valid_map = {}
        for k, v in raw.items():
            if isinstance(k, str) and re.fullmatch(r"[0-9a-fA-F]{40}", k) and isinstance(v, str) and v.strip():
                valid_map[k] = v.strip()
        return valid_map
    except Exception as e:
        log_v(f"[警告] 讀取 chat_dirs.json 失敗: {e}")
        return {}


def channel_label(chat_hash):
    """
    紀錄裡的快取頻道標示：已綁定的帶聊天室名稱，例如「b1acf4e0...」(林如婷)；沒綁定的標「未綁定」。
    頻道代號是 LINE 自己產生的雜湊，光看代號分不出是誰，所以每一行紀錄都一起帶出名稱。
    """
    short = f"「{(chat_hash or '')[:8]}...」"
    try:
        name = next((n for h, n in load_chat_dirs_map().items() if h.lower() == (chat_hash or "").lower()), None)
    except Exception:
        name = None
    return f"{short}({name})" if name else f"{short}(未綁定)"


def load_provisional_bindings():
    """從 chat_dirs.json 安全讀取已持久化之暫定綁定清單，支援跨重啟延續核驗 (🔴 核心修復：防止重啟遺失暫定狀態)"""
    if not os.path.exists(CHAT_MAP_PATH):
        return {}
    try:
        with open(CHAT_MAP_PATH, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
        if isinstance(raw, dict) and isinstance(raw.get("_provisional"), dict):
            prov = {}
            for k, v in raw["_provisional"].items():
                if isinstance(k, str) and re.fullmatch(r"[0-9a-fA-F]{40}", k) and isinstance(v, dict):
                    target = v.get("target")
                    if isinstance(target, str) and target.strip():
                        prov[k] = {
                            "target": target.strip(),
                            "bind_fp": v.get("bind_fp"),
                            "bind_time": float(v.get("bind_time", 0.0)),
                        }
            return prov
    except Exception:
        pass
    return {}

def acquire_chat_dirs_mutex(timeout_ms=5000):
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        h = kernel32.CreateMutexW(None, False, "Local\\LineGrandmaBot_ChatDirs_Mutex")
        if h:
            wait_res = kernel32.WaitForSingleObject(h, timeout_ms)
            WAIT_OBJECT_0 = 0
            WAIT_ABANDONED = 0x80
            if wait_res not in (WAIT_OBJECT_0, WAIT_ABANDONED):
                kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
                kernel32.CloseHandle.restype = ctypes.c_bool
                kernel32.CloseHandle(h)
                return None
        return h
    except Exception:
        return None

def release_chat_dirs_mutex(handle):
    if handle:
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
            kernel32.ReleaseMutex.restype = ctypes.c_bool
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_bool
            kernel32.ReleaseMutex(handle)
            kernel32.CloseHandle(handle)
        except Exception:
            pass

def save_chat_dirs_map(chat_map):
    """安全原子寫入 chat_dirs.json 頻道對應表"""
    h = acquire_chat_dirs_mutex()
    if not h:
        return False
    try:
        return atomic_save_json(CHAT_MAP_PATH, chat_map)
    finally:
        release_chat_dirs_mutex(h)

@contextlib.contextmanager
def locked_chat_dirs(timeout_ms=5000):
    """跨行程具名 Mutex 上下文管理器，完整包覆 load -> mutate -> save 整個讀寫循環，支援暫定綁定持久化 (Fail-Closed)"""
    h = acquire_chat_dirs_mutex(timeout_ms)
    if not h:
        raise TimeoutError("無法取得 chat_dirs 互斥鎖 (等待逾時或鎖建立失敗)")
    try:
        raw_data = {}
        if os.path.exists(CHAT_MAP_PATH):
            try:
                with open(CHAT_MAP_PATH, "r", encoding="utf-8-sig") as f:
                    content = json.load(f)
                if isinstance(content, dict):
                    raw_data = content
                else:
                    raise ValueError(f"{CHAT_MAP_PATH} 根節點格式非 dict")
            except Exception as e:
                log_v(f"[致命安全攔截] 讀取 {CHAT_MAP_PATH} 失敗: {e}，遵循 Fail-Closed 拒絕以空字典覆寫磁碟！")
                raise IOError(f"讀取 {CHAT_MAP_PATH} 失敗: {e}")
        yield raw_data
        saved = atomic_save_json(CHAT_MAP_PATH, raw_data)
        if not saved:
            raise IOError(f"寫入 {CHAT_MAP_PATH} 原子替換失敗！")
    finally:
        release_chat_dirs_mutex(h)

AUTOBIND_TIMEOUT = 15.0       # 自動學習綁定超時統一常數 (涵蓋滑動視窗並行隔離、暫緩重試與延遲渲染重測，15.0s)
AUTOBIND_TIME_WINDOW = 6.0    # 快取檔案寫入時間 (cand_m) 與視窗訊息異動時間之容許時間差範圍 (±6.0s)
_TARGET_WINDOW_STATE = {}     # {target_name: {"fp": str, "changed_at": float, "consumed": bool, "has_media": bool, "is_incoming": bool}}
_RECENT_UNKNOWN_CHANNELS = {} # {chat_hash: timestamp} 用於 15 秒滑動視窗多頻道並行隔離防禦
_PROVISIONAL_BINDINGS = load_provisional_bindings() # 跨重啟持久化暫定綁定狀態

def _fetch_chat_window_items_info(hwnd):
    """底層讀取聊天室控制項最末與次末項目名稱及指紋"""
    try:
        import uiautomation as auto
        win = auto.ControlFromHandle(hwnd)
        if win and win.Exists(0, 0):
            view = win.ListControl(ClassName="LcListView")
            if view and view.Exists(0, 0):
                items = view.GetChildren()
                if items:
                    last_name = items[-1].Name or ""
                    prev_name = items[-2].Name or "" if len(items) > 1 else ""
                    combined = f"{len(items)}|{last_name}|{prev_name}"
                    fp_hash = hashlib.sha256(combined.encode("utf-8", errors="ignore")).hexdigest()[:16]
                    return f"{len(items)}_{fp_hash}", last_name, prev_name
    except Exception:
        pass
    return None, "", ""

def get_chat_window_fingerprint(hwnd):
    """取得聊天室視窗內部訊息指紋"""
    fp, _, _ = _fetch_chat_window_items_info(hwnd)
    return fp

def get_chat_window_details(hwnd):
    """取得聊天室指紋與訊息名稱 (單次讀取 UIA)"""
    return _fetch_chat_window_items_info(hwnd)

def get_chat_latest_media_info(hwnd):
    """
    透過 UIA 偵測目標視窗最末項目中的多媒體元素 (ImageControl) 與實體位置方向 (高度嚴重核心防禦)：
    1. 支援 Per-Monitor 高 DPI 動態縮放門檻 (int(80 * scale))，徹底排除 36x36 / 40x40 之好友大頭貼
    2. 深度遞迴 (depth <= 3) 穿透 Qt 巢狀 Pane 尋找真實 ImageControl
    3. 量測邊距：(ir.left - vr.left) < (vr.right - ir.right) 或 left_margin <= 75*scale，寬圖與窄視窗皆精準判別
    4. 完全免啟動視窗、免前景視窗、不搶奪使用者焦點
    回傳: (has_media: bool, is_incoming: bool)
    """
    try:
        import uiautomation as auto
        win = auto.ControlFromHandle(hwnd)
        if win and win.Exists(0, 0):
            view = win.ListControl(ClassName="LcListView")
            if view and view.Exists(0, 0):
                items = view.GetChildren()
                if items:
                    last_item = items[-1]
                    vr = view.BoundingRectangle

                    # 動態計算視窗 DPI 縮放係數 (高 DPI 下頭像 40px 會被縮放為 60px~80px)
                    scale = 1.0
                    try:
                        dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
                        if dpi > 0:
                            scale = dpi / 96.0
                    except Exception:
                        pass

                    # 動態尺寸門檻：100% DPI -> 80px, 150% DPI -> 120px, 200% DPI -> 160px
                    min_media_size = int(80 * scale)

                    # 深度遞迴搜尋真實 ImageControl 圖片/貼圖控制項 (最多遞迴 3 層以穿透 Qt 巢狀 Pane)
                    def find_media_images(element, depth=0, max_depth=3):
                        if not element or depth > max_depth:
                            return []
                        found = []
                        try:
                            children = element.GetChildren()
                        except Exception:
                            return []
                        for child in children:
                            try:
                                ctype = getattr(child, "ControlTypeName", "")
                                if ctype == "ImageControl":
                                    r = child.BoundingRectangle
                                    w = r.right - r.left if hasattr(r, "right") else r.width()
                                    h = r.bottom - r.top if hasattr(r, "bottom") else r.height()
                                    if w >= min_media_size and h >= min_media_size:
                                        found.append(child)
                                        continue  # 找到圖片控制項後不需再向下遞迴子節點
                                found.extend(find_media_images(child, depth + 1, max_depth))
                            except Exception:
                                continue
                        return found

                    media_images = find_media_images(last_item)

                    if media_images:
                        target_img = media_images[-1]
                        ir = target_img.BoundingRectangle
                        left_margin = ir.left - vr.left
                        right_margin = vr.right - ir.right

                        # 判定實體位置方向：
                        # 依實體 LINE PC (Qt6) 視窗量測：
                        # 1. 對方傳入 (Incoming)：左側有好友頭像 (36~40px) 與邊距，ImageControl 靠左 (left_margin 約 50~75px * scale)。
                        # 2. 本機送出 (Outgoing)：右側貼近視窗邊界 (右邊距約 15~35px * scale，無大頭貼)。
                        # 3. 判定邏輯：
                        #    - 本機送出必定靠右 (right_margin < left_margin)，故若 left_margin < right_margin 必定為對方傳入。
                        #    - 若 left_margin >= right_margin (例如窄視窗超寬圖)：
                        #      * 若 right_margin <= 35 * scale，判定為本機送出 (is_incoming = False，Fail-safe 拒絕綁定)。
                        #      * 若 left_margin <= 75 * scale，判定為對方傳入 (is_incoming = True)。
                        #      * 其他情況無法確證，Fail-safe 拒絕。
                        if left_margin < right_margin:
                            is_incoming = True
                        elif right_margin <= int(35 * scale):
                            is_incoming = False
                        elif left_margin <= int(75 * scale):
                            is_incoming = True
                        else:
                            is_incoming = False
                        return True, is_incoming
    except Exception:
        pass
    return False, False

def is_chat_latest_message_media(hwnd):
    """相容層：核驗最末訊息是否為多媒體"""
    has_media, _ = get_chat_latest_media_info(hwnd)
    return has_media

def is_chat_latest_message_incoming(hwnd):
    """相容層：核驗最末訊息是否為對方傳入"""
    _, is_incoming = get_chat_latest_media_info(hwnd)
    return is_incoming

def update_target_window_fingerprints(targets):
    """在主監控迴圈持續輪詢目標視窗指紋，記錄異動時間戳並自動調整更新縮圖渲染狀態 (防延遲載入盲點)"""
    now = time.time()
    for t in targets:
        wins = find_target_chat_window(t)
        if wins:
            hwnd = wins[0][0]
            fp, last_name, prev_name = get_chat_window_details(hwnd)
            if fp is not None:
                state = _TARGET_WINDOW_STATE.get(t)
                if state is None:
                    # 初次建立基準，不標記為異動 (changed_at = 0, consumed = True)
                    _TARGET_WINDOW_STATE[t] = {
                        "fp": fp,
                        "last_name": last_name,
                        "prev_name": prev_name,
                        "changed_at": 0.0,
                        "consumed": True
                    }
                elif state["fp"] != fp:
                    # 偵測到新指紋異動！
                    has_media, is_incoming = get_chat_latest_media_info(hwnd)
                    old_fp = state.get("fp", "")
                    try:
                        old_count = int(old_fp.split("_")[0]) if "_" in old_fp else -1
                        new_count = int(fp.split("_")[0]) if "_" in fp else -1
                    except Exception:
                        old_count, new_count = -1, -1

                    is_same_last_item = bool(last_name and last_name == state.get("last_name") and old_count == new_count)

                    # 🟡 核心防禦 1：若最末項未變且項目數未變 (次末項已讀/分隔線更新，或舊圖保留)：
                    # 若先前已有媒體或已消費，嚴禁將既有舊圖誤當作新圖片異動建立新狀態！
                    if is_same_last_item and (state.get("has_media") or state.get("consumed")):
                        state["fp"] = fp
                        state["prev_name"] = prev_name
                        continue

                    # 🟡 核心防禦 2：精確判定同訊息縮圖安定化：
                    # 僅在「先前無媒體且未消費、目前解析出媒體、且最末項名稱同為同一則訊息」時才保留 first_changed_at；
                    # 若最末項名稱改變或項目數增加 (如文字後傳圖)，一律嚴格視為新訊息抵達重置時間！
                    is_thumbnail_settle = (
                        not state.get("consumed")
                        and not state.get("has_media")
                        and has_media
                        and is_same_last_item
                        and (now - state.get("changed_at", 0) <= AUTOBIND_TIMEOUT)
                    )
                    first_t = state.get("first_changed_at", state["changed_at"]) if is_thumbnail_settle else now
                    media_seen = now if has_media else None
                    last_fp_t = state.get("last_fp_change", state["changed_at"]) if is_thumbnail_settle else now
                    _TARGET_WINDOW_STATE[t] = {
                        "fp": fp,
                        "last_name": last_name,
                        "prev_name": prev_name,
                        "changed_at": first_t,
                        "first_changed_at": first_t,
                        "last_fp_change": last_fp_t,
                        "consumed": False,
                        "has_media": has_media,
                        "is_incoming": is_incoming,
                        "last_retest_at": now,
                        "media_seen_at": media_seen,
                        "is_thumbnail_settle": is_thumbnail_settle,
                    }
                elif not state.get("consumed") and not state.get("has_media") and (now - state.get("changed_at", 0) <= AUTOBIND_TIMEOUT):
                    # 🔴 核心防禦：若指紋已變動但縮圖處於非同步延遲載入 (尚未出現 ImageControl)，在超時範圍內節流重測 (至少間隔 1.0s)
                    if now - state.get("last_retest_at", 0) >= 1.0:
                        state["last_retest_at"] = now
                        has_media, is_incoming = get_chat_latest_media_info(hwnd)
                        if has_media:
                            state["has_media"] = True
                            state["is_incoming"] = is_incoming
                            state["media_seen_at"] = now
                            state["is_thumbnail_settle"] = True
        else:
            # 視窗已關閉：主動清除狀態紀錄，防止重新開啟視窗時因新控制項指紋誤觸發異動
            _TARGET_WINDOW_STATE.pop(t, None)

def init_target_window_fingerprints(specific_targets=None):
    """初始化監控目標視窗指紋，用於高可信度自動學習綁定"""
    targets = specific_targets or []
    update_target_window_fingerprints(targets)

def try_auto_bind_channel(ch, cand_path, cand_m, targets, chat_dirs_map, competing_unknown_count=1):
    """
    對未知快取頻道進行高可信度自動學習綁定：
    1. 必須有且僅有 1 位目標好友視窗上線
    2. 該視窗必須通過 UIA 1:1 核驗
    3. 近 15 秒滑動視窗不可有 2 個以上不同之未知頻道同時活動 (中度 #1 並行滑動視窗防禦)
    4. 該視窗近期必須剛發生未消費之內容指紋異動 (杜絕拿舊歷史誤認他人頻道)
    5. 快取檔案寫入時間 (cand_m) 與視窗異動時間 (changed_at) 差距在容許範圍內 (-6.0s ~ +6.0s)
    6. 該視窗最末訊息必須為真實照片/貼圖 (高 DPI 動態門檻，排除純文字與大頭貼，延遲載入自動調整重測)
    7. 該視窗最末照片必須為對方傳入 (量測 ImageControl 左右邊距，寬圖亦精準判定，完全不搶焦點)
    8. 持久化寫入成功後才標記已消費，寫入失敗立即撤銷
    """
    if ch in chat_dirs_map:
        return chat_dirs_map[ch]

    if competing_unknown_count > 1:
        log_v(f"[安全隔離] 滑動視窗內偵測到 {competing_unknown_count} 個不同之未知快取頻道，存在多頻道衝突風險，拒絕自動綁定！")
        return None

    candidate_targets = []
    for t in targets:
        if is_group_target(t):
            continue                    # 群組只能手動綁定 (--bind-view)：開著的群組視窗不能算候選，更不能被自動綁上
        wins = find_target_chat_window(t)
        if wins and verify_is_not_group_window_uia(wins[0][0]):
            candidate_targets.append((t, wins[0][0]))

    if len(candidate_targets) != 1:
        # 若無上線目標或有多個上線目標上線，無法唯一判定歸屬，安全隔離拒絕盲目自動綁定
        return None

    target_name, hwnd = candidate_targets[0]

    # 檢查桌面上所有開啟之 1:1 獨立視窗，若有非候選目標之視窗存在，隔離拒絕
    all_open_chats = get_all_open_chat_windows()
    non_target_chats = [c for c in all_open_chats if c[0] != hwnd and c[1].strip().lower() != target_name.lower()]
    if non_target_chats:
        log_v(f"[安全隔離] 桌面上偵測到非監控對象之聊天視窗 (「{non_target_chats[0][1]}」)，存在歸屬混淆風險，拒絕自動綁定！")
        return None

    # 視窗最小化處理：不強制搶奪焦點彈出
    if win32gui.IsIconic(hwnd):
        log_v(f"[安全隔離] 目標好友「{target_name}」視窗處於最小化狀態，為避免搶奪焦點不強制彈出；請保持視窗可見以完成初次自動綁定。")
        return None

    # 1. 訊息內容指紋異動驗證與防漂移核驗 (🔴 核心防禦：Fail-Closed)：
    state = _TARGET_WINDOW_STATE.get(target_name)
    if state is None or state["consumed"] or state["changed_at"] == 0.0:
        log_v(f"[安全隔離] 快取頻道{channel_label(ch)}抵達，目標好友「{target_name}」視窗近期無未消費的新訊息異動，拒絕自動綁定！")
        return None

    now_wall = time.time()
    last_t = state.get("last_fp_change", state["changed_at"])

    # 狀態過期防禦 (🟡 中度防禦)：狀態超過 AUTOBIND_TIMEOUT (15.0s) 未綁定者對真實時鐘安全過期，杜絕無限期殘留
    if now_wall - last_t > AUTOBIND_TIMEOUT:
        log_v(f"[安全隔離] 目標好友「{target_name}」異動狀態已逾時過期 ({now_wall - last_t:.1f}s > {AUTOBIND_TIMEOUT}s)，拒絕自動綁定！")
        return None

    # 防漂移核驗 (Fail-Closed)：取不到指紋 (None) 或指紋改變，一律拒絕！杜絕插入新訊息套用舊快照
    current_fp = get_chat_window_fingerprint(hwnd)
    if current_fp is None or current_fp != state.get("fp"):
        log_v(f"[安全隔離] 目標好友「{target_name}」目前視窗指紋與異動狀態不符或無法讀取 (防漂移 Fail-Closed)，拒絕自動綁定！")
        return None

    first_t = state.get("first_changed_at", state["changed_at"])
    ref_media_t = state.get("media_seen_at")

    # 2. 時間範圍相關性驗證 (中度防禦：精準以圖片出現時間 media_seen_at 比對 ±AUTOBIND_TIME_WINDOW)：
    time_diff = cand_m - first_t
    if cand_m < first_t - AUTOBIND_TIME_WINDOW or cand_m > last_t + AUTOBIND_TIME_WINDOW:
        log_v(f"[安全隔離] 快取頻道{channel_label(ch)}寫入時間 ({cand_m:.1f}) 與視窗變更時間 ({first_t:.1f}) 差距超標 ({time_diff:+.1f}s)，拒絕自動綁定！")
        return None

    if ref_media_t is not None:
        if state.get("is_thumbnail_settle"):
            # 延遲縮圖渲染：快取檔案可能於訊息抵達時 (first_t) 或渲染完成時 (ref_media_t) 寫入
            diff_media = abs(cand_m - ref_media_t)
            diff_first = abs(cand_m - first_t)
            if diff_media > AUTOBIND_TIME_WINDOW and diff_first > AUTOBIND_TIME_WINDOW:
                log_v(f"[安全隔離] 快取頻道{channel_label(ch)}寫入時間 ({cand_m:.1f}) 與延遲縮圖時間差距超標，拒絕自動綁定！")
                return None
        else:
            # 🟡 核心防禦 3：一般即時照片，快取檔案寫入時間必須嚴格落在圖片時間 ±AUTOBIND_TIME_WINDOW 內！
            if abs(cand_m - ref_media_t) > AUTOBIND_TIME_WINDOW:
                log_v(f"[安全隔離] 快取頻道{channel_label(ch)}寫入時間 ({cand_m:.1f}) 與圖片出現時間 ({ref_media_t:.1f}) 差距超標，拒絕自動綁定！")
                return None

    # 3. 媒體類型與實體位置方向核驗 (快照若因延遲載入為 False，在綁定前即時重測一次)：
    has_media = state.get("has_media")
    is_incoming = state.get("is_incoming")
    if not has_media:
        # 指紋已在上方經 Fail-Closed 核驗無誤，此處安全呼叫 UIA 重測
        has_media, is_incoming = get_chat_latest_media_info(hwnd)
        # 關閉 TOCTOU 空隙：重測後再次比對指紋，確保 UIA 讀取期間無新訊息插入
        post_fp = get_chat_window_fingerprint(hwnd)
        if post_fp is None or post_fp != state.get("fp"):
            log_v(f"[安全隔離] 目標好友「{target_name}」重測期間視窗指紋發生漂移，拒絕自動綁定！")
            return None
        if has_media:
            state["has_media"] = True
            state["is_incoming"] = is_incoming
            now_retest = time.time()
            state["media_seen_at"] = now_retest
            state["is_thumbnail_settle"] = True
            # 🟡 核心防禦：重測後嚴格核驗時間範圍！延遲縮圖快取檔案時間必須在抵達時間 (first_t) 或重測渲染時間 (now_retest) ±AUTOBIND_TIME_WINDOW (6.0s) 之內
            if abs(cand_m - now_retest) > AUTOBIND_TIME_WINDOW and abs(cand_m - first_t) > AUTOBIND_TIME_WINDOW:
                log_v(f"[安全隔離] 快取頻道{channel_label(ch)}寫入時間 ({cand_m:.1f}) 與延遲重測時間 ({now_retest:.1f}) 及抵達時間 ({first_t:.1f}) 差距超標，拒絕自動綁定！")
                return None
            # 🟢 不更新 last_fp_change，避免將有效上界往後推！

    if not has_media:
        log_v(f"[安全隔離] 快取頻道{channel_label(ch)}為圖片/貼圖，但目標好友「{target_name}」異動訊息無真實圖片控制項 (純文字訊息)，拒絕自動綁定！")
        return None

    if not is_incoming:
        log_v(f"[安全隔離] 快取頻道{channel_label(ch)}抵達，但目標好友「{target_name}」異動圖片為本機送出 (靠右對齊)，拒絕自動綁定！")
        return None

    # 4. 通過所有核驗，原子持久化儲存 (成功後才標記已消費)
    try:
        now_bind = time.time()
        p_info = {
            "target": target_name,
            "bind_fp": state.get("fp"),
            "bind_time": now_bind,
        }
        with locked_chat_dirs() as disk_map:
            disk_map[ch] = target_name
            if "_provisional" not in disk_map or not isinstance(disk_map["_provisional"], dict):
                disk_map["_provisional"] = {}
            disk_map["_provisional"][ch] = p_info
            chat_dirs_map[ch] = target_name
        state["consumed"] = True
        _PROVISIONAL_BINDINGS[ch] = p_info
        log_v(f"[自動學習綁定] 快取頻道{channel_label(ch)}經視窗多媒體指紋與接收狀態核驗無誤，已成功自動綁定至「{target_name}」！")
        return target_name
    except Exception as e:
        chat_dirs_map.pop(ch, None)
        log_v(f"[警告] 自動學習綁定頻道「{ch[:8]}...」寫入磁碟失敗: {e}，已撤銷綁定。")
        return None


def is_likely_photo(file_path):
    """
    精確判斷媒體是否為真實照片 (有效排除所有 LINE 貼圖與加密大貼圖)：
    - LINE 貼圖 (包括大貼圖/動態貼圖) 通常為透明 PNG/APNG (RGBA/P 模式) 或 WEBP 格式，尺寸 <= 512x512，且大小普遍小於 100KB
    - 加密貼圖 (.eimg) 亦不超過 100KB；真實相機照片幾乎 100% 超過 250KB (250000 bytes)
    - 本函式強制要求非加密圖片必須為標準 JPEG/RGB 格式、大於 512x512 尺寸且大小 >= 50KB；.eimg 必須 >= 250KB，杜絕任何貼圖誤觸發 photo_only 模式
    """
    try:
        sz = os.path.getsize(file_path)
        if file_path.lower().endswith('.eimg'):
            return sz >= 250000
        if sz < 50000:
            return False
        with Image.open(file_path) as img:
            # 排除帶 Alpha 透明通道的貼圖格式 (RGBA/P)，並要求尺寸超過貼圖標準上限 (512x512)
            if img.mode == 'RGB' and (img.width > 512 or img.height > 512):
                return True
    except Exception:
        pass
    return False

def is_valid_image_or_eimg(file_path):
    """嚴格核驗檔案是否為合法圖片格式 (JPEG/PNG/GIF/WEBP) 或 LINE 加密圖片 (.eimg)，排除文件與一般附件"""
    if file_path.lower().endswith('.eimg'):
        return True
    try:
        with open(file_path, 'rb') as f:
            header = f.read(12)
            if header.startswith(b'\xff\xd8\xff'):  # JPEG
                return True
            if header.startswith(b'\x89PNG\r\n\x1a\n'):  # PNG
                return True
            if header.startswith(b'GIF87a') or header.startswith(b'GIF89a'):  # GIF
                return True
            if header.startswith(b'RIFF') and header[8:12] == b'WEBP':  # WEBP
                return True
    except Exception:
        pass
    return False

def is_media_cache_dir(dir_path, cache_base=None):
    """標準化判斷目錄是否為 LINE 媒體快取路徑 (m 或 tmp)，相對於快取根目錄檢查以防誤判"""
    if cache_base is None:
        cache_base = get_active_line_cache_dir()
    if cache_base and dir_path.lower().startswith(cache_base.lower()):
        rel = os.path.relpath(dir_path, cache_base)
        parts = os.path.normpath(rel).lower().split(os.sep)
        return 'm' in parts or 'tmp' in parts
    parts = os.path.normpath(dir_path).lower().split(os.sep)
    return 'm' in parts or 'tmp' in parts

def prune_ignored_cache_files(ttl_seconds=3600):
    """清理超過 TTL (預設 1 小時) 的舊快取檔案黑名單，防止記憶體膨脹"""
    now = time.time()
    with _state_lock:
        expired = [p for p, meta in IGNORED_CACHE_FILES.items() if now - meta[0] > ttl_seconds]
        for p in expired:
            IGNORED_CACHE_FILES.pop(p, None)

def get_all_cache_files(cache_dir=None):
    """快照目前快取目錄內所有的媒體檔案 (路徑、mtime、檔案大小)，支援內容改寫偵測"""
    if cache_dir is None:
        cache_dir = get_active_line_cache_dir()
    files_dict = {}
    if cache_dir and os.path.exists(cache_dir):
        for root, _, files in os.walk(cache_dir):
            if is_media_cache_dir(root, cache_dir):
                for f in files:
                    p = os.path.join(root, f)
                    try:
                        files_dict[p] = (os.path.getmtime(p), os.path.getsize(p))
                    except Exception:
                        pass
    return files_dict

def advance_cache_mtime_to_current():
    """將監聽時間戳記更新為目前快取目錄中最晚的檔案時間，防止觸發過去的舊圖或剛發送的圖"""
    global last_seen_cache_mtime
    active_cache = get_active_line_cache_dir()
    newest = time.time()
    if active_cache:
        for root, _, files in os.walk(active_cache):
            if is_media_cache_dir(root, active_cache):
                for f in files:
                    try:
                        m = os.path.getmtime(os.path.join(root, f))
                        if m > newest:
                            newest = m
                    except Exception:
                        pass
    last_seen_cache_mtime = max(newest, time.time())

def check_circuit_breaker(target, max_replies=2, window_seconds=300, lockout_seconds=1800):
    """
    斷路器保護（含 30 分鐘退避鎖定與完整跨重啟持久化存檔）：
    若同一對象在 5 分鐘內累計回覆達 2 次，立刻鎖定該聯絡人 30 分鐘，停止自動回覆，徹底防禦循環！
    """
    now = time.time()
    with _state_lock:
        # 全域損毀防護檢查 (Fail-Closed)：狀態檔損毀時全域鎖定生效，拒絕所有發送
        global_blocked = CIRCUIT_BREAKER_BLOCKED_UNTIL.get("__GLOBAL__", 0)
        if now < global_blocked:
            log_v(f"[斷路器全域鎖定] 系統處於損毀防禦全域鎖定中 (剩餘 {int(global_blocked - now)} 秒)，拒絕發送給「{target}」！")
            return False

        blocked_until = CIRCUIT_BREAKER_BLOCKED_UNTIL.get(target, 0)
        if now < blocked_until:
            log_v(f"[斷路器鎖定中]「{target}」仍在 30 分鐘防護鎖定內 (剩餘 {int(blocked_until - now)} 秒)，本次不回覆。")
            return False
        elif blocked_until > 0 and now >= blocked_until:
            CIRCUIT_BREAKER_BLOCKED_UNTIL.pop(target, None)
            saved = save_circuit_breaker_state()
            if not saved or now < CIRCUIT_BREAKER_BLOCKED_UNTIL.get("__GLOBAL__", 0):
                log_v(f"[斷路器持久化防禦] 解除鎖定後存檔失敗，遵循 Fail-Closed 原則拒絕發送給「{target}」！")
                return False
            log_v(f"[斷路器解除]「{target}」的 30 分鐘防護鎖定已滿，自動恢復正常待命。")

        timestamps = REPLY_HISTORY_TIMESTAMPS.setdefault(target, [])
        valid_timestamps = [t for t in timestamps if now - t < window_seconds]
        REPLY_HISTORY_TIMESTAMPS[target] = valid_timestamps
        if len(valid_timestamps) >= max_replies:
            CIRCUIT_BREAKER_BLOCKED_UNTIL[target] = now + lockout_seconds
            save_circuit_breaker_state()
            log_v(f"[致命告警 / 斷路器鎖定]「{target}」在 5 分鐘內連續回覆達 {max_replies} 次上限！已啟動斷路器，暫停回覆該對象 30 分鐘以徹底杜絕無窮迴圈！")
            return False
    return True

def record_reply_sent(target):
    """記錄成功發送時間，更新斷路器歷史時間戳與 5 秒發送防抖保護並持久化"""
    now = time.time()
    with _state_lock:
        LAST_SENT_TIME_BY_TARGET[target] = now
        timestamps = REPLY_HISTORY_TIMESTAMPS.setdefault(target, [])
        timestamps.append(now)
        valid_timestamps = [t for t in timestamps if now - t < 300]
        REPLY_HISTORY_TIMESTAMPS[target] = valid_timestamps
        # 若發送成功後在 5 分鐘內累計已達 2 次，立刻預先設定斷路器鎖定 30 分鐘
        if len(valid_timestamps) >= 2:
            CIRCUIT_BREAKER_BLOCKED_UNTIL[target] = now + 1800
            log_v(f"[斷路器就地鎖定]「{target}」在 5 分鐘內已達 2 次上限，立即啟動 30 分鐘冷卻保護！")
        save_circuit_breaker_state()

WIDE_JUDGE_MIN_WIDTH = 800      # 聊天視窗邏輯寬度低於此值時，左右判斷 (自己發或對方發) 不可靠，判斷前先暫時最大化
WIDE_JUDGE_SETTLE = 1.0         # 最大化後等待 LINE 重新排版與繪製的時間 (秒)

@contextlib.contextmanager
def _wide_window_for_judgment(chat_hwnd):
    """
    窄視窗下 LINE 的氣泡位置會讓左右判斷出錯 (實測：最後一則在左側，視窗窄時被判成自己發的)。
    視窗不夠寬時暫時最大化，離開時還原成原本的大小與位置；已最大化、最小化、不可見或任何例外都不動視窗。
    """
    restore = False
    try:
        if (win32gui.IsWindow(chat_hwnd) and not win32gui.IsIconic(chat_hwnd) and win32gui.IsWindowVisible(chat_hwnd)
                and win32gui.GetWindowPlacement(chat_hwnd)[1] != win32con.SW_SHOWMAXIMIZED):
            scale = 1.0
            try:
                dpi = ctypes.windll.user32.GetDpiForWindow(chat_hwnd)
                if dpi > 0:
                    scale = dpi / 96.0
            except Exception:
                pass
            rect = get_window_true_rect(chat_hwnd)
            if (rect[2] - rect[0]) / scale < WIDE_JUDGE_MIN_WIDTH:
                win32gui.ShowWindow(chat_hwnd, win32con.SW_MAXIMIZE)
                restore = True
                time.sleep(WIDE_JUDGE_SETTLE)
    except Exception:
        restore = False
    try:
        yield
    finally:
        if restore:
            try:
                win32gui.ShowWindow(chat_hwnd, win32con.SW_RESTORE)
            except Exception:
                pass

def is_last_message_from_self_in_chat(chat_hwnd):
    """最底部訊息是否為自己發的 (Fail-Closed)；視窗太窄時先暫時最大化再判斷，細節見 _is_last_message_from_self_in_chat"""
    with _wide_window_for_judgment(chat_hwnd):
        return _is_last_message_from_self_in_chat(chat_hwnd)

def _is_last_message_from_self_in_chat(chat_hwnd):
    """
    實體位置方向判定 (Fail-Closed)：檢查目標聊天視窗最底部訊息是否位於右側 (本機送出)。
    - 若視窗最小化、不可見、或非前景視窗，一律回傳 True (安全攔截，不回覆)。
    - 支援 Windows DWM 精確邊界與動態 DPI 縮放。
    - 雙欄模式以固定側欄寬度 (~320px * scale) 嚴格隔離左側聯絡人清單。
    - 採用左側錨定原理 (LINE 接收訊息必靠左對齊，發送訊息左側必完全留白)，徹底免疫大圖跨欄誤判。
    """
    try:
        if not win32gui.IsWindow(chat_hwnd):
            return True
        if win32gui.IsIconic(chat_hwnd) or not win32gui.IsWindowVisible(chat_hwnd):
            return True

        # 嚴格驗證前景視窗 (Fail-Closed)：非前景一律拒絕發送
        fg_hwnd = win32gui.GetForegroundWindow()
        if fg_hwnd != chat_hwnd:
            return True

        rect = get_window_true_rect(chat_hwnd)
        w = rect[2] - rect[0]
        h = rect[3] - rect[1]
        if w < 200 or h < 200:
            return True

        # 計算視窗目前 DPI 縮放係數
        scale = 1.0
        try:
            dpi = ctypes.windll.user32.GetDpiForWindow(chat_hwnd)
            if dpi > 0:
                scale = dpi / 96.0
        except Exception:
            pass

        input_box_h = int(145 * scale)

        chat_crop_x = rect[0]
        chat_crop_y = rect[1] + int(h * 0.18)
        chat_crop_w = w
        chat_crop_h = max(50, h - int(h * 0.18) - input_box_h)

        shot = ImageGrab.grab(
            bbox=(chat_crop_x, chat_crop_y, chat_crop_x + chat_crop_w, chat_crop_y + chat_crop_h),
            all_screens=True
        )

        # 計算聊天訊息工作區的真實寬度與起點 (僅在 LINE 主視窗雙欄模式下避開側欄；獨立聊天視窗無側欄)
        line_main_hwnd = find_line_main_window()
        is_main_win = (chat_hwnd == line_main_hwnd)
        if is_main_win and (w / scale) > 650:
            sidebar_w = int(320 * scale)
            chat_x_offset = sidebar_w
            chat_w = max(100, chat_crop_w - sidebar_w)
        else:
            chat_x_offset = 0
            chat_w = chat_crop_w

        # 動態取樣背景顏色 (取樣點完全位於聊天區域內部，避開左側側欄干擾)
        sample_pts = [
            (chat_x_offset + int(chat_w * 0.5), 10),
            (chat_x_offset + 25, 10),
            (chat_x_offset + chat_w - 25, 10),
            (chat_x_offset + int(chat_w * 0.5), min(30, chat_crop_h - 5)),
        ]
        bg_colors = [shot.getpixel(pt) for pt in sample_pts]
        bg_r = sorted(c[0] for c in bg_colors)[len(bg_colors) // 2]
        bg_g = sorted(c[1] for c in bg_colors)[len(bg_colors) // 2]
        bg_b = sorted(c[2] for c in bg_colors)[len(bg_colors) // 2]

        def is_content_pixel(px):
            return (abs(px[0] - bg_r) + abs(px[1] - bg_g) + abs(px[2] - bg_b)) > 30

        # 定義極窄邊緣錨點：左側接收區 (3%-12%) 與極右側發送區 (88%-97%)，徹底免疫中大圖或長文泡泡跨欄
        left_x_start = chat_x_offset + int(chat_w * 0.03)
        left_x_end = chat_x_offset + int(chat_w * 0.12)
        right_x_start = chat_x_offset + int(chat_w * 0.88)
        right_x_end = chat_x_offset + int(chat_w * 0.97)

        latest_right_y = -1
        latest_left_y = -1

        # 由下往上掃描最後一則訊息 (步距 2px 垂直，4px 水平)
        for y in range(chat_crop_h - 1, 0, -2):
            for x in range(right_x_start, right_x_end, 4):
                if is_content_pixel(shot.getpixel((x, y))):
                    if latest_right_y == -1:
                        latest_right_y = y
                    break
            for x in range(left_x_start, left_x_end, 4):
                if is_content_pixel(shot.getpixel((x, y))):
                    if latest_left_y == -1:
                        latest_left_y = y
                    break

            if latest_left_y != -1 or latest_right_y != -1:
                cur_max = max(latest_left_y, latest_right_y)
                if cur_max - y > 30:
                    break

        # Fail-Closed：若兩邊都找不到任何內容，安全起見視為自己發送
        if latest_right_y == -1 and latest_left_y == -1:
            return True

        # 若左右兩側底部垂直高度接近 (abs(diff) <= 25，例如本機訊息泡泡與左側時間標籤，或桌布紋理)，遵循 Fail-Closed 視為自己發送
        if latest_left_y != -1 and latest_right_y != -1 and abs(latest_left_y - latest_right_y) <= 25:
            log_v(f"[實體位置方向安全攔截] 左右兩側底部高度接近 (L:{latest_left_y}, R:{latest_right_y})，判定為本機送出訊息或裝飾線，安全攔截！")
            return True

        # 若右側留白 (latest_right_y == -1) 或左側內容比右側更靠近底部 25px 以上，判定為對方送出
        if latest_left_y != -1 and (latest_right_y == -1 or latest_left_y > latest_right_y + 25):
            return False

        # 最底部內容出現在右側，左側留白，判定為自己發送
        return True
    except Exception as e:
        log_v(f"[警告] 實體位置方向核驗發生例外: {e}，遵循 Fail-Closed 原則視為自己發送以確保安全！")
        return True

# 左側訊息類型判斷參數 (邏輯像素，相對 96 DPI)
MEDIA_NOISE_MAX_HEIGHT = 12     # 非氣泡區塊低於此高度視為細線、雜訊或連結列
MEDIA_STICKER_MIN_HEIGHT = 40   # 連結列上方的區塊至少要有此高度才算照片
MEDIA_ACTION_GAP_MAX = 16       # 連結列與上方照片之間的最大間距

def _left_message_blocks(chat_hwnd, side="left", max_blocks=2):
    """
    截取聊天室歷史區並找出左側 (對方) 或右側 (自己，side="right") 訊息的內容區塊。回傳
    (shot, scale, x_start, x_end, is_content, blocks)，blocks 為由下往上最多 max_blocks (預設 2) 個 (底, 頂) 區塊；
    暫時無法判斷 (視窗縮小/被遮住/太窄、該側無內容) 回傳 None。
    左側取樣帶固定自 56px 起寬 160px (避開頭像)；右側取樣帶為距右緣 24px 往左 160px (氣泡靠右對齊，沒有頭像)。
    連續約 6px 無內容即分開區塊；背景色取 10 個取樣點中位數。
    """
    if not win32gui.IsWindow(chat_hwnd) or win32gui.IsIconic(chat_hwnd) or not win32gui.IsWindowVisible(chat_hwnd):
        return None
    rect = get_window_true_rect(chat_hwnd)
    w = rect[2] - rect[0]
    h = rect[3] - rect[1]
    if w < 300 or h < 200:
        return None
    scale = 1.0
    try:
        dpi = ctypes.windll.user32.GetDpiForWindow(chat_hwnd)
        if dpi > 0:
            scale = dpi / 96.0
    except Exception:
        pass
    crop_y = rect[1] + int(h * 0.18)
    crop_h = max(50, h - int(h * 0.18) - int(145 * scale))
    shot = ImageGrab.grab(bbox=(rect[0], crop_y, rect[0] + w, crop_y + crop_h), all_screens=True)

    pts = [shot.getpixel((int(w * fx), y)) for fx in (0.3, 0.4, 0.5, 0.6, 0.7) for y in (10, 30) if y < crop_h]
    bg = tuple(sorted(c[i] for c in pts)[len(pts) // 2] for i in range(3))
    # 截圖頂端剛好落在某個長氣泡中間時 (窄視窗常見)，上面的取樣點會把氣泡的灰色當成背景，整片白色背景反而變成「內容」。
    # 氣泡不會貼到視窗右緣 (自己的訊息右緣約留 15px)，右緣內側的窄帶 (避開最外側的捲動軸) 一定是聊天背景，取得出一致的顏色就優先用它
    edge_pts = [shot.getpixel((x, y)) for x in (w - int(k * scale) for k in (9, 11, 13)) if 0 <= x < shot.width
                for y in range(5, crop_h, 25)]
    if len(edge_pts) >= 6:
        edge_bg = tuple(sorted(c[i] for c in edge_pts)[len(edge_pts) // 2] for i in range(3))
        agree = sum(1 for c in edge_pts if sum(abs(c[i] - edge_bg[i]) for i in range(3)) <= 12)
        if agree / len(edge_pts) >= 0.75:
            bg = edge_bg

    def is_content(px):
        return (abs(px[0] - bg[0]) + abs(px[1] - bg[1]) + abs(px[2] - bg[2])) > 30

    if side == "right":
        x_end = w - int(24 * scale)
        x_start = max(int(w * 0.5), x_end - int(160 * scale))
    else:
        x_start = int(56 * scale)
        x_end = min(x_start + int(160 * scale), int(w * 0.5))

    def row_has_content(y):
        return any(is_content(shot.getpixel((x, y))) for x in range(x_start, x_end, 4))

    # 由下往上分區塊 [(底, 頂)]，只需最底部兩個
    blocks = []
    cur_bottom = cur_top = None
    gap = 0
    for y in range(crop_h - 1, 0, -2):
        if row_has_content(y):
            if cur_bottom is None:
                cur_bottom = y
            cur_top, gap = y, 0
        elif cur_bottom is not None:
            gap += 2
            if gap > 4:
                blocks.append((cur_bottom, cur_top))
                cur_bottom = cur_top = None
                gap = 0
                if len(blocks) >= max_blocks:
                    break
    if cur_bottom is not None and len(blocks) < max_blocks:
        blocks.append((cur_bottom, cur_top))
    if not blocks:
        return None
    return shot, scale, x_start, x_end, is_content, blocks

MEDIA_CHECK_ATTEMPTS = 3        # 剛把視窗還原並帶到前景時，LINE 可能還沒重繪完，判斷不出來就稍等再試
MEDIA_CHECK_RETRY_WAIT = 0.8    # 每次重試前等待的秒數

def _media_type_with_retry(chat_hwnd, expect_media=False):
    """
    last_left_message_is_media 加上重試：回傳 True (照片或貼圖)、False (文字)、None (試完仍無法判斷)。
    expect_media=True 表示這次是被「圖片快取」觸發的，最後一則一定是圖：圖還在載入時 LINE 顯示的灰色方塊
    會被看成文字氣泡，所以判成文字也要重試，最多等 IMAGE_LOAD_WAIT_SECONDS 秒。
    """
    result = None
    _wait_until_chat_settled(chat_hwnd)    # 前一步檢查自己訊息會最大化再還原視窗，畫面還在重排時判斷會讀錯
    attempts = MEDIA_CHECK_ATTEMPTS
    wait = MEDIA_CHECK_RETRY_WAIT
    if expect_media:
        wait = IMAGE_LOAD_POLL_SECONDS
        attempts = max(attempts, int(IMAGE_LOAD_WAIT_SECONDS / wait) + 1)
    for attempt in range(attempts):
        result = last_left_message_is_media(chat_hwnd)
        if result is True or (result is False and not expect_media):
            return result
        if attempt < attempts - 1:
            time.sleep(wait)
    return result

def last_left_message_is_media(chat_hwnd):
    """
    判斷聊天室最底部「左側 (對方)」訊息是否為照片/貼圖。回傳 True (照片/貼圖)、False (文字)、
    None (暫時無法判斷：視窗縮小/被遮住/太窄、截圖失敗、左側無內容)。
    只看最底部區塊 B (區塊切分見 _left_message_blocks)。只有明確的文字氣泡才算文字，其餘一律視為照片或貼圖
    (含透明背景被分段的貼圖片段與照片下方的「儲存｜另存新檔｜分享」連結列)，避免貼圖被誤略過：
    - B 為文字氣泡：LINE 接收氣泡為單一淺灰填色，占區塊內容 55% 以上，文字 (不論高度，長文字也算)。
    - B 非氣泡且高度 (邏輯像素，已除以 DPI 縮放) 達 MEDIA_NOISE_MAX_HEIGHT：照片或貼圖。
    - B 非氣泡且低於該高度：若緊貼 (間距 <= MEDIA_ACTION_GAP_MAX) 在上方非氣泡且達 MEDIA_STICKER_MIN_HEIGHT
      的區塊下方，是照片的連結列，視為照片；否則是細線或雜訊，文字。
    已知取捨：網址預覽卡片 (白底) 不是氣泡，對方只傳連結時會被當成照片而回覆。
    """
    try:
        found = _left_message_blocks(chat_hwnd)
        if found is None:
            return None
        shot, scale, x_start, x_end, is_content, blocks = found

        def height(blk):
            return int((blk[0] - blk[1] + 1) / scale)

        def is_text_bubble(blk):
            counts = {}
            total = 0
            for y in range(blk[1], blk[0] + 1, 2):
                for x in range(x_start, x_end, 4):
                    px = shot.getpixel((x, y))
                    if is_content(px):
                        total += 1
                        counts[px] = counts.get(px, 0) + 1
            if total == 0:
                return False
            top_px, top_n = max(counts.items(), key=lambda kv: kv[1])
            neutral = max(top_px) - min(top_px) <= 6 and 215 <= min(top_px) <= 250
            return neutral and top_n / total >= 0.55

        last = blocks[0]
        if is_text_bubble(last):
            return False
        if height(last) >= MEDIA_NOISE_MAX_HEIGHT:
            return True
        # 細小區塊：若緊貼在非氣泡的大區塊 (照片) 下方，是「儲存｜另存新檔｜分享」連結列，屬於照片；否則為雜訊
        if len(blocks) >= 2:
            above = blocks[1]
            gap_px = int((last[1] - above[0]) / scale)
            return (gap_px <= MEDIA_ACTION_GAP_MAX and height(above) >= MEDIA_STICKER_MIN_HEIGHT
                    and not is_text_bubble(above))
        return False
    except Exception as e:
        log_v(f"[警告] 訊息類型量測發生例外: {e}，視為無法判斷 (Fail-Closed)。")
        return None


# 停止指令 (你在 stop_command_chats 內的聊天室自己發了含 stop_command 的文字訊息時，程式立即結束)
STOP_EXIT_CODE = 3              # 結束碼：run_auto_reply.bat 看到此值會直接關閉視窗
STOP_FUZZY_RATIO = 0.85         # OCR 偶爾認錯一字，整句相似度達此值也視為指令
STOP_OCR_UPSCALES = (2, 4)      # OCR 截圖放大倍數 (小字放大後辨識較準，各倍數結果不同，都試)
STOP_SIG_MIN_INTERVAL = 5.0       # 視窗簽章觸發的檢查最短間隔 (秒)，避免使用者捲動歷史訊息時每秒都做 OCR
STOP_READ_ATTEMPTS = 3          # 視窗在前景時，讀不到你發的訊息最多再試幾次
STOP_RECHECKS = 5               # 簽章變了但沒讀到停止指令時，保留舊簽章、之後最多再讀幾次 (新氣泡可能還沒畫好；每次至少隔 STOP_SIG_MIN_INTERVAL 秒，約 25 秒)
STOP_RECHECK_WINDOW = 60.0      # 重讀計數的有效秒數 (從第一次沒讀到算起)；超過就當成新的一次簽章變化，重新完整讀
_STOP_RECHECKS = {}             # {聊天室: (簽章, 這個簽章已經讀過幾次但沒讀到指令, 第一次沒讀到的時間)}
_STOP_SIG_LAST_CHECK = {}        # {聊天室: 上次因視窗簽章變化而檢查的時間}
_STOP_SEEN_SIG = {}             # {聊天室: 視窗展開時最後一次記下的訊息列表簽章}

class StopRequested(BaseException):
    """收到遠端停止指令。繼承 BaseException，避免被監聽迴圈的 except Exception 吞掉"""


class AutoStopReached(StopRequested):
    """已回覆到啟動時指定的則數：走跟遠端停止一樣的收尾流程，結束後讓電腦睡眠"""

_AUTO_STOP = {"limit": 0, "replies": 0}      # limit 0 = 不啟用；replies = 這次執行已實際送出的回覆數 (圖片與文字都算)
DEFAULT_STOP_AFTER = 1
SLEEP_GRACE_SECONDS = 3.0                    # 睡眠前多等一下，讓最後的通知與備援訊息送完

def resolve_stop_after(arg_value, interactive, ask=input):
    """
    啟動時要在回覆幾則後自動關閉並讓電腦睡眠。命令列給了數字就用它 (0 = 不啟用)；沒給且有主控台就詢問 (直接 Enter = 1)；
    沒有主控台 (不顯示視窗的啟動) 沒給就不啟用。輸入不是非負整數時重問。
    """
    if arg_value is not None:
        return max(0, arg_value)
    if not interactive:
        return 0
    while True:
        try:
            text = ask(f"回覆幾則圖片或訊息後自動關閉機器人並讓電腦睡眠？(Enter = {DEFAULT_STOP_AFTER}，0 = 不自動關閉): ").strip()
        except EOFError:
            return 0
        except KeyboardInterrupt:
            print("\n已取消，程式未啟動。")
            sys.exit(0)
        if not text:
            return DEFAULT_STOP_AFTER
        try:
            if int(text) >= 0:
                return int(text)
        except ValueError:                           # 含 "²" 這類 isdigit() 為真但 int() 轉不了的字元
            pass
        print("請輸入 0 或正整數。")

def note_reply_for_auto_stop():
    with _state_lock:
        _AUTO_STOP["replies"] += 1

def check_auto_stop():
    """主迴圈每輪呼叫：回覆數到了指定則數就丟出 AutoStopReached (此時上一則回覆已完整收尾，視窗也處理完了)"""
    limit = _AUTO_STOP["limit"]
    if limit and _AUTO_STOP["replies"] >= limit:
        raise AutoStopReached(f"已回覆 {_AUTO_STOP['replies']} 則，依啟動時的設定自動關閉並讓電腦睡眠")

def suspend_computer():
    """讓電腦進入睡眠 (不是休眠)。成功時這個呼叫會在喚醒後才回來；失敗只記一行，不影響結束"""
    try:
        import ctypes
        if not ctypes.windll.powrprof.SetSuspendState(False, False, False):
            log_v("[自動關閉] 讓電腦睡眠失敗 (系統回報不成功)。")
    except Exception as e:
        log_v(f"[自動關閉] 讓電腦睡眠失敗: {e}")

def text_matches_stop_command(text, phrase):
    """去除空白與標點後，訊息含指令全文，或整句與指令高度相似 (容許 OCR 認錯一字) 即成立"""
    t = re.sub(r"[\W_]+", "", text or "")
    p = re.sub(r"[\W_]+", "", phrase or "")
    if not t or not p:
        return False
    if p in t:
        return True
    return len(t) <= len(p) + 2 and difflib.SequenceMatcher(None, t, p).ratio() >= STOP_FUZZY_RATIO

def _stop_ocr_variants(crop):
    """
    產生同一張訊息截圖的幾種前處理版本。Windows OCR 對淺灰氣泡裡的小字很挑剔 (是否有白邊、放大倍數、
    是否二值化都會讓某個字漏掉)，實測同一則六字訊息不同版本會漏掉不同的字，
    所以多試幾種，任何一種讀到指令就算數。
    """
    gray = ImageOps.autocontrast(crop.convert("L"))
    for up in STOP_OCR_UPSCALES:
        padded = ImageOps.expand(gray, border=24, fill=255)
        yield padded.resize((padded.width * up, padded.height * up), Image.LANCZOS)
    binary = ImageOps.expand(gray.point(lambda v: 0 if v < 160 else 255), border=24, fill=255)
    yield binary.resize((binary.width * 3, binary.height * 3), Image.LANCZOS)

OWN_BUBBLE_MIN_RATIO = 0.55     # 自己的文字氣泡是單一填色：最多的顏色占區塊內容比例達此值才算文字 (照片、貼圖不做 OCR)
OWN_BUBBLE_COLOR_TOLERANCE = 8  # 判斷像素屬於氣泡底色的每通道容差

def _own_bubble_box(shot, scale, x_start, x_end, top, bottom, is_content):
    """
    由右側區塊 (底, 頂) 找出自己的文字氣泡範圍，回傳 (左, 上, 右, 下) 截圖座標；區塊不是純色氣泡 (照片、貼圖、
    連結預覽) 回傳 None。氣泡底色取區塊內最多的內容色，左右邊界取該底色出現的最左與最右像素，
    讓 OCR 只讀氣泡本身，不會讀到氣泡左側的時間與已讀標籤。
    """
    counts = {}
    total = 0
    for y in range(top, bottom + 1, 2):
        for x in range(x_start, x_end, 4):
            px = shot.getpixel((x, y))
            if is_content(px):
                total += 1
                counts[px] = counts.get(px, 0) + 1
    if total == 0:
        return None
    base, base_n = max(counts.items(), key=lambda kv: kv[1])
    if base_n / total < OWN_BUBBLE_MIN_RATIO:
        return None

    def near_base(px):
        return all(abs(px[i] - base[i]) <= OWN_BUBBLE_COLOR_TOLERANCE for i in range(3))

    left = right = None
    for y in range(top, bottom + 1, 2):
        for x in range(0, shot.width, 2):
            if near_base(shot.getpixel((x, y))):
                if left is None or x < left:
                    left = x
                if right is None or x > right:
                    right = x
    if left is None:
        return None
    pad = int(4 * scale)
    return (max(0, left - pad), max(0, top - pad), min(shot.width, right + pad + 1), min(shot.height, bottom + pad + 1))

def last_own_message_has_text(chat_hwnd, phrase):
    """
    用 OCR 讀自己最底部的文字氣泡 (右側)，任何一種前處理讀到 phrase 就回傳 True。
    讀得到字但不含 phrase、或最底部不是文字氣泡 (照片、貼圖) 回傳 False；
    截不到圖，或每種前處理都讀不出字 (無法排除指令在那裡) 回傳 None。
    """
    found = _left_message_blocks(chat_hwnd, side="right")
    if found is None:
        return None
    shot, scale, x_start, x_end, is_content, blocks = found
    bottom, top = blocks[0]
    box = _own_bubble_box(shot, scale, x_start, x_end, top, bottom, is_content)
    if box is None:
        return False
    crop = shot.crop(box)
    read = False
    for variant in _stop_ocr_variants(crop):
        fd, tmp_path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            variant.save(tmp_path)
            text = ocr_image_text(tmp_path)
        except Exception:
            continue
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        if text:
            read = True
            if text_matches_stop_command(text, phrase):
                return True
    return False if read else None

def _chat_has_stop_command(chat, phrase, attempts=None):
    """
    確認該聊天室你發的最底部文字含停止指令，而且它是整個聊天室的最後一則。回傳三種結果：
    True：是指令，要停止。
    False：確定不用再讀了 (沒有這個聊天室視窗，或讀到指令但它不是最後一則：下面已有官方帳號推來的新訊息)。
    None：這次沒讀到指令，但可能只是新氣泡還沒畫好、剛好失焦、讀不出字，稍後值得再讀 (見 check_stop_command)；依 Fail-Closed 本次不停止。
    attempts 是「讀不出字」時最多讀幾次 (預設 STOP_READ_ATTEMPTS)；稍後的重讀只讀一次，免得每次都睡上好幾秒卡住主迴圈。
    你在聊天室打字時視窗一定在前景，所以只在視窗已是前景時檢查，不會為了檢查搶焦點或縮小視窗；
    只有讀到指令時才可能暫時最大化視窗 (確認它是最後一則)，平常打字聊天不會動你的視窗。
    """
    attempts = STOP_READ_ATTEMPTS if attempts is None else max(1, attempts)
    matched = find_target_chat_window(chat)
    if not matched:
        return False
    hwnd = matched[0][0]
    for _ in range(attempts):
        if win32gui.GetForegroundWindow() != hwnd:
            return None                       # 失焦 (可能剛好切走)：不讀，稍後再說
        if win32gui.GetWindowText(hwnd).strip().lower() != chat.lower():
            return False                      # 標題對不上 (例如帶了未讀數)：這是持續的狀況，重讀也不會變，不浪費次數
        found = last_own_message_has_text(hwnd, phrase)
        if found is None:
            if attempts > 1:
                time.sleep(0.8)
            continue
        if not found:
            return None                       # 沒有指令：視窗完全不動 (新氣泡可能還沒畫好，由 check_stop_command 保留基準線、稍後再讀)
        # 看到指令才確認它是最底部一則 (而不是上面留著的舊指令，下面已有官方帳號推來的新訊息)；視窗太窄時這一步會暫時最大化
        return verify_message_sent_to_chat(hwnd)
    if attempts > 1:
        log_v(f"[停止指令待確認]「{chat}」最底部你發的訊息無法讀取，本次不停止。")
    return None

def _window_hwnd(windows, chat):
    """好友視窗的控制代碼 (標題可帶未讀數)；找不到回傳 None"""
    for hwnd, title, _, _ in windows:
        m = re.search(r"^(.*?)\s*[\(\uFF08](\d+\+?)[\)\uFF09]\s*$", title)
        name = m.group(1).strip() if m else title.strip()
        if name.lower() == chat.lower():
            return hwnd
    return None

def _expanded_window_signature(hwnd):
    """
    視窗展開 (未最小化) 時的訊息列表簽章：項目數加最末兩個項目的位置。LINE 在視窗展開時不在標題顯示未讀數，
    新訊息只能從列表變化看出來。視窗最小化、讀取失敗回傳 None。
    """
    try:
        if win32gui.IsIconic(hwnd) or not win32gui.IsWindowVisible(hwnd):
            return None
        import uiautomation as auto
        view = auto.ControlFromHandle(hwnd).ListControl(ClassName="LcListView")
        if not view.Exists(0, 0):
            return None
        items = view.GetChildren()
        if not items:
            return None

        def rect(item):
            r = item.BoundingRectangle
            return (r.left, r.top, r.right, r.bottom)

        def ident(item):
            # 每個列表項目有獨一無二的 runtime ID：列表已滿時新訊息把舊的擠出去，項目數與最底部位置可能完全不變，只有 ID 會換
            try:
                return tuple(item.GetRuntimeId())
            except Exception:
                return None

        return (len(items), rect(items[-1]), rect(items[-2]) if len(items) > 1 else None,
                ident(items[-1]), ident(items[-2]) if len(items) > 1 else None)
    except Exception:
        return None

TEXT_WATCH_MIN_INTERVAL = 5.0     # 同一個聊天室兩次「列表變了」觸發之間最短間隔 (秒)
_TEXT_SEEN_SIG = {}               # {聊天室: 上次記下的訊息列表簽章}
_TEXT_LAST_TRIGGER = {}           # {聊天室: 上次觸發的時間}


def _user_is_viewing(hwnd):
    """
    你正在看這個視窗：它是前景視窗，而且視窗中心點最上面真的是它。
    只看前景不夠：回覆完我們把視窗放到最底層，但它仍是「前景視窗」(只是被別的視窗蓋住)，
    這時你看不到它，之後進來的新文字卻會被當成你正在看而漏掉 (真機實測 2026-11-01 19:39)。
    """
    try:
        if win32gui.GetForegroundWindow() != hwnd:
            return False
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        return _root_window_at((left + right) // 2, (top + bottom) // 2) == hwnd
    except Exception:
        return True                       # 讀不到：保守當成你正在看，不去打擾


def text_window_changed(windows, target, config):
    """
    一對一視窗展開、放在背景時，訊息列表有變化 (對方傳來新文字，或我們自己剛送出東西) 回傳 True。
    真機實測 (2026-11-01)：這個版本的 LINE 獨立視窗不論縮小或展開，標題都不顯示未讀數，所以文字訊息只能靠列表變化偵測 (同停止指令)。
    視窗縮小或沒開：偵測不到 (清掉舊簽章)。視窗在前景 (你正在看)：只更新簽章不觸發。
    停止指令的聊天室不看 (官方帳號的推播會讓它一直變)。觸發時不更新簽章，由呼叫端處理完再 text_window_rebaseline，
    處理失敗 (本次沒送出) 就保留舊簽章，下一輪還會再試。
    """
    if target in (config.get("stop_command_chats") or []):
        return False
    hwnd = _window_hwnd(windows, target)
    sig = _expanded_window_signature(hwnd) if hwnd else None
    if sig is None:
        _TEXT_SEEN_SIG.pop(target, None)
        _BACKGROUND_WINDOWS.discard(hwnd)
        return False
    old = _TEXT_SEEN_SIG.get(target)
    if old is None or sig == old:
        _TEXT_SEEN_SIG[target] = sig
        return False
    if _user_is_viewing(hwnd):
        _TEXT_SEEN_SIG[target] = sig
        return False
    if time.time() - _TEXT_LAST_TRIGGER.get(target, 0.0) < TEXT_WATCH_MIN_INTERVAL:
        return False
    _TEXT_LAST_TRIGGER[target] = time.time()
    _BACKGROUND_WINDOWS.add(hwnd)
    return True


def text_window_rebaseline(target):
    """處理完 (含略過) 後記下現在的列表簽章，之後只有再變化才觸發"""
    try:
        matched = _window_hwnd(get_all_line_windows(), target)
        sig = _expanded_window_signature(matched) if matched else None
        if sig is not None:
            _TEXT_SEEN_SIG[target] = sig
        else:
            _TEXT_SEEN_SIG.pop(target, None)
    except Exception:
        pass


def init_stop_baselines(config, chats=None):
    """啟動 (或熱重載新增聊天室時) 記下停止指令聊天室目前的視窗簽章，避免先前留在聊天室的舊指令在啟動後立刻停止程式"""
    windows = get_all_line_windows()
    for chat in (config.get("stop_command_chats") or []) if chats is None else chats:
        hwnd = _window_hwnd(windows, chat)
        sig = _expanded_window_signature(hwnd) if hwnd else None
        _STOP_RECHECKS.pop(chat, None)              # 重新建立基準：之前進行到一半的重讀作廢
        if sig is not None:
            _STOP_SEEN_SIG[chat] = sig

def check_stop_command(config):
    """
    檢查 stop_command_chats 內的聊天室，你是否在裡面發了停止指令，是的話擲出 StopRequested。
    你自己發的訊息不會產生未讀數，所以靠訊息列表簽章 (項目數加最末兩個項目的位置) 偵測變化：
    列表一有變化，且視窗在前景 (你正在打字)、最底部是自己發的文字氣泡，才用 OCR 讀內容。
    視窗縮小或沒開時你不可能在裡面打字，直接略過；視窗在背景時保留舊簽章，回到前景再查。
    """
    chats = config.get("stop_command_chats") or []
    phrase = config.get("stop_command") or ""
    if not chats or not phrase:
        return
    windows = get_all_line_windows()
    for chat in chats:
        sig = None
        try:
            hwnd = _window_hwnd(windows, chat)
            sig = _expanded_window_signature(hwnd) if hwnd else None
            if sig is None:
                _STOP_SEEN_SIG.pop(chat, None)      # 視窗關閉或縮小：展開後重新建立簽章
                _STOP_RECHECKS.pop(chat, None)
                continue
            old_sig = _STOP_SEEN_SIG.get(chat)
            if old_sig is None or sig == old_sig:
                _STOP_SEEN_SIG[chat] = sig
                _STOP_RECHECKS.pop(chat, None)
                continue
            if time.time() - _STOP_SIG_LAST_CHECK.get(chat, 0.0) < STOP_SIG_MIN_INTERVAL:
                continue                            # 太密集 (例如捲動歷史訊息)：保留舊簽章，間隔過後再查
            if win32gui.GetForegroundWindow() != hwnd:
                continue                            # 背景視窗 (例如官方帳號推來 log)：不搶焦點，保留舊簽章
            _STOP_SIG_LAST_CHECK[chat] = time.time()
            now = time.time()
            state = _STOP_RECHECKS.get(chat)
            # 重讀計數屬於「這一次簽章變化」：更新的簽章要重新算，不繼承舊的；而且只在 STOP_RECHECK_WINDOW 秒內有效
            # (「氣泡還沒畫好」只可能發生在簽章剛變的那一小段時間，你切走很久才回來要重新完整讀一次)
            fresh = bool(state) and state[0] == sig and now - state[2] <= STOP_RECHECK_WINDOW
            tries = state[1] if fresh else 0
            first_miss = state[2] if fresh else now
            try:
                result = _chat_has_stop_command(chat, phrase, attempts=STOP_READ_ATTEMPTS if tries == 0 else 1)
            except Exception as e:
                # 讀取當下的暫時性錯誤 (視窗正在重繪、截圖失敗...) 不能當成「沒有指令」而推進基準線：算一次「稍後再讀」
                if tries == 0:
                    log_v(f"[警告] 讀取「{chat}」的停止指令時發生例外: {e}，稍後再試。")
                result = None
            if result:
                raise StopRequested(f"你在「{chat}」聊天室發了停止指令")
            if result is None:
                # 沒讀到指令：列表一有新項目簽章就變，但新氣泡的內容可能還沒畫好 (真機實測 2026-11-01：程式送出的停止指令
                # 過了約 2 分鐘才被偵測到，因為讀不到那次基準線就已經前進，除非列表再變一次否則不會再讀)。
                # 所以先不推進基準線，之後幾輪 (每次至少隔 STOP_SIG_MIN_INTERVAL 秒) 再讀 (只讀一次，不睡)，最多 STOP_RECHECKS 次才放棄
                tries += 1
                if tries <= STOP_RECHECKS:
                    _STOP_RECHECKS[chat] = (sig, tries, first_miss)
                    continue
            # 確定不用再讀 (False)，或重讀用完：推進基準線，之後同樣的簽章不再讀
            _STOP_RECHECKS.pop(chat, None)
            _STOP_SEEN_SIG[chat] = sig
        except StopRequested:
            raise
        except Exception as e:
            if sig is not None:
                _STOP_SEEN_SIG[chat] = sig          # 出錯就推進基準線，免得每個間隔都重複出錯與重複警告
            _STOP_RECHECKS.pop(chat, None)
            log_v(f"[警告] 檢查「{chat}」的停止指令時發生例外: {e}")


def get_chat_history_image(chat_hwnd):
    """擷取聊天歷史區域影像 (完全避開底部 145px 輸入框以排除閃爍游標干擾)"""
    try:
        if not win32gui.IsWindow(chat_hwnd) or win32gui.IsIconic(chat_hwnd) or not win32gui.IsWindowVisible(chat_hwnd):
            return None
        rect = get_window_true_rect(chat_hwnd)
        w = rect[2] - rect[0]
        h = rect[3] - rect[1]
        if w < 200 or h < 200:
            return None
        scale = 1.0
        try:
            dpi = ctypes.windll.user32.GetDpiForWindow(chat_hwnd)
            if dpi > 0:
                scale = dpi / 96.0
        except Exception:
            pass
        input_box_h = int(145 * scale)
        chat_crop_x = rect[0]
        chat_crop_y = rect[1] + int(h * 0.18)
        chat_crop_w = w
        chat_crop_h = max(50, h - int(h * 0.18) - input_box_h)
        return ImageGrab.grab(
            bbox=(chat_crop_x, chat_crop_y, chat_crop_x + chat_crop_w, chat_crop_y + chat_crop_h),
            all_screens=True
        )
    except Exception:
        return None

def get_chat_visual_hash(chat_hwnd):
    """
    取得聊天室歷史內容之動態視覺雜湊值 (排除頂部標題列與底部輸入框，僅計算實際聊天泡泡區域)：
    """
    shot = get_chat_history_image(chat_hwnd)
    if shot:
        return hashlib.md5(shot.tobytes()).hexdigest()
    return None

def verify_message_sent_to_chat(chat_hwnd):
    """最底部訊息是否明確在右側 (自己發的)；視窗太窄時先暫時最大化再判斷，細節見 _verify_message_sent_to_chat"""
    with _wide_window_for_judgment(chat_hwnd):
        return _verify_message_sent_to_chat(chat_hwnd)

def _verify_message_sent_to_chat(chat_hwnd):
    """
    發送後實體送達驗證 (三態邏輯中的明確 OUTGOING 判定)：
    - 嚴格要求 chat_hwnd 目前必須為前景視窗；若失焦、最小化或不可見，一律回傳 False。
    - 截圖並由下往上掃描最後一則訊息，只有當最底部訊息明確位於右側發送區 (latest_right_y > latest_left_y)，才回傳 True！
    - 若無訊息、最後一則在左側、或發生任何例外，均回傳 False (絕不假陽性)。
    """
    try:
        if not win32gui.IsWindow(chat_hwnd) or win32gui.IsIconic(chat_hwnd) or not win32gui.IsWindowVisible(chat_hwnd):
            return False
        if win32gui.GetForegroundWindow() != chat_hwnd:
            return False
        rect = get_window_true_rect(chat_hwnd)
        w = rect[2] - rect[0]
        h = rect[3] - rect[1]
        if w < 200 or h < 200:
            return False
        scale = 1.0
        try:
            dpi = ctypes.windll.user32.GetDpiForWindow(chat_hwnd)
            if dpi > 0:
                scale = dpi / 96.0
        except Exception:
            pass
        input_box_h = int(145 * scale)
        chat_crop_x = rect[0]
        chat_crop_y = rect[1] + int(h * 0.18)
        chat_crop_w = w
        chat_crop_h = max(50, h - int(h * 0.18) - input_box_h)
        shot = ImageGrab.grab(
            bbox=(chat_crop_x, chat_crop_y, chat_crop_x + chat_crop_w, chat_crop_y + chat_crop_h),
            all_screens=True
        )
        line_main_hwnd = find_line_main_window()
        is_main_win = (chat_hwnd == line_main_hwnd)
        if is_main_win and (w / scale) > 650:
            sidebar_w = int(320 * scale)
            chat_x_offset = sidebar_w
            chat_w = max(100, chat_crop_w - sidebar_w)
        else:
            chat_x_offset = 0
            chat_w = chat_crop_w

        sample_pts = [
            (chat_x_offset + int(chat_w * 0.5), 10),
            (chat_x_offset + 25, 10),
            (chat_x_offset + chat_w - 25, 10),
            (chat_x_offset + int(chat_w * 0.5), min(30, chat_crop_h - 5)),
        ]
        bg_colors = [shot.getpixel(pt) for pt in sample_pts]
        bg_r = sorted(c[0] for c in bg_colors)[len(bg_colors) // 2]
        bg_g = sorted(c[1] for c in bg_colors)[len(bg_colors) // 2]
        bg_b = sorted(c[2] for c in bg_colors)[len(bg_colors) // 2]

        def is_content_pixel(px):
            return (abs(px[0] - bg_r) + abs(px[1] - bg_g) + abs(px[2] - bg_b)) > 30

        left_x_start = chat_x_offset + int(chat_w * 0.03)
        left_x_end = chat_x_offset + int(chat_w * 0.12)
        right_x_start = chat_x_offset + int(chat_w * 0.88)
        right_x_end = chat_x_offset + int(chat_w * 0.97)

        latest_right_y = -1
        latest_left_y = -1

        for y in range(chat_crop_h - 1, 0, -2):
            for x in range(right_x_start, right_x_end, 4):
                if is_content_pixel(shot.getpixel((x, y))):
                    if latest_right_y == -1:
                        latest_right_y = y
                        break
            for x in range(left_x_start, left_x_end, 4):
                if is_content_pixel(shot.getpixel((x, y))):
                    if latest_left_y == -1:
                        latest_left_y = y
                        break
            if latest_right_y != -1 and latest_left_y != -1:
                break

        if latest_right_y != -1 and (latest_left_y == -1 or latest_right_y > latest_left_y):
            return True
        return False
    except Exception:
        return False


def report_ocr_engine():
    """啟動時回報用哪一套 OCR；沒安裝 RapidOCR 要明確警告 (辨識率會大幅下降，節日、時段、性質多半認不出來)"""
    if ocr_engine.is_available():
        log_v("[OCR] 使用 RapidOCR (離線)。")
        return True
    log_v(f"⚠️ [警告] 找不到 RapidOCR ({ocr_engine.unavailable_reason()})，退回 Windows 內建 OCR，辨識率大幅下降，"
          "多數圖會辨識不出來而被捨棄。請執行: pip install rapidocr-onnxruntime")
    return False

def warm_up_image_library(config):
    """在背景預先辨識整個圖庫 (結果快取在 image_kinds.json，之後挑圖不必等)，完成後回報各性質的張數"""
    images_dir = config.get("images_dir", "images")
    abs_dir = images_dir if os.path.isabs(images_dir) else os.path.join(SCRIPT_DIR, images_dir)
    os.makedirs(abs_dir, exist_ok=True)

    def work():
        try:
            # 圖庫不進版控，剛下載的專案是空的：啟動時先上網補到 min_image_pool 張 (已經夠就不連網)，再辨識
            get_images(images_dir, auto_replenish=config.get("auto_crawl_images", True), min_pool=config.get("min_image_pool", 10))
            library = image_library.scan_library(abs_dir, KIND_INDEX, log=log_v)
            counts = {}
            for img in library:
                counts[img.kind.category] = counts.get(img.kind.category, 0) + 1
            detail = "、".join(f"{image_kind.CATEGORY_NAMES[c]} {n}" for c, n in sorted(counts.items())) or "沒有"
            log_v(f"[圖庫] 辨識完成：可用 {len(library)} 張 ({detail})。辨識不出性質的圖不會被挑到。")
        except Exception as e:
            log_v(f"[警告] 預先辨識圖庫失敗: {e}")

    threading.Thread(target=work, name="library-warmup", daemon=True).start()

SKIPPED_RESULTS = ("SKIPPED_TEXT", "SKIPPED_NOT_GRANDMA", "SKIPPED_DAILY_LIMIT", "SKIPPED_NO_IMAGE")   # 已處理、沒有送出：不啟動冷卻、不重試

def _record_reply(target_name):
    """記下一次實際送出的回覆：冷卻、斷路器用的記帳，加上每日上限的計數"""
    record_reply_sent(target_name)
    reply_budget.record_reply(REPLY_COUNTS_PATH, target_name)
    note_reply_for_auto_stop()

def _forbidden_md5s(target_name, config):
    """百輪內不能再送給該對象的圖的 MD5 集合 (曾送出的加上對方曾傳來的)"""
    max_rounds = config.get("history_rounds", 100)
    history = load_contact_history()
    contact_data = history.get(target_name, {"sent": [], "received": []})
    if not isinstance(contact_data, dict):
        contact_data = {"sent": [], "received": []}
    sent_list = contact_data.get("sent", []) if isinstance(contact_data.get("sent"), list) else []
    recv_list = contact_data.get("received", []) if isinstance(contact_data.get("received"), list) else []
    return set(sent_list[-max_rounds:] + recv_list[-max_rounds:])

def choose_reply_image(target_name, incoming_kind, incoming_text, config):
    """
    依對方傳來的圖的性質挑回覆用的圖 (規則見 image_library.py)：同性質但不同張、百輪內沒送過。
    圖庫沒有合適的圖就立即上網抓 (邊抓邊辨識，抓到符合的就停)。挑不到回傳 None。
    """
    images_dir = config.get("images_dir", "images")
    abs_dir = images_dir if os.path.isabs(images_dir) else os.path.join(SCRIPT_DIR, images_dir)
    forbidden = _forbidden_md5s(target_name, config)
    slot = current_time_slot()
    pools = image_library.candidate_pools(incoming_kind, slot, config.get("time_aware_images", True))
    library = image_library.scan_library(abs_dir, KIND_INDEX, log=log_v)
    is_festival = incoming_kind.category == "festival"
    for pool in pools:
        picked = image_library.pick_from_pool(library, pool, incoming_text, forbidden)
        if picked:
            log_v(f" -> 圖庫挑到 {os.path.basename(picked.path)} ({picked.kind.label})，對方傳來的是 {incoming_kind.label}。")
            return picked.path
        if not config.get("auto_crawl_images", True):
            continue
        log_v(f" -> [即時抓圖] 圖庫沒有合適的圖 (對方傳來 {incoming_kind.label}，現在時段 {slot})，立即上網抓...")
        candidates = crawl_images.fetch_festival_candidates() if is_festival else build_candidates(_IMAGE_SOURCES)
        limit = config.get("instant_crawl_max_downloads", 40) * (2 if is_festival else 1)
        fetched = image_library.fetch_matching(pool, abs_dir, KIND_INDEX, candidates, download_and_validate_image,
                                               incoming_text, forbidden, max_downloads=limit,
                                               max_pool=config.get("max_image_pool", 120), log=log_v)
        if fetched:
            log_v(f" -> [即時抓圖] 抓到 {os.path.basename(fetched.path)} ({fetched.kind.label})。")
            return fetched.path
        library = image_library.scan_library(abs_dir, KIND_INDEX, log=log_v)     # 抓的過程中留下的庫存，下一個池子可能用得到
    return None

def _peek_incoming_text(target_name, config, info=None):
    """
    文字觸發時，在「隨機等待」之前先把視窗帶到前景看一眼最後一則是不是短問候或祝福 (文字，或一對一的貼圖)，看完馬上縮回去。
    不是就不用睡 5~900 秒 (主迴圈是單執行緒，睡著時其他好友都被擋住)，也不必往下走。
    回傳 (狀態, 性質, 文字)，同 recognize_incoming_text：True / False / None (暫時看不到，稍後重試)。
    info 是字典時，最底部是貼圖就寫入 info["sticker"] = True，呼叫端要靠它判斷接下來該不該回 (文字與貼圖各有各的開關)。
    """
    matched = find_target_chat_window(target_name)
    if not matched:
        return None, None, ""
    hwnd = matched[0][0]
    try:
        was_viewing = _user_is_viewing(hwnd)            # 在我們把視窗帶到前景之前量：你有沒有正在看它
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.3)
        activate_window(hwnd)
        time.sleep(0.4)
        if win32gui.GetForegroundWindow() != hwnd:
            return None, None, ""
        _scroll_chat_to_bottom(hwnd, was_viewing)
        if is_last_message_from_self_in_chat(hwnd):
            return False, None, ""                      # 最底部是自己發的：沒有新訊息要回
        max_chars = config.get("text_reply_max_chars", image_kind.TEXT_MESSAGE_MAX_CHARS)
        settled = False
        if _sticker_text_allowed(config, target_name):
            _wait_until_chat_settled(hwnd)
            settled = True
            sticker_state, sticker_crop = _bottom_sticker(hwnd, wait_seconds=STICKER_LOAD_WAIT_SECONDS)
            if sticker_state is None:
                return None, None, ""                   # 暫時看不清楚 (不是「不是貼圖」)：稍後重試
            if sticker_state:
                if info is not None:
                    info["sticker"] = True
                return recognize_incoming_sticker(hwnd, max_chars, first_crop=sticker_crop)    # 剛偵測過的裁切圖直接拿去讀，不再等一次、偵測一次
        return recognize_incoming_text(hwnd, max_chars, settle=not settled)
    finally:
        _minimize_if_configured(hwnd, config)


def _send_grandma_image_impl(target_name, config, triggered_by_photo=True, triggered_file=None, text_trigger=False):
    """執行長輩圖發送"""
    global last_global_reply_time
    # 每日上限：每個聊天室一天最多回覆 max_replies_per_day 次 (本機午夜重置，只計實際送出的)
    if is_group_target(target_name):
        max_per_day = config.get("group_max_replies_per_day", DEFAULT_CONFIG["group_max_replies_per_day"])
    else:
        max_per_day = config.get("max_replies_per_day", reply_budget.DEFAULT_MAX_PER_DAY)
    if not reply_budget.can_reply(REPLY_COUNTS_PATH, target_name, max_per_day):
        log_v(f"[每日上限]「{target_name}」今天已回覆 {reply_budget.count_today(REPLY_COUNTS_PATH, target_name)} 次 (上限 {max_per_day} 次)，不再回覆。")
        return "SKIPPED_DAILY_LIMIT"

    # 識別關卡 (Fail-Closed)：對方傳來的必須是辨識得出性質的長輩圖才回。貼圖、生活照、辨識不出來的、沒有圖可驗證的 (例如只傳文字) 一律不理會
    deferred_identify = False
    deferred_text = False
    incoming_kind, incoming_text = None, ""
    reply_text = None                               # 要回的是一句文字 (生日快樂 -> 謝謝)，不是圖
    thanks_text = birthday_reply_text(config, target_name)
    thanks_enabled = bool(thanks_text)
    # 貼圖上寫著短問候或祝福也回：只限一對一 (群組一律只回長輩圖照片)；字數上限與文字訊息共用 text_reply_max_chars
    sticker_text_ok = _sticker_text_allowed(config, target_name)
    text_max_chars = config.get("text_reply_max_chars", image_kind.TEXT_MESSAGE_MAX_CHARS)
    incoming_is_sticker = False
    if text_trigger and (config.get("reply_to_text", True) or thanks_enabled or sticker_text_ok) and config.get("require_grandma_image", True) and not is_group_target(target_name):
        deferred_text = deferred_identify = True      # 文字訊息：讀字要視窗在前景，等視窗帶到前景再辨識 (群組不適用)
    elif config.get("require_grandma_image", True) or is_group_target(target_name):     # 群組一律要先辨識出是長輩圖才回 (貼圖、生活照、辨識不出的不理會)
        early = find_target_chat_window(target_name)
        state, incoming_kind, incoming_text = recognize_incoming_image(triggered_file, early[0][0] if early else None)
        if state is False:
            return _skip_not_grandma(target_name, triggered_file, config, incoming_text)
        if state:
            log_v(f" -> [長輩圖識別]「{target_name}」傳來的圖通過識別，性質：{incoming_kind.label}。")
            if thanks_enabled and image_kind.is_birthday(incoming_kind) and image_kind.is_birthday_wish(incoming_text):
                reply_text = thanks_text
        deferred_identify = state is None     # 加密快取且視窗不在前景：等實際要回覆、視窗已帶到前景時再用截圖辨識
    else:
        incoming_kind = image_kind.ImageKind("greeting")     # 關閉識別：不管對方傳什麼，一律照現在時段回問候圖 (舊行為)

    if deferred_text:
        peek_info = {}
        peek_state, _peek_kind, _peek_text = _peek_incoming_text(target_name, config, peek_info)
        if peek_state is None:
            log_v(f"[文字識別待確認]「{target_name}」暫時看不到最後一則文字，本次不處理，稍後重試。")
            return "NOT_SENT"
        if not peek_state:
            if peek_info.get("sticker"):
                log_v(f"[貼圖略過]「{target_name}」傳來的貼圖上沒有短問候或祝福 (沒字、字不是問候、太長或競選相關)，不回覆。")
            else:
                log_v(f"[文字訊息略過]「{target_name}」最後一則文字不是短問候或祝福 (太長、不是問候、競選相關或讀不到字)，不回覆。")
            return "SKIPPED_TEXT"
        if (not peek_info.get("sticker") and not config.get("reply_to_text", True)
                and not (thanks_enabled and image_kind.is_birthday(_peek_kind) and image_kind.is_birthday_wish(_peek_text))):
            # 文字回覆關著、而且不是會回「謝謝」的生日祝賀：之後一定是略過，不要先睡 5~900 秒才略過 (只開貼圖設定時文字問候會走到這裡)
            log_v(f"[文字訊息略過]「{target_name}」傳來的文字不是生日祝福，而且 reply_to_text 已關閉，不回覆。")
            return "SKIPPED_TEXT"

    # 借花獻佛：僅精確收納本次觸發該回覆的圖片，絕不掃描其他聊天室快取
    if config.get("collect_incoming_images", True) and triggered_file:
        max_rounds = config.get("history_rounds", 100)
        harvest_line_incoming_images(config.get("images_dir", "images"), sender_name=target_name, max_rounds=max_rounds, specific_file_path=triggered_file)

    # 圖庫庫存不足時先補一批 (辨識不出來的會被捨棄)
    get_images(config.get("images_dir", "images"), auto_replenish=config.get("auto_crawl_images", True), min_pool=config.get("min_image_pool", 10))

    selected_image = None
    picked_slot = current_time_slot()
    log_v(f"\n偵測到「{target_name}」發來訊息！")
    if reply_text:
        log_v(f" -> 對方傳來的是生日祝福，只回一句「{reply_text}」，不回圖。")
    elif not deferred_identify:
        selected_image = choose_reply_image(target_name, incoming_kind, incoming_text, config)
        if not selected_image:
            log_v(f"[無合適圖片]「{target_name}」傳來 {incoming_kind.label}，圖庫與即時抓圖都找不到合適的回覆圖，本次不回覆。")
            return "SKIPPED_NO_IMAGE"
        log_v(f" -> 挑選回覆圖片: {os.path.basename(selected_image)}")
    else:
        log_v(" -> 對方那則文字的性質等視窗在前景時再辨識。" if deferred_text else " -> 對方那張圖的性質等視窗在前景時再辨識。")

    # 隨機延遲時間 (避免固定規律被看穿是機器人)
    min_d = max(1, config.get("min_delay_seconds", 5))
    max_d = max(min_d, config.get("max_delay_seconds", 900))
    delay = random.randint(min_d, max_d)
    log_v(f" -> 為了模擬真人自然反應，本次隨機等待 {delay} 秒後發送...")
    countdown_sleep(delay, abort_check=lambda: check_stop_command(config))

    # 等待期間時段可能已跨越 (例如 10:55 挑早安圖、11:10 才送出)：節日以外的回覆送出前重新挑圖
    if selected_image and config.get("time_aware_images", True) and incoming_kind.category != "festival" and current_time_slot() != picked_slot:
        log_v(f" -> [時段重驗] 等待後時段已變為 {current_time_slot()}，重新挑選...")
        selected_image = choose_reply_image(target_name, incoming_kind, incoming_text, config)
        if not selected_image:
            log_v("⚠️ [時段保護] 重新挑選後仍無合適長輩圖，安全終止本次發送。")
            return "SKIPPED_NO_IMAGE"
        log_v(f" -> [時段重驗] 改選圖片: {os.path.basename(selected_image)}")

    # 尋找現有聊天視窗 (嚴格 Fail-Closed)；視窗不見時只會用 chat_opener 做「完全相符」的開啟，絕不盲目開啟其他視窗
    matched = find_target_chat_window(target_name)
    if not matched and open_missing_chat_window(target_name, config):
        matched = find_target_chat_window(target_name)
    if not matched:
        log_v(f"[錯誤] 無法取得「{target_name}」的聊天視窗，為防誤傳終止發送。請在 LINE 中保持該視窗開啟。")
        return "NOT_SENT"

    chat_hwnd = matched[0][0]
    was_viewing = _user_is_viewing(chat_hwnd)           # 在我們把視窗帶到前景之前量：你有沒有正在看它
    if win32gui.IsIconic(chat_hwnd):
        win32gui.ShowWindow(chat_hwnd, win32con.SW_RESTORE)
        time.sleep(0.3)
    activate_window(chat_hwnd)
    time.sleep(0.3)
    _scroll_chat_to_bottom(chat_hwnd, was_viewing)
    if not focus_chat_input(chat_hwnd, target_name):
        log_v(f"[安全攔截] 無法聚焦「{target_name}」視窗之輸入框，為防誤操作終止發送！")
        _minimize_if_configured(chat_hwnd, config)
        return "NOT_SENT"

    time.sleep(0.4)

    # 送出前嚴格安全核對：前景視窗 HWND 與標題必須完全符合目標對象，否則絕不發送！
    fg_hwnd = win32gui.GetForegroundWindow()
    if fg_hwnd != chat_hwnd:
        log_v(f"[致命安全攔截] 目前前景視窗 (HWND: {fg_hwnd}) 非目標聊天室視窗 (HWND: {chat_hwnd})，嚴格終止發送！")
        _minimize_if_configured(chat_hwnd, config)
        return "NOT_SENT"

    fg_title = win32gui.GetWindowText(fg_hwnd)
    # 嚴格群組排除：任何帶有成員數或未讀括號的視窗，在前景時均視為群組（因 1:1 視窗置於前景後未讀數已自動清除，完全不帶括號）
    if not is_group_target(target_name) and re.search(r"[\(\uFF08]\d+\+?[\)\uFF09]", fg_title):
        log_v(f"[致命安全攔截] 目前前景視窗「{fg_title}」帶有成員數或未讀括號，判定為群組視窗，嚴格拒絕發送！")
        _minimize_if_configured(chat_hwnd, config)
        return "NOT_SENT"

    if not title_matches_target(fg_title, target_name):
        log_v(f"[致命安全攔截] 目前前景視窗為「{fg_title}」，非 1:1 目標對象「{target_name}」，嚴格終止發送！")
        _minimize_if_configured(chat_hwnd, config)
        return "NOT_SENT"

    # 實體位置方向核驗 (Fail-Closed)：在貼上圖片發送前核驗聊天室最後一則訊息
    if is_last_message_from_self_in_chat(chat_hwnd):
        log_v(f"[防自循環保護]「{target_name}」視窗內最後訊息位於右側（本機送出），安全攔截絕不發送！")
        _minimize_if_configured(chat_hwnd, config)
        return "NOT_SENT"

    # 訊息類型核驗 (Fail-Closed)：對方最後一則若只是文字氣泡，不需回應 (trigger_mode="all" 時文字也要回，不套用)
    if config.get("trigger_mode", "photo_and_sticker") != "all" or deferred_text:
        is_media = _media_type_with_retry(chat_hwnd, expect_media=triggered_file is not None)
        if is_media is None:
            # 重試後仍無法判斷 (截圖失敗、視窗被遮住等)：不丟棄，走既有重試流程
            log_v(f"[訊息類型待確認]「{target_name}」暫時無法判斷最後一則訊息類型，本次不發送，稍後重試。")
            _minimize_if_configured(chat_hwnd, config)
            return "NOT_SENT"
        if deferred_text and is_media:
            sticker_state = _bottom_sticker(chat_hwnd)[0] if sticker_text_ok else False
            if sticker_state is None:
                # 貼圖設定開著，但暫時分不出是貼圖還是照片：不能當成「不是貼圖」而永久略過
                log_v(f"[貼圖待確認]「{target_name}」暫時分不出最後一則是貼圖還是照片，本次不發送，稍後重試。")
                _minimize_if_configured(chat_hwnd, config)
                return "NOT_SENT"
            if sticker_state:
                incoming_is_sticker = True          # 貼圖：下面讀貼圖上的字，是問候才回 (貼圖不走快取路徑，只能在這裡處理)
            else:
                # 被「文字」觸發，但最底部其實是圖片 (或貼圖而且 reply_to_sticker_text 關著)：圖片是快取路徑的事，這裡不當文字處理
                log_v(f"[文字訊息略過]「{target_name}」最後一則訊息是圖片或貼圖，不是文字。")
                _finish_with_window(chat_hwnd, config)
                return "SKIPPED_TEXT"
        if not is_media and not deferred_text:
            log_v(f"[文字訊息略過]「{target_name}」最後一則訊息為文字，不需回應。")
            _finish_with_window(chat_hwnd, config)
            return "SKIPPED_TEXT"

    # 延後的識別：視窗此時已在前景，用截圖辨識對方那張圖的性質 (Fail-Closed)，再挑回覆圖
    if deferred_text:
        if incoming_is_sticker:
            state, incoming_kind, incoming_text = recognize_incoming_sticker(chat_hwnd, text_max_chars)
        else:
            state, incoming_kind, incoming_text = recognize_incoming_text(chat_hwnd, text_max_chars)
        if state is None:
            log_v(f"[文字識別待確認]「{target_name}」暫時無法讀取對方傳來的{'貼圖' if incoming_is_sticker else '文字'}，本次不發送，稍後重試。")
            _minimize_if_configured(chat_hwnd, config)
            return "NOT_SENT"
        if not state:
            if incoming_is_sticker:
                log_v(f"[貼圖略過]「{target_name}」傳來的貼圖上沒有短問候或祝福 (沒字、字不是問候、太長或競選相關)，不回覆。")
            else:
                log_v(f"[文字訊息略過]「{target_name}」最後一則文字不是短問候或祝福 (太長、不是問候、競選相關或讀不到字)，不回覆。")
            _finish_with_window(chat_hwnd, config)
            return "SKIPPED_TEXT"
        if incoming_is_sticker:
            log_v(f" -> [貼圖識別]「{target_name}」傳來的貼圖寫著問候或祝福，性質：{incoming_kind.label}。")
        else:
            log_v(f" -> [文字識別]「{target_name}」傳來的文字通過識別，性質：{incoming_kind.label}。")
        if thanks_enabled and image_kind.is_birthday(incoming_kind) and image_kind.is_birthday_wish(incoming_text):
            reply_text = thanks_text
            log_v(f" -> 對方傳來的是生日祝福，只回一句「{reply_text}」，不回圖。")
        elif not config.get("reply_to_text", True) and not incoming_is_sticker:
            log_v(f"[文字訊息略過]「{target_name}」傳來的文字不是生日祝福，而且 reply_to_text 已關閉，不回覆。")
            _finish_with_window(chat_hwnd, config)
            return "SKIPPED_TEXT"
        else:
            selected_image = choose_reply_image(target_name, incoming_kind, incoming_text, config)
            if not selected_image:
                log_v(f"[無合適圖片]「{target_name}」傳來 {incoming_kind.label} 的文字，圖庫與即時抓圖都找不到合適的回覆圖，本次不回覆。")
                _finish_with_window(chat_hwnd, config)
                return "SKIPPED_NO_IMAGE"
            log_v(f" -> 挑選回覆圖片: {os.path.basename(selected_image)}")
    elif deferred_identify:
        state, incoming_kind, incoming_text = recognize_incoming_image(triggered_file, chat_hwnd)
        if state is None:
            log_v(f"[長輩圖識別待確認]「{target_name}」暫時無法識別對方傳來的圖，本次不發送，稍後重試。")
            _minimize_if_configured(chat_hwnd, config)
            return "NOT_SENT"
        if not state:
            _finish_with_window(chat_hwnd, config)
            return _skip_not_grandma(target_name, triggered_file, config, incoming_text)
        log_v(f" -> [長輩圖識別]「{target_name}」傳來的圖通過識別 (視窗截圖)，性質：{incoming_kind.label}。")
        if thanks_enabled and image_kind.is_birthday(incoming_kind) and image_kind.is_birthday_wish(incoming_text):
            reply_text = thanks_text
            log_v(f" -> 對方傳來的是生日祝福，只回一句「{reply_text}」，不回圖。")
        else:
            selected_image = choose_reply_image(target_name, incoming_kind, incoming_text, config)
            if not selected_image:
                log_v(f"[無合適圖片]「{target_name}」傳來 {incoming_kind.label}，圖庫與即時抓圖都找不到合適的回覆圖，本次不回覆。")
                _finish_with_window(chat_hwnd, config)
                return "SKIPPED_NO_IMAGE"
            log_v(f" -> 挑選回覆圖片: {os.path.basename(selected_image)}")

    # 發送前先對快取進行檔案集合快照 (包含 mtime 與大小)
    active_cache = get_active_line_cache_dir()
    before_cache_files = get_all_cache_files(active_cache)

    # 送出前核驗前景視窗未被轉移
    if win32gui.GetForegroundWindow() != chat_hwnd:
        log_v(f"[安全攔截] 送出前焦點已被移轉至其他視窗，終止發送！")
        _minimize_if_configured(chat_hwnd, config)
        return "NOT_SENT"

    # 送出圖片與記帳防護 (保證例外發生時不丟失記帳、不重複記帳)。圖片經附加檔案對話框送出，沒有貼上、不會在輸入框留下殘留
    enter_sent = False
    accounting_done = False
    history_recorded = False
    try:
        if reply_text:
            log_v(f" -> 送出文字「{reply_text}」...")
            if not send_text_via_input(chat_hwnd, reply_text):
                log_v("[安全攔截] 無法把文字送進輸入框，本次未送出。")
                _minimize_if_configured(chat_hwnd, config)
                return "NOT_SENT"
        else:
            log_v(" -> 透過附加檔案按鈕送出長輩圖...")
            if not send_image_via_attach_dialog(chat_hwnd, selected_image):
                log_v("[安全攔截] 無法經由附加檔案對話框送出長輩圖，本次未送出。")
                _minimize_if_configured(chat_hwnd, config)
                return "NOT_SENT"
        enter_sent = True
        time.sleep(1.0)

        # 發送後實體確認與快取差集快照 (分段輪詢以完整捕捉本機送出產生的快取檔案)
        time.sleep(1.0)
        after_cache_files = get_all_cache_files(active_cache)
        time.sleep(1.0)
        after_cache_files.update(get_all_cache_files(active_cache))
        now_ts = time.time()
        new_count = 0

        # 精確黑名單隔離：僅將屬於本次發送目標對象聊天室的新增快取列入黑名單，絕不誤封鎖其他好友同時送來的訊息！
        c_map = load_chat_dirs_map()
        target_chat_hashes = {h for h, t in c_map.items() if t.lower() == target_name.lower()}

        with _state_lock:
            for p, meta in after_cache_files.items():
                if p not in before_cache_files or before_cache_files[p] != meta:
                    p_hash = extract_chat_hash_from_path(p)
                    # 嚴格黑名單與確認隔離：只有當確定屬於目標對象已知頻道時才加入黑名單並計數，絕不吃進他人快取
                    if target_chat_hashes and (p_hash in target_chat_hashes):
                        IGNORED_CACHE_FILES[p] = (now_ts, meta[0], meta[1])
                        new_count += 1

        # 實體送達驗證：Enter 鍵已按下，若氣泡已明確出現在右側，確立為 SENT_CONFIRMED；
        # 若氣泡因失焦或繪製延遲無法確認，則判定為 SENT_UNCERTAIN (嚴禁重複重送，防止雙重發送長輩圖)
        verified_bubble = verify_message_sent_to_chat(chat_hwnd)

        send_now = time.time()
        last_global_reply_time = send_now
        _record_reply(target_name)
        accounting_done = True

        if new_count > 0:
            log_v(f" -> [差集快照] 成功辨識 {new_count} 個本機發送快取檔案，加入黑名單！")

        # 安全最小化 (避免維持在前景導致後續持續秒已讀)
        if config.get("close_window_after_send", True) or chat_hwnd in _BOT_OPENED_HWNDS:
            time.sleep(1.5)
            _finish_with_window(chat_hwnd, config, log_target=target_name)

        # Enter 已按下：無論氣泡是否延遲渲染，一律寫入百輪記憶庫，杜絕未來重複挑選同一張發送 (只回文字時沒有圖)
        if not reply_text:
            sent_md5 = get_file_md5(selected_image)
            record_contact_sent(target_name, sent_md5, max_rounds=config.get("history_rounds", 100))
        history_recorded = True

        # 若啟用 replace_after_send，送出後自動淘汰該張長輩圖以維持圖庫滾動換新
        if config.get("replace_after_send", True) and selected_image and not reply_text:
            try:
                images_dir_path = os.path.join(SCRIPT_DIR, config.get("images_dir", "images"))
                retire_image(selected_image, images_dir=images_dir_path)
            except Exception as e:
                log_v(f"[圖庫汰換] 自動汰換圖片失敗: {e}")

        what = f"文字「{reply_text}」" if reply_text else "長輩圖"
        notifier.notify(f"📨 已回覆「{target_name}」{what}" if reply_text else f"📨 已回覆「{target_name}」長輩圖：{os.path.basename(selected_image)}")
        if verified_bubble:
            log_v(f"[送達確認]「{target_name}」聊天視窗右側氣泡驗證通過，{what}確認已成功送出！")
            return "SENT_CONFIRMED"
        else:
            log_v(f"{what}已按下 Enter 送出，視覺氣泡暫無法 100% 辨識 (判定為 SENT_UNCERTAIN)。已記為送出並記入百輪歷史，絕不重複重送。")
            return "SENT_UNCERTAIN"
    except Exception as e:
        log_v(f"[例外防護] 發送流程發生異常: {e}")
        if not enter_sent:
            # 尚未送出就中斷（包括 FailSafeException）：send_image_via_attach_dialog 沒回傳 True 就代表什麼都沒送
            # 回傳 NOT_SENT 讓外層計入重試次數並執行指數退避，徹底杜絕無上限高頻無窮迴圈重試
            return "NOT_SENT"
        else:
            # Enter 已送出：若尚未記帳，補全發送記帳與百輪記錄，杜絕二次重複記帳與重複發送
            if not accounting_done:
                send_now = time.time()
                last_global_reply_time = send_now
                _record_reply(target_name)
                accounting_done = True
            if not history_recorded:
                if not reply_text:
                    sent_md5 = get_file_md5(selected_image)
                    record_contact_sent(target_name, sent_md5, max_rounds=config.get("history_rounds", 100))
                history_recorded = True
            return "SENT_UNCERTAIN"

def send_grandma_image(target_name, config, triggered_by_photo=True, triggered_file=None, text_trigger=False):
    """
    送出流程 (見 _send_grandma_image_impl)。結果不是「稍後重試」(NOT_SENT) 時，順手把機器人自己為這位對象開出來的視窗關掉：
    不論是送出成功，或在「看一眼」、每日上限、沒有合適圖片等提早結束的路徑，不然那個視窗會一直留在桌面上。
    出例外時不關 (視窗留著方便查)。
    """
    result = None
    try:
        result = _send_grandma_image_impl(target_name, config, triggered_by_photo, triggered_file, text_trigger)
        return result
    finally:
        if result is not None and result != "NOT_SENT" and _BOT_OPENED_HWNDS:
            try:
                wins = find_target_chat_window(target_name)
                if wins:
                    _close_if_bot_opened(wins[0][0], config, target_name)
            except Exception:
                pass


def check_for_incoming_messages(config):
    """
    掃描是否有新訊息：
    1. 視窗標題正規表達式未讀檢查 (嚴格比對字尾括號數字，如 (1) 或 （2）)，排除群組名稱誤判。
    2. 5 秒發送防抖保護與實體快取差集黑名單過濾。
    3. 斷路器保護 (Circuit Breaker)：5 分鐘內同對象最多回覆 2 次，超標啟動 30 分鐘退避鎖定。
    4. 實體位置方向核驗 (Fail-Closed)：對話框最後一則在右側或不可見時絕不發送。
    """
    global last_seen_cache_mtime, last_global_reply_time, PROCESSED_CACHE_FILES
    new_image_found = None
    targets = config.get("targets", [])
    cooldown = config.get("cooldown_seconds", 30)
    current_time = time.time()
    update_target_window_fingerprints(targets)
    check_stop_command(config)

    # 全域防洗版保護：只要剛送出過任何圖片，在冷卻時間內絕不再次觸發！
    if current_time - last_global_reply_time < cooldown:
        return

    trigger_mode = config.get("trigger_mode", "photo_and_sticker")
    # 0. 超前檢查：發送失敗之暫時重試佇列 (保證照片與貼圖失敗後在冷卻期過後能精確重試)
    target_to_reply = None
    new_image_found = None
    text_trigger = False
    for target in targets:
        if target in PENDING_RETRY_MEDIA:
            last_time = last_reply_times.get(target, 0)
            if current_time - last_time >= cooldown:
                target_to_reply = target
                new_image_found = PENDING_RETRY_MEDIA[target]
                log_v(f" -> [重試觸發]「{target}」暫存佇列中的媒體已過冷卻期，啟動安全重試發送！")
                break

    # 1. 優先檢查：桌面上的未讀視窗標題 (正則嚴格比對字尾括號數字，如 "(1)"、"（2）")
    # 核心機制：以未讀數字「遞增」作為唯一觸發依據，同一個未讀數字永不重複觸發！
    windows = get_all_line_windows() if not target_to_reply else []

    for target in targets:
        if target_to_reply:
            break
        if is_group_target(target):
            continue                                   # 群組標題的數字是成員數，不是未讀數：只走下面的快取新圖偵測
        last_time = last_reply_times.get(target, 0)
        # 不論是否在冷卻期或觸發模式，持續觀察視窗未讀標記並動態校準基準線
        for hwnd, title, cls, vis in windows:
            m = re.search(r"^(.*?)\s*[\(\uFF08](\d+\+?)[\)\uFF09]\s*$", title)
            if m:
                name_part = m.group(1).strip()
                if name_part.lower() == target.lower():
                    raw_unread = m.group(2)
                    current_unread = parse_unread_count(raw_unread)
                    last_processed = LAST_PROCESSED_UNREAD.get(target, 0)
                    if current_unread > last_processed:
                        # 讀取已受控校準之頻道映射表 (chat_dirs.json，嚴格 schema 過濾)
                        c_dirs = load_chat_dirs_map()
                        target_hashes = [h for h, t in c_dirs.items() if t.lower() == target.lower()]
                        recent_media = None
                        active_c = get_active_line_cache_dir()
                        if active_c and target_hashes:
                            found_recent = False
                            for r_root, _, r_files in os.walk(active_c):
                                if found_recent:
                                    break
                                if is_media_cache_dir(r_root, active_c):
                                    ch = extract_chat_hash_from_path(r_root)
                                    if ch in target_hashes:
                                        for r_f in r_files:
                                            r_p = os.path.join(r_root, r_f)
                                            try:
                                                r_m = os.path.getmtime(r_p)
                                                if current_time - r_m < 5.0:
                                                    r_sz = os.path.getsize(r_p)
                                                    # 內容改寫偵測：精確比對 mtime 與檔案大小，避免同檔名覆寫被誤封鎖
                                                    if r_p in IGNORED_CACHE_FILES:
                                                        ign_ts, ign_m, ign_sz = IGNORED_CACHE_FILES[r_p]
                                                        if abs(r_m - ign_m) < 1.0 and r_sz == ign_sz:
                                                            continue
                                                    if any(r_f.lower().endswith(ext) for ext in ('.mp4', '.m4a', '.aac', '.opus', '.wav', '.mp3', '.avi', '.mov', '.pdf', '.zip', '.txt', '.docx', '.xlsx', '.pptx')):
                                                        continue
                                                    if trigger_mode == "photo_only" and not is_likely_photo(r_p):
                                                        continue
                                                    # 嚴格驗證真實圖片 magic bytes 或 LINE 加密圖 (.eimg)，排除 Office 附件與未知二進位
                                                    if (r_f.endswith('.eimg') or r_sz > 5000) and is_valid_image_or_eimg(r_p):
                                                        recent_media = r_p
                                                        found_recent = True
                                                        break
                                            except Exception:
                                                pass
                                            if recent_media:
                                                break
                        if recent_media:
                            if current_time - last_time >= cooldown and current_time - LAST_SENT_TIME_BY_TARGET.get(target, 0) >= 5:
                                log_v(f" -> [新訊息觸發]「{target}」未讀數增加且偵測到合格新媒體，準備自動回覆！")
                                target_to_reply = target
                                new_image_found = recent_media
                                break
                        elif trigger_mode == "all":
                            if current_time - last_time >= cooldown and current_time - LAST_SENT_TIME_BY_TARGET.get(target, 0) >= cooldown:
                                log_v(f" -> [文字訊息觸發]「{target}」未讀數增加 (all 模式)，準備自動回覆！")
                                target_to_reply = target
                                break
                        elif _text_trigger_enabled(config, target) and config.get("require_grandma_image", True):
                            # 文字訊息沒有快取檔，只能靠未讀數增加；內容要等視窗帶到前景才讀得到，是不是問候由送出流程判斷
                            if current_time - last_time >= cooldown and current_time - LAST_SENT_TIME_BY_TARGET.get(target, 0) >= 5:
                                log_v(f" -> [文字訊息觸發]「{target}」未讀數增加且沒有新的圖片或貼圖，準備讀取最後一則文字！")
                                target_to_reply = target
                                text_trigger = True
                                break
                        else:
                            # 未讀遞增但無對應新媒體快取：若正處於失敗重試中，嚴格不推進已處理基準線！
                            if target not in PENDING_RETRY_MEDIA and SEND_FAIL_COUNTS.get(target, 0) == 0:
                                LAST_PROCESSED_UNREAD[target] = current_unread
                                save_circuit_breaker_state()
                    elif current_unread < last_processed:
                        # 使用者手動已讀或已處理，即時向下校正基準線並持久化
                        LAST_PROCESSED_UNREAD[target] = current_unread
                        save_circuit_breaker_state()
            elif title.strip().lower() == target.lower():
                # 視窗標題無未讀數字且精確相符，代表目前為已讀狀態
                if target in LAST_PROCESSED_UNREAD and LAST_PROCESSED_UNREAD[target] > 0:
                    LAST_PROCESSED_UNREAD[target] = 0
                    save_circuit_breaker_state()
        if target_to_reply:
            break

    # 1b. 文字訊息：視窗展開、放在背景時看訊息列表有沒有變化 (標題沒有未讀數的 LINE 版本只能這樣偵測)
    if not target_to_reply and config.get("require_grandma_image", True):
        for target in targets:
            if is_group_target(target):
                continue
            if not _text_trigger_enabled(config, target):
                continue                               # 這位好友的文字、生日「謝謝」、貼圖都不回：不用看他的訊息列表
            try:
                changed = text_window_changed(windows, target, config)
            except Exception as e:
                log_v(f"[警告] 檢查「{target}」的訊息列表時發生例外: {e}")
                continue
            if changed and current_time - last_reply_times.get(target, 0) >= cooldown and current_time - LAST_SENT_TIME_BY_TARGET.get(target, 0) >= 5:
                log_v(f" -> [文字訊息觸發]「{target}」視窗的訊息列表有變化，準備讀取最後一則文字！")
                target_to_reply = target
                text_trigger = True
                break

    # 2. 次要檢查：底層快取新圖片監聽 (照片與貼圖專屬通道)
    if not target_to_reply:
        prune_ignored_cache_files()
        active_cache = get_active_line_cache_dir()
        chosen_candidate = None
        chosen_target = None

        if active_cache:
            candidate_files = []
            chat_dirs_map = load_chat_dirs_map()
            for root, _, files in os.walk(active_cache):
                if is_media_cache_dir(root, active_cache):
                    for f in files:
                        p = os.path.join(root, f)
                        try:
                            m = os.path.getmtime(p)
                            if (p, m) in PROCESSED_CACHE_FILES:
                                continue
                            if _in_view_bind_window(m):
                                PROCESSED_CACHE_FILES.add((p, m))       # 自動綁定時我們自己點圖造成的寫入，不是對方傳來的新圖
                                continue
                            if m >= last_seen_cache_mtime:
                                # 🟡 核心防禦：在所有大小/格式過濾前，先登記所有具快取異動的未知頻道，
                                # 防止目標好友傳貼圖/被過濾小檔時，未被算入 competing_count 導致其他好友頻道被誤綁！
                                h_any = extract_chat_hash_from_path(p)
                                if h_any:
                                    if h_any in chat_dirs_map:
                                        _RECENT_UNKNOWN_CHANNELS.pop(h_any, None)
                                    elif h_any not in _RECENT_UNKNOWN_CHANNELS:
                                        _RECENT_UNKNOWN_CHANNELS[h_any] = current_time
                                sz = os.path.getsize(p)
                                if p in IGNORED_CACHE_FILES:
                                    ign_ts, ign_m, ign_sz = IGNORED_CACHE_FILES[p]
                                    if abs(m - ign_m) < 1.0 and sz == ign_sz:
                                        continue
                                if any(f.lower().endswith(ext) for ext in ('.mp4', '.m4a', '.aac', '.opus', '.wav', '.mp3', '.avi', '.mov', '.pdf', '.zip', '.txt')):
                                    continue
                                if trigger_mode == "photo_only" and not is_likely_photo(p):
                                    continue
                                if (f.endswith('.eimg') or sz > 5000) and is_valid_image_or_eimg(p):
                                    candidate_files.append((m, p))
                        except Exception:
                            pass

            if candidate_files:
                candidate_files.sort(key=lambda x: x[0])
                chat_dirs_map = load_chat_dirs_map()

                # 維護 15 秒滑動視窗未知頻道活動記錄 (中度 #1 跨輪詢多頻道並行隔離防禦)
                # 僅在新未知頻道初次出現時記錄時間戳，不於每輪重複更新；已綁定頻道即刻移出
                for _, p in candidate_files:
                    h = extract_chat_hash_from_path(p)
                    if h:
                        if h in chat_dirs_map:
                            _RECENT_UNKNOWN_CHANNELS.pop(h, None)
                        elif h not in _RECENT_UNKNOWN_CHANNELS:
                            _RECENT_UNKNOWN_CHANNELS[h] = current_time

                # 清理超逾 15 秒之未知頻道活動記錄
                for h in list(_RECENT_UNKNOWN_CHANNELS.keys()):
                    if current_time - _RECENT_UNKNOWN_CHANNELS[h] > AUTOBIND_TIMEOUT or h in chat_dirs_map:
                        _RECENT_UNKNOWN_CHANNELS.pop(h, None)

                competing_count = len(_RECENT_UNKNOWN_CHANNELS)

                max_processed_m = last_seen_cache_mtime
                deferred_mtimes = []
                view_bind_wanted = False

                for cand_m, cand_path in candidate_files:
                    ch = extract_chat_hash_from_path(cand_path)
                    if not ch:
                        PROCESSED_CACHE_FILES.add((cand_path, cand_m))
                        max_processed_m = max(max_processed_m, cand_m)
                        continue

                    cand_target = None
                    if ch in chat_dirs_map:
                        cand_target = chat_dirs_map[ch]
                        # 🟡 核心防禦：暫定綁定二次獨立圖片核驗與錯綁自動撤銷
                        if ch in _PROVISIONAL_BINDINGS:
                            prov_info = _PROVISIONAL_BINDINGS[ch]
                            prov_target = prov_info["target"]
                            bind_fp = prov_info["bind_fp"]
                            bind_time = prov_info["bind_time"]
                            now_chk = time.time()

                            # 4. 舊圖過濾 (中度防禦)：若快取檔案時間早於綁定時間 (使用者滾動檢視舊圖)，標記處理並跳過核驗與撤銷
                            if cand_m < bind_time:
                                PROCESSED_CACHE_FILES.add((cand_path, cand_m))
                                max_processed_m = max(max_processed_m, cand_m)
                                continue

                            # 晉升永久嚴格要求實質新事件核驗 (至少相隔 6.0s 獨立事件間隔)，絕不憑空純按時間盲目晉升
                            promoted = False
                            if cand_m >= bind_time + AUTOBIND_TIME_WINDOW:
                                wins = find_target_chat_window(prov_target)
                                if wins:
                                    hwnd = wins[0][0]
                                    # 最小化視窗缺乏指紋異動，絕對不誤撤銷合法綁定
                                    if not win32gui.IsIconic(hwnd):
                                        curr_fp = get_chat_window_fingerprint(hwnd)
                                        if curr_fp is not None:
                                            w_state = _TARGET_WINDOW_STATE.get(prov_target)
                                            has_media, is_incoming = get_chat_latest_media_info(hwnd)
                                            fp_changed = (curr_fp != bind_fp)

                                            # 晉升永久：必須為獨立新傳入圖片，且時間差在 ±AUTOBIND_TIME_WINDOW (6.0s) 之內
                                            target_has_incoming_media = (
                                                w_state is not None
                                                and has_media
                                                and is_incoming
                                                and fp_changed
                                                and abs(cand_m - w_state.get("changed_at", 0)) <= AUTOBIND_TIME_WINDOW
                                            )

                                            # 視窗完全閒置判定 (以快取檔案時間 cand_m 與時鐘雙重防禦，杜絕輪詢延遲誤撤銷)：
                                            # 1. 目前視窗指紋已與已登記狀態完全同步 (無新抵達未處理指紋)
                                            # 2. cand_m 與目前時鐘皆距目標視窗最近異動超過 AUTOBIND_TIMEOUT=15s (縮圖渲染考驗期已渡過)
                                            is_window_completely_idle = (
                                                w_state is not None
                                                and curr_fp == w_state.get("fp")
                                                and (abs(cand_m - w_state.get("changed_at", 0)) > AUTOBIND_TIMEOUT)
                                                and (now_chk - w_state.get("changed_at", 0) > AUTOBIND_TIMEOUT)
                                            )

                                            if target_has_incoming_media:
                                                try:
                                                    unbound_on_disk = False
                                                    with locked_chat_dirs() as disk_map:
                                                        if disk_map.get(ch) != prov_target:
                                                            unbound_on_disk = True
                                                        elif isinstance(disk_map.get("_provisional"), dict):
                                                            disk_map["_provisional"].pop(ch, None)
                                                    if unbound_on_disk:
                                                        # 磁碟綁定已被 --unbind 清除：同步清除記憶體並放棄該圖片
                                                        chat_dirs_map.pop(ch, None)
                                                        _PROVISIONAL_BINDINGS.pop(ch, None)
                                                        PROCESSED_CACHE_FILES.add((cand_path, cand_m))
                                                        max_processed_m = max(max_processed_m, cand_m)
                                                        log_v(f"[安全隔離] 頻道「{ch[:8]}...」綁定已於磁碟清除，同步清除記憶體並略過該圖片。")
                                                        continue
                                                    _PROVISIONAL_BINDINGS.pop(ch, None)
                                                    promoted = True
                                                    log_v(f"[綁定確認] 頻道「{ch[:8]}...」經獨立傳入圖片二次核驗無誤，正式確立與「{prov_target}」之永久綁定！")
                                                except Exception as e:
                                                    log_v(f"[警告] 綁定確認寫入磁碟失敗: {e}")
                                            elif fp_changed and is_window_completely_idle:
                                                # 撤銷條件：視窗指紋實質改變轉入其他訊息，且目標視窗在 cand_m 期間徹底閒置無對應傳入媒體，證實錯綁
                                                try:
                                                    with locked_chat_dirs() as disk_map:
                                                        if disk_map.get(ch) == prov_target:
                                                            disk_map.pop(ch, None)
                                                        if isinstance(disk_map.get("_provisional"), dict):
                                                            disk_map["_provisional"].pop(ch, None)
                                                    chat_dirs_map.pop(ch, None)
                                                    _PROVISIONAL_BINDINGS.pop(ch, None)
                                                    PROCESSED_CACHE_FILES.add((cand_path, cand_m))
                                                    max_processed_m = max(max_processed_m, cand_m)
                                                    log_v(f"[安全撤銷] 頻道「{ch[:8]}...」發生快取活動，但開啟之目標「{prov_target}」視窗已轉入其他訊息且閒置無傳入圖片，判定為先前錯綁，已即刻自動撤銷解除綁定！")
                                                    continue
                                                except Exception as e:
                                                    log_v(f"[警告] 撤銷綁定寫入磁碟失敗: {e}")
                                                    # Fail-Closed 核心防禦：磁碟寫入即使失敗，記憶體與安全防護立即生效，標記處理並跳過該圖片絕不回覆錯誤對象
                                                    chat_dirs_map.pop(ch, None)
                                                    _PROVISIONAL_BINDINGS.pop(ch, None)
                                                    PROCESSED_CACHE_FILES.add((cand_path, cand_m))
                                                    max_processed_m = max(max_processed_m, cand_m)
                                                    continue
                                            else:
                                                # 灰區 Fail-Closed：既未晉升亦未撤銷之暫定頻道絕不放行回覆
                                                changed_at = w_state.get("changed_at", 0) if w_state else 0
                                                if (abs(cand_m - changed_at) <= AUTOBIND_TIME_WINDOW
                                                        and now_chk - changed_at <= AUTOBIND_TIMEOUT):
                                                    # 縮圖渲染期間：暫緩，留待下輪重新核驗
                                                    deferred_mtimes.append(cand_m)
                                                else:
                                                    PROCESSED_CACHE_FILES.add((cand_path, cand_m))
                                                    max_processed_m = max(max_processed_m, cand_m)
                                                continue
                            if not promoted:
                                # Fail-Closed 保底：暫定頻道未經晉升核驗 (6 秒範圍內的後續檔、視窗缺失/最小化、
                                # 指紋讀取失敗、晉升寫入磁碟失敗) 一律不放行；檔案尚新則暫緩重驗，逾時才標記已處理
                                if cand_m >= bind_time + AUTOBIND_TIME_WINDOW and now_chk - cand_m <= AUTOBIND_TIMEOUT:
                                    deferred_mtimes.append(cand_m)
                                else:
                                    PROCESSED_CACHE_FILES.add((cand_path, cand_m))
                                    max_processed_m = max(max_processed_m, cand_m)
                                continue
                    else:
                        cand_target = try_auto_bind_channel(ch, cand_path, cand_m, targets, chat_dirs_map, competing_count)

                    if cand_target and cand_target in targets:
                        matched = find_target_chat_window(cand_target)
                        if not matched and _worth_opening_window(cand_target, config, current_time, cooldown) and open_missing_chat_window(cand_target, config):
                            matched = find_target_chat_window(cand_target)
                        if matched:
                            # 電腦版獨立視窗標題正常為好友名稱 (不帶括號未讀數)，視窗上線即刻確認
                            chosen_candidate = cand_path
                            chosen_target = cand_target
                            PROCESSED_CACHE_FILES.add((cand_path, cand_m))
                            max_processed_m = max(max_processed_m, cand_m)
                            break
                        else:
                            PROCESSED_CACHE_FILES.add((cand_path, cand_m))
                            max_processed_m = max(max_processed_m, cand_m)
                            log_v(f"[安全隔離] 快取頻道{channel_label(ch)}對應「{cand_target}」，但桌面無該好友專屬視窗，安全略過。")
                    else:
                        # 中度 #2 防禦：若為尚未綁定之頻道，暫緩加入 PROCESSED_CACHE_FILES，並納入 deferred_mtimes 防吞
                        if ch and ch not in chat_dirs_map:
                            file_age = current_time - cand_m
                            if file_age <= AUTOBIND_TIMEOUT:
                                view_bind_wanted = True
                                deferred_mtimes.append(cand_m)
                                log_v(f"[安全暫緩] 快取頻道{channel_label(ch)}暫未匹配視窗異動 (保留重試機會，暫不標記為已處理)。")
                            else:
                                PROCESSED_CACHE_FILES.add((cand_path, cand_m))
                                max_processed_m = max(max_processed_m, cand_m)
                                log_v(f"[安全隔離] 快取頻道{channel_label(ch)}超逾 15 秒未通過目標綁定核驗，標記略過。")
                        else:
                            PROCESSED_CACHE_FILES.add((cand_path, cand_m))
                            max_processed_m = max(max_processed_m, cand_m)

                if len(PROCESSED_CACHE_FILES) > 1000:
                    # 依 mtime 保留較新的 500 筆，避免全部清空導致同 mtime 檔案重覆處理
                    sorted_items = sorted(PROCESSED_CACHE_FILES, key=lambda x: x[1])
                    PROCESSED_CACHE_FILES = set(sorted_items[500:])

                if deferred_mtimes:
                    # 暫緩檔防吞保護：max_processed_m 絕不可越過最舊的暫緩檔
                    oldest_deferred = min(deferred_mtimes)
                    last_seen_cache_mtime = min(max_processed_m, oldest_deferred - 0.001)
                else:
                    last_seen_cache_mtime = max_processed_m

                # 未綁定的頻道傳來新圖、現有的自動綁定又認不出是誰：自己開視窗、點照片確認後綁定 (觸發的那張圖若綁定成功時還沒過期，下一輪照一般核驗仍可能回覆；來不及的就略過)
                if view_bind_wanted and not chosen_target and config.get("auto_bind_by_viewing", True):
                    try:
                        auto_bind_by_viewing(config, targets, chat_dirs_map, since=min(deferred_mtimes) if deferred_mtimes else current_time)
                    except Exception as e:
                        log_v(f"[警告] 自動綁定發生例外: {e}")

        # 快取掃描命中合格新媒體，在此統籌觸發發送！
        if chosen_target and chosen_candidate:
            last_sent_to_cand = LAST_SENT_TIME_BY_TARGET.get(chosen_target, 0)
            eff_cooldown = config.get("group_cooldown_seconds", DEFAULT_CONFIG["group_cooldown_seconds"]) if is_group_target(chosen_target) else cooldown
            if current_time - last_sent_to_cand < 5:
                last_reply_times[chosen_target] = current_time
                log_v(f"[防自循環保護]「{chosen_target}」在發送後 5 秒內偵測到本機寫入快取，安全略過。")
            elif current_time - last_reply_times.get(chosen_target, 0) >= eff_cooldown:
                target_to_reply = chosen_target
                new_image_found = chosen_candidate
                log_v(f"\n[即時偵測] 鎖定快取頻道{channel_label(extract_chat_hash_from_path(new_image_found))}收到新貼圖/照片！(檔案: {os.path.basename(new_image_found)})")
            else:
                remain = int(eff_cooldown - (current_time - last_reply_times.get(chosen_target, 0)))
                log_v(f"[冷卻中]「{chosen_target}」距上次回覆未滿 {int(eff_cooldown)} 秒 (剩餘 {remain} 秒)，本次新圖不回覆。")

    # 3. 執行發送前的斷路器 (Circuit Breaker) 與發送
    if target_to_reply:
        # 斷路器檢查：5 分鐘內最多 2 次，超標則啟動 30 分鐘防禦退避鎖定
        if not check_circuit_breaker(target_to_reply, max_replies=2, window_seconds=300, lockout_seconds=1800):
            last_reply_times[target_to_reply] = current_time + 300
            # 推進該目標之未讀基準線，防止退避期間或鎖定解除後對同一筆未讀訊息無窮迴圈重試
            matched_cb = find_target_chat_window(target_to_reply)
            if matched_cb:
                m_cb = re.search(r"[\(\uFF08](\d+\+?)[\)\uFF09]\s*$", matched_cb[0][1])
                if m_cb:
                    LAST_PROCESSED_UNREAD[target_to_reply] = parse_unread_count(m_cb.group(1))
            return

        log_v(f" -> 經安全驗證確認，觸發自動回覆長輩圖給:「{target_to_reply}」")
        prev_reply_time = last_reply_times.get(target_to_reply, 0)
        send_result = send_grandma_image(target_to_reply, config, triggered_file=new_image_found, text_trigger=text_trigger)
        # 對方只傳文字而略過時沒有送出任何東西，不啟動冷卻 (避免擋住隨後真正的新圖)
        last_reply_times[target_to_reply] = prev_reply_time if send_result in SKIPPED_RESULTS else time.time()
        if text_trigger and send_result != "NOT_SENT":
            text_window_rebaseline(target_to_reply)          # 沒送出 (NOT_SENT) 就保留舊簽章，下一輪再試

        if send_result in ("SENT_CONFIRMED", "SENT_UNCERTAIN") + SKIPPED_RESULTS:
            # 為防重複刷屏一律視為已處理並更新基準線 (SKIPPED_TEXT：對方只傳文字，同樣標記已處理且不重試)
            if new_image_found:
                try:
                    m_f = os.path.getmtime(new_image_found)
                    PROCESSED_CACHE_FILES.add((new_image_found, m_f))
                    last_seen_cache_mtime = max(last_seen_cache_mtime, m_f)
                except Exception:
                    pass
            SEND_FAIL_COUNTS[target_to_reply] = 0
            PENDING_RETRY_MEDIA.pop(target_to_reply, None)
            matched_after = find_target_chat_window(target_to_reply)
            if matched_after:
                m_after = re.search(r"[\(\uFF08](\d+\+?)[\)\uFF09]\s*$", matched_after[0][1])
                if m_after:
                    LAST_PROCESSED_UNREAD[target_to_reply] = parse_unread_count(m_after.group(1))
                else:
                    LAST_PROCESSED_UNREAD[target_to_reply] = 0
            else:
                LAST_PROCESSED_UNREAD[target_to_reply] = 0
            save_circuit_breaker_state()
        elif send_result == "NOT_SENT":
            # 真正未送出 (Enter 前視窗失焦或安全攔截)：支援最多重試 3 次
            current_fails = SEND_FAIL_COUNTS.get(target_to_reply, 0) + 1
            SEND_FAIL_COUNTS[target_to_reply] = current_fails
            if new_image_found:
                PENDING_RETRY_MEDIA[target_to_reply] = new_image_found

            if current_fails >= 3:
                log_v(f"⚠️ [重試達上限]「{target_to_reply}」連續發送失敗達 3 次，更新基準線以防無窮迴圈。")
                PENDING_RETRY_MEDIA.pop(target_to_reply, None)
                if new_image_found:
                    try:
                        m_f = os.path.getmtime(new_image_found)
                        PROCESSED_CACHE_FILES.add((new_image_found, m_f))
                        last_seen_cache_mtime = max(last_seen_cache_mtime, m_f)
                    except Exception:
                        pass
                matched_after = find_target_chat_window(target_to_reply)
                if matched_after:
                    m_after = re.search(r"[\(\uFF08](\d+\+?)[\)\uFF09]\s*$", matched_after[0][1])
                    if m_after:
                        LAST_PROCESSED_UNREAD[target_to_reply] = parse_unread_count(m_after.group(1))
                    else:
                        LAST_PROCESSED_UNREAD[target_to_reply] = 0
                else:
                    LAST_PROCESSED_UNREAD[target_to_reply] = 0
                save_circuit_breaker_state()
                SEND_FAIL_COUNTS[target_to_reply] = 0
            else:
                log_v(f"[暫時失敗重試]「{target_to_reply}」第 {current_fails} 次發送失敗，保留未讀水位將於冷卻後重試。")
        return

def bind_channel_manually(name, hash_prefix, targets):
    """
    手動綁定：以快取頻道資料夾 Hash 前綴直接建立永久綁定 (不需傳送或接收任何訊息)。
    Fail-Closed：名稱須在監控名單內、前綴須唯一對應快取資料夾、頻道與名稱皆不可已綁定他人。
    回傳 (成功與否, 訊息)。
    """
    prefix = (hash_prefix or "").strip().lower()
    if len(prefix) < 8 or not re.fullmatch(r"[0-9a-f]+", prefix):
        return False, "Hash 前綴須為至少 8 碼的 16 進位字元。"
    canon = next((t for t in targets if t.lower() == name.strip().lower()), None)
    if canon is None:
        return False, f"「{name}」不在 config.json 的 targets 名單內，請先加入。"
    cache_dir = get_active_line_cache_dir()
    if not cache_dir or not os.path.isdir(cache_dir):
        return False, "找不到 LINE 本機快取目錄。"
    matches = [
        d for d in os.listdir(cache_dir)
        if re.fullmatch(r"[0-9a-fA-F]{40}", d) and d.lower().startswith(prefix)
        and os.path.isdir(os.path.join(cache_dir, d))
    ]
    if len(matches) != 1:
        return False, f"前綴「{prefix}」對應到 {len(matches)} 個快取資料夾，必須剛好 1 個。"
    ch = matches[0]
    with locked_chat_dirs() as disk_map:
        existing = disk_map.get(ch)
        if isinstance(existing, str):
            if existing.lower() == canon.lower():
                return True, f"頻道 {ch[:8]}... 已綁定「{existing}」，無需變更。"
            return False, f"頻道 {ch[:8]}... 已綁定給「{existing}」，請先用 --unbind 解除。"
        for k, v in disk_map.items():
            if k != "_provisional" and isinstance(v, str) and v.lower() == canon.lower():
                return False, f"「{canon}」已綁定頻道 {k[:8]}...，請先用 --unbind 解除。"
        disk_map[ch] = canon
        if isinstance(disk_map.get("_provisional"), dict):
            disk_map["_provisional"].pop(ch, None)
    return True, f"已手動綁定: {ch[:8]}... <-> {canon}"

def foreground_window_title():
    """目前前景視窗標題 (取不到回傳空字串)"""
    try:
        return win32gui.GetWindowText(win32gui.GetForegroundWindow()) or ""
    except Exception:
        return ""

def bind_channel_by_viewing(name, targets, seconds=30):
    """
    觀看綁定：使用者在 LINE 開啟該好友聊天室並點開任一張舊圖片，期間只有一個快取頻道有寫入時，
    即視為該好友的頻道並綁定 (不需傳送或接收訊息)。
    Fail-Closed：0 個或多於 1 個頻道有寫入一律中止；快取寫入的當下，前景視窗標題必須等於目標名稱
    (排除群組與其他聊天室背景自動下載縮圖造成的誤綁)，否則中止。回傳 (成功與否, 訊息)。
    """
    cache_dir = get_active_line_cache_dir()
    if not cache_dir or not os.path.isdir(cache_dir):
        return False, "找不到 LINE 本機快取目錄。"
    before = get_all_cache_files(cache_dir)
    changed = set()
    unverified = False
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(0.5)
        cur = get_all_cache_files(cache_dir)
        for p, meta in cur.items():
            if before.get(p) != meta:
                ch = extract_chat_hash_from_path(p)
                if ch:
                    changed.add(ch)
                    if not title_matches_target(foreground_window_title(), name):
                        unverified = True
        before = cur
    if unverified:
        return False, f"快取寫入當下，前景視窗不是「{name}」的聊天室，無法確認頻道歸屬，已中止。請把該聊天室放在最上層後重試。"
    if len(changed) != 1:
        return False, f"觀察期間有 {len(changed)} 個頻道有快取寫入，必須剛好 1 個。請只開啟目標好友的聊天室並點開一張舊圖片後重試。"
    return bind_channel_manually(name, next(iter(changed)), targets)

VIEW_BIND_RETRY_SECONDS = 600.0     # 同一個對象自動綁定失敗後，至少隔多久才再試 (避免一直開他的視窗、點他的圖)；連續失敗每次加倍
VIEW_BIND_RETRY_MAX_SECONDS = 6 * 3600.0
VIEW_BIND_MAX_PER_SWEEP = 3         # 一次觸發最多試幾位對象 (其餘等下次，已經開著視窗的優先)
VIEW_BIND_SCROLL_NOTCHES = 5        # 找照片時每次滑鼠滾輪捲幾格 (約半頁到一頁)
VIEW_BIND_QUIET_AFTER = 3.0        # 自動綁定結束後這幾秒內的快取寫入也算是我們點圖造成的
_VIEW_BIND_LAST_TRY = {}            # {對象 (小寫): 上次嘗試時間}
_VIEW_BIND_FAILS = {}               # {對象 (小寫): 連續失敗次數}
_VIEW_BIND_WINDOWS = collections.deque(maxlen=5)     # [(開始, 結束)] 自動綁定期間：這段時間的快取寫入是我們自己點圖造成的，不能當成對方傳來的新圖


def _in_view_bind_window(mtime):
    """這個快取檔的寫入時間，是不是落在自動綁定 (點圖) 期間"""
    return any(start <= mtime <= end for start, end in _VIEW_BIND_WINDOWS)


VIEW_BIND_PHOTO_MIN_HEIGHT = 80     # 邏輯像素：比這矮的區塊不當成照片 (文字氣泡、貼圖、連結列)
VIEW_BIND_PHOTO_MIN_WIDTH = 100
VIEW_BIND_FLAT_MAX_SHARE = 0.4      # 區塊裡同一種顏色 (量化後) 的像素超過這個比例，就是純色氣泡 (灰色對方氣泡、綠色自己的氣泡)，不是照片


def _flat_share(sample):
    """取樣像素裡最大宗那種顏色 (每個色頻量化成 32 階) 佔的比例：文字氣泡是單一底色，照片不會有這麼大的一塊同色"""
    counts = {}
    for px in sample:
        key = (px[0] >> 3, px[1] >> 3, px[2] >> 3)
        counts[key] = counts.get(key, 0) + 1
    return max(counts.values()) / len(sample)


def _history_photo_centers(chat_hwnd, side="left", limit=6):
    """
    依截圖找出聊天室歷史區看得到的照片 (大的、不是文字氣泡灰底的區塊) 的螢幕座標中心，由下往上 (最新的在前)。
    LINE 新版不再把訊息內容暴露給 UIA (每則訊息底下都沒有子控制項)，所以只能靠截圖。
    貼圖也可能被當成照片挑出來，沒關係：點了沒有快取寫入就換下一張，綁定只看快取有沒有寫入。
    """
    found = _left_message_blocks(chat_hwnd, side=side, max_blocks=12)
    if found is None:
        return []
    shot, scale, x_start, _x_end, is_content, blocks = found
    rect = get_window_true_rect(chat_hwnd)
    origin_x, origin_y = rect[0], rect[1] + int((rect[3] - rect[1]) * 0.18)    # 與 _left_message_blocks 的截圖範圍相同
    scan_from, scan_to = (x_start, shot.width - int(12 * scale)) if side == "left" else (int(shot.width * 0.1), shot.width - int(12 * scale))
    centers = []
    for bottom, top in blocks:
        if int((bottom - top + 1) / scale) < VIEW_BIND_PHOTO_MIN_HEIGHT:
            continue                            # 太矮 (被截圖範圍切到時只看露出的部分夠不夠大：照片常比歷史區還高，點露出的部分就好)
        ys = range(top, bottom + 1, 8)
        xs = [x for x in range(scan_from, scan_to, 4) if any(is_content(shot.getpixel((x, y))) for y in ys)]
        if not xs or int((max(xs) - min(xs) + 1) / scale) < VIEW_BIND_PHOTO_MIN_WIDTH:
            continue
        sample = [shot.getpixel((x, y)) for x in range(min(xs), max(xs) + 1, 6) for y in range(top, bottom + 1, 6)]
        if _flat_share(sample) > VIEW_BIND_FLAT_MAX_SHARE:
            continue                            # 純色的文字氣泡
        centers.append((origin_x + (min(xs) + max(xs)) // 2, origin_y + (top + bottom) // 2))
        if len(centers) >= limit:
            break
    return centers


def _scroll_chat(hwnd, notches):
    """把滑鼠移到訊息列表中央，滾輪捲 notches 格 (正數往上、負數往下)；列表找不到或中央被別的視窗蓋住就不捲，回傳有沒有捲"""
    try:
        import uiautomation as auto
        win = auto.ControlFromHandle(hwnd)
        view = win.ListControl(ClassName="LcListView") if win and win.Exists(0, 0) else None
        if not view or not view.Exists(0, 0):
            return False
        vr = view.BoundingRectangle
        cx, cy = (vr.left + vr.right) // 2, (vr.top + vr.bottom) // 2
        if _root_window_at(cx, cy) != hwnd:
            return False
        pyautogui.moveTo(cx, cy)
        pyautogui.scroll(120 * notches)             # Windows 的滾輪單位：一格 = 120
        return True
    except Exception:
        return False


SCROLL_TO_BOTTOM_NOTCHES = 20       # 捲到最下面：每輪往下捲幾格
SCROLL_TO_BOTTOM_MAX_ROUNDS = 12    # 最多捲幾輪 (歷史再長也到得了；正常到底了就會提早停)


def _scroll_chat_to_bottom(hwnd, was_viewing):
    """
    讀最底部那一則之前，先把聊天視窗捲到最下面 (視窗若停在你捲過的舊位置，讀到的會是舊訊息)。
    **只對你沒在看的視窗做**：was_viewing 要在「我們把視窗帶到前景之前」量好 (_user_is_viewing)，你正在看就完全不動。
    LINE 的捲動有動畫、歷史長短不一，所以不捲固定的格數：一輪一輪往下捲，每輪等畫面穩定，捲了畫面也不再變就是到底了。
    """
    if was_viewing:
        return
    previous = None
    for round_no in range(SCROLL_TO_BOTTOM_MAX_ROUNDS):
        if not _scroll_chat(hwnd, -SCROLL_TO_BOTTOM_NOTCHES):
            if round_no == 0:
                log_v(" -> [捲到最下面] 找不到訊息列表或列表被別的視窗蓋住，沒有捲動，照目前畫面讀取。")
            break
        settled = _wait_until_chat_settled(hwnd)
        try:
            current = _chat_signature(hwnd)
        except Exception:
            break
        if settled and previous is not None and ImageStat.Stat(ImageChops.difference(previous, current)).mean[0] < CHAT_SETTLE_TOLERANCE:
            break                               # 又捲了一輪、畫面穩定而且沒動：已經在最底部 (畫面還在動就不算)
        previous = current


class _ViewBindIo:
    """把 view_binder 要用的動作接到實際的 LINE 視窗與快取目錄"""

    def __init__(self, name, config, targets):
        self.name = name
        self.config = config
        self.targets = targets

    def find_window(self):
        wins = find_target_chat_window(self.name)
        return wins[0][0] if wins else None

    def open_window(self):
        return open_missing_chat_window(self.name, self.config)

    def bring_to_front(self, hwnd):
        return activate_window(hwnd)

    def foreground_matches(self):
        return title_matches_target(foreground_window_title(), self.name)

    def photo_centers(self, hwnd):
        """視窗裡看得到的照片的中心點：對方傳的 (左側) 優先，再來是自己傳的 (右側)；各自由新到舊"""
        try:
            centers = _history_photo_centers(hwnd, "left")
            if len(centers) < view_binder.MAX_CLICKS:       # 左側 (對方傳的) 不夠試才再掃右側 (自己傳的)，省一次截圖與逐點掃描
                centers += _history_photo_centers(hwnd, "right")
            return centers
        except Exception:
            return []

    def scroll(self, hwnd, up):
        """捲一頁 (找照片用)；列表找不到或中央被別的視窗蓋住就不捲"""
        return _scroll_chat(hwnd, VIEW_BIND_SCROLL_NOTCHES if up else -VIEW_BIND_SCROLL_NOTCHES)

    def click(self, hwnd, x, y):
        if _root_window_at(x, y) != hwnd:
            return False                    # 點的位置被別的視窗蓋住：不點
        pyautogui.click(x, y)
        return True

    def close_viewer(self):
        pyautogui.press("esc")

    def snapshot(self):
        return get_all_cache_files()

    def channel_of(self, path):
        return extract_chat_hash_from_path(path)

    def bind(self, ch):
        return bind_channel_manually(self.name, ch, self.targets)

    def finish(self, hwnd, opened_by_us):
        if opened_by_us:
            _finish_with_window(hwnd, self.config, self.name)


def _view_bind_wait(name):
    """這位對象自動綁定失敗後要隔多久才再試：連續失敗每次加倍，最多 VIEW_BIND_RETRY_MAX_SECONDS"""
    return min(VIEW_BIND_RETRY_SECONDS * 2 ** _VIEW_BIND_FAILS.get(name.lower(), 0), VIEW_BIND_RETRY_MAX_SECONDS)


def _recently_active_targets(targets, since):
    """
    剛有新照片的對象：視窗內容在 since (新照片寫入快取的時間) 前後 AUTOBIND_TIMEOUT 秒內有變動 (_TARGET_WINDOW_STATE)。
    視窗沒開的人沒有任何跡象說他剛傳了東西。(標題的未讀數不能當依據：主迴圈在這之前已經把它記成已處理。)
    """
    changed = {t: (_TARGET_WINDOW_STATE.get(t) or {}).get("changed_at", 0.0) for t in targets}
    return [t for t, at in changed.items() if at and abs(at - since) <= AUTOBIND_TIMEOUT]


def auto_bind_by_viewing(config, targets, chat_dirs_map, since=None):
    """
    有未綁定的快取頻道傳來新圖，但現有的自動綁定認不出是誰：依序替「還沒綁定的對象」開視窗、點一張照片、確認快取頻道後綁定
    (一對一與群組都適用，核驗規則見 view_binder)。每位對象失敗後隔 VIEW_BIND_RETRY_SECONDS 才再試，一次最多 VIEW_BIND_MAX_PER_SWEEP 位。
    期間自己點圖造成的快取寫入標成已處理，不會被當成對方傳來的新圖而回覆。
    since (新照片寫入快取的時間) 有給時，只試剛有新照片跡象的對象 (見 _recently_active_targets)，沒有跡象的對象不去開窗搜尋。
    """
    bound_names = {v.strip().lower() for v in chat_dirs_map.values() if isinstance(v, str)}
    now = time.time()
    if since is not None:
        active = _recently_active_targets(targets, since)
        if not active:
            log_v("[自動綁定] 沒有任何對象的視窗在這張新圖前後有變動，無從判斷是誰傳的，這次不開窗搜尋。")
            return
        targets = active
    pending = [t for t in targets
               if t.strip().lower() not in bound_names and now - _VIEW_BIND_LAST_TRY.get(t.lower(), 0) >= _view_bind_wait(t)]
    pending.sort(key=lambda t: 0 if find_target_chat_window(t) else 1)       # 視窗已經開著的優先
    for name in pending[:VIEW_BIND_MAX_PER_SWEEP]:
        _VIEW_BIND_LAST_TRY[name.lower()] = time.time()
        log_v(f"[自動綁定] 有未綁定的頻道傳來新圖，嘗試開啟「{name}」的聊天視窗、點開一張照片來確認它的頻道...")
        start = time.time() - 1.0
        ok, msg, touched = view_binder.bind_by_opening(name, _ViewBindIo(name, config, targets), log=log_v)
        _VIEW_BIND_WINDOWS.append((start, time.time() + VIEW_BIND_QUIET_AFTER))
        for path, meta in touched.items():
            PROCESSED_CACHE_FILES.add((path, meta[0]))
        log_v(f"[自動綁定] {'成功' if ok else '未綁定'}：{msg}")
        _VIEW_BIND_FAILS[name.lower()] = 0 if ok else _VIEW_BIND_FAILS.get(name.lower(), 0) + 1
        if ok:
            break


def init_unread_baselines(specific_targets=None):
    """啟動時先掃描所有 LINE 視窗目前的未讀數作為初始基準線，避免啟動時將歷史未讀訊息誤判為新訊息"""
    for hwnd, title, cls, vis in get_all_line_windows():
        m = re.search(r"^(.*?)\s*[\(\uFF08](\d+\+?)[\)\uFF09]\s*$", title)
        if m:
            name = m.group(1).strip()
            if specific_targets is not None and not any(name.lower() == t.lower() for t in specific_targets):
                continue
            if is_group_target(name):
                continue                                # 群組標題的數字是成員數
            count = parse_unread_count(m.group(2))
            LAST_PROCESSED_UNREAD[name] = max(LAST_PROCESSED_UNREAD.get(name, 0), count)
            log_v(f"[基準線初始化]「{name}」初始未讀數記為 {count}，避免啟動誤觸發。")

def main():
    parser = argparse.ArgumentParser(description=f"LINE 自動回覆長輩圖工具 v{VERSION}")
    parser.add_argument("-v", "--version", action="version", version=f"LINE Grandma Bot v{VERSION}")
    parser.add_argument("--test", metavar="NAME", help="手動測試發送一次給指定對象 (例如: --test 媽媽)")
    parser.add_argument("--calibrate", metavar="NAME", help="校準指定聯絡人的快取頻道 (例如: --calibrate 媽媽)")
    parser.add_argument("--tag-image", nargs=2, metavar=("FILE", "SLOT"), help="手動標記圖片時段：morning、noon、evening 或 neutral (無時段字樣，任何時段皆可用)")
    parser.add_argument("--fill-pool", nargs="?", const=30, type=int, metavar="TOTAL", help="把圖庫補到指定張數 (預設 30)：已標記 neutral 的圖先計入，其餘名額由早安、午安、晚安平均分配")
    parser.add_argument("--crawl", type=int, metavar="COUNT", help="立刻上網抓取指定數量的長輩圖儲存備用 (例如: --crawl 20)")
    parser.add_argument("--stop-after", type=int, metavar="N", help="回覆 N 則圖片或訊息後自動關閉機器人並讓電腦睡眠 (0 = 不啟用；沒給時在主控台詢問，直接 Enter = 1，沒有主控台就不啟用)")
    parser.add_argument("--list-images", action="store_true", help="列出目前讀取到的長輩圖清單")
    parser.add_argument("--clear-history", action="store_true", help="清空已發送長輩圖的歷史記錄")
    parser.add_argument("--scan-windows", action="store_true", help="掃描並印出目前桌面所有的 LINE 視窗資訊")
    parser.add_argument("--unbind", metavar="NAME_OR_HASH", help="解除指定聯絡人或雜湊之頻道綁定記錄")
    parser.add_argument("--bind-view", metavar="NAME", help="觀看綁定：開啟該好友聊天室並點開一張舊圖片，自動辨識並綁定頻道 (最簡單，不需傳送或接收訊息)")
    parser.add_argument("--bind", nargs=2, metavar=("NAME", "HASH_PREFIX"), help="手動綁定聯絡人與快取頻道資料夾 Hash 前綴 (不需傳送或接收訊息)")
    args = parser.parse_args()

    if args.unbind:
        token = args.unbind.strip()
        removed = []
        try:
            with locked_chat_dirs() as disk_map:
                for k in list(disk_map.keys()):
                    if k == "_provisional":
                        continue
                    v = disk_map[k]
                    is_match = (
                        isinstance(v, str) and (
                            k.lower() == token.lower()
                            or (len(token) >= 6 and k.lower().startswith(token.lower()))
                            or v.lower() == token.lower()
                        )
                    )
                    if is_match:
                        removed.append((k, v))
                        disk_map.pop(k, None)
                        if isinstance(disk_map.get("_provisional"), dict):
                            disk_map["_provisional"].pop(k, None)
                        _PROVISIONAL_BINDINGS.pop(k, None)
            if removed:
                for k, v in removed:
                    log_v(f"已成功解除綁定: {k[:8]}... <-> {v}")
                sys.exit(0)
            else:
                log_v(f"找不到符合「{token}」的頻道綁定記錄。")
                sys.exit(0)
        except Exception as e:
            log_v(f"解除綁定失敗: {e}")
            sys.exit(1)

    if args.bind_view:
        log_v(f"請在 LINE 開啟「{args.bind_view}」的聊天室，並點開任意一張舊圖片 (30 秒內，其他聊天室請勿操作)...")
        try:
            ok, msg = bind_channel_by_viewing(args.bind_view, load_config().get("targets", []))
        except Exception as e:
            ok, msg = False, f"觀看綁定失敗: {e}"
        log_v(msg if ok else f"[綁定中止] {msg}")
        sys.exit(0 if ok else 1)

    if args.bind:
        try:
            ok, msg = bind_channel_manually(args.bind[0], args.bind[1], load_config().get("targets", []))
        except Exception as e:
            ok, msg = False, f"手動綁定失敗: {e}"
        log_v(msg if ok else f"[綁定中止] {msg}")
        sys.exit(0 if ok else 1)

    # 唯讀或診斷查詢指令 (如 --list-images, --scan-windows, --unbind) 不搶佔單一執行個體互斥鎖
    if not (args.list_images or args.scan_windows):
        ensure_single_instance()

    config = load_config()
    configure_image_options(config)

    if args.clear_history:
        log_v("=== 清除歷史發送紀錄 ===")
        em_path = HISTORY_PATH + ".emergency_copy"
        for p in (HISTORY_PATH, em_path):
            if os.path.exists(p):
                try:
                    os.remove(p)
                    log_v(f"已成功清除 {os.path.basename(p)}")
                except Exception as e:
                    log_v(f"[警告] 清除 {p} 失敗: {e}")
        _LAST_VALID_CONTACT_HISTORY.clear()
        log_v("所有聯絡人之百輪發送紀錄與應急複本已徹底重置。")
        return

    if args.tag_image:
        # 只接受圖庫資料夾內的檔名 (不含路徑)，避免指向圖庫以外的檔案
        name = args.tag_image[0]
        path = os.path.join(SCRIPT_DIR, config.get("images_dir", "images"), os.path.basename(name))
        if os.path.basename(name) != name:
            log_v("[標記失敗] 請只輸入圖庫資料夾內的檔名，不要包含路徑。")
            sys.exit(1)
        old_slots = classify_image_slots(path) if os.path.isfile(path) else None
        if old_slots and old_slots != [args.tag_image[1]]:
            log_v(f"[提醒] 此圖原本標記為 {old_slots}，將改為 {args.tag_image[1]}。")
        ok = os.path.isfile(path) and set_image_slots(path, [args.tag_image[1]])
        log_v(f"已標記 {os.path.basename(path)} 為 {args.tag_image[1]}" if ok else "[標記失敗] 檔案不存在或時段名稱不正確 (morning/noon/evening/neutral)。")
        sys.exit(0 if ok else 1)

    if args.fill_pool is not None:
        fill_image_pool(config.get("images_dir", "images"), total=max(1, args.fill_pool))
        return

    if args.crawl:
        count = args.crawl
        target_dir = os.path.join(SCRIPT_DIR, config.get("images_dir", "images"))
        crawl_images_now(count, target_dir)
        return

    if args.list_images:
        images = get_images(config.get("images_dir", "images"), auto_replenish=False)
        history = load_contact_history()
        log_v(f"目前在 [{config.get('images_dir', 'images')}] 共有 {len(images)} 張圖片:")
        for idx, img in enumerate(images, 1):
            name = os.path.basename(img)
            m = get_file_md5(img)
            seen_contacts = []
            for contact, data in history.items():
                if m in data.get("sent", []) or m in data.get("received", []):
                    seen_contacts.append(contact)
            status = f"(已在此人百輪歷史中: {', '.join(seen_contacts)})" if seen_contacts else "(全新未用)"
            print(f"  {idx}. {name} {status}")
        return

    if args.scan_windows:
        log_v("=== 正在掃描桌面上的 LINE 視窗 ===")
        wins = get_all_line_windows()
        if not wins:
            log_v("目前桌面上沒有偵測到任何 LINE 視窗，請確認 LINE 是否正在執行。")
        for h, t, c, v in wins:
            print(f"HWND: {h} | Title: {t!r} | Class: {c!r} | Visible: {v}")
        return

    if args.calibrate:
        target = args.calibrate
        log_v(f"=== 頻道配對校準：為「{target}」進行精確快取頻道配對 ===")
        log_v(f"請在 15 秒內，在 LINE 電腦版「{target}」的對話室中發送一張任意貼圖或照片...")

        win_candidates = find_target_chat_window(target)
        if not win_candidates:
            log_v(f"❌ [校準中止] 尚未偵測到「{target}」的獨立對話視窗！請先在 LINE 好友名單連點兩下開啟「{target}」的獨立視窗後再執行校準。")
            return

        active_c = get_active_line_cache_dir()
        if not active_c:
            log_v("❌ [錯誤] 找不到 LINE 本機快取目錄，請確認 LINE 電腦版是否正在執行。")
            return

        before_f = get_all_cache_files(active_c)
        prev_f = before_f.copy()
        new_hashes = set()
        calib_start_time = time.time()
        deadline = calib_start_time + 15
        log_v("正在持續監聽快取變更 (15 秒完整觀測窗)...")

        target_hwnds = {w[0] for w in win_candidates}
        primary_target_hwnd = list(target_hwnds)[0]
        initial_chat_img = get_chat_history_image(primary_target_hwnd)
        verified_target_hashes = set()

        while time.time() < deadline:
            time.sleep(0.5)
            fg_start = win32gui.GetForegroundWindow()
            current_f = get_all_cache_files(active_c)
            fg_end = win32gui.GetForegroundWindow()
            # 增量連續焦點核驗：只有輪詢起點與終點均持續處於目標視窗前景焦點時，才給予雙重確認
            continuous_target_fg = (fg_start in target_hwnds and fg_end in target_hwnds)
            for p, meta in current_f.items():
                if p not in prev_f or prev_f[p] != meta:
                    ch = extract_chat_hash_from_path(p)
                    if ch:
                        new_hashes.add(ch)
                        # 只有當建立快取檔案時目標視窗持續為前景焦點，且檔案修改時間晚於校準開始時才列入候選
                        if continuous_target_fg and meta[0] >= calib_start_time - 1.0:
                            verified_target_hashes.add(ch)
            prev_f = current_f
            if len(new_hashes) > 1:
                break

        if len(verified_target_hashes) == 1 and len(new_hashes) == 1:
            matched_hash = list(verified_target_hashes)[0]
            target_win_hwnd = fg_end if (fg_end in target_hwnds) else list(target_hwnds)[0]
            final_chat_img = get_chat_history_image(target_win_hwnd)

            # 影像歷史比對：驗證目標視窗訊息區域確實產生了本機發送的新像素變更 (右側氣泡)
            diff_right_count = 0
            diff_left_count = 0
            if initial_chat_img and final_chat_img and initial_chat_img.size == final_chat_img.size:
                w, h = initial_chat_img.size
                b1 = initial_chat_img.tobytes()
                b2 = final_chat_img.tobytes()
                # 掃描下半部 (最新訊息區域)
                start_y = int(h * 0.4)
                for y in range(start_y, h, 2):
                    row_offset = y * w * 3
                    for x in range(0, w, 4):
                        idx = row_offset + x * 3
                        if abs(b1[idx] - b2[idx]) + abs(b1[idx+1] - b2[idx+1]) + abs(b1[idx+2] - b2[idx+2]) > 30:
                            if x > w * 0.45:
                                diff_right_count += 1
                            else:
                                diff_left_count += 1

            # 因果嚴格核驗：聊天歷史區域必須產生足夠顯著的右側新送出氣泡 (diff_right_count >= 100)
            # 若右側無足夠變更或左側變更佔主導，判定非本機於目標視窗發送，嚴格拒絕 (Fail-Closed)
            if diff_right_count < 100 or diff_left_count > diff_right_count:
                log_v(f"⚠️ [校準中止] 偵測到快取異動，但未能證實「{target}」聊天室畫面有本機送出的新訊息氣泡 (右側像素變更: {diff_right_count}, 左側: {diff_left_count})！")
                log_v("快取異動疑似由背景其他好友傳訊所致，嚴格拒絕綁定以維護因果安全！")
                return

            try:
                with locked_chat_dirs() as disk_map:
                    for k in list(disk_map.keys()):
                        if k == "_provisional":
                            continue
                        if isinstance(disk_map[k], str) and disk_map[k].lower() == target.lower():
                            del disk_map[k]
                            if isinstance(disk_map.get("_provisional"), dict):
                                disk_map["_provisional"].pop(k, None)
                    disk_map[matched_hash] = target
                    if isinstance(disk_map.get("_provisional"), dict):
                        disk_map["_provisional"].pop(matched_hash, None)
                    _PROVISIONAL_BINDINGS.pop(matched_hash, None)
                log_v(f"🎉 [校準成功] 成功驗證本機訊息氣泡並將頻道「{matched_hash[:8]}...」綁定至「{target}」，已持久化儲存！")
            except Exception as e:
                log_v(f"❌ [校準失敗] 寫入 {CHAT_MAP_PATH} 失敗: {e}，無法持久化儲存頻道綁定！")
        else:
            if len(new_hashes) == 0:
                log_v(f"⚠️ [校準逾時] 15 秒內未偵測到「{target}」聊天室的新快取檔案，請確認是否有在該聊天室發送貼圖/照片後重試。")
            elif len(new_hashes) > 1:
                log_v(f"⚠️ [校準中止] 觀測窗內偵測到多個不同頻道 ({len(new_hashes)} 個) 產生快取異動！為防錯綁，安全終止！")
            else:
                log_v(f"⚠️ [校準中止] 偵測到的快取異動發生時，目標好友「{target}」的視窗並非前景焦點視窗！為防錯綁其他聯絡人，安全終止！")
        return

    if args.test:
        target = args.test
        log_v(f"=== 手動測試模式：發送長輩圖給「{target}」===")
        if not check_circuit_breaker(target, max_replies=2, window_seconds=300):
            log_v(f"❌ [斷路器鎖定]「{target}」在 5 分鐘內已達發送上限，已啟動防禦退避鎖定，拒絕發送！")
            return
        init_stop_baselines(config)
        try:
            result = send_grandma_image(target, config)
        except StopRequested as e:
            log_v(f"\n[遠端停止] {e}，程式立即結束。")
            sys.exit(STOP_EXIT_CODE)
        log_v(f"手動測試結果: {result}")
        return

    _AUTO_STOP["limit"] = resolve_stop_after(args.stop_after, sys.stdin is not None and sys.stdin.isatty())
    configure_file_log(config)        # 先打開檔案紀錄，橫幅也要寫進去
    banner_print("=" * 65)
    banner_print(f"  LINE 電腦版 - 長輩圖智慧自動回覆程式 v{VERSION} (常駐背景)")
    banner_print("=" * 65)
    banner_print(f"程式版本 (version)       : v{VERSION}")
    if _AUTO_STOP["limit"]:
        banner_print(f"自動關閉                 : 回覆 {_AUTO_STOP['limit']} 則後關閉機器人並讓電腦睡眠")
    banner_print(f"監控對象名單 (targets)   : {config.get('targets', [])}")
    t_mode = config.get('trigger_mode', 'photo_and_sticker')
    mode_desc = "照片與貼圖都會觸發偵測，貼圖要有問候字才回" if t_mode == "photo_and_sticker" else ("只回照片" if t_mode == "photo_only" else "文字照片貼圖都會觸發偵測")
    banner_print(f"觸發模式 (trigger_mode)  : {t_mode} ({mode_desc})")
    banner_print(f"隨機延遲範圍 (delay)     : {config.get('min_delay_seconds', 5)} ~ {config.get('max_delay_seconds', 900)} 秒")
    banner_print(f"每位聯絡人記憶輪數       : {config.get('history_rounds', 100)} 輪 (不重複發送或回送對方發過的圖)")
    banner_print(f"自動收納對方圖片 (借花獻佛): {config.get('collect_incoming_images', True)}")
    banner_print(f"發送後自動汰換新圖       : {config.get('replace_after_send', True)}")
    banner_print(f"舊圖保存期限 (TTL)       : {config.get('image_ttl_days', 7)} 天")
    banner_print(f"防洗版冷卻 (cooldown)    : {config.get('cooldown_seconds', 30)} 秒")
    banner_print(f"紀錄檔 (log_to_file)     : {'開 (logs/，保留 ' + str(config.get('log_keep_days', 7)) + ' 天)' if config.get('log_to_file', True) else '關'}")
    banner_print(f"文字訊息回圖 (reply_to_text): {'開 (一對一、' + str(config.get('text_reply_max_chars', 30)) + ' 字內)' if config.get('reply_to_text', True) else '關'}")
    banner_print(f"貼圖問候字 (reply_to_sticker_text): {'開 (一對一；貼圖上的字是問候或祝福才回，其餘貼圖略過)' if config.get('reply_to_sticker_text', True) else '關 (貼圖一律略過)'}")
    banner_print(f"回覆後安全最小化         : {config.get('close_window_after_send', True)}")
    stop_chats = config.get("stop_command_chats", [])
    banner_print(f"停止指令                 : {'在 ' + '、'.join(stop_chats) + ' 聊天室自己發「' + config.get('stop_command', '') + '」' if stop_chats else '未啟用'}")
    banner_print("-" * 65)
    banner_print("說明：")
    banner_print("1. 對方傳長輩圖來時，程式會隨機等待數秒，並挑選一張不同的長輩圖回傳。")
    banner_print("2. 圖片送出後自動淘汰換新，且定期清理超過期限的舊圖，保持圖庫新鮮。")
    banner_print("3. 視窗會自動最小化，維持後續未讀。")
    banner_print("4. 按下 Ctrl + C 即可隨時停止程式。")
    banner_print("=" * 65)

    # 建立目前快取基準線，確保啟動後只監聽「新傳入」的圖片，絕不觸發舊圖或剛才發送的圖
    advance_cache_mtime_to_current()
    init_unread_baselines()
    init_stop_baselines(config)
    init_target_window_fingerprints(config.get("targets", []))
    log_v("快取監聽基準線已就緒，正靜態待命中...")
    print("-" * 65)

    report_ocr_engine()
    warm_up_image_library(config)
    configure_notifier(config)
    configure_file_log(config)
    notifier.log_sink = _notifier_log_line
    notifier.install_logging_handler()
    stop_chats_desc = "、".join(config.get("stop_command_chats", [])) or "未啟用"
    notifier.notify(f"🟢 長輩圖機器人 v{VERSION} 已啟動\n監控：{'、'.join(config.get('targets', [])) or '無'}\n停止指令：{stop_chats_desc}")

    last_cleanup_time = 0
    current_targets = list(config.get("targets", []))
    current_stop_chats = list(config.get("stop_command_chats", []))

    try:
        while True:
            check_auto_stop()
            try:
                config = load_config()
                configure_notifier(config)
                configure_file_log(config)
                configure_image_options(config)

                # 熱重載偵測：若背景修改了 config.json 中的名單，即時印出提示
                new_targets = config.get("targets", [])
                if new_targets != current_targets:
                    log_v(f"\n[熱更新生效] 偵測到名單變更！新監控名單: {new_targets}")
                    # 為新加入之好友即時鎖定現存未讀基準線，防止 trigger_mode='all' 對熱重載前已存在之舊未讀誤發送
                    added_targets = [t for t in new_targets if t not in current_targets]
                    if added_targets:
                        init_unread_baselines(added_targets)
                        init_target_window_fingerprints(added_targets)
                    current_targets = list(new_targets)

                new_stop_chats = config.get("stop_command_chats", [])
                if new_stop_chats != current_stop_chats:
                    added_stop = [c for c in new_stop_chats if c not in current_stop_chats]
                    if added_stop:
                        init_stop_baselines(config, added_stop)
                    current_stop_chats = list(new_stop_chats)

                # 每 1 小時檢查一次過期長輩圖
                if time.time() - last_cleanup_time > 3600:
                    ttl = config.get("image_ttl_days", 7)
                    target_dir = os.path.join(SCRIPT_DIR, config.get("images_dir", "images"))
                    clean_expired_images(images_dir=target_dir, ttl_days=ttl)
                    last_cleanup_time = time.time()

                flush_fallback_notices(config)
                check_for_incoming_messages(config)
            except pyautogui.FailSafeException:
                logging.warning("[防護] 滑鼠移至螢幕角落觸發 pyautogui 安全保護，等待 3 秒後繼續監聽...")
                time.sleep(3)
            except Exception as e:
                logging.warning(f"[警告] 監聽迴圈發生短暫異常: {e}，2 秒後重試...")
                time.sleep(2)

            time.sleep(1)
    except KeyboardInterrupt:
        log_v("\n程式已停止。")
        notifier.notify("🔴 長輩圖機器人已停止 (Ctrl+C)")
        notifier.flush()
        flush_fallback_notices_at_exit(config)          # 推播額度用完時，「已停止」是最想看到的通知，結束前盡量送出
    except StopRequested as e:
        log_v(f"\n[遠端停止] {e}，程式立即結束。")
        notifier.notify(f"🔴 長輩圖機器人已停止：{e}")
        notifier.flush()
        flush_fallback_notices_at_exit(config)
        if isinstance(e, AutoStopReached):
            time.sleep(SLEEP_GRACE_SECONDS)
            suspend_computer()
        sys.exit(STOP_EXIT_CODE)


@contextlib.contextmanager
def keep_system_awake():
    """Keep Windows awake for this process without changing the power plan."""
    continuous = 0x80000000
    system_required = 0x00000001
    set_state = ctypes.windll.kernel32.SetThreadExecutionState
    set_state.argtypes = [ctypes.c_uint32]
    set_state.restype = ctypes.c_uint32
    if not set_state(continuous | system_required):
        message = "無法啟用防止自動休眠，機器人未啟動。"
        # 此時 main 尚未設定日誌；隱藏啟動也必須留下失敗原因。
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(os.path.join(LOG_DIR, "startup_errors.log"), "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n")
        except OSError:
            logging.exception("[電源] 無法寫入啟動失敗紀錄")
        raise OSError(message)
    try:
        log_v("[電源] 執行期間防止電腦自動休眠；螢幕仍依原設定關閉。")
        yield
    finally:
        if not set_state(continuous):
            logging.warning("[電源] 解除防止自動休眠失敗；處理程序結束後由 Windows 釋放。")


if __name__ == "__main__":
    with keep_system_awake():
        main()
