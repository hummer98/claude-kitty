#!/usr/bin/env python3
"""macOS で動かすための分岐（Linux の上で、sys.platform と ps / sysctl / CoreGraphics / launchctl を偽物にして確認する）。

本物の KWin・kitty・Chrome・flatpak・launchctl・systemctl は呼ばない（PATH は偽物と要るものだけ）。
画面・フォーカス・窓を動かすコマンドも呼ばない。
"""
import ast
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import plistlib
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tools"))
import browser as B  # noqa: E402
import doctor as D  # noqa: E402
import guard as G  # noqa: E402
import kstate  # noqa: E402
import macwin  # noqa: E402
import merge_hooks as M  # noqa: E402
import proc  # noqa: E402
import rundir  # noqa: E402
from core import choose_key, profile_processes  # noqa: E402


def load_bin(name, modname=None):
    path = ROOT / "bin" / name
    loader = importlib.machinery.SourceFileLoader(modname or name.replace("-", "_"), str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(mod)
    return mod


def load_watcher():
    loader = importlib.machinery.SourceFileLoader("watcher_for_mac_test", str(ROOT / "kitty/claude_state_watcher.py"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(mod)
    return mod


@contextlib.contextmanager
def platform(mod, name):
    old = mod.PLATFORM
    mod.PLATFORM = name
    try:
        yield
    finally:
        mod.PLATFORM = old


def procargs2(argv, env, exec_path="/usr/bin/x"):
    """KERN_PROCARGS2 の形の bytes を作る（argc、実行ファイル、詰め物の NUL、引数、環境、空）。"""
    body = exec_path.encode() + b"\0\0\0\0"
    body += b"".join(a.encode() + b"\0" for a in argv)
    body += b"".join(f"{k}={v}".encode() + b"\0" for k, v in env.items()) + b"\0"
    return struct.pack("=i", len(argv)) + body


def bsdinfo(ppid, sec, usec):
    buf = bytearray(proc.BSDINFO_SIZE)
    struct.pack_into("=I", buf, 16, ppid)
    struct.pack_into("=QQ", buf, 120, sec, usec)
    return bytes(buf)


class FakeDarwin:
    """lib/proc.py の ctypes の層の偽物。None を返すものは「ctypes が使えない」。"""

    def __init__(self, procs=None, info=None, pids=None):
        self.procs, self.info, self._pids = procs or {}, info or {}, pids

    def procargs(self, pid):
        return self.procs.get(pid)

    def bsdinfo(self, pid):
        return self.info.get(pid)

    def pids(self):
        return self._pids


@contextlib.contextmanager
def darwin(fake, ps=None):
    """proc を macOS のつもりにする。ps は {(引数のタプル): (rc, stdout)} か関数。"""
    old = proc.PLATFORM, proc._darwin, proc.run
    calls = []

    def run(argv, timeout=5):
        calls.append(list(argv))
        if callable(ps):
            return ps(argv)
        return (ps or {}).get(tuple(argv), (1, ""))
    proc.PLATFORM, proc._darwin, proc.run = "darwin", fake, run
    try:
        yield calls
    finally:
        proc.PLATFORM, proc._darwin, proc.run = old


# ---- lib/proc.py --------------------------------------------------------------

class Proc(unittest.TestCase):
    def test_parse_procargs2(self):
        buf = procargs2(["/usr/local/bin/node", "/x/claude", "--resume", "a b"], {"CLAUDECODE": "1", "HOME": "/Users/u"})
        argv, env = proc.parse_procargs2(buf)
        self.assertEqual(argv, ["/usr/local/bin/node", "/x/claude", "--resume", "a b"])  # 空白を含む引数も切れない
        self.assertEqual(env, {"CLAUDECODE": "1", "HOME": "/Users/u"})
        self.assertIsNone(proc.parse_procargs2(b""))
        self.assertIsNone(proc.parse_procargs2(b"\x05\x00\x00\x00/x\x00a\x00"))  # argc より引数が少ない
        self.assertIsNone(proc.parse_procargs2(None))

    def test_parse_bsdinfo(self):
        self.assertEqual(proc.parse_bsdinfo(bsdinfo(77, 1700000000, 42)), (77, "1700000000.000042"))
        self.assertIsNone(proc.parse_bsdinfo(b"\0" * 10))

    def test_linux_reads_proc(self):
        if not os.path.isdir("/proc/self"):
            self.skipTest("/proc が無い")
        with platform(proc, "linux"):
            me = os.getpid()
            self.assertTrue(proc.argv(me))
            self.assertEqual(proc.ppid(me), os.getppid())
            self.assertIsNotNone(proc.start_time(me))
            self.assertEqual(proc.environ(me).get("PATH"), os.environ.get("PATH"))
            self.assertIn(me, proc.pids())
            self.assertIsNone(proc.argv(2 ** 30))
            self.assertIsNone(proc.start_time(-1))

    def test_darwin_uses_sysctl_and_proc_pidinfo(self):
        fake = FakeDarwin(procs={10: procargs2(["claude", "--resume", "x"], {"CLAUDECODE": "1"})},
                          info={10: bsdinfo(5, 1700000000, 7)}, pids=[1, 10])
        with darwin(fake) as calls:
            self.assertEqual(proc.argv(10), ["claude", "--resume", "x"])
            self.assertEqual(proc.environ(10), {"CLAUDECODE": "1"})
            self.assertEqual(proc.ppid(10), 5)
            self.assertEqual(proc.start_time(10), "1700000000.000007")
            self.assertEqual(proc.pids(), [1, 10])
            self.assertEqual(proc.processes(), [(10, ["claude", "--resume", "x"])])  # 1 は読めないので飛ばす
        self.assertEqual(calls, [])  # ctypes で読めれば ps は呼ばない（読めないプロセスごとに ps を起こさない）

    def test_darwin_processes_without_ctypes_calls_ps_once(self):
        ps = {("ps", "-A", "-ww", "-o", "pid=,args="): (0, "  1 /sbin/launchd\n 10 /x/Google Chrome --user-data-dir=/p\n")}
        with darwin(FakeDarwin(), ps) as calls:
            self.assertEqual(proc.processes(), [(1, ["/sbin/launchd"]), (10, ["/x/Google", "Chrome", "--user-data-dir=/p"])])
        self.assertEqual(len(calls), 1)

    def test_darwin_falls_back_to_ps(self):
        ps = {("ps", "-ww", "-o", "args=", "-p", "10"): (0, "/bin/zsh -l\n"),
              ("ps", "-ww", "-o", "ppid=", "-p", "10"): (0, "  5\n"),
              ("ps", "-ww", "-o", "lstart=", "-p", "10"): (0, "Thu Nov 20 10:00:00 2026\n"),
              ("ps", "-A", "-o", "pid="): (0, "  1\n 10\n")}
        with darwin(FakeDarwin(), ps):
            self.assertEqual(proc.argv(10), ["/bin/zsh", "-l"])
            self.assertEqual(proc.ppid(10), 5)
            self.assertEqual(proc.start_time(10), "Thu Nov 20 10:00:00 2026")
            self.assertEqual(proc.pids(), [1, 10])
            self.assertIsNone(proc.environ(10))      # ps では起動時の環境は読まない（入れ子でないとみなす）
            self.assertIsNone(proc.argv(11))         # ps が何も返さない = いない
            self.assertIsNone(proc.start_time(11))

    def test_proc_start_is_compared_only_on_linux(self):
        with platform(proc, "linux"):
            self.assertTrue(proc.proc_start_matches("123", "123"))
            self.assertFalse(proc.proc_start_matches(123, "124"))
            self.assertTrue(proc.proc_start_matches(None, "124"))
        with platform(proc, "darwin"):
            # Claude Code が macOS で何を書くか分からないので比べない（待つ側に倒れる）
            self.assertTrue(proc.proc_start_matches("Thu Nov 20", "1700000000.000007"))
            self.assertTrue(proc.proc_start_matches(17000, "1700000000.000007"))

    def test_alive(self):
        self.assertTrue(proc.alive(os.getpid()))
        p = subprocess.Popen(["true"])
        p.wait()
        self.assertFalse(proc.alive(p.pid))
        for bad in (0, -1, None, "12", True, 2 ** 40):
            self.assertFalse(proc.alive(bad), bad)

    def test_kstate_alive_on_darwin(self):
        me = os.getpid()
        fake = FakeDarwin(procs={me: procargs2(["claude"], {})})
        with darwin(fake):
            self.assertTrue(kstate.alive({"pid": me}))
        with darwin(FakeDarwin(procs={me: procargs2(["zsh"], {})})):
            self.assertFalse(kstate.alive({"pid": me}))   # 別のプロセス（pid の再利用）
        with darwin(FakeDarwin()):
            self.assertTrue(kstate.alive({"pid": me}))    # 引数が読めなくても、生きていれば残す
        p = subprocess.Popen(["true"])
        p.wait()
        with darwin(FakeDarwin()):
            self.assertFalse(kstate.alive({"pid": p.pid}))

    def test_claude_state_ancestors_on_darwin(self):
        from unittest import mock
        cs = load_bin("claude-state", "claude_state_for_mac")
        me = os.getpid()
        parent = 4242                       # コンテナの中では本物の親が 1 のことがあるので、親の pid も決める
        fake = FakeDarwin(procs={parent: procargs2(["node", "/x/claude-code/cli.js"], {"CLAUDECODE": "1"}),
                                 50: procargs2(["zsh"], {})},
                          info={parent: bsdinfo(50, 1, 0), 50: bsdinfo(1, 1, 0)})
        with darwin(fake), mock.patch("os.getppid", return_value=parent):
            anc = cs.ancestors()
            self.assertEqual(anc, [(parent, ["node", "/x/claude-code/cli.js"]), (50, ["zsh"])])
            self.assertEqual(cs.read_environ(parent), {"CLAUDECODE": "1"})
        del me


# ---- lib/rundir.py と kitty の watcher ----------------------------------------

class RunDir(unittest.TestCase):
    def test_runtime_dir(self):
        self.assertEqual(rundir.runtime_dir({"XDG_RUNTIME_DIR": "/run/user/5"}), Path("/run/user/5"))
        # 無ければ /tmp/claude-kitty-<uid>（/tmp はほかのユーザーと共有なので uid で分ける）
        self.assertEqual(rundir.runtime_dir({}, create=False), Path(f"/tmp/claude-kitty-{os.getuid()}"))
        self.assertEqual(rundir.runtime_dir({"XDG_RUNTIME_DIR": ""}, create=False), rundir.fallback_dir())

    def test_fallback_is_created_private(self):
        with tempfile.TemporaryDirectory() as t:
            old = rundir.fallback_dir
            rundir.fallback_dir = lambda uid=None: Path(t) / "ck"
            try:
                d = rundir.runtime_dir({})
            finally:
                rundir.fallback_dir = old
            self.assertEqual(stat.S_IMODE(d.stat().st_mode) & 0o077, 0)

    def test_fallback_owned_by_someone_else_is_not_used(self):
        with tempfile.TemporaryDirectory() as t:
            target = Path(t) / "elsewhere"
            target.mkdir()
            (Path(t) / "ck").symlink_to(target)          # ほかの人が先にリンクを置いた
            old = rundir.fallback_dir
            rundir.fallback_dir = lambda uid=None: Path(t) / "ck"
            try:
                d = rundir.runtime_dir({})
            finally:
                rundir.fallback_dir = old
            self.assertEqual(d, Path(t) / "ck-own")
            self.assertFalse(d.is_symlink())
            self.assertTrue(rundir.owned_private(d))
            loose = Path(t) / "loose"
            loose.mkdir()
            loose.chmod(0o777)
            self.assertFalse(rundir.owned_private(loose))

    def test_socket_dirs(self):
        self.assertEqual(rundir.socket_dirs({"XDG_RUNTIME_DIR": "/run/user/5"}, "linux"), ["/run/user/5"])
        self.assertEqual(rundir.socket_dirs({}, "linux"), ["/tmp"])
        # macOS: kitty/claude-kitty-macos.conf は /tmp に置く。XDG_RUNTIME_DIR を決めている人のために両方見る
        self.assertEqual(rundir.socket_dirs({"XDG_RUNTIME_DIR": "/x"}, "darwin"), ["/x", "/tmp"])
        self.assertEqual(rundir.socket_dirs({}, "darwin"), ["/tmp"])

    def test_live_kitty_sockets(self):
        with tempfile.TemporaryDirectory() as t:
            for n in ("kitty-11", "kitty-22", "kitty-x", "other"):
                Path(t, n).write_text("")
            got = rundir.live_kitty_sockets({"XDG_RUNTIME_DIR": t}, "linux", alive=lambda pid: pid == 11)
            self.assertEqual(got, [f"unix:{t}/kitty-11"])

    def test_watcher_uses_the_same_state_dir(self):
        w = load_watcher()
        self.assertEqual(w.state_dir({"XDG_RUNTIME_DIR": "/r"}), kstate.state_dir({"XDG_RUNTIME_DIR": "/r"}))
        self.assertEqual(w.state_dir({}), rundir.fallback_dir() / "claude-state")

    def test_watcher_alive_without_proc(self):
        w = load_watcher()
        p = subprocess.Popen(["true"])
        p.wait()
        self.assertTrue(w._alive({"pid": os.getpid()}))
        self.assertFalse(w._alive({"pid": p.pid}))
        self.assertTrue(w._alive({}))          # pid が分からない記録は残す
        self.assertTrue(w._alive({"pid": "x"}))

    def test_no_proc_paths_left(self):
        # /proc を直接読むのは lib/proc.py だけ（ほかは lib/proc.py を通す）
        hits = []
        for p in list((ROOT / "bin").iterdir()) + list((ROOT / "lib").glob("*.py")) + [ROOT / "kitty/claude_state_watcher.py"]:
            if p.is_file() and p.name != "proc.py":
                text = p.read_text(errors="replace")
                if '"/proc' in text or "f\"/proc" in text or "Path(\"/proc\")" in text:
                    hits.append(p.name)
        self.assertEqual(hits, [])


# ---- Python 3.9 ---------------------------------------------------------------

class Python39(unittest.TestCase):
    def test_all_python_parses_as_39(self):
        # macOS の /usr/bin/python3（Command Line Tools）は 3.9。構文だけは 3.9 で読めること
        bad = []
        files = [p for d in ("bin", "lib", "tools", "kitty", "tests") for p in (ROOT / d).iterdir() if p.is_file()]
        for p in files:
            text = p.read_text(errors="replace")
            if not (p.suffix == ".py" or text.startswith("#!/usr/bin/env python3")):
                continue
            try:
                ast.parse(text, str(p), feature_version=(3, 9))
            except SyntaxError as e:
                bad.append(f"{p.name}: {e}")
        self.assertEqual(bad, [])

    def test_install_and_doctor_say_39(self):
        self.assertEqual(D.MIN_PYTHON, (3, 9))
        self.assertIn("(3, 9)", (ROOT / "tools/install.sh").read_text())


# ---- macOS の窓（lib/macwin.py） ------------------------------------------------

WINS = [{"number": 31, "pid": 500, "layer": 0, "x": 0.0, "y": 25.0, "w": 1440.0, "h": 875.0},
        {"number": 32, "pid": 500, "layer": 0, "x": 100.0, "y": 100.0, "w": 800.0, "h": 600.0},
        {"number": 33, "pid": 500, "layer": 25, "x": 0.0, "y": 0.0, "w": 30.0, "h": 30.0},
        {"number": 40, "pid": 600, "layer": 0, "x": 0.0, "y": 0.0, "w": 500.0, "h": 500.0}]


class MacWin(unittest.TestCase):
    def test_pick_by_platform_window_id(self):
        self.assertEqual(macwin.pick_window(WINS, 500, 32)["number"], 32)
        self.assertIsNone(macwin.pick_window(WINS, 500, None))          # 候補が 2 つ: 決めない
        self.assertIsNone(macwin.pick_window(WINS, 500, 40))            # 番号が別の pid の窓
        self.assertEqual(macwin.pick_window(WINS, 600, None)["number"], 40)
        self.assertIsNone(macwin.pick_window(WINS, 700, None))

    def test_titlebar(self):
        self.assertEqual(macwin.titlebar_height({}, []), macwin.TITLEBAR)
        self.assertEqual(macwin.titlebar_height({}, [("hide_window_decorations", "titlebar-only")]), 0)
        self.assertEqual(macwin.titlebar_height({}, [("hide_window_decorations", "yes"),
                                                     ("hide_window_decorations", "no")]), macwin.TITLEBAR)  # 最後が効く
        self.assertEqual(macwin.titlebar_height({"CLAUDE_BROWSER_MAC_TITLEBAR": "32"}, []), 32)
        self.assertEqual(macwin.titlebar_height({"CLAUDE_BROWSER_MAC_TITLEBAR": "x"}, []), macwin.TITLEBAR)

    def test_client_rect(self):
        self.assertEqual(macwin.client_rect(WINS[0], 28), (0, 53, 1440, 847))
        self.assertEqual(macwin.client_rect(WINS[0], 0), (0, 25, 1440, 875))
        self.assertEqual(macwin.client_rect({"x": 0, "y": 0, "w": 10, "h": 5}, 28), (0, 4, 10, 1))  # 高さを 0 にしない

    def test_kitty_client(self):
        self.assertEqual(macwin.kitty_client(500, 31, {}, wins=WINS, conf_values=[]), (0, 53, 1440, 847))
        with self.assertRaises(macwin.MacWinError):
            macwin.kitty_client(500, None, {}, wins=WINS, conf_values=[])

    def test_kitty_conf_values_follow_include(self):
        with tempfile.TemporaryDirectory() as t:
            Path(t, "kitty.conf").write_text("include extra.conf\n")
            Path(t, "extra.conf").write_text("hide_window_decorations titlebar-only\n")
            vals = macwin.kitty_conf_values({"KITTY_CONFIG_DIRECTORY": t})
            self.assertEqual(macwin.titlebar_height({}, vals), 0)


# ---- claude-browser の macOS の分岐 ----------------------------------------------

TAB = {"layout_state": {"class": "Splits", "pairs": {"horizontal": True, "bias": 0.25, "one": 10,
                                                     "two": {"horizontal": False, "one": 20, "two": 30}}},
       "groups": [{"id": 10, "windows": [1]}, {"id": 20, "windows": [2]}, {"id": 30, "windows": [3]}],
       "windows": [{"id": 1, "user_vars": {"role": "main"}}, {"id": 2}, {"id": 3}], "is_active": True}


class BrowserMac(unittest.TestCase):
    def setUp(self):
        self.b = load_bin("claude-browser", "claude_browser_for_mac")
        self.b.PLATFORM = "darwin"
        self.calls = []
        self.answers = {}

        def node(port, *args, check=True):
            self.calls.append((port,) + args)
            rc, out = self.answers.get(args[0], (0, ""))
            return SimpleNamespace(returncode=rc, stdout=out, stderr="boom" if rc else "")
        self.b.node = node
        self.b.left_window_id = lambda tab: 1
        self.b.own_window_id = lambda: 2

    def opts(self, **kw):
        o = {"mode": "self", "ratio": 0.65, "over": None, "area": "pane", "focus": "kitty"}
        o.update(kw)
        return o

    def place(self, kitty_win=(None, (0, 53, 1440, 847)), **kw):
        st = {"port": 9222, "class": "c"}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rect = self.b.placement(st, self.opts(**kw), kitty_win, TAB, {"id": 2})
        return rect, st, err.getvalue()

    def test_places_with_cdp_and_keeps_focus(self):
        self.answers = {"window": (0, "7\n"), "place": (0, json.dumps([360, 53, 1080, 424]))}
        rect, st, err = self.place()
        self.assertEqual(rect, (360, 53, 1080, 424))       # ペイン 2 = 右上（左 1/4 を除いた上半分）
        # 既定（focus kitty）は入力先を動かさない（Page.bringToFront を呼ばない）
        self.assertEqual(self.calls, [(9222, "window"), (9222, "place", "7", "360", "53", "1080", "424")])
        self.assertEqual((st["mode"], st["area"], st["kwin_id"]), ("self", "pane", None))
        self.assertIn("後ろに隠れて", err)

    def test_only_focus_browser_brings_to_front(self):
        self.answers = {"window": (0, "7\n"), "place": (0, json.dumps([360, 53, 1080, 424]))}
        self.place(focus="none")
        self.assertNotIn((9222, "front"), self.calls)
        self.place(focus="browser")
        self.assertEqual(self.calls[-1], (9222, "front"))

    def test_failures_do_not_stop(self):
        # kitty の窓が読めない・窓の ID が取れない・置けない: どれも止めずに普通の窓のまま（rect は None）
        rect, st, err = self.place(kitty_win=None)
        self.assertIsNone(rect)
        self.assertEqual(self.calls, [])
        self.assertEqual(st["mode"], "self")
        self.answers = {"window": (1, "")}
        rect, _st, err = self.place()
        self.assertIsNone(rect)
        self.assertIn("普通の窓", err)
        self.answers = {"window": (0, "7"), "place": (1, "")}
        rect, _st, err = self.place()
        self.assertIsNone(rect)
        self.assertIn("置けなかった", err)

    def test_off_by_a_lot_is_reported(self):
        self.answers = {"window": (0, "7"), "place": (0, json.dumps([0, 0, 500, 500]))}
        _rect, _st, err = self.place()
        self.assertIn("収まらない", err)

    def test_find_kitty_mac_failure_is_a_warning(self):
        old = macwin.window_list
        macwin.window_list = lambda: (_ for _ in ()).throw(macwin.MacWinError("CoreGraphics を読めない"))
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertIsNone(self.b.find_kitty_mac(500, {"platform_window_id": 31}))
            self.assertIn("ペインに重ねずに", err.getvalue())
            macwin.window_list = lambda: WINS
            got = self.b.find_kitty_mac(500, {"platform_window_id": 31})
            self.assertEqual(got[1][0::2], (0, 1440))
        finally:
            macwin.window_list = old

    def test_prepare_on_mac_does_not_call_kwin(self):
        self.b.kitty_socket = lambda: "unix:/tmp/kitty-500"
        self.b.kitty_ls = lambda sock: [{"platform_window_id": 31, "tabs": [TAB]}]
        self.b.find_kitty = lambda *a: self.fail("KWin を呼んだ")
        self.b.find_kitty_mac = lambda pid, osw: (None, (pid, osw["platform_window_id"]))
        args = SimpleNamespace(mode=None, ratio=None, over=None, focus=None, area=None)
        opts, kitty_win, tab, pane = self.b.prepare(args, {})
        self.assertEqual(kitty_win, (None, (500, 31)))
        self.assertEqual(pane["id"], 2)

    def test_window_wait_uses_cdp(self):
        self.b.browser_windows = lambda cls: self.fail("KWin を呼んだ")
        self.answers = {"window": (0, "7")}
        self.assertTrue(self.b.has_window("c", 9222))
        self.answers = {"window": (1, "")}
        self.assertFalse(self.b.has_window("c", 9222))

    def test_scan_procs_uses_proc_module(self):
        profile = "/Users/u/Library/Application Support/claude-kitty/browser-profiles/role-x"
        argv = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", f"--user-data-dir={profile}",
                "--remote-debugging-port=9333"]
        with darwin(FakeDarwin(procs={41: procargs2(argv, {})}, pids=[41])):
            self.assertEqual(self.b.running(profile), ([41], 9333))


class BrowserProfiles(unittest.TestCase):
    def test_mac_profile_root_and_apps(self):
        home = Path("/Users/u")
        conf = {"command": None, "profile_root": None}
        b = B.find_browser(conf, {}, home, lambda n: None, lambda p: p == B.MAC_CHROME, lambda a: False, "Darwin")
        self.assertEqual(b["command"], [B.MAC_CHROME])
        self.assertEqual(b["profile_root"], home / "Library/Application Support/claude-kitty/browser-profiles")
        # ~/Applications の Chrome も、Chromium.app も
        user_app = str(home / B.MAC_CHROME.lstrip("/"))
        b = B.find_browser(conf, {}, home, lambda n: None, lambda p: p == user_app, lambda a: False, "Darwin")
        self.assertEqual(b["command"], [user_app])
        b = B.find_browser(conf, {}, home, lambda n: None, lambda p: p == B.MAC_CHROMIUM, lambda a: False, "Darwin")
        self.assertEqual(b["command"], [B.MAC_CHROMIUM])
        self.assertEqual(b["profile_root"], home / "Library/Application Support/claude-kitty/chromium-profiles")
        # 見つからなくても置き場は macOS の場所。XDG_DATA_HOME を決めていればそちら
        b = B.find_browser(conf, {}, home, lambda n: None, lambda p: False, lambda a: False, "Darwin")
        self.assertIsNone(b["command"])
        self.assertEqual(b["profile_root"], home / "Library/Application Support/claude-kitty/browser-profiles")
        b = B.find_browser(conf, {"XDG_DATA_HOME": "/d"}, home, lambda n: None, lambda p: p == B.MAC_CHROME,
                           lambda a: False, "Darwin")
        self.assertEqual(b["profile_root"], Path("/d/claude-kitty/browser-profiles"))
        # Linux は前と同じ
        b = B.find_browser(conf, {}, Path("/home/u"), lambda n: None, lambda p: p == B.MAC_CHROME, lambda a: False)
        self.assertIsNone(b["command"])
        self.assertEqual(b["profile_root"], Path("/home/u/.local/share/claude-kitty/browser-profiles"))

    def test_profile_with_space_matches_split_argv(self):
        # macOS で ps の空白区切りでしか読めなかったとき（Application Support の空白で切れている）
        root = "/Users/u/Library/Application Support/claude-kitty/browser-profiles"
        split = f"/x/Chrome --user-data-dir={root}/role-x --remote-debugging-port=9333".split()
        self.assertEqual(profile_processes([(5, split)], f"{root}/role-x", loose=True), [(5, 9333)])
        self.assertEqual(profile_processes([(5, split)], f"{root}/role", loose=True), [])
        self.assertEqual(profile_processes([(5, split)], f"{root}/role-x"), [])          # Linux は緩く探さない   # 名前の頭が同じだけの別のプロファイル
        self.assertEqual(profile_processes([(6, [f"--user-data-dir={root}/role-x"])], f"{root}/role-x"), [(6, None)])

    def test_linux_does_not_count_a_shell_as_chrome(self):
        # 1 つの引数の中に --user-data-dir=… を含むシェルを Chrome と数えない（close で SIGTERM を送らないように）
        shell = ["bash", "-c", "google-chrome --user-data-dir=/p/role-x --remote-debugging-port=1 x"]
        self.assertEqual(profile_processes([(9, shell)], "/p/role-x"), [])
        b = load_bin("claude-browser", "claude_browser_for_linux")
        old = proc.PLATFORM, proc.processes
        proc.PLATFORM, proc.processes = "linux", lambda: [(9, shell)]
        try:
            self.assertEqual(b.running("/p/role-x"), ([], None))
        finally:
            proc.PLATFORM, proc.processes = old

    def test_key_error_is_japanese(self):
        with self.assertRaises(ValueError) as e:
            choose_key(None, {}, {})
        self.assertIn("プロファイルの名前を決められない", str(e.exception))
        self.assertIn("--key", str(e.exception))


# ---- view.guard（lib/guard.py）と claude-show ------------------------------------

def fake_run(answers):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        for key, (rc, out) in answers.items():
            if tuple(argv[:len(key)]) == key:
                return SimpleNamespace(returncode=rc, stdout=out, stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="")
    return run, calls


class GuardMac(unittest.TestCase):
    def test_front_app_by_lsappinfo(self):
        base = {("pgrep",): (0, "100\n101\n"), ("lsappinfo", "front"): (0, "ASN:0x0-0x1234:\n")}
        run, calls = fake_run({**base, ("lsappinfo", "info"): (0, '"pid"=101\n')})
        self.assertTrue(G.guarded_in_front(["Keynote"], run=run, platform="darwin"))
        self.assertIn(["lsappinfo", "info", "-only", "pid", "ASN:0x0-0x1234:"], calls)
        run, _ = fake_run({**base, ("lsappinfo", "info"): (0, '"pid"=999\n')})
        self.assertFalse(G.guarded_in_front(["Keynote"], run=run, platform="darwin"))

    def test_unknown_front_app_is_guarded(self):
        # 前面のアプリが読めなければ、当たるプロセスが動いているだけで「前面」とみなす（動かさない側）
        run, _ = fake_run({("pgrep",): (0, "100\n"), ("lsappinfo", "front"): (1, "")})
        self.assertTrue(G.guarded_in_front(["Keynote"], run=run, platform="darwin"))
        run, _ = fake_run({("pgrep",): (1, ""), ("lsappinfo", "front"): (1, "")})
        self.assertFalse(G.guarded_in_front(["Keynote"], run=run, platform="darwin"))   # 動いていなければ控えない

    def test_never_calls_kwin_on_mac(self):
        run, calls = fake_run({("pgrep",): (0, "100\n"), ("lsappinfo", "front"): (0, "ASN\n"),
                               ("lsappinfo", "info"): (0, "pid = 100\n")})
        self.assertTrue(G.guarded_in_front(["x"], run=run, platform="darwin"))
        self.assertFalse([c for c in calls if "qdbus" in c[0]])

    def test_parse_lsappinfo(self):
        self.assertEqual(G.parse_lsappinfo_pid('"pid"=1234'), 1234)
        self.assertEqual(G.parse_lsappinfo_pid("pid = 77"), 77)
        self.assertIsNone(G.parse_lsappinfo_pid(""))
        self.assertIsNone(G.parse_lsappinfo_pid(None))


class ShowMac(unittest.TestCase):
    def test_outside_kitty_uses_open_on_mac(self):
        show = load_bin("claude-show", "claude_show_for_mac")
        with tempfile.TemporaryDirectory() as t:
            f = Path(t, "a.html")
            f.write_text("<p>")
            conf = Path(t, "none.json")
            env = {"CLAUDE_KITTY_CONFIG": str(conf)}
            run, calls = fake_run({("open",): (0, ""), ("xdg-open",): (0, "")})
            show.PLATFORM = "darwin"
            code, out = show.show(str(f), env, None, run=run)
            self.assertEqual((code, out["shown"]), (0, "open"))
            self.assertEqual(calls[-1], ["open", str(f)])
            show.PLATFORM = "linux"
            code, out = show.show(str(f), env, None, run=run)
            self.assertEqual(out["shown"], "xdg-open")

            def missing(argv, **kw):
                raise FileNotFoundError(argv[0])
            show.PLATFORM = "darwin"
            code, out = show.show(str(f), env, None, run=missing)
            self.assertEqual(code, 1)
            self.assertIn("open", out["error"])


# ---- claude-wait・claude-view・claude-browser の小さな直し -------------------------

class SmallFixes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.fake = self.d / "fake"
        self.fake.mkdir()
        (self.d / "run").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def env(self, **extra):
        e = {"PATH": f"{self.fake}", "HOME": str(self.d), "XDG_RUNTIME_DIR": str(self.d / "run"),
             "CLAUDE_KITTY_CONFIG": str(self.d / "none.json"), "CLAUDE_KITTY_KITTEN": str(self.fake / "kitten"),
             "CLAUDE_SESSIONS_DIR": str(self.d / "sessions"), "LANG": "C.UTF-8"}
        e.update(extra)
        return e

    def test_wait_help_prints_usage(self):
        r = subprocess.run([sys.executable, str(ROOT / "bin/claude-wait"), "--help"], env=self.env(),
                           capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0)
        self.assertIn("usage: claude-wait", r.stdout)

    def test_wait_usage_is_not_lost_before_exec(self):
        # 引数の誤りで使い方を出したあと、ログインシェルを exec する。バッファのまま exec すると表示が消えていた
        sh = self.fake / "bash"
        sh.write_text("#!/bin/sh\necho shell-started \"$@\"\n")
        sh.chmod(0o755)
        r = subprocess.run([sys.executable, str(ROOT / "bin/claude-wait"), "only-one"],
                           env=self.env(SHELL=str(sh), PATH=f"{self.fake}:/usr/bin:/bin"),
                           capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        self.assertIn("usage: claude-wait", r.stdout)
        self.assertIn("shell-started -l", r.stdout)

    def test_wait_login_shell(self):
        w = load_bin("claude-wait", "claude_wait_for_mac")
        self.assertEqual(w.login_shell({"SHELL": "/bin/zsh"}), "/bin/zsh")    # macOS の既定のシェル
        self.assertEqual(w.login_shell({"SHELL": "/bin/bash"}), "/bin/bash")
        self.assertEqual(w.login_shell({"SHELL": "/usr/bin/fish"}), "bash")
        self.assertEqual(w.login_shell({}), "bash")

    def test_wait_start_time_on_mac_is_safe(self):
        w = load_bin("claude-wait", "claude_wait_for_mac2")
        me = os.getpid()
        with darwin(FakeDarwin()):          # 起動時刻が読めない（ctypes も ps も）
            self.assertEqual(w.start_time(me), "alive")   # 生きていれば「いる」（同じ ID を 2 か所で開かない）
            p = subprocess.Popen(["true"])
            p.wait()
            self.assertIsNone(w.start_time(p.pid))
        with darwin(FakeDarwin(info={me: bsdinfo(1, 1700000000, 5)})):
            self.assertEqual(w.start_time(me), "1700000000.000005")

    def test_wait_ignores_proc_start_on_mac(self):
        w = load_bin("claude-wait", "claude_wait_for_mac3")
        sessions = self.d / "sessions"
        sessions.mkdir()
        me = os.getpid()
        sid = "11111111-2222-3333-4444-555555555555"
        (sessions / f"{me}.json").write_text(json.dumps({"pid": me, "sessionId": sid, "procStart": "Thu Nov 20"}))
        w.SESSIONS = sessions
        with darwin(FakeDarwin(info={me: bsdinfo(1, 1700000000, 5)})):
            self.assertEqual(w.find_live(sid)[0], "live")    # procStart の形が違っても、生きている pid なら待つ
        with platform(proc, "linux"):
            self.assertEqual(w.find_live(sid)[0], "none")    # Linux は今まで通り比べる（違えば別のプロセス）

    def test_view_without_any_kitty(self):
        r = subprocess.run([sys.executable, str(ROOT / "bin/claude-view"), "status"], env=self.env(),
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 1)
        self.assertIn("kitty が見つからない", r.stderr)
        self.assertNotIn("1 つに決められない", r.stderr)

    def test_browser_outside_kitty_is_japanese(self):
        r = subprocess.run([sys.executable, str(ROOT / "bin/claude-browser"), "status"], env=self.env(),
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 1)
        self.assertIn("プロファイルの名前を決められない", r.stderr)
        self.assertNotIn("no --key", r.stderr)


# ---- claude-kitty（bin/claude-kitty）の macOS ------------------------------------

FAKE_KITTY = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_LOG/kitty.args.tmp"
mv "$FAKE_LOG/kitty.args.tmp" "$FAKE_LOG/kitty.args"
"""
FAKE_QDBUS = """#!/bin/sh
echo "$*" >> "$FAKE_LOG/qdbus.calls"
"""


class KittyLaunchMac(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log = self.root / "log"
        self.log.mkdir()
        self.fake = self.root / "fake"
        self.fake.mkdir()
        for name, body in (("kitty", FAKE_KITTY), ("qdbus", FAKE_QDBUS)):
            (self.fake / name).write_text(body)
            (self.fake / name).chmod(0o755)
        # 本物の qdbus（KWin）に届かないよう、PATH は要るコマンドへのリンクだけにする（/usr/bin を入れない）
        self.tools = self.root / "tools"
        self.tools.mkdir()
        for name in ("python3", "dirname", "readlink", "env", "grep", "sed", "sleep", "rm", "cat", "cp", "nohup",
                     "uname", "mv", "printf"):
            found = shutil.which(name)
            if found:
                (self.tools / name).symlink_to(found)
        self.cfg = self.root / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def launch(self, config=None, via=None, args=("/x/my.session",), **extra):
        if config is not None:
            self.cfg.write_text(json.dumps(config))
        env = {"PATH": f"{self.fake}:{self.tools}", "HOME": str(self.root), "XDG_RUNTIME_DIR": str(self.root),
               "CLAUDE_KITTY_KITTY": str(self.fake / "kitty"), "CLAUDE_KITTY_CONFIG": str(self.cfg),
               "FAKE_LOG": str(self.log), "CLAUDE_KITTY_UNAME": "Darwin"}
        env.update(extra)
        assert shutil.which("qdbus", path=env["PATH"]) == str(self.fake / "qdbus")
        r = subprocess.run([str(via or ROOT / "bin/claude-kitty"), *args], env=env, capture_output=True,
                           text=True, timeout=30)
        return r

    def args(self):
        f = self.log / "kitty.args"
        for _ in range(100):  # nohup で後ろで動くので、書くのを待つ
            if f.exists():
                return f.read_text().splitlines()
            time.sleep(0.05)
        self.fail("kitty が起動されない")

    def test_no_detach_and_no_kwin(self):
        r = self.launch({"kitty": {"desktop": 2, "maximize": True}})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.args(), ["--class", "kitty-claude", "--start-as", "normal", "--session", "/x/my.session"])
        self.assertIn("kitty.maximize を使わない", r.stderr)
        self.assertIn("kitty.desktop を使わない", r.stderr)
        time.sleep(0.2)
        self.assertFalse((self.log / "qdbus.calls").exists())

    def test_default_has_no_notice(self):
        r = self.launch()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("--detach", self.args())
        self.assertEqual(r.stderr, "")

    def test_runs_through_a_symlink_without_readlink_f(self):
        link_dir = self.root / "bin"
        link_dir.mkdir()
        (link_dir / "claude-kitty").symlink_to(ROOT / "bin/claude-kitty")
        r = self.launch(via=link_dir / "claude-kitty", args=("--version",))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn((ROOT / "VERSION").read_text().strip(), r.stdout)

    def test_linux_is_unchanged(self):
        r = self.launch(CLAUDE_KITTY_UNAME="Linux")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.args()[0], "--detach")


# ---- tools/install.sh --launchd -------------------------------------------------

FAKE_LAUNCHCTL = """#!/bin/sh
# 偽の launchctl: 読み込んだものを $FAKE_LD/loaded.<label> で持ち、引数を calls に残す
d=${FAKE_LD:?}; mkdir -p "$d"; echo "$*" >> "$d/calls"
case "$1" in
print) [ -e "$d/loaded.${2##*/}" ] && exit 0; exit 113 ;;
bootstrap) l=$(basename "$3" .plist); touch "$d/loaded.$l" ;;
bootout) rm -f "$d/loaded.${2##*/}" ;;
esac
"""


class InstallLaunchd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))
        self.home = self.root / "home"
        self.home.mkdir()
        fake = self.root / "fake"
        fake.mkdir()
        self.lc = fake / "launchctl"
        self.lc.write_text(FAKE_LAUNCHCTL)
        self.lc.chmod(0o755)
        self.sc = fake / "systemctl"
        self.sc.write_text("#!/bin/sh\necho \"$*\" >> \"$FAKE_LD/systemctl\"\nexit 1\n")
        self.sc.chmod(0o755)
        self.ld = self.root / "ld"
        self.agents = self.home / "Library/LaunchAgents"
        self.fake = fake

    def tearDown(self):
        self.tmp.cleanup()

    def run_install(self, *args, os_name="Darwin", **extra):
        env = {"PATH": f"{self.fake}:/usr/bin:/bin", "HOME": str(self.home), "LANG": "C.UTF-8",
               "CLAUDE_KITTY_UNAME": os_name, "CLAUDE_KITTY_LAUNCHCTL": str(self.lc),
               "CLAUDE_KITTY_SYSTEMCTL": str(self.sc), "FAKE_LD": str(self.ld), "TMPDIR": str(self.root)}
        env.update(extra)
        return subprocess.run(["sh", str(ROOT / "tools/install.sh"), *args], env=env, capture_output=True, text=True,
                              timeout=120)

    def calls(self):
        try:
            return (self.ld / "calls").read_text().splitlines()
        except FileNotFoundError:
            return []

    def test_install_again_and_uninstall(self):
        r = self.run_install("--launchd", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        labels = ["claude-kitty.state-watch", "claude-kitty.roster-save", "claude-kitty.session-stats"]
        for label in labels:
            p = self.agents / f"{label}.plist"
            data = plistlib.loads(p.read_bytes())         # 正しい plist（印の行は XML のコメント）
            self.assertEqual(data["Label"], label)
            self.assertTrue(data["ProgramArguments"][0].startswith(f"{os.path.realpath(ROOT)}/bin/"))
            path = data["EnvironmentVariables"]["PATH"].split(":")
            self.assertEqual(path[:2], [str(self.home / ".local/bin"), "/opt/homebrew/bin"])
            self.assertEqual(data["EnvironmentVariables"]["HOME"], str(self.home))
            self.assertNotIn("XDG_RUNTIME_DIR", data["EnvironmentVariables"])
            self.assertNotIn("@@", p.read_text())
        watch = plistlib.loads((self.agents / "claude-kitty.state-watch.plist").read_bytes())
        self.assertEqual(watch["ProgramArguments"][1:], ["watch"])
        self.assertTrue(watch["KeepAlive"])
        uid = os.getuid()
        self.assertIn(f"bootstrap gui/{uid} {self.agents}/claude-kitty.state-watch.plist", self.calls())
        self.assertTrue((self.home / "Library/Logs/claude-kitty").is_dir())
        self.assertFalse((self.ld / "systemctl").exists())   # macOS では systemctl を呼ばない
        # 2 回目は何も変えない
        n = len([c for c in self.calls() if not c.startswith("print")])
        r = self.run_install("--launchd", "--yes")
        self.assertIn("変えるものは無い", r.stdout)
        self.assertEqual(len([c for c in self.calls() if not c.startswith("print")]), n)
        # 外す
        r = self.run_install("--launchd", "--uninstall", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"bootout gui/{uid}/claude-kitty.state-watch", self.calls())
        self.assertEqual(list(self.agents.glob("*.plist")), [])

    def test_unloaded_agent_is_loaded_again(self):
        self.run_install("--launchd", "--yes")
        (self.ld / "loaded.claude-kitty.roster-save").unlink()
        r = self.run_install("--launchd", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("+ launchctl bootstrap", r.stdout)
        self.assertTrue((self.ld / "loaded.claude-kitty.roster-save").exists())

    def test_hand_made_agent_is_kept(self):
        self.agents.mkdir(parents=True)
        mine = self.agents / "claude-kitty.state-watch.plist"
        mine.write_text("<plist><dict/></plist>\n")
        r = self.run_install("--launchd", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(mine.read_text(), "<plist><dict/></plist>\n")
        self.assertIn("手で入れたもの", r.stdout)
        r = self.run_install("--launchd", "--uninstall", "--yes")
        self.assertEqual(mine.read_text(), "<plist><dict/></plist>\n")
        self.assertIn("手で直したもの", r.stdout)

    def test_changed_template_replaces_and_reloads(self):
        self.run_install("--launchd", "--yes")
        p = self.agents / "claude-kitty.roster-save.plist"
        p.write_text(p.read_text().replace("<integer>300</integer>", "<integer>60</integer>"))
        r = self.run_install("--launchd", "--yes")
        self.assertIn("~ ", r.stdout)
        self.assertIn("<integer>300</integer>", p.read_text())
        calls = self.calls()
        self.assertLess(calls.index(f"bootout gui/{os.getuid()}/claude-kitty.roster-save"),
                        len(calls) - 1 - calls[::-1].index(f"bootstrap gui/{os.getuid()} {p}"))

    def test_xdg_runtime_dir_is_passed(self):
        rt = self.root / "rt"
        rt.mkdir()
        self.run_install("--launchd", "--yes", XDG_RUNTIME_DIR=str(rt))
        data = plistlib.loads((self.agents / "claude-kitty.state-watch.plist").read_bytes())
        self.assertEqual(data["EnvironmentVariables"]["XDG_RUNTIME_DIR"], str(rt))

    def test_all_picks_the_os_service(self):
        r = self.run_install("--all", "--dry-run")
        self.assertIn("launchd", r.stdout)
        self.assertNotIn("## systemd", r.stdout)
        r = self.run_install("--all", "--dry-run", os_name="Linux")
        self.assertNotIn("## launchd", r.stdout)
        r = self.run_install("--launchd", "--dry-run", os_name="Linux")
        self.assertIn("Linux では launchd を使わない", r.stdout)
        self.assertTrue(all(c.startswith("print ") for c in self.calls()), self.calls())   # 見せるだけなら読むだけ

    def test_old_python_stops(self):
        real = shutil.which("python3")
        py = self.fake / "python3"
        py.write_text(f"""#!/bin/sh
