# -*- coding: utf-8 -*-
"""
長輩圖自動下載工具 (crawl_images.py)
- 精準爬取台灣經典正統長輩圖 (早安圖、平安喜樂、每日問候、蓮花佛語)
- 來源直連台灣知名 Formosa 貼圖酷每日問候專屬圖庫 (收錄超過 1,000 幅高畫質長輩圖)
- 自動過濾破損、過小縮圖與重複圖片 (MD5 去重)
- 支援定時 TTL 自動清理與發送後汰舊換新
"""

import os
import re
import sys
import time
import random
import hashlib
import urllib.parse
import urllib.request
import argparse
from PIL import Image

VERSION = "2.5.1"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_IMAGES_DIR = os.path.join(SCRIPT_DIR, "images")

def log_crawl(msg):
    """印出包含時間與版本號碼的爬蟲訊息"""
    now = time.strftime("%H:%M:%S")
    prefix = ""
    if msg.startswith("\n"):
        prefix = "\n"
        msg = msg[1:]
    print(f"{prefix}[{now}][v{VERSION}] {msg}")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

def get_existing_md5s(target_dir):
    """計算目前已存在圖片的 MD5，避免重複儲存"""
    md5s = set()
    if not os.path.exists(target_dir):
        return md5s
    for root, _, files in os.walk(target_dir):
        for f in files:
            if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.bmp')):
                p = os.path.join(root, f)
                try:
                    with open(p, "rb") as fp:
                        md5s.add(hashlib.md5(fp.read()).hexdigest())
                except Exception:
                    pass
    return md5s

def clean_expired_images(images_dir=DEFAULT_IMAGES_DIR, ttl_days=7):
    """定時清理超過 TTL 天數的舊長輩圖 (保護使用者自備圖)"""
    if not os.path.exists(images_dir) or ttl_days <= 0:
        return 0
    now = time.time()
    max_age_seconds = ttl_days * 86400
    cleaned_count = 0

    for f in os.listdir(images_dir):
        if f.startswith("grandma_") and f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.bmp')):
            path = os.path.join(images_dir, f)
            try:
                mtime = os.path.getmtime(path)
                if now - mtime > max_age_seconds:
                    os.remove(path)
                    cleaned_count += 1
                    log_crawl(f" -> [淘汰過期圖] {f} (存放超過 {ttl_days} 天)")
            except Exception as e:
                log_crawl(f"[警告] 清理檔案 {f} 失敗: {e}")

    if cleaned_count > 0:
        log_crawl(f"[圖庫維護] 本次共清理淘汰了 {cleaned_count} 張過期長輩圖。")
    return cleaned_count

def retire_image(image_path, images_dir=DEFAULT_IMAGES_DIR):
    """送出後淘汰此張長輩圖，避免日後重複發送 (保護非 grandma_ 開頭的使用者自備圖)"""
    if not os.path.exists(image_path):
        return False
    filename = os.path.basename(image_path)
    if filename.startswith("grandma_"):
        try:
            os.remove(image_path)
            log_crawl(f" -> [自動換新] 剛送出的「{filename}」已完成使命並淘汰，騰出庫存位。")
            return True
        except Exception as e:
            log_crawl(f"[警告] 移除已發送圖片失敗: {e}")
            return False
    return False

def download_and_validate_image(url, save_dir, existing_md5s):
    """下載圖片並使用 Pillow 驗證其有效性與尺寸"""
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = resp.read(8 * 1024 * 1024 + 1)
    except Exception:
        return None

    # 過濾過小檔案 (< 20KB) 或過大檔案 (> 8MB)
    if len(data) < 20 * 1024 or len(data) > 8 * 1024 * 1024:
        return None

    # MD5 去重
    img_md5 = hashlib.md5(data).hexdigest()
    if img_md5 in existing_md5s:
        return None

    temp_path = os.path.join(save_dir, f"_temp_{img_md5}.tmp")
    try:
        with open(temp_path, "wb") as f:
            f.write(data)
        with Image.open(temp_path) as img:
            w, h = img.size
            fmt = img.format.lower()
            if w < 300 or h < 300:
                os.remove(temp_path)
                return None
            ext = ".jpg" if fmt in ("jpeg", "jpg") else f".{fmt}"
        
        final_filename = f"grandma_{int(time.time())}_{img_md5[:8]}{ext}"
        final_path = os.path.join(save_dir, final_filename)
        os.replace(temp_path, final_path)
        existing_md5s.add(img_md5)
        return final_path
    except Exception:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        return None

