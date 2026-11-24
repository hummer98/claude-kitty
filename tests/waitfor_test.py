#!/usr/bin/env python3
"""claude-waitfor（lib/waitfor.py・bin/claude-waitfor）と、長く続く背景のシェル（lib/bgshell.py）のテスト。"""
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import bgshell as B  # noqa: E402
import waitfor as W  # noqa: E402

BIN = ROOT / "bin" / "claude-waitfor"


class FakeSys:
    """プロセスの木と時計の偽物。table: {pid: (ppid, argv)}。steps: sleep のたびに呼ぶ関数のリスト。"""

    def __init__(self, table, steps=()):
        self.table = dict(table)
        self.now = 0.0
        self.steps = list(steps)
        self.sleeps = []

    def processes(self):
        return [(p, a) for p, (_q, a) in sorted(self.table.items())]

    def ppid(self, pid):
        return self.table.get(pid, (None, None))[0]

    def argv(self, pid):
        return self.table.get(pid, (None, None))[1]

    def clock(self):
        return self.now

    def sleep(self, sec):
        self.sleeps.append(sec)
        self.now += sec
        if self.steps:
            self.steps.pop(0)(self)


SHELL = ["/bin/bash", "-c", "until ! pgrep -f 'luajit tests/run.lua'; do sleep 2; done"]
WAITER = ["python3", "/x/bin/claude-waitfor", "proc", "luajit tests/run.lua"]


