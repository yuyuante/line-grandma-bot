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
import struct
import argparse
import hashlib
import shutil
import subprocess
import logging
import tempfile
import ctypes
from io import BytesIO

import win32gui
import win32con
import win32process
import win32clipboard
import psutil
from PIL import Image
import pyautogui
import pyperclip
import uiautomation as auto

from crawl_images import crawl_grandma_images, clean_expired_images, retire_image

VERSION = "2.2.0"

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
    print(f"{prefix}[{now}][v{VERSION}] {msg}")

# 設定檔與歷史記錄路徑
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
HISTORY_PATH = os.path.join(SCRIPT_DIR, "contact_history.json")

def atomic_save_json(file_path, data):
    """使用暫存檔與原子替換 (os.replace)，防止當機或意外中斷損毀 JSON 檔案"""
    dirname, basename = os.path.split(file_path)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=dirname, delete=False, encoding="utf-8") as tf:
            json.dump(data, tf, ensure_ascii=False, indent=2)
            temp_name = tf.name
        os.replace(temp_name, file_path)
        return True
    except Exception as e:
        logging.warning(f"原子寫入 {file_path} 失敗: {e}")
        if temp_name and os.path.exists(temp_name):
            try:
                os.remove(temp_name)
            except Exception:
                pass
        return False

