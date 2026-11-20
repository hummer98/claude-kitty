#!/usr/bin/env python3
"""最小のテスト。python3 tests/run.py"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from geometry import (fit_correction, others_area_fraction, pane_fraction, right_area_fraction,  # noqa: E402
                      split_rects, tab_pane_fraction, target_rect)
from view import (add_role, current_rows, left_percent, parse_rows, plan, remove_role,  # noqa: E402
                  split_two, swap_roles, validate)
ALLROWS = [["dev-a", "dev-c", "dev-d"], ["dev-b", "dev-e"]]
import json  # noqa: E402
from core import (check_layout, choose_key, kitty_pid_from_socket, resolve_place_opts,  # noqa: E402
                  ledger_touch, parse_journal, profile_processes, resolve_pane, sanitize)


class BrowserArea(unittest.TestCase):
    # 左 1/4 の統括（1）と、右の上下（2, 3）
    TAB = {"layout_state": {"class": "Splits", "pairs": {"horizontal": True, "bias": 0.25, "one": 10,
                                                         "two": {"horizontal": False, "one": 20, "two": 30}}},
           "groups": [{"id": 10, "windows": [1]}, {"id": 20, "windows": [2]}, {"id": 30, "windows": [3]}],
           "windows": [{"id": 1, "user_vars": {"role": "main"}}, {"id": 2}, {"id": 3}]}
    CLIENT = (0, 40, 4000, 2000)

    def test_right_area_excludes_left_column(self):
        self.assertEqual(right_area_fraction(self.TAB, 1), (0.25, 0.0, 0.75, 1.0))
        self.assertEqual(target_rect(self.CLIENT, right_area_fraction(self.TAB, 1), "show", 0.6),
                         (1000, 40, 3000, 1200))  # 右側全体の上 6 割（ペインの全高ではない）

    def test_right_area_without_left_role_uses_root_split(self):
        self.assertEqual(right_area_fraction(self.TAB, None), (0.25, 0.0, 0.75, 1.0))
        self.assertEqual(right_area_fraction(self.TAB, 99), (0.25, 0.0, 0.75, 1.0))

    def test_right_area_of_unsplit_tab_is_whole(self):
        tab = {"layout_state": {"class": "Splits", "pairs": {"horizontal": False, "one": 1, "two": 2}},
               "groups": [{"id": 1, "windows": [1]}, {"id": 2, "windows": [2]}], "windows": [{"id": 1}, {"id": 2}]}
        self.assertEqual(right_area_fraction(tab, None), (0.0, 0.0, 1.0, 1.0))
        self.assertEqual(right_area_fraction({}, None), (0.0, 0.0, 1.0, 1.0))
        one = {"layout_state": {"class": "Splits", "pairs": {}}, "groups": [], "windows": [{"id": 1}]}
        self.assertEqual(right_area_fraction(one, 1), (0.0, 0.0, 1.0, 1.0))  # 統括しかいない

    def test_show_ratio_in_pane_mode(self):
        x, y, w, h = target_rect(self.CLIENT, tab_pane_fraction(self.TAB, 2), "show", 0.6)
        self.assertEqual((x, y, w, h), (1000, 40, 3000, 600))

    def test_fit_correction(self):
        want = (1000, 40, 3000, 1200)
        self.assertIsNone(fit_correction(want, want))
        self.assertIsNone(fit_correction(want, (1001, 41, 3002, 1198)))
        self.assertIsNone(fit_correction(want, None))
        # 下に 40 はみ出した（装飾の分などの一定のずれ）→ その分だけ小さく置く
        self.assertEqual(fit_correction(want, (1000, 40, 3000, 1240)), (1000, 40, 3000, 1160))
        self.assertEqual(fit_correction(want, (1000, 80, 3000, 1200)), (1000, 0, 3000, 1200))
        # 装飾の分より大きなずれ（大きさを戻された）は打ち消さない（極端に小さな窓にしない）
        self.assertIsNone(fit_correction((1000, 40, 3000, 720), (1000, 40, 3000, 1200)))
        self.assertEqual(fit_correction((0, 0, 100, 100), (0, 0, 100, 160))[3], 40)


class BrowserPlacement(unittest.TestCase):
    """placement の置き直し（KWin は偽物）。"""

    def setUp(self):
        import importlib.machinery as m
        import importlib.util as u
        path = Path(__file__).resolve().parent.parent / "bin" / "claude-browser"
        loader = m.SourceFileLoader("claude_browser", str(path))
        self.b = u.module_from_spec(u.spec_from_loader("claude_browser", loader))
        loader.exec_module(self.b)
        self.placed = []
        self.b.main_browser_window = lambda st: "B"
        self.b.left_window_id = lambda tab: 1

    def run_(self, results, opts=None):
        seq = list(results)
        self.b.place_browser = lambda cls, bid, kid, rect, focus: (self.placed.append((rect, focus)), seq.pop(0))[1]
        self.b.browser_rect = lambda bid: seq.pop(0)
        o = {"mode": "show", "ratio": 0.6, "over": None, "area": "right", "focus": "none"}
        o.update(opts or {})
        st = {"class": "c"}
        rect = self.b.placement(st, o, ("K", (0, 40, 4000, 2000)), BrowserArea.TAB, {"id": 2}, settle=0)
        return rect, st

    def test_fits_first_time(self):
        rect, st = self.run_([(1000, 40, 3000, 1200), (1000, 40, 3000, 1200)])
        self.assertEqual((rect, len(self.placed), st["area"]), ((1000, 40, 3000, 1200), 1, "right"))

    def test_size_restored_by_chrome_is_placed_again(self):
        full = (1000, 40, 3000, 2000)  # 最大化の解除などで前の大きさに戻された
        self.run_([(1000, 40, 3000, 1200), full, (1000, 40, 3000, 1200), (1000, 40, 3000, 1200)])
        self.assertEqual([r for r, _ in self.placed], [(1000, 40, 3000, 1200)] * 2)

    def test_constant_offset_is_cancelled(self):
        off = (1000, 40, 3000, 1240)  # 下に 40 はみ出す
        self.run_([off, off, off, off, (1000, 40, 3000, 1200), (1000, 40, 3000, 1200)])
        self.assertEqual([r for r, _ in self.placed][-1], (1000, 40, 3000, 1160))
        self.assertEqual([f for _, f in self.placed], ["none", "none", "none"])  # 置き直しで入力先を変えない

    def test_size_restored_twice_is_not_shrunk(self):
        full = (1000, 40, 3000, 2000)  # 2 度とも全高に戻される
        self.run_([full, full, full, full])
        self.assertEqual([r for r, _ in self.placed], [(1000, 40, 3000, 1200)] * 2)  # 打ち消しで極端に小さくしない

    def test_worse_after_cancel_goes_back_to_rect(self):
        off = (1000, 40, 3000, 1240)
        worse = (1000, 40, 3000, 1100)
        self.run_([off, off, off, off, worse, worse, (1000, 40, 3000, 1200)])
        self.assertEqual([r for r, _ in self.placed], [(1000, 40, 3000, 1200), (1000, 40, 3000, 1200),
                                                         (1000, 40, 3000, 1160), (1000, 40, 3000, 1200)])

    def _fake_kitty(self):
        self.b.kitty_socket = lambda: "s"
        self.b.kitty_ls = lambda sock: [{"tabs": [dict(BrowserArea.TAB, is_active=True)]}]
        self.b.resolve_pane = lambda ls, over, own: (ls[0], ls[0]["tabs"][0], {"id": 2})
        self.b.kitty_pid_from_socket = lambda sock: 1
        self.b.find_kitty = lambda pid, title: ("K", (0, 0, 100, 100))

    def test_area_is_not_inherited_by_open(self):
        self._fake_kitty()
        args = SimpleNamespace(mode=None, ratio=None, over=None, focus=None, area=None)
        opts = self.b.prepare(args, {"mode": "show", "ratio": 0.6, "area": "right", "focus": "none"})[0]
        # 前回が他のペインの場所（right）なら、open は自分の作業用の self（自分のペイン）に戻る
        self.assertEqual((opts["mode"], opts["area"], opts["ratio"], opts["focus"]), ("self", "pane", 0.65, "kitty"))

    def test_move_keeps_previous_area(self):
        self._fake_kitty()
        args = SimpleNamespace(mode=None, ratio=None, over=None, focus=None, area=None)
        opts = self.b.prepare(args, {"mode": "show", "ratio": 0.6, "area": "right", "focus": "none"}, move=True)[0]
        self.assertEqual((opts["mode"], opts["area"], opts["ratio"], opts["focus"]), ("show", "right", 0.6, "kitty"))

    def test_others_avoids_own_pane_not_over_target(self):
        # 自分は 23（上段の右端）、--over の先は 31（下段の左）。others は「自分」を避ける（--over の先ではない）
        self.b.own_window_id = lambda: 23
        self.b.left_window_id = lambda tab: 10
        seq = [(800, 40, 2133, 2000)] * 2
        self.b.place_browser = lambda cls, bid, kid, rect, focus: (self.placed.append((rect, focus)), seq.pop(0))[1]
        self.b.browser_rect = lambda bid: seq.pop(0)
        st = {"class": "c"}
        o = {"mode": "show", "ratio": 1.0, "over": "31", "area": "others", "focus": "none"}
        rect = self.b.placement(st, o, ("K", (0, 40, 4000, 2000)), OthersArea.TAB, {"id": 31}, settle=0)
        self.assertEqual((rect, st["area"]), ((800, 40, 2133, 2000), "others"))  # 23 の左の隙間（31 の上も覆う）
        one = {"layout_state": {"class": "Splits", "pairs": 5}, "groups": [{"id": 5, "windows": [7]}],
               "windows": [{"id": 7}]}
        frac = self.b.area_fraction({"area": "others"}, one, {"id": 7}, own_id=3)
        self.assertEqual(frac, (0.0, 0.0, 1.0, 1.0))  # 自分は別のタブ（ペイン 1 つのタブでも自分と取り違えない）
        frac = self.b.area_fraction({"area": "others"}, BrowserArea.TAB, {"id": 2}, own_id=1)
        self.assertEqual(frac, (0.25, 0.0, 0.75, 1.0))  # 自分が統括なら右側全体
        frac = self.b.area_fraction({"area": "others"}, BrowserArea.TAB, {"id": 2}, own_id=99)
        self.assertEqual(frac, (0.25, 0.0, 0.75, 1.0))  # 自分がこのタブにいない
        frac = self.b.area_fraction({"area": "others"}, BrowserArea.TAB, {"id": 2}, own_id=None)
        self.assertEqual(frac, (0.25, 0.0, 0.75, 1.0))

    def test_pane_area(self):
        rect, _ = self.run_([(1000, 40, 3000, 600), (1000, 40, 3000, 600)], {"area": "pane"})
        self.assertEqual(rect, (1000, 40, 3000, 600))


class BrowserStaleOver(unittest.TestCase):
    """前回の over の先（aom-text）が見えないタブ（standby）に移ったあとの open / claude-show / move。
    本物の resolve_pane を使い、kitty の ls だけを偽物にする。"""

    def setUp(self):
        import importlib.machinery as m
        import importlib.util as u
        path = Path(__file__).resolve().parent.parent / "bin" / "claude-browser"
        loader = m.SourceFileLoader("claude_browser_stale", str(path))
        self.b = u.module_from_spec(u.spec_from_loader("claude_browser_stale", loader))
        loader.exec_module(self.b)
        standby = {"is_active": False, "layout_state": {"class": "Splits", "pairs": 40},
                   "groups": [{"id": 40, "windows": [40]}], "windows": [{"id": 40, "user_vars": {"role": "aom-text"}}]}
        self.ls = [{"tabs": [dict(BrowserArea.TAB, is_active=True), standby]}]
        self.b.kitty_socket = lambda: "s"
        self.b.kitty_ls = lambda sock: self.ls
        self.b.kitty_pid_from_socket = lambda sock: 1
        self.b.find_kitty = lambda pid, title: ("K", (0, 0, 100, 100))
        self.b.is_mac = lambda: False
        self.b.own_window_id = lambda: 2  # 呼び出した自分は見えているタブのペイン 2

    def prep(self, st, move=False, **given):
        import contextlib
        import io
        args = SimpleNamespace(**dict({"mode": None, "ratio": None, "over": None, "focus": None, "area": None}, **given))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            opts, _kw, tab, pane = self.b.prepare(args, st, move=move)
        return opts, pane, err.getvalue()

    def test_claude_show_after_over_moved_to_hidden_tab(self):
        # claude-show は open --mode show --area others --focus none。others に over は関係ないので探しに行かない
        for st in ({"mode": "show", "ratio": 0.6, "area": "pane", "over": "aom-text"},
                   {"mode": "self", "ratio": 0.65, "area": "pane", "over": "40"}):
            opts, pane, err = self.prep(st, mode="show", area="others", focus="none")
            self.assertEqual((opts["area"], opts["over"], pane["id"], err), ("others", None, 2, ""), st)

    def test_area_others_open_ignores_previous_over(self):
        opts, pane, err = self.prep({"mode": "self", "area": "pane", "over": "aom-text"}, area="others")
        self.assertEqual((opts["area"], opts["over"], pane["id"], err), ("others", None, 2, ""))
        opts, pane, _ = self.prep({"mode": "self", "area": "pane", "over": "aom-text"}, area="right")
        self.assertEqual((opts["area"], opts["over"], pane["id"]), ("right", None, 2))

    def test_open_without_area_falls_back_to_own_pane(self):
        # 前回は self で aom-text の上（pane）。area も over も省いた open は pane で over を引き継ぐが、先が見えない
        opts, pane, err = self.prep({"mode": "self", "ratio": 0.65, "area": "pane", "over": "aom-text"})
        self.assertEqual((opts["area"], opts["over"], pane["id"]), ("pane", None, 2))
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertIn("自分のペインに戻す", err)
        # 前回が show・pane なら、open は show の既定の others（over は使わない）
        opts, pane, err = self.prep({"mode": "show", "ratio": 0.6, "area": "pane", "over": "aom-text"})
        self.assertEqual((opts["area"], opts["over"], pane["id"], err), ("others", None, 2, ""))

    def test_fallback_is_saved_as_self(self):
        st = {"class": "c", "mode": "self", "ratio": 0.65, "area": "pane", "over": "aom-text"}
        opts, pane, _ = self.prep(st)
        self.b.main_browser_window = lambda st: "B"
        self.b.place_browser = lambda cls, bid, kid, rect, focus: rect
        self.b.browser_rect = lambda bid: None
        self.b.placement(st, opts, ("K", (0, 40, 4000, 2000)), BrowserArea.TAB, pane, settle=0)
        self.assertIsNone(st["over"])  # 次の open で同じ見えない先を探しに行かない
        opts, pane, err = self.prep(st)
        self.assertEqual((opts["over"], pane["id"], err), (None, 2, ""))

    def test_gone_over_falls_back_too(self):
        opts, pane, err = self.prep({"mode": "self", "area": "pane", "over": "no-such-role"})
        self.assertEqual((opts["over"], pane["id"]), (None, 2))
        self.assertIn("no-such-role", err)

    def test_move_falls_back_to_own_pane(self):
        opts, pane, err = self.prep({"mode": "show", "ratio": 0.6, "area": "pane", "over": "aom-text"}, move=True)
        self.assertEqual((opts["mode"], opts["area"], opts["ratio"], opts["over"], pane["id"]),
                         ("show", "pane", 0.6, None, 2))
        self.assertIn("自分のペインに戻す", err)
        opts, pane, err = self.prep({"mode": "show", "area": "others", "over": "aom-text"}, move=True)
        self.assertEqual((opts["area"], opts["over"], pane["id"], err), ("others", None, 2, ""))

    def test_visible_inherited_over_is_still_used(self):
        opts, pane, err = self.prep({"mode": "self", "area": "pane", "over": "3"})
        self.assertEqual((opts["over"], pane["id"], err), ("3", 3, ""))
        opts, pane, _ = self.prep({"mode": "show", "ratio": 0.6, "area": "pane", "over": "3"}, move=True)
        self.assertEqual((opts["over"], pane["id"]), ("3", 3))

    def test_outside_kitty_fails_without_fallback_warning(self):
        self.b.own_window_id = lambda: None  # kitty の外: 戻る先の自分のペインが無い
        import contextlib
        import io
        err = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(err):
            self.b.prepare(SimpleNamespace(mode=None, ratio=None, over=None, focus=None, area=None),
                           {"mode": "self", "area": "pane", "over": "aom-text"})
        self.assertNotIn("自分のペインに戻す", err.getvalue())
        self.assertIn("表示されていないタブ", err.getvalue())

    def test_explicit_over_on_hidden_tab_still_fails(self):
        for given in ({"over": "aom-text"}, {"over": "aom-text", "mode": "show", "area": "others"},
                      {"over": "no-such-role"}):
            with self.assertRaises(SystemExit):
                self.prep({"mode": "self", "area": "pane"}, **given)
            with self.assertRaises(SystemExit):
                self.prep({"mode": "self", "area": "pane", "over": "aom-text"}, move=True, **given)


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


class PlaceOpts(unittest.TestCase):
    """claude-browser の配置オプションの既定と引き継ぎ（resolve_place_opts）。"""
    NONE = {"mode": None, "ratio": None, "over": None, "focus": None, "area": None}

    def r(self, state=None, move=False, **given):
        return resolve_place_opts(dict(self.NONE, **given), state, move=move)

    def test_no_state_is_self_pane(self):
        self.assertEqual(self.r(), {"mode": "self", "ratio": 0.65, "over": None, "focus": "kitty", "area": "pane"})
        self.assertEqual(self.r(None, move=True)["area"], "pane")
        self.assertEqual(resolve_place_opts({}, {}), self.r())

    def test_show_defaults_to_others_full_height(self):
        o = self.r(mode="show")
        self.assertEqual((o["area"], o["ratio"]), ("others", 1.0))
        self.assertEqual(self.r(mode="show", ratio=0.6)["ratio"], 0.6)

    def test_explicit_pane_show_is_the_old_behaviour(self):
        o = self.r(mode="show", area="pane")
        self.assertEqual((o["area"], o["ratio"]), ("pane", 0.65))
        self.assertEqual(self.r(mode="show", area="right")["ratio"], 0.65)

    def test_open_again_after_pane_show_keeps_show_mode(self):
        st = {"mode": "show", "ratio": 0.6, "over": None, "area": "pane"}  # 前からの、ペインの上の show
        o = self.r(st)
        self.assertEqual((o["mode"], o["area"], o["ratio"]), ("show", "others", 1.0))  # 会話を隠す self に戻らない

    def test_open_after_others_goes_back_to_own_pane(self):
        # claude-show（show・others）のあとの、--mode なしの open は自分の作業用: 他のペインを覆わない
        for area in ("others", "right"):
            o = self.r({"mode": "show", "ratio": 1.0, "area": area})
            self.assertEqual((o["mode"], o["area"], o["ratio"]), ("self", "pane", 0.65), area)
        o = self.r({"mode": "show", "ratio": 0.8, "area": "others"}, mode="show")
        self.assertEqual((o["area"], o["ratio"]), ("others", 0.8))  # show を明示すれば前回の ratio
        st = {"mode": "self", "ratio": 1.0, "area": "others"}  # self で others を明示したあと
        self.assertEqual((self.r(st)["area"], self.r(st)["ratio"]), ("pane", 0.65))
        o = self.r({"mode": "show", "ratio": 0.6, "area": "others"}, move=True)
        self.assertEqual((o["mode"], o["area"]), ("show", "others"))  # move は置き直すだけ

    def test_over_defaults_to_pane(self):
        self.assertEqual(self.r(mode="show", over="dev-a")["area"], "pane")  # その先に重ねる指定
        self.assertEqual(self.r(mode="show", over="dev-a", area="others")["area"], "others")
        self.assertEqual(self.r(mode="show", over="self")["area"], "others")
        self.assertEqual(self.r({"mode": "show", "area": "pane", "over": "dev-a"}, mode="show")["area"], "others")

    def test_previous_over_only_for_pane(self):
        # 前回の over を引き継ぐのは pane のときだけ（others・right は自分のペインと左の列で決まり、over は関係ない）
        st = {"mode": "self", "area": "pane", "over": "dev-a"}
        self.assertEqual(self.r(st)["over"], "dev-a")
        for given in ({"area": "others"}, {"area": "right"}, {"mode": "show"}, {"mode": "show", "area": "others"}):
            self.assertIsNone(self.r(st, **given)["over"], given)
        self.assertIsNone(self.r({"mode": "show", "area": "others", "over": "dev-a"}, move=True)["over"])
        self.assertEqual(self.r(dict(st, mode="show"), move=True)["over"], "dev-a")
        self.assertEqual(self.r(st, over="dev-b", area="others")["over"], "dev-b")  # 明示はそのまま

    def test_move_keeps_area_and_ratio(self):
        st = {"mode": "show", "ratio": 0.6, "area": "pane", "over": "dev-a"}
        self.assertEqual(self.r(st, move=True), {"mode": "show", "ratio": 0.6, "over": "dev-a", "focus": "kitty",
                                                 "area": "pane"})

    def test_move_with_mode_uses_that_modes_area(self):
        st = {"mode": "show", "ratio": 1.0, "area": "others"}
        self.assertEqual(self.r(st, move=True, mode="self")["area"], "pane")
        o = self.r(st, move=True, area="pane")  # area を変えたら ratio はその area の既定
        self.assertEqual((o["mode"], o["area"], o["ratio"]), ("show", "pane", 0.65))

    def test_old_state_without_area(self):
        st = {"mode": "show", "ratio": 0.5}  # area が無かったころの state（ペインの上 5 割）
        self.assertEqual((self.r(st)["area"], self.r(st)["ratio"]), ("others", 1.0))
        o = self.r({"class": "c", "profile": "p"}, move=True)  # need_running が作る最小の state
        self.assertEqual((o["mode"], o["area"], o["ratio"]), ("self", "pane", 0.65))

    def test_focus_is_not_inherited_over_is(self):
        st = {"mode": "self", "focus": "none", "over": "dev-a"}
        self.assertEqual((self.r(st)["focus"], self.r(st)["over"]), ("kitty", "dev-a"))
        self.assertEqual(self.r(st, over="self")["over"], "self")  # self は prepare で None にする
        self.assertEqual(self.r(st, mode="show", focus="none")["mode"], "show")


def _near(test, got, want):
    test.assertEqual(len(got), len(want))
    for g, w in zip(got, want):
        test.assertAlmostEqual(g, w, places=6, msg=f"{got} != {want}")


class OthersArea(unittest.TestCase):
    """show の既定 others: 自分以外のペインの場所に縦 100%。"""
    # 左 2 割の統括（1）。右は上下に分かれ、上段は 3 列（21, 22, 23）、下段は 2 列（31, 32）
    TAB = {"layout_state": {"class": "Splits", "pairs": {
        "horizontal": True, "bias": 0.2, "one": 10,
        "two": {"horizontal": False, "bias": 0.5,
                "one": {"horizontal": True, "bias": 1 / 3, "one": 21, "two": {"horizontal": True, "one": 22, "two": 23}},
                "two": {"horizontal": True, "bias": 0.5, "one": 31, "two": 32}}}},
        "groups": [{"id": g, "windows": [g]} for g in (10, 21, 22, 23, 31, 32)],
        "windows": [{"id": 10, "user_vars": {"role": "main"}}] + [{"id": g} for g in (21, 22, 23, 31, 32)]}

    def area(self, tab, own):
        right = right_area_fraction(tab, 10)
        return others_area_fraction(right, tab_pane_fraction(tab, own) if own is not None else None)

    def test_top_row_third_column_uses_left_gap(self):
        _near(self, self.area(self.TAB, 23), (0.2, 0.0, 0.8 * 2 / 3, 1.0))  # 右端に接する: 左の隙間を全高で

    def test_bottom_row_first_column_uses_right_gap(self):
        _near(self, self.area(self.TAB, 31), (0.6, 0.0, 0.4, 1.0))  # 左の列に接する: 右の隙間

    def test_middle_column_tie_goes_right(self):
        _near(self, self.area(self.TAB, 22), (0.2 + 0.8 * 2 / 3, 0.0, 0.8 / 3, 1.0))

    def test_main_column_gets_whole_right(self):
        _near(self, self.area(self.TAB, 10), (0.2, 0.0, 0.8, 1.0))
        _near(self, self.area(self.TAB, None), (0.2, 0.0, 0.8, 1.0))
        self.assertEqual(target_rect((0, 40, 4000, 2000), self.area(self.TAB, 31), "show", 1.0),
                         (2400, 40, 1600, 2000))
        self.assertEqual(target_rect((0, 40, 4000, 2000), self.area(self.TAB, 31), "show", 0.5),
                         (2400, 40, 1600, 1000))  # ratio を明示すれば、その矩形の上から

    def test_pane_across_right_side_takes_the_other_half(self):
        tab = json.loads(json.dumps(self.TAB))
        tab["layout_state"]["pairs"]["two"]["two"]["bias"] = 0.85  # 下段の 31 が右側の 85% を占める
        _near(self, self.area(tab, 31), (0.6, 0.0, 0.4, 1.0))  # 隙間 15% < 30%: 中心が左なので右半分
        _near(self, self.area(tab, 32), (0.2, 0.0, 0.8 * 0.85, 1.0))  # 細い 32 は左の広い隙間
        tab["layout_state"]["pairs"]["two"]["two"]["bias"] = 0.15  # 32 が 85%: 中心が右なので左半分
        _near(self, self.area(tab, 32), (0.2, 0.0, 0.4, 1.0))

    def test_gap_threshold(self):
        right = (0.0, 0.0, 1.0, 1.0)
        _near(self, others_area_fraction(right, (0.0, 0.0, 0.7, 0.5)), (0.7, 0.0, 0.3, 1.0))  # ちょうど 30% は使う
        _near(self, others_area_fraction(right, (0.0, 0.0, 0.71, 0.5)), (0.5, 0.0, 0.5, 1.0))
        _near(self, others_area_fraction(right, (0.25, 0.0, 0.7, 0.5)), (0.0, 0.0, 0.5, 1.0))  # 中心 0.6 → 左半分
        self.assertEqual(others_area_fraction(right, None), right)
        self.assertEqual(others_area_fraction((0.3, 0.0, 0.0, 1.0), (0.0, 0.0, 0.3, 1.0)), (0.3, 0.0, 0.0, 1.0))

    def test_single_pane_tab(self):
        one = {"layout_state": {"class": "Splits", "pairs": 5}, "groups": [{"id": 5, "windows": [7]}],
               "windows": [{"id": 7}]}
        right = right_area_fraction(one, None)
        _near(self, others_area_fraction(right, tab_pane_fraction(one, 7)), (0.5, 0.0, 0.5, 1.0))

    def test_unsplit_root_uses_whole_tab(self):
        # 根が上下の分割（左の列が無い）: 右側 = タブ全体。上のペインが全幅なので半分に分ける
        tab = {"layout_state": {"class": "Splits", "pairs": {"horizontal": False, "one": 1,
                                                             "two": {"horizontal": True, "one": 2, "two": 3}}},
               "groups": [{"id": i, "windows": [i]} for i in (1, 2, 3)], "windows": [{"id": i} for i in (1, 2, 3)]}
        _near(self, others_area_fraction(right_area_fraction(tab, None), tab_pane_fraction(tab, 1)),
              (0.5, 0.0, 0.5, 1.0))
        _near(self, others_area_fraction(right_area_fraction(tab, None), tab_pane_fraction(tab, 2)),
              (0.5, 0.0, 0.5, 1.0))  # 左下の 2 は右の隙間（50%）

    def test_not_splits_layout_uses_neighbors(self):
        tab = {"layout_state": {"class": "Tall"}, "windows": THREE}
        right = right_area_fraction(tab, 1)
        _near(self, right, (98 / 393, 0.0, 295 / 393, 1.0))
        _near(self, others_area_fraction(right, tab_pane_fraction(tab, 1)), right)  # 統括
        mid = 98 / 393 + 295 / 393 / 2
        _near(self, others_area_fraction(right, tab_pane_fraction(tab, 2)), (mid, 0.0, 1.0 - mid, 1.0))


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
    """偽の sessions ディレクトリと偽の claude で、二重起動しないことを確認する。

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
        self.write({"view": {"left": "boss", "restore": [["a"], ["b", "c"]], "guard": ["^\\./fullscreen-app"]}})
        self.assertEqual(load_view_config({}, self.path),
                         {"left": "boss", "restore": [["a"], ["b", "c"]], "guard": ["^\\./fullscreen-app"]})

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
        # 空のまま export された環境変数で、設定ファイルの guard（保護する窓の指定）が外れない
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
            # journalctl の無い環境（コンテナ）でも qdbus まで進むよう、偽物を置く
            (d / "journalctl").write_text("#!/bin/sh\nexit 0\n")
            (d / "journalctl").chmod(0o755)
            self.conf.write_text(json.dumps({"view": {"guard": ["^sleep 30$"]}}))
            r = self.run_view("restore", PATH=f"{d}:/usr/bin:/bin")
        finally:
            s.kill(); s.wait()
        self.assertIn("qdbus", self.calls.read_text())  # 前面の確認をした
        self.assertNotIn("detach-window", self.calls.read_text())  # 前面を確認できなければ並べ替えない
        self.assertNotEqual(r.returncode, 0)

    def test_guard_does_not_send_desktop_notifications(self):
        # 自動の通知は出さない（stderr だけ）
        d = Path(self.tmp.name)
        s = subprocess.Popen(["sleep", "31"])
        try:
            for name in ("qdbus", "notify-send"):
                (d / name).write_text("#!/bin/sh\necho %s >> %s\necho -1\n" % (name, self.calls))
                (d / name).chmod(0o755)
            self.conf.write_text(json.dumps({"view": {"guard": ["^sleep 31$"]}}))
            r = self.run_view("restore", PATH=f"{d}:/usr/bin:/bin")
        finally:
            s.kill(); s.wait()
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("notify-send", self.calls.read_text())
        self.assertNotIn("notify-send", VIEW.read_text())  # 前面と判定された枝も含めて呼ばない

    def test_marks_cleared_before_parking_and_set_again(self):
        # standby タブの題に「●」が付いていると @title standby で見つからず、2 つめの standby ができてしまう
        d = Path(self.tmp.name)
        ls = json.loads((Path(__file__).parent / "fixture_ls_all.json").read_text())
        ls[0]["tabs"].append({"id": 9, "title": "● standby", "is_active": False, "windows": []})
        fixture = d / "ls.json"; fixture.write_text(json.dumps(ls))
        (d / "kitten").write_text(f"#!/bin/sh\necho \"$@\" >> {self.calls}\n"
                                  f"case \"$*\" in *' ls') cat {fixture};; esac\n")
        r = self.run_view("restore")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls.read_text().splitlines()
        first_park = next(i for i, c in enumerate(calls) if "detach-window" in c)
        clear = next(i for i, c in enumerate(calls) if "set-tab-title --match id:9 standby" in c)
        self.assertLess(clear, first_park)

    def test_overlay_in_main_tab_is_not_a_role_less_pane(self):
        # claude-show の画像の overlay は重ねた先と同じ window group に入り、role の無いペインとして ls に出る
        d = Path(self.tmp.name)
        ls = json.loads((Path(__file__).parent / "fixture_ls_all.json").read_text())
        tab = ls[0]["tabs"][0]
        tab["windows"].append({"id": 99, "user_vars": {}, "columns": 1, "lines": 1, "neighbors": {}})
        groups = tab["layout_state"]["all_windows"]["window_groups"]
        g = next(g for g in groups if 2 in g["window_ids"])
        g["window_ids"].append(99)
        fixture = d / "ls.json"; fixture.write_text(json.dumps(ls))
        (d / "kitten").write_text(f"#!/bin/sh\necho \"$@\" >> {self.calls}\n"
                                  f"case \"$*\" in *' ls') cat {fixture};; esac\n")
        r = self.run_view("restore")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("id:99", self.calls.read_text())

    def test_bad_guard_regex_refused(self):
        # pgrep が失敗したのを「当たらない」と見なすと保護が黙って外れるので、止める
        for pat in ("[", "(?"):
            self.conf.write_text(json.dumps({"view": {"guard": [pat]}}))
            r = self.run_view("restore", PATH=f"{Path(self.tmp.name)}:/usr/bin:/bin")
            self.assertNotEqual(r.returncode, 0, pat)
            self.assertTrue(not self.calls.exists() or "detach-window" not in self.calls.read_text(), pat)