class Duration(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(W.parse_duration("30m"), 1800)
        self.assertEqual(W.parse_duration("90s"), 90)
        self.assertEqual(W.parse_duration("2h"), 7200)
        self.assertEqual(W.parse_duration("45"), 45)
        self.assertEqual(W.parse_duration("1.5m"), 90)

    def test_rejects_zero_negative_and_too_long(self):
        for bad in ("0", "0m", "-1", "25h", "abc", "", "1d", "nan", "inf"):
            with self.assertRaises(ValueError, msg=bad):
                W.parse_duration(bad)
        self.assertEqual(W.parse_duration("24h"), 86400)


class Proc(unittest.TestCase):
    def tree(self):
        # 1 ← 100 claude ← 200 bash -c（待つシェル） ← 300 claude-waitfor（自分）
        return {100: (1, ["claude"]), 200: (100, SHELL), 300: (200, WAITER)}

    def test_self_and_ancestors_are_not_counted(self):
        s = FakeSys(self.tree())
        code, msg = W.wait_proc("luajit tests/run.lua", 60, 2, me=300, sys_=s)
        self.assertEqual(code, W.EXIT_DONE, msg)
        self.assertEqual(s.sleeps, [])          # 初めから無い
        self.assertIn("無い", msg)

    def test_fork_of_ancestor_is_not_counted(self):
        # パイプやサブシェルで fork した子（祖先と同じコマンド行のまま）は数えない
        s = FakeSys(self.tree())
        s.table[210] = (200, SHELL)
        s.table[211] = (210, SHELL)             # fork の fork
        self.assertEqual(W.matching(re.compile("luajit"), 300, s), [])

    def test_unrelated_shell_with_same_text_is_counted(self):
        # 祖先でない別のシェル（別のセッションの同じ待ち）は数える（pgrep -f と同じ）
        s = FakeSys(self.tree())
        s.table[500] = (400, SHELL)
        self.assertEqual([p for p, _ in W.matching(re.compile("luajit"), 300, s)], [500])

    def test_other_waiters_and_their_shells_are_not_counted(self):
        # 別のセッションが同じものを claude-waitfor で待っていても、互いに一致して待ち合わない
        s = FakeSys(self.tree())
        s.table[600] = (1, ["claude"])
        s.table[610] = (600, ["/bin/bash", "-c", "claude-waitfor proc 'luajit tests/run.lua'"])
        s.table[620] = (610, ["timeout", "60", "python3", "/x/bin/claude-waitfor", "proc", "luajit tests/run.lua"])
        s.table[630] = (620, ["python3", "/x/bin/claude-waitfor", "proc", "luajit tests/run.lua"])
        s.table[640] = (610, ["luajit", "tests/run.lua"])               # 本当に待つ相手は数える
        self.assertEqual([p for p, _ in W.matching(re.compile("luajit tests/run.lua"), 300, s)], [640])
        # 上りはシェルの間だけ（呼んだ claude 自体は外さない）
        s.table[600] = (1, ["claude", "luajit tests/run.lua"])
        self.assertEqual([p for p, _ in W.matching(re.compile("luajit tests/run.lua"), 300, s)], [600, 640])

    def test_waits_until_gone(self):
        def finish(fs):
            if fs.now >= 6:
                fs.table.pop(250, None)
        s = FakeSys(self.tree(), steps=[finish] * 10)
        s.table[250] = (200, ["luajit", "tests/run.lua"])
        code, msg = W.wait_proc("luajit tests/run.lua", 60, 2, me=300, sys_=s)
        self.assertEqual(code, W.EXIT_DONE)
        self.assertEqual(s.sleeps, [2, 2, 2])
        self.assertIn("無くなった", msg)

    def test_timeout(self):
        s = FakeSys(self.tree())
        s.table[250] = (1, ["luajit", "tests/run.lua"])
        code, msg = W.wait_proc("luajit", 5, 2, me=300, sys_=s)
        self.assertEqual(code, W.EXIT_TIMEOUT)
        self.assertEqual(s.sleeps, [2, 2, 1])   # 上限を越えて眠らない
        self.assertIn("時間切れ", msg)
        self.assertIn("pid 250", msg)
        self.assertNotIn("\n", msg)

    def test_regex_like_pgrep(self):
        s = FakeSys({250: (1, ["./TransportFever3", "-x"])})
        self.assertEqual(len(W.matching(re.compile(r"^\./TransportFever3"), 300, s)), 1)
        self.assertEqual(W.matching(re.compile(r"^TransportFever3"), 300, s), [])
        with self.assertRaises(ValueError):
            W.wait_proc("x(", 5, 1, me=300, sys_=s)

    def test_unreadable_processes_are_skipped(self):
        s = FakeSys({250: (1, None), 251: (1, [])})
        self.assertEqual(W.matching(re.compile(""), 300, s), [])

    def test_ancestor_loop_stops(self):
        # 親の記録が循環していても止まる
        s = FakeSys({300: (301, WAITER), 301: (300, SHELL)})
        self.assertEqual(W.ancestors(300, s.ppid), {300, 301})


class File(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "out.log"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_then_appears(self):
        def write(fs):
            if fs.now >= 4:
                self.path.write_text('{"x":1}\n{"stop_reason":"end_turn"}\n')
        s = FakeSys({}, steps=[write] * 10)
        code, msg = W.wait_file(str(self.path), '"stop_reason":"end_turn"', 60, 2, sys_=s)
        self.assertEqual(code, W.EXIT_DONE, msg)
        self.assertEqual(s.sleeps, [2, 2])

    def test_already_there(self):
        self.path.write_text("done\n")
        code, msg = W.wait_file(str(self.path), "done", 60, 2, sys_=FakeSys({}))
        self.assertEqual((code, msg), (W.EXIT_DONE, "もう現れている"))

    def test_timeout_missing_and_present(self):
        code, msg = W.wait_file(str(self.path), "x", 3, 2, sys_=FakeSys({}))
        self.assertEqual(code, W.EXIT_TIMEOUT)
        self.assertIn("ファイルがまだ無い", msg)
        self.path.write_text("abc")
        code, msg = W.wait_file(str(self.path), "x", 3, 2, sys_=FakeSys({}))
        self.assertIn("文字列がまだ現れない", msg)

    def test_needle_split_across_reads(self):
        # 前の読みと次の読みの境目で切れた文字列も見つける（続きから読む）
        r = W._Reader(str(self.path), "end_turn")
        self.path.write_text("aaa end_t")
        self.assertFalse(r.found())
        with open(self.path, "a") as f:
            f.write("urn bbb")
        self.assertTrue(r.found())

    def test_truncated_or_replaced_file_is_read_again(self):
        r = W._Reader(str(self.path), "OK")
        self.path.write_text("x" * 100)
        self.assertFalse(r.found())
        self.path.write_text("OK")              # 縮んだ → 頭から
        self.assertTrue(r.found())

    def test_utf8_needle_and_empty(self):
        self.path.write_text("完了しました\n", encoding="utf-8")
        self.assertEqual(W.wait_file(str(self.path), "完了", 1, 1, sys_=FakeSys({}))[0], W.EXIT_DONE)
        with self.assertRaises(ValueError):
            W.wait_file(str(self.path), "", 1, 1, sys_=FakeSys({}))

    def test_directory_is_an_error(self):
        with self.assertRaises(ValueError):
            W.wait_file(self.tmp.name, "x", 1, 1, sys_=FakeSys({}))


class Cli(unittest.TestCase):
    """本物のプロセスで: 呼んだシェルのコマンド行にパターンがあっても、自分に一致して待ち続けない。"""

    def run_cli(self, script, timeout=30):
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=timeout,
                              env=dict(os.environ, CLAUDE_KITTY_TEST="1"))

    def test_does_not_match_calling_shell(self):
        pat = f"waitfor-selftest-{os.getpid()}-{time.time_ns()}"
        # pipe でサブシェルも fork させる（祖先と同じコマンド行の子）
        r = self.run_cli(f"{{ {BIN} proc '{pat}' --timeout 10s --interval 0.2; echo rc=$?; }} | cat; : {pat}")
        self.assertIn("rc=0", r.stdout, r.stderr)

    def test_waits_for_a_real_process(self):
        pat = f"waitfor-sleeper-{os.getpid()}-{time.time_ns()}"
        p = subprocess.Popen(["bash", "-c", f"sleep 1.5; : {pat}"])
        try:
            t = time.monotonic()
            r = self.run_cli(f"{BIN} proc '{pat}' --timeout 20s --interval 0.2")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertGreater(time.monotonic() - t, 0.5)
        finally:
            p.kill()
            p.wait()

    def test_timeout_code_and_one_line(self):
        pat = f"waitfor-stay-{os.getpid()}-{time.time_ns()}"
        p = subprocess.Popen(["bash", "-c", f"sleep 30; : {pat}"])
        try:
            r = self.run_cli(f"{BIN} proc '{pat}' --timeout 1s --interval 0.2")
            self.assertEqual(r.returncode, 124)
            self.assertEqual(len(r.stderr.strip().splitlines()), 1)
            self.assertIn("時間切れ", r.stderr)
        finally:
            p.kill()
            p.wait()

    def test_file_and_usage_errors(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "a.log"
            r = self.run_cli(f"(sleep 0.5; echo READY >> {f}) & {BIN} file {f} READY --timeout 10s --interval 0.2")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("現れた", r.stdout)
        for args in ("proc x --timeout 0", "proc 'x('", "bogus", "proc x --interval 0", "file /tmp/x ''"):
            r = self.run_cli(f"{BIN} {args}")
            self.assertEqual(r.returncode, 2, args)
            self.assertEqual(len(r.stderr.strip().splitlines()), 1, args)

    def test_version(self):
        r = subprocess.run([str(BIN), "--version"], capture_output=True, text=True)
        self.assertTrue(r.stdout.startswith("claude-waitfor "))


class FakeProcs:
    """bgshell の Sys の偽物。table: {pid: (ppid, argv, 経過の秒)}。"""

    def __init__(self, table):
        self.table = table

    def pids(self):
        return list(self.table)

    def ppid(self, pid):
        return self.table[pid][0]

    def argv(self, pid):
        return self.table[pid][1]

    def age(self, pid):
        return self.table[pid][2]


CC_WRAP = ("source /home/u/.claude/shell-snapshots/snapshot-bash-1.sh 2>/dev/null || true && "
           "\\builtin shopt -u extglob 2>/dev/null || true && eval 'cd ~/x && until ! pgrep -f '\"'\"'a b'\"'\"'"
           " >/dev/null; do sleep 3; done; tail b.log' < /dev/null && pwd -P >| /tmp/claude-1-cwd")


class BgShell(unittest.TestCase):
    def test_long_child_shells_only(self):
        t = {
            10: (1, ["claude"], 99999),
            11: (10, ["/bin/bash", "-c", CC_WRAP], 3 * 3600 + 120),       # 長い背景のシェル
            12: (10, ["/bin/bash", "-c", "sleep 1"], 60),                  # 短い
            13: (10, ["node", "mcp-server.js"], 99999),                     # シェルでない
            14: (11, ["/bin/bash", "-c", "x"], 99999),                      # 孫（直下の子でない）
            15: (10, ["/bin/bash"], 99999),                                 # -c でない
            16: (10, ["/bin/zsh", "-lc", "while :; do :; done"], 2 * 86400 + 3600),
            17: (10, ["/bin/bash", "-c", "y"], None),                       # 経過が読めない
        }
        got = B.long_shells(10, sys_=FakeProcs(t))
        self.assertEqual([g[0] for g in got], [16, 11])                    # 古い順
        self.assertEqual(got[1][2], "cd ~/x && until ! pgrep -f 'a b' >/dev/null; do sleep 3; done; tail b.log")
        self.assertEqual(B.line(*got[1]), "背景のシェル pid 11（3時間2分）: " + got[1][2])
        self.assertIn("（2日1時間）", B.brief(*got[0]))
        self.assertEqual(B.long_shells(None, sys_=FakeProcs(t)), [])
        self.assertEqual(B.long_shells(99, sys_=FakeProcs(t)), [])

    def test_head_is_one_line_and_short(self):
        self.assertEqual(B.head("a\n  b\tc"), "a b c")
        self.assertEqual(len(B.head("x" * 500)), 80)
        self.assertTrue(B.head("x" * 500).endswith("…"))

    def test_script_of(self):
        self.assertIsNone(B.script_of(None))
        self.assertIsNone(B.script_of([]))
        self.assertIsNone(B.script_of(["python3", "-c", "x"]))
        self.assertEqual(B.script_of(["-bash", "-c", "x"]), "x")
        self.assertEqual(B.script_of(["bash", "-c"]), "")

    def test_child_map_ignores_unknown_parents(self):
        t = {1: (None, [], 0), 2: (1, [], 0), 3: (3, [], 0)}
        self.assertEqual(B.child_map(FakeProcs(t)), {1: [2]})

    def test_real_age(self):
        import proc
        if not proc.is_linux():
            self.skipTest("Linux だけ")
        p = subprocess.Popen(["sleep", "5"])
        try:
            a = proc.age(p.pid)
            self.assertIsNotNone(a)
            self.assertLess(a, 5)
        finally:
            p.kill()
            p.wait()
        self.assertIsNone(proc.age(-1))


if __name__ == "__main__":
    unittest.main()
