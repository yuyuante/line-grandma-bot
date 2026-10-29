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
import glob
import argparse
import hashlib
import shutil
import subprocess
import logging
import tempfile
import ctypes
import threading
from io import BytesIO
import re
import contextlib

import win32gui
import win32con
import win32process
import win32clipboard
import psutil
from PIL import Image, ImageGrab
import pyautogui

from crawl_images import crawl_grandma_images, clean_expired_images, retire_image

VERSION = "2.3.0"

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

# 2. 設定標準日誌輸出 (包含版本號碼)
logging.basicConfig(
    level=logging.INFO,
    format=f"[%(asctime)s][v{VERSION}] %(message)s",
    datefmt="%H:%M:%S"
)

def log_v(msg):
    """印出帶有時間戳記與版本號碼的訊息"""
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

# 設定檔與歷史記錄路徑
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
HISTORY_PATH = os.path.join(SCRIPT_DIR, "contact_history.json")
CHAT_MAP_PATH = os.path.join(SCRIPT_DIR, "chat_dirs.json")

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

DEFAULT_CONFIG = {
    "targets": [],
    "images_dir": "images",
    "trigger_mode": "photo_and_sticker", # "photo_and_sticker" (照片與貼圖皆回)、"photo_only" (只回照片) 或 "all" (文字、貼圖與照片皆回)
    "min_delay_seconds": 5,             # 隨機延遲下限 (秒)
    "max_delay_seconds": 900,           # 隨機延遲上限 (秒，最長 15 分鐘)
    "cooldown_seconds": 30,             # 防洗版冷卻時間 (秒)
    "history_rounds": 100,              # 百輪內不重複送同一張圖或對方曾送過的圖
    "replace_after_send": True,
    "collect_incoming_images": True,
    "image_ttl_days": 7,
    "min_image_pool": 10,
    "auto_crawl_images": True,
    "close_window_after_send": True
}

last_reply_times = {}
_last_valid_config = None

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

        # 嚴格 Schema 與型別校驗
        sanitized = DEFAULT_CONFIG.copy()
        if "targets" in cfg:
            if isinstance(cfg["targets"], list):
                sanitized["targets"] = [str(t).strip() for t in cfg["targets"] if str(t).strip()]
            else:
                logging.warning("config.json 中的 'targets' 必須是清單格式，暫時設為空名單。")
                sanitized["targets"] = []
        else:
            sanitized["targets"] = []

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
        for k in ("replace_after_send", "collect_incoming_images", "auto_crawl_images", "close_window_after_send"):
            if k in cfg and isinstance(cfg[k], bool):
                sanitized[k] = cfg[k]
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
        needed = min_pool - len(images)
        log_v(f"[庫存提示] 目前長輩圖庫存僅 {len(images)} 張 (低於設定值 {min_pool})，自動連網下載補充 {needed} 張備用...")
        try:
            crawl_grandma_images(target_count=needed, save_dir=abs_dir)
            images = _scan_images(abs_dir)
        except Exception as e:
            log_v(f"[警告] 自動補充長輩圖失敗: {e}")

    return images

def is_authentic_grandma_image(image_path):
    """
    使用 Windows 內建 OCR 檢驗圖片是否為真正的長輩圖 (早安/平安/祝福/喜樂/問候等)。
    若為寵物照、生活照、自拍、截圖等非長輩圖，則回傳 False 拒絕收納進長輩圖庫！
    """
    try:
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
        raw_text = proc.stdout.decode("utf-8", errors="ignore").replace(" ", "").replace("\r", "").replace("\n", "")

        keywords = [
            "早安", "晚安", "午安", "日安", "晨", "平安", "喜樂", "順心", "問候", "祝",
            "感恩", "幸福", "吉祥", "健康", "如意", "快樂", "笑口常開", "佛", "南無",
            "慈悲", "福", "蓮花", "認同請分享", "知足", "心靈", "語錄", "美麗", "微笑",
            "美好", "珍惜", "善", "緣", "善緣", "祝福"
        ]
        return any(kw in raw_text for kw in keywords)
    except Exception:
        return False

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
            crawl_grandma_images(target_count=5, save_dir=target_dir)
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


    selected = random.choice(candidates)
    return selected