# 圖庫來源：url 為含 {n} 的網址範本，n 取 start~end (含)。category 選填，表示這個來源的圖適用的時段
# (morning / noon / evening / neutral)；有填就不必靠 OCR 判斷，例如「平安祝福」類的來源可填 neutral。
# 預設是 Formosa 貼圖酷的每日問候攝影與插畫 (這兩類圖上都有早安、午安或晚安字樣，所以不填 category)。
# 節慶類 (festival-*) 是特定節日的圖 (父親節、中秋節)，note-short 是短影片，都不適合當通用圖，沒有收進預設。
DEFAULT_IMAGE_SOURCES = [
    {"name": "fpg-daily-photo", "url": "https://sticker.fpg.com.tw/sticker/daily-photo/daily-photo-{n}.jpg", "start": 100, "end": 749},
    {"name": "fpg-daily-illustration", "url": "https://sticker.fpg.com.tw/sticker/daily-illustration/daily-illustration-{n}.jpg", "start": 30, "end": 244},
]
SOURCE_CATEGORIES = ("morning", "noon", "evening", "neutral")
MAX_SOURCE_SPAN = 5000              # 單一來源最多展開幾個編號，避免設定打錯數字展開成百萬個網址

def normalize_sources(sources):
    """
    檢查並整理 config 的 image_sources：每項需有含 {n} 的 http(s) url 與整數 start <= end；
    category 只接受 morning / noon / evening / neutral。不合格的項目略過；清單空或不是清單時用預設來源。
    回傳 (來源清單, 被略過的原因清單)。
    """
    if not isinstance(sources, list) or not sources:
        return [dict(s) for s in DEFAULT_IMAGE_SOURCES], []
    good, skipped = [], []
    for i, src in enumerate(sources):
        label = (src.get("name") if isinstance(src, dict) else None) or f"#{i + 1}"
        try:
            url = src["url"]
            start, end = int(src["start"]), int(src["end"])
            if not (isinstance(url, str) and url.startswith(("http://", "https://")) and "{n}" in url):
                raise ValueError("url 必須是含 {n} 的 http(s) 網址")
            if start > end or end - start + 1 > MAX_SOURCE_SPAN:
                raise ValueError(f"start~end 必須是 1 到 {MAX_SOURCE_SPAN} 個編號")
            category = src.get("category")
            if category is not None and category not in SOURCE_CATEGORIES:
                raise ValueError("category 只能是 morning/noon/evening/neutral")
            pad = int(src.get("pad", 0))
            good.append({"name": str(label), "url": url, "start": start, "end": end, "category": category, "pad": max(0, pad)})
        except Exception as e:
            skipped.append(f"來源「{label}」略過：{e}")
    if not good:
        return [dict(s) for s in DEFAULT_IMAGE_SOURCES], skipped
    return good, skipped

def build_candidates(sources=None):
    """把所有來源展開成隨機排序的 (網址, 類別或 None) 清單；各來源的編號先各自洗牌再交錯混合，避免一個來源獨佔。"""
    srcs, _ = normalize_sources(sources)
    pools = []
    for src in srcs:
        ids = list(range(src["start"], src["end"] + 1))
        random.shuffle(ids)
        pad = src.get("pad", 0)
        pools.append([(src["url"].replace("{n}", str(i).zfill(pad)), src.get("category")) for i in ids])
    merged = [c for pool in pools for c in pool]
    random.shuffle(merged)
    return merged

def build_candidate_urls(sources=None):
    """相容舊介面：只回傳隨機排序的候選網址"""
    return [url for url, _ in build_candidates(sources)]

