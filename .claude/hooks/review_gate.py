#!/usr/bin/env python3
"""
commit 前的審查關卡：commit 之前，/simplify、/code-review、/security-review 這三個審查都要跑過。

hook 只能執行 shell 指令，沒辦法直接呼叫 slash 技能，所以做成三個動作 (由 .claude/settings.json 掛上)：
    record  PostToolUse (Skill)       Claude 每執行一次上面三個技能就記一筆
    gate    PreToolUse  (Bash)        偵測到 `git commit`，三個審查沒跑齊就擋下，並說明缺哪幾個、要怎麼補
    clear   PostToolUse (Bash)        `git commit` 成功之後清空紀錄，下一輪重新計
另外兩個給人手動用：
    mark <名稱>   補記錄 (你自己在對話框手動打 /code-review 時不會經過 Claude 的 Skill 工具，紀錄不到)
    status        看目前記了哪些

紀錄放在 .claude/review-state.json (不進版控)，可用環境變數 REVIEW_GATE_STATE 指定別的位置 (測試用)。
任何非預期的錯誤都「放行」(只印在 stderr)：關卡壞掉不能讓你什麼都 commit 不了。
"""
import json
import os
import re
import sys
import time
from pathlib import Path

REQUIRED = ("simplify", "code-review", "security-review")
HOW_TO = {
    "simplify": "/simplify (整理這次改動的重複、多餘與可簡化之處)",
    "code-review": "/code-review (找 bug 與邏輯問題)",
    "security-review": "/security-review (檢查這次改動有沒有安全問題)",
}

# git commit：允許 git 與 commit 之間夾 -c key=val、-C 路徑、--git-dir=... 之類的選項 (例如 git -c user.name=x commit ...)
_GIT_COMMIT = re.compile(r"(?:^|[\s;&|(])git\s+(?:(?:-c\s+\S+|-C\s+\S+|--[\w-]+(?:=\S+)?)\s+)*commit(?=\s|$)")
_QUOTED = re.compile(r"\"(?:\\.|[^\"\\])*\"|'[^']*'")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2(?=\s|$)", re.DOTALL)


def state_path():
    override = os.environ.get("REVIEW_GATE_STATE")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "review-state.json"


def load_state():
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state):
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def is_commit(command):
    """這行指令裡有沒有真的 `git commit` (先拿掉引號裡的字與 heredoc 內容，免得 commit 訊息裡寫到 git commit 被誤判)"""
    if not isinstance(command, str) or not command:
        return False
    text = _HEREDOC.sub(" ", command)
    text = _QUOTED.sub('""', text)
    return bool(_GIT_COMMIT.search(text))


def missing(state):
    return [name for name in REQUIRED if name not in state]


def deny_reason(lacking):
    lines = ["commit 前還有審查沒跑，已擋下這次 git commit。請先執行下面還沒跑的審查，並處理它們找到的問題，再重新 commit："]
    lines += [f"  - {HOW_TO[name]}" for name in lacking]
    lines.append("用 Skill 工具執行即可 (會自動記錄)。如果使用者已經手動跑過，執行 "
                 "`python .claude/hooks/review_gate.py mark <名稱>` 補記錄。commit 成功後紀錄會清空，下一輪重新計。")
    return "\n".join(lines)


def cmd_gate(payload):
    """回傳要輸出的 JSON (擋下) 或 None (放行)"""
    command = (payload.get("tool_input") or {}).get("command", "")
    if not is_commit(command):
        return None
    lacking = missing(load_state())
    if not lacking:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": deny_reason(lacking)}}


def _stamp(name, args):
    """記下這個審查剛跑過，回傳更新後的狀態。"""
    state = load_state()
    state[name] = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "args": args}
    save_state(state)
    return state


def cmd_record(payload):
    tool_input = payload.get("tool_input") or {}
    name = str(tool_input.get("skill") or "").split(":")[-1].strip().lower()
    if name not in REQUIRED:
        return False
    _stamp(name, str(tool_input.get("args") or ""))
    return True


def cmd_clear(payload):
    command = (payload.get("tool_input") or {}).get("command", "")
    if not is_commit(command):
        return False
    if load_state():
        save_state({})
    return True


def cmd_mark(name):
    name = (name or "").strip().lower()
    if name not in REQUIRED:
        print(f"不認得「{name}」，可用：{', '.join(REQUIRED)}", file=sys.stderr)
        return 2
    state = _stamp(name, "manual")
    print(f"已記錄 {name}。還缺：{', '.join(missing(state)) or '無 (可以 commit 了)'}")
    return 0


def cmd_status():
    state = load_state()
    for name in REQUIRED:
        item = state.get(name)
        print(f"{name:16s} {'已跑 ' + str(item.get('at')) if isinstance(item, dict) else '還沒跑'}")
    return 0


def read_payload():
    try:
        data = json.loads(sys.stdin.read() or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def main(argv):
    action = argv[1] if len(argv) > 1 else ""
    try:
        if action == "gate":
            out = cmd_gate(read_payload())
            if out is not None:
                print(json.dumps(out, ensure_ascii=True))
        elif action == "record":
            cmd_record(read_payload())
        elif action == "clear":
            cmd_clear(read_payload())
        elif action == "mark":
            return cmd_mark(argv[2] if len(argv) > 2 else "")
        elif action == "status":
            return cmd_status()
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except Exception as e:                      # 關卡壞掉不能讓你什麼都 commit 不了：放行，只留一行在 stderr
        print(f"[review_gate] 發生非預期錯誤 ({action})，已放行：{e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
