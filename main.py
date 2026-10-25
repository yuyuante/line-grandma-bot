"""主程式：監看 LINE 通知，對方傳長輩圖就回一張不一樣的。

用法：
    python main.py            # 持續監看
    python main.py --once     # 立刻跑一次「打開聊天室 → 判斷 → 回圖」，測試用
停止：在本資料夾建立名為 STOP 的檔案，或把滑鼠甩到螢幕左上角。
"""
import argparse
import logging
import random
import sys
import time
from datetime import datetime, timedelta

from grandma.config import load_config
from grandma.library import Library
from grandma.notify_watch import NotifyWatcher
from grandma.timing import in_quiet_hours, quiet_end

log = logging.getLogger("grandma")


class Bot:
    def __init__(self, cfg):
        from grandma import classifier, line_ui

        self.cfg = cfg
        self.classifier = classifier
        self.line_ui = line_ui
        self.lib = Library(cfg["db_path"], cfg["library_dir"])
        self.ui = line_ui.LineUI(cfg["templates_dir"], cfg["debug_dir"], cfg["ui"])
        self.stop_file = cfg["base_dir"] / "STOP"
        self.fire_at = None

    def _random_delay(self):
        return timedelta(seconds=random.uniform(*self.cfg["delay_range_sec"]))

    def run(self):
        watcher = NotifyWatcher(self.cfg["friend_name"], self.cfg["photo_keywords"], self.cfg["line_app_id_contains"])
        self.classifier.load()  # 先載入模型，收到圖時才不會卡很久
        log.info("開始監看「%s」（dry_run=%s，圖庫 %d 張）", self.cfg["friend_name"], self.cfg["dry_run"], self.lib.count())

        while True:
            if self.stop_file.exists():
                log.info("偵測到 STOP 檔案，結束")
                return
            if watcher.poll():
                if self.fire_at is None:
                    self.fire_at = datetime.now() + self._random_delay()
                    log.info("預計 %s 回覆", f"{self.fire_at:%H:%M:%S}")
                else:
                    log.info("等待期間又收到照片，合併成一次回覆")
            if self.fire_at and datetime.now() >= self.fire_at:
                self._try_fire()
            time.sleep(self.cfg["poll_interval_sec"])

    def _try_fire(self):
        now = datetime.now()
        start, end = self.cfg["quiet_hours"]
        if in_quiet_hours(now.hour, start, end):
            self.fire_at = quiet_end(now, start, end) + self._random_delay()
            log.info("勿擾時段，改到 %s 回覆", f"{self.fire_at:%m/%d %H:%M:%S}")
            return
        if self.lib.sent_today(now) >= self.cfg["daily_limit"]:
            log.info("今天已達回覆上限 %d 次，這次不回", self.cfg["daily_limit"])
            self.fire_at = None
            return
        if self.line_ui.user_idle_seconds() < self.cfg["user_idle_sec"]:
            self.fire_at = now + timedelta(seconds=30)  # 你正在用電腦，晚點再操作
            return

        self.fire_at = None
        self.reply_once()

    def reply_once(self):
        try:
            self._reply()
        except self.line_ui.pyautogui.FailSafeException:
            log.warning("偵測到滑鼠移到左上角，緊急停止")
            raise
        except Exception:
            log.exception("回覆失敗")
            self.ui.save_debug("error")

    def _reply(self):
        self.ui.open_chat(self.cfg["friend_name"])
        try:
            received = self.ui.capture_latest_image()
            if received is None:
                log.warning("聊天室裡找不到對方的圖片")
                return
            ok, score = self.classifier.is_grandma_image(received, self.cfg["clip_threshold"])
            log.info("長輩圖分數 %.2f（門檻 %.2f）", score, self.cfg["clip_threshold"])
            if not ok:
                log.info("不是長輩圖，不回")
                return

            path = self.lib.pick_different(received, self.cfg["phash_min_distance"], self.cfg["recent_exclude"])
            if path is None:
                log.warning("圖庫沒有可用的圖，請先執行 python -m grandma.crawler")
                return

            if self.cfg["dry_run"]:
                saved = self.ui.debug_dir / f"{datetime.now():%Y%m%d_%H%M%S}_dryrun_received.png"
                received.save(saved)
                log.info("[dry_run] 收到的圖存到 %s，原本會回 %s", saved, path)
                return

            self.ui.send_image(path)
            self.lib.mark_sent(path)
            log.info("已回覆 %s", path.name)
        finally:
            self.ui.close_chat()


def main():
    parser = argparse.ArgumentParser(description="LINE 長輩圖自動回覆")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--once", action="store_true", help="立刻跑一次回覆流程（忽略延遲、勿擾、上限）")
    args = parser.parse_args()

    if sys.platform != "win32":
        sys.exit("這個程式要用 Windows 的 Python 執行（不是 WSL）")

    cfg = load_config(args.config)
    cfg["debug_dir"].mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(cfg["base_dir"] / "data" / "bot.log", encoding="utf-8")],
    )

    bot = Bot(cfg)
    if args.once:
        bot.reply_once()
    else:
        bot.run()


if __name__ == "__main__":
    main()
