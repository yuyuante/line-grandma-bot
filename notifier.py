"""
把重要事件推播到 LINE 官方帳號 (Messaging API)，讓你在聊天室裡看到程式的狀況 (啟動、回覆、停止、警告與錯誤)。

憑證放在 ~/.line-notify.env (可用環境變數 LINE_NOTIFY_ENV 指定；找不到時改讀 ~/.line-test.env)：
    LINE_TOKEN=<Channel access token (long-lived)>
    LINE_USER_ID=<Your user ID，U 開頭>

設計重點：
- 推播在背景執行緒送出，網路慢或失敗不會卡住監聽迴圈；任何錯誤只印在本機，不會往外擲出。
- 官方帳號每月有推播額度 (免費方案 200 則)，所以：警告與錯誤同類 30 分鐘最多推一次、全部每小時最多 6 則；
  本機另外記錄本月用量，達 monthly_cap 就不再推；LINE 回 429 (額度用完) 也視為已達上限。
- 完整的 log 仍留在本機，這裡只推重要事件。
"""
import json
import logging
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.request

API = "https://api.line.me/v2/bot"
HTTP_TIMEOUT = 10
MAX_TEXT = 500                      # 單則推播字數上限 (LINE 本身上限 5000)，長訊息截斷以保持聊天室易讀
DEFAULT_MONTHLY_CAP = 150           # 預設每月最多推播則數：留 50 則給手動測試或其他用途
ALERT_THROTTLE_SECONDS = 1800       # 同一類警告/錯誤最短間隔
ALERT_HOURLY_MAX = 6                # 警告/錯誤每小時總上限，避免不同訊息輪番洗版
WARN_INTERVAL_SECONDS = 600         # 本機「推播失敗」提示的最短間隔
ALERT_TAGS = ("[安全隔離]", "[警告]", "[錯誤]", "[例外防護]", "[停止指令待確認]", "❌")

_lock = threading.Lock()
_settings = {"enabled": False, "monthly_cap": DEFAULT_MONTHLY_CAP, "state_path": None}
_throttle = {}                      # {類別: 上次推播時間}
_alert_times = []                   # 近一小時內的警告/錯誤推播時間
_warned = {}                        # {提示類別: 上次印出時間}
_queue = queue.Queue()
_worker = None
_creds = None
_creds_checked = False
_synced = False                     # 本次執行是否已向 LINE 查過本月用量


def configure(enabled, monthly_cap=DEFAULT_MONTHLY_CAP, state_path=None):
    """更新設定 (主迴圈每輪都會呼叫，重複呼叫沒有副作用)"""
    with _lock:
        _settings["enabled"] = bool(enabled)
        _settings["monthly_cap"] = max(1, int(monthly_cap))
        if state_path:
            _settings["state_path"] = state_path


def _warn(key, msg):
    """只印在本機 (不用 logging，避免推播自己的錯誤又觸發推播)，同一類提示隔一段時間才印一次"""
    now = time.time()
    with _lock:
        if now - _warned.get(key, 0.0) < WARN_INTERVAL_SECONDS:
            return
        _warned[key] = now
    try:
        print(f"[通知] {msg}")
    except Exception:
        pass


def _load_creds():
    global _creds, _creds_checked
    if _creds_checked:
        return _creds
    _creds_checked = True
    explicit = os.environ.get("LINE_NOTIFY_ENV")
    paths = [explicit] if explicit else [os.path.expanduser("~/.line-notify.env"), os.path.expanduser("~/.line-test.env")]
    for path in paths:
        env = {}
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        env[k.strip()] = v.strip()
        except OSError:
            continue
        if env.get("LINE_TOKEN") and env.get("LINE_USER_ID"):
            _creds = {"token": env["LINE_TOKEN"], "user_id": env["LINE_USER_ID"]}
            return _creds
    _warn("creds", "已開啟 notify_log，但找不到 LINE_TOKEN 與 LINE_USER_ID (~/.line-notify.env)，不會推播。")
    return None


