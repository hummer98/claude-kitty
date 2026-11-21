#!/usr/bin/env python3
"""claude-presence（lib/presence.py・bin/claude-presence）と、claude-state の人の入力の記録のテスト（一時の置き場だけを使う）。"""
import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import presence as P  # noqa: E402
from config import load_model_policy  # noqa: E402

PRESENCE_BIN = ROOT / "bin" / "claude-presence"
STATE_BIN = ROOT / "bin" / "claude-state"
ON = dict(P.DEFAULT_MODEL_POLICY, enabled=True)
NOW = 1_800_000_000.0


def agent(**ti):
    base = {"description": "Implement the parser", "prompt": "Write lib/x.py and tests.", "subagent_type": "general-purpose"}
    base.update(ti)
    return {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Agent",
            "tool_input": {k: v for k, v in base.items() if v is not None}}


def last(minutes_ago):
    return {"at": NOW - minutes_ago * 60, "session_id": "s0"}


class HumanInput(unittest.TestCase):
    def test_plain_text_is_human(self):
        self.assertTrue(P.is_human("テストを足して"))
        self.assertTrue(P.is_human("/clear"))

    def test_machine_inputs(self):
        for p in ('<cross-session-message from="x">hi</cross-session-message>',
                  '  \n<cross-session-message from="x">hi</cross-session-message>',
                  'note <cross-session-message from="x">hi</cross-session-message>',
                  "<task-notification><task-id>a</task-id></task-notification>",
                  '<agent-message from="a1">[Subagent hand-back] 終わった</agent-message>',
                  "<system-reminder>x</system-reminder>",
                  "claude-relay: 別の端末から 1 通届いた [relay-wake ids=r1]\n（…）"):
            self.assertFalse(P.is_human(p), p)

    def test_empty_and_not_text(self):
        for p in (None, "", "   \n", 3, ["x"], {"a": 1}):
            self.assertFalse(P.is_human(p), repr(p))

    def test_notice_markers(self):
        notice = {"contains": ["返事は不要"], "prefix": ["【通知】"]}
        self.assertFalse(P.is_human('<cross-session-message from="a">【通知】終わった</cross-session-message>', notice))
        self.assertTrue(P.is_human("【通知】と書いたのは人", notice))  # 通知の印は他のセッションからのメッセージだけに効く

    def test_relay_wake_text_carries_a_machine_mark(self):
        # claude-kitty の道具が socket に直接送る文は、MACHINE_MARKS のどれかを含む（印の一覧を 1 か所に保つ）
        import relay as R
        text = R.wake_text("main", [{"id": "20261122T000000Z-abcd", "from": "mac-a", "body": "x", "t": 0}])
        self.assertTrue(any(m in text for m in P.MACHINE_MARKS))
        self.assertFalse(P.is_human(text))


class Record(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {"XDG_RUNTIME_DIR": self.tmp.name}

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_and_private(self):
        self.assertIsNone(P.load_last(self.env))
        P.record_human(self.env, "s1", "dev-a", "/w", now=NOW)
        rec = P.load_last(self.env)
        self.assertEqual((rec["at"], rec["session_id"], rec["role"], rec["cwd"]), (NOW, "s1", "dev-a", "/w"))
        p = Path(self.tmp.name) / "claude-presence" / "last.json"
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(p.parent.stat().st_mode), 0o700)
        P.record_human(self.env, "s2", None, None, now=NOW + 5)  # 1 つを置き換える（全セッション共通）
        self.assertEqual(P.load_last(self.env)["session_id"], "s2")
        self.assertEqual([x.name for x in p.parent.iterdir()], ["last.json"])  # 一時ファイルが残らない

    def test_broken_record_is_none(self):
        p = Path(self.tmp.name) / "claude-presence" / "last.json"
        p.parent.mkdir()
        for text in ("{broken", "[]", '{"at": true}', '{"at": "x"}', "\xff"):
            p.write_text(text, errors="surrogateescape")
            self.assertIsNone(P.load_last(self.env), text)

    def test_idle_minutes(self):
        self.assertIsNone(P.idle_minutes(None, NOW))
        self.assertEqual(P.idle_minutes(last(30), NOW), 30.0)
        self.assertEqual(P.idle_minutes({"at": NOW + 600}, NOW), 0.0)  # 時計が戻った: 在席とみる
        self.assertTrue(P.is_away(last(20), NOW, ON))
        self.assertFalse(P.is_away(last(19.9), NOW, ON))
        self.assertFalse(P.is_away(None, NOW, ON))


