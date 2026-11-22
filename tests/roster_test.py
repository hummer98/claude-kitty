#!/usr/bin/env python3
"""claude-roster と claude-wait の続きの指示の単体テスト（kitty も claude も使わない）。

  python3 tests/roster_test.py

kitten は偽のスクリプト（ls の JSON を返し、ほかの呼び出しを記録する）、claude は偽のシェルスクリプト
（~/.claude/sessions の代わりのディレクトリに JSON を書いて眠る）にする。
"""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import roster as R  # noqa: E402
from geometry import tab_pane_fraction  # noqa: E402

SID_L = "aaaaaaaa-0000-0000-0000-000000000001"
SID_A = "aaaaaaaa-0000-0000-0000-000000000002"
SID_B = "aaaaaaaa-0000-0000-0000-000000000003"
SID_C = "aaaaaaaa-0000-0000-0000-000000000004"
SID_D = "aaaaaaaa-0000-0000-0000-000000000005"


def proc(pid, *argv, cwd="/w"):
    return {"pid": pid, "cmdline": list(argv), "cwd": cwd}


def win(wid, role, procs, cwd="/w"):
    return {"id": wid, "user_vars": {"role": role} if role else {}, "foreground_processes": procs, "cwd": cwd,
            "columns": 80, "lines": 24}


def fake_ls():
    """main タブ: 左 lead、右上 a,b、右下 c。standby タブ: d（claude-wait が待っているだけ）、e（シェル）。"""
    main = {"id": 1, "title": "main", "windows": [
        win(1, "lead", [proc(101, "/bin/bash", "/u/bin/claude-wait", "lead", SID_L), proc(11, "claude", "--resume", SID_L)]),
        win(2, "a", [proc(12, "claude", "--resume", SID_A, "--model", "sonnet")]),
        win(3, "b", [proc(13, "node", "x"), proc(14, "claude", "--resume", SID_B)]),  # JSON は無い（背景の前面など）
        win(4, "c", [proc(15, "claude", "-n", "c")]),
        win(9, None, []),  # overlay（group の 2 つめ）
    ], "groups": [{"id": 1, "windows": [1]}, {"id": 2, "windows": [2]}, {"id": 3, "windows": [3]},
                  {"id": 4, "windows": [4, 9]}],
        "layout_state": {"class": "Splits", "all_windows": {"window_groups": [
            {"id": 1, "window_ids": [1]}, {"id": 2, "window_ids": [2]}, {"id": 3, "window_ids": [3]},
            {"id": 4, "window_ids": [4, 9]}]},
            "pairs": {"bias": 0.33, "one": 1, "two": {"horizontal": False, "one": {"one": 2, "two": 3}, "two": 4}}}}
    standby = {"id": 2, "title": "standby", "windows": [
        win(5, "d", [proc(105, "python3", "/u/bin/claude-wait", "d", SID_D, "--model", "haiku")]),
        win(6, "e", [proc(106, "bash")], cwd="/e"),
        win(7, "bad,name", [])], "groups": [], "layout_state": {"class": "Splits", "pairs": {"one": 5}}}
    return [{"id": 1, "tabs": [standby, main]}]


def fake_sessions():
    return [{"pid": 11, "sessionId": SID_L, "cwd": "/lead", "name": "lead", "kind": "interactive", "status": "idle"},
            {"pid": 12, "sessionId": SID_A, "cwd": "/a", "name": "a", "kind": "interactive", "status": "busy"},
            {"pid": 900, "sessionId": SID_B, "cwd": "/b", "name": "bee", "kind": "bg", "status": "idle"},
            {"pid": 15, "sessionId": SID_C, "cwd": "/c", "name": "c", "kind": "interactive", "status": "idle"}]


def built(**kw):
    return R.build_roster(fake_ls(), fake_sessions(), "lead", tab_pane_fraction, now=100.0, **kw)


class ParseArgv(unittest.TestCase):
    def test_claude_forms(self):
        self.assertEqual(R.parse_argv(["claude", "--resume", SID_A, "--model", "opus", "-n", "x"])["model"], "opus")
        got = R.parse_argv(["/opt/claude.exe", "--model=fable", "-r", SID_A])
        self.assertEqual((got["session_id"], got["model"]), (SID_A, "fable"))
        self.assertIsNone(R.parse_argv(["claude", "--resume", "not-a-sid"])["session_id"])
        self.assertIsNone(R.parse_argv(["bash", "-l"]))
        self.assertIsNone(R.parse_argv([]))
        self.assertIsNone(R.parse_argv(None))

    def test_claude_wait_forms(self):
        got = R.parse_argv(["python3", "/x/claude-wait", "dev", SID_A, "--model", "opus", "--rename"])
        self.assertEqual((got["tool"], got["role"], got["session_id"], got["model"]), ("claude-wait", "dev", SID_A, "opus"))
        # bash -lic 'claude-wait …' の bash の引数は 1 つの文字列なので、claude-wait とは見なさない
        self.assertIsNone(R.parse_argv(["bash", "-lic", f"claude-wait dev {SID_A}"]))