DEFAULT_CONFIG = {
    "targets": ["媽媽"],
    "images_dir": "images",
    "trigger_mode": "photo_only",       # "photo_only" (只回照片) 或 "all" (照片與文字皆回)
    "min_delay_seconds": 5,             # 隨機延遲下限 (秒)
    "max_delay_seconds": 25,            # 隨機延遲上限 (秒)
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

def load_config():
    if not os.path.exists(CONFIG_PATH):
        atomic_save_json(CONFIG_PATH, DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f"讀取 config.json 失敗 ({e})，使用預設配置。")
        return DEFAULT_CONFIG.copy()

def get_file_md5(file_path):
    """計算檔案 MD5 數位指紋"""
    try:
        with open(file_path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except Exception:
        return None

def load_contact_history():
    """讀取各聯絡人的百輪收發歷史紀錄"""
    if not os.path.exists(HISTORY_PATH):
        return {}
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f"讀取 contact_history.json 失敗: {e}")
        return {}

def save_contact_history(history):
    """原子持久化儲存各聯絡人的百輪收發歷史紀錄"""
    atomic_save_json(HISTORY_PATH, history)

def record_contact_sent(target_name, md5_hash, max_rounds=100):
    if not md5_hash:
        return
    history = load_contact_history()
    if target_name not in history:
        history[target_name] = {"sent": [], "received": []}
    if "sent" not in history[target_name]:
        history[target_name]["sent"] = []
    history[target_name]["sent"].append(md5_hash)
    history[target_name]["sent"] = history[target_name]["sent"][-max_rounds:]
    save_contact_history(history)

def record_contact_received(target_name, md5_hash, max_rounds=100):
    if not md5_hash:
        return
    history = load_contact_history()
    if target_name not in history:
        history[target_name] = {"sent": [], "received": []}
    if "received" not in history[target_name]:
        history[target_name]["received"] = []
    history[target_name]["received"].append(md5_hash)
    history[target_name]["received"] = history[target_name]["received"][-max_rounds:]
    save_contact_history(history)

def get_images(images_dir, auto_replenish=True, min_pool=10):
    if not os.path.isabs(images_dir):
        abs_dir = os.path.join(SCRIPT_DIR, images_dir)
    else:
        abs_dir = images_dir
    if not os.path.exists(abs_dir):
        os.makedirs(abs_dir, exist_ok=True)
    extensions = ("*.jpg", "*.jpeg", "*.png", "*.bmp", "*.webp")
    files = []
    for ext in extensions:
        files.extend(glob.glob(os.path.join(abs_dir, ext)))
        files.extend(glob.glob(os.path.join(abs_dir, ext.upper())))
    images = list(set(files))

    # 自動從網路補充庫存
    if auto_replenish and len(images) < min_pool:
        needed = min_pool - len(images)
        log_v(f"[庫存提示] 目前長輩圖庫存僅 {len(images)} 張 (低於設定值 {min_pool})，自動連網下載補充 {needed} 張備用...")
        try:
            crawl_grandma_images(target_count=needed, save_dir=abs_dir)
            # 重新掃描
            files = []
            for ext in extensions:
                files.extend(glob.glob(os.path.join(abs_dir, ext)))
                files.extend(glob.glob(os.path.join(abs_dir, ext.upper())))
                images = list(set(files))
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
        ps_script = f"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
[Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = [System.WindowsRuntimeSystemExtensions].GetMethods() | ? {{ $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.IsGenericMethod }} | Select-Object -First 1

function Await($winRtOp, $resultType) {{
    $m = $asTaskGeneric.MakeGenericMethod($resultType)
    $t = $m.Invoke($null, @($winRtOp))
    return $t.GetAwaiter().GetResult()
}}

try {{
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync('{abs_p}')) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    
    $lang = [Windows.Globalization.Language]::new("zh-Hant")
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
    if ($null -eq $engine) {{ $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }}
    $res = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    foreach ($line in $res.Lines) {{
        Write-Output $line.Text
    }}
}} catch {{}}
"""
        cmd = ["powershell", "-NoProfile", "-Command", ps_script]
        proc = subprocess.run(cmd, capture_output=True, timeout=8)
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

def harvest_line_incoming_images(images_dir, sender_name=None, max_rounds=100):
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

    cache_pattern = os.path.expandvars(r"%LOCALAPPDATA%\LINE\Cache\*\tmp\*.*")
    cached_files = glob.glob(cache_pattern)
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
    從圖庫挑選長輩圖：
    1. 百輪內不重複送同一張圖給「{target_name}」 (sent 歷史)
    2. 百輪內不送「{target_name}」曾送過的相同圖給對方 (received 歷史)
    3. 若本機可用圖均已在百輪記憶中，自動從網路補充全新長輩圖！
    """
    if not images:
        return None

    max_rounds = config.get("history_rounds", 100)
    history = load_contact_history()
    contact_data = history.get(target_name, {"sent": [], "received": []})

    # 取得百輪內禁止發給該對象的 MD5 集合
    forbidden_md5s = set(contact_data.get("sent", [])[-max_rounds:] + contact_data.get("received", [])[-max_rounds:])

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
        crawl_grandma_images(target_count=5, save_dir=target_dir)
        fresh_images = get_images(config.get("images_dir", "images"), auto_replenish=False)
        for img in fresh_images:
            m = get_file_md5(img)
            if m and m not in forbidden_md5s:
                candidates.append(img)

        # 極端保險：若仍無法取得全新圖片，只排除上一張
        if not candidates:
            last_sent = contact_data.get("sent", [])[-1] if contact_data.get("sent") else ""
            candidates = [img for img in images if get_file_md5(img) != last_sent] or images

    selected = random.choice(candidates)
    return selected

def copy_image_to_clipboard(image_path):
    """使用 Windows 原生 Set-Clipboard 將圖片注入剪貼簿，確保 LINE 100% 識別為圖片檔案"""
    abs_path = os.path.abspath(image_path)
    try:
        import subprocess
        ps_cmd = f"Set-Clipboard -Path '{abs_path}'"
        subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], check=True)
        return True
    except Exception as e:
        log_v(f"[警告] PowerShell 寫入剪貼簿失敗 ({e})，改用 Win32 API 備用方案...")
        try:
            with Image.open(abs_path) as img:
                output = BytesIO()
                img.convert("RGB").save(output, "BMP")
                dib_data = output.getvalue()[14:]
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_DIB, dib_data)
            win32clipboard.CloseClipboard()
            return True
        except Exception as e2:
            log_v(f"[錯誤] 剪貼簿備用方案失敗: {e2}")
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