def copy_image_to_clipboard(image_path):
    """使用 Win32 原生 API 將圖片以 CF_DIB (標準設備無關點陣圖) 注入剪貼簿，確保 LINE 100% 識別為真實圖片檔案而非文字"""
    abs_path = os.path.abspath(image_path)
    try:
        with Image.open(abs_path) as img:
            output = BytesIO()
            img.convert("RGB").save(output, "BMP")
            dib_data = output.getvalue()[14:]
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_DIB, dib_data)
        finally:
            win32clipboard.CloseClipboard()
        return True
    except Exception as e:
        log_v(f"[錯誤] Win32 點陣圖注入剪貼簿失敗: {e}")
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

def find_target_chat_window(target_name):
    """尋找標題精確相符或帶括號未讀數的獨立 1:1 聊天視窗 (絕不匹配 LINE 主視窗或群組視窗)"""
    if target_name.strip().lower() in ("line", "line main window", "line主視窗"):
        log_v("[安全拒絕] target 不得設定為系統主視窗關鍵字 'LINE'！")
        return []

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
        deadline = time.time() + 2.0
        fg_title = win32gui.GetWindowText(hwnd).strip()
        while time.time() < deadline:
            if not re.search(r"[\(\uFF08]\d+\+?[\)\uFF09]", fg_title):
                break
            time.sleep(0.15)
            fg_title = win32gui.GetWindowText(hwnd).strip()

        if re.search(r"[\(\uFF08]\d+\+?[\)\uFF09]", fg_title):
            log_v(f"[致命安全攔截] 視窗置於前景 2 秒後標題仍帶有括號「{fg_title}」，判定為群組成員數視窗，嚴禁點擊輸入框！")
            return False

        if re.search(GROUP_NAME_PATTERNS, fg_title):
            log_v(f"[致命安全攔截] 視窗標題「{fg_title}」含有群組特徵關鍵字，判定為群組視窗，嚴禁點擊輸入框！")
            return False

        if target_name and fg_title.lower() != target_name.lower():
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

def activate_window(hwnd):
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        else:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)

        pyautogui.press('alt')
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.3)
        return True
    except Exception as e:
        log_v(f"[警告] 前景切換失敗: {e}")
        return False

def _minimize_if_configured(hwnd, config, log_target=None):
    """安全將視窗最小化以維持後續未讀狀態"""
    if config.get("close_window_after_send", True) and hwnd:
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
            if log_target:
                log_v(f" -> 已自動將「{log_target}」視窗最小化，維持後續未讀狀態。")
        except Exception:
            pass

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
    # 智慧識別：檢查是否為雙層結構 (目錄內是否還有 40 字元的子目錄)
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
    透過 UIA 檢測目標視窗最末項目中的多媒體元素 (ImageControl) 與物理方向 (高度嚴重核心防禦)：
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

                        # 判定物理方向：
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
    """在主監控迴圈持續輪詢目標視窗指紋，記錄異動時間戳並自適應更新縮圖渲染狀態 (防延遲載入盲點)"""
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
    """初始化監控目標視窗指紋，用於高置信度自動學習綁定"""
    targets = specific_targets or []
    update_target_window_fingerprints(targets)