FESTIVAL_LISTING_URL = "https://sticker.fpg.com.tw/festival_all.aspx"
FESTIVAL_BASE_URL = "https://sticker.fpg.com.tw/"
_FESTIVAL_IMAGE_RE = re.compile(r'data-original="(sticker/festival-(?:illustration|photo)/[a-z-]+-\d+\.jpg)"')
FESTIVAL_CACHE_SECONDS = 3600
_festival_cache = {"time": 0.0, "urls": []}

def fetch_festival_candidates(listing_url=FESTIVAL_LISTING_URL, now=None):
    """
    取得節慶圖的候選清單 [(網址, None)]，隨機排序。節日圖的編號不連續 (只有部分編號存在)，所以要先讀列表頁
    取得有效的圖片網址；festival-short 是影片 (6~10MB)，不收。一小時內重複呼叫用快取，抓不到回傳空清單。
    """
    now = time.time() if now is None else now
    if _festival_cache["urls"] and now - _festival_cache["time"] < FESTIVAL_CACHE_SECONDS:
        urls = list(_festival_cache["urls"])
    else:
        try:
            req = urllib.request.Request(listing_url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read(4 * 1024 * 1024).decode("utf-8", errors="ignore")
        except Exception as e:
            log_crawl(f"[警告] 讀取節慶圖列表失敗: {e}")
            return []
        paths = sorted(set(_FESTIVAL_IMAGE_RE.findall(html)))
        urls = [FESTIVAL_BASE_URL + p for p in paths]
        if urls:
            _festival_cache.update({"time": now, "urls": urls})
    random.shuffle(urls)
    return [(u, None) for u in urls]

def crawl_grandma_images(target_count=10, keywords=None, save_dir=DEFAULT_IMAGES_DIR, sources=None, on_saved=None):
    """
    從長輩圖圖庫隨機抓取全新長輩圖。來源由 sources 指定 (見 DEFAULT_IMAGE_SOURCES，預設是 Formosa 貼圖酷的
    每日問候攝影與插畫)。on_saved(路徑, 該來源的類別或 None) 在每張圖存檔後呼叫，回傳 False 表示不要這張圖 (已被呼叫端捨棄，不計入張數)。
    """
    os.makedirs(save_dir, exist_ok=True)
    existing_md5s = get_existing_md5s(save_dir)
    log_crawl(f"\n=== 開始下載正統台灣長輩圖 (目標新增: {target_count} 張) ===")
    log_crawl(f"儲存目錄: {save_dir}")

    candidates = build_candidates(sources)

    # 限制單次最大嘗試數，避免無網路時長時間卡死阻塞
    candidates = candidates[:max(target_count * 4, 30)]

    downloaded_count = 0
    consecutive_fails = 0
    for u, category in candidates:
        if downloaded_count >= target_count:
            break
        if consecutive_fails >= 10:
            log_crawl(" -> [網路保護] 連續失敗超過 10 次，觸發保護熔斷停止本次下載。")
            break
        saved = download_and_validate_image(u, save_dir, existing_md5s)
        if saved:
            consecutive_fails = 0
            if on_saved:
                try:
                    if on_saved(saved, category) is False:      # 呼叫端回傳 False = 這張圖不要 (例如辨識不出來，已捨棄)，不計入張數
                        continue
                except Exception as e:
                    log_crawl(f"[警告] 處理下載的圖片失敗: {e}")
            downloaded_count += 1
            log_crawl(f" -> [{downloaded_count}/{target_count}] 成功下載正統長輩圖: {os.path.basename(saved)}")
            time.sleep(0.2)
        else:
            consecutive_fails += 1

    log_crawl(f"下載完成！共收納 {downloaded_count} 張正統長輩圖進圖庫備用。")
    return downloaded_count

def main():
    parser = argparse.ArgumentParser(description=f"正統台灣長輩圖自動下載工具 v{VERSION}")
    parser.add_argument("-v", "--version", action="version", version=f"Crawl Images v{VERSION}")
    parser.add_argument("--count", type=int, default=10, help="要下載的圖片數量 (預設: 10)")
    parser.add_argument("--dir", type=str, default=DEFAULT_IMAGES_DIR, help="儲存目錄 (預設: images/)")
    args = parser.parse_args()

    crawl_grandma_images(target_count=args.count, save_dir=args.dir)

if __name__ == "__main__":
    main()
