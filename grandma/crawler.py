"""從 Bing 圖片搜尋抓長輩圖，過濾後存進圖庫。

用法：python -m grandma.crawler --count 200
"""
import argparse
import logging
import math
import tempfile
import uuid
from pathlib import Path

from PIL import Image

from grandma import classifier
from grandma.config import load_config
from grandma.library import Library, phash

log = logging.getLogger(__name__)

MAX_SIDE = 1600


def ingest(path, keyword, lib, cfg) -> bool:
    """檢查一張下載的圖，合格就存進圖庫。"""
    try:
        with Image.open(path) as im:
            img = im.convert("RGB")
    except Exception:
        return False

    if min(img.size) < cfg["crawler"]["min_side"]:
        return False
    h = phash(img)
    if lib.has_similar(h, cfg["crawler"]["dup_distance"]):
        return False
    ok, score = classifier.is_grandma_image(img, cfg["clip_threshold"])
    if not ok:
        return False

    img.thumbnail((MAX_SIDE, MAX_SIDE))
    filename = f"{uuid.uuid4().hex}.jpg"
    img.save(lib.library_dir / filename, "JPEG", quality=90)
    lib.add(filename, h, keyword, score)
    return True


def crawl(cfg, total):
    from icrawler.builtin import BingImageCrawler

    lib = Library(cfg["db_path"], cfg["library_dir"])
    keywords = cfg["crawler"]["keywords"]
    per_keyword = math.ceil(total / len(keywords))
    classifier.load()

    added = 0
    for kw in keywords:
        with tempfile.TemporaryDirectory() as tmp:
            crawler = BingImageCrawler(downloader_threads=4, storage={"root_dir": tmp}, log_level=logging.WARNING)
            crawler.crawl(keyword=kw, max_num=per_keyword)
            files = sorted(Path(tmp).iterdir())
            kept = sum(ingest(f, kw, lib, cfg) for f in files)
        log.info("「%s」下載 %d 張，收錄 %d 張", kw, len(files), kept)
        added += kept

    log.info("完成：新增 %d 張，圖庫共 %d 張", added, lib.count())


def main():
    parser = argparse.ArgumentParser(description="爬長輩圖建圖庫")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--count", type=int, default=100, help="這次總共要下載幾張（過濾前）")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    crawl(load_config(args.config), args.count)


if __name__ == "__main__":
    main()