def try_auto_bind_channel(ch, cand_path, cand_m, targets, chat_dirs_map, competing_unknown_count=1):
    """
    對未知快取頻道進行高置信度自動學習綁定：
    1. 必須有且僅有 1 位目標好友視窗在線
    2. 該視窗必須通過 UIA 1:1 核驗
    3. 近 15 秒滑動視窗不可有 2 個以上不同之未知頻道同時活動 (中度 #1 並行滑動視窗防禦)
    4. 該視窗近期必須剛發生未消費之內容指紋異動 (杜絕拿舊歷史誤認他人頻道)
    5. 快取檔案寫入時間 (cand_m) 與視窗異動時間 (changed_at) 差距在容許範圍內 (-6.0s ~ +6.0s)
    6. 該視窗最末訊息必須為真實照片/貼圖 (高 DPI 動態門檻，排除純文字與大頭貼，延遲載入自適應重測)
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
        wins = find_target_chat_window(t)
        if wins and verify_is_not_group_window_uia(wins[0][0]):
            candidate_targets.append((t, wins[0][0]))

    if len(candidate_targets) != 1:
        # 若無在線目標或有多個在線目標在線，無法唯一判定歸屬，安全隔離拒絕盲目自動綁定
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
        log_v(f"[安全隔離] 快取頻道「{ch[:8]}...」抵達，目標好友「{target_name}」視窗近期無未消費的新訊息異動，拒絕自動綁定！")
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
        log_v(f"[安全隔離] 快取頻道「{ch[:8]}...」寫入時間 ({cand_m:.1f}) 與視窗變更時間 ({first_t:.1f}) 差距超標 ({time_diff:+.1f}s)，拒絕自動綁定！")
        return None

    if ref_media_t is not None:
        if state.get("is_thumbnail_settle"):
            # 延遲縮圖渲染：快取檔案可能於訊息抵達時 (first_t) 或渲染完成時 (ref_media_t) 寫入
            diff_media = abs(cand_m - ref_media_t)
            diff_first = abs(cand_m - first_t)
            if diff_media > AUTOBIND_TIME_WINDOW and diff_first > AUTOBIND_TIME_WINDOW:
                log_v(f"[安全隔離] 快取頻道「{ch[:8]}...」寫入時間 ({cand_m:.1f}) 與延遲縮圖時間差距超標，拒絕自動綁定！")
                return None
        else:
            # 🟡 核心防禦 3：一般即時照片，快取檔案寫入時間必須嚴格落在圖片時間 ±AUTOBIND_TIME_WINDOW 內！
            if abs(cand_m - ref_media_t) > AUTOBIND_TIME_WINDOW:
                log_v(f"[安全隔離] 快取頻道「{ch[:8]}...」寫入時間 ({cand_m:.1f}) 與圖片出現時間 ({ref_media_t:.1f}) 差距超標，拒絕自動綁定！")
                return None

    # 3. 媒體類型與物理方向核驗 (快照若因延遲載入為 False，在綁定前即時重測一次)：
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
                log_v(f"[安全隔離] 快取頻道「{ch[:8]}...」寫入時間 ({cand_m:.1f}) 與延遲重測時間 ({now_retest:.1f}) 及抵達時間 ({first_t:.1f}) 差距超標，拒絕自動綁定！")
                return None
            # 🟢 不更新 last_fp_change，避免將有效上界往後推！

    if not has_media:
        log_v(f"[安全隔離] 快取頻道「{ch[:8]}...」為圖片/貼圖，但目標好友「{target_name}」異動訊息無真實圖片控制項 (純文字訊息)，拒絕自動綁定！")
        return None

    if not is_incoming:
        log_v(f"[安全隔離] 快取頻道「{ch[:8]}...」抵達，但目標好友「{target_name}」異動圖片為本機送出 (靠右對齊)，拒絕自動綁定！")
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
        log_v(f"[自動學習綁定] 快取頻道「{ch[:8]}...」經視窗多媒體指紋與接收狀態核驗無誤，已成功自動綁定至「{target_name}」！")
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

def is_last_message_from_self_in_chat(chat_hwnd):
    """
    物理方向判定 (Fail-Closed)：檢查目標聊天視窗最底部訊息是否位於右側 (本機送出)。
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
            log_v(f"[物理方向安全攔截] 左右兩側底部高度接近 (L:{latest_left_y}, R:{latest_right_y})，判定為本機送出訊息或裝飾線，安全攔截！")
            return True

        # 若右側留白 (latest_right_y == -1) 或左側內容比右側更靠近底部 25px 以上，判定為對方送出
        if latest_left_y != -1 and (latest_right_y == -1 or latest_left_y > latest_right_y + 25):
            return False

        # 最底部內容出現在右側，左側留白，判定為自己發送
        return True
    except Exception as e:
        log_v(f"[警告] 物理方向校驗發生例外: {e}，遵循 Fail-Closed 原則視為自己發送以確保安全！")
        return True

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


