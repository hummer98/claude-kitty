#!/usr/bin/env python3
"""claude-view hot（注目のペインを統括の右の 2 列目に縦 100% で出す／splits に戻す）のテスト。

kitty は偽物（tests/run.py の決まり）。偽の kitten は tall の列の順（window group の並び）と入力先を持ち、
focus-window・move_window_to_top・move_window_backward・goto-layout をその状態に効かせる。
move_window_to_top は入力先と先頭を入れ替える、move_window_backward は入力先を 1 つ前と入れ替える
（どちらも kitty 0.51 の試験用の kitty で計測した動き）。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from view import base_window, column_of, find_window, focused_window, is_hot, role_window  # noqa: E402

VIEW = ROOT / "bin" / "claude-view"

FAKE = r'''#!PYTHON
import json, sys
state_path, calls_path = STATE, CALLS
a = sys.argv[1:]
st = json.load(open(state_path))
to = a[a.index("--to") + 1] if "--to" in a else None
cmd = [x for i, x in enumerate(a) if x != "@" and x != "--to" and (i == 0 or a[i - 1] != "--to")]
open(calls_path, "a").write(json.dumps({"to": to, "args": cmd}) + "\n")
if st.get("fail"):
    sys.exit(1)

def splits_state(order):
    tree = order[-1]
    for g in reversed(order[:-1]):
        tree = {"horizontal": True, "one": g, "two": tree}
    return {"class": "Splits", "pairs": tree if len(order) > 1 else {},
            "all_windows": {"window_groups": [{"id": g, "window_ids": [g] + st["overlays"].get(str(g), [])}
                                              for g in order]}}

if cmd[:1] == ["ls"]:
    if st.get("broken_ls"):
        sys.stdout.write("not json")
        sys.exit(0)
    order = st["order"]
    if st["layout"] == "tall":
        ls_state = {"class": "Tall", "all_windows": {"window_groups": [
            {"id": g, "window_ids": [g] + st["overlays"].get(str(g), [])} for g in order]}}
    else:
        ls_state = splits_state(order)
    wins = []
    for g in order:
        role = st["roles"].get(str(g))
        wins.append({"id": g, "is_active": g == st["active"], "columns": 50, "lines": 40, "neighbors": {},
                     "user_vars": {"role": role} if role else {}})
        for o in st["overlays"].get(str(g), []):
            wins.append({"id": o, "is_active": False, "columns": 50, "lines": 40, "neighbors": {}, "user_vars": {}})
    tabs = [{"id": 1, "is_active": True, "title": "main", "layout": st["layout"], "layout_state": ls_state,
             "groups": [{"id": g, "windows": [g]} for g in order], "windows": wins}]
    for t in st.get("extra_tabs", []):
        tabs.append(t)
    sys.stdout.write(json.dumps([{"id": 1, "is_active": True, "is_focused": True, "tabs": tabs}]))
elif cmd[:1] == ["focus-window"]:
    st["active"] = int(cmd[cmd.index("--match") + 1].split(":")[1])
elif cmd[:1] == ["goto-layout"]:
    st["layout"] = cmd[-1]
elif cmd[:2] == ["action", "move_window_to_top"] and not st.get("stuck") and not st.get("stuck_top"):
    o = st["order"]; i = o.index(st["active"]); o[0], o[i] = o[i], o[0]
elif cmd[:2] == ["action", "move_window_backward"] and not st.get("stuck"):
    o = st["order"]; i = o.index(st["active"])
    if i > 0:
        o[i - 1], o[i] = o[i], o[i - 1]
json.dump(st, open(state_path, "w"))
'''


class HotCli(unittest.TestCase):
    ROLES = {"1": "main", "2": "dev-a", "3": "dev-b", "4": "dev-c", "5": "dev-d"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.state = self.d / "state.json"
        self.calls = self.d / "calls"
        k = self.d / "kitten"
        k.write_text(FAKE.replace("PYTHON", sys.executable).replace("STATE", repr(str(self.state)))
                     .replace("CALLS", repr(str(self.calls))))
        k.chmod(0o755)
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.d), "LANG": "C.UTF-8",
                    "CLAUDE_KITTY_KITTEN": str(k), "CLAUDE_KITTY_CONFIG": str(self.d / "config.json"),
                    "XDG_RUNTIME_DIR": str(self.d / "run")}
        (self.d / "run").mkdir()
        self.set_state()

    def tearDown(self):
        self.tmp.cleanup()

    def set_state(self, **kw):
        st = {"layout": "splits", "order": [1, 2, 3, 4, 5], "active": 4, "roles": dict(self.ROLES), "overlays": {}}
        st.update(kw)
        self.state.write_text(json.dumps(st))

    def st(self):
        return json.loads(self.state.read_text())

    def run_view(self, *args, to=True, **env):
        pre = ["--to", "unix:/fake.sock"] if to else []
        return subprocess.run([sys.executable, str(VIEW), *pre, *args], env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=30)

    def calls_list(self):
        if not self.calls.exists():
            return []
        return [json.loads(x)["args"] for x in self.calls.read_text().splitlines()]

    def changes(self):
        return [c for c in self.calls_list() if c[0] != "ls"]

    def errors(self):
        p = self.d / "run" / "claude-view" / "errors.log"
        return p.read_text() if p.exists() else ""

    def test_splits_to_tall_puts_target_right_of_left(self):
        r = self.run_view("hot", "4")
        self.assertEqual(r.returncode, 0, r.stderr)
        st = self.st()
        self.assertEqual(st["layout"], "tall")
        self.assertEqual(st["order"][:2], [1, 4])  # 統括が左の列、注目が 2 列目
        self.assertEqual(st["active"], 4)          # 入力先は押したペインのまま
        ch = self.changes()
        self.assertEqual(ch[0], ["goto-layout", "--match", "window_id:4", "tall"])
        self.assertEqual(ch[1], ["resize-window", "--match", "id:4", "--axis", "reset"])
        self.assertEqual(ch[2:5], [["focus-window", "--match", "id:1"], ["action", "move_window_to_top"],
                                   ["focus-window", "--match", "id:4"]])
        self.assertEqual(ch.count(["action", "move_window_backward"]), 2)  # 4 番目（0 始まり 3）→ 1
        self.assertEqual(ch[-1], ["focus-window", "--match", "id:4"])
        self.assertEqual(self.errors(), "")

    def test_tall_hot_target_goes_back_to_splits(self):
        self.set_state(layout="tall", order=[1, 4, 2, 3, 5])
        r = self.run_view("hot", "4")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.st()["layout"], "splits")
        self.assertEqual(self.changes(), [["goto-layout", "--match", "window_id:4", "splits"],
                                          ["focus-window", "--match", "id:4"]])  # 並べ替えない
        self.assertEqual(self.st()["order"], [1, 4, 2, 3, 5])

    def test_tall_other_target_switches_without_leaving_tall(self):
        self.set_state(layout="tall", order=[1, 4, 2, 3, 5], active=5)
        r = self.run_view("hot", "dev-d")  # 役割の名前でも呼べる
        self.assertEqual(r.returncode, 0, r.stderr)
        st = self.st()
        self.assertEqual((st["layout"], st["order"][:2], st["active"]), ("tall", [1, 5], 5))

    def test_role_call_returns_focus_to_caller(self):
        # 統括のペインから役割の名前で呼ぶ: 並べ替えのあと、入力先を統括へ戻す
        self.set_state(active=1)
        r = self.run_view("hot", "dev-b")
        self.assertEqual(r.returncode, 0, r.stderr)
        st = self.st()
        self.assertEqual((st["order"][:2], st["active"]), ([1, 3], 1))
        self.assertEqual(self.changes()[-1], ["focus-window", "--match", "id:1"])

    def test_left_moved_to_top_when_not_first(self):
        self.set_state(order=[2, 3, 1, 4, 5], active=5)
        r = self.run_view("hot", "5")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.st()["order"][:2], [1, 5])

    def test_already_second_needs_no_backward(self):
        r = self.run_view("hot", "2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(["action", "move_window_backward"], self.changes())
        self.assertEqual(self.st()["order"][:2], [1, 2])

    def test_left_missing_refused_and_logged(self):
        roles = dict(self.ROLES); del roles["1"]
        self.set_state(roles=roles)
        r = self.run_view("hot", "4")
        self.assertEqual(r.returncode, 1)
        self.assertIn("統括（main）がいない", r.stderr)
        self.assertEqual(self.changes(), [])  # レイアウトを変えない
        self.assertIn("hot 4: ", self.errors())
        self.assertIn("いない", self.errors())

    def test_left_missing_in_tall_still_goes_back(self):
        roles = dict(self.ROLES); del roles["1"]
        self.set_state(layout="tall", roles=roles)
        r = self.run_view("hot", "4")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.st()["layout"], "splits")

    def test_target_is_left(self):
        r = self.run_view("hot", "main")
        self.assertEqual(r.returncode, 1)
        self.assertIn("自身", r.stderr)
        self.assertEqual(self.changes(), [])
        self.assertIn("自身", self.errors())
        # tall のまま統括で押したら戻す
        self.set_state(layout="tall", order=[1, 4, 2, 3, 5], active=1)
        r = self.run_view("hot", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.st()["layout"], "splits")

    def test_unknown_target(self):
        r = self.run_view("hot", "99")
        self.assertEqual(r.returncode, 1)
        self.assertIn("99 が kitty に見つからない", self.errors())
        r = self.run_view("hot", "ghost")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.changes(), [])

    def test_broken_ls_logged_without_traceback(self):
        self.set_state(broken_ls=True)
        r = self.run_view("hot", "4")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("ls の出力が読めない", self.errors())
        self.assertEqual(self.changes(), [])

    def test_kitten_failure_logged(self):
        self.set_state(fail=True)
        r = self.run_view("hot", "4")
        self.assertEqual(r.returncode, 1)
        self.assertIn("kitten @ ls に失敗", self.errors())

    def test_stuck_backward_gives_up_and_logs(self):
        # move_window_backward が効かない（kitty の動きが変わった）: 回り続けず、上限で止めて残す
        self.set_state(order=[1, 2, 3, 4, 5], active=5, stuck=True)
        r = self.run_view("hot", "5")
        self.assertEqual(r.returncode, 1)
        self.assertIn("2 列目に動かせなかった", self.errors())
        self.assertEqual(self.changes().count(["action", "move_window_backward"]), 9)  # 3 回 × 3 つ
        self.assertEqual(self.st()["active"], 5)  # 失敗しても入力先は呼ぶ前へ戻す

    def test_stuck_to_top_detected(self):
        self.set_state(order=[2, 1, 3, 4, 5], active=2, stuck=True)
        r = self.run_view("hot", "2")
        self.assertEqual(r.returncode, 1)
        self.assertIn("統括を先頭に動かせなかった", self.errors())

    def test_only_to_top_stuck_is_not_reported_as_success(self):
        # 回帰: move_window_to_top だけが効かず backward は効くと、注目が 2 列目に来ても統括が先頭にいない
        self.set_state(order=[2, 1, 3, 4, 5], active=3, stuck_top=True)
        r = self.run_view("hot", "4")
        self.assertEqual(r.returncode, 1)
        self.assertIn("統括を先頭に動かせなかった", self.errors())
        self.assertNotIn(["action", "move_window_backward"], self.changes())
        self.assertEqual(self.st()["active"], 3)  # 入力先は呼ぶ前のペインへ戻す
        self.assertEqual(self.changes()[-1], ["focus-window", "--match", "id:3"])

    def test_backward_sent_in_one_batch(self):
        # 入力先が注目のペインに移っている間を短くする: ls は並べる前と確かめの 2 回だけ
        self.set_state(order=[1, 2, 3, 4, 5], active=5)
        r = self.run_view("hot", "5")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls_list()
        first = calls.index(["action", "move_window_to_top"])
        last = len(calls) - 1 - calls[::-1].index(["action", "move_window_backward"])
        self.assertEqual(calls[first + 1:last + 1].count(["ls"]), 1)
        self.assertEqual(calls[first + 1:].count(["ls"]), 2)
        self.assertEqual(self.st()["order"][:2], [1, 5])

    def test_guard_in_front_stops_and_logs(self):
        d = self.d
        s = subprocess.Popen(["sleep", "33"])
        self.addCleanup(s.wait)
        self.addCleanup(s.kill)
        tok = d / "token"
        (d / "qdbus").write_text(f"#!/bin/sh\ncase \"$3\" in *loadScript) echo \"$5\" > {tok}; echo 1;; esac\n")
        (d / "journalctl").write_text(f"#!/bin/sh\nt=$(cat {tok}); echo \"$t {s.pid}\"; echo \"$t END\"\n")
        for n in ("qdbus", "journalctl"):
            (d / n).chmod(0o755)
        (d / "config.json").write_text(json.dumps({"view": {"guard": ["^sleep 33$"]}}))
        env = dict(PATH=f"{d}:/usr/bin:/bin", CLAUDE_KITTY_QDBUS=str(d / "qdbus"))
        r = self.run_view("hot", "4", **env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("保護する窓", self.errors())
        self.assertEqual(self.changes(), [])
        # tall のままのタブへのほかの並べ替えも、戻す前に止める
        self.set_state(layout="tall", order=[1, 4, 2, 3, 5])
        r = self.run_view("equalize", **env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.changes(), [])
        self.assertEqual(self.st()["layout"], "tall")

    def test_error_log_rotates(self):
        log = self.d / "run" / "claude-view" / "errors.log"
        log.parent.mkdir()
        log.write_text("x" * (256 * 1024))
        self.run_view("hot", "ghost")
        self.assertIn("ghost", log.read_text())
        self.assertEqual(len((log.parent / "errors.log.1").read_text()), 256 * 1024)

    def test_dry_run_changes_nothing(self):
        r = self.run_view("--dry-run", "hot", "dev-c")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.changes(), [])
        self.assertIn("goto-layout --match window_id:4 tall", r.stdout)
        self.assertEqual(r.stdout.splitlines()[-1], "focus-window --match id:4")

    def test_config_left_is_used(self):
        (self.d / "config.json").write_text(json.dumps({"view": {"left": "dev-a"}}))
        r = self.run_view("hot", "4")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.st()["order"][:2], [2, 4])

    def test_overlay_id_maps_to_base(self):
        # claude-show の画像に入力先があると @active-kitty-window-id は overlay の id になる
        self.set_state(overlays={"4": [9]})
        r = self.run_view("hot", "9")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.st()["order"][:2], [1, 4])

    def test_success_writes_no_log(self):
        self.run_view("hot", "4")
        self.run_view("hot", "4")
        self.assertEqual(self.st()["layout"], "splits")
        self.assertFalse((self.d / "run" / "claude-view" / "errors.log").exists())

    def test_other_commands_leave_tall_first(self):
        # hot で tall のままのタブに claude-view equalize: splits の木が前提なので、先に splits に戻す
        self.set_state(layout="tall", order=[1, 4, 2, 3, 5])
        r = self.run_view("--dry-run", "equalize")
        self.assertTrue(r.stdout.startswith("goto-layout splits（tall から戻す"), r.stdout)
        self.assertNotIn("park", r.stdout)  # tall の位置から段を読んだ操作は出さない
        self.assertEqual(self.changes(), [])
        r = self.run_view("equalize")
        self.assertEqual(r.returncode, 0, r.stderr)
        ch = self.changes()
        self.assertEqual(ch[0], ["goto-layout", "--match", "window_id:1", "splits"])
        self.assertTrue(any(c[0] == "detach-window" for c in ch[1:]))
        self.assertEqual(self.st()["layout"], "splits")

    def test_splits_commands_do_not_touch_layout(self):
        r = self.run_view("equalize")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(any(c[0] == "goto-layout" for c in self.changes()))

    # --- ソケットの決め方（--to が無いとき） ---

    def sock_env(self):
        """$XDG_RUNTIME_DIR に kitty-<このテストの pid>（claude-view の親）と、統括のいる別の kitty-<生きている pid>。"""
        run = self.d / "run"
        (run / f"kitty-{os.getpid()}").write_text("")
        other = subprocess.Popen(["sleep", "30"])
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        (run / f"kitty-{other.pid}").write_text("")
        return run, other

    def tos(self):
        return {json.loads(x)["to"] for x in self.calls.read_text().splitlines()}

    def test_socket_from_parent_kitty_pid(self):
        # launch --type=background の子には KITTY_LISTEN_ON が無い。親（kitty）の pid の socket を選ぶ
        run, _ = self.sock_env()
        r = self.run_view("hot", "4", to=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.tos(), {f"unix:{run}/kitty-{os.getpid()}"})

    def test_socket_env_wins_over_parent(self):
        self.sock_env()
        r = self.run_view("hot", "4", to=False, KITTY_LISTEN_ON="unix:/from-env")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.tos(), {"unix:/from-env"})

    def test_socket_without_parent_is_ambiguous(self):
        # 親の socket が無ければ従来どおり統括のいる kitty を探す（2 つとも統括がいれば決めない）
        run, other = self.sock_env()
        (run / f"kitty-{os.getpid()}").unlink()
        third = subprocess.Popen(["sleep", "30"])
        self.addCleanup(third.wait)
        self.addCleanup(third.kill)
        (run / f"kitty-{third.pid}").write_text("")
        r = self.run_view("hot", "4", to=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("1 つに決められない", self.errors())

    def test_socket_none_logged(self):
        r = self.run_view("hot", "4", to=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("kitty が見つからない", self.errors())


class HotPure(unittest.TestCase):
    def tab(self, layout, order, roles, overlays=None, active=None):
        overlays = overlays or {}
        return {"layout": layout, "is_active": True,
                "layout_state": {"all_windows": {"window_groups": [
                    {"id": g, "window_ids": [g] + overlays.get(g, [])} for g in order]}},
                "windows": [{"id": g, "is_active": g == active, "user_vars": {"role": roles[g]} if g in roles else {}}
                            for g in order] + [{"id": o, "user_vars": {}} for v in overlays.values() for o in v]}

    def test_columns_and_hot(self):
        t = self.tab("tall", [1, 4, 2], {1: "main", 4: "dev-a", 2: "dev-b"})
        self.assertEqual((column_of(t, 1), column_of(t, 4), column_of(t, 2), column_of(t, 99)), (0, 1, 2, None))
        self.assertTrue(is_hot(t, 4, 1))
        self.assertFalse(is_hot(t, 2, 1))
        self.assertFalse(is_hot(dict(t, layout="splits"), 4, 1))
        self.assertFalse(is_hot(t, 4, None))

    def test_groups_fallback_and_empty(self):
        t = {"layout": "tall", "groups": [{"id": 7, "windows": [3]}, {"id": 8, "windows": [5]}], "windows": []}
        self.assertEqual(column_of(t, 5), 1)  # group id でなく window id で数える
        self.assertIsNone(column_of({}, 1))
        self.assertIsNone(role_window({}, "main"))
        self.assertIsNone(find_window([], "main"))
        self.assertIsNone(focused_window([]))

    def test_find_window_role_id_overlay(self):
        t = self.tab("splits", [1, 2], {1: "main", 2: "dev-a"}, overlays={2: [9]}, active=1)
        osw = [{"is_focused": True, "tabs": [t]}]
        self.assertEqual(find_window(osw, "dev-a")[0], 2)
        self.assertEqual(find_window(osw, "2")[0], 2)
        self.assertEqual(find_window(osw, "9")[0], 2)  # overlay → 本体
        self.assertEqual(base_window(t, 1), 1)
        self.assertIsNone(find_window(osw, "nobody"))
        self.assertEqual(focused_window(osw), 1)
        self.assertEqual(focused_window([{"is_focused": False, "is_active": True, "tabs": [t]}]), 1)

    def test_user_vars_missing_or_none(self):
        t = {"layout": "splits", "windows": [{"id": 1, "user_vars": None}, {"id": 2}]}
        self.assertIsNone(role_window(t, "main"))
        self.assertEqual(find_window([{"tabs": [t]}], "2")[0], 2)


if __name__ == "__main__":
    unittest.main()