def find_target_chat_window(target_name):
    """尋找標題包含目標名稱，或內部包含目標名稱的獨立聊天視窗 (支援 Qt6 無標題視窗)"""
    # 1. 傳統 Win32 標題檢查
    windows = get_all_line_windows()
    for hwnd, title, cls, vis in windows:
        if target_name.lower() in title.lower():
            return [(hwnd, title, cls, vis)]

    # 2. Qt6 獨立聊天室：透過 UIAutomation 檢查視窗內部頂部的聯絡人標籤
    try:
        root = auto.GetRootControl()
        for win in root.GetChildren():
            if "QWindowIcon" in win.ClassName:
                for ctrl, depth in auto.WalkControl(win, maxDepth=6):
                    try:
                        if ctrl.Name and target_name.lower() in ctrl.Name.lower():
                            hwnd = win.NativeWindowHandle
                            return [(hwnd, ctrl.Name, win.ClassName, True)]
                    except Exception:
                        pass
    except Exception:
        pass
    return []

def focus_chat_input(hwnd):
    """聚焦對話視窗底部的輸入訊息區域 (精確適配雙欄主視窗與獨立小視窗)"""
    try:
        rect = win32gui.GetWindowRect(hwnd)
        w = rect[2] - rect[0]
        h = rect[3] - rect[1]
        if w > 600:
            # 雙欄式主視窗：聊天輸入框位於右側 65% 處
            click_x = rect[0] + int(w * 0.65)
        else:
            # 獨立小視窗：聊天輸入框位於水平中央 50% 處
            click_x = rect[0] + int(w * 0.5)
        click_y = rect[1] + h - 75
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

def open_chat_via_main_window(target_name):
    line_hwnd = find_line_main_window()
    if not line_hwnd:
        line_exe = os.path.expandvars(r"%LOCALAPPDATA%\LINE\bin\current\LINE.exe")
        if os.path.exists(line_exe):
            log_v("[提示] 嘗試喚醒 LINE 程式...")
            os.system(f'start "" "{line_exe}"')
            time.sleep(2)
            line_hwnd = find_line_main_window()

    if not line_hwnd:
        log_v("[錯誤] 找不到 LINE 視窗，請確認 LINE 已在背景或桌面開啟。")
        return None

    activate_window(line_hwnd)
    time.sleep(0.4)

    # 1. 直接點擊左上角「以姓名搜尋」搜尋框 (位於 X+150, Y+55 處)
    try:
        r = win32gui.GetWindowRect(line_hwnd)
        pyautogui.click(r[0] + 150, r[1] + 55)
        time.sleep(0.3)
    except Exception:
        pyautogui.hotkey('ctrl', 'f')
        time.sleep(0.3)

    # 清空並填入目標名稱
    pyautogui.hotkey('ctrl', 'a')
    time.sleep(0.2)
    pyautogui.press('backspace')
    time.sleep(0.2)
    pyperclip.copy(target_name)
    pyautogui.hotkey('ctrl', 'v')
    time.sleep(1.0)

    # 2. 開啟目標好友聊天室 (100% 純鍵盤快捷鍵，絕不點擊聯絡人列避免誤觸通話圖示！)
    # LINE 電腦版官方標準操作：在搜尋列輸入名字後，按「方向鍵下 (↓)」選中第一筆好友，再按 Enter 開啟聊天
    pyautogui.press('down')
    time.sleep(0.3)
    pyautogui.press('enter')
    time.sleep(1.0)

    matched = find_target_chat_window(target_name)
    if matched:
        return matched[0][0]

    fg_hwnd = win32gui.GetForegroundWindow()
    fg_title = win32gui.GetWindowText(fg_hwnd)
    if target_name in fg_title or "LINE" in fg_title:
        return fg_hwnd

    return line_hwnd

def is_photo_message(message_text):
    """判斷訊息內容是否為長輩圖/照片"""
    if not message_text:
        return False
    indicators = ["[照片]", "[圖片]", "照片", "圖片", "Photo", "photo", "已傳送照片"]
    return any(ind in message_text for ind in indicators)