class Decide(unittest.TestCase):
    def test_disabled_does_nothing(self):
        self.assertIsNone(P.decide(agent(), P.DEFAULT_MODEL_POLICY, last(120), NOW))
        self.assertIsNone(P.decide(agent(), None, last(120), NOW))

    def test_present_does_nothing(self):
        self.assertIsNone(P.decide(agent(model="opus"), ON, last(5), NOW))

    def test_no_record_does_nothing(self):
        self.assertIsNone(P.decide(agent(), ON, None, NOW))

    def test_away_implementation_goes_to_haiku(self):
        p = agent(model="opus", run_in_background=True, isolation="worktree")
        d = P.decide(p, ON, last(25), NOW)
        self.assertEqual(d["input"], dict(p["tool_input"], model="haiku"))  # ほかの項目はそのまま写す
        self.assertEqual((d["from"], d["to"], d["idle_minutes"], d["description"]), ("opus", "haiku", 25.0, "Implement the parser"))
        self.assertEqual(p["tool_input"]["model"], "opus")  # 元の入力は変えない
        d = P.decide(agent(), ON, last(25), NOW)  # model を指定していない（親と同じ）
        self.assertEqual((d["from"], d["input"]["model"]), (None, "haiku"))
        d = P.decide(dict(agent(), tool_name="Task"), ON, last(25), NOW)  # 古い名前
        self.assertEqual(d["to"], "haiku")
        d = P.decide(agent(), dict(ON, away_model="sonnet", away_minutes=60), last(61), NOW)
        self.assertEqual(d["to"], "sonnet")
        self.assertIsNone(P.decide(agent(), dict(ON, away_minutes=60), last(59), NOW))

    def test_keep_ascii_words_match_whole_words(self):
        # 英字の語は単語として: guide は guideline に、review は preview に当たらない（実装の依頼を誤って残さない）
        for ti in ({"prompt": "follow the guidelines in CONTRIBUTING"}, {"prompt": "add a preview pane"},
                   {"description": "unverifyable"}):
            self.assertIsNotNone(P.decide(agent(**ti), ON, last(60), NOW), ti)
        for ti in ({"prompt": "see the guide."}, {"prompt": "(Review)"}, {"description": "code-review"}):
            self.assertIsNone(P.decide(agent(**ti), ON, last(60), NOW), ti)

    def test_keep_words(self):
        for ti in ({"description": "Review the diff"}, {"description": "差分のレビュー"}, {"prompt": "結果を評価して"},
                   {"prompt": "VERIFY the fix"}, {"prompt": "動かして検証する"}, {"description": "API を調べる"},
                   {"prompt": "read the guide"}):
            self.assertIsNone(P.decide(agent(**ti), ON, last(60), NOW), ti)
        # keep を設定で変えれば、その語だけ
        self.assertIsNotNone(P.decide(agent(description="Review"), dict(ON, keep=["監査"]), last(60), NOW))
        self.assertIsNone(P.decide(agent(description="監査"), dict(ON, keep=["監査"]), last(60), NOW))

    def test_skip_types_and_models(self):
        for st in ("claude-code-guide", "Explore", "Plan", "web-fetch", "fork"):
            self.assertIsNone(P.decide(agent(subagent_type=st), ON, last(60), NOW), st)
        self.assertIsNone(P.decide(agent(model="fable"), ON, last(60), NOW))
        self.assertIsNone(P.decide(agent(model="haiku"), ON, last(60), NOW))  # 既に軽い（ログも残さない）
        self.assertIsNotNone(P.decide(agent(subagent_type=None), ON, last(60), NOW))  # 種類なし = general-purpose

    def test_not_an_agent_call(self):
        for p in ({"tool_name": "Bash", "tool_input": {"command": "ls", "prompt": "x"}},
                  {"tool_name": "Agent", "tool_input": "x"}, {"tool_name": "Agent", "tool_input": {"description": "d"}},
                  {"tool_name": "Agent"}, {}, [], None, "x"):
            self.assertIsNone(P.decide(p, ON, last(60), NOW), repr(p))


class HookRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.cfg = self.d / "config.json"
        self.env = {"XDG_RUNTIME_DIR": str(self.d), "CLAUDE_KITTY_CONFIG": str(self.cfg)}

    def tearDown(self):
        self.tmp.cleanup()

    def policy(self, **mp):
        self.cfg.write_text(json.dumps({"model_policy": mp}))

    def test_switch_is_logged_with_role(self):
        self.policy(enabled=True)
        P.record_human(self.env, "s0", "main", None, now=NOW - 30 * 60)
        (self.d / "claude-state").mkdir()
        (self.d / "claude-state" / "s1.json").write_text(json.dumps({"state": "working", "role": "dev-a"}))
        out = P.run_hook(json.dumps(agent(model="opus")), self.env, now=NOW)
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertEqual(hso["updatedInput"]["model"], "haiku")
        self.assertNotIn("permissionDecision", hso)  # 許可の判断はしない
        self.assertIn("haiku", hso["additionalContext"])
        log = P.recent_switches(self.env)
        self.assertEqual(len(log), 1)
        self.assertEqual({k: log[0][k] for k in ("session_id", "role", "from", "to", "description", "idle_minutes")},
                         {"session_id": "s1", "role": "dev-a", "from": "opus", "to": "haiku",
                          "description": "Implement the parser", "idle_minutes": 30.0})

    def test_retry_of_same_call_is_not_switched_again(self):
        # auto mode で断られて元の model のまま出し直されたとき、書き換えと断りをくり返さない
        self.policy(enabled=True)
        P.record_human(self.env, "s0", "main", None, now=NOW - 30 * 60)
        call = json.dumps(agent(model="opus"))
        self.assertIsNotNone(P.run_hook(call, self.env, now=NOW))
        self.assertIsNone(P.run_hook(call, self.env, now=NOW + 10))
        self.assertIsNone(P.run_hook(json.dumps(agent(model="haiku")), self.env, now=NOW + 20))  # 書き換えた形での出し直し
        self.assertIsNotNone(P.run_hook(json.dumps(agent(model="opus", prompt="別の作業")), self.env, now=NOW + 30))
        other = dict(agent(model="opus"), session_id="s2")
        self.assertIsNotNone(P.run_hook(json.dumps(other), self.env, now=NOW + 40))  # 別のセッション
        self.assertIsNotNone(P.run_hook(call, self.env, now=NOW + P.RETRY_SECONDS + 1))  # 時間がたてば新しい呼び出し
        self.assertEqual(len(P.recent_switches(self.env, 10)), 4)

    def test_nothing_when_disabled_present_or_broken(self):
        P.record_human(self.env, "s0", "main", None, now=NOW - 30 * 60)
        self.assertIsNone(P.run_hook(json.dumps(agent()), self.env, now=NOW))  # 設定ファイルが無い = 既定は無効
        self.policy(enabled=False)
        self.assertIsNone(P.run_hook(json.dumps(agent()), self.env, now=NOW))
        self.policy(enabled=True)
        self.assertIsNone(P.run_hook(json.dumps(agent()), self.env, now=NOW - 29 * 60))  # 1 分前に打った
        for text in ("", "{broken", "[]", "null", '{"tool_name": "Agent", "tool_input": null}'):
            self.assertIsNone(P.run_hook(text, self.env, now=NOW), text)
        self.cfg.write_text('{"model_policy": {"enabled": "yes"}}')
        self.assertIsNone(P.run_hook(json.dumps(agent()), self.env, now=NOW))
        self.assertIn("設定を読めない", (self.d / "claude-presence" / "errors.log").read_text())
        self.assertEqual(P.recent_switches(self.env), [])

    def test_log_rotates(self):
        old = P.LOG_MAX
        P.LOG_MAX = 100
        try:
            for i in range(5):
                P.append_switch(self.env, {"i": i, "pad": "x" * 40})
        finally:
            P.LOG_MAX = old
        self.assertTrue((self.d / "claude-presence" / "switches.jsonl.1").exists())
        self.assertEqual([r["i"] for r in P.recent_switches(self.env, 10)][-1], 4)
        self.assertEqual(P.recent_switches(self.env, 0), [])