class Build(unittest.TestCase):
    def test_roles_tabs_and_rows(self):
        r = built()
        self.assertEqual([t["title"] for t in r["tabs"]], ["main", "standby"])  # 統括のタブが先頭
        self.assertEqual(r["tabs"][0]["rows"], [["a", "b"], ["c"]])
        self.assertEqual(r["tabs"][0]["roles"], ["lead", "a", "b", "c"])  # overlay（role 無し）は入らない
        self.assertEqual(r["tabs"][1]["roles"], ["d", "e"])
        self.assertEqual(r["skipped"], ["bad,name"])
        self.assertEqual(r["left"], "lead")

    def test_sessions_matched_by_pid_cmdline_and_waiter(self):
        s = built()["sessions"]
        self.assertEqual((s["lead"]["session_id"], s["lead"]["cwd"], s["lead"]["state"]), (SID_L, "/lead", "running"))
        self.assertEqual(s["b"]["session_id"], SID_B)  # pid の JSON が無くても --resume の ID で
        self.assertEqual((s["b"]["kind"], s["b"]["name"]), ("bg", "bee"))
        self.assertEqual(s["c"]["session_id"], SID_C)  # -n だけで始めたものは JSON の ID
        self.assertEqual((s["d"]["state"], s["d"]["session_id"], s["d"]["model"]), ("waiting", SID_D, "haiku"))
        self.assertEqual((s["e"]["state"], s["e"]["session_id"], s["e"]["cwd"]), ("shell", None, "/e"))

    def test_waiter_found_whatever_the_order(self):
        w = win(1, "x", [proc(14, "claude", "--resume", SID_B), proc(101, "python3", "/u/claude-wait", "x", SID_A)])
        ps = R.pane_session(w, {}, {})
        self.assertEqual((ps["waiter_pid"], ps["wait_id"], ps["session_id"], ps["front_pid"]), (101, SID_A, SID_B, 14))

    def test_duplicate_ids_kept_once(self):
        # x の claude-wait が SID_A を待っていて、y で SID_A が動いている（別の場所から移している途中）
        osw = [{"tabs": [{"title": "t", "windows": [
            win(1, "x", [proc(101, "python3", "/u/claude-wait", "x", SID_A)]),
            win(2, "y", [proc(12, "claude", "--resume", SID_A)])]}]}]
        r = R.build_roster(osw, [{"pid": 12, "sessionId": SID_A}], "lead", tab_pane_fraction)
        self.assertEqual(r["sessions"]["y"]["session_id"], SID_A)
        self.assertIsNone(r["sessions"]["x"]["session_id"])
        self.assertEqual(r["sessions"]["x"]["dup_of"], "y")
        R.check_roster(json.loads(json.dumps(r)))  # 名簿として読める（保存も復元も止まらない）

    def test_carry_does_not_duplicate(self):
        old = built()
        old["sessions"]["e"]["session_id"] = SID_A  # e は前に SID_A を開いていた。いまは a が SID_A を持つ
        new = R.carry_over(built(), old, 200.0, 3600)
        self.assertIsNone(new["sessions"]["e"]["session_id"])
        R.check_roster(json.loads(json.dumps(new)))

    def test_tab_mark_stripped(self):
        ls = fake_ls()
        ls[0]["tabs"][0]["title"] = "● standby"
        r = R.build_roster(ls, fake_sessions(), "lead", tab_pane_fraction)
        self.assertEqual([t["title"] for t in r["tabs"]], ["main", "standby"])
        self.assertEqual(R.signature(r), R.signature(built()))

    def test_ignore(self):
        r = built(ignore={"c"})
        self.assertNotIn("c", r["sessions"])
        self.assertEqual(r["tabs"][0]["rows"], [["a", "b"]])

    def test_empty_and_odd_inputs(self):
        r = R.build_roster([], [], "lead", tab_pane_fraction)
        self.assertEqual((r["tabs"], r["sessions"]), ([], {}))
        r = R.build_roster([{"tabs": [{"title": "t", "windows": [win(1, "x", None)]}]}], [None, 3], "lead", tab_pane_fraction)
        self.assertEqual(r["sessions"]["x"]["state"], "shell")
        self.assertNotIn("rows", r["tabs"][0])  # 統括のいないタブに段組みは無い


class Model(unittest.TestCase):
    def call(self, conf=None, argv=None, settings=None, transcript=None):
        return R.resolve_model("a", "/a", SID_A, argv, conf, lambda cwd: settings, lambda sid: transcript)

    def test_order(self):
        self.assertEqual(self.call({"a": "fable"}, "opus", "sonnet", "haiku"), ("fable", "config"))
        self.assertEqual(self.call({"b": "fable"}, "opus", "sonnet", "haiku"), ("opus", "argv"))
        self.assertEqual(self.call(None, None, "sonnet", "haiku"), ("sonnet", "settings"))
        self.assertEqual(self.call(None, None, None, "claude-x"), ("claude-x", "transcript"))
        self.assertEqual(self.call(None, None, None, None), (None, None))
        self.assertEqual(self.call({"a": ""}, None, None, None), (None, None))  # 空の指定は無視

    def test_no_sid_skips_transcript(self):
        self.assertEqual(R.resolve_model("a", "/a", None, None, None, lambda c: None, lambda s: 1 / 0), (None, None))

    def test_settings_local_first(self):
        files = {"/p/.claude/settings.local.json": '{"model": "haiku"}', "/p/.claude/settings.json": '{"model": "opus"}',
                 "/q/.claude/settings.json": '{"model": "opus"}', "/r/.claude/settings.json": "broken"}

        def read(p):
            if p not in files:
                raise FileNotFoundError(p)
            return files[p]
        self.assertEqual(R.settings_model("/p", read), "haiku")
        self.assertEqual(R.settings_model("/q", read), "opus")
        self.assertIsNone(R.settings_model("/r", read))
        self.assertIsNone(R.settings_model(None, read))

    def test_transcript_last_main_assistant(self):
        lines = [json.dumps({"type": "assistant", "message": {"model": "old"}}),
                 json.dumps({"type": "assistant", "message": {"model": "new"}}),
                 json.dumps({"type": "assistant", "isSidechain": True, "message": {"model": "sub"}}),
                 json.dumps({"type": "assistant", "message": {"model": "<synthetic>"}}),
                 json.dumps({"type": "user", "message": {"model": "x"}}), "{broken"]
        self.assertEqual(R.transcript_model(lines), "new")
        self.assertIsNone(R.transcript_model([]))

    def test_build_records_source(self):
        s = R.build_roster(fake_ls(), fake_sessions(), "lead", tab_pane_fraction, conf_models={"c": "fable"},
                           settings_fn=lambda cwd: "opus" if cwd == "/lead" else None,
                           transcript_fn=lambda sid: "tmodel")["sessions"]
        self.assertEqual((s["c"]["model"], s["c"]["model_source"]), ("fable", "config"))
        self.assertEqual((s["a"]["model"], s["a"]["model_source"]), ("sonnet", "argv"))
        self.assertEqual((s["lead"]["model"], s["lead"]["model_source"]), ("opus", "settings"))
        self.assertEqual((s["b"]["model"], s["b"]["model_source"]), ("tmodel", "transcript"))
        self.assertEqual((s["e"]["model"], s["e"]["model_source"]), (None, None))  # ID が無ければ記録も見ない