def send_grandma_image(target_name, config, triggered_by_photo=True):
    """執行長輩圖發送"""
    # 借花獻佛：自動收納對方傳過來的圖片儲存備用 (並將指紋記入該對象的 received 歷史)
    if config.get("collect_incoming_images", True):
        max_rounds = config.get("history_rounds", 100)
        harvest_line_incoming_images(config.get("images_dir", "images"), sender_name=target_name, max_rounds=max_rounds)

    auto_crawl = config.get("auto_crawl_images", True)
    min_pool = config.get("min_image_pool", 10)
    images = get_images(config.get("images_dir", "images"), auto_replenish=auto_crawl, min_pool=min_pool)
    if not images:
        log_v(f"[錯誤] {config.get('images_dir', 'images')} 資料夾中沒有任何圖片！請放入長輩圖。")
        return False

    # 依據百輪記憶挑選一張不重複的長輩圖 (絕不重複送過、絕不回送對方曾發過的圖)
    selected_image = pick_unique_image(images, target_name, config)
    log_v(f"\n偵測到「{target_name}」發來訊息！")
    log_v(f" -> 隨機挑選一張合格且百輪不重複長輩圖: {os.path.basename(selected_image)}")

    # 隨機延遲時間 (避免固定規律被看穿是機器人)
    min_d = max(1, config.get("min_delay_seconds", 5))
    max_d = max(min_d, config.get("max_delay_seconds", 25))
    delay = random.randint(min_d, max_d)
    log_v(f" -> 為了模擬真人自然反應，本次隨機等待 {delay} 秒後發送...")
    time.sleep(delay)

    # 尋找現有聊天視窗或開啟
    matched = find_target_chat_window(target_name)
    chat_hwnd = None
    if matched:
        chat_hwnd = matched[0][0]
        activate_window(chat_hwnd)
        time.sleep(0.3)
        focus_chat_input(chat_hwnd)
    else:
        chat_hwnd = open_chat_via_main_window(target_name)
        if chat_hwnd:
            focus_chat_input(chat_hwnd)

    if not chat_hwnd:
        log_v(f"[錯誤] 無法取得「{target_name}」的聊天視窗。")
        return False

    time.sleep(0.4)

    # 送出前安全核對：若為獨立視窗，標題必須完全吻合目標名稱，防止近名錯送！
    fg_hwnd = win32gui.GetForegroundWindow()
    fg_title = win32gui.GetWindowText(fg_hwnd)
    line_main = find_line_main_window()
    if chat_hwnd != line_main and target_name.lower() not in fg_title.lower():
        log_v(f"[安全防護攔截] 目前前景視窗「{fg_title}」與目標「{target_name}」不符，中止發送！")
        return False

    # 複製圖片至剪貼簿
    if not copy_image_to_clipboard(selected_image):
        log_v("[錯誤] 複製圖片到剪貼簿失敗。")
        return False

    time.sleep(0.3)
    # 貼上圖片 (Ctrl+V)
    pyautogui.hotkey('ctrl', 'v')
    log_v(" -> 已貼上長輩圖，等待 LINE 預覽載入...")
    time.sleep(1.0)

    # 第一次 Enter：確認「送出為圖片」
    pyautogui.press('enter')
    time.sleep(0.8)

    # 第二次 Enter：確認送出訊息
    pyautogui.press('enter')
    log_v("長輩圖回覆成功！")

    # 成功發送後才寫入百輪歷史記憶 (避免失敗卻記為已送)
    record_contact_sent(target_name, get_file_md5(selected_image), max_rounds=config.get("history_rounds", 100))

    # 安全最小化 (避免維持在前景導致後續持續秒已讀)
    if config.get("close_window_after_send", True):
        time.sleep(1.5)
        try:
            win32gui.ShowWindow(chat_hwnd, win32con.SW_MINIMIZE)
            log_v(f" -> 已自動將「{target_name}」視窗最小化，維持後續未讀狀態。")
        except Exception:
            pass

    # 汰舊換新：送出後自動淘汰該張長輩圖，並從網路補充全新圖片遞補
    if config.get("replace_after_send", True):
        retired = retire_image(selected_image)
        if retired and config.get("auto_crawl_images", True):
            target_dir = os.path.join(SCRIPT_DIR, config.get("images_dir", "images"))
            log_v(" -> [汰舊換新] 正在從網路補充 1 張全新長輩圖遞補庫存...")
            try:
                crawl_grandma_images(target_count=1, save_dir=target_dir)
            except Exception as e:
                log_v(f"[警告] 補充新圖失敗: {e}")

    last_reply_times[target_name] = time.time()
    global last_global_reply_time
    last_global_reply_time = time.time()
    
    # 送出完成後等待 2 秒讓 LINE 本機將剛送出的圖片快取寫入完畢，並將監聽時間戳記推進到當前最新，徹底避免觸發自己送出的圖片！
    time.sleep(2.0)
    advance_cache_mtime_to_current()
    log_v(" -> [防自循環] 快取時間戳記已推進至發送完畢，進入全域冷卻待命。")
    return True