# ---------- claude-state / claude-show ----------
import importlib.machinery  # noqa: E402
import importlib.util  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from config import load_state_config  # noqa: E402
from guard import guarded_in_front  # noqa: E402
from state import (DEFAULT_COLORS, MARK, background_for, duration, format_list, kitty_conf_background,  # noqa: E402
                   list_rows, main_tab_ids, merge_colors, nested_claude, paint, parse_hook_input,
                   strip_mark, tab_mark_ops, transition, valid_session_id, viewing)
from state import STATES, age_ja, is_notice, merge_notice, merge_subagent_colors, repaint_ops, subagent_summary  # noqa: E402

STATE_BIN = Path(__file__).resolve().parent.parent / "bin" / "claude-state"
SHOW_BIN = Path(__file__).resolve().parent.parent / "bin" / "claude-show"
FORBIDDEN = ("close-window", "close-tab", "close-os-window", "send-text", "send-key")


def kwin_(id, role=None, focused=False):
    return {"id": id, "user_vars": {"role": role} if role else {}, "is_focused": focused}


# main タブ（統括 main と dev-a）+ standby タブ（dev-b）
LS = [{"id": 1, "is_focused": True, "tabs": [
    {"id": 1, "title": "main", "is_active": True, "windows": [kwin_(1, "main", True), kwin_(2, "dev-a")]},
    # is_focused はタブごとの「最後の入力先」なので、表示されていないタブにも True のペインがある（実機と同じ）
    {"id": 3, "title": "standby", "is_active": False, "windows": [kwin_(4, "dev-b", True)]},
]}]