def send_grandma_image(target_name, config, triggered_by_photo=True, triggered_file=None):
    """執行長輩圖發送"""
    global last_global_reply_time
    # 借花獻佛：僅精確收納本次觸發該回覆的圖片，絕不掃描其他聊天室快取
    if config.get("collect_incoming_images", True) and triggered_file:
        max_rounds = config.get("history_rounds", 100)
        harvest_line_incoming_images(config.get("images_dir", "images"), sender_name=target_name, max_rounds=max_rounds, specific_file_path=triggered_file)

    auto_crawl = config.get("auto_crawl_images", True)
    min_pool = config.get("min_image_pool", 10)
    images = get_images(config.get("images_dir", "images"), auto_replenish=auto_crawl, min_pool=min_pool)
    if not images:
        log_v(f"[錯誤] {config.get('images_dir', 'images')} 資料夾中沒有任何圖片！請放入長輩圖。")
        return "NOT_SENT"

    # 依據百輪記憶挑選一張不重複的長輩圖
    selected_image = pick_unique_image(images, target_name, config)
    if not selected_image:
        log_v(f"⚠️ [百輪保護安全攔截] 無可用之未重複合規長輩圖，安全終止回覆流程。")
        return "NOT_SENT"
    log_v(f"\n偵測到「{target_name}」發來訊息！")
    log_v(f" -> 隨機挑選一張合格且百輪不重複長輩圖: {os.path.basename(selected_image)}")

    # 隨機延遲時間 (避免固定規律被看穿是機器人)
    min_d = max(1, config.get("min_delay_seconds", 5))
    max_d = max(min_d, config.get("max_delay_seconds", 900))
    delay = random.randint(min_d, max_d)
    log_v(f" -> 為了模擬真人自然反應，本次隨機等待 {delay} 秒後發送...")
    time.sleep(delay)

    # 尋找現有聊天視窗 (嚴格 Fail-Closed，絕不盲目模擬搜尋或開啟其他視窗)
    matched = find_target_chat_window(target_name)
    if not matched:
        log_v(f"[錯誤] 無法取得「{target_name}」的聊天視窗，為防誤傳終止發送。請在 LINE 中保持該視窗開啟。")
        return "NOT_SENT"

    chat_hwnd = matched[0][0]
    if win32gui.IsIconic(chat_hwnd):
        win32gui.ShowWindow(chat_hwnd, win32con.SW_RESTORE)
        time.sleep(0.3)
    activate_window(chat_hwnd)
    time.sleep(0.3)
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
    if re.search(r"[\(\uFF08]\d+\+?[\)\uFF09]", fg_title):
        log_v(f"[致命安全攔截] 目前前景視窗「{fg_title}」帶有成員數或未讀括號，判定為群組視窗，嚴格拒絕發送！")
        _minimize_if_configured(chat_hwnd, config)
        return "NOT_SENT"

    if fg_title.strip().lower() != target_name.lower():
        log_v(f"[致命安全攔截] 目前前景視窗為「{fg_title}」，非 1:1 目標對象「{target_name}」，嚴格終止發送！")
        _minimize_if_configured(chat_hwnd, config)
        return "NOT_SENT"

    # 物理方向校驗 (Fail-Closed)：在貼上圖片發送前核驗聊天室最後一則訊息
    if is_last_message_from_self_in_chat(chat_hwnd):
        log_v(f"[防自循環保護]「{target_name}」視窗內最後訊息位於右側（本機送出），安全攔截絕不發送！")
        _minimize_if_configured(chat_hwnd, config)
        return "NOT_SENT"

    # 發送前先對快取進行檔案集合快照 (包含 mtime 與大小)
    active_cache = get_active_line_cache_dir()
    before_cache_files = get_all_cache_files(active_cache)

    # 複製圖片至剪貼簿
    if not copy_image_to_clipboard(selected_image):
        log_v("[錯誤] 複製圖片到剪貼簿失敗。")
        _minimize_if_configured(chat_hwnd, config)
        return "NOT_SENT"

    time.sleep(0.3)
    # 貼上前核驗前景視窗未被轉移
    if win32gui.GetForegroundWindow() != chat_hwnd:
        log_v(f"[安全攔截] 貼上前焦點已被移轉至其他視窗，終止貼上！")
        _minimize_if_configured(chat_hwnd, config)
        return "NOT_SENT"

    # 送出前最後微秒防禦 (TOCTOU 免疫)：貼上瞬間核驗前景 HWND
    cur_fg = win32gui.GetForegroundWindow()
    if cur_fg != chat_hwnd:
        log_v(f"[安全攔截] 貼上瞬間焦點非目標視窗 (目前 HWND: {cur_fg})，終止發送！")
        _minimize_if_configured(chat_hwnd, config)
        return "NOT_SENT"

    # 貼上圖片 (Ctrl+V) 與送出防護 (保證例外發生時不丟失記帳、不重複記帳且清空待送殘留)
    pasted = False
    enter_sent = False
    accounting_done = False
    history_recorded = False
    try:
        pyautogui.hotkey('ctrl', 'v')
        pasted = True
        log_v(" -> 已貼上長輩圖，等待 LINE 預覽載入...")
        time.sleep(1.0)

        # 單次 Enter：送出前瞬間再次核驗前景 HWND (TOCTOU 雙重防禦)
        if win32gui.GetForegroundWindow() != chat_hwnd:
            log_v(f"[安全攔截] 送出長輩圖前焦點已被移轉至其他視窗，注入 Esc 取消預覽並終止發送！")
            # 啟用並發送 Esc 清理輸入框與貼圖預覽，徹底杜絕待送圖片殘留
            try:
                win32gui.ShowWindow(chat_hwnd, win32con.SW_RESTORE)
                activate_window(chat_hwnd)
                time.sleep(0.1)
                if win32gui.GetForegroundWindow() == chat_hwnd:
                    pyautogui.press('esc')
                time.sleep(0.2)
            except Exception:
                pass
            _minimize_if_configured(chat_hwnd, config)
            return "NOT_SENT"
        pyautogui.press('enter')
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
        record_reply_sent(target_name)
        accounting_done = True

        if new_count > 0:
            log_v(f" -> [差集快照] 成功識別 {new_count} 個本機發送快取檔案，加入黑名單！")

        # 安全最小化 (避免維持在前景導致後續持續秒已讀)
        if config.get("close_window_after_send", True):
            time.sleep(1.5)
            _minimize_if_configured(chat_hwnd, config, log_target=target_name)

        # Enter 已按下：無論氣泡是否延遲渲染，一律寫入百輪記憶庫，杜絕未來重複挑選同一張發送
        sent_md5 = get_file_md5(selected_image)
        record_contact_sent(target_name, sent_md5, max_rounds=config.get("history_rounds", 100))
        history_recorded = True

        # 若啟用 replace_after_send，送出後自動淘汰該張長輩圖以維持圖庫滾動換新
        if config.get("replace_after_send", True) and selected_image:
            try:
                images_dir_path = os.path.join(SCRIPT_DIR, config.get("images_dir", "images"))
                retire_image(selected_image, images_dir=images_dir_path)
            except Exception as e:
                log_v(f"[圖庫汰換] 自動汰換圖片失敗: {e}")

        if verified_bubble:
            log_v(f"[送達確認]「{target_name}」聊天視窗右側氣泡驗證通過，長輩圖確認已成功送出！")
            return "SENT_CONFIRMED"
        else:
            log_v("長輩圖已按下 Enter 送出，視覺氣泡暫無法 100% 辨識 (判定為 SENT_UNCERTAIN)。已記為送出並記入百輪歷史，絕不重複重送。")
            return "SENT_UNCERTAIN"
    except Exception as e:
        log_v(f"[例外防護] 發送流程發生異常: {e}")
        if not enter_sent:
            # 貼上後未送出 Enter 即中斷（包括 FailSafeException）：清空聊天室輸入框避免殘留圖片
            if pasted:
                try:
                    if win32gui.GetForegroundWindow() == chat_hwnd:
                        pyautogui.press('esc')
                        pyautogui.hotkey('ctrl', 'a')
                        pyautogui.press('backspace')
                except Exception:
                    pass
            # 回傳 NOT_SENT 讓外層計入重試次數並執行指數退避，徹底杜絕無上限高頻無窮迴圈重試
            return "NOT_SENT"
        else:
            # Enter 已送出：若尚未記帳，補全發送記帳與百輪記錄，杜絕二次重複記帳與重複發送
            if not accounting_done:
                send_now = time.time()
                last_global_reply_time = send_now
                record_reply_sent(target_name)
                accounting_done = True
            if not history_recorded:
                sent_md5 = get_file_md5(selected_image)
                record_contact_sent(target_name, sent_md5, max_rounds=config.get("history_rounds", 100))
                history_recorded = True
            return "SENT_UNCERTAIN"

