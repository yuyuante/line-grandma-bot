"""commit 前的審查關卡 (.claude/hooks/review_gate.py) 的測試"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE_PATH = os.path.join(ROOT, ".claude", "hooks", "review_gate.py")
spec = importlib.util.spec_from_file_location("review_gate", GATE_PATH)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state.json")
        patcher = unittest.mock.patch.dict(os.environ, {"REVIEW_GATE_STATE": self.state})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def bash(self, command):
        return {"tool_name": "Bash", "tool_input": {"command": command}}

    def skill(self, name, args=""):
        return {"tool_name": "Skill", "tool_input": {"skill": name, "args": args}}


import unittest.mock  # noqa: E402  (上面的 patch.dict 要用)


class TestIsCommit(unittest.TestCase):
    def test_real_commits_are_detected(self):
        for command in (
            "git commit -m hi",
            "git commit",
            "git -c user.name=yuyuante -c user.email=a@b.c commit -F msg.txt",
            "git add . && git commit -m x",
            "cd repo; git commit --amend",
            "git add a b && git -c user.name=x commit -F f && git push origin main",
            "(git commit -m x)",
            "git -C some/dir commit -m x",
            "git --no-pager commit -m x",
        ):
            self.assertTrue(gate.is_commit(command), command)

    def test_other_git_commands_are_not_commits(self):
        for command in (
            "git status", "git log --oneline", "git commit-tree abc", "git push origin main", "git diff --stat HEAD",
            "git fetch origin", "git add -A", "ls", "python -m unittest", "", None,
        ):
            self.assertFalse(gate.is_commit(command), command)

    def test_the_words_inside_quotes_or_heredocs_do_not_count(self):
        # commit 訊息或說明文字裡提到 git commit 不能被誤判
        self.assertFalse(gate.is_commit('git log --grep="git commit"'))
        self.assertFalse(gate.is_commit("git log --grep='git commit'"))
        self.assertFalse(gate.is_commit('echo "請先 git commit 再推送"'))
        self.assertFalse(gate.is_commit("cat <<'EOF'\n先 git commit 再 push\nEOF"))
        self.assertTrue(gate.is_commit("git commit -m \"fix: 說明\" && echo done"))


class TestGate(_Base):
    def test_a_commit_is_blocked_until_all_three_reviews_ran(self):
        out = gate.cmd_gate(self.bash("git commit -m x"))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        for name in gate.REQUIRED:
            self.assertIn(gate.HOW_TO[name], reason)

    def test_only_the_missing_reviews_are_listed(self):
        gate.cmd_record(self.skill("code-review"))
        gate.cmd_record(self.skill("simplify"))
        reason = gate.cmd_gate(self.bash("git commit -m x"))["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn(gate.HOW_TO["security-review"], reason)
        self.assertNotIn(gate.HOW_TO["code-review"], reason)
        self.assertNotIn(gate.HOW_TO["simplify"], reason)

    def test_a_commit_is_allowed_once_all_three_ran(self):
        for name in gate.REQUIRED:
            gate.cmd_record(self.skill(name))
        self.assertIsNone(gate.cmd_gate(self.bash("git commit -m x")))

    def test_commands_that_are_not_commits_are_never_blocked(self):
        self.assertIsNone(gate.cmd_gate(self.bash("git status")))
        self.assertIsNone(gate.cmd_gate(self.bash("git push origin main")))
        self.assertIsNone(gate.cmd_gate({"tool_name": "Bash", "tool_input": {}}))
        self.assertIsNone(gate.cmd_gate({}))

    def test_the_deny_message_says_how_to_record_a_manual_run(self):
        reason = gate.cmd_gate(self.bash("git commit -m x"))["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("review_gate.py mark", reason)


class TestRecordAndClear(_Base):
    def test_only_the_three_reviews_are_recorded(self):
        self.assertTrue(gate.cmd_record(self.skill("security-review")))
        self.assertFalse(gate.cmd_record(self.skill("update-config")))
        self.assertFalse(gate.cmd_record({"tool_name": "Skill", "tool_input": {}}))
        self.assertEqual(list(gate.load_state()), ["security-review"])

    def test_a_plugin_prefix_and_letter_case_are_ignored(self):
        self.assertTrue(gate.cmd_record(self.skill("some-plugin:Code-Review")))
        self.assertIn("code-review", gate.load_state())

    def test_the_arguments_and_time_are_kept(self):
        gate.cmd_record(self.skill("code-review", "high"))
        item = gate.load_state()["code-review"]
        self.assertEqual(item["args"], "high")
        self.assertRegex(item["at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_a_successful_commit_clears_the_records(self):
        for name in gate.REQUIRED:
            gate.cmd_record(self.skill(name))
        self.assertTrue(gate.cmd_clear(self.bash("git add . && git commit -m x")))
        self.assertEqual(gate.load_state(), {})
        self.assertIsNotNone(gate.cmd_gate(self.bash("git commit -m y")))               # 下一輪又要重新跑

    def test_other_commands_do_not_clear_the_records(self):
        gate.cmd_record(self.skill("simplify"))
        self.assertFalse(gate.cmd_clear(self.bash("git push origin main")))
        self.assertFalse(gate.cmd_clear(self.bash("git status")))
        self.assertIn("simplify", gate.load_state())

    def test_mark_records_a_manual_run_and_rejects_unknown_names(self):
        self.assertEqual(gate.cmd_mark("code-review"), 0)
        self.assertEqual(gate.cmd_mark("  SIMPLIFY "), 0)
        self.assertEqual(gate.cmd_mark("lint"), 2)
        self.assertEqual(sorted(gate.load_state()), ["code-review", "simplify"])
        self.assertEqual(gate.load_state()["code-review"]["args"], "manual")

    def test_a_broken_state_file_counts_as_no_records(self):
        with open(self.state, "w", encoding="utf-8") as f:
            f.write("{ not json")
        self.assertEqual(gate.load_state(), {})
        with open(self.state, "w", encoding="utf-8") as f:
            f.write("[1, 2]")
        self.assertEqual(gate.load_state(), {})
        self.assertEqual(gate.missing({}), list(gate.REQUIRED))


class TestAsAHook(_Base):
    """像真的 hook 一樣用子行程執行：stdin 給 JSON，檢查輸出與結束碼"""

    def run_hook(self, action, payload=None, raw=None):
        env = dict(os.environ, REVIEW_GATE_STATE=self.state, PYTHONIOENCODING="utf-8")
        data = raw if raw is not None else json.dumps(payload or {})
        return subprocess.run([sys.executable, "-X", "utf8", GATE_PATH, action], input=data, capture_output=True,
                              text=True, encoding="utf-8", env=env, timeout=30)

    def test_a_blocked_commit_prints_the_deny_json_and_exits_zero(self):
        result = self.run_hook("gate", self.bash("git commit -m x"))
        self.assertEqual(result.returncode, 0)
        out = json.loads(result.stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_the_output_is_ascii_safe_so_any_console_encoding_works(self):
        result = self.run_hook("gate", self.bash("git commit -m x"))
        result.stdout.encode("ascii")                                                   # 中文都被跳脫成 \\uXXXX

    def test_an_allowed_command_prints_nothing(self):
        result = self.run_hook("gate", self.bash("git status"))
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_record_then_gate_then_clear_work_across_processes(self):
        for name in gate.REQUIRED:
            self.run_hook("record", self.skill(name))
        self.assertEqual(self.run_hook("gate", self.bash("git commit -m x")).stdout, "")
        self.run_hook("clear", self.bash("git commit -m x"))
        self.assertNotEqual(self.run_hook("gate", self.bash("git commit -m x")).stdout, "")

    def test_garbage_input_never_blocks(self):
        for raw in ("not json", "", "[1,2,3]", "null"):
            result = self.run_hook("gate", raw=raw)
            self.assertEqual((result.returncode, result.stdout), (0, ""), raw)

    def test_an_unexpected_error_lets_the_commit_through(self):
        # 狀態檔位置是個資料夾 (讀不了也寫不了)：關卡壞掉不能讓你什麼都 commit 不了
        os.makedirs(self.state)
        result = self.run_hook("record", self.skill("simplify"))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.run_hook("gate", self.bash("git status")).stdout, "")

    def test_an_unknown_action_is_a_usage_error(self):
        self.assertEqual(self.run_hook("bogus").returncode, 2)

    def test_status_and_mark_commands(self):
        self.assertEqual(self.run_hook("mark", raw="").returncode, 2)                   # 沒給名稱
        status = self.run_hook("status")
        self.assertEqual(status.returncode, 0)
        for name in gate.REQUIRED:
            self.assertIn(name, status.stdout)


class TestSettings(unittest.TestCase):
    """專案設定 (.claude/settings.json) 要真的把三個動作掛上去"""

    def setUp(self):
        with open(os.path.join(ROOT, ".claude", "settings.json"), encoding="utf-8") as f:
            self.hooks = json.load(f)["hooks"]

    def commands(self, event, matcher):
        return [h["command"] for entry in self.hooks.get(event, []) if entry.get("matcher") == matcher for h in entry["hooks"]]

    def test_the_three_actions_are_wired(self):
        self.assertTrue(any(c.endswith(" gate") for c in self.commands("PreToolUse", "Bash")))
        self.assertTrue(any(c.endswith(" record") for c in self.commands("PostToolUse", "Skill")))
        self.assertTrue(any(c.endswith(" clear") for c in self.commands("PostToolUse", "Bash")))

    def test_the_commands_point_at_the_script_in_the_project(self):
        for event, matcher in (("PreToolUse", "Bash"), ("PostToolUse", "Skill"), ("PostToolUse", "Bash")):
            for command in self.commands(event, matcher):
                self.assertIn("$CLAUDE_PROJECT_DIR/.claude/hooks/review_gate.py", command)

    def test_the_git_hooks_only_run_for_git_commands(self):
        for event in ("PreToolUse", "PostToolUse"):
            for entry in self.hooks[event]:
                if entry["matcher"] == "Bash":
                    for hook in entry["hooks"]:
                        self.assertEqual(hook.get("if"), "Bash(git *)")

    def test_the_state_file_is_not_tracked(self):
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
            self.assertIn(".claude/review-state.json", f.read())


if __name__ == "__main__":
    unittest.main()