class StateTransition(unittest.TestCase):
    def test_prompt_makes_working(self):
        n = transition(None, "UserPromptSubmit", {}, 10)
        self.assertEqual((n["state"], n["since"]), ("working", 10))

    def test_stop_makes_done(self):
        self.assertEqual(transition({"state": "working"}, "Stop", {}, 5)["state"], "done")

    def test_seen_turns_only_done_into_idle(self):
        d = transition({"state": "working"}, "Stop", {}, 5)
        n = transition(d, "seen", {}, 7)
        self.assertEqual((n["state"], n["since"]), ("idle", 7))
        for st in ("working", "waiting", "idle"):
            self.assertIsNone(transition({"state": st}, "seen", {}, 7), st)
        self.assertIsNone(transition(None, "seen", {}, 7))

    def test_since_kept_while_same_state(self):
        n = transition({"state": "working", "since": 3}, "SubagentStart", {"agent_id": "x"}, 9)
        self.assertEqual((n["state"], n["since"]), ("working", 3))
        self.assertIsNone(transition({"state": "working", "since": 3}, "UserPromptSubmit", {}, 9))

    def test_permission_notification_waits(self):
        n = transition({"state": "working"}, "Notification", {"notification_type": "permission_prompt"}, 1)
        self.assertEqual((n["state"], n["reason"]), ("waiting", "permission"))

    def test_notification_without_type_waits(self):
        # matcher で絞って登録するので、種類の無い通知は待ちと見なす
        self.assertEqual(transition({"state": "working"}, "Notification", {}, 1)["state"], "waiting")

    def test_idle_prompt_is_not_waiting(self):
        self.assertIsNone(transition({"state": "idle"}, "Notification", {"notification_type": "idle_prompt"}, 1))

    def test_ask_user_question(self):
        n = transition({"state": "working"}, "PreToolUse", {"tool_name": "AskUserQuestion", "tool_use_id": "t1"}, 1)
        self.assertEqual((n["state"], n["reason"], list(n["waits"])), ("waiting", "ask", ["ask:t1"]))
        self.assertIsNone(transition({"state": "working"}, "PreToolUse", {"tool_name": "Bash"}, 1))

    def test_answer_returns_to_working_only_for_same_tool_use(self):
        prev = {"state": "waiting", "reason": "ask", "tool_use_id": "t1"}
        self.assertIsNone(transition(prev, "PostToolUse", {"tool_use_id": "other"}, 2))  # 背景の subagent など
        self.assertEqual(transition(prev, "PostToolUse", {"tool_use_id": "t1"}, 2)["state"], "working")

    def test_permission_granted_returns_to_working(self):
        prev = {"state": "waiting", "reason": "permission"}
        self.assertEqual(transition(prev, "PostToolUse", {"tool_use_id": "x"}, 2)["state"], "working")
        self.assertEqual(transition(prev, "PostToolUseFailure", {}, 2)["state"], "working")
        self.assertIsNone(transition({"state": "working"}, "PostToolUse", {}, 2))

    def test_permission_does_not_override_ask_or_manual(self):
        for reason in ("ask", "manual"):
            self.assertIsNone(transition({"state": "waiting", "reason": reason}, "Notification", {}, 2))

    def test_manual_waiting_survives_stop_until_next_prompt(self):
        w = transition({"state": "working"}, "manual-waiting", {}, 1)
        self.assertEqual((w["state"], w["reason"]), ("waiting", "manual"))
        s = transition(w, "Stop", {}, 2)
        self.assertEqual((s["state"], s["since"]), ("waiting", 1))
        self.assertEqual(transition(s, "PostToolUse", {}, 2)["state"], "waiting")
        self.assertEqual(transition(s, "UserPromptSubmit", {}, 3)["state"], "working")

    def test_session_start_end_and_unknown(self):
        self.assertEqual(transition(None, "SessionStart", {}, 1)["state"], "idle")
        self.assertEqual(transition({"state": "waiting"}, "SessionEnd", {}, 1), {"state": "end"})
        self.assertIsNone(transition({"state": "idle"}, "Nope", {}, 1))
        self.assertIsNone(transition({"state": "working"}, "PreToolUse", None, 1))
        # 記録が無くてもツールが動いていれば作業中（Stop の後に本体が自分で再開したときなど）
        self.assertEqual(transition(None, "PreToolUse", None, 1)["state"], "working")

    def test_does_not_mutate_prev(self):
        prev = {"state": "working", "since": 1}
        transition(prev, "Stop", {}, 2)
        self.assertEqual(prev, {"state": "working", "since": 1})


def run_events(events, prev=None, t0=100):
    """[(event, payload)] を順に流し、各イベントの後の state のリストと最後の記録を返す（変わらなければ前のまま）。"""
    rec, out = prev, []
    for i, (ev, pl) in enumerate(events):
        n = transition(rec, ev, pl, t0 + i * 10)
        if n is not None:
            rec = n
        out.append((rec or {}).get("state"))
    return out, rec


BASH_A = {"tool_name": "Bash", "tool_input": {"command": "rm -rf build"}}
BASH_B = {"tool_name": "Bash", "tool_input": {"command": "ls"}}


class StateWaits(unittest.TestCase):
    """待ちを id ごとに数える: 全部片付くまで waiting。"""

    def test_permission_cleared_only_by_same_tool_call(self):
        sts, rec = run_events([("UserPromptSubmit", {}), ("PermissionRequest", BASH_A),
                               ("PostToolUse", dict(BASH_B, agent_id="x", tool_use_id="u9")),  # 並行するサブエージェント
                               ("PostToolUse", dict(BASH_B, tool_use_id="u8")),  # 本体の別のツール
                               ("PostToolUse", dict(BASH_A, tool_use_id="u1"))])
        self.assertEqual(sts, ["working", "waiting", "waiting", "waiting", "working"])
        self.assertEqual(rec["waits"], {})

    def test_two_waiters_need_both_answers(self):
        ask = {"tool_name": "AskUserQuestion", "tool_use_id": "q1"}
        sts, rec = run_events([("UserPromptSubmit", {}), ("SubagentStart", {"agent_id": "x"}),
                               ("PermissionRequest", dict(BASH_A, agent_id="x")), ("PreToolUse", ask),
                               ("PostToolUse", ask), ("PostToolUse", dict(BASH_A, agent_id="x"))])
        self.assertEqual(sts, ["working", "working", "waiting", "waiting", "waiting", "working"])

    def test_reason_is_the_strongest_wait(self):
        _, rec = run_events([("PermissionRequest", BASH_A), ("PreToolUse", {"tool_name": "AskUserQuestion",
                                                                             "tool_use_id": "q"}),
                             ("manual-waiting", {})])
        self.assertEqual((rec["state"], rec["reason"]), ("waiting", "manual"))

    def test_denied_permission_cleared_when_agent_moves_on(self):
        # 断ると PostToolUse は来ない。同じエージェントが次のツールを呼び始めたら片付いたと見なす
        r = transition(None, "PermissionRequest", BASH_A, 10)
        self.assertEqual(transition(r, "PreToolUse", BASH_B, 11), None)  # 並行する呼び出し（すぐ来る）では外さない
        self.assertEqual(transition(r, "PreToolUse", BASH_A, 30), None)  # 同じ呼び出し
        self.assertEqual(transition(r, "PreToolUse", BASH_B, 13)["state"], "working")
        o = transition(r, "PreToolUse", dict(BASH_B, agent_id="other"), 30)  # 別のエージェントでは外さない
        self.assertEqual((o["state"], o["waits"]), ("waiting", r["waits"]))

    def test_subagent_stop_and_stop_clear_their_own_waits(self):
        _, rec = run_events([("UserPromptSubmit", {}), ("SubagentStart", {"agent_id": "x"}),
                             ("PermissionRequest", dict(BASH_A, agent_id="x")), ("PermissionRequest", BASH_B)])
        st = transition(rec, "Stop", {}, 500)
        self.assertEqual((st["state"], [w["agent"] for w in st["waits"].values()]), ("waiting", ["x"]))
        sub = transition(st, "SubagentStop", {"agent_id": "x", "background_tasks": []}, 510)
        self.assertEqual((sub["state"], sub["waits"], sub["agents"]), ("done", {}, {}))

    def test_permission_notification_after_permission_request_is_ignored(self):
        r = transition(None, "PermissionRequest", BASH_A, 1)
        self.assertIsNone(transition(r, "Notification", {"notification_type": "permission_prompt"}, 7))

    def test_permission_notification_without_request_hook(self):
        # PermissionRequest の hook が無い設定でも動く（どのツールか分からないので、どれかが終われば外す）
        sts, _ = run_events([("UserPromptSubmit", {}), ("Notification", {"notification_type": "permission_prompt"}),
                             ("PostToolUse", dict(BASH_B, agent_id="y"))])
        self.assertEqual(sts, ["working", "waiting", "working"])

    def test_other_dialogs_wait_until_next_tool_event(self):
        sts, _ = run_events([("UserPromptSubmit", {}), ("Notification", {"notification_type": "elicitation_dialog"}),
                             ("PostToolUse", BASH_B)])
        self.assertEqual(sts, ["working", "waiting", "working"])

    def test_legacy_record_is_understood(self):
        self.assertEqual(transition({"state": "waiting", "reason": "permission"}, "PostToolUse", {}, 2)["state"], "working")
        self.assertEqual(transition({"state": "waiting", "reason": "manual"}, "Stop", {}, 2)["state"], "waiting")
        self.assertEqual(transition({"state": "idle"}, "Stop", {}, 2)["state"], "done")

    def test_broken_records_and_payloads_do_not_raise(self):
        prevs = [None, {}, {"state": "waiting", "waits": [1], "agents": "x"}, {"waits": {"a": 1}, "agents": {"b": None}},
                 {"state": 5, "main": "yes", "seen": None}]
        pls = [None, {}, {"agent_id": 5, "tool_use_id": [1]}, {"background_tasks": "x"},
               {"background_tasks": [None, 1, {"id": 3}, {"id": "a", "type": "subagent"}]},
               {"tool_name": "TaskStop", "tool_input": "x"}, {"tool_input": {"x": object()}, "tool_name": "Bash"},
               {"notification_type": 7}, {"source": None}]
        evs = ["UserPromptSubmit", "Stop", "StopFailure", "SubagentStart", "SubagentStop", "PreToolUse",
               "PermissionRequest", "PostToolUse", "PostToolUseFailure", "Notification", "SessionStart", "seen",
               "manual-waiting", "SessionEnd", "Unknown"]
        for prev in prevs:
            for pl in pls:
                for ev in evs:
                    n = transition(prev, ev, pl, 1)
                    self.assertTrue(n is None or n.get("state") in tuple(STATES) + ("end",), (prev, ev, pl, n))


class StateSubagents(unittest.TestCase):
    """サブエージェントが動いている間は、本体が止まっても working。"""

    def test_background_subagent_keeps_working_after_stop(self):
        # 実物の hook の並び（Claude Code 2.1.328 で記録した）
        bt = [{"id": "a1", "type": "subagent", "status": "running", "agent_type": "general-purpose"}]
        sts, rec = run_events([
            ("UserPromptSubmit", {}), ("PreToolUse", {"tool_name": "Agent", "tool_use_id": "t1"}),
            ("PostToolUse", {"tool_name": "Agent", "tool_use_id": "t1"}),
            ("SubagentStart", {"agent_id": "a1", "agent_type": "general-purpose"}),
            ("Stop", {"background_tasks": bt}),
            ("PreToolUse", {"agent_id": "a1", "tool_name": "Bash", "tool_use_id": "t2"}),
            ("PostToolUse", {"agent_id": "a1", "tool_name": "Bash", "tool_use_id": "t2"}),
            ("SubagentStop", {"agent_id": "a1", "background_tasks": bt}),
            ("UserPromptSubmit", {}),  # 完了の知らせで本体が再開する
            ("Stop", {"background_tasks": []})])
        self.assertEqual(sts, ["working"] * 7 + ["done", "working", "done"])
        self.assertEqual((rec["agents"]["a1"]["bg"], rec["agents"]["a1"]["end"]), (True, 170))  # 終わった背景のものは残す

    def test_foreground_subagent_is_forgotten_when_done(self):
        sts, rec = run_events([("UserPromptSubmit", {}), ("SubagentStart", {"agent_id": "f"}),
                               ("SubagentStop", {"agent_id": "f", "background_tasks": []}), ("Stop", {})])
        self.assertEqual((sts[-1], rec["agents"]), ("done", {}))

    def test_stop_reconciles_a_missed_subagent_stop(self):
        _, rec = run_events([("UserPromptSubmit", {}), ("SubagentStart", {"agent_id": "f"}),
                             ("Stop", {"background_tasks": []})])
        self.assertEqual((rec["state"], rec["agents"]), ("done", {}))
        # background_tasks が無い（古い版）なら数えたまま
        _, rec = run_events([("UserPromptSubmit", {}), ("SubagentStart", {"agent_id": "f"}), ("Stop", {})])
        self.assertEqual(rec["state"], "working")

    def test_stop_adds_running_background_agents_and_ignores_shells(self):
        bt = [{"id": "w1", "type": "workflow", "status": "running"}, {"id": "s1", "type": "shell", "status": "running"},
              {"id": "m1", "type": "monitor", "status": "running"}, {"id": "a9", "type": "subagent", "status": "completed"}]
        r = transition({"state": "working"}, "Stop", {"background_tasks": bt}, 5)
        self.assertEqual((r["state"], sorted(r["agents"])), ("working", ["w1"]))
        r = transition({"state": "working"}, "Stop", {"background_tasks": bt[1:]}, 5)
        self.assertEqual(r["state"], "done")

    def test_tool_events_after_done_mean_working_again(self):
        sts, _ = run_events([("UserPromptSubmit", {}), ("Stop", {}), ("PostToolUse", BASH_B), ("Stop", {})])
        self.assertEqual(sts, ["working", "done", "working", "done"])
        r = transition({"state": "idle"}, "PreToolUse", dict(BASH_B, agent_id="late"), 3)
        self.assertEqual((r["state"], list(r["agents"])), ("working", ["late"]))

    def test_seen_waits_for_subagents(self):
        r = transition({"state": "done"}, "SubagentStart", {"agent_id": "x"}, 1)
        self.assertIsNone(transition(r, "seen", {}, 2))

    def test_task_stop_drops_lingering_agent(self):
        _, rec = run_events([("SubagentStart", {"agent_id": "a1"}),
                             ("SubagentStop", {"agent_id": "a1", "background_tasks": [{"id": "a1"}]})])
        self.assertIn("a1", rec["agents"])
        r = transition(rec, "PostToolUse", {"tool_name": "TaskStop", "tool_input": {"task_id": "a1"}}, 999)
        self.assertNotIn("a1", r["agents"])

    def test_unknown_internal_subagent_stop_is_ignored(self):
        self.assertIsNone(transition({"state": "idle"}, "SubagentStop", {"agent_id": "zz", "background_tasks": []}, 1))
        self.assertIsNone(transition({"state": "idle"}, "SubagentStart", {}, 1))