def _call(creds, method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {creds['token']}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def _load_state():
    month = time.strftime("%Y-%m")
    path = _settings["state_path"]
    state = {"month": month, "count": 0}
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            if saved.get("month") == month and isinstance(saved.get("count"), int):
                state = {"month": month, "count": saved["count"]}
        except (OSError, ValueError):
            pass
    return state


def _save_state(state):
    path = _settings["state_path"]
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass


def _deliver(text):
    global _synced
    creds = _load_creds()
    if creds is None:
        return
    cap = _settings["monthly_cap"]
    state = _load_state()
    if not _synced:
        _synced = True
        try:
            used = _call(creds, "GET", "/message/quota/consumption").get("totalUsage")
            if isinstance(used, int):
                state["count"] = max(state["count"], used)     # 手動測試推的也算，以較大者為準
        except Exception:
            pass                                               # 查不到就只信本機記錄
    if state["count"] >= cap:
        _warn("cap", f"本月推播已達上限 ({cap} 則)，之後只記在本機，下個月恢復。")
        _save_state(state)
        return
    try:
        _call(creds, "POST", "/message/push", {"to": creds["user_id"], "messages": [{"type": "text", "text": text}]})
    except urllib.error.HTTPError as e:
        if e.code == 429:
            state["count"] = cap                               # LINE 說額度用完，本月不再嘗試
            _save_state(state)
            _warn("quota", "LINE 回報本月推播額度已用完，本月不再推播。")
        else:
            _warn("http", f"推播失敗：LINE API 回應 {e.code}。請確認 LINE_TOKEN 是否有效。")
        return
    state["count"] += 1
    _save_state(state)


def _run():
    while True:
        text = _queue.get()
        try:
            _deliver(text)
        except Exception as e:
            _warn("deliver", f"推播失敗：{e}")
        finally:
            _queue.task_done()


def _ensure_worker():
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="notifier", daemon=True)
            _worker.start()


def notify(text, key=None, throttle=0.0, alert=False):
    """
    把訊息排進推播佇列 (非同步，立刻返回)。回傳是否排入：未啟用、被節流、超過每小時警告上限都回傳 False。
    key 與 throttle：同一個 key 在 throttle 秒內只排入一次。alert 為 True 時另計每小時總上限。
    """
    if not _settings["enabled"]:
        return False
    now = time.time()
    with _lock:
        if key is not None and now - _throttle.get(key, -1e18) < throttle:
            return False
        if alert:
            _alert_times[:] = [t for t in _alert_times if now - t < 3600]
            if len(_alert_times) >= ALERT_HOURLY_MAX:
                return False
            _alert_times.append(now)
        if key is not None:
            _throttle[key] = now
    text = f"[{time.strftime('%H:%M:%S', time.localtime(now))}] {text}"
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT - 1] + "…"
    _ensure_worker()
    _queue.put(text)
    return True


def on_log(msg, severe=False):
    """
    判斷一行 log 要不要推播：以警告/錯誤標籤開頭的 (或 severe 為 True，例如 logging 的 WARNING 以上) 才推。
    類別鍵把數字與雜湊 (快取頻道代碼等) 換成 #，所以同一種訊息不論內容細節都算同一類，30 分鐘只推一次。
    """
    if not _settings["enabled"]:
        return
    body = (msg or "").strip()
    if not body or not (severe or body.startswith(ALERT_TAGS)):
        return
    key = re.sub(r"[0-9a-fA-F]{6,}|\d+(?:\.\d+)?", "#", body)[:40]
    notify(f"⚠️ {body}", key=key, throttle=ALERT_THROTTLE_SECONDS, alert=True)


def flush(timeout=5.0):
    """等佇列送完 (程式即將結束時呼叫)，最多等 timeout 秒；回傳是否已送完"""
    deadline = time.time() + timeout
    while _queue.unfinished_tasks and time.time() < deadline:
        time.sleep(0.05)
    return not _queue.unfinished_tasks


class LogHandler(logging.Handler):
    """把 logging 的 WARNING 以上轉給 on_log"""
    def emit(self, record):
        try:
            if record.levelno >= logging.WARNING:
                on_log(record.getMessage(), severe=True)
        except Exception:
            pass


def install_logging_handler():
    root = logging.getLogger()
    if not any(isinstance(h, LogHandler) for h in root.handlers):
        root.addHandler(LogHandler(level=logging.WARNING))