class SessionFile(unittest.TestCase):
    def roster(self):
        r = built()
        r["sessions"]["lead"]["model"] = "opus"
        r["sessions"]["b"]["model"] = "my model"  # 空白も壊れずに渡る
        return R.check_roster(json.loads(json.dumps(r)))

    def launches(self, text):
        out = {}
        for line in text.splitlines():
            if line.startswith("launch "):
                a = shlex.split(line)
                role = a[a.index("--var") + 1].split("=", 1)[1]
                out[role] = a
        return out

    def test_file_contents(self):
        r = self.roster()
        text = R.session_file(r, ["lead", "a", "b", "c", "d", "e"], "/bin/claude-wait")
        L = self.launches(text)
        self.assertEqual(set(L), {"lead", "a", "b", "c", "d", "e"})
        lead = L["lead"]
        self.assertEqual(lead[lead.index("--cwd") + 1], "/lead")
        self.assertEqual(lead[-3:-1], ["bash", "-lic"])
        self.assertEqual(shlex.split(lead[-1]), ["/bin/claude-wait", "lead", SID_L, "--model", "opus"])
        self.assertEqual(shlex.split(L["b"][-1]), ["/bin/claude-wait", "b", SID_B, "--model", "my model", "--rename"])
        self.assertEqual(L["e"][-2:], ["bash", "-l"])  # ID の無い役はシェルだけ（役の名前と場所は戻す）
        # 統括のタブが先頭で、統括が最初のペイン。standby は題で作る（claude-view が題で探す）
        lines = text.splitlines()
        self.assertEqual(lines[lines.index("new_tab main") + 2].split()[2], "lead")
        self.assertIn("new_tab standby", lines)
        self.assertLess(lines.index("new_tab main"), lines.index("new_tab standby"))
        for bad in ("close", "send-text", "send_text"):
            self.assertNotIn(bad, text)

    def test_main_tab_shape(self):
        lines = R.main_tab_lines("lead", [["a", "b"], ["c"]], self.roster()["sessions"], "w")
        loc = [(shlex.split(x)[2], next((y for y in shlex.split(x) if y.startswith("--location")), None),
                next((y for y in shlex.split(x) if y.startswith("--bias")), None))
               if x.startswith("launch") else x for x in lines]
        self.assertEqual(loc, [
            ("lead", None, None),
            ("a", "--location=vsplit", "--bias=67"),  # 右側 2 列 + 統括 1 列の等分 → 統括 33%
            "focus_matching_window var:role=a", ("c", "--location=hsplit", "--bias=50"),
            "focus_matching_window var:role=a", ("b", "--location=vsplit", "--bias=50")])

    def test_main_tab_pinned_first(self):
        sess = self.roster()["sessions"]
        lines = R.main_tab_lines("lead", [["a", "b"], ["c"]], sess, "w", pinned=["c"])
        first = [shlex.split(x)[2] for x in lines if x.startswith("launch")]
        self.assertEqual(first[:2], ["lead", "c"])  # 統括のすぐ右 = 1 段目の先頭
        self.assertIn("--bias=75", lines[1])  # c が抜けて 1 段になり、c,a,b の 3 列 + 統括 → 統括 25%
        self.assertEqual(first, ["lead", "c", "a", "b"])
        # left と同じ・名簿に無い pinned は置かない
        self.assertEqual(R.main_tab_lines("lead", [["a"]], sess, "w", pinned=["lead", "ghost"]),
                         R.main_tab_lines("lead", [["a"]], sess, "w"))

    def test_session_file_pinned_from_other_tab(self):
        r = self.roster()
        text = R.session_file(r, ["lead", "a", "b", "c", "d", "e"], "w", pinned=["d"])
        lines = text.splitlines()
        i = lines.index("new_tab main")
        self.assertEqual(shlex.split(lines[i + 3])[2], "d")  # 統括の次 = 1 段目の先頭
        launches = [x for x in lines if x.startswith("launch ") and "role=d" in shlex.split(x)]
        self.assertEqual(len(launches), 1, text)  # 元のタブ（standby）には開かない（二重に開かない）
        # 開かない pinned は置かない
        self.assertNotIn("role=d", R.session_file(r, ["lead", "a"], "w", pinned=["d"]))
        self.assertEqual(R.target_tab(r, "d", ["d"]), R.STANDBY)

    def test_only_launched_roles_and_missing_rows(self):
        r = self.roster()
        text = R.session_file(r, ["lead", "c", "d"], "w")
        self.assertEqual(set(self.launches(text)), {"lead", "c", "d"})
        self.assertIsNone(R.session_file(r, [], "w"))

    def test_without_left(self):
        text = R.session_file(self.roster(), ["a", "d"], "w")
        self.assertNotIn("new_tab main", text)
        self.assertEqual(set(self.launches(text)), {"a", "d"})  # 統括のタブにいた a も standby に開く

    def test_rows_spec_and_filter(self):
        self.assertEqual(R.rows_spec([["a", "b"], ["c"]]), "a,b/c")
        self.assertEqual(R.rows_spec([]), "")
        self.assertEqual(R.filter_rows([["a", "b"], ["c"]], {"b", "c"}), [["b"], ["c"]])
        self.assertEqual(R.filter_rows([["a"], ["c"]], {"c"}), [["c"]])
        self.assertEqual(R.filter_rows(None, {"c"}), [])