case "$*" in
*"version_info >= (3, 9)"*) exit 1 ;;
*python_version*) echo 3.8.9; exit 0 ;;
esac
exec {real} "$@"
""")
        py.chmod(0o755)
        r = self.run_install("--launchd", "--dry-run")
        self.assertEqual(r.returncode, 1)
        self.assertIn("3.8.9", r.stderr)
        self.assertIn("brew install python", r.stderr)
        self.assertFalse(self.agents.exists())


# ---- hook の PATH（tools/merge_hooks.py と doctor） ---------------------------------

class HookPaths(unittest.TestCase):
    def test_bare_names_are_not_doubled(self):
        # 作者の環境の形（素の名前・~/.local/bin）で入っていれば、絶対パスの hook を足さない
        data = {"hooks": {}}
        for ev, m, tool, args in M.wanted(["state", "guard"]):
            g = {"hooks": [{"type": "command", "command": " ".join([tool] + args)}]}
            data["hooks"].setdefault(ev, []).append(dict(g, matcher=m) if m else g)
        lines, new = M.install(data, "/r", ["state", "guard"])
        self.assertEqual(new, data)
        self.assertTrue(all(line.startswith("= ") for line in lines))

    def test_new_hooks_use_absolute_paths(self):
        _lines, new = M.install({}, "/opt/claude kitty", ["state"])
        cmd = new["hooks"]["Stop"][0]["hooks"][0]["command"]
        self.assertEqual(cmd, "'/opt/claude kitty/bin/claude-state' hook Stop")
        self.assertEqual(M.program(cmd), "/opt/claude kitty/bin/claude-state")

    def test_commands_and_program(self):
        data = {"hooks": {"Stop": [{"hooks": [{"command": "~/.local/bin/claude-state hook Stop"},
                                              {"command": "FOO=1 claude-lang-check"}, {"command": "other"}]}],
                          "PreToolUse": [{"hooks": [{"command": "/r/bin/claude-guard hook"}]}]}}
        self.assertEqual(M.commands(data, "state"), ["~/.local/bin/claude-state hook Stop"])
        self.assertEqual(M.commands(data, "lang"), ["FOO=1 claude-lang-check"])
        self.assertEqual(M.program("FOO=1 claude-lang-check"), "claude-lang-check")
        self.assertIsNone(M.program("'broken"))

    def doctor_items(self, commands, which=None):
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            (home / ".claude").mkdir()
            data = {"hooks": {}}
            for ev, m, tool, args in M.wanted(["state"]):
                data["hooks"].setdefault(ev, []).append({"hooks": [{"command": commands(tool, args)}]})
            (home / ".claude/settings.json").write_text(json.dumps(data))
            (home / ".local/bin").mkdir(parents=True)
            (home / ".local/bin/claude-state").symlink_to(ROOT / "bin/claude-state")
            ctx = D.Ctx(env={"HOME": str(home), "PATH": "/nowhere"}, os_name="Linux", run=lambda a, t: (1, ""),
                        which=which or (lambda n: None), repo=ROOT)
            rep = D.Report()
            D.check_hooks(ctx, rep)
            return {i["name"]: i for i in rep.items}

    def test_doctor_ng_when_hook_tool_is_missing(self):
        name = "hook: claude-state（状態の色）"
        items = self.doctor_items(lambda tool, args: " ".join([tool] + args))     # 素の名前で、PATH に無い
        self.assertEqual(items[name]["status"], "ng")
        self.assertIn("見つからない", items[name]["detail"])
        items = self.doctor_items(lambda tool, args: " ".join([tool] + args), which=lambda n: f"/somewhere/{n}")
        self.assertEqual(items[name]["status"], "ok")                             # PATH にあればよい
        items = self.doctor_items(lambda tool, args: " ".join([f"~/.local/bin/{tool}"] + args))
        self.assertEqual(items[name]["status"], "ok")                             # ~ は HOME
        items = self.doctor_items(lambda tool, args: M.command_for(ROOT, tool, args))
        self.assertEqual(items[name]["status"], "ok")                             # リポジトリの絶対パス
        items = self.doctor_items(lambda tool, args: " ".join(["/gone/bin/" + tool] + args))
        self.assertEqual(items[name]["status"], "ng")


class SessionStatsHome(unittest.TestCase):
    def test_home_project_name_like_claude_code(self):
        import sessionstats as ss
        self.assertEqual(ss.default_home_project("/var/home/yamamoto"), "-var-home-yamamoto")   # 前と同じ
        self.assertEqual(ss.default_home_project("/Users/first.last"), "-Users-first-last")    # macOS の名前の点


class DoctorLaunchd(unittest.TestCase):
    def test_loaded_and_not_loaded(self):
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            agents = home / "Library/LaunchAgents"
            agents.mkdir(parents=True)
            for label in D.AGENTS:
                (agents / f"{label}.plist").write_text("<plist/>")
            calls = []

            def run(argv, timeout):
                calls.append(argv)
                return (0, "") if argv[-1].endswith("state-watch") else (113, "")
            ctx = D.Ctx(env={"HOME": str(home), "PATH": "/x"}, os_name="Darwin", run=run,
                        which=lambda n: "/bin/launchctl" if n == "launchctl" else None, repo=ROOT)
            rep = D.Report()
            D.check_systemd(ctx, rep)
            items = {i["name"]: i for i in rep.items}
            self.assertEqual(items["launchd: claude-kitty.state-watch"]["status"], "ok")
            self.assertEqual(items["launchd: claude-kitty.roster-save"]["status"], "ng")
            self.assertIn("launchctl bootstrap", items["launchd: claude-kitty.roster-save"]["fix"])
            self.assertTrue(all(c[:2] == ["/bin/launchctl", "print"] for c in calls))   # 読むだけ


if __name__ == "__main__":
    unittest.main()
