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

VERSION = "2.3.0"

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

def build_candidate_urls():
    """隨機混合攝影類 (1~750) 與插畫類 (30~245) 長輩圖候選網址"""
    photo_ids = list(range(100, 750))
    random.shuffle(photo_ids)
    illust_ids = list(range(30, 245))
    random.shuffle(illust_ids)
    urls = [f"https://sticker.fpg.com.tw/sticker/daily-photo/daily-photo-{i}.jpg" for i in photo_ids]
    urls += [f"https://sticker.fpg.com.tw/sticker/daily-illustration/daily-illustration-{i}.jpg" for i in illust_ids]
    random.shuffle(urls)
    return urls

def crawl_grandma_images(target_count=10, keywords=None, save_dir=DEFAULT_IMAGES_DIR):
    """
    從正統台灣長輩圖專屬圖庫隨機抓取全新長輩圖：
    1. Formosa 貼圖酷每日問候攝影圖庫 (1~750)
    2. Formosa 貼圖酷精選問候插畫圖庫 (1~250)
    3. 保證 100% 均為正統早安、平安、喜樂、祝福長輩圖，杜絕任何無關雜圖
    """
    os.makedirs(save_dir, exist_ok=True)
    existing_md5s = get_existing_md5s(save_dir)
    log_crawl(f"\n=== 開始下載正統台灣長輩圖 (目標新增: {target_count} 張) ===")
    log_crawl(f"儲存目錄: {save_dir}")

    candidate_urls = build_candidate_urls()

    # 限制單次最大嘗試數，避免無網路時長時間卡死阻塞
    candidate_urls = candidate_urls[:max(target_count * 4, 30)]

    downloaded_count = 0
    consecutive_fails = 0
    for u in candidate_urls:
        if downloaded_count >= target_count:
            break
        if consecutive_fails >= 10:
            log_crawl(" -> [網路保護] 連續失敗超過 10 次，觸發保護熔斷停止本次下載。")
            break
        saved = download_and_validate_image(u, save_dir, existing_md5s)
        if saved:
            downloaded_count += 1
            consecutive_fails = 0
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