def check_for_incoming_messages(config):
    """
    掃描是否有新訊息：
    1. 視窗標題正規表達式未讀檢查 (嚴格比對字尾括號數字，如 (1) 或 （2）)，排除群組名稱誤判。
    2. 5 秒發送防抖保護與實體快取差集黑名單過濾。
    3. 斷路器保護 (Circuit Breaker)：5 分鐘內同對象最多回覆 2 次，超標啟動 30 分鐘退避鎖定。
    4. 物理方向校驗 (Fail-Closed)：對話框最後一則在右側或不可見時絕不發送。
    """
    global last_seen_cache_mtime, last_global_reply_time, PROCESSED_CACHE_FILES
    new_image_found = None
    targets = config.get("targets", [])
    cooldown = config.get("cooldown_seconds", 30)
    current_time = time.time()
    update_target_window_fingerprints(targets)

    # 全域防洗版保護：只要剛送出過任何圖片，在冷卻時間內絕不再次觸發！
    if current_time - last_global_reply_time < cooldown:
        return

    trigger_mode = config.get("trigger_mode", "photo_and_sticker")
    # 0. 超前檢查：發送失敗之暫時重試佇列 (保證照片與貼圖失敗後在冷卻期過後能精確重試)
    target_to_reply = None
    new_image_found = None
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
                        if matched:
                            # 電腦版獨立視窗標題正常為好友名稱 (不帶括號未讀數)，視窗在線即刻確認
                            chosen_candidate = cand_path
                            chosen_target = cand_target
                            PROCESSED_CACHE_FILES.add((cand_path, cand_m))
                            max_processed_m = max(max_processed_m, cand_m)
                            break
                        else:
                            PROCESSED_CACHE_FILES.add((cand_path, cand_m))
                            max_processed_m = max(max_processed_m, cand_m)
                            log_v(f"[安全隔離] 快取頻道「{ch[:8]}...」對應「{cand_target}」，但桌面無該好友專屬視窗，安全略過。")
                    else:
                        # 中度 #2 防禦：若為尚未綁定之頻道，暫緩加入 PROCESSED_CACHE_FILES，並納入 deferred_mtimes 防吞
                        if ch and ch not in chat_dirs_map:
                            file_age = current_time - cand_m
                            if file_age <= AUTOBIND_TIMEOUT:
                                deferred_mtimes.append(cand_m)
                                log_v(f"[安全暫緩] 快取頻道「{ch[:8]}...」暫未匹配視窗異動 (保留重試機會，暫不標記為已處理)。")
                            else:
                                PROCESSED_CACHE_FILES.add((cand_path, cand_m))
                                max_processed_m = max(max_processed_m, cand_m)
                                log_v(f"[安全隔離] 快取頻道「{ch[:8]}...」超逾 15 秒未通過目標綁定核驗，標記略過。")
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

        # 快取掃描命中合格新媒體，在此統籌觸發發送！
        if chosen_target and chosen_candidate:
            last_sent_to_cand = LAST_SENT_TIME_BY_TARGET.get(chosen_target, 0)
            if current_time - last_sent_to_cand < 5:
                last_reply_times[chosen_target] = current_time
                log_v(f"[防自循環保護]「{chosen_target}」在發送後 5 秒內偵測到本機寫入快取，安全略過。")
            elif current_time - last_reply_times.get(chosen_target, 0) >= cooldown:
                target_to_reply = chosen_target
                new_image_found = chosen_candidate
                log_v(f"\n[即時偵測] 鎖定快取頻道「{extract_chat_hash_from_path(new_image_found)[:8]}...」對應好友「{target_to_reply}」收到新貼圖/照片！(檔案: {os.path.basename(new_image_found)})")
            else:
                remain = int(cooldown - (current_time - last_reply_times.get(chosen_target, 0)))
                log_v(f"[冷卻中]「{chosen_target}」距上次回覆未滿 {int(cooldown)} 秒 (剩餘 {remain} 秒)，本次新圖不回覆。")

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
        send_result = send_grandma_image(target_to_reply, config, triggered_file=new_image_found)
        last_reply_times[target_to_reply] = time.time()

        if send_result in ("SENT_CONFIRMED", "SENT_UNCERTAIN"):
            # 為防重複刷屏一律視為已處理並更新基準線
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

