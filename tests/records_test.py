#!/usr/bin/env python3
"""後から数える記録（lib/eventlog.py）と、それを書く claude-guard・kitty の watcher のテスト。標準ライブラリだけ。

claude-state の hook が書く分（状態の移り・メッセージ・タスクの札）は tests/run.py の ClaudeStateCli にある
（偽の kitten を使うため）。"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "kitty"))
import eventlog as E  # noqa: E402

GUARD_BIN = ROOT / "bin" / "claude-guard"
LOCAL_ISO = r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$"


def lines(p):
    return [json.loads(x) for x in Path(p).read_text().splitlines()]


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.env = {"XDG_STATE_HOME": str(self.d / "state"), "HOME": str(self.d / "home")}

    def tearDown(self):
        self.tmp.cleanup()


class Append(Tmp):
    def test_writes_one_line_with_local_time_and_private_modes(self):
        self.assertTrue(E.append(self.env, "transition", {"role": "dev-a", "sid": "s", "from": None, "to": "working"}))
        p = self.d / "state" / "claude-kitty" / "state" / "transitions.jsonl"
        rec, = lines(p)
        self.assertRegex(rec["at"], LOCAL_ISO)
        self.assertEqual(list(rec)[0], "at")  # 時刻が頭
        self.assertEqual(rec["to"], "working")
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(p.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(p.parent.parent.stat().st_mode), 0o700)

    def test_local_time_has_the_offset(self):
        import time
        old = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Tokyo"
        time.tzset()
        try:
            self.assertEqual(E.local_time(0), "1970-01-01T09:00:00+09:00")
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

    def test_home_is_the_fallback_and_nothing_without_either(self):
        env = {"HOME": str(self.d / "h")}
        self.assertTrue(E.append(env, "task", {"id": "1"}))
        self.assertTrue((self.d / "h" / ".local" / "state" / "claude-kitty" / "state" / "tasks.jsonl").is_file())
        self.assertFalse(E.append({}, "task", {"id": "1"}))  # 置き場が決まらなければ書かない（本物の置き場に倒さない）

    def test_rotates_past_the_limit(self):
        with mock.patch.object(E, "LOG_MAX", 100):
            for i in range(5):
                E.append(self.env, "msg-in", {"n": i, "pad": "x" * 40})
        p = E.path_of(self.env, "msg-in")
        old = p.with_name(p.name + ".1")
        self.assertTrue(old.is_file())
        allrecs = lines(old) + lines(p)
        self.assertLess(len(allrecs), 5)            # 2 回目の回しで最初の分は消える
        self.assertEqual(allrecs[-1]["n"], 4)       # 最後の行は今のファイルに
        self.assertLessEqual(p.stat().st_size, 200)

    def test_unwritable_place_is_silent(self):
        (self.d / "state").write_text("not a folder")
        self.assertFalse(E.append(self.env, "guard-block", {"x": 1}))
        ro = self.d / "ro"
        (ro / "claude-kitty" / "guard").mkdir(parents=True)
        (ro / "claude-kitty" / "guard" / "blocks.jsonl").mkdir()  # ファイルの名前にフォルダ
        self.assertFalse(E.append({"XDG_STATE_HOME": str(ro)}, "guard-block", {"x": 1}))


class Messages(unittest.TestCase):
    def test_cross_session_message_with_name(self):
        p = '<cross-session-message from="abc" from-name="dev-b" from-mode="x">\n本文です\n</cross-session-message>'
        self.assertEqual(E.message_entries(p), [{"from": "dev-b", "via": "cross-session", "bytes": len("本文です".encode())}])

    def test_falls_back_to_from_and_counts_each(self):
        p = ('<cross-session-message from="s1">a</cross-session-message> x '
             '<cross-session-message from="s2" from-name="dev-c">bb</cross-session-message>')
        self.assertEqual([(m["from"], m["bytes"]) for m in E.message_entries(p)], [("s1", 1), ("dev-c", 2)])

    def test_relay_wake(self):
        wake = "claude-relay: dev-d から 2 通 [relay-wake ids=a1,b2]。渡っていなければ …"
        m, = E.message_entries(f'<cross-session-message from="x">{wake}</cross-session-message>')
        self.assertEqual((m["from"], m["via"], m["n"]), ("dev-d", "relay", 2))  # 枠の送り元より知らせの送り元
        m, = E.message_entries(wake)  # 枠の無い印だけ
        self.assertEqual((m["from"], m["via"], m["n"]), ("dev-d", "relay", 2))
        m, = E.message_entries('<cross-session-message from="x">[relay-wake ids=z9]</cross-session-message>')
        self.assertEqual((m["from"], m["n"]), ("x", 1))  # 送り元の文が無ければ枠の送り元
        m, = E.message_entries('<cross-session-message from="x">ふつう</cross-session-message>')
        self.assertNotIn("n", m)

    def test_not_a_message(self):
        for p in (None, 3, "", "ふつうの入力", "<cross-session-message from=\"x\">閉じていない", "<task-notification>x"):
            self.assertEqual(E.message_entries(p), [], p)


class Tasks(unittest.TestCase):
    def post(self, tool, ti, tr):
        return {"hook_event_name": "PostToolUse", "tool_name": tool, "tool_input": ti, "tool_response": tr}

    def test_create_takes_the_id_from_the_response(self):
        e = E.task_entry(self.post("TaskCreate", {"subject": "🧑 題の案 A/B", "description": "…"},
                                   {"task": {"id": "3", "subject": "🧑 題の案 A/B"}}))
        self.assertEqual(e, {"id": "3", "subject": "🧑 題の案 A/B", "status": "pending", "op": "create"})

    def test_update_status_and_subject(self):
        e = E.task_entry(self.post("TaskUpdate", {"taskId": "7", "status": "in_progress"},
                                   {"success": True, "taskId": "7", "updatedFields": ["status"],
                                    "statusChange": {"from": "pending", "to": "in_progress"}}))
        self.assertEqual(e, {"id": "7", "subject": None, "status": "in_progress", "op": "update"})
        e = E.task_entry(self.post("TaskUpdate", {"taskId": "7", "subject": "【ゲーム待ち】計測"}, {"success": True}))
        self.assertEqual((e["subject"], e["status"]), ("【ゲーム待ち】計測", None))
        # tool_input に status が無くても、応答の statusChange から
        e = E.task_entry(self.post("TaskUpdate", {"taskId": "7"}, {"statusChange": {"to": "completed"}}))
        self.assertEqual(e["status"], "completed")

    def test_subject_is_clipped(self):
        e = E.task_entry(self.post("TaskCreate", {"subject": "あ" * 300}, {"task": {"id": "1"}}))
        self.assertEqual(len(e["subject"]), E.SUBJECT_MAX)

    def test_broken_or_other(self):
        self.assertIsNone(E.task_entry(self.post("Bash", {"command": "x"}, {})))
        self.assertIsNone(E.task_entry(self.post("TaskUpdate", {"taskId": "9"}, {"success": False})))
        self.assertIsNone(E.task_entry(self.post("TaskUpdate", None, "text")))
        self.assertIsNone(E.task_entry(self.post("TaskCreate", {"subject": 5}, {"task": []})))
        e = E.task_entry(self.post("TaskCreate", {"subject": "s"}, "文字列の応答"))  # 応答が読めなくても題は残す
        self.assertEqual((e["id"], e["subject"]), (None, "s"))


class GuardHook(Tmp):
    """bin/claude-guard hook を動かし、止めたときだけ blocks.jsonl に書くこと。stdout は deny の JSON だけ。"""

    def setUp(self):
        super().setUp()
        self.protected = self.d / "home" / "data"
        self.protected.mkdir(parents=True)
        self.run = self.d / "run"
        (self.run / "claude-state").mkdir(parents=True)
        (self.run / "claude-state" / "s-1.json").write_text(json.dumps({"session_id": "s-1", "role": "dev-a"}))
        cfg = self.d / "config.json"
        cfg.write_text(json.dumps({"guard_hook": {"guards": {"busy": {
            "command": ["true"], "paths": [str(self.protected)], "commands": ["git push"]}}}}))
        self.env.update(XDG_RUNTIME_DIR=str(self.run), CLAUDE_KITTY_CONFIG=str(cfg), PATH=os.environ.get("PATH", "/usr/bin:/bin"))
        self.log = self.d / "state" / "claude-kitty" / "guard" / "blocks.jsonl"

    def hook(self, tool, tool_input, raw=None, env=None):
        text = raw if raw is not None else json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool,
                                                       "tool_input": tool_input, "cwd": str(self.d), "session_id": "s-1"})
        r = subprocess.run([sys.executable, str(GUARD_BIN), "hook"], input=text, capture_output=True, text=True,
                           env=env or self.env, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_command_path_and_loop_kinds(self):
        out = self.hook("Bash", {"command": "git push origin main"})
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.hook("Write", {"file_path": str(self.protected / "a.txt"), "content": "x"})
        self.hook("Bash", {"command": f"rm -rf {self.protected}/old"})
        self.hook("Bash", {"command": "until ! pgrep -f 'luajit tests/run.lua'; do sleep 1; done"})
        recs = lines(self.log)
        self.assertEqual([(r["guard"], r["kind"]) for r in recs],
                         [("busy", "command"), ("busy", "path"), ("busy", "path"), ("wait-loop", "loop")])
        r = recs[0]
        self.assertEqual((r["ev"], r["role"], r["sid"], r["cmd"]), ("guard-block", "dev-a", "s-1", "git push origin main"))
        self.assertRegex(r["at"], LOCAL_ISO)
        self.assertEqual(recs[1]["cmd"], f"Write {self.protected / 'a.txt'}")
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)

    def test_cmd_is_clipped(self):
        self.hook("Bash", {"command": "git push " + "x" * 500})
        self.assertEqual(len(lines(self.log)[0]["cmd"]), E.CMD_MAX)

    def test_passing_calls_and_broken_input_write_nothing(self):
        self.assertEqual(self.hook("Bash", {"command": "ls"}), "")
        self.assertEqual(self.hook("Write", {"file_path": str(self.d / "free.txt")}), "")
        self.assertEqual(self.hook(None, None, raw="{broken"), "")
        self.assertEqual(self.hook(None, None, raw=json.dumps({"tool_name": "Bash", "tool_input": "x"})), "")
        self.assertFalse(self.log.exists())

    def test_unwritable_log_still_denies_with_only_the_json(self):
        (self.d / "state").write_text("not a folder")
        out = self.hook("Bash", {"command": "git push"})
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")  # stdout は JSON 1 つだけ

    def test_unknown_role_is_null(self):
        self.hook("Bash", {"command": "git push"}, raw=json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push"},
                                                                    "session_id": "s-unknown"}))
        self.assertIsNone(lines(self.log)[0]["role"])


class RelayTransition(Tmp):
    """claude-relay が付け外しする印 relay（waiting になる）も、状態の移りとして残す。"""

    def test_notify_role_writes_the_transition(self):
        import relay as R
        run = self.d / "run"
        (run / "claude-state").mkdir(parents=True)
        (run / "claude-state" / "s-1.json").write_text(json.dumps({
            "session_id": "s-1", "state": "idle", "role": "dev-a", "window": 2, "socket": "unix:/x",
            "waits": {}, "agents": {}, "main": False, "seen": True}))
        env = dict(self.env, XDG_RUNTIME_DIR=str(run), CLAUDE_KITTY_KITTEN="/bin/true",
                   CLAUDE_KITTY_CONFIG=str(self.d / "none.json"))
        self.assertEqual(R.notify_role(env, "dev-a", "relay", 1000.0, alive=lambda e: True), 1)
        R.notify_role(env, "dev-a", "relay", 1001.0, alive=lambda e: True)   # もう waiting: 書かない
        R.notify_role(env, "dev-a", "relay-clear", 1002.0, alive=lambda e: True)
        recs = lines(self.d / "state" / "claude-kitty" / "state" / "transitions.jsonl")
        self.assertEqual([(r["from"], r["to"], r.get("reason")) for r in recs],
                         [("idle", "waiting", "relay"), ("waiting", "done", None)])  # 待受の後は未読
        self.assertEqual({(r["role"], r["sid"]) for r in recs}, {("dev-a", "s-1")})


class WatcherIdle(Tmp):
    """kitty の watcher（と claude-state watch）が done → idle にしたときの記録。"""

    def test_mark_seen_writes_done_to_idle(self):
        import claude_state_watcher as W
        d = self.d / "cs"
        d.mkdir()
        (d / "s-1.json").write_text(json.dumps({"session_id": "s-1", "state": "done", "window": 3, "socket": "u",
                                                "role": "dev-a"}))
        (d / "s-2.json").write_text(json.dumps({"session_id": "s-2", "state": "working", "window": 3, "socket": "u"}))
        self.assertEqual([x[0] for x in W.mark_seen(d, 3, "u", 1000.0, env=self.env)], ["s-1"])
        rec, = lines(self.d / "state" / "claude-kitty" / "state" / "transitions.jsonl")
        self.assertEqual((rec["role"], rec["sid"], rec["from"], rec["to"]), ("dev-a", "s-1", "done", "idle"))
        self.assertEqual(W.mark_seen(d, 3, "u", 1001.0, env=self.env), [])  # もう idle: 2 行目は書かない
        self.assertEqual(len(lines(self.d / "state" / "claude-kitty" / "state" / "transitions.jsonl")), 1)

    def test_unwritable_place_does_not_stop_mark_seen(self):
        import claude_state_watcher as W
        d = self.d / "cs"
        d.mkdir()
        (d / "s-1.json").write_text(json.dumps({"session_id": "s-1", "state": "done", "window": 3, "socket": "u"}))
        (self.d / "state").write_text("not a folder")
        self.assertEqual([x[0] for x in W.mark_seen(d, 3, "u", 1000.0, env=self.env)], ["s-1"])
        self.assertEqual(json.loads((d / "s-1.json").read_text())["state"], "idle")


if __name__ == "__main__":
    unittest.main()
