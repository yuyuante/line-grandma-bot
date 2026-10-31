"""
測試用推播工具：由 LINE 官方帳號 (Messaging API) 主動傳訊息給自己，模擬好友傳來長輩圖、貼圖或文字。

設定檔 (預設 ~/.line-test.env，可用環境變數 LINE_TEST_ENV 指定)：
    LINE_TOKEN=<Channel access token (long-lived)>
    LINE_USER_ID=<Your user ID，U 開頭>

用法：
    python tools/line_push_test.py info            # 核驗 token，顯示官方帳號名稱 (填入 config.json 的 targets)
    python tools/line_push_test.py image [檔名]     # 傳長輩圖 (預設從 images/ 隨機挑一張已推上 GitHub 的圖)
    python tools/line_push_test.py sticker         # 傳 LINE 貼圖
    python tools/line_push_test.py text [內容]      # 傳純文字 (程式應保持沉默、維持未讀)
    python tools/line_push_test.py stop            # 傳遠端停止指令「結束長輩圖機器人」
    python tools/line_push_test.py quota           # 查詢本月推播則數用量
"""
import json
import os
import random
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.line.me/v2/bot"
REPO_RAW = "https://raw.githubusercontent.com/yuyuante/line-grandma-bot/main/"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STOP_COMMAND = "結束長輩圖機器人"
# LINE 官方公開可傳送的貼圖 (package 446)
STICKERS = [("446", str(i)) for i in range(1988, 2028)]


def load_env():
    path = os.environ.get("LINE_TEST_ENV") or os.path.expanduser("~/.line-test.env")
    env = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except FileNotFoundError:
        sys.exit(f"[錯誤] 找不到設定檔 {path}，請先建立並填入 LINE_TOKEN 與 LINE_USER_ID。")
    for key in ("LINE_TOKEN", "LINE_USER_ID"):
        if not env.get(key):
            sys.exit(f"[錯誤] 設定檔 {path} 缺少 {key}。")
    return env


def call(env, method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {env['LINE_TOKEN']}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"[錯誤] LINE API 回應 {e.code}: {e.read().decode('utf-8', errors='replace')}")


def push(env, message):
    call(env, "POST", "/message/push", {"to": env["LINE_USER_ID"], "messages": [message]})


def published_images():
    """只挑已推上 GitHub 的圖，LINE 才抓得到 (raw.githubusercontent.com)"""
    out = subprocess.run(["git", "-C", ROOT, "ls-tree", "--name-only", "origin/main", "images/"],
                         capture_output=True, text=True, check=True).stdout
    return [p for p in out.split() if p.lower().endswith((".jpg", ".jpeg", ".png"))]


def main():
    args = sys.argv[1:]
    if not args or args[0] not in ("info", "image", "sticker", "text", "stop", "quota"):
        sys.exit(__doc__)
    env = load_env()
    cmd = args[0]

    if cmd == "info":
        info = call(env, "GET", "/info")
        print(f"token 有效。官方帳號名稱：「{info.get('displayName')}」(basicId: {info.get('basicId')})")
    elif cmd == "quota":
        quota = call(env, "GET", "/message/quota")
        used = call(env, "GET", "/message/quota/consumption")
        limit = quota.get("value", "無上限") if quota.get("type") == "limited" else "無上限"
        print(f"本月已推播 {used.get('totalUsage')} 則，上限 {limit}")
    elif cmd == "image":
        images = published_images()
        if len(args) > 1:
            images = [p for p in images if os.path.basename(p) == args[1]]
            if not images:
                sys.exit(f"[錯誤] origin/main 的 images/ 裡沒有 {args[1]}。")
        path = random.choice(images)
        url = REPO_RAW + urllib.parse.quote(path)
        push(env, {"type": "image", "originalContentUrl": url, "previewImageUrl": url})
        print(f"已傳送長輩圖：{os.path.basename(path)}")
    elif cmd == "sticker":
        pkg, sid = random.choice(STICKERS)
        push(env, {"type": "sticker", "packageId": pkg, "stickerId": sid})
        print(f"已傳送貼圖：{pkg}/{sid}")
    elif cmd == "text":
        text = " ".join(args[1:]) or "今天天氣不錯，有空回家吃飯"
        push(env, {"type": "text", "text": text})
        print(f"已傳送文字：{text}")
    elif cmd == "stop":
        push(env, {"type": "text", "text": STOP_COMMAND})
        print(f"已傳送停止指令：{STOP_COMMAND}")


if __name__ == "__main__":
    main()
