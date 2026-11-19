#!/usr/bin/env python3
"""最小のテスト。python3 tests/run.py"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from geometry import pane_fraction, split_rects, tab_pane_fraction, target_rect  # noqa: E402
from view import (add_role, current_rows, left_percent, parse_rows, plan, remove_role,  # noqa: E402
                  split_two, swap_roles, validate)
ALLROWS = [["dev-a", "dev-c", "dev-d"], ["dev-b", "dev-e"]]
import json  # noqa: E402
from core import (check_layout, choose_key, kitty_pid_from_socket, merge_place_opts,  # noqa: E402
                  ledger_touch, parse_journal, profile_processes, resolve_pane, sanitize)


def win(id, cols, lines, **nb):
    return {"id": id, "columns": cols, "lines": lines,
            "neighbors": {d: nb.get(d, []) for d in ("left", "right", "top", "bottom")}}


# 左 1 枚 + 右上下 2 枚（claude3.session と同じ形）
THREE = [
    win(1, 98, 75, right=[2, 3]),
    win(2, 295, 37, left=[1], bottom=[3]),
    win(3, 295, 37, left=[1], top=[2]),
]


class Fraction(unittest.TestCase):
    def test_left_full_height(self):
        x, y, w, h = pane_fraction(THREE, 1)
        self.assertEqual((x, y, h), (0, 0, 1))
        self.assertAlmostEqual(w, 98 / 393)

    def test_right_bottom(self):
        x, y, w, h = pane_fraction(THREE, 3)
        self.assertAlmostEqual(x, 98 / 393)
        self.assertAlmostEqual(y, 0.5)
        self.assertAlmostEqual(w, 295 / 393)
        self.assertAlmostEqual(h, 0.5)

    def test_single_pane(self):
        self.assertEqual(pane_fraction([win(7, 80, 24)], 7), (0, 0, 1, 1))

    def test_missing_pane(self):
        with self.assertRaises(KeyError):
            pane_fraction(THREE, 99)

    def test_empty(self):
        with self.assertRaises(KeyError):
            pane_fraction([], 1)

    def test_cyclic_neighbors_terminate(self):
        # 壊れた隣接情報（循環）でも無限ループしない
        ws = [win(1, 10, 10, right=[2]), win(2, 10, 10, right=[1], left=[1])]
        x, y, w, h = pane_fraction(ws, 2)
        self.assertTrue(0 <= x < 1 and 0 < w <= 1 and x + w <= 1)

    def test_neighbor_not_in_tab_ignored(self):
        ws = [win(1, 10, 10, right=[42])]
        self.assertEqual(pane_fraction(ws, 1), (0, 0, 1, 1))

    def test_no_neighbors_key(self):
        self.assertEqual(pane_fraction([{"id": 1, "columns": 5, "lines": 5}], 1), (0, 0, 1, 1))

    def test_zero_size(self):
        with self.assertRaises(ValueError):
            pane_fraction([win(1, 0, 0)], 1)


class Rect(unittest.TestCase):
    CLIENT = (0, 30, 4000, 1000)

    def test_self_covers_pane(self):
        self.assertEqual(target_rect(self.CLIENT, (0.25, 0.5, 0.75, 0.5)), (1000, 530, 3000, 500))

    def test_show_keeps_bottom_visible(self):
        x, y, w, h = target_rect(self.CLIENT, (0.25, 0.5, 0.75, 0.5), "show", 0.6)
        self.assertEqual((x, y, w, h), (1000, 530, 3000, 300))

    def test_bad_mode(self):
        with self.assertRaises(ValueError):
            target_rect(self.CLIENT, (0, 0, 1, 1), "full")

    def test_bad_ratio(self):
        for r in (0, 0.05, 1.5):
            with self.assertRaises(ValueError):
                target_rect(self.CLIENT, (0, 0, 1, 1), "show", r)


FIXTURE = json.loads((Path(__file__).parent / "fixture_ls_three.json").read_text())


class Key(unittest.TestCase):
    def test_role_beats_slot_and_session(self):
        k = choose_key(None, {"role": "dev-c", "slot": "left"}, {"CLAUDE_CODE_SESSION_ID": "ab"})
        self.assertEqual(k, "role-dev-c")

    def test_slot_when_no_role(self):
        self.assertEqual(choose_key(None, {"slot": "right-top"}, {}), "slot-right-top")

    def test_explicit_key_sanitized(self):
        self.assertEqual(choose_key("../evil key", {"role": "x"}, {}), "evilkey")

    def test_explicit_key_empty_after_sanitize(self):
        with self.assertRaises(ValueError):
            choose_key("../", {}, {})

    def test_session_id_fallback(self):
        self.assertEqual(choose_key(None, {}, {"CLAUDE_CODE_SESSION_ID": "ab-12"}), "s-ab-12")

    def test_kitty_fallback(self):
        env = {"KITTY_WINDOW_ID": "3", "KITTY_LISTEN_ON": "unix:/run/user/1000/kitty-160411"}
        self.assertEqual(choose_key(None, None, env), "k160411-w3")

    def test_nothing(self):
        with self.assertRaises(ValueError):
            choose_key(None, {}, {})

    def test_sanitize_length(self):
        self.assertEqual(len(sanitize("a" * 200)), 64)


    def test_lowercase(self):
        self.assertEqual(sanitize("MyKey"), "mykey")


class Socket(unittest.TestCase):
    def test_pid(self):
        self.assertEqual(kitty_pid_from_socket("unix:/run/user/1000/kitty-160411"), 160411)

    def test_bad_names(self):
        for s in ("unix:@mykitty", "", None, "unix:/tmp/kitty"):
            self.assertIsNone(kitty_pid_from_socket(s))


class Procs(unittest.TestCase):
    PROF = "/home/u/.var/app/com.google.Chrome/claude-profiles/role-dev-c"

    def test_finds_port_of_own_profile_only(self):
        procs = [
            (10, ["/usr/bin/bwrap", "--user-data-dir=" + self.PROF, "--remote-debugging-port=5000"]),
            (11, ["chrome", "--type=renderer", "--user-data-dir=" + self.PROF]),
            (12, ["chrome", "--user-data-dir=" + self.PROF + "-other", "--remote-debugging-port=6000"]),
            (13, ["chrome", "--remote-debugging-port=7000"]),
        ]
        self.assertEqual(profile_processes(procs, self.PROF), [(10, 5000), (11, None)])

    def test_bad_port_ignored(self):
        procs = [(1, ["--user-data-dir=" + self.PROF, "--remote-debugging-port=abc"])]
        self.assertEqual(profile_processes(procs, self.PROF), [(1, None)])

    def test_none(self):
        self.assertEqual(profile_processes([], self.PROF), [])


class Merge(unittest.TestCase):
    D = {"mode": "self", "ratio": 0.65, "over": None}

    def test_open_again_keeps_show_mode(self):
        st = {"mode": "show", "ratio": 0.5, "over": None}
        self.assertEqual(merge_place_opts({"mode": None, "ratio": None}, st, self.D),
                         {"mode": "show", "ratio": 0.5, "over": None})

    def test_given_wins(self):
        self.assertEqual(merge_place_opts({"mode": "self"}, {"mode": "show"}, self.D)["mode"], "self")

    def test_no_state(self):
        self.assertEqual(merge_place_opts({}, None, self.D), self.D)


class Journal(unittest.TestCase):
    def test_parse(self):
        text = "noise\nkwin_wayland: tok1 {\"a\":1}\nother tok2 x\ntok1 END\n"
        self.assertEqual(parse_journal(text, "tok1"), (['{"a":1}'], True))

    def test_not_ended(self):
        self.assertEqual(parse_journal("tok1 a\n", "tok1"), (["a"], False))

    def test_empty(self):
        self.assertEqual(parse_journal("", "t"), ([], False))


class Pane(unittest.TestCase):
    def test_own_pane_from_real_kitty_output(self):
        osw, tab, pane = resolve_pane(FIXTURE, None, 3)
        self.assertEqual(pane["user_vars"]["slot"], "right-bottom")
        x, y, w, h = tab_pane_fraction(tab, pane["id"])
        self.assertAlmostEqual(y, 0.5)
        self.assertAlmostEqual(h, 0.5)
        self.assertAlmostEqual(x + w, 1)

    def test_over_by_slot_and_id(self):
        self.assertEqual(resolve_pane(FIXTURE, "left", None)[2]["id"], 1)
        self.assertEqual(resolve_pane(FIXTURE, "2", None)[2]["id"], 2)

    def test_outside_kitty_needs_over(self):
        with self.assertRaises(LookupError):
            resolve_pane(FIXTURE, None, None)

    def test_missing(self):
        with self.assertRaises(LookupError):
            resolve_pane(FIXTURE, "nope", None)

    def test_inactive_tab(self):
        data = [{"tabs": [{"is_active": False, "windows": [{"id": 9, "columns": 1, "lines": 1}]}]}]
        with self.assertRaises(LookupError):
            resolve_pane(data, None, 9)

    def test_no_neighbors_with_many_panes_refused(self):
        with self.assertRaises(LookupError):
            check_layout([{"id": 1, "neighbors": {}}, {"id": 2}])

    def test_single_pane_ok(self):
        check_layout([{"id": 1}])


ALL = json.loads((Path(__file__).parent / "fixture_ls_all.json").read_text())


class SplitTree(unittest.TestCase):
    def frac(self, role):
        tab = ALL[0]["tabs"][0]
        wid = next(w["id"] for w in tab["windows"] if w["user_vars"]["role"] == role)
        return tab_pane_fraction(tab, wid)

    def test_all_preset_from_real_kitty(self):
        # 左 1/4 全高、右上 3 等分、右下 2 等分（neighbors の番号が window id とずれた状態の実データ）
        expect = {"main": (0, 0, .25, 1), "dev-a": (.25, 0, .2475, .5),
                  "dev-c": (.4975, 0, .25125, .5), "dev-d": (.74875, 0, .25125, .5),
                  "dev-b": (.25, .5, .375, .5), "dev-e": (.625, .5, .375, .5)}
        for role, e in expect.items():
            for a, b in zip(self.frac(role), e):
                self.assertAlmostEqual(a, b, places=3, msg=role)

    def test_leaf_only(self):
        self.assertEqual(split_rects(7), {7: (0.0, 0.0, 1.0, 1.0)})

    def test_none(self):
        self.assertEqual(split_rects(None), {})

    def test_vertical_bias(self):
        r = split_rects({"horizontal": False, "bias": 0.3, "one": 1, "two": 2})
        self.assertEqual(r[1], (0.0, 0.0, 1.0, 0.3))
        self.assertAlmostEqual(r[2][1], 0.3)

    def test_single_window_tab_without_tree(self):
        tab = {"layout_state": {"class": "Splits", "pairs": {}}, "groups": [], "windows": [{"id": 1}]}
        self.assertEqual(tab_pane_fraction(tab, 1), (0.0, 0.0, 1.0, 1.0))

    def test_window_not_in_tree(self):
        tab = {"layout_state": {"class": "Splits", "pairs": {"one": 1, "two": 2}},
               "groups": [{"id": 1, "windows": [1]}, {"id": 2, "windows": [2]}], "windows": [{"id": 1}, {"id": 2}]}
        with self.assertRaises(KeyError):
            tab_pane_fraction(tab, 9)


class View(unittest.TestCase):
    ALL6 = {"main", "dev-a", "dev-b", "dev-c", "dev-e", "dev-d"}

    def test_parse_rows(self):
        self.assertEqual(parse_rows("a, b/c//"), [["a", "b"], ["c"]])
        self.assertEqual(parse_rows(""), [])

    def test_validate_ok(self):
        validate("main", ALLROWS, self.ALL6)

    def test_validate_errors(self):
        for rows in ([["main"]], [["dev-c", "dev-c"]], [["nobody"]], [[]]):
            with self.assertRaises(ValueError):
                validate("main", rows, self.ALL6)

    def test_left_missing(self):
        with self.assertRaises(ValueError):
            validate("main", [["dev-c"]], {"dev-c"})

    def test_plan_parks_everything_but_left_first(self):
        ops = plan("L", [["a"]], ["L", "x", "y"])
        self.assertEqual(ops[:2], [("park", "x"), ("park", "y")])
        self.assertEqual(ops[-1], ("focus", "L"))  # 最後は統括へ戻す

    def test_plan_pair_stacks_and_equalizes(self):
        ops = plan("L", [["a"], ["b"]], ["L"])
        self.assertEqual(ops, [("focus", "L"), ("bring", "a"), ("focus", "L"), ("bias", 50),
                               ("focus", "a"), ("bring", "b"), ("focus", "b"), ("rotate",),
                               ("focus", "a"), ("bias", 50), ("focus", "L")])

    def test_plan_row_thirds(self):
        ops = plan("L", [["a", "b", "c"]], ["L"])
        self.assertIn(("bias", 33), ops)
        self.assertIn(("bias", 50), ops)
        self.assertEqual([o for o in ops if o[0] == "bring"], [("bring", "a"), ("bring", "b"), ("bring", "c")])

    def test_plan_empty_rows(self):
        self.assertEqual(plan("L", [], ["L", "a"]), [("park", "a"), ("focus", "L")])

    def test_never_closes_or_sends_text(self):
        kinds = {o[0] for o in plan("L", ALLROWS, list(self.ALL6 | {"L"}))}
        self.assertTrue(kinds <= {"park", "bring", "focus", "rotate", "bias"})


class ViewOps(unittest.TestCase):
    def test_left_column_counts_as_one(self):
        self.assertEqual(left_percent(ALLROWS), 25)          # 上段 3 人 + 統括 = 4 等分
        self.assertEqual(left_percent([["a"], ["b"]]), 50)   # 右が 1 列なら半分
        self.assertEqual(left_percent([]), 100)

    def test_current_rows_from_real_kitty(self):
        self.assertEqual(current_rows(ALL[0]["tabs"][0], "main", tab_pane_fraction), ALLROWS)

    def test_split_two(self):
        self.assertEqual(split_two(list("abcde")), [list("abc"), list("de")])
        self.assertEqual(split_two(["a"]), [["a"]])
        self.assertEqual(split_two([]), [])

    def test_add_to_shortest_row_or_given(self):
        self.assertEqual(add_role(ALLROWS, "x")[1], ["dev-b", "dev-e", "x"])
        self.assertEqual(add_role(ALLROWS, "x", 0)[0][-1], "x")
        self.assertEqual(add_role(ALLROWS, "x", 5)[-1], ["x"])
        self.assertEqual(add_role([], "x"), [["x"]])

    def test_add_existing_moves_it(self):
        rows = add_role([["a", "b"], ["c"]], "a", 1)
        self.assertEqual(rows, [["b"], ["c", "a"]])

    def test_remove_drops_empty_rows(self):
        self.assertEqual(remove_role([["a"], ["b", "c"]], "a"), [["b", "c"]])

    def test_swap_inside_and_with_standby(self):
        self.assertEqual(swap_roles([["a", "b"], ["c"]], "a", "c"), [["c", "b"], ["a"]])
        self.assertEqual(swap_roles([["a", "b"]], "a", "z"), [["z", "b"]])
        with self.assertRaises(ValueError):
            swap_roles([["a"]], "y", "z")

    def test_add_then_plan_is_equal(self):
        ops = plan("L", add_role([["a", "b"], ["c"]], "d"), ["L", "a", "b", "c"])
        self.assertIn(("bias", 33), ops)  # 左 = 1/(2+1)


class Ledger(unittest.TestCase):
    def test_new_entry(self):
        led = ledger_touch({}, "role-dev-c", "2026-11-20 10:00", url="http://x", session="s1",
                           pane_vars={"role": "dev-c", "slot": "left", "other": "z"})
        e = led["role-dev-c"]
        self.assertEqual((e["created"], e["last_used"], e["last_url"], e["last_session"]),
                         ("2026-11-20 10:00", "2026-11-20 10:00", "http://x", "s1"))
        self.assertEqual(e["pane"], {"role": "dev-c", "slot": "left"})

    def test_update_keeps_created_and_notes(self):
        led = ledger_touch({}, "k", "t1", note="github: passkey")
        led2 = ledger_touch(led, "k", "t2", url="u")
        self.assertEqual(led2["k"]["created"], "t1")
        self.assertEqual(led2["k"]["notes"], ["t1 github: passkey"])
        self.assertEqual(led["k"].get("last_url"), None)  # 元の dict は変えない

    def test_none_ledger(self):
        self.assertIn("k", ledger_touch(None, "k", "t"))


import os  # noqa: E402
import select  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402

WAIT = Path(__file__).resolve().parent.parent / "bin" / "claude-wait"
SID = "11111111-2222-3333-4444-555555555555"


def proc_start(pid):
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


class ClaudeWait(unittest.TestCase):
    """偽の sessions ディレクトリと偽の claude で、二重起動しないことを確かめる。

    固定の sleep で「そろそろ読んだはず」と決めると、負荷しだいで落ちる（2026-11-20、20 回に 1 回）。
    待ち受けの出力（CLAUDE_WAIT_TRACE の判定の変化と、待ち始めの表示）が出るまで待ってから次へ進む。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.sessions = d / "sessions"; self.sessions.mkdir()
        self.marker = d / "ran"
        fake = d / "bin"; fake.mkdir()
        (fake / "claude").write_text(f"#!/bin/sh\necho \"$@\" > {self.marker}\n")
        (fake / "claude").chmod(0o755)
        self.env = dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}", CLAUDE_SESSIONS_DIR=str(self.sessions),
                        CLAUDE_WAIT_INTERVAL="0.1", CLAUDE_WAIT_NO_SHELL="1", CLAUDE_WAIT_TRACE="1",
                        XDG_RUNTIME_DIR=str(d))
        self.procs = []
        self.out = {}

    def tearDown(self):
        for p in self.procs:
            p.kill(); p.wait()
            if p.stdout:
                p.stdout.close()
        self.tmp.cleanup()

    def start(self, sid=SID, *extra, name="t"):
        p = subprocess.Popen([sys.executable, str(WAIT), name, sid, *extra], env=self.env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        self.procs.append(p)
        self.out[p] = b""
        return p

    def live_session(self, text=None, sid=SID, start=None, kind=None):
        s = subprocess.Popen(["sleep", "60"]); self.procs.append(s)
        data = {"pid": s.pid, "sessionId": sid, "procStart": start or proc_start(s.pid)}
        if kind is not None:
            data["kind"] = kind
        body = text if text is not None else json.dumps(data)
        (self.sessions / f"{s.pid}.json").write_text(body)
        return s

    def read_some(self, p, t):
        """t 秒まで待って、出ている分を self.out に足す。EOF なら False（出力が無かっただけなら True）。"""
        r, _, _ = select.select([p.stdout], [], [], max(t, 0))
        if not r:
            return True
        chunk = os.read(p.stdout.fileno(), 65536)
        self.out[p] += chunk
        return bool(chunk)

    def output(self, p):
        return self.out[p].decode(errors="replace")

    def wait_output(self, p, text, t=10):
        """text が出るまで待つ（出ずに終わった・時間切れなら失敗）。"""
        deadline = time.monotonic() + t
        while text not in self.output(p):
            left = deadline - time.monotonic()
            if left <= 0 or not self.read_some(p, left):
                self.fail(f"{text!r} が出ない。出力: {self.output(p)!r}")

    def finished(self, p, t):
        try:
            p.wait(timeout=t)
        except subprocess.TimeoutExpired:
            return False
        # 残りの出力を EOF まで取る。孫プロセスがパイプを持ったまま黙っていても固まらないよう締め切りを置く
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self.read_some(p, deadline - time.monotonic()):
            pass
        return True

    def first_finished(self, procs, t):
        """procs のうち先に終わった 1 つ（t 秒で誰も終わらなければ None）。"""
        deadline = time.monotonic() + t
        while time.monotonic() < deadline:
            for p in procs:
                if p.poll() is not None:
                    self.finished(p, 0)
                    return p
            time.sleep(0.02)
        return None

    def test_no_live_session_resumes(self):
        p = self.start()
        self.assertTrue(self.finished(p, 10))
        self.assertEqual(self.marker.read_text().strip(), f"--resume {SID}")
        self.assertNotIn("/rename", self.output(p))

    def test_waits_while_live_then_resumes(self):
        s = self.live_session()
        p = self.start()
        self.wait_output(p, "いま別の場所で動いています")
        self.assertFalse(self.finished(p, 0.5))
        self.assertFalse(self.marker.exists())
        s.kill(); s.wait()
        self.assertTrue(self.finished(p, 10))
        self.assertTrue(self.marker.exists())

    def test_spaced_json_still_detected(self):
        s = subprocess.Popen(["sleep", "60"]); self.procs.append(s)
        (self.sessions / f"{s.pid}.json").write_text(
            '{ "pid": %d, "sessionId": "%s", "procStart": "%s" }' % (s.pid, SID, proc_start(s.pid)))
        p = self.start()
        self.wait_output(p, f"(pid {s.pid})")
        self.assertFalse(self.finished(p, 0.5))

    def test_keeps_waiting_after_json_removed_or_session_changed(self):
        s = self.live_session()
        p = self.start()
        self.wait_output(p, "いま別の場所で動いています")
        # /clear で sessionId が変わる・後片付けで JSON が消える。プロセスは生きている
        (self.sessions / f"{s.pid}.json").write_text(json.dumps({"pid": s.pid, "sessionId": "other"}))
        self.assertFalse(self.finished(p, 0.5))
        (self.sessions / f"{s.pid}.json").unlink()
        self.assertFalse(self.finished(p, 1.0))
        self.assertFalse(self.marker.exists())
        s.kill(); s.wait()
        self.assertTrue(self.finished(p, 10))
        self.assertTrue(self.marker.exists())

    def test_empty_file_moment_does_not_resume(self):
        s = self.live_session()
        f = self.sessions / f"{s.pid}.json"
        good = f.read_text()
        f.write_text("")  # 書き直しの途中
        # 空のまま約 20 回読むと壊れたファイルとみなされる。その猶予（20 × INTERVAL/4）を 2 秒に広げて、負荷に強くする
        self.env["CLAUDE_WAIT_INTERVAL"] = "0.4"
        p = self.start()
        # 空のファイルを読んで「わからない」と判断したのを見てから戻す（約 20 回読むまでは待ってくれる）
        self.wait_output(p, "trace: unsure")
        f.write_text(good)
        self.wait_output(p, "いま別の場所で動いています")
        self.assertNotIn("trace: none", self.output(p))
        self.assertFalse(self.finished(p, 1.0))
        self.assertFalse(self.marker.exists())

    def test_reused_pid_is_not_live(self):
        self.live_session(start="1")  # procStart が合わない＝別のプロセス
        p = self.start()
        self.assertTrue(self.finished(p, 10))
        self.assertTrue(self.marker.exists())

    def test_stale_file_of_dead_pid_ignored(self):
        (self.sessions / "999999.json").write_text(json.dumps({"pid": 999999, "sessionId": SID}))
        self.assertTrue(self.finished(self.start(), 10))

    def test_two_waiters_only_one_resumes(self):
        self.live_session()
        a, b = self.start(), self.start()
        loser = self.first_finished([a, b], 10)  # 片方はロックで即終了
        self.assertIsNotNone(loser)
        self.assertIn("別のペイン", self.output(loser))
        winner = b if loser is a else a
        self.wait_output(winner, "いま別の場所で動いています")
        self.assertIsNone(winner.poll())
        self.assertFalse(self.marker.exists())

    def test_rename_passes_name_and_asks_for_slash_rename(self):
        p = self.start(SID, "--rename", name="worker")
        self.assertTrue(self.finished(p, 10))
        self.assertEqual(self.marker.read_text().strip(), f"--resume {SID} -n worker")
        # resume では -n が効かないので、手で /rename してもらう
        self.assertIn("再開したら /rename worker と打ってください", self.output(p))

    def test_rename_hint_shown_after_waiting(self):
        s = self.live_session()
        p = self.start(SID, "--rename", name="worker")
        self.wait_output(p, "いま別の場所で動いています")
        self.assertNotIn("/rename", self.output(p))  # 待っている間はまだ出さない（再開の直前に出す）
        s.kill(); s.wait()
        self.assertTrue(self.finished(p, 10))
        out = self.output(p)
        self.assertLess(out.index("終了を確認しました"), out.index("/rename worker"))

    def test_bg_session_warns_and_does_not_kill(self):
        s = self.live_session(kind="bg")
        p = self.start()
        self.wait_output(p, "背景（daemon）で動いています")
        out = self.output(p)
        self.assertIn("前面の claude を閉じても終わりません", out)
        self.assertIn(f"pid {s.pid}", out)
        self.assertNotIn("そちらで /exit すると", out)
        self.assertFalse(self.finished(p, 0.5))
        self.assertIsNone(s.poll())  # 自動では止めない
        s.kill(); s.wait()  # 人が止めれば再開する
        self.assertTrue(self.finished(p, 10))
        self.assertTrue(self.marker.exists())

    def test_interactive_kind_gets_normal_message(self):
        self.live_session(kind="interactive")
        p = self.start()
        self.wait_output(p, "そちらで /exit すると")
        self.assertNotIn("daemon", self.output(p))

    def test_bad_id_refused(self):
        p = self.start(sid="abc")
        self.assertTrue(self.finished(p, 10))
        self.assertFalse(self.marker.exists())


from config import DEFAULT_VIEW, config_path, kitten_path, load_view_config  # noqa: E402


class Config(unittest.TestCase):
    """手元の役割名は設定ファイル・環境変数で与える（リポジトリには汎用の既定値だけを置く）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data):
        self.path.write_text(data if isinstance(data, str) else json.dumps(data))

    def test_no_file_gives_generic_defaults(self):
        c = load_view_config({}, self.path)
        self.assertEqual(c, {"left": "main", "restore": [["dev-a"], ["dev-b"]], "guard": []})
        c["restore"].append(["x"])  # 返した値を変えても既定値は変わらない
        self.assertEqual(DEFAULT_VIEW["restore"], [["dev-a"], ["dev-b"]])

    def test_file_values(self):
        self.write({"view": {"left": "boss", "restore": [["a"], ["b", "c"]], "guard": ["^\\./Game"]}})
        self.assertEqual(load_view_config({}, self.path),
                         {"left": "boss", "restore": [["a"], ["b", "c"]], "guard": ["^\\./Game"]})

    def test_restore_as_spec_string_and_guard_as_string(self):
        self.write({"view": {"restore": "a,b/c", "guard": "x"}})
        c = load_view_config({}, self.path)
        self.assertEqual((c["restore"], c["guard"], c["left"]), ([["a", "b"], ["c"]], ["x"], "main"))

    def test_env_beats_file(self):
        self.write({"view": {"left": "boss", "restore": [["a"]], "guard": ["g"]}})
        env = {"CLAUDE_VIEW_LEFT": "env-left", "CLAUDE_VIEW_RESTORE": "p/q", "CLAUDE_VIEW_GUARD": "none"}
        self.assertEqual(load_view_config(env, self.path), {"left": "env-left", "restore": [["p"], ["q"]], "guard": []})
        self.assertEqual(load_view_config({"CLAUDE_VIEW_GUARD": "^x"}, self.path)["guard"], ["^x"])

    def test_empty_env_does_not_drop_file_values(self):
        # 空のまま export された環境変数で、設定ファイルの guard（ゲームの保護）が外れない
        self.write({"view": {"left": "boss", "restore": [["a"]], "guard": ["g"]}})
        env = {"CLAUDE_VIEW_LEFT": "", "CLAUDE_VIEW_RESTORE": "", "CLAUDE_VIEW_GUARD": ""}
        self.assertEqual(load_view_config(env, self.path), {"left": "boss", "restore": [["a"]], "guard": ["g"]})

    def test_empty_file_view_keeps_defaults(self):
        self.write({})
        self.assertEqual(load_view_config({}, self.path)["left"], "main")

    def test_broken_files_refused(self):
        for bad in ("", "{", "[]", {"view": []}, {"view": {"left": ""}}, {"view": {"left": None}},
                    {"view": {"restore": []}}, {"view": {"restore": [[]]}}, {"view": {"restore": "/"}},
                    {"view": {"restore": [["a", 1]]}}, {"view": {"restore": None}},
                    {"view": {"guard": [""]}}, {"view": {"guard": 3}}):
            self.write(bad)
            with self.assertRaises(ValueError, msg=repr(bad)):
                load_view_config({}, self.path)

    def test_bad_env_restore_refused(self):
        with self.assertRaises(ValueError):
            load_view_config({"CLAUDE_VIEW_RESTORE": " / , "}, self.path)

    def test_unreadable_path_refused(self):
        with self.assertRaises(ValueError):
            load_view_config({}, Path(self.tmp.name))  # ディレクトリは読めない

    def test_config_path(self):
        self.assertEqual(config_path({"CLAUDE_KITTY_CONFIG": "/x/c.json"}), Path("/x/c.json"))
        self.assertEqual(config_path({"XDG_CONFIG_HOME": "/cfg"}), Path("/cfg/claude-kitty/config.json"))
        self.assertEqual(config_path({}), Path.home() / ".config/claude-kitty/config.json")

    def test_kitten_path(self):
        self.assertEqual(kitten_path({"CLAUDE_KITTY_KITTEN": "/k"}), "/k")
        local = os.path.expanduser("~/.local/kitty.app/bin/kitten")
        self.assertEqual(kitten_path({}, exists=lambda p: p == local, which=lambda n: "/usr/bin/kitten"), local)
        self.assertEqual(kitten_path({}, exists=lambda p: False, which=lambda n: "/usr/bin/kitten"), "/usr/bin/kitten")
        mac = "/Applications/kitty.app/Contents/MacOS/kitten"
        self.assertEqual(kitten_path({}, exists=lambda p: p == mac, which=lambda n: None), mac)
        self.assertEqual(kitten_path({}, exists=lambda p: False, which=lambda n: None), "kitten")


VIEW = Path(__file__).resolve().parent.parent / "bin" / "claude-view"


class ClaudeViewCli(unittest.TestCase):
    """偽の kitten（ls に fixture を返し、ほかの呼び出しを記録する）で claude-view を通しで動かす。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.calls = d / "calls"
        kitten = d / "kitten"
        fixture = Path(__file__).parent / "fixture_ls_all.json"
        kitten.write_text(f"#!/bin/sh\necho \"$@\" >> {self.calls}\n"
                          f"case \"$*\" in *' ls') cat {fixture};; esac\n")
        kitten.chmod(0o755)
        self.conf = d / "config.json"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_VIEW_")}
        self.env.update(CLAUDE_KITTY_KITTEN=str(kitten), CLAUDE_KITTY_CONFIG=str(self.conf), XDG_RUNTIME_DIR=str(d))

    def tearDown(self):
        self.tmp.cleanup()

    def run_view(self, *args, **env):
        return subprocess.run([sys.executable, str(VIEW), "--to", "unix:/nonexistent", *args],
                              env=dict(self.env, **env), capture_output=True, text=True, timeout=30)

    def test_dry_run_restore_uses_generic_defaults(self):
        r = self.run_view("--dry-run", "restore")
        self.assertEqual(r.returncode, 0, r.stderr)
        ops = r.stdout.splitlines()
        self.assertEqual(ops[0], "park dev-a")
        self.assertIn("bring dev-a", ops)
        self.assertIn("bring dev-b", ops)
        self.assertEqual(ops[-1], "focus main")
        # dry-run は ls だけ（並べ替えの操作を kitty に送らない）
        self.assertTrue(all(line.endswith(" ls") for line in self.calls.read_text().splitlines()))

    def test_config_file_sets_left_and_restore(self):
        # 左を dev-a にすると main は右側の人になる（設定の left が効いていること）
        self.conf.write_text(json.dumps({"view": {"left": "dev-a", "restore": [["main"], ["dev-c"]]}}))
        r = self.run_view("--dry-run", "restore")
        self.assertEqual(r.returncode, 0, r.stderr)
        ops = r.stdout.splitlines()
        self.assertNotIn("park dev-a", ops)
        self.assertIn("bring main", ops)
        self.assertIn("bring dev-c", ops)
        self.assertEqual(ops[-1], "focus dev-a")

    def test_env_and_cli_override(self):
        r = self.run_view("--dry-run", "restore", CLAUDE_VIEW_LEFT="dev-b", CLAUDE_VIEW_RESTORE="dev-c")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines()[-1], "focus dev-b")
        r = self.run_view("--left", "dev-e", "--dry-run", "restore", CLAUDE_VIEW_LEFT="dev-b", CLAUDE_VIEW_RESTORE="dev-c")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines()[-1], "focus dev-e")

    def test_unknown_left_refused(self):
        r = self.run_view("--dry-run", "restore", CLAUDE_VIEW_LEFT="nobody")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nobody", r.stderr)

    def test_broken_config_refused(self):
        self.conf.write_text("{")
        r = self.run_view("--dry-run", "restore")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("JSON", r.stderr)

    def test_guard_not_matching_runs_without_kwin(self):
        # 当たるプロセスが無ければ KWin（qdbus）を呼ばずに並べ替える
        self.conf.write_text(json.dumps({"view": {"guard": ["^no-such-process-claude-kitty-test$"]}}))
        r = self.run_view("restore", PATH=f"{Path(self.tmp.name)}:/usr/bin:/bin")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls.read_text()
        self.assertIn("detach-window", calls)
        self.assertNotIn("close", calls)
        self.assertNotIn("send-text", calls)

    def test_guard_matching_is_checked(self):
        # 当たるプロセスがあれば前面の窓を調べに行く（ここでは qdbus を偽物にして「前面ではない」と答えさせる）
        d = Path(self.tmp.name)
        s = subprocess.Popen(["sleep", "30"])
        try:
            (d / "qdbus").write_text("#!/bin/sh\necho qdbus >> %s\necho -1\n" % self.calls)
            (d / "qdbus").chmod(0o755)
            self.conf.write_text(json.dumps({"view": {"guard": ["^sleep 30$"]}}))
            r = self.run_view("restore", PATH=f"{d}:/usr/bin:/bin")
        finally:
            s.kill(); s.wait()
        self.assertIn("qdbus", self.calls.read_text())  # 前面の確認をした
        self.assertNotIn("detach-window", self.calls.read_text())  # 前面を確かめられなければ並べ替えない
        self.assertNotEqual(r.returncode, 0)

    def test_bad_guard_regex_refused(self):
        # pgrep が失敗したのを「当たらない」と見なすと保護が黙って外れるので、止める
        for pat in ("[", "(?"):
            self.conf.write_text(json.dumps({"view": {"guard": [pat]}}))
            r = self.run_view("restore", PATH=f"{Path(self.tmp.name)}:/usr/bin:/bin")
            self.assertNotEqual(r.returncode, 0, pat)
            self.assertTrue(not self.calls.exists() or "detach-window" not in self.calls.read_text(), pat)


if __name__ == "__main__":
    unittest.main(verbosity=1)