class StateInterruptsAndSessions(unittest.TestCase):
    def test_idle_prompt_after_escape_ends_working(self):
        # Esc で止めると Stop が来ない。入力欄のまま 60 秒の通知で止まったことにする（止めた本人は見ている）
        _, rec = run_events([("UserPromptSubmit", {}), ("PermissionRequest", BASH_A),
                             ("Notification", {"notification_type": "idle_prompt"})])
        self.assertEqual((rec["state"], rec["waits"]), ("idle", {}))

    def test_idle_prompt_keeps_done_manual_and_subagents(self):
        self.assertIsNone(transition({"state": "done"}, "Notification", {"notification_type": "idle_prompt"}, 1))
        w = transition({"state": "idle"}, "manual-waiting", {}, 1)
        self.assertIsNone(transition(w, "Notification", {"notification_type": "idle_prompt"}, 2))  # 手の待ちは残る
        bt = [{"id": "b", "type": "subagent", "status": "running"}]
        _, rec = run_events([("UserPromptSubmit", {}), ("SubagentStart", {"agent_id": "b"}),
                             ("Stop", {"background_tasks": bt}), ("UserPromptSubmit", {}),
                             ("SubagentStart", {"agent_id": "f"}), ("PermissionRequest", dict(BASH_A, agent_id="f")),
                             ("Notification", {"notification_type": "idle_prompt"})])
        # 背景の b は動いたまま、前面の f（とその待ち）は入力欄に戻った時点で終わっている
        self.assertEqual((rec["state"], sorted(rec["agents"]), rec["waits"]), ("working", ["b"], {}))

    def test_compact_keeps_state(self):
        w = transition({"state": "working"}, "PermissionRequest", BASH_A, 1)
        self.assertIsNone(transition(w, "SessionStart", {"source": "compact"}, 2))
        for src in ("startup", "resume", "clear", None):
            self.assertEqual(transition(w, "SessionStart", {"source": src}, 2)["state"], "idle", src)

    def test_system_prompts_keep_subagent_and_manual_waits(self):
        # 背景のタスクの完了の通知・他のセッションからの依頼は人の入力でない: サブエージェントの待ちも手の待ちも残す
        for prompt in ("<task-notification>\n<task-id>x</task-id>", peer_prompt("これを直して")):
            _, rec = run_events([("UserPromptSubmit", {}), ("SubagentStart", {"agent_id": "a"}),
                                 ("PermissionRequest", dict(BASH_A, agent_id="a")), ("manual-waiting", {}),
                                 ("PermissionRequest", BASH_B), ("UserPromptSubmit", {"prompt": prompt})])
            self.assertEqual((rec["state"], sorted(w["kind"] for w in rec["waits"].values())),
                             ("waiting", ["manual", "permission"]), prompt)
            self.assertEqual([w["agent"] for w in rec["waits"].values() if w["kind"] == "permission"], ["a"])
        # 人が打てば手の待ちと本体の待ちは消える（サブエージェントの待ちは、そのエージェントの hook で消える）
        r = transition(rec, "UserPromptSubmit", {"prompt": "続けて"}, 999)
        self.assertEqual([w["agent"] for w in r["waits"].values()], ["a"])
        r = transition(r, "PostToolUse", dict(BASH_A, agent_id="a"), 1000)
        self.assertEqual(r["state"], "working")

    def test_stop_drops_waits_of_agents_no_longer_running(self):
        _, rec = run_events([("UserPromptSubmit", {}), ("SubagentStart", {"agent_id": "a"}),
                             ("PermissionRequest", dict(BASH_A, agent_id="a")), ("Stop", {"background_tasks": []})])
        self.assertEqual((rec["state"], rec["waits"]), ("done", {}))

    def test_ended_agents_are_capped(self):
        from state import MAX_ENDED
        rec = None
        for i in range(MAX_ENDED + 5):
            rec = transition(rec, "SubagentStop", {"agent_id": f"a{i}", "background_tasks": [{"id": f"a{i}"}]}, i)
        self.assertEqual(len(rec["agents"]), MAX_ENDED)
        self.assertNotIn("a0", rec["agents"])
        self.assertIn(f"a{MAX_ENDED + 4}", rec["agents"])

    def test_stop_failure_is_like_stop(self):
        self.assertEqual(transition({"state": "working"}, "StopFailure", {}, 1)["state"], "done")


def peer_prompt(body):
    return ('Another Claude session sent a message:\n<cross-session-message from="uds:/run/user/1000/cc-socks/1.sock" '
            f'from-name="boss" from-mode="prompting">\n{body}\n</cross-session-message>\n\nThis came from another session.')