class Config(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "c.json"
        self.env = {"CLAUDE_KITTY_CONFIG": str(self.path)}

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults(self):
        self.assertEqual(load_model_policy(self.env), P.DEFAULT_MODEL_POLICY)
        self.path.write_text("{}")
        self.assertFalse(load_model_policy(self.env)["enabled"])
        self.path.write_text('{"model_policy": null}')
        self.assertFalse(load_model_policy(self.env)["enabled"])
        self.path.write_text('{"model_policy": {"enabled": true, "away_minutes": 5.5, "away_model": "sonnet", "keep": []}}')
        self.assertEqual(load_model_policy(self.env), {"enabled": True, "away_minutes": 5.5, "away_model": "sonnet", "keep": []})

    def test_default_keep_is_not_shared(self):
        load_model_policy(self.env)["keep"].append("x")
        self.assertNotIn("x", P.DEFAULT_MODEL_POLICY["keep"])

    def test_invalid(self):
        for mp in ([], {"enabled": 1}, {"away_minutes": 0}, {"away_minutes": True}, {"away_minutes": "20"},
                   {"away_model": ""}, {"away_model": "ha iku"}, {"keep": "review"}, {"keep": [""]}, {"enable": True}):
            self.path.write_text(json.dumps({"model_policy": mp}))
            with self.assertRaises(ValueError, msg=repr(mp)):
                load_model_policy(self.env)
        self.path.write_text("{broken")
        with self.assertRaises(ValueError):
            load_model_policy(self.env)


def fake_kitten(d):
    """ls に、ウィンドウ 4（役割 dev-b）がある kitty を返す偽の kitten。"""
    ls = [{"id": 1, "is_focused": False, "tabs": [{"id": 1, "title": "t", "is_active": True, "windows": [
        {"id": 4, "user_vars": {"role": "dev-b"}, "is_focused": True}]}]}]
    k = d / "kitten"
    k.write_text(f"#!{sys.executable}\nimport sys\nif 'ls' in sys.argv:\n    sys.stdout.write({json.dumps(json.dumps(ls))})\n")
    k.chmod(0o755)
    return k


class StateRecordsHuman(unittest.TestCase):
    """claude-state の UserPromptSubmit の hook が、人の入力のときだけ記録する（色の記録は今までどおり）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_", "KITTY_"))}
        self.env.update(CLAUDE_KITTY_KITTEN=str(fake_kitten(self.d)), CLAUDE_KITTY_CONFIG=str(self.d / "none.json"),
                        XDG_CONFIG_HOME=str(self.d), XDG_RUNTIME_DIR=str(self.d), CLAUDE_STATE_SKIP_NESTED_CHECK="1",
                        KITTY_WINDOW_ID="4", KITTY_LISTEN_ON="unix:/fake/kitty-1")

    def tearDown(self):
        self.tmp.cleanup()

    def hook(self, event, prompt=None, sid="sess-1", **env):
        payload = {"session_id": sid, "cwd": "/w"}
        if prompt is not None:
            payload["prompt"] = prompt
        r = subprocess.run([sys.executable, str(STATE_BIN), "hook", event], input=json.dumps(payload),
                           capture_output=True, text=True, env=dict(self.env, **env), timeout=20)
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)  # UserPromptSubmit の stdout は文脈に入る

    def last(self):
        return P.load_last(self.env)

    def state(self, sid="sess-1"):
        p = self.d / "claude-state" / f"{sid}.json"
        return json.loads(p.read_text()) if p.exists() else None

    def test_human_prompt_is_recorded_with_role(self):
        t0 = time.time()
        self.hook("UserPromptSubmit", "テストを足して")
        rec = self.last()
        self.assertEqual((rec["session_id"], rec["role"], rec["cwd"]), ("sess-1", "dev-b", "/w"))
        self.assertGreaterEqual(rec["at"], t0 - 1)
        self.assertEqual(self.state()["state"], "working")  # 色の記録は今までどおり

    def test_machine_prompts_do_not_update(self):
        self.hook("UserPromptSubmit", "人の入力", sid="sess-0")
        before = self.last()
        for p in ('<cross-session-message from="aom-master">進めて</cross-session-message>',
                  "<task-notification>done</task-notification>",
                  "claude-relay: 別の端末から 1 通届いた [relay-wake ids=r1]", ""):
            self.hook("UserPromptSubmit", p)
            self.assertEqual(self.last(), before, p)
        self.hook("UserPromptSubmit")  # prompt が無い
        self.assertEqual(self.last(), before)
        self.assertEqual(self.state()["state"], "working")  # 色の方は今までどおり動く

    def test_other_events_and_off_do_not_record(self):
        self.hook("Stop")
        self.hook("PreToolUse")
        self.assertIsNone(self.last())
        self.hook("UserPromptSubmit", "hi", CLAUDE_KITTY_STATE="off")
        self.assertIsNone(self.last())
        self.hook("UserPromptSubmit", "hi", sid="../x")  # 正しくない session id
        self.assertIsNone(self.last())

    def test_broken_config_outside_kitty_is_quiet(self):
        # 設定が壊れていても、kitty の外のプロンプトごとに警告を出さない（前の版と同じく静か）。記録はする
        (self.d / "bad.json").write_text("{broken")
        r = subprocess.run([sys.executable, str(STATE_BIN), "hook", "UserPromptSubmit"],
                           input=json.dumps({"session_id": "sess-1", "prompt": "hi"}), capture_output=True, text=True,
                           env=dict(self.env, KITTY_WINDOW_ID="", CLAUDE_KITTY_CONFIG=str(self.d / "bad.json")), timeout=20)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertEqual(self.last()["session_id"], "sess-1")

    def test_outside_kitty_is_recorded_without_role(self):
        self.hook("UserPromptSubmit", "hi", KITTY_WINDOW_ID="")
        rec = self.last()
        self.assertEqual((rec["session_id"], rec["role"]), ("sess-1", None))
        self.assertIsNone(self.state())

    def test_broken_input_records_nothing(self):
        r = subprocess.run([sys.executable, str(STATE_BIN), "hook", "UserPromptSubmit"], input="{broken",
                           capture_output=True, text=True, env=self.env, timeout=20)
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertIsNone(self.last())

    def test_unwritable_presence_dir_does_not_break_state(self):
        (self.d / "claude-presence").write_text("not a dir")
        self.hook("UserPromptSubmit", "hi")
        self.assertEqual(self.state()["state"], "working")
        self.assertIn("presence:", (self.d / "claude-state" / "errors.log").read_text())

    def test_nested_claude_is_not_recorded(self):
        loader = importlib.machinery.SourceFileLoader("claude_state_for_presence", str(STATE_BIN))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cs = importlib.util.module_from_spec(spec)
        loader.exec_module(cs)
        env = dict(self.env)
        cs.who = lambda e: (123, True)
        self.assertEqual(cs.note_presence({"session_id": "s", "prompt": "hi"}, env, "outside kitty"), "nested")
        self.assertEqual(cs.note_presence({"session_id": "s", "prompt": "hi"}, env, "nested claude"), "skip")
        self.assertIsNone(self.last())
        cs.who = lambda e: (123, False)
        self.assertEqual(cs.note_presence({"session_id": "s", "prompt": "hi"}, env, "unchanged"), "human")
        self.assertEqual(cs.note_presence({"session_id": "s", "prompt": "hi"}, env, "working"), "human")


class PresenceCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.cfg = self.d / "config.json"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_", "KITTY_"))}
        self.env.update(XDG_RUNTIME_DIR=str(self.d), CLAUDE_KITTY_CONFIG=str(self.cfg), CLAUDE_CONFIG_DIR=str(self.d / "cc"),
                        HOME=str(self.d))

    def tearDown(self):
        self.tmp.cleanup()

    def run_(self, *args, stdin=""):
        r = subprocess.run([str(PRESENCE_BIN), *args], input=stdin, capture_output=True, text=True, env=self.env, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_hook(self):
        P.record_human(self.env, "s0", "main", None, now=time.time() - 3600)
        self.assertEqual(self.run_("hook", stdin=json.dumps(agent())), "")  # 既定は無効
        self.cfg.write_text('{"model_policy": {"enabled": true}}')
        out = json.loads(self.run_("hook", stdin=json.dumps(agent(model="opus"))))
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "haiku")
        for bad in ("", "{broken", "\x00\xff", json.dumps(agent(description="レビュー"))):
            self.assertEqual(self.run_("hook", stdin=bad), "", bad)
        P.record_human(self.env, "s0", "main", None)  # いま打った
        self.assertEqual(self.run_("hook", stdin=json.dumps(agent())), "")

    def test_status(self):
        s = json.loads(self.run_("--json"))
        self.assertEqual((s["last"], s["away"], s["policy"]["enabled"], s["hook"], s["recent"]), (None, None, False, "none", []))
        self.assertIn("記録が無い", self.run_())
        P.record_human(self.env, "s0", "main", None, now=time.time() - 25 * 60)
        self.cfg.write_text('{"model_policy": {"enabled": true}}')
        P.append_switch(self.env, {"at": "2026-11-22T10:00:00+0900", "role": "dev-a", "from": "opus", "to": "haiku",
                                   "description": "Implement"})
        s = json.loads(self.run_("--json"))
        self.assertEqual((s["away"], s["last"]["role"], len(s["recent"])), (True, "main", 1))
        self.assertGreaterEqual(s["idle_minutes"], 25)
        text = self.run_()
        for want in ("判定: 離席", "役割 main", "有効", "haiku", "dev-a  opus → haiku  Implement"):
            self.assertIn(want, text)
        self.cfg.write_text('{"model_policy": {"away_minutes": -1}}')
        s = json.loads(self.run_("--json"))
        self.assertIn("away_minutes", s["policy_error"])
        self.assertFalse(s["policy"]["enabled"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
