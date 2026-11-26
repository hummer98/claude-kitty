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
                  "claude-relay: 別の端末から 1 通 [relay-wake ids=r1]。渡っていなければ …"):
            self.assertFalse(P.is_human(p), p)

    def test_empty_and_not_text(self):
        for p in (None, "", "   \n", 3, ["x"], {"a": 1}):
            self.assertFalse(P.is_human(p), repr(p))

    def test_notice_markers(self):
        notice = {"contains": ["返事は不要"], "prefix": ["【通知】"]}
        self.assertFalse(P.is_human('<cross-session-message from="a">【通知】終わった</cross-session-message>', notice))
        self.assertTrue(P.is_human("【通知】と書いたのは人", notice))  # 通知の印は他のセッションからのメッセージだけに効く

    def test_relay_wake_text_carries_a_machine_mark(self):
        # claude-kitty のツールが socket に直接送る文は、MACHINE_MARKS のどれかを含む（印の一覧を 1 か所に保つ）
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
        self.assertNotIn("input", d)  # 入力は書き換えない（断って出し直してもらう）
        self.assertEqual((d["from"], d["to"], d["idle_minutes"], d["description"]), ("opus", "haiku", 25.0, "Implement the parser"))
        self.assertEqual(p["tool_input"]["model"], "opus")  # 元の入力は変えない
        d = P.decide(agent(), ON, last(25), NOW)  # model を指定していない（親と同じ）
        self.assertEqual((d["from"], d["to"]), (None, "haiku"))
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

    def away(self):
        self.policy(enabled=True)
        P.record_human(self.env, "s0", "main", None, now=NOW - 30 * 60)

    def denials(self):
        return P.read_log(self.env, P.DENIALS, 50)

    def test_away_call_is_denied_not_rewritten(self):
        # auto mode では updatedInput が判定を無効にし、同じ返答のほかの呼び出しまで断られた。判定（deny）だけを返す
        self.away()
        out = P.run_hook(json.dumps(agent(model="opus", run_in_background=True)), self.env, now=NOW)
        hso = out["hookSpecificOutput"]
        self.assertEqual(set(hso), {"hookEventName", "permissionDecision", "permissionDecisionReason"})
        self.assertEqual((hso["hookEventName"], hso["permissionDecision"]), ("PreToolUse", "deny"))
        self.assertNotIn("updatedInput", hso)
        reason = hso["permissionDecisionReason"]
        for want in ("離席中", "model を haiku にして同じ呼び出しを出し直して", "ほかの項目はそのまま", "opus"):
            self.assertIn(want, reason)
        self.assertNotIn("そのまま出し直せば", reason)  # 2 度目を通す安全弁は知らせない
        self.assertEqual(P.recent_switches(self.env), [])  # 断っただけでは切り替えの記録にしない
        self.assertEqual(len(self.denials()), 1)
        d = P.decide(agent(), dict(ON, away_model="sonnet"), last(25), NOW)
        self.assertIn("model を sonnet にして", P.hook_output(d)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_retry_with_away_model_passes_and_is_logged_with_role(self):
        self.away()
        (self.d / "claude-state").mkdir()
        (self.d / "claude-state" / "s1.json").write_text(json.dumps({"state": "working", "role": "dev-a"}))
        self.assertIsNotNone(P.run_hook(json.dumps(agent(model="opus")), self.env, now=NOW))
        self.assertIsNone(P.run_hook(json.dumps(agent(model="haiku")), self.env, now=NOW + 5))  # 出し直しは通す
        log = P.recent_switches(self.env)
        self.assertEqual(len(log), 1)
        self.assertEqual({k: log[0][k] for k in ("session_id", "role", "from", "to", "description", "idle_minutes")},
                         {"session_id": "s1", "role": "dev-a", "from": "opus", "to": "haiku",
                          "description": "Implement the parser", "idle_minutes": 30.0})
        self.assertIn("denied_at", log[0])
        self.assertIsNone(P.run_hook(json.dumps(agent(model="haiku")), self.env, now=NOW + 6))  # 2 度出し直しても 1 件
        self.assertEqual(len(P.recent_switches(self.env)), 1)

    def test_away_model_call_without_denial_is_not_logged(self):
        # 初めから haiku の呼び出し・断ってから時間がたったもの・別のセッションは、切り替えではない
        self.away()
        self.assertIsNone(P.run_hook(json.dumps(agent(model="haiku")), self.env, now=NOW))
        self.assertIsNotNone(P.run_hook(json.dumps(agent(model="opus")), self.env, now=NOW))
        self.assertIsNone(P.run_hook(json.dumps(dict(agent(model="haiku"), session_id="s2")), self.env, now=NOW + 1))
        self.assertIsNone(P.run_hook(json.dumps(agent(model="haiku")), self.env, now=NOW + P.RETRY_SECONDS + 1))
        self.assertEqual(P.recent_switches(self.env), [])

    def test_second_try_with_original_model_is_not_denied_again(self):
        # Claude が従わず元の model のまま出し直したとき、断りをくり返して作業を止めない
        self.away()
        call = json.dumps(agent(model="opus"))
        self.assertIsNotNone(P.run_hook(call, self.env, now=NOW))
        self.assertIsNone(P.run_hook(call, self.env, now=NOW + 10))
        self.assertIsNone(P.run_hook(json.dumps(agent()), self.env, now=NOW + 15))  # model を外しても同じ依頼
        self.assertEqual(P.recent_switches(self.env), [])  # 切り替わっていない
        self.assertIsNotNone(P.run_hook(json.dumps(agent(model="opus", prompt="別の作業")), self.env, now=NOW + 30))
        other = dict(agent(model="opus"), session_id="s2")
        self.assertIsNotNone(P.run_hook(json.dumps(other), self.env, now=NOW + 40))  # 別のセッション
        self.assertIsNotNone(P.run_hook(call, self.env, now=NOW + P.RETRY_SECONDS + 1))  # 時間がたてば新しい呼び出し
        self.assertEqual(len(self.denials()), 4)

    def test_unwritable_denial_log_lets_the_call_through(self):
        # 断りを記録できないと 2 度目を見分けられず、断りがくり返しうる: 切り替えずに通す
        self.away()
        (self.d / "claude-presence" / P.DENIALS).mkdir()
        self.assertIsNone(P.run_hook(json.dumps(agent(model="opus")), self.env, now=NOW))
        self.assertIn("断らない", (self.d / "claude-presence" / "errors.log").read_text())

    def test_broken_line_in_denial_log_does_not_hide_denials(self):
        # 書きかけで切れた行があっても、ほかの断りの記録は読む（見失うと断りがくり返される）
        self.away()
        self.assertIsNotNone(P.run_hook(json.dumps(agent(model="opus")), self.env, now=NOW))
        log = self.d / "claude-presence" / P.DENIALS
        log.write_bytes(b'{"half": "\xe3\x81\n' + log.read_bytes())
        self.assertIsNone(P.run_hook(json.dumps(agent(model="opus")), self.env, now=NOW + 10))

    def test_unreadable_denial_log_lets_the_call_through(self):
        # 書けるが読めない（chmod 200）とき、断りを見分けられないまま断り続けない
        if os.geteuid() == 0:
            self.skipTest("root は読める")
        self.away()
        self.assertIsNotNone(P.run_hook(json.dumps(agent(model="opus")), self.env, now=NOW))
        log = self.d / "claude-presence" / P.DENIALS
        log.chmod(0o200)
        try:
            for i in range(3):
                self.assertIsNone(P.run_hook(json.dumps(agent(model="opus", prompt=f"別 {i}")), self.env, now=NOW + i), i)
        finally:
            log.chmod(0o600)
        self.assertIn("読めないので断らない", (self.d / "claude-presence" / "errors.log").read_text())

    def test_unterminated_last_line_does_not_swallow_next_record(self):
        # 前の書き込みが改行の前で切れていても、次の断りの記録は別の行になり、出し直しを 1 回余分に断らない
        self.away()
        log = self.d / "claude-presence" / P.DENIALS
        log.parent.mkdir(exist_ok=True)
        log.write_text('{"cut": ')
        self.assertIsNotNone(P.run_hook(json.dumps(agent(model="opus")), self.env, now=NOW))
        self.assertIsNone(P.run_hook(json.dumps(agent(model="opus")), self.env, now=NOW + 5))
        self.assertEqual(len(self.denials()), 1)
        self.assertEqual(log.read_text().count("\n"), 2)  # 切れた行の後の区切り + 記録の 1 行
        sw = self.d / "claude-presence" / P.SWITCHES
        sw.write_text('{"i": 0}\n{"cut')
        P.append_switch(self.env, {"i": 1})
        P.append_switch(self.env, {"i": 2})
        self.assertEqual([r.get("i") for r in P.recent_switches(self.env, 10)], [0, 1, 2])

    def test_write_only_log_is_still_appended(self):
        # 書けるが読めないファイルにも書く（末尾の確かめは省く）
        if os.geteuid() == 0:
            self.skipTest("root は読める")
        P.append_switch(self.env, {"i": 1})
        sw = self.d / "claude-presence" / P.SWITCHES
        sw.chmod(0o200)
        try:
            P.append_switch(self.env, {"i": 2})
        finally:
            sw.chmod(0o600)
        self.assertEqual([r.get("i") for r in P.recent_switches(self.env, 10)], [1, 2])

    def test_denial_found_among_many_other_sessions(self):
        # ほかのセッションの断りが 50 件より多くても、時間の内の自分の断りを見失わない
        self.away()
        self.assertIsNotNone(P.run_hook(json.dumps(agent(model="opus")), self.env, now=NOW))
        for i in range(80):
            other = dict(agent(model="opus", prompt=f"p{i}"), session_id=f"o{i}")
            self.assertIsNotNone(P.run_hook(json.dumps(other), self.env, now=NOW + 1))
        self.assertIsNone(P.run_hook(json.dumps(agent(model="opus")), self.env, now=NOW + 2))
        self.assertIsNone(P.run_hook(json.dumps(agent(model="haiku")), self.env, now=NOW + 3))
        self.assertEqual(len(P.recent_switches(self.env)), 1)

    def test_denial_found_after_rotation(self):
        self.away()
        old = P.LOG_MAX
        P.LOG_MAX = 300
        try:
            self.assertIsNotNone(P.run_hook(json.dumps(agent(model="opus")), self.env, now=NOW))
            for i in range(3):  # 回して、自分の断りを denials.jsonl.1 に送る
                other = dict(agent(model="opus", prompt=f"p{i}"), session_id=f"o{i}")
                self.assertIsNotNone(P.run_hook(json.dumps(other), self.env, now=NOW + 1))
        finally:
            P.LOG_MAX = old
        d = self.d / "claude-presence"
        self.assertIn('"session_id": "s1"', (d / (P.DENIALS + ".1")).read_text())
        self.assertNotIn('"session_id": "s1"', (d / P.DENIALS).read_text())
        self.assertIsNone(P.run_hook(json.dumps(agent(model="opus")), self.env, now=NOW + 2))

    def test_denial_counts(self):
        self.away()
        self.assertEqual(P.denial_counts(self.env), {"denied": 0, "switched": 0})
        P.run_hook(json.dumps(agent(model="opus")), self.env, now=NOW)
        P.run_hook(json.dumps(agent(model="opus", prompt="別")), self.env, now=NOW + 1)
        P.run_hook(json.dumps(agent(model="haiku")), self.env, now=NOW + 2)  # 1 件目だけ従った
        self.assertEqual(P.denial_counts(self.env), {"denied": 2, "switched": 1})

    def test_unwritable_switch_log_lets_the_retry_through(self):
        self.away()
        self.assertIsNotNone(P.run_hook(json.dumps(agent(model="opus")), self.env, now=NOW))
        (self.d / "claude-presence" / P.SWITCHES).mkdir()
        self.assertIsNone(P.run_hook(json.dumps(agent(model="haiku")), self.env, now=NOW + 5))
        self.assertIn(P.SWITCHES, (self.d / "claude-presence" / "errors.log").read_text())

    def test_retry_after_human_returns_is_still_logged(self):
        # 断った後に人が打っても、away_model での出し直しは切り替えとして載る
        self.away()
        self.assertIsNotNone(P.run_hook(json.dumps(agent(model="opus")), self.env, now=NOW))
        P.record_human(self.env, "s0", "main", None, now=NOW + 1)
        self.assertIsNone(P.run_hook(json.dumps(agent(model="haiku")), self.env, now=NOW + 5))
        self.assertEqual(len(P.recent_switches(self.env)), 1)

    def test_present_keep_and_fable_do_nothing(self):
        self.away()
        for p in (agent(description="差分のレビュー"), agent(prompt="VERIFY the fix"), agent(model="fable"),
                  agent(subagent_type="Explore")):
            self.assertIsNone(P.run_hook(json.dumps(p), self.env, now=NOW), p)
        self.assertIsNone(P.run_hook(json.dumps(agent(model="opus")), self.env, now=NOW - 29 * 60))  # 在席
        self.assertEqual((self.denials(), P.recent_switches(self.env)), ([], []))

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
                  "claude-relay: 別の端末から 1 通 [relay-wake ids=r1]", ""):
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
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertNotIn("updatedInput", out["hookSpecificOutput"])
        self.assertEqual(self.run_("hook", stdin=json.dumps(agent(model="haiku"))), "")  # 出し直しは通す
        self.assertEqual(json.loads(self.run_("--json"))["recent"][-1]["to"], "haiku")
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
        self.assertNotIn("断った呼び出し", text)  # 断りが無ければ出さない
        self.assertEqual(s["denials"], {"denied": 0, "switched": 0})
        P.append_log(self.env, P.DENIALS, {"at": "x", "t": 1, "session_id": "s1", "prompt_key": "k"})
        P.append_log(self.env, P.DENIALS, {"at": "y", "t": 2, "session_id": "s1", "prompt_key": "k2"})
        P.append_switch(self.env, {"at": "z", "t": 3, "session_id": "s1", "prompt_key": "k", "denied_at": "x", "to": "haiku"})
        self.assertIn("断った呼び出し: 2 件、うち出し直しで切り替わった 1 件", self.run_())
        self.assertEqual(json.loads(self.run_("--json"))["denials"], {"denied": 2, "switched": 1})
        self.cfg.write_text('{"model_policy": {"away_minutes": -1}}')
        s = json.loads(self.run_("--json"))
        self.assertIn("away_minutes", s["policy_error"])
        self.assertFalse(s["policy"]["enabled"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