class Plan(unittest.TestCase):
    def roster(self):
        return R.check_roster(json.loads(json.dumps(built())))

    def test_only_missing(self):
        launch, skipped = R.plan_restore(self.roster(), {"lead", "a", "d"}, {})
        self.assertEqual((launch, skipped), (["b", "c", "e"], []))

    def test_busy_ids_are_not_opened_twice(self):
        launch, skipped = R.plan_restore(self.roster(), {"lead"}, {SID_A: "いま動いている (pid 5)", SID_D: "claude-wait が待っている"})
        self.assertNotIn("a", launch)
        self.assertNotIn("d", launch)
        self.assertEqual([r for r, _ in skipped], ["a", "d"])
        self.assertIn("二重に開かない", skipped[0][1])

    def test_nothing_present(self):
        launch, _ = R.plan_restore(self.roster(), set(), {})
        self.assertEqual(launch, ["lead", "a", "b", "c", "d", "e"])

    def test_absent_and_named(self):
        r = self.roster()
        r["sessions"]["c"]["absent_since"] = 50.0
        launch, skipped = R.plan_restore(r, {"lead"}, {})
        self.assertNotIn("c", launch)
        self.assertEqual(skipped[0][0], "c")
        self.assertIn("c", R.plan_restore(r, {"lead"}, {}, include_absent=True)[0])
        self.assertEqual(R.plan_restore(r, {"lead"}, {}, only=["c"])[0], ["c"])
        self.assertEqual(R.plan_restore(r, {"lead", "c"}, {}, only=["c"])[0], [])  # いれば開かない
        with self.assertRaises(ValueError):
            R.plan_restore(r, set(), {}, only=["nobody"])

    def test_launch_args(self):
        r = self.roster()
        self.assertEqual(R.target_tab(r, "a"), "standby")  # 統括のタブの役は standby に足して claude-view で出す
        self.assertEqual(R.target_tab(r, "e"), "standby")
        a = R.launch_args("a", r["sessions"]["a"], "w", "standby", True)
        self.assertEqual(a[:2], ["launch", "--keep-focus"])  # 入力先を動かさない
        self.assertIn("title:^standby$", a)
        self.assertEqual(a[a.index("--cwd") + 1], "/a")
        b = R.launch_args("a", r["sessions"]["a"], "w", "new tab", False)
        self.assertIn("--type=tab", b)
        self.assertEqual(b[b.index("--tab-title") + 1], "new tab")
        self.assertNotIn("--match", b)

    def test_check_roster(self):
        r = self.roster()
        r["sessions"]["e"]["session_id"] = SID_A
        with self.assertRaises(ValueError):
            R.check_roster(r)  # 同じ ID を 2 人に持たせない
        for bad in ({}, {"version": 99}, {"version": 1, "sessions": [], "tabs": []},
                    {"version": 1, "sessions": {"x": {}}, "tabs": [{"roles": ["y"]}]},
                    {"version": 1, "sessions": {"x": {"session_id": "nope"}}, "tabs": []},
                    {"version": 1, "sessions": {"a,b": {}}, "tabs": []}):
            with self.assertRaises(ValueError):
                R.check_roster(bad)


class Carry(unittest.TestCase):
    def test_shell_pane_keeps_previous_id(self):
        old = built()
        old["sessions"]["e"].update(session_id=SID_C.replace("4", "6"), model="opus", name="e")
        new = R.carry_over(built(), old, 200.0, 3600)
        self.assertEqual(new["sessions"]["e"]["session_id"], old["sessions"]["e"]["session_id"])
        self.assertTrue(new["sessions"]["e"]["carried"])

    def test_absent_kept_then_dropped(self):
        old = built()
        cur = R.build_roster([{"tabs": [t for t in fake_ls()[0]["tabs"] if t["title"] == "main"]}], fake_sessions(),
                             "lead", tab_pane_fraction, now=200.0)
        new = R.carry_over(cur, old, 200.0, 3600)
        self.assertEqual(new["sessions"]["d"]["absent_since"], 200.0)
        self.assertIn("d", next(t for t in new["tabs"] if t["title"] == "standby")["roles"])
        R.check_roster(json.loads(json.dumps(new)))
        later = R.carry_over(cur, new, 200.0 + 3599, 3600)
        self.assertEqual(later["sessions"]["d"]["absent_since"], 200.0)  # 消えた時刻は最初のまま
        gone = R.carry_over(cur, new, 200.0 + 3601, 3600)
        self.assertNotIn("d", gone["sessions"])
        self.assertNotIn("d", R.carry_over(cur, old, 200.0, 3600, drop_absent=True)["sessions"])  # 手の save はいまが正

    def test_no_old(self):
        self.assertEqual(R.carry_over(built(), None, 1, 1), built())

    def test_signature_ignores_volatile(self):
        a, b = built(), built()
        b["saved_at"] = 999
        b["sessions"]["a"].update(status="idle", pid=1, window_id=77, state="shell")
        self.assertEqual(R.signature(a), R.signature(b))
        b["sessions"]["a"]["session_id"] = SID_C
        self.assertNotEqual(R.signature(a), R.signature(b))


class Restart(unittest.TestCase):
    def pane(self, **kw):
        p = {"state": "running", "session_id": SID_A, "front_pid": 12, "waiter_pid": 101}
        p.update(kw)
        return p

    def test_refusals(self):
        sj = {"status": "idle", "pid": 12, "sessionId": SID_A}
        w = {"pid": 101}
        ok = R.restart_refusal("a", self.pane(), sj, "idle", False, w)
        self.assertIsNone(ok)
        self.assertIsNone(R.restart_refusal("a", self.pane(), sj, "done", False, w))
        self.assertIsNone(R.restart_refusal("a", self.pane(), sj, "", False, w))
        self.assertIn("保護するプロセス", R.restart_refusal("a", self.pane(), sj, "idle", True, w))
        self.assertIn("idle でない", R.restart_refusal("a", self.pane(), dict(sj, status="busy"), "idle", False, w))
        self.assertIn("idle でない", R.restart_refusal("a", self.pane(), dict(sj, status=None), "idle", False, w))
        self.assertIn("claude-state=working", R.restart_refusal("a", self.pane(), sj, "working", False, w))
        self.assertIn("claude-state=waiting", R.restart_refusal("a", self.pane(), sj, "waiting", False, w))
        self.assertIn("claude-wait", R.restart_refusal("a", self.pane(), sj, "idle", False, None))
        self.assertIn("claude-wait", R.restart_refusal("a", self.pane(), sj, "idle", False, {"pid": 5}))
        self.assertIn("claude-wait", R.restart_refusal("a", self.pane(waiter_pid=None), sj, "idle", False, w))
        self.assertIn("ペインが kitty に無い", R.restart_refusal("a", None, sj, "idle", False, w))
        self.assertIn("動いていない", R.restart_refusal("a", self.pane(state="waiting"), sj, "idle", False, w))
        self.assertIn("見つからない", R.restart_refusal("a", self.pane(), None, "idle", False, w))

    def test_stop_target(self):
        sj = {"pid": 12, "sessionId": SID_A, "kind": "interactive"}
        self.assertEqual(R.stop_target(self.pane(), sj, [sj]), 12)
        bg = {"pid": 900, "sessionId": SID_A, "kind": "bg"}
        self.assertIsNone(R.stop_target(self.pane(), bg, [bg]))  # 既定では背景セッションは止めない
        self.assertEqual(R.stop_target(self.pane(), bg, [bg], allow_bg=True), 900)  # 前面でなく daemon の中の host
        other = {"pid": 900, "sessionId": SID_B, "kind": "bg"}
        self.assertIsNone(R.stop_target(self.pane(), bg, [bg, other], allow_bg=True))  # 共有の pid は止めない
        self.assertIsNone(R.stop_target(self.pane(), {"kind": "bg", "sessionId": SID_A}, [], allow_bg=True))

    def test_read_next(self):
        good = json.dumps(R.next_payload(42, SID_A, SID_A, "/x", "opus", 1000.0))
        self.assertEqual(R.read_next(good, 42, SID_A, 1010.0), {"session_id": SID_A, "cwd": "/x", "model": "opus"})
        # /clear のあと: ファイルは claude-wait の引数の ID 宛て、開くのはいまの ID
        cleared = json.dumps(R.next_payload(42, SID_A, SID_C, None, None, 1000.0))
        self.assertEqual(R.read_next(cleared, 42, SID_A, 1010.0)["session_id"], SID_C)
        self.assertIsNone(R.read_next(json.dumps(dict(json.loads(good), session_id="x")), 42, SID_A, 1010.0))
        self.assertIsNone(R.read_next(good, 43, SID_A, 1010.0))  # 別の claude-wait 宛て
        self.assertIsNone(R.read_next(good, 42, SID_B, 1010.0))
        self.assertIsNone(R.read_next(good, 42, SID_A, 1000.0 + R.NEXT_MAX_AGE + 1))  # 古い
        self.assertIsNone(R.read_next(good, 42, SID_A, 900.0))  # 未来の時刻
        self.assertIsNone(R.read_next("{", 42, SID_A, 1010.0))
        self.assertIsNone(R.read_next(None, 42, SID_A, 1010.0))
        self.assertIsNone(R.read_next(json.dumps(dict(json.loads(good), cwd=3)), 42, SID_A, 1010.0))
        self.assertEqual(R.read_next(json.dumps(R.next_payload(42, SID_A, SID_A, None, None, 1000.0)), 42, SID_A, 1000.0),
                         {"session_id": SID_A, "cwd": None, "model": None})