class StateNotice(unittest.TestCase):
    """他のセッションからの「通知」で始まったターンは、色を変えない。"""
    JA = {"contains": ["返事は不要", "返事不要"], "prefix": ["【通知】"]}  # docs/config.md の例（0.4 までの既定）

    def test_is_notice(self):
        m = merge_notice(self.JA)
        self.assertTrue(is_notice(peer_prompt("【通知】ビルドが通りました"), m))
        self.assertTrue(is_notice(peer_prompt("決まりました。返事は不要です。"), m))
        self.assertTrue(is_notice(peer_prompt("念のため。返事不要"), m))
        self.assertFalse(is_notice(peer_prompt("この件を直してください"), m))  # 印の無い依頼
        self.assertFalse(is_notice(peer_prompt("前置き【通知】"), m))  # 【通知】は頭だけ
        self.assertFalse(is_notice("【通知】返事は不要", m))  # 人が打ったもの（cross-session-message でない）
        self.assertFalse(is_notice(None, m))
        self.assertTrue(is_notice(None, m, body="  【通知】x"))
        m = merge_notice({"contains": ["FYI"]})
        self.assertEqual(m["prefix"], [])  # 書かなかった方は既定（空）のまま
        self.assertTrue(is_notice(peer_prompt("FYI: done"), m))
        self.assertFalse(is_notice(peer_prompt("返事は不要"), m))

    def test_default_has_no_markers(self):
        # 印の言い回しはチームの約束ごとなので、既定では何も通知とみなさない
        self.assertEqual(merge_notice(None), {"contains": [], "prefix": []})
        for text in ("【通知】ビルドが通りました", "返事は不要です", "FYI", ""):
            self.assertFalse(is_notice(peer_prompt(text)), text)
            self.assertFalse(is_notice(peer_prompt(text), merge_notice(None)), text)
            self.assertFalse(is_notice(None, body=text), text)
        self.assertEqual(merge_notice({}), {"contains": [], "prefix": []})
        self.assertEqual(merge_notice({"contains": [], "prefix": []}), {"contains": [], "prefix": []})

    def test_config_restores_previous_markers(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text(json.dumps({"state": {"notice": self.JA}}))
            from config import load_state_config
            n = load_state_config({"CLAUDE_KITTY_CONFIG": str(p), "XDG_CONFIG_HOME": d})["notice"]
            self.assertEqual(n, self.JA)
            self.assertTrue(is_notice(peer_prompt("【通知】x"), n))
            p.write_text("{}")
            n = load_state_config({"CLAUDE_KITTY_CONFIG": str(p), "XDG_CONFIG_HOME": d})["notice"]
            self.assertFalse(is_notice(peer_prompt("【通知】x"), n))

    def test_merge_notice_refuses_bad_shapes(self):
        for bad in ([], {"contains": "x"}, {"contains": [""]}, {"other": ["x"]}, {"prefix": [1]}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                merge_notice(bad)

    def test_notice_turn_keeps_previous_state(self):
        for before in ("idle", "done"):
            prev = {"state": before}
            sts, rec = run_events([("UserPromptSubmit", {"_notice": True}),
                                   ("PreToolUse", BASH_B), ("PostToolUse", BASH_B), ("Stop", {})], prev=prev)
            self.assertEqual(sts, [before] * 4, before)
            self.assertFalse(rec["quiet"])

    def test_plain_peer_request_ends_in_done(self):
        sts, _ = run_events([("UserPromptSubmit", {"_notice": False}), ("Stop", {})], prev={"state": "idle"})
        self.assertEqual(sts, ["working", "done"])

    def test_notice_turn_still_shows_waits_and_subagents(self):
        sts, _ = run_events([("UserPromptSubmit", {"_notice": True}), ("PermissionRequest", BASH_A),
                             ("PostToolUse", BASH_A), ("SubagentStart", {"agent_id": "x"}),
                             ("SubagentStop", {"agent_id": "x", "background_tasks": []}), ("Stop", {})],
                            prev={"state": "idle"})
        self.assertEqual(sts, ["idle", "waiting", "idle", "working", "idle", "idle"])

    def test_notice_while_working_changes_nothing(self):
        r = transition({"state": "working"}, "UserPromptSubmit", {"_notice": True}, 1)
        self.assertIsNone(r)

    def test_notice_found_only_at_stop_returns_to_before(self):
        sts, rec = run_events([("UserPromptSubmit", {}), ("Stop", {"_notice_turn": True})], prev={"state": "idle"})
        self.assertEqual(sts, ["working", "idle"])
        sts, rec = run_events([("UserPromptSubmit", {}), ("Stop", {"_notice_turn": True})], prev={"state": "done"})
        self.assertEqual(sts, ["working", "done"])
        # ツールの hook で本体が動き出した場合も、その前に戻す
        sts, _ = run_events([("PreToolUse", BASH_B), ("Stop", {"_notice_turn": True})], prev={"state": "idle"})
        self.assertEqual(sts, ["working", "idle"])


class TurnOrigin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Path(self.tmp.name) / "t.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, *recs):
        self.p.write_text("".join((r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)) + "\n" for r in recs))

    def test_peer_turn_body(self):
        from kstate import turn_origin
        self.write({"type": "user", "turnOrigin": "human", "message": {"content": "a"}},
                   {"type": "user", "turnOrigin": "peer", "promptSource": "system",
                    "origin": {"kind": "peer", "name": "boss", "body": "【通知】x"}},
                   {"type": "assistant", "message": {}}, {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
                   "{broken", "")
        self.assertEqual(turn_origin(self.p), ("peer", "【通知】x"))

    def test_human_turn_and_missing(self):
        from kstate import turn_origin
        self.write({"type": "user", "turnOrigin": "peer", "origin": {"kind": "peer", "body": "【通知】"}},
                   {"type": "user", "turnOrigin": "human"})
        self.assertEqual(turn_origin(self.p), ("human", None))
        self.assertEqual(turn_origin(self.p.with_name("none.jsonl")), (None, None))
        self.assertEqual(turn_origin(None), (None, None))
        self.write("[1]", {"type": "assistant", "turnOrigin": "x"})
        self.assertEqual(turn_origin(self.p), (None, None))


class SubagentSummary(unittest.TestCase):
    def test_empty(self):
        for rec in (None, {}, {"agents": {}}, {"agents": "x"}, {"agents": {"a": None}}):
            self.assertEqual(subagent_summary(rec, 100), "", rec)

    def test_running_and_ended(self):
        rec = {"agents": {"a": {"end": None}, "b": {"end": None}, "c": {"end": 100 - 3 * 86400 - 5},
                          "d": {"end": 100 - 3600}}}
        self.assertEqual(subagent_summary(rec, 100, {"running": None, "ended": None}),
                         "サブ: 動作 2 ／ 終了済み 2（最古 3日）")
        self.assertEqual(subagent_summary(rec, 100),
                         "サブ: \033[38;5;110m動作 2\033[0m ／ \033[38;5;245m終了済み 2（最古 3日）\033[0m")
        only_ended = {"agents": {"c": {"end": 0}}}
        self.assertEqual(subagent_summary(only_ended, 7200, {"ended": None}), "サブ: 終了済み 1（最古 2時間）")
        self.assertEqual(subagent_summary({"agents": {"a": {}}}, 0, {"running": None}), "サブ: 動作 1")

    def test_age_and_colors(self):
        self.assertEqual([age_ja(x) for x in (-1, 59, 600, 3600, 86399, 86400 * 9)], ["0分", "0分", "10分", "1時間", "23時間", "9日"])
        self.assertEqual(merge_subagent_colors({"running": 70})["ended"], 245)
        for bad in ([], {"x": 1}, {"running": 300}, {"ended": True}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                merge_subagent_colors(bad)

    def test_lifecycle_from_hooks(self):
        bt = [{"id": "a1", "type": "subagent", "status": "running"}]
        _, rec = run_events([("UserPromptSubmit", {}), ("SubagentStart", {"agent_id": "a1"}),
                             ("SubagentStart", {"agent_id": "f1"}), ("Stop", {"background_tasks": bt})])
        self.assertEqual(subagent_summary(rec, 1000, {"running": None}), "サブ: 動作 1")  # 前面の f1 は Stop で終わり
        rec = transition(rec, "SubagentStop", {"agent_id": "a1", "background_tasks": bt}, 1000)
        self.assertEqual(subagent_summary(rec, 1000 + 86400 * 2, {"ended": None}), "サブ: 終了済み 1（最古 2日）")
        rec = transition(rec, "PostToolUse", {"tool_name": "TaskStop", "tool_input": {"task_id": "a1"}}, 1001)
        self.assertEqual(subagent_summary(rec, 2000), "")


class StateHookInput(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_hook_input('{"session_id": "a"}'), {"session_id": "a"})
        for bad in ("", None, "   ", "{", "[1, 2]", "null", "\x00\xff", '"str"'):
            self.assertEqual(parse_hook_input(bad), {}, bad)

    def test_session_id(self):
        self.assertTrue(valid_session_id("0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"))
        for bad in ("../x", "", None, "a/b", "a" * 200, 12, ".."):
            self.assertFalse(valid_session_id(bad), bad)


class StateColors(unittest.TestCase):
    def test_defaults_and_base(self):
        c = merge_colors(None)
        self.assertEqual(background_for("working", c, "#232627"), DEFAULT_COLORS["working"]["background"])
        self.assertEqual(background_for("idle", c, "#232627"), "#232627")  # idle は元の色
        self.assertEqual(background_for("bogus", c, "#232627"), "#232627")

    def test_override(self):
        c = merge_colors({"waiting": {"background": "#300000"}, "idle": {"name": None}})
        self.assertEqual(c["waiting"], {"background": "#300000", "name": 208})
        self.assertEqual(paint("x", "idle", c), "x")

    def test_bad_values_refused(self):
        for bad in ([], {"busy": {}}, {"waiting": {"background": "red"}}, {"waiting": {"name": 300}},
                    {"waiting": {"name": True}}, {"waiting": {"font": 1}}, {"waiting": None}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                merge_colors(bad)

    def test_done_colors_default_and_config(self):
        c = merge_colors(None)
        self.assertEqual(background_for("done", c, "#232627"), "#222b25")
        self.assertEqual(paint("dev", "done", c), "\033[38;5;108mdev\033[0m")
        c = merge_colors({"done": {"background": "#203020", "name": 70}})
        self.assertEqual((background_for("done", c, "#000000"), paint("x", "done", c)),
                         ("#203020", "\033[38;5;70mx\033[0m"))
        self.assertEqual(background_for("done", merge_colors({"done": {"background": None}}), "#111111"), "#111111")

    def test_paint(self):
        c = merge_colors(None)
        self.assertEqual(paint("dev", "waiting", c), "\033[38;5;208mdev\033[0m")
        self.assertEqual(paint("dev", "working", c), "dev")
        self.assertEqual(paint("dev", None, c), "dev")

    def test_kitty_conf_background(self):
        self.assertEqual(kitty_conf_background("foreground #fff\nbackground #232627\n"), "#232627")
        self.assertEqual(kitty_conf_background("background #111111\nbackground #222222"), "#222222")
        self.assertIsNone(kitty_conf_background("# background #111111\nbackground_opacity 0.9"))
        self.assertIsNone(kitty_conf_background(None))


class StateConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        (self.d / "kitty").mkdir()
        (self.d / "kitty" / "kitty.conf").write_text("background #232627\n")
        self.conf = self.d / "c.json"
        self.env = {"CLAUDE_KITTY_CONFIG": str(self.conf), "XDG_CONFIG_HOME": str(self.d)}

    def tearDown(self):
        self.tmp.cleanup()

    def test_base_from_kitty_conf(self):
        c = load_state_config(self.env)
        self.assertEqual((c["base"], c["left"]), ("#232627", "main"))

    def test_file_values(self):
        self.conf.write_text(json.dumps({"view": {"left": "boss"}, "state": {
            "base_background": "#101010", "colors": {"working": {"background": "#202020"}}}}))
        c = load_state_config(self.env)
        self.assertEqual((c["base"], c["left"], c["colors"]["working"]["background"]), ("#101010", "boss", "#202020"))

    def test_no_kitty_conf_gives_kitty_default(self):
        (self.d / "kitty" / "kitty.conf").unlink()
        self.assertEqual(load_state_config(self.env)["base"], "#000000")

    def test_broken_refused(self):
        for text in ("{", json.dumps({"state": []}), json.dumps({"state": {"base_background": "blue"}}),
                     json.dumps({"state": {"colors": {"waiting": {"background": 1}}}}),
                     json.dumps({"state": {"done_dwell_seconds": "2"}}), json.dumps({"state": {"done_dwell_seconds": -1}}),
                     json.dumps({"state": {"done_dwell_seconds": True}}), json.dumps({"state": {"notice": {"prefix": "x"}}})):
            self.conf.write_text(text)
            with self.assertRaises(ValueError, msg=text):
                load_state_config(self.env)


class StateList(unittest.TestCase):
    def test_rows_sorted_waiting_first(self):
        es = [{"role": "a", "state": "idle", "since": 0, "window": 1},
              {"role": "b", "state": "waiting", "since": 400, "reason": "ask", "window": 2},
              {"state": "working", "since": 940, "window": 3}]
        rows = list_rows(es, 1000)
        self.assertEqual([r["role"] for r in rows], ["b", "w3", "a"])
        self.assertEqual(rows[0]["minutes"], 10)
        text = format_list(rows)
        self.assertIn("b   waiting     10m  (ask)", text)
        self.assertEqual(len(text.splitlines()), 3)

    def test_order_waiting_done_working_idle(self):
        es = [{"role": r, "state": st, "since": 0} for r, st in
              (("i", "idle"), ("w", "working"), ("d", "done"), ("q", "waiting"), ("x", None))]
        self.assertEqual([r["role"] for r in list_rows(es, 100)], ["q", "d", "w", "i", "x"])

    def test_missing_fields_and_empty(self):
        rows = list_rows([{}], 5)
        self.assertEqual((rows[0]["role"], rows[0]["minutes"]), ("?", 0))
        self.assertIn("無い", format_list([]))

    def test_duration(self):
        self.assertEqual([duration(x) for x in (-5, 59, 61, 3600 + 120, 90000)], ["0s", "59s", "1m", "1h02m", "1d1h"])


class StateMarks(unittest.TestCase):
    def test_mark_standby_when_waiting(self):
        self.assertEqual(tab_mark_ops(LS, {4}, "main"), [(3, MARK + "standby")])

    def test_waiting_in_main_tab_not_marked(self):
        self.assertEqual(tab_mark_ops(LS, {2}, "main"), [])

    def test_unmark_when_not_waiting_and_clear(self):
        ls = json.loads(json.dumps(LS))
        ls[0]["tabs"][1]["title"] = MARK + "standby"
        self.assertEqual(tab_mark_ops(ls, set(), "main"), [(3, "standby")])
        self.assertEqual(tab_mark_ops(ls, {4}, "main"), [])  # もう付いている
        self.assertEqual(tab_mark_ops(ls, {4}, "main", clear=True), [(3, "standby")])

    def test_no_left_uses_active_tab_as_main(self):
        self.assertEqual(main_tab_ids(LS, "nobody"), {1})
        self.assertEqual(tab_mark_ops(LS, {4}, "nobody"), [(3, MARK + "standby")])

    def test_empty_and_strip(self):
        self.assertEqual(tab_mark_ops([], {1}, "main"), [])
        self.assertEqual(tab_mark_ops([{"tabs": []}], None, "main"), [])
        self.assertEqual(strip_mark(MARK + MARK + "x"), "x")
        self.assertEqual(strip_mark(None), "")


class Overlay(unittest.TestCase):
    def test_overlay_ids(self):
        from view import overlay_ids
        tab = {"layout_state": {"all_windows": {"window_groups": [{"id": 1, "window_ids": [12, 13, 14]},
                                                                   {"id": 2, "window_ids": [5]}]}}}
        self.assertEqual(overlay_ids(tab), {13, 14})
        self.assertEqual(overlay_ids({}), set())
        self.assertEqual(overlay_ids({"layout_state": None}), set())


class StateNested(unittest.TestCase):
    def test_nested(self):
        sh = (["/bin/sh", "-c", "claude-state hook Stop"], None)
        self.assertFalse(nested_claude([sh, (["claude", "--resume"], {"PATH": "/bin"})]))
        self.assertTrue(nested_claude([sh, (["claude", "-p", "x"], {"CLAUDECODE": "1"})]))
        # いちばん近い claude で決める（その外側のペインの claude は見ない）
        self.assertFalse(nested_claude([(["claude.exe"], {}), (["claude"], {"CLAUDECODE": "1"})]))
        self.assertTrue(nested_claude([(["node", "/x/claude-code/cli.js"], {"CLAUDE_CODE_CHILD_SESSION": "1"})]))
        self.assertFalse(nested_claude([]))
        self.assertFalse(nested_claude([(["claude"], None)]))  # environ が読めない
        # 値が空なら入れ子ではない（CLAUDECODE= で起動されたペイン）
        self.assertFalse(nested_claude([sh, (["claude"], {"CLAUDECODE": ""})]))
        self.assertFalse(nested_claude([(["claude"], {"CLAUDECODE": "", "CLAUDE_CODE_CHILD_SESSION": ""})]))
        self.assertTrue(nested_claude([(["claude"], {"CLAUDECODE": "", "CLAUDE_CODE_CHILD_SESSION": "1"})]))


class Guard(unittest.TestCase):
    def run_(self, out, code=0):
        return lambda *a, **k: SimpleNamespace(returncode=code, stdout=out, stderr="bad")

    def test_empty_patterns_skip_front_check(self):
        self.assertFalse(guarded_in_front([], active_pid=lambda: self.fail("呼ばない")))

    def test_front(self):
        self.assertTrue(guarded_in_front(["x"], run=self.run_("12\n13\n"), active_pid=lambda: 13))
        self.assertFalse(guarded_in_front(["x"], run=self.run_("12\n"), active_pid=lambda: 99))
        self.assertFalse(guarded_in_front(["x"], run=self.run_("", 1), active_pid=lambda: self.fail("呼ばない")))

    def test_pgrep_error_refused(self):
        with self.assertRaises(ValueError):
            guarded_in_front(["["], run=self.run_("", 2))


class StateRepaint(unittest.TestCase):
    def test_repaint_ops(self):
        c = merge_colors(None)
        es = [{"window": 1, "socket": "s", "state": "working", "updated": 5},
              {"window": 1, "socket": "s", "state": "waiting", "updated": 9},
              {"window": 2, "socket": "s", "state": "idle"}, {"window": 3, "socket": "t", "state": "done"},
              {"window": "4", "socket": "s", "state": "done"}, {"socket": "s"}]
        self.assertEqual(repaint_ops(es, "s", c, "#232627"), [(1, "#2e2a22"), (2, "#232627")])
        self.assertEqual(repaint_ops(es, "s", c, "#232627", alive=lambda e: False), [])
        self.assertEqual(repaint_ops([], "s", c, "#000000"), [])


class StateViewing(unittest.TestCase):
    def test_focused_pane_in_front(self):
        self.assertTrue(viewing(LS, 1))
        self.assertFalse(viewing(LS, 2))  # 同じタブの別のペイン
        self.assertFalse(viewing(LS, 4))  # 表示されていないタブの「最後の入力先」
        self.assertFalse(viewing(LS, 99))
        self.assertFalse(viewing(None, 1))
        self.assertFalse(viewing([], 1))

    def test_kitty_not_in_front_is_not_viewing(self):
        ls = json.loads(json.dumps(LS))
        ls[0]["is_focused"] = False  # 別のアプリ・別のデスクトップを見ている
        self.assertFalse(viewing(ls, 1))


WATCHER = Path(__file__).resolve().parent.parent / "kitty" / "claude_state_watcher.py"


def load_watcher():
    loader = importlib.machinery.SourceFileLoader("claude_state_watcher", str(WATCHER))
    spec = importlib.util.spec_from_loader("claude_state_watcher", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class FakeBoss:
    def __init__(self, sock="unix:/fake/kitty-1", active=None):
        self.listening_on = sock
        self.active_window = active
        self.calls = []

    def call_remote_control(self, window, args):
        self.calls.append(args)


def win_(id, osw=1):
    return SimpleNamespace(id=id, os_window_id=osw, destroyed=False)


class FakeKitty:
    """kitty.fast_data_types の偽物: タイマーは手で進め、前面の OS 窓は focused で決める。"""

    def __init__(self):
        import types
        self.timers, self.focused, self.opts = [], 1, object()
        self.mod = types.ModuleType("kitty.fast_data_types")
        self.mod.add_timer = lambda fn, delay, repeat: self.timers.append((delay, fn))
        self.mod.current_focused_os_window_id = lambda: self.focused
        self.mod.get_options = lambda: self.opts

    def __enter__(self):
        import types
        self.saved = {k: sys.modules.get(k) for k in ("kitty", "kitty.fast_data_types")}
        sys.modules.update({"kitty": types.ModuleType("kitty"), "kitty.fast_data_types": self.mod})
        return self

    def __exit__(self, *a):
        for k, v in self.saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def run_timers(self):
        ts, self.timers = self.timers, []
        for _delay, fn in ts:
            fn(1)
        return [d for d, _ in ts]


class StateWatcher(unittest.TestCase):
    SOCK = "unix:/fake/kitty-1"

    def setUp(self):
        self.w = load_watcher()
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = Path(self.tmp.name)
        self.d = self.rt / "claude-state"
        self.d.mkdir()
        self.old_env = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = str(self.rt)
        self.k = FakeKitty().__enter__()

    def tearDown(self):
        self.k.__exit__()
        if self.old_env is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = self.old_env
        self.tmp.cleanup()

    def put(self, sid, **e):
        (self.d / f"{sid}.json").write_text(json.dumps(dict({"session_id": sid, "socket": self.SOCK}, **e)))

    def get(self, sid):
        return json.loads((self.d / f"{sid}.json").read_text())

    def focus(self, boss, w):
        boss.active_window = w
        self.w.on_focus_change(boss, w, {"focused": True})

    def test_done_becomes_idle_after_dwell(self):
        self.put("a", window=4, state="done", since=1, idle_background="#232627")
        self.put("b", window=5, state="done")
        self.put("c", window=4, state="working", socket="unix:/other")
        boss, w4 = FakeBoss(), win_(4)
        self.focus(boss, w4)
        self.assertEqual(self.get("a")["state"], "done")  # すぐには消さない
        self.assertEqual(self.k.run_timers(), [2.0])  # 既定 2 秒
        self.assertEqual(self.get("a")["state"], "idle")
        self.assertGreater(self.get("a")["since"], 1)
        self.assertEqual((self.get("b")["state"], self.get("c")["state"]), ("done", "working"))
        self.assertEqual(boss.calls, [("set-colors", "--match=id:4", "background=#232627")])

    def test_focus_leaving_before_dwell_keeps_done(self):
        self.put("a", window=4, state="done", idle_background="#232627")
        boss = FakeBoss()
        self.focus(boss, win_(4))
        self.focus(boss, win_(5))  # 2 秒より前に別のペインへ
        self.k.run_timers()
        self.assertEqual(self.get("a")["state"], "done")
        self.assertEqual(boss.calls, [])

    def test_leaving_and_coming_back_restarts_the_count(self):
        self.put("a", window=4, state="done")
        boss, w4 = FakeBoss(), win_(4)
        self.focus(boss, w4)
        w5 = win_(5)
        boss.active_window = w5
        self.w.on_focus_change(boss, w4, {"focused": False})
        self.focus(boss, w5)
        self.focus(boss, w4)  # 2 秒以内に戻った
        first, second = self.k.timers[0][1], self.k.timers[-1][1]
        self.k.timers = []
        first(1)  # 最初のフォーカスからの 2 秒: 途中で離れたので数えない
        self.assertEqual(self.get("a")["state"], "done")
        second(1)  # 戻ってからの 2 秒
        self.assertEqual(self.get("a")["state"], "idle")

    def test_locked_record_is_retried_once(self):
        import fcntl
        self.put("a", window=4, state="done")
        boss, w4 = FakeBoss(), win_(4)
        self.focus(boss, w4)
        with open(self.d / ".a.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            self.k.run_timers()  # hook がロックを持っている
        self.assertEqual(self.get("a")["state"], "done")
        self.assertEqual(self.k.run_timers(), [self.w.RETRY_DELAY])
        self.assertEqual(self.get("a")["state"], "idle")
        self.assertEqual(self.k.timers, [])

    def test_kitty_not_in_front_does_not_count(self):
        self.put("a", window=4, state="done")
        boss, w4 = FakeBoss(), win_(4)
        self.k.focused = 0  # どの OS 窓も前面でない（別のアプリを見ている）
        self.focus(boss, w4)
        self.assertEqual(self.k.timers, [])
        self.k.focused = 1
        self.focus(boss, w4)
        self.k.focused = 0  # 数えている間に kitty から離れた
        self.k.run_timers()
        self.assertEqual(self.get("a")["state"], "done")

    def test_dwell_from_record(self):
        self.put("a", window=4, state="done", done_dwell=5)
        boss, w4 = FakeBoss(), win_(4)
        self.focus(boss, w4)
        self.assertEqual(self.k.run_timers(), [5.0])
        self.put("b", window=6, state="done", done_dwell=0)  # 0 ならすぐ
        self.focus(boss, win_(6))
        self.assertEqual((self.k.timers, self.get("b")["state"]), ([], "idle"))
        for bad in ("x", -1, True, None):
            self.put("c", window=7, state="done", done_dwell=bad)
            self.assertEqual(self.w.pending_dwell(self.d, 7, self.SOCK), 2.0, bad)

    def test_closed_window_is_not_marked(self):
        self.put("a", window=4, state="done")
        boss, w4 = FakeBoss(), win_(4)
        self.focus(boss, w4)
        w4.destroyed = True
        self.k.run_timers()
        self.assertEqual(self.get("a")["state"], "done")

    def test_other_states_and_unfocus_untouched(self):
        for st in ("working", "waiting", "idle"):
            self.put(st, window=4, state=st)
        boss, w4 = FakeBoss(), win_(4)
        boss.active_window = w4
        self.w.on_focus_change(boss, w4, {"focused": False})
        self.assertEqual(self.k.timers, [])
        self.focus(boss, w4)
        self.assertEqual(self.k.timers, [])  # done が無ければタイマーも要らない
        self.put("d", window=4, state="done")
        self.focus(boss, w4)
        self.k.run_timers()
        self.assertEqual([self.get(st)["state"] for st in ("working", "waiting", "idle", "d")],
                         ["working", "waiting", "idle", "idle"])
        self.assertEqual(boss.calls, [])  # 戻す色の記録が無ければ set-colors しない

    def test_never_raises(self):
        (self.d / "junk.json").write_text("{")
        (self.d / "list.json").write_text("[1]")
        self.put("e", window="4", state="done")
        cases = [(FakeBoss(), None, {"focused": True}), (FakeBoss(), SimpleNamespace(), {"focused": True}),
                 (FakeBoss(None), SimpleNamespace(id=4), {"focused": True}), (object(), SimpleNamespace(id=4), {}),
                 (FakeBoss(), SimpleNamespace(id=4), None), (None, None, None)]
        for boss, win, data in cases:
            self.w.on_focus_change(boss, win, data)
            self.w.on_tab_bar_dirty(boss, win, data)
        self.w.on_tab_bar_dirty(FakeBoss(), None, {"tab_manager": SimpleNamespace()})

        class Boom(FakeBoss):
            def call_remote_control(self, window, args):
                raise RuntimeError("x")
        self.put("f", window=4, state="done", idle_background="#000000")
        boss = Boom()
        self.focus(boss, win_(4))
        self.k.run_timers()  # タイマーの中の例外も kitty に投げない
        self.assertEqual(self.get("f")["state"], "idle")

    def test_missing_state_dir(self):
        self.tmp.cleanup()
        self.assertEqual(self.w.mark_seen(self.d, 4, self.SOCK), [])
        self.assertIsNone(self.w.pending_dwell(self.d, 4, self.SOCK))
        self.w.on_focus_change(FakeBoss(), win_(4), {"focused": True})
        self.d.mkdir(parents=True)  # tearDown の cleanup のため

    def test_locked_record_is_skipped(self):
        import fcntl
        self.put("g", window=4, state="done")
        with open(self.d / ".g.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            self.assertEqual(self.w.mark_seen(self.d, 4, self.SOCK), [])
        self.assertEqual(self.get("g")["state"], "done")
        self.assertEqual(self.w.mark_seen(self.d, 4, self.SOCK), [("g", None)])

    def test_seen_record_keeps_idle_after_next_event(self):
        self.put("s", window=4, state="done", idle_background="#232627", background="#222b25", waits={}, agents={},
                 main=False, seen=False)
        self.w.mark_seen(self.d, 4, self.SOCK)
        e = self.get("s")
        self.assertEqual((e["seen"], e["background"]), (True, "#232627"))
        self.assertIsNone(transition(e, "Notification", {"notification_type": "idle_prompt"}, 5))

    def test_tab_bar_dirty_follows_active_window_once(self):
        self.put("h", window=7, state="done", idle_background="#232627")
        w7 = win_(7)
        boss = FakeBoss(active=w7)
        tm = SimpleNamespace(os_window_id=1, active_window=w7)
        self.w.on_tab_bar_dirty(boss, None, {"tab_manager": tm})
        self.k.timers = [t for t in self.k.timers if t[0] != self.w.REPAINT_DELAY]  # 初回の塗り直しは別に見る
        self.k.run_timers()
        self.assertEqual(self.get("h")["state"], "idle")
        self.put("h", window=7, state="done", idle_background="#232627")
        self.w.on_tab_bar_dirty(boss, None, {"tab_manager": tm})  # 同じペインのまま（題が変わっただけ）
        self.assertEqual(self.k.timers, [])
        self.assertEqual(len(boss.calls), 1)

    def test_repaint_after_config_reload(self):
        self.put("a", window=4, state="working", background="#22282e", updated=1)
        self.put("b", window=4, state="done", background="#222b25", updated=2)  # 同じペインの新しい方
        self.put("c", window=6, state="waiting", background="#2e2a22", pid=999999999)  # claude が落ちた
        self.put("d", window=8, state="working", background="#22282e", socket="unix:/other")
        self.put("e", window=9, state="working", background="#22282e")  # kitty にもう無いペイン
        boss = FakeBoss()
        boss.window_id_map = {4: win_(4), 6: win_(6), 8: win_(8)}
        tm = SimpleNamespace(os_window_id=1, active_window=win_(1))
        self.w.on_tab_bar_dirty(boss, None, {"tab_manager": tm})
        self.assertEqual(self.k.run_timers(), [self.w.REPAINT_DELAY])  # 読み直しで色が戻されるより後に塗る
        self.assertEqual(boss.calls, [("set-colors", "--match=id:4", "background=#222b25")])
        self.w.on_tab_bar_dirty(boss, None, {"tab_manager": tm})
        self.assertEqual(self.k.timers, [])  # 設定が同じなら塗らない
        self.k.opts = object()
        self.w.on_tab_bar_dirty(boss, None, {"tab_manager": tm})
        self.assertEqual(len(self.k.timers), 1)


def fake_kitten(d, ls=LS, fail=False):
    """引数を 1 行 1 JSON で記録し、ls に ls を返す偽の kitten。"""
    calls = d / "kitten_calls"
    ls_file = d / "ls.json"
    ls_file.write_text(json.dumps(ls))
    k = d / "kitten"
    # set-tab-title は ls の題に反映する（次の ls で付いた印が見えるように）
    k.write_text(f"""#!{sys.executable}
import json, sys
a = sys.argv[1:]
open({str(calls)!r}, 'a').write(json.dumps(a) + '\\n')
{"sys.exit(1)" if fail else ""}
ls = json.load(open({str(ls_file)!r}))
if 'ls' in a:
    sys.stdout.write(json.dumps(ls))
if 'set-tab-title' in a:
    tid = int(a[a.index('--match') + 1].split(':')[1])
    for o in ls:
        for t in o['tabs']:
            if t['id'] == tid:
                t['title'] = a[-1]
    json.dump(ls, open({str(ls_file)!r}, 'w'))
""")
    k.chmod(0o755)
    return k, calls


def read_calls(calls):
    return [json.loads(x) for x in calls.read_text().splitlines()] if calls.exists() else []


class ClaudeStateCli(unittest.TestCase):
    SID = "sess-1"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.kitten, self.calls = fake_kitten(self.d)
        (self.d / "kitty").mkdir()
        (self.d / "kitty" / "kitty.conf").write_text("background #232627\n")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_", "KITTY_"))}
        self.env.update(CLAUDE_KITTY_KITTEN=str(self.kitten), CLAUDE_KITTY_CONFIG=str(self.d / "none.json"),
                        XDG_CONFIG_HOME=str(self.d), XDG_RUNTIME_DIR=str(self.d), CLAUDE_STATE_SKIP_NESTED_CHECK="1",
                        KITTY_WINDOW_ID="4", KITTY_LISTEN_ON="unix:/fake/kitty-1")
        self.sdir = self.d / "claude-state"

    def tearDown(self):
        for c in read_calls(self.calls):
            for bad in FORBIDDEN:
                self.assertNotIn(bad, c)
        self.tmp.cleanup()

    def hook(self, event, payload=None, raw=None, **env):
        text = raw if raw is not None else json.dumps(dict({"session_id": self.SID}, **(payload or {})))
        r = subprocess.run([sys.executable, str(STATE_BIN), "hook", event], input=text, capture_output=True,
                           text=True, env=dict(self.env, **env), timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")  # UserPromptSubmit では stdout が文脈に入るので何も出さない
        return r

    def cli(self, *args, **env):
        return subprocess.run([sys.executable, str(STATE_BIN), *args], capture_output=True, text=True,
                              env=dict(self.env, **env), timeout=20)

    def state(self, sid=None):
        p = self.sdir / f"{sid or self.SID}.json"
        return json.loads(p.read_text()) if p.exists() else None

    def colors(self):
        return [c for c in read_calls(self.calls) if "set-colors" in c]

    def titles(self):
        return [c[-1] for c in read_calls(self.calls) if "set-tab-title" in c]

    def test_full_cycle_colors_and_standby_mark(self):
        self.hook("UserPromptSubmit", {"cwd": "/w"})
        st = self.state()
        self.assertEqual((st["state"], st["window"], st["role"], st["cwd"]), ("working", 4, "dev-b", "/w"))
        self.assertEqual(self.colors()[-1], ["@", "--to", "unix:/fake/kitty-1", "set-colors", "--match", "id:4",
                                             "background=#22282e"])
        self.hook("Notification", {"notification_type": "permission_prompt"})
        self.assertEqual(self.state()["state"], "waiting")
        self.assertEqual(self.colors()[-1][-1], "background=#2e2a22")
        self.assertEqual(self.titles(), [MARK + "standby"])  # dev-b は standby タブにいる
        self.hook("PostToolUse", {"tool_use_id": "x"})
        self.assertEqual(self.state()["state"], "working")
        self.hook("Stop")
        self.assertEqual(self.state()["state"], "done")  # standby タブにいる（見ていない）
        self.assertEqual(self.colors()[-1][-1], "background=#222b25")
        self.assertEqual(self.state()["idle_background"], "#232627")  # watcher が戻す色（kitty.conf の色）
        self.assertEqual(len(self.colors()), 4)
        self.hook("SessionEnd")
        self.assertIsNone(self.state())

    def test_stop_on_viewed_pane_is_idle(self):
        self.hook("UserPromptSubmit", KITTY_WINDOW_ID="1")  # main タブの入力先、OS 窓も前面
        self.hook("Stop", KITTY_WINDOW_ID="1")
        self.assertEqual(self.state()["state"], "idle")
        self.assertEqual(self.colors()[-1][-1], "background=#232627")

    def test_stop_while_kitty_in_background_is_done(self):
        ls = json.loads(json.dumps(LS))
        ls[0]["is_focused"] = False
        (self.d / "ls.json").write_text(json.dumps(ls))
        self.hook("UserPromptSubmit", KITTY_WINDOW_ID="1")
        self.hook("Stop", KITTY_WINDOW_ID="1")
        self.assertEqual(self.state()["state"], "done")
        self.assertEqual(self.colors()[-1][-1], "background=#222b25")
        self.assertEqual(json.loads(self.cli("list", "--json").stdout)[0]["state"], "done")

    def test_manual_waiting_beats_stop_not_done(self):
        self.hook("UserPromptSubmit")
        self.cli("waiting", CLAUDE_CODE_SESSION_ID=self.SID)
        self.hook("Stop")
        self.assertEqual(self.state()["state"], "waiting")

    def test_done_color_from_config(self):
        (self.d / "c.json").write_text(json.dumps({"state": {"colors": {"done": {"background": "#203020"}}}}))
        self.hook("UserPromptSubmit", CLAUDE_KITTY_CONFIG=str(self.d / "c.json"))
        self.hook("Stop", CLAUDE_KITTY_CONFIG=str(self.d / "c.json"))
        self.assertEqual(self.colors()[-1][-1], "background=#203020")

    def test_done_mark_not_set_in_standby(self):
        self.hook("UserPromptSubmit")
        self.hook("Stop")
        self.assertEqual(self.state()["state"], "done")
        self.assertEqual(self.titles(), [])  # 「●」は waiting だけ

    def test_permission_request_hook_is_silent_and_waits_for_its_tool(self):
        self.hook("UserPromptSubmit")
        self.hook("PermissionRequest", dict(BASH_A))  # stdout が空 = 許可の判断をしない（hook() が確認する）
        self.assertEqual(self.state()["state"], "waiting")
        self.hook("PostToolUse", dict(BASH_B, agent_id="x", tool_use_id="z"))
        self.assertEqual(self.state()["state"], "waiting")
        self.hook("PostToolUse", dict(BASH_A, tool_use_id="y"))
        self.assertEqual(self.state()["state"], "working")

    def test_resume_in_another_pane_resets_old_pane(self):
        self.hook("UserPromptSubmit", KITTY_WINDOW_ID="2")
        self.hook("SessionStart", {"source": "resume"}, KITTY_WINDOW_ID="4")
        self.assertEqual(self.state()["window"], 4)
        self.assertIn(["@", "--to", "unix:/fake/kitty-1", "set-colors", "--match", "id:2", "background=#232627"],
                      self.colors())

    def test_resume_elsewhere_keeps_color_of_pane_still_in_use(self):
        (self.sdir).mkdir(parents=True, exist_ok=True)
        (self.sdir / "other.json").write_text(json.dumps({"state": "working", "window": 2,
                                                          "socket": "unix:/fake/kitty-1"}))
        self.hook("UserPromptSubmit", KITTY_WINDOW_ID="2", **{})
        self.hook("UserPromptSubmit", KITTY_WINDOW_ID="4")
        self.assertNotIn("id:2", [c[5] for c in self.colors() if c[-1] == "background=#232627"])

    def test_repaint_from_records(self):
        self.hook("Notification")
        self.hook("UserPromptSubmit", {"session_id": "sess-2"}, KITTY_WINDOW_ID="2")
        (self.sdir / "dead.json").write_text(json.dumps({"state": "working", "window": 9, "pid": 999999999,
                                                         "socket": "unix:/fake/kitty-1"}))
        n = len(self.colors())
        r = self.cli("repaint")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), ["window 2: #22282e", "window 4: #2e2a22"])
        self.assertEqual([c[-1] for c in self.colors()[n:]], ["background=#22282e", "background=#2e2a22"])
        env = {k: v for k, v in self.env.items() if k != "KITTY_LISTEN_ON"}
        r = subprocess.run([sys.executable, str(STATE_BIN), "repaint"], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 1)

    def test_subagent_keeps_pane_working_after_stop(self):
        bt = [{"id": "a1", "type": "subagent", "status": "running"}]
        self.hook("UserPromptSubmit")
        self.hook("SubagentStart", {"agent_id": "a1", "agent_type": "general-purpose"})
        self.hook("Stop", {"background_tasks": bt})
        self.assertEqual(self.state()["state"], "working")
        self.hook("SubagentStop", {"agent_id": "a1", "background_tasks": bt})
        self.assertEqual(self.state()["state"], "done")
        self.assertEqual(self.colors()[-1][-1], "background=#222b25")

    def test_stored_backgrounds_for_watcher(self):
        self.hook("UserPromptSubmit")
        st = self.state()
        self.assertEqual((st["background"], st["idle_background"]), ("#22282e", "#232627"))

    NOTICE_JA = {"state": {"notice": {"contains": ["返事は不要", "返事不要"], "prefix": ["【通知】"]}}}

    def test_notice_from_other_session_keeps_color(self):
        (self.d / "none.json").write_text(json.dumps(self.NOTICE_JA))
        self.hook("UserPromptSubmit")
        self.hook("Stop")
        n = len(self.colors())
        self.hook("UserPromptSubmit", {"prompt": peer_prompt("【通知】ビルドが通りました")})
        self.hook("PostToolUse", dict(BASH_B))
        self.hook("Stop")
        self.assertEqual(self.state()["state"], "done")
        self.assertEqual(len(self.colors()), n)  # 一瞬の青も出さない
        self.hook("UserPromptSubmit", {"prompt": peer_prompt("これを直して")})
        self.hook("Stop")
        self.assertEqual(self.state()["state"], "done")
        self.assertEqual([c[-1] for c in self.colors()[n:]], ["background=#22282e", "background=#222b25"])

    def test_notice_markers_from_config(self):
        (self.d / "c.json").write_text(json.dumps({"state": {"notice": {"contains": ["FYI"]}}}))
        self.hook("UserPromptSubmit", {"prompt": peer_prompt("FYI")}, CLAUDE_KITTY_CONFIG=str(self.d / "c.json"))
        self.assertEqual(self.state()["state"], "idle")

    def test_notice_without_markers_colors_as_usual(self):
        # 既定（印なし）では、他のセッションからの「【通知】」も普通の依頼と同じに色を変える
        self.hook("UserPromptSubmit")
        self.hook("Stop")
        n = len(self.colors())
        self.hook("UserPromptSubmit", {"prompt": peer_prompt("【通知】ビルドが通りました")})
        self.hook("Stop")
        self.assertEqual(self.state()["state"], "done")
        self.assertEqual([c[-1] for c in self.colors()[n:]], ["background=#22282e", "background=#222b25"])

    def test_notice_found_in_transcript_at_stop(self):
        (self.d / "none.json").write_text(json.dumps(self.NOTICE_JA))
        t = self.d / "t.jsonl"
        t.write_text(json.dumps({"type": "user", "turnOrigin": "peer", "origin": {"kind": "peer", "body": "返事は不要"}}) + "\n")
        self.hook("SessionStart")
        self.hook("UserPromptSubmit")  # prompt に本文が無かった場合
        self.hook("Stop", {"transcript_path": str(t)})
        self.assertEqual(self.state()["state"], "idle")
        t.write_text(json.dumps({"type": "user", "turnOrigin": "human"}) + "\n")
        self.hook("UserPromptSubmit")
        self.hook("Stop", {"transcript_path": str(t)})
        self.assertEqual(self.state()["state"], "done")

    def test_done_dwell_stored_from_config(self):
        (self.d / "c.json").write_text(json.dumps({"state": {"done_dwell_seconds": 5}}))
        self.hook("UserPromptSubmit", CLAUDE_KITTY_CONFIG=str(self.d / "c.json"))
        self.assertEqual(self.state()["done_dwell"], 5.0)
        self.hook("Stop")
        self.assertEqual(self.state()["done_dwell"], 2.0)

    def test_subagents_command(self):
        self.assertEqual(self.cli("subagents", self.SID).stdout, "")
        self.hook("UserPromptSubmit")
        self.hook("SubagentStart", {"agent_id": "a1"})
        (self.d / "c.json").write_text(json.dumps({"state": {"subagent_colors": {"running": None}}}))
        r = self.cli("subagents", self.SID, CLAUDE_KITTY_CONFIG=str(self.d / "c.json"))
        self.assertEqual((r.returncode, r.stdout), (0, "サブ: 動作 1"))
        self.assertEqual(self.cli("subagents", "../x").stdout, "")

    def test_unchanged_state_does_not_call_kitten(self):
        self.hook("UserPromptSubmit")
        n = len(read_calls(self.calls))
        self.hook("UserPromptSubmit")
        self.hook("PreToolUse", {"tool_name": "Bash"})
        self.assertEqual(len(read_calls(self.calls)), n)

    def test_broken_input_exits_zero(self):
        for raw in ("", "{", "[]", "null", "\x00\x01garbage", json.dumps({"session_id": "../../etc/x"}),
                    json.dumps({"session_id": 5}), "x" * 100000):
            self.hook("UserPromptSubmit", raw=raw)
        self.assertFalse(self.sdir.exists() and any(self.sdir.glob("*.json")))
        self.assertEqual(read_calls(self.calls), [])

    def test_outside_kitty_does_nothing(self):
        env = {k: v for k, v in self.env.items() if not k.startswith("KITTY_")}
        r = subprocess.run([sys.executable, str(STATE_BIN), "hook", "UserPromptSubmit"],
                           input=json.dumps({"session_id": self.SID}), capture_output=True, text=True, env=env)
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertEqual(read_calls(self.calls), [])
        self.assertIsNone(self.state())

    def test_failing_or_missing_kitten_exits_zero(self):
        k, _ = fake_kitten(self.d / ".." / self.d.name, fail=True)
        self.hook("UserPromptSubmit", CLAUDE_KITTY_KITTEN=str(k))
        self.assertEqual(self.state()["state"], "working")
        self.hook("Stop", CLAUDE_KITTY_KITTEN=str(self.d / "no-such-kitten"))
        self.assertEqual(self.state()["state"], "done")  # 見ているか分からなければ未読のまま

    def test_unwritable_state_dir_exits_zero(self):
        (self.d / "claude-state").write_text("not a dir")
        self.hook("UserPromptSubmit")

    def test_broken_config_falls_back_to_defaults(self):
        (self.d / "bad.json").write_text("{")
        self.hook("UserPromptSubmit", CLAUDE_KITTY_CONFIG=str(self.d / "bad.json"))
        self.assertEqual(self.colors()[-1][-1], "background=#22282e")

    def test_manual_waiting_until_next_prompt(self):
        self.hook("UserPromptSubmit")
        r = self.cli("waiting", CLAUDE_CODE_SESSION_ID=self.SID)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "waiting"))
        self.hook("Stop")
        self.assertEqual(self.state()["state"], "waiting")
        self.assertEqual(self.cli("get", self.SID).stdout.strip(), "waiting")
        self.hook("UserPromptSubmit")
        self.assertEqual(self.state()["state"], "working")
        self.assertEqual(self.titles(), [MARK + "standby", "standby"])

    def test_manual_waiting_finds_session_by_window(self):
        self.hook("UserPromptSubmit")
        r = self.cli("waiting")
        self.assertEqual(r.stdout.strip(), "waiting")
        self.assertEqual(self.state()["reason"], "manual")

    def test_new_session_in_same_pane_replaces_old_record(self):
        self.hook("Notification")
        self.hook("SessionStart", {"session_id": "sess-2"})
        self.assertIsNone(self.state())
        self.assertEqual(self.state("sess-2")["state"], "idle")
        self.assertEqual(self.titles(), [MARK + "standby", "standby"])

    def test_waiting_in_main_tab_no_mark(self):
        self.hook("Notification", KITTY_WINDOW_ID="2")
        self.assertEqual(self.titles(), [])
        self.assertEqual(self.state()["role"], "dev-a")

    def test_get_paint_list(self):
        self.assertEqual(self.cli("get", self.SID).stdout, "\n")
        self.assertEqual(self.cli("paint", self.SID, "dev").stdout, "dev")
        self.hook("Notification")
        self.assertEqual(self.cli("paint", self.SID, "dev").stdout, "\033[38;5;208mdev\033[0m")
        self.assertEqual(self.cli("paint", "../bad", "dev").stdout, "dev")
        # 落ちた claude の記録（pid が動いていない）は既定では出さない
        (self.sdir / "dead.json").write_text(json.dumps({"state": "working", "pid": 999999999, "role": "ghost"}))
        (self.sdir / "junk.json").write_text("{")
        out = self.cli("list").stdout
        self.assertIn("dev-b", out)
        self.assertNotIn("ghost", out)
        self.assertIn("ghost", self.cli("list", "--all").stdout)
        rows = json.loads(self.cli("list", "--json").stdout)
        self.assertEqual([(r["role"], r["state"]) for r in rows], [("dev-b", "waiting")])

    def test_marks_command(self):
        self.hook("Notification")
        r = self.cli("marks", "--clear")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("tab 3: standby", r.stdout)

    def run_under_fake_claude(self, claudecode):
        """argv[0] が claude のプロセスの子として hook を走らせる（入れ子の判定を、抜け道なしで通す）。"""
        env = dict(self.env, CLAUDECODE=claudecode)
        env.pop("CLAUDE_STATE_SKIP_NESTED_CHECK")
        inner = f"{sys.executable} {STATE_BIN} hook UserPromptSubmit; true"  # true: bash が exec で置き換わらないように
        return subprocess.run(["bash", "-c", f'exec -a claude bash -c "{inner}"'], input=json.dumps({"session_id": self.SID}),
                              capture_output=True, text=True, env=env, timeout=20)

    def test_empty_claudecode_is_not_nested(self):
        r = self.run_under_fake_claude("")
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        st = self.state()
        self.assertEqual(st["state"], "working")
        self.assertIsInstance(st.get("pid"), int)  # 祖先の claude を見つけた

    def test_claudecode_set_is_nested(self):
        r = self.run_under_fake_claude("1")
        self.assertEqual(r.returncode, 0)
        self.assertIsNone(self.state())

    def test_nested_check_runs_without_skip(self):
        # テスト用の抜け道を外しても落ちない（祖先に claude がいてもいなくても）
        env = dict(self.env); env.pop("CLAUDE_STATE_SKIP_NESTED_CHECK")
        r = subprocess.run([sys.executable, str(STATE_BIN), "hook", "UserPromptSubmit"],
                           input=json.dumps({"session_id": self.SID}), capture_output=True, text=True, env=env)
        self.assertEqual((r.returncode, r.stdout), (0, ""))


def load_show():
    loader = importlib.machinery.SourceFileLoader("claude_show", str(SHOW_BIN))
    spec = importlib.util.spec_from_loader("claude_show", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class ClaudeShow(unittest.TestCase):
    def setUp(self):
        self.show = load_show()
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.kitten, self.calls = fake_kitten(self.d)
        self.conf = self.d / "c.json"
        self.conf.write_text(json.dumps({"view": {"guard": []}}))
        self.img = self.d / "a b.png"; self.img.write_bytes(b"\x89PNG")
        self.html = self.d / "r.html"; self.html.write_text("<p>x")
        self.env = {"CLAUDE_KITTY_KITTEN": str(self.kitten), "CLAUDE_KITTY_CONFIG": str(self.conf),
                    "KITTY_WINDOW_ID": "4", "KITTY_LISTEN_ON": "unix:/fake/kitty-1"}
        self.runs = []

    def tearDown(self):
        for c in read_calls(self.calls):
            for bad in FORBIDDEN:
                self.assertNotIn(bad, c)
        self.tmp.cleanup()

    def run_(self, argv, **kw):
        self.runs.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def test_classify(self):
        c = self.show.classify
        self.assertEqual(c("https://x.org/a.png")[0], "url")
        self.assertEqual(c("file:///tmp/a.HTML")[0], "html")
        for ext in ("png", "JPG", "jpeg", "webp", "gif", "svg"):
            self.assertEqual(c(f"/tmp/a.{ext}")[0], "image", ext)
        self.assertEqual(c("/tmp/a.txt")[0], "other")

    def test_image_overlay_on_own_pane_keeps_focus_elsewhere(self):
        code, out = self.show.show(str(self.img), self.env, None, run=self.run_)
        self.assertEqual(code, 0, out)
        launch = [c for c in read_calls(self.calls) if "launch" in c][0]
        self.assertIn("--type=overlay", launch)
        self.assertEqual(launch[launch.index("--match") + 1], "window_id:4")
        self.assertIn("--keep-focus", launch)  # 自分のペイン（4）に入力先が無いので入力先を変えない
        self.assertEqual(launch[launch.index("--") + 1:], [str(self.kitten), "icat", "--hold", "--align", "center",
                                                          str(self.img)])
        self.assertEqual(launch[-1], str(self.img))
        self.assertIn("note", out)  # standby タブにいる
        self.assertEqual(self.runs, [])

    def test_image_on_focused_pane_keeps_focus_too(self):
        # 入力中の文字が overlay に吸われないよう、自分のペインが入力先でも入力先は変えない
        code, out = self.show.show(str(self.img), dict(self.env, KITTY_WINDOW_ID="1"), None, run=self.run_)
        launch = [c for c in read_calls(self.calls) if "launch" in c][0]
        self.assertIn("--keep-focus", launch)
        self.assertEqual(out["focus"], "kept")
        self.assertIn("クリック", out["close"])

    def test_html_goes_to_claude_browser_show(self):
        code, out = self.show.show(str(self.html), self.env, 0.6, run=self.run_)
        self.assertEqual(code, 0)
        argv = self.runs[-1]
        self.assertTrue(argv[0].endswith("claude-browser"))
        self.assertEqual(argv[1:], ["open", self.html.as_uri(), "--area", "others", "--mode", "show", "--focus", "none",
                                    "--ratio", "0.6"])
        self.assertEqual(read_calls(self.calls), [])

    def test_url(self):
        self.show.show("https://example.com/", self.env, None, run=self.run_)
        self.assertEqual(self.runs[-1][1:], ["open", "https://example.com/", "--area", "others", "--mode", "show",
                                             "--focus", "none"])

    def test_outside_kitty_uses_xdg_open(self):
        env = {k: v for k, v in self.env.items() if not k.startswith("KITTY_")}
        code, out = self.show.show(str(self.img), env, None, run=self.run_)
        self.assertEqual((code, self.runs[-1]), (0, ["xdg-open", str(self.img)]))

    def test_guarded_front_does_nothing(self):
        self.conf.write_text(json.dumps({"view": {"guard": ["^\\./fullscreen-app"]}}))
        pg = lambda argv, **kw: (self.runs.append(argv), SimpleNamespace(returncode=0, stdout="77\n", stderr=""))[1]  # noqa: E731
        for target in (str(self.img), str(self.html)):
            code, out = self.show.show(target, self.env, None, run=pg, active_pid=lambda: 77)
            self.assertEqual(code, 2)
            self.assertIn("skipped", out)
        self.assertTrue(all(a[0] == "pgrep" for a in self.runs))
        self.assertEqual(read_calls(self.calls), [])

    def test_front_unknown_counts_as_guarded(self):
        self.conf.write_text(json.dumps({"view": {"guard": ["x"]}}))
        pg = lambda argv, **kw: SimpleNamespace(returncode=0, stdout="77\n", stderr="")  # noqa: E731

        def boom():
            raise SystemExit(1)
        self.assertEqual(self.show.show(str(self.img), self.env, None, run=pg, active_pid=boom)[0], 2)

    def test_missing_and_unknown_files(self):
        self.assertEqual(self.show.show(str(self.d / "nope.png"), self.env, None, run=self.run_)[0], 1)
        txt = self.d / "a.txt"; txt.write_text("x")
        self.assertEqual(self.show.show(str(txt), self.env, None, run=self.run_)[0], 1)
        self.assertEqual(read_calls(self.calls), [])

    def test_own_pane_missing(self):
        code, out = self.show.show(str(self.img), dict(self.env, KITTY_WINDOW_ID="99"), None, run=self.run_)
        self.assertEqual(code, 1)
        self.assertFalse([c for c in read_calls(self.calls) if "launch" in c])


import fcntl  # noqa: E402
import socket  # noqa: E402
from statewatch import RcError, Watch, done_info, find_sockets, focused_pane, rc_background, rc_ls  # noqa: E402


def wls(focused_osw=True, active_tab=1, panes=((1, (4, 5)), (2, (7,))), focus=None):
    """偽の ls: OS 窓 1 つ。panes は (タブ id, (ペイン id …))。各タブの最後の入力先は focus[タブ id]（既定は先頭）。"""
    focus = focus or {}
    tabs = []
    for tid, wids in panes:
        f = focus.get(tid, wids[0])
        tabs.append({"id": tid, "is_active": tid == active_tab, "is_focused": focused_osw and tid == active_tab,
                     "windows": [{"id": w, "is_active": w == f, "is_focused": w == f} for w in wids]})
    return [{"id": 1, "is_focused": focused_osw, "is_active": True, "tabs": tabs}]


class StateWatch(unittest.TestCase):
    """claude-state watch（kitty の外の常駐）。ls・get-colors・set-colors は偽物。"""
    A, B = "unix:/fake/kitty-1", "unix:/fake/kitty-2"
    BASE = "#232627"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.lss = {self.A: wls()}
        self.colors = {}  # (socket, ペイン) → 今の背景（get-colors の答え）
        self.sets, self.logs = [], []
        self.watch = Watch(self.d, merge_colors(None), self.BASE, ls=self.ls, get_bg=self.get_bg,
                           set_bg=self.set_bg, log=self.logs.append, alive=lambda e: True)

    def tearDown(self):
        self.tmp.cleanup()

    def ls(self, sock):
        v = self.lss.get(sock)
        if v is None:
            raise RcError("refused")
        return v

    def get_bg(self, sock, wid):
        return self.colors.get((sock, wid))  # 分からなければ None（塗り直しを確認するテストだけ色を入れる）

    def set_bg(self, sock, wid, bg):
        self.sets.append((sock, wid, bg))
        self.colors[(sock, wid)] = bg
        return True

    def put(self, sid, sock=None, **e):
        rec = {"session_id": sid, "socket": sock or self.A, "since": 0, "idle_background": self.BASE}
        rec.update(e)
        (self.d / f"{sid}.json").write_text(json.dumps(rec))

    def state(self, sid):
        return json.loads((self.d / f"{sid}.json").read_text())["state"]

    def run_ticks(self, times, socks=None):
        out = []
        for t in times:
            out += self.watch.tick(socks or [self.A], t)
        return out

    def test_done_becomes_idle_after_dwell(self):
        self.put("a", window=4, state="done")
        self.assertEqual(self.run_ticks([100, 101]), [])  # 1 秒では消さない
        self.assertEqual(self.state("a"), "done")
        self.assertEqual(self.run_ticks([102]), [(self.A, 4, "a")])  # 続けて 2 秒
        self.assertEqual(self.state("a"), "idle")
        self.assertIn((self.A, 4, self.BASE), self.sets)  # 背景を元の色に戻す
        self.assertEqual(self.run_ticks([103, 104]), [])  # 2 回目は何もしない

    def test_dwell_from_record_and_from_done_time(self):
        self.put("a", window=4, state="done", done_dwell=5, since=0)
        self.run_ticks([100, 104])
        self.assertEqual(self.state("a"), "done")
        self.run_ticks([105])
        self.assertEqual(self.state("a"), "idle")
        # 見ている最中に done になった（hook の ls が失敗したなど）: done になってから dwell 秒は残す
        self.put("b", window=4, state="done", since=200)
        self.run_ticks([201])
        self.assertEqual(self.state("b"), "done")
        self.run_ticks([202])
        self.assertEqual(self.state("b"), "idle")

    def test_leaving_midway_restarts_count(self):
        self.put("a", window=4, state="done")
        self.run_ticks([100, 101])
        self.lss[self.A] = wls(focus={1: 5})  # 同じタブの別のペインへ
        self.run_ticks([102])
        self.lss[self.A] = wls()  # 戻った: ここから数え直す
        self.run_ticks([103, 104])
        self.assertEqual(self.state("a"), "done")
        self.run_ticks([105])
        self.assertEqual(self.state("a"), "idle")

    def test_kitty_not_in_front_does_not_count(self):
        self.put("a", window=4, state="done")
        self.lss[self.A] = wls(focused_osw=False)  # 別のアプリが前面（OS 窓の is_focused が false）
        self.run_ticks(range(100, 110))
        self.assertEqual(self.state("a"), "done")
        self.lss[self.A] = wls()  # kitty が前面に来たところから数える
        self.run_ticks([110, 111])
        self.assertEqual(self.state("a"), "done")
        self.run_ticks([112])
        self.assertEqual(self.state("a"), "idle")

    def test_pane_in_hidden_tab_does_not_count(self):
        # タブ 2 のペイン 7 は、タブの中では最後の入力先（is_focused）でも、タブが表示されていない
        self.put("a", window=7, state="done")
        self.run_ticks(range(100, 110))
        self.assertEqual(self.state("a"), "done")
        self.assertIsNone(focused_pane([{"is_focused": True, "tabs": [
            {"is_active": False, "windows": [{"id": 7, "is_focused": True}]}]}]))
        self.lss[self.A] = wls(active_tab=2)
        self.run_ticks([110, 112])
        self.assertEqual(self.state("a"), "idle")

    def test_multiple_kitties(self):
        self.lss[self.B] = wls(focused_osw=False, panes=((1, (4,)),))
        self.put("a", window=4, state="done")
        self.put("b", window=4, state="done", sock=self.B)  # 同じ id のペインが別の kitty にある
        self.run_ticks([100, 102], [self.A, self.B])
        self.assertEqual((self.state("a"), self.state("b")), ("idle", "done"))
        # 前面が kitty B に移った（A は前面でなくなる）
        self.lss[self.A] = wls(focused_osw=False)
        self.lss[self.B] = wls(panes=((1, (4,)),))
        self.run_ticks([103, 104], [self.A, self.B])
        self.assertEqual(self.state("b"), "done")
        self.run_ticks([105], [self.A, self.B])
        self.assertEqual(self.state("b"), "idle")
        self.assertEqual([s for s in self.sets if s[2] == self.BASE], [(self.A, 4, self.BASE), (self.B, 4, self.BASE)])

    def test_ls_failure_is_logged_once_and_resets(self):
        self.put("a", window=4, state="done")
        self.run_ticks([100, 101])
        self.lss.pop(self.A)  # kitty が応答しない
        self.run_ticks([102, 103, 104])
        self.assertEqual(len(self.logs), 1)
        self.assertEqual(self.state("a"), "done")
        self.lss[self.A] = wls()
        self.run_ticks([105, 106])
        self.assertEqual(self.state("a"), "done")  # 見えなかった間は数えない
        self.run_ticks([107])
        self.assertEqual(self.state("a"), "idle")

    def test_exception_in_one_kitty_does_not_stop_others(self):
        self.lss[self.B] = wls()
        self.put("b", window=4, state="done", sock=self.B)
        self.lss[self.A] = "not a list"  # 壊れた ls
        self.run_ticks([100, 102], [self.A, self.B])
        self.assertEqual(self.state("b"), "idle")
        self.assertTrue(self.logs)

    def test_lock_held_by_hook_retries_next_tick(self):
        self.put("a", window=4, state="done")
        lock = open(self.d / ".a.lock", "w")
        fcntl.flock(lock, fcntl.LOCK_EX)  # hook が書いている最中
        try:
            self.run_ticks([100, 102])
            self.assertEqual(self.state("a"), "done")
        finally:
            lock.close()
        self.run_ticks([103])
        self.assertEqual(self.state("a"), "idle")

    def test_double_with_kitty_watcher_is_harmless(self):
        """kitty の watcher が先に idle にしていても、watch は何もしない（同じ mark_seen を 2 回呼んでも同じ）。"""
        self.put("a", window=4, state="done")
        self.assertEqual(len(self.watch.w.mark_seen(self.d, 4, self.A, 101)), 1)  # watcher の分
        self.assertEqual(self.watch.w.mark_seen(self.d, 4, self.A, 101), [])
        self.sets.clear()
        self.assertEqual(self.run_ticks([100, 102, 103]), [])
        self.assertEqual(self.state("a"), "idle")
        self.assertEqual(self.sets, [])

    def test_repaint_after_config_reload(self):
        self.put("a", window=4, state="working", updated=1)
        self.put("b", window=5, state="waiting", updated=1)
        self.put("c", window=9, state="working", updated=1)  # kitty にもう無いペイン
        self.colors.update({(self.A, 4): "#22282e", (self.A, 5): "#2e2a22"})
        self.run_ticks([100, 101])
        self.assertEqual(self.sets, [])  # 色が合っていれば何もしない
        self.colors.update({(self.A, 4): self.BASE, (self.A, 5): self.BASE})  # load-config で kitty.conf の色に戻った
        self.run_ticks([102])
        self.assertEqual(self.sets, [])  # 1 回目は待つ（hook の書きかけかもしれない）
        self.run_ticks([103])
        self.assertEqual(sorted(self.sets), [(self.A, 4, "#22282e"), (self.A, 5, "#2e2a22")])
        self.sets.clear()
        self.run_ticks([104, 105])
        self.assertEqual(self.sets, [])

    def test_transient_mismatch_is_not_repainted(self):
        self.put("a", window=4, state="working")
        self.colors[(self.A, 4)] = "#2e2a22"  # hook が記録を書いた直後、色を変える前
        self.run_ticks([100])
        self.colors[(self.A, 4)] = "#22282e"
        self.run_ticks([101])
        self.assertEqual(self.sets, [])

    def test_idle_pane_matching_base_is_left_alone(self):
        self.put("a", window=4, state="idle")
        self.colors[(self.A, 4)] = self.BASE
        self.run_ticks([100, 101, 102])
        self.assertEqual(self.sets, [])

    def test_expected_color_is_the_records_not_watchs_config(self):
        # 設定の色を変えたあと watch を再起動していない: hook が書いた記録の色を正とし、取り合わない
        self.put("a", window=4, state="working", background="#101010")
        self.colors[(self.A, 4)] = "#101010"
        self.run_ticks([100, 101, 102])
        self.assertEqual(self.sets, [])

    def test_failed_set_colors_is_logged_and_repaired(self):
        self.put("a", window=4, state="done")
        self.colors[(self.A, 4)] = "#222b25"
        ok = [False]

        def set_bg(sock, wid, bg):
            self.sets.append((sock, wid, bg))
            if ok[0]:
                self.colors[(sock, wid)] = bg
            return ok[0]
        self.watch.set_bg = set_bg
        self.run_ticks([100, 102])  # idle にしたが、塗るのに失敗（kitten の打ち切りなど）
        self.assertEqual(self.state("a"), "idle")
        self.assertTrue(any("塗れなかった" in m for m in self.logs))
        ok[0] = True
        self.assertEqual(self.colors[(self.A, 4)], "#222b25")  # 色の違いは、この回はまだ 1 回目なので待つ
        self.run_ticks([103])
        self.assertEqual(self.colors[(self.A, 4)], self.BASE)  # 2 回続いたので記録（idle）の色に直る

    def test_hook_racing_after_mark_is_repaired(self):
        self.put("a", window=4, state="done")
        self.colors[(self.A, 4)] = "#222b25"
        hook = {}

        def set_bg(sock, wid, bg):  # watch が塗る直前に hook が working にして塗った
            self.put("a", window=4, state="working", background="#22282e")
            self.colors[(sock, wid)] = bg
            hook["done"] = True
            return True
        self.watch.set_bg = set_bg
        self.run_ticks([100, 102])
        self.assertTrue(hook)
        self.watch.set_bg = self.set_bg
        self.run_ticks([103, 104])
        self.assertEqual(self.colors[(self.A, 4)], "#22282e")

    def test_failing_socket_is_forgotten_when_it_disappears(self):
        self.lss.pop(self.A)
        self.run_ticks([100])
        self.watch.tick([], 101)  # socket が消えた
        self.run_ticks([102])  # 同じパスで現れてまた失敗
        self.assertEqual(len(self.logs), 2)

    def test_done_info_edges(self):
        self.assertIsNone(done_info([], 4, self.A, 2.0))
        self.assertEqual(done_info([{"window": 4, "socket": self.A, "state": "done", "done_dwell": True}], 4, self.A, 2.0),
                         (2.0, 0.0))  # 壊れた値は既定
        self.assertIsNone(focused_pane(None))
        self.assertIsNone(focused_pane([]))

    def test_find_sockets(self):
        rt = self.d / "rt"
        rt.mkdir()
        socks = []
        for name in ("kitty-111", "kitty-222", "kitty-x"):
            s = socket.socket(socket.AF_UNIX)
            s.bind(str(rt / name))
            socks.append(s)
        (rt / "kitty-333").write_text("")  # socket でない
        try:
            got = find_sockets({"XDG_RUNTIME_DIR": str(rt)}, exists=lambda pid: pid == 111)
            self.assertEqual(got, [f"unix:{rt}/kitty-111", f"unix:{rt}/kitty-x"])  # 222 は kitty が落ちて残ったもの
            self.assertEqual(find_sockets({}, ["unix:/a", "unix:/a", "", "unix:/b"]), ["unix:/a", "unix:/b"])
            self.assertEqual(find_sockets({"XDG_RUNTIME_DIR": str(self.d / "none")}), [])
        finally:
            for s in socks:
                s.close()


class FakeKittySocket:
    """kitty のリモート操作の socket の偽物（1 回の接続に 1 つ答える）。"""

    def __init__(self, path, answers):
        import threading
        self.path, self.answers, self.got = path, answers, []
        self.srv = socket.socket(socket.AF_UNIX)
        self.srv.bind(path)
        self.srv.listen(4)
        self.t = threading.Thread(target=self.serve, daemon=True)
        self.t.start()

    def serve(self):
        for ans in self.answers:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            with c:
                buf = b""
                while not buf.endswith(b"\x1b\\"):
                    chunk = c.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                self.got.append(json.loads(buf[len(b"\x1bP@kitty-cmd"):-2]))
                if ans is not None:
                    c.sendall(ans)

    def close(self):
        self.srv.close()


class StateWatchRc(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "kitty-1")

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def reply(obj):
        return b"\x1bP@kitty-cmd" + json.dumps(obj).encode() + b"\x1b\\"

    def test_ls_and_get_colors(self):
        data = json.dumps(wls())
        srv = FakeKittySocket(self.path, [self.reply({"ok": True, "data": data}),
                                          self.reply({"ok": True, "data": "active_border_color #e0a000\nbackground  #222B25\n"})])
        try:
            self.assertEqual(focused_pane(rc_ls("unix:" + self.path)), 4)
            self.assertEqual(rc_background("unix:" + self.path, 4), "#222b25")
        finally:
            srv.close()
        self.assertEqual([g["cmd"] for g in srv.got], ["ls", "get-colors"])
        self.assertEqual(srv.got[1]["payload"]["match"], "id:4")

    def test_errors(self):
        srv = FakeKittySocket(self.path, [self.reply({"ok": False, "error": "no"}), b"garbage", None])
        try:
            for _ in range(3):
                with self.assertRaises(RcError):
                    rc_ls("unix:" + self.path, timeout=1)
        finally:
            srv.close()
        with self.assertRaises(RcError):
            rc_ls("unix:" + self.path + "-none")  # socket が無い（kitty が落ちた）
        with self.assertRaises(RcError):
            rc_ls("tcp:localhost:1")


class StateWatchCli(unittest.TestCase):
    def test_watch_once_marks_and_survives(self):
        with tempfile.TemporaryDirectory() as t:
            rt = Path(t)
            (rt / "claude-state").mkdir()
            path = str(rt / "kitty-1")
            rec = {"session_id": "a", "socket": "unix:" + path, "window": 4, "state": "done", "since": 0,
                   "idle_background": "#232627"}
            (rt / "claude-state" / "a.json").write_text(json.dumps(rec))
            data = json.dumps(wls())
            srv = FakeKittySocket(path, [StateWatchRc.reply({"ok": True, "data": data})] * 4)
            kit, calls = fake_kitten(rt)
            env = {"XDG_RUNTIME_DIR": str(rt), "CLAUDE_KITTY_CONFIG": str(rt / "none.json"),
                   "CLAUDE_KITTY_KITTEN": str(kit), "XDG_CONFIG_HOME": str(rt)}
            cs = load_claude_state()
            clock = iter([100.0, 103.0])
            sleeps = []

            class Stop(Exception):
                pass

            def sleep(sec):
                sleeps.append(sec)
                if len(sleeps) >= 2:
                    raise Stop  # 2 回まわしたら止める
            try:
                # 1 回目でフォーカスを覚え、2 回目（3 秒後）に idle
                with self.assertRaises(Stop):
                    cs.cmd_watch(env, ["unix:" + path], sleep=sleep, now=lambda: next(clock), monotonic=lambda: 5.0)
            finally:
                srv.close()
            self.assertEqual(sleeps, [1.0, 1.0])  # 壁時計でなく単調な時計で待つ
            self.assertEqual(read_calls(calls), [["@", "--to", "unix:" + path, "set-colors", "--match", "id:4",
                                                  "background=#232627"]])
            self.assertEqual(json.loads((rt / "claude-state" / "a.json").read_text())["state"], "idle")
            # socket が無くても落ちずに errors.log に残す
            self.assertEqual(cs.cmd_watch(env, ["unix:" + path + "-gone"], once=True), 0)
            self.assertIn("watch", (rt / "claude-state" / "errors.log").read_text())


class StateWatchClock(unittest.TestCase):
    def test_wall_clock_going_back_does_not_stall(self):
        with tempfile.TemporaryDirectory() as t:
            env = {"XDG_RUNTIME_DIR": t, "CLAUDE_KITTY_CONFIG": str(Path(t) / "none.json"), "XDG_CONFIG_HOME": t}
            cs = load_claude_state()
            walls = iter([5000.0, 1400.0])  # 1 周の間に壁時計が 1 時間戻った
            mono = iter([10.0, 10.2])
            sleeps = []

            class Stop(Exception):
                pass

            def sleep(sec):
                sleeps.append(sec)
                raise Stop
            with self.assertRaises(Stop):
                cs.cmd_watch(env, ["unix:" + t + "/none"], sleep=sleep, now=lambda: next(walls, 0.0),
                             monotonic=lambda: next(mono))
            self.assertLessEqual(sleeps[0], 1.0)


def load_claude_state():
    p = Path(__file__).resolve().parent.parent / "bin" / "claude-state"
    loader = importlib.machinery.SourceFileLoader("claude_state_cli", str(p))
    spec = importlib.util.spec_from_loader("claude_state_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# 別ファイルのテストも、ここから 1 回で走らせる（コミットの前と CI は python3 tests/run.py だけでよいように）。
# 名前がぶつからないよう、クラス名の頭にファイルごとの印を付ける
SIDE_TESTS = {
    "lang_check_test": "LangCheck",
    "guard_hook_test": "GuardHook",
    "session_stats_test": "SessionStats",
    "roster_test": "Roster",
    "version_test": "Version",
    "changelog_test": "Changelog",
    "release_test": "Release",
    "install_test": "Install",
    "doctor_test": "Doctor",
    "launch_test": "Launch",
    "browser_test": "Browser",
    "macos_test": "Mac",
}


def _load_side_tests():
    here = Path(__file__).resolve().parent
    for modname, prefix in SIDE_TESTS.items():
        loader = importlib.machinery.SourceFileLoader(modname, str(here / f"{modname}.py"))
        spec = importlib.util.spec_from_loader(modname, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        for name, obj in vars(mod).items():
            if isinstance(obj, type) and issubclass(obj, unittest.TestCase) and obj.__module__ == modname:
                assert prefix + name not in globals(), prefix + name
                globals()[prefix + name] = obj


_load_side_tests()

if __name__ == "__main__":
    unittest.main(verbosity=1)