# 全域發送時間與快取時間戳記
last_global_reply_time = 0
last_seen_cache_mtime = time.time()

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
    subdirs.sort(key=lambda x: x[0], reverse=True)
    return subdirs[0][1] if subdirs else None

def advance_cache_mtime_to_current():
    """將監聽時間戳記更新為當前快取目錄中最晚的檔案時間，防止觸發過去的舊圖或剛發送的圖"""
    global last_seen_cache_mtime
    active_cache = get_active_line_cache_dir()
    newest = time.time()
    if active_cache:
        for root, _, files in os.walk(active_cache):
            if '\\m\\' in root or '/m/' in root:
                for f in files:
                    try:
                        m = os.path.getmtime(os.path.join(root, f))
                        if m > newest:
                            newest = m
                    except Exception:
                        pass
    last_seen_cache_mtime = max(newest, time.time())

def check_for_incoming_messages(config):
    """
    掃描是否有新訊息：
    1. 快取底層監聽 (Cache Watcher)：當 LINE 接收到新圖片時，底層檔案即時更新 (.eimg 加密圖片或普通圖檔)，100% 免疫 Qt 視窗標題無效問題！
    2. 傳統標題未讀標記
    """
    global last_seen_cache_mtime, last_global_reply_time
    targets = config.get("targets", [])
    cooldown = config.get("cooldown_seconds", 30)
    current_time = time.time()

    # 全域防洗版保護：只要剛送出過任何圖片，在冷卻時間內絕不再次觸發！
    if current_time - last_global_reply_time < cooldown:
        return

    # 1. LINE 底層圖片接收監聽 (精準、零誤判)
    active_cache = get_active_line_cache_dir()
    if active_cache:
        new_image_found = None
        newest_m = last_seen_cache_mtime

        for root, _, files in os.walk(active_cache):
            # LINE 接收到的對話圖片與媒體存放於各對話目錄下的 m 子資料夾或 tmp 資料夾中
            if '\\m\\' in root or '/m/' in root or '\\tmp\\' in root:
                for f in files:
                    p = os.path.join(root, f)
                    try:
                        m = os.path.getmtime(p)
                        if m > last_seen_cache_mtime:
                            sz = os.path.getsize(p)
                            # 判定為聊天照片：.eimg 加密圖片或大於 5KB 的媒體檔
                            if f.endswith('.eimg') or sz > 5000:
                                new_image_found = p
                                if m > newest_m:
                                    newest_m = m
                                break
                    except Exception:
                        pass
                if new_image_found:
                    break

        if new_image_found:
            last_seen_cache_mtime = max(newest_m, current_time)
            last_global_reply_time = current_time
            sz = os.path.getsize(new_image_found) if os.path.exists(new_image_found) else 0
            log_v(f"\n[即時偵測] 成功捕捉到 LINE 剛剛接收到新長輩圖！(檔案: {os.path.basename(new_image_found)}, 大小: {sz} bytes)")
            
            # 多人精準識別：透過快取路徑的 40 位元對話室 Hash 精確定位是哪位好友發送的！
            target_to_reply = None
            chat_dirs_map = {}
            chat_map_path = os.path.join(SCRIPT_DIR, "chat_dirs.json")
            if os.path.exists(chat_map_path):
                try:
                    with open(chat_map_path, "r", encoding="utf-8") as f:
                        chat_dirs_map = json.load(f)
                except Exception:
                    pass

            # 解析本次新圖所在的對話目錄 Hash
            norm_parts = os.path.normpath(new_image_found).split(os.sep)
            detected_chat_hash = None
            for idx, part in enumerate(norm_parts):
                if len(part) == 40 and idx > 0 and norm_parts[idx-1] != 'Cache':
                    detected_chat_hash = part

            if detected_chat_hash and detected_chat_hash in chat_dirs_map:
                candidate = chat_dirs_map[detected_chat_hash]
                if candidate in targets:
                    target_to_reply = candidate
                    log_v(f" -> [目錄指紋匹配] 鎖定快取頻道「{detected_chat_hash[:8]}...」對應好友:「{target_to_reply}」！")
            elif detected_chat_hash and len(targets) == 1:
                target_to_reply = targets[0]
                chat_dirs_map[detected_chat_hash] = target_to_reply
                try:
                    with open(chat_map_path, "w", encoding="utf-8") as f:
                        json.dump(chat_dirs_map, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass

            # 備用識別：若有獨立未讀視窗標題，以未讀標記為準
            if not target_to_reply:
                windows = get_all_line_windows()
                for t in targets:
                    for hwnd, title, cls, vis in windows:
                        if t.lower() in title.lower() and ("(" in title or "（" in title):
                            target_to_reply = t
                            break
                    if target_to_reply:
                        break

            # 最後備案：選擇目前不在冷卻中的對象
            if not target_to_reply:
                for t in targets:
                    if current_time - last_reply_times.get(t, 0) >= cooldown:
                        target_to_reply = t
                        break

            if target_to_reply:
                log_v(f" -> 觸發自動回覆長輩圖給:「{target_to_reply}」")
                send_grandma_image(target_to_reply, config)
                return

    # 2. 傳統視窗標題未讀標記 (備用保險)
    windows = get_all_line_windows()
    for target in targets:
        last_time = last_reply_times.get(target, 0)
        if current_time - last_time < cooldown:
            continue

        for hwnd, title, cls, vis in windows:
            if target.lower() in title.lower() and ("(" in title or "（" in title):
                log_v(f"\n[偵測] 發現「{target}」有未讀新訊息！(視窗: {title})")
                send_grandma_image(target, config)
                return

def main():
    parser = argparse.ArgumentParser(description=f"LINE 自動回覆長輩圖工具 v{VERSION}")
    parser.add_argument("-v", "--version", action="version", version=f"LINE Grandma Bot v{VERSION}")
    parser.add_argument("--test", metavar="NAME", help="手動測試發送一次給指定對象 (例如: --test 媽媽)")
    parser.add_argument("--crawl", type=int, metavar="COUNT", help="立刻上網抓取指定數量的長輩圖儲存備用 (例如: --crawl 20)")
    parser.add_argument("--list-images", action="store_true", help="列出當前讀取到的長輩圖清單")
    parser.add_argument("--clear-history", action="store_true", help="清空已發送長輩圖的歷史記錄")
    parser.add_argument("--scan-windows", action="store_true", help="掃描並印出當前桌面所有的 LINE 視窗資訊")
    args = parser.parse_args()

    config = load_config()

    if args.clear_history:
        if os.path.exists(HISTORY_PATH):
            os.remove(HISTORY_PATH)
        log_v("已成功清空長輩圖發送歷史記錄！")
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
            log_v("目前桌面上沒有偵測到任何 LINE 視窗，請確認 LINE 是否在運行。")
        for h, t, c, v in wins:
            print(f"HWND: {h} | Title: {t!r} | Class: {c!r} | Visible: {v}")
        return

    if args.test:
        target = args.test
        log_v(f"=== 手動測試模式：發送長輩圖給「{target}」===")
        send_grandma_image(target, config)
        return

    print("=" * 65)
    print(f"  LINE 電腦版 - 長輩圖智慧自動回覆程式 v{VERSION} (常駐背景)")
    print("=" * 65)
    print(f"程式版本 (version)       : v{VERSION}")
    print(f"監控對象名單 (targets)   : {config.get('targets', [])}")
    print(f"觸發模式 (trigger_mode)  : {config.get('trigger_mode', 'photo_only')} (photo_only = 只回照片)")
    print(f"隨機延遲範圍 (delay)     : {config.get('min_delay_seconds', 5)} ~ {config.get('max_delay_seconds', 25)} 秒")
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