class Status(unittest.TestCase):
    def test_diff(self):
        saved = built()
        now = json.loads(json.dumps(saved))
        self.assertEqual(R.diff_lines(saved, now), [])
        del now["sessions"]["c"]
        now["sessions"]["a"]["session_id"] = SID_C
        now["sessions"]["b"]["cwd"] = "/elsewhere"
        now["sessions"]["new"] = {"state": "running"}
        now["sessions"]["lead"]["state"] = "shell"
        lines = R.diff_lines(saved, now)
        self.assertTrue(any(x.startswith("c: いない") for x in lines))
        self.assertTrue(any(x.startswith("a: ID が変わった aaaaaaaa → aaaaaaaa") for x in lines))
        self.assertTrue(any(x.startswith("b: 別のフォルダ") for x in lines))
        self.assertTrue(any(x.startswith("new: 名簿に無い") for x in lines))
        self.assertTrue(any(x.startswith("lead: claude が動いていない（shell）") for x in lines))
        self.assertTrue(all(x.split(":")[0] for x in R.diff_lines(saved, None)))


# ---- 実際のプロセスを使うもの（偽の claude と偽の kitten） ---------------------------

FAKE_CLAUDE = r"""#!/bin/bash
# 偽の claude: --resume SID [--model M] [-n N]。sessions に JSON を書いて眠る。SIGTERM で JSON を消して終わる
sid=$2; model=""
[ "$3" = --model ] && model=$4
echo "start $sid model=$model cwd=$PWD" >> "$FAKE_CLAUDE_LOG"
st=$(cut -d' ' -f22 /proc/$$/stat)
printf '{"pid":%d,"sessionId":"%s","cwd":"%s","procStart":"%s","status":"%s","kind":"interactive"}' \
  $$ "$sid" "$PWD" "$st" "$(cat "$FAKE_STATUS_FILE" 2>/dev/null || echo idle)" > "$CLAUDE_SESSIONS_DIR/$$.json"
if [ -n "$FAKE_HOOK" ]; then "$FAKE_HOOK" "$sid"; fi
if [ -n "$FAKE_ONESHOT" ]; then rm -f "$CLAUDE_SESSIONS_DIR/$$.json"; exit 0; fi
trap 'rm -f "$CLAUDE_SESSIONS_DIR/$$.json"; echo "term $sid" >> "$FAKE_CLAUDE_LOG"; kill $! 2>/dev/null; exit 0' TERM
sleep 120 & wait
"""

FAKE_KITTEN = r"""#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
if a[:1] == ["@"]:
    a = a[1:]
if a[:1] == ["--to"]:
    a = a[2:]
with open(os.environ["FAKE_KITTEN_LOG"], "a") as f:
    f.write(json.dumps(a) + "\n")
if a[:1] == ["ls"]:
    sys.stdout.write(open(os.environ["FAKE_LS"]).read())
"""