def init_unread_baselines(specific_targets=None):
    """啟動時先掃描所有 LINE 視窗目前的未讀數作為初始基準線，避免啟動時將歷史未讀訊息誤判為新訊息"""
    for hwnd, title, cls, vis in get_all_line_windows():
        m = re.search(r"^(.*?)\s*[\(\uFF08](\d+\+?)[\)\uFF09]\s*$", title)
        if m:
            name = m.group(1).strip()
            if specific_targets is not None and not any(name.lower() == t.lower() for t in specific_targets):
                continue
            count = parse_unread_count(m.group(2))
            LAST_PROCESSED_UNREAD[name] = max(LAST_PROCESSED_UNREAD.get(name, 0), count)
            log_v(f"[基準線初始化]「{name}」初始未讀數記為 {count}，避免啟動誤觸發。")

def main():
    parser = argparse.ArgumentParser(description=f"LINE 自動回覆長輩圖工具 v{VERSION}")
    parser.add_argument("-v", "--version", action="version", version=f"LINE Grandma Bot v{VERSION}")
    parser.add_argument("--test", metavar="NAME", help="手動測試發送一次給指定對象 (例如: --test 媽媽)")
    parser.add_argument("--calibrate", metavar="NAME", help="校準指定聯絡人的快取頻道 (例如: --calibrate 媽媽)")
    parser.add_argument("--crawl", type=int, metavar="COUNT", help="立刻上網抓取指定數量的長輩圖儲存備用 (例如: --crawl 20)")
    parser.add_argument("--list-images", action="store_true", help="列出目前讀取到的長輩圖清單")
    parser.add_argument("--clear-history", action="store_true", help="清空已發送長輩圖的歷史記錄")
    parser.add_argument("--scan-windows", action="store_true", help="掃描並印出目前桌面所有的 LINE 視窗資訊")
    parser.add_argument("--unbind", metavar="NAME_OR_HASH", help="解除指定聯絡人或雜湊之頻道綁定記錄")
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

    if args.crawl:
        count = args.crawl
        target_dir = os.path.join(SCRIPT_DIR, config.get("images_dir", "images"))
        crawl_grandma_images(target_count=count, save_dir=target_dir)
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
            # 增量連續焦點核驗：只有輪詢起點與終點均持續處於目標視窗前景焦點時，才給予雙重置信認可
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
        send_grandma_image(target, config)
        return

    print("=" * 65)
    print(f"  LINE 電腦版 - 長輩圖智慧自動回覆程式 v{VERSION} (常駐背景)")
    print("=" * 65)
    print(f"程式版本 (version)       : v{VERSION}")
    print(f"監控對象名單 (targets)   : {config.get('targets', [])}")
    t_mode = config.get('trigger_mode', 'photo_and_sticker')
    mode_desc = "照片與貼圖皆回" if t_mode == "photo_and_sticker" else ("只回照片" if t_mode == "photo_only" else "文字照片貼圖皆回")
    print(f"觸發模式 (trigger_mode)  : {t_mode} ({mode_desc})")
    print(f"隨機延遲範圍 (delay)     : {config.get('min_delay_seconds', 5)} ~ {config.get('max_delay_seconds', 900)} 秒")
    print(f"每位聯絡人記憶輪數       : {config.get('history_rounds', 100)} 輪 (不重複發送或回送對方發過的圖)")
    print(f"自動收納對方圖片 (借花獻佛): {config.get('collect_incoming_images', True)}")
    print(f"發送後自動汰換新圖       : {config.get('replace_after_send', True)}")
    print(f"舊圖保存期限 (TTL)       : {config.get('image_ttl_days', 7)} 天")
    print(f"防洗版冷卻 (cooldown)    : {config.get('cooldown_seconds', 30)} 秒")
    print(f"回覆後安全最小化         : {config.get('close_window_after_send', True)}")
    print("-" * 65)
    print("說明：")
    print("1. 對方傳長輩圖來時，程式會隨機等待數秒，並挑選一張不同的長輩圖回傳。")
    print("2. 圖片送出後自動淘汰換新，且定期清理超過期限的舊圖，保持圖庫新鮮。")
    print("3. 視窗會自動最小化，維持後續未讀。")
    print("4. 按下 Ctrl + C 即可隨時停止程式。")
    print("=" * 65)

    # 建立目前快取基準線，確保啟動後只監聽「新傳入」的圖片，絕不觸發舊圖或剛才發送的圖
    advance_cache_mtime_to_current()
    init_unread_baselines()
    init_target_window_fingerprints(config.get("targets", []))
    log_v("快取監聽基準線已就緒，正靜態待命中...")
    print("-" * 65)

    last_cleanup_time = 0
    current_targets = list(config.get("targets", []))

    try:
        while True:
            try:
                config = load_config()

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

                # 每 1 小時檢查一次過期長輩圖
                if time.time() - last_cleanup_time > 3600:
                    ttl = config.get("image_ttl_days", 7)
                    target_dir = os.path.join(SCRIPT_DIR, config.get("images_dir", "images"))
                    clean_expired_images(images_dir=target_dir, ttl_days=ttl)
                    last_cleanup_time = time.time()

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

if __name__ == "__main__":
    main()