class Live(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.d = Path(self.tmp.name)
        for sub in ("sessions", "rt", "bin", "data", "work", "other"):
            (d / sub).mkdir()
        (d / "rt").chmod(0o700)
        for name, body in (("claude", FAKE_CLAUDE), ("kitten", FAKE_KITTEN)):
            (d / "bin" / name).write_text(body)
            (d / "bin" / name).chmod(0o755)
        (d / "config.json").write_text(json.dumps({"view": {"left": "lead", "guard": []}}))
        self.log = d / "claude.log"
        self.klog = d / "kitten.log"
        self.env = dict(os.environ, CLAUDE_SESSIONS_DIR=str(d / "sessions"), XDG_RUNTIME_DIR=str(d / "rt"),
                        CLAUDE_WAIT_CLAUDE=str(d / "bin/claude"), CLAUDE_WAIT_INTERVAL="0.1", CLAUDE_WAIT_NO_SHELL="1",
                        FAKE_CLAUDE_LOG=str(self.log), FAKE_STATUS_FILE=str(d / "status"),
                        CLAUDE_KITTY_KITTEN=str(d / "bin/kitten"), FAKE_KITTEN_LOG=str(self.klog), FAKE_LS=str(d / "ls.json"),
                        CLAUDE_KITTY_CONFIG=str(d / "config.json"), CLAUDE_ROSTER_DIR=str(d / "data"),
                        CLAUDE_PROJECTS_DIR=str(d / "projects"), CLAUDE_ROSTER_WAIT_CMD="claude-wait")
        for k in ("KITTY_LISTEN_ON", "CLAUDE_VIEW_LEFT", "CLAUDE_VIEW_GUARD", "CLAUDE_KITTY_CLASS"):
            self.env.pop(k, None)
        # 偽の kitty の socket（名前の pid が生きていればよい）
        (d / "rt" / f"kitty-{os.getpid()}").write_text("")
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()
            if p.stdout:
                p.stdout.close()
        # 偽の claude が残っていれば止める
        for f in (self.d / "sessions").glob("*.json"):
            try:
                os.kill(int(f.stem), 9)
            except (OSError, ValueError):
                pass
        self.tmp.cleanup()

    def wait_until(self, cond, t=15, what="条件"):
        deadline = time.monotonic() + t
        while time.monotonic() < deadline:
            v = cond()
            if v:
                return v
            time.sleep(0.05)
        self.fail(f"{what} にならない。claude.log={self.log.read_text() if self.log.exists() else ''!r}")

    def lines(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def start_wait(self, *args, cwd=None):
        p = subprocess.Popen([sys.executable, str(ROOT / "bin/claude-wait"), *args], env=self.env, cwd=cwd or self.d / "work",
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        self.procs.append(p)
        return p

    def roster(self, *args, ok=(0,)):
        r = subprocess.run([sys.executable, str(ROOT / "bin/claude-roster"), *args], env=self.env,
                           capture_output=True, text=True, timeout=90)
        self.assertIn(r.returncode, ok, r.stdout + r.stderr)
        return r

    def session_json(self, sid=SID_A):
        for f in (self.d / "sessions").glob("*.json"):
            try:
                d = json.loads(f.read_text())
            except ValueError:
                continue
            if d.get("sessionId") == sid:
                return d
        return None

    # -- claude-wait の続きの指示 --

    def test_wait_passes_model(self):
        self.env["FAKE_ONESHOT"] = "1"
        p = self.start_wait("t", SID_A, "--model", "opus")
        p.wait(timeout=15)
        self.assertEqual(self.lines(), [f"start {SID_A} model=opus cwd={self.d / 'work'}"])
        self.assertFalse((self.d / "rt/claude-wait" / f"{SID_A}.waiter").exists())  # 終われば消す

    def write_next_hook(self, at_offset=0, waiter=True, cwd=None, model="haiku"):
        """偽の claude の 1 回目に、続きの指示を書かせる（restart の代わり）。2 回目以降は書かない。"""
        hook = self.d / "bin/hook"
        nxt = self.d / "rt/claude-wait" / f"{SID_A}.next"
        waiter_file = self.d / "rt/claude-wait" / f"{SID_A}.waiter"
        hook.write_text(f"""#!/usr/bin/env python3
import json, os, sys, time
flag = {str(self.d / 'hooked')!r}
if os.path.exists(flag):
    sys.exit(0)
open(flag, "w").close()
pid = json.load(open({str(waiter_file)!r}))["pid"] if {waiter!r} else 1
json.dump({{"waiter_pid": pid, "wait_id": {SID_A!r}, "session_id": {SID_A!r}, "cwd": {str(cwd or self.d / 'other')!r},
           "model": {model!r}, "at": time.time() + {at_offset}}}, open({str(nxt)!r}, "w"))
""")
        hook.chmod(0o755)
        self.env.update(FAKE_HOOK=str(hook), FAKE_ONESHOT="1")

    def test_wait_follows_next_file(self):
        self.write_next_hook()
        p = self.start_wait("t", SID_A, "--model", "opus", "--rename")
        p.wait(timeout=20)
        out = p.stdout.read().decode()
        self.assertEqual(self.lines(), [f"start {SID_A} model=opus cwd={self.d / 'work'}",
                                        f"start {SID_A} model=haiku cwd={self.d / 'other'}"])
        self.assertIn("続きの指示で再開します", out)
        self.assertEqual(out.count("/rename t"), 2)  # 開き直すたびに案内する
        self.assertFalse((self.d / "rt/claude-wait" / f"{SID_A}.next").exists())  # 1 回で消える

    def test_wait_ignores_stale_or_foreign_next(self):
        for kw in ({"at_offset": -R.NEXT_MAX_AGE - 10}, {"waiter": False}):
            with self.subTest(**kw):
                for f in (self.d / "hooked", self.log):
                    f.unlink(missing_ok=True)
                self.write_next_hook(**kw)
                p = self.start_wait("t", SID_A)
                p.wait(timeout=20)
                self.assertEqual(len(self.lines()), 1)  # シェルに戻る（勝手に開き直さない）

    def test_wait_bad_cwd_stays(self):
        self.write_next_hook(cwd=str(self.d / "nope"))
        p = self.start_wait("t", SID_A)
        p.wait(timeout=20)
        self.assertEqual(self.lines()[1], f"start {SID_A} model=haiku cwd={self.d / 'work'}")

    # -- claude-roster を偽の kitty で --

    def write_ls(self, windows_main, windows_standby=()):
        tabs = [{"id": 1, "title": "main", "windows": windows_main,
                 "groups": [{"id": w["id"], "windows": [w["id"]]} for w in windows_main],
                 "layout_state": {"class": "Splits", "pairs": self.pairs([w["id"] for w in windows_main])}}]
        if windows_standby:
            tabs.append({"id": 2, "title": "standby", "windows": list(windows_standby), "groups": [],
                         "layout_state": {"class": "Splits", "pairs": {"one": windows_standby[0]["id"]}}})
        (self.d / "ls.json").write_text(json.dumps([{"id": 1, "wm_class": "kitty-claude", "tabs": tabs}]))

    @staticmethod
    def pairs(ids):
        if len(ids) == 1:
            return {"one": ids[0]}
        right = ids[1] if len(ids) == 2 else {"horizontal": False, "one": ids[1], "two": ids[2]}
        return {"bias": 0.33, "one": ids[0], "two": right}

    def saved(self):
        return json.loads((self.d / "data/roster.json").read_text())

    def test_save_generations_and_no_kitty(self):
        self.write_ls([win(1, "lead", []), win(2, "a", []), win(3, "b", [])])
        self.roster("save")
        r = self.saved()
        self.assertEqual(r["tabs"][0]["rows"], [["a"], ["b"]])
        self.roster("save")
        self.assertFalse((self.d / "data/roster.json.1").exists())  # 中身が同じなら世代を回さない
        self.write_ls([win(1, "lead", []), win(2, "a", [])])
        self.roster("save")
        self.assertTrue((self.d / "data/roster.json.1").exists())
        self.assertEqual(json.loads((self.d / "data/roster.json.1").read_text())["tabs"][0]["rows"], [["a"], ["b"]])
        # kitty が無いときは上書きしない（落ちた直後のタイマーで名簿を失わない）
        (self.d / "rt" / f"kitty-{os.getpid()}").unlink()
        self.roster("save", "--auto")
        self.assertEqual(self.saved()["tabs"][0]["rows"], [["a"]])
        self.roster("save", ok=(1,))

    def test_auto_save_keeps_closed_pane(self):
        self.write_ls([win(1, "lead", []), win(2, "a", []), win(3, "b", [])])
        self.roster("save")
        self.write_ls([win(1, "lead", []), win(2, "a", [])])
        self.roster("save", "--auto")
        self.assertIn("absent_since", self.saved()["sessions"]["b"])
        st = self.roster("status").stdout
        self.assertIn("b: 保存の前からペインが無い", st)
        self.roster("save")
        self.assertNotIn("b", self.saved()["sessions"])

    def test_restore_adds_only_missing_and_skips_live_ids(self):
        sleeper = subprocess.Popen(["sleep", "60"]); self.procs.append(sleeper)
        st = Path(f"/proc/{sleeper.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        roster = {"version": 1, "saved_at": 1.0, "left": "lead",
                  "tabs": [{"title": "main", "roles": ["lead", "a", "b"], "rows": [["a", "b"]]},
                           {"title": "standby", "roles": ["c"]}],
                  "sessions": {"lead": {"session_id": SID_L, "cwd": "/l"}, "a": {"session_id": SID_A, "cwd": "/a"},
                               "b": {"session_id": SID_B, "cwd": "/b", "model": "opus"},
                               "c": {"session_id": SID_C, "cwd": "/c"}}}
        (self.d / "data/roster.json").write_text(json.dumps(roster))
        # c の ID は別の場所で動いている（開けば二重になる）
        (self.d / "sessions" / f"{sleeper.pid}.json").write_text(
            json.dumps({"pid": sleeper.pid, "sessionId": SID_C, "procStart": st}))
        self.write_ls([win(1, "lead", []), win(2, "a", [])])
        out = self.roster("restore").stdout
        calls = [json.loads(x) for x in self.klog.read_text().splitlines()]
        launches = [c for c in calls if c[:1] == ["launch"]]
        self.assertEqual(len(launches), 1, out)
        la = launches[0]
        self.assertEqual(la[la.index("--var") + 1], "role=b")
        self.assertEqual(shlex.split(la[-1]), ["claude-wait", "b", SID_B, "--model", "opus"])
        self.assertIn("--keep-focus", la)
        self.assertIn("開かない: c", out)
        self.assertIn("claude-view rows 'a,b'", out)
        flat = json.dumps(calls)
        for bad in ("close-window", "close-tab", "send-text", "send-key"):
            self.assertNotIn(bad, flat)

    def pinned_restore(self, ls_roles, main_tab=True):
        cfg = json.loads((self.d / "config.json").read_text())
        cfg["view"]["pinned"] = ["c"]
        (self.d / "config.json").write_text(json.dumps(cfg))
        tabs = ([{"title": "main", "roles": ["lead", "a", "b"], "rows": [["a"], ["b"]]}] if main_tab else []) + [
            {"title": "work", "roles": ["c"]}]
        roster = {"version": 1, "saved_at": 1.0, "left": "lead", "tabs": tabs,
                  "sessions": {r: {"session_id": sid, "cwd": "/" + r}
                               for r, sid in (("lead", SID_L), ("a", SID_A), ("b", SID_B), ("c", SID_C))}}
        if not main_tab:
            roster["tabs"].insert(0, {"title": "main", "roles": ["lead", "a", "b"]})
        (self.d / "data/roster.json").write_text(json.dumps(roster))
        self.write_ls([win(i + 1, r, []) for i, r in enumerate(ls_roles)])
        out = self.roster("restore").stdout
        calls = [json.loads(x) for x in self.klog.read_text().splitlines()]
        tabs_of = {c[c.index("--var") + 1]: (c[c.index("--match") + 1] if "--match" in c else c[c.index("--tab-title") + 1])
                   for c in calls if c[:1] == ["launch"]}
        return out, tabs_of

    def test_restore_pinned_only_uses_equalize(self):
        # 足したのが常時表示だけなら、保存した段組みでなく、いまの並びのまま先頭に置く
        out, tabs_of = self.pinned_restore(["lead", "b", "a"])
        self.assertEqual(tabs_of, {"role=c": "standby"})  # 名簿のタブ（work）でなく standby から前に出す
        self.assertIn("並べ直す: claude-view equalize", out)
        self.assertNotIn("claude-view rows", out)

    def test_restore_pinned_with_row_role_goes_first(self):
        out, tabs_of = self.pinned_restore(["lead", "a"])
        self.assertEqual(set(tabs_of), {"role=b", "role=c"})
        self.assertIn("並べ直す: claude-view rows 'c,a/b'", out)

    def test_restore_pinned_without_rearrange_keeps_roster_tab(self):
        # 統括のタブに段組みが無い（並べ直しが走らない）なら、standby に入れず名簿のタブへ
        out, tabs_of = self.pinned_restore(["lead", "a", "b"], main_tab=False)
        self.assertEqual(tabs_of, {"role=c": "work"})
        self.assertNotIn("並べ直す", out)

    def test_restore_without_kitty_writes_session_file(self):
        roster = {"version": 1, "saved_at": 1.0, "left": "lead",
                  "tabs": [{"title": "main", "roles": ["lead", "a"], "rows": [["a"]]}],
                  "sessions": {"lead": {"session_id": SID_L, "cwd": "/l"}, "a": {"session_id": SID_A, "cwd": "/a"}}}
        (self.d / "data/roster.json").write_text(json.dumps(roster))
        (self.d / "rt" / f"kitty-{os.getpid()}").unlink()
        out = self.roster("restore", "--dry-run").stdout
        self.assertIn("kitty が無いので", out)
        self.assertIn(f"claude-wait lead {SID_L}", out)
        self.assertIn("--var role=a", out)

    def start_pane(self, role, sid, model):
        """claude-wait で偽の claude を開いたペインを作り、ls にそのプロセスを載せる。"""
        p = self.start_wait(role, sid, "--model", model)
        js = self.wait_until(lambda: self.session_json(sid), what="偽の claude の起動")
        front = js["pid"]
        return p, front

    def test_restart_resumes_in_same_pane_with_new_model(self):
        p, front = self.start_pane("a", SID_A, "opus")
        self.write_ls([win(1, "lead", []),
                       win(2, "a", [proc(p.pid, "python3", "claude-wait", "a", SID_A),
                                    proc(front, "/bin/bash", str(self.d / "bin/claude"), "--resume", SID_A)])])
        out = self.roster("restart", "a", "--model", "haiku", "--cwd", str(self.d / "other")).stdout
        self.assertIn("開き直した", out)
        self.assertEqual(self.lines(), [f"start {SID_A} model=opus cwd={self.d / 'work'}", f"term {SID_A}",
                                        f"start {SID_A} model=haiku cwd={self.d / 'other'}"])
        self.assertIsNone(p.poll())  # claude-wait は同じまま（ペインも同じ）
        self.assertNotEqual(self.session_json()["pid"], front)

    def pane_ls(self, p, front, sid=SID_A, wait_id=SID_A):
        self.write_ls([win(1, "lead", []),
                       win(2, "a", [proc(p.pid, "python3", "claude-wait", "a", wait_id),
                                    proc(front, "/bin/bash", str(self.d / "bin/claude"), "--resume", sid)])])

    def test_restart_twice_and_stale_roster_entry(self):
        p, front = self.start_pane("a", SID_A, "opus")
        self.pane_ls(p, front)
        self.roster("save")
        self.roster("restart", "a", "--model", "haiku")
        js = self.wait_until(lambda: (lambda j: j if j and j["pid"] != front else None)(self.session_json()), what="1 回目")
        self.pane_ls(p, js["pid"])
        # 2 回目: 同じ claude-wait で、また開き直せる（モデルの指定が無ければ、いまのモデルのまま）
        self.roster("restart", "a")
        self.wait_until(lambda: len(self.lines()) == 5, what="2 回目")
        self.assertEqual(self.lines()[-1], f"start {SID_A} model=haiku cwd={self.d / 'work'}")
        # 名簿の ID が違う（保存のあとにセッションが替わった）なら、名簿のフォルダは使わない
        roster = self.saved()
        roster["sessions"]["a"].update(session_id=SID_B, cwd=str(self.d / "other"), model="fable")
        (self.d / "data/roster.json").write_text(json.dumps(roster))
        js = self.session_json()
        self.pane_ls(p, js["pid"])
        self.roster("restart", "a")
        self.wait_until(lambda: len(self.lines()) == 7, what="3 回目")
        self.assertEqual(self.lines()[-1], f"start {SID_A} model=haiku cwd={self.d / 'work'}")

    def test_restart_after_clear(self):
        # claude-wait は SID_A で開いたが、/clear で SID_C になった（JSON の ID が変わる）
        p, front = self.start_pane("a", SID_A, "opus")
        f = self.d / "sessions" / f"{front}.json"
        f.write_text(json.dumps(dict(json.loads(f.read_text()), sessionId=SID_C)))
        self.pane_ls(p, front)
        out = self.roster("restart", "a").stdout
        self.assertIn("開き直した", out)
        self.assertEqual(self.lines()[-1], f"start {SID_C} model=opus cwd={self.d / 'work'}")  # いまの会話に戻る

    def test_broken_roster_does_not_stop_autosave(self):
        (self.d / "data/roster.json").write_text("{broken")
        self.write_ls([win(1, "lead", []), win(2, "a", [])])
        self.roster("save", "--auto")
        self.assertEqual(self.saved()["tabs"][0]["rows"], [["a"]])
        self.assertTrue((self.d / "data/roster.json.bad").exists())

    def test_autosave_skips_when_lead_missing(self):
        self.write_ls([win(1, "lead", []), win(2, "a", [])])
        self.roster("save")
        self.write_ls([win(2, "a", [])])
        self.roster("save", "--auto")
        self.assertIn("lead", self.saved()["sessions"])
        self.assertEqual(self.saved()["tabs"][0]["rows"], [["a"]])

    def test_restart_refuses_busy_guarded_and_foreign(self):
        (self.d / "status").write_text("busy")
        p, front = self.start_pane("a", SID_A, "opus")
        self.write_ls([win(1, "lead", []),
                       win(2, "a", [proc(p.pid, "python3", "claude-wait", "a", SID_A),
                                    proc(front, "/bin/bash", str(self.d / "bin/claude"), "--resume", SID_A)])])
        r = self.roster("restart", "a", ok=(3,))
        self.assertIn("idle でない", r.stderr)
        # 保護するプロセス（view.guard）が動いているとき
        guarded = subprocess.Popen(["sleep", "61.234"]); self.procs.append(guarded)
        (self.d / "config.json").write_text(json.dumps({"view": {"left": "lead", "guard": ["^sleep 61\\.234"]}}))
        f = self.d / "sessions" / f"{front}.json"
        f.write_text(json.dumps(dict(json.loads(f.read_text()), status="idle")))
        r = self.roster("restart", "a", ok=(2,))
        self.assertIn("保護するプロセス", r.stderr)
        guarded.kill(); guarded.wait()
        # claude-wait から開かれていない claude（親が違う）は止めない
        (self.d / "config.json").write_text(json.dumps({"view": {"left": "lead", "guard": []}}))
        other = subprocess.Popen(["sleep", "60"]); self.procs.append(other)
        self.write_ls([win(1, "lead", []),
                       win(2, "a", [proc(other.pid, "python3", "claude-wait", "a", SID_A),
                                    proc(front, "/bin/bash", str(self.d / "bin/claude"), "--resume", SID_A)])])
        r = self.roster("restart", "a", ok=(3,))
        self.assertIn("claude-wait から開かれていない", r.stderr)
        self.assertEqual(self.lines(), [f"start {SID_A} model=opus cwd={self.d / 'work'}"])  # 一度も止めていない
        self.assertFalse((self.d / "rt/claude-wait" / f"{SID_A}.next").exists())


if __name__ == "__main__":
    unittest.main()
