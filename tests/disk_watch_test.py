#!/usr/bin/env python3
"""ディスクの見張り（lib/diskwatch.py・config の disk_watch・claude-state watch・doctor）の単体テスト。

statvfs・送り先・通知は偽物。tests/run.py から読み込まれる（クラス名の頭に Disk が付く）。
"""
import importlib.machinery
import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tools"))
import diskwatch as DW  # noqa: E402
from config import DEFAULT_DISK_WATCH, load_disk_watch_config  # noqa: E402


def vfs(bytes_pct=10.0, inode_pct=None, files=1048576, blocks=1000):
    """容量 bytes_pct %・inode inode_pct % の statvfs。inode_pct が None なら inode の数を持たない（files=0）。"""
    bfree = int(round(blocks * (100 - bytes_pct) / 100))
    if inode_pct is None:
        f_files, ffree = files if files <= 0 else 0, 0
    else:
        f_files, ffree = files, int(round(files * (100 - inode_pct) / 100))
    return SimpleNamespace(f_bsize=4096, f_frsize=4096, f_blocks=blocks, f_bfree=bfree, f_bavail=bfree,
                           f_files=f_files, f_ffree=ffree, f_favail=ffree)


class FakeFs:
    """path → statvfs の値。set で変える。"""

    def __init__(self, **by_path):
        self.by_path = dict(by_path)

    def __call__(self, path):
        if path not in self.by_path:
            raise FileNotFoundError(path)
        return self.by_path[path]


def conf(**kw):
    c = json.loads(json.dumps(DEFAULT_DISK_WATCH))
    c.update(kw)
    return c


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.dir = self.t / "watched"
        self.dir.mkdir()
        self.p = str(self.dir)
        self.state = self.t / "rt" / "claude-disk-watch" / "state.json"
        self.sent, self.notified, self.logs = [], [], []
        self.send_result = (True, "socket")

    def tearDown(self):
        self.tmp.cleanup()

    def send(self, role, text):
        self.sent.append((role, text))
        if isinstance(self.send_result, Exception):
            raise self.send_result
        return self.send_result

    def notify(self, text):
        self.notified.append(text)
        return True

    def watch(self, fs, **kw):
        c = conf(paths=[self.p], notify_role="aom-master", **kw)
        return DW.DiskWatch({"HOME": str(self.t)}, c, statvfs=fs, send=self.send, notify=self.notify,
                            top=lambda p: {"total": 900, "groups": [("tmp*", 850), ("lua_*", 40)]}, log=self.logs.append, state_file=self.state)


class OncePerThreshold(Base):
    def test_each_threshold_once(self):
        fs = FakeFs(**{self.p: vfs(inode_pct=60)})
        w = self.watch(fs)
        self.assertEqual(w.check(), [])
        fs.by_path[self.p] = vfs(inode_pct=72)
        self.assertEqual(w.check(), [(self.p, "inodes", 70, "socket")])
        for pct in (72, 75, 80):
            fs.by_path[self.p] = vfs(inode_pct=pct)
            self.assertEqual(w.check(), [])
        fs.by_path[self.p] = vfs(inode_pct=86)
        self.assertEqual(w.check(), [(self.p, "inodes", 85, "socket")])
        fs.by_path[self.p] = vfs(inode_pct=99)
        self.assertEqual(w.check(), [])
        self.assertEqual(len(self.sent), 2)
        role, text = self.sent[0]
        self.assertEqual(role, "aom-master")
        self.assertTrue(text.startswith(f"[claude-kitty disk] {self.p} の inode が 72%（754,975 / 1,048,576）。70% を超えました。"),
                        text)
        self.assertIn("直下は 900 項目。多いもの（名前の頭でまとめた数）: tmp*（850）、lua_*（40）。", text)
        self.assertEqual(self.notified, [])

    def test_jump_over_two_thresholds_sends_one_with_the_higher(self):
        fs = FakeFs(**{self.p: vfs(bytes_pct=90)})
        w = self.watch(fs)
        self.assertEqual(w.check(), [(self.p, "bytes", 85, "socket")])
        self.assertIn("の容量が 90%", self.sent[0][1])
        fs.by_path[self.p] = vfs(bytes_pct=88)
        self.assertEqual(w.check(), [])  # 70 も印が付いている

    def test_bytes_and_inodes_are_separate(self):
        fs = FakeFs(**{self.p: vfs(bytes_pct=71, inode_pct=71)})
        w = self.watch(fs)
        counted = []
        w.top = lambda p: counted.append(p) or {"total": 1, "groups": [("a", 1)]}
        out = w.check()
        self.assertEqual(sorted(k for _p, k, _t, _v in out), ["bytes", "inodes"])
        self.assertEqual(counted, [self.p])  # 直下は path ごとに 1 回だけ数える
        self.assertTrue(all("a（1）" in text for _r, text in self.sent))

    def test_top_omitted_when_none(self):
        text = DW.message("/tmp", "inodes", 10, 100, 72.0, 70, None)
        self.assertNotIn("多いもの", text)
        self.assertIn("ユーザーが打ったものではない", text)


class Hysteresis(Base):
    def test_rearm_only_10_points_below(self):
        fs = FakeFs(**{self.p: vfs(inode_pct=72)})
        w = self.watch(fs)
        self.assertEqual(len(w.check()), 1)
        for pct in (65, 61, 72, 60):  # 60 は 70 - 10 ちょうど: まだ下回っていない
            fs.by_path[self.p] = vfs(inode_pct=pct)
            self.assertEqual(w.check(), [], pct)
        fs.by_path[self.p] = vfs(inode_pct=59)
        self.assertEqual(w.check(), [])
        fs.by_path[self.p] = vfs(inode_pct=71)
        self.assertEqual(w.check(), [(self.p, "inodes", 70, "socket")])

    def test_upper_rearms_independently(self):
        fs = FakeFs(**{self.p: vfs(inode_pct=90)})
        w = self.watch(fs)
        w.check()
        fs.by_path[self.p] = vfs(inode_pct=74)  # 85 - 10 = 75 を下回った。70 はまだ超えたまま
        self.assertEqual(w.check(), [])
        fs.by_path[self.p] = vfs(inode_pct=86)
        self.assertEqual(w.check(), [(self.p, "inodes", 85, "socket")])


class NoInodes(Base):
    def test_zero_or_negative_inode_count_is_ignored(self):
        for files in (0, -1):
            with self.subTest(files=files):
                v = vfs(bytes_pct=50, inode_pct=None, files=files)
                v.f_ffree = -281234  # composefs などで実際に見た形
                m = DW.measure("/x", lambda p: v)
                self.assertIsNone(m["inodes"])
                self.assertAlmostEqual(m["bytes"][2], 50.0)

    def test_zero_blocks_is_ignored(self):
        v = vfs(inode_pct=10)
        v.f_blocks = v.f_bfree = v.f_bavail = 0
        self.assertIsNone(DW.measure("/x", lambda p: v)["bytes"])

    def test_only_bytes_alerted(self):
        fs = FakeFs(**{self.p: vfs(bytes_pct=95, inode_pct=None)})
        self.assertEqual(self.watch(fs).check(), [(self.p, "bytes", 85, "socket")])

    def test_bytes_percent_uses_avail_like_df(self):
        v = SimpleNamespace(f_bsize=1, f_frsize=1, f_blocks=100, f_bfree=20, f_bavail=10, f_files=0, f_ffree=0)
        used, total, pct = DW.measure("/x", lambda p: v)["bytes"]
        self.assertEqual((used, total), (80, 100))
        self.assertAlmostEqual(pct, 80 * 100 / 90)  # 予約の分を除いた空きで割る


class MissingPath(Base):
    def test_missing_and_unexpandable_paths_are_skipped(self):
        fs = FakeFs(**{self.p: vfs(inode_pct=72)})
        c = conf(paths=[str(self.t / "none"), "$NOT_SET_ANYWHERE/x", "${ALSO_NOT}", self.p], notify_role=None)
        w = DW.DiskWatch({"HOME": str(self.t)}, c, statvfs=fs, notify=self.notify, top=lambda p: None,
                         log=self.logs.append, state_file=self.state)
        self.assertEqual(w.check(), [(self.p, "inodes", 70, "desktop")])

    def test_statvfs_error_is_skipped(self):
        def boom(p):
            raise PermissionError(p)
        self.assertEqual(self.watch(boom).check(), [])

    def test_expand(self):
        env = {"HOME": "/h", "XDG_RUNTIME_DIR": "/run/user/1"}
        self.assertEqual(DW.expand("~/.cache/cc-tmp", env), "/h/.cache/cc-tmp")
        self.assertEqual(DW.expand("$XDG_RUNTIME_DIR", env), "/run/user/1")
        self.assertEqual(DW.expand("${XDG_RUNTIME_DIR}/x", env), "/run/user/1/x")
        self.assertIsNone(DW.expand("$XDG_RUNTIME_DIR", {}))  # 空にして / を見ない
        self.assertEqual(DW.expand("/tmp", {}), "/tmp")
        self.assertEqual(DW.expand("~user/x", env), "~user/x")


class Restart(Base):
    def test_restart_does_not_repeat(self):
        fs = FakeFs(**{self.p: vfs(inode_pct=72)})
        self.assertEqual(len(self.watch(fs).check()), 1)
        self.assertTrue(self.state.exists())
        self.assertEqual(oct(self.state.parent.stat().st_mode & 0o777), "0o700")
        w2 = self.watch(fs)  # watch を起動し直した
        self.assertEqual(w2.check(), [])
        self.assertEqual(len(self.sent), 1)
        # 起動し直したあとでも、下がってからまた超えれば知らせる
        fs.by_path[self.p] = vfs(inode_pct=50)
        w2.check()
        w3 = self.watch(fs)
        fs.by_path[self.p] = vfs(inode_pct=72)
        self.assertEqual(len(w3.check()), 1)

    def test_broken_state_file_starts_fresh(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text("{not json")
        fs = FakeFs(**{self.p: vfs(inode_pct=72)})
        self.assertEqual(len(self.watch(fs).check()), 1)
        self.state.write_text('{"fired": {"x": "yes"}}')  # 形の違う印は捨てる
        self.assertEqual(self.watch(fs)._load(), {})

    def test_unwritable_state_keeps_memory(self):
        blocker = self.t / "file"
        blocker.write_text("")
        fs = FakeFs(**{self.p: vfs(inode_pct=72)})
        c = conf(paths=[self.p], notify_role="r")
        w = DW.DiskWatch({}, c, statvfs=fs, send=self.send, notify=self.notify, top=lambda p: None,
                         log=self.logs.append, state_file=blocker / "sub" / "state.json")  # 書けない
        self.assertEqual(len(w.check()), 1)
        self.assertEqual(w.check(), [])  # 動いている間は覚えている
        self.assertTrue(any("知らせた印を書けない" in m for m in self.logs), self.logs)

    def test_state_path_is_under_runtime_dir(self):
        self.assertEqual(DW.state_path({"XDG_RUNTIME_DIR": "/run/user/9"}),
                         Path("/run/user/9/claude-disk-watch/state.json"))


class Fallback(Base):
    def test_send_fails_then_desktop(self):
        self.send_result = (False, "使える部品が無い")
        fs = FakeFs(**{self.p: vfs(inode_pct=72)})
        self.assertEqual(self.watch(fs).check(), [(self.p, "inodes", 70, "desktop")])
        self.assertEqual(len(self.notified), 1)
        self.assertIn("aom-master に送れない", self.logs[0])

    def test_send_raises_then_desktop(self):
        self.send_result = OSError("connection refused")
        fs = FakeFs(**{self.p: vfs(inode_pct=72)})
        self.assertEqual(self.watch(fs).check()[0][3], "desktop")
        self.assertIn("connection refused", self.logs[0])

    def test_no_role_only_desktop(self):
        fs = FakeFs(**{self.p: vfs(inode_pct=72)})
        w = self.watch(fs)
        w.conf["notify_role"] = None
        self.assertEqual(w.check()[0][3], "desktop")
        self.assertEqual(self.sent, [])

    def test_both_fail(self):
        self.send_result = (False, "x")
        fs = FakeFs(**{self.p: vfs(inode_pct=72)})
        w = self.watch(fs)
        w.notify = lambda text: False
        self.assertEqual(w.check()[0][3], "none")
        self.assertEqual(w.check(), [])  # 毎分くり返さない
        self.assertTrue(any("デスクトップの通知も出せない" in m for m in self.logs))

    def test_desktop_notify_argv(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return SimpleNamespace(returncode=0)
        self.assertTrue(DW.desktop_notify("本文", platform="linux", run=run))
        self.assertEqual(calls[-1][0], "notify-send")
        self.assertEqual(calls[-1][-1], "本文")
        self.assertTrue(DW.desktop_notify('a"b', platform="darwin", run=run))
        self.assertEqual(calls[-1][:2], ["osascript", "-e"])
        self.assertIn('"a\\"b"', calls[-1][2])

        def missing(argv, **kw):
            raise FileNotFoundError(argv[0])
        self.assertFalse(DW.desktop_notify("x", platform="linux", run=missing))
        self.assertFalse(DW.desktop_notify("x", platform="linux", run=lambda a, **k: SimpleNamespace(returncode=1)))


class WakeParts(unittest.TestCase):
    """起こす部品（lib/relay.py の WAKERS）との受け渡し。"""

    def test_wakers_tried_in_order(self):
        class W:
            def __init__(self, name, avail, ok):
                self.name, self.avail, self.ok, self.got = name, avail, ok, []

            def available(self, env, role):
                return self.avail

            def wake(self, env, role, text):
                if isinstance(self.ok, Exception):
                    raise self.ok
                self.got.append((role, text))
                return self.ok
        a, b, c = W("a", False, True), W("b", True, OSError("gone")), W("c", True, True)
        self.assertEqual(DW.wake_send({}, "main", "t", wakers=[a, b, c]), (True, "c"))
        self.assertEqual((a.got, c.got), ([], [("main", "t")]))
        ok, why = DW.wake_send({}, "main", "t", wakers=[a])
        self.assertFalse(ok)
        self.assertIn("使える部品が無い", why)
        ok, why = DW.wake_send({}, "main", "t", wakers=[b])
        self.assertEqual((ok, why), (False, "b: OSError: gone"))

    def test_socket_waker_end_to_end(self):
        """実際の SocketWaker で、wake/<役割>/ の記録の socket に 1 通届く。記録が無ければ送れない。"""
        import proc
        import relay
        with tempfile.TemporaryDirectory() as t:
            env = {"HOME": t, "XDG_STATE_HOME": t + "/state", "XDG_RUNTIME_DIR": t}
            self.assertEqual(DW.wake_send(env, "main", "x")[0], False)  # 記録が無い
            sock_path = t + "/s.sock"
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(sock_path)
            srv.listen(1)
            got = []

            def serve():
                c, _ = srv.accept()
                buf = b""
                while True:
                    d = c.recv(65536)
                    if not d:
                        break
                    buf += d
                got.append(buf)
                c.close()
            th = threading.Thread(target=serve, daemon=True)  # 届かなくてもテストの終わりで待たない
            th.start()
            try:
                rec = {"role": "main", "session_id": "s1", "socket": sock_path, "token": "tok",
                       "claude_pid": os.getpid(), "claude_start": proc.start_time(os.getpid()), "t": 1}
                d = relay.wake_dir(env, "main")
                d.mkdir(parents=True)
                (d / "s1.json").write_text(json.dumps(rec))
                text = DW.message("/tmp", "inodes", 754975, 1048576, 72.0, 70, {"total": 9, "groups": [("tmp*", 9)]})
                self.assertEqual(DW.wake_send(env, "main", text), (True, "socket"))
                th.join(5)
            finally:
                srv.close()
            lines = [json.loads(x) for x in got[0].decode().splitlines()]
            self.assertEqual(lines[0], {"type": "auth", "token": "tok"})
            self.assertEqual(lines[1]["message"]["content"], text)

    def test_default_send_uses_relay_wakers(self):
        """DiskWatch の既定の送り先が relay.WAKERS を通る（env と役割と本文が部品に渡る）。"""
        import relay
        got = []

        class W:
            name = "fake"

            def available(self, env, role):
                return True

            def wake(self, env, role, text):
                got.append((env.get("HOME"), role, text))
                return True
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "w"
            d.mkdir()
            c = conf(paths=[str(d)], notify_role="aom-master")
            w = DW.DiskWatch({"HOME": t, "XDG_RUNTIME_DIR": t}, c, statvfs=lambda p: vfs(inode_pct=72),
                             notify=lambda text: self.fail("通知に落ちた"), top=lambda p: None)
            old = relay.WAKERS
            relay.WAKERS = [W()]
            try:
                self.assertEqual(w.check(), [(str(d), "inodes", 70, "fake")])
            finally:
                relay.WAKERS = old
            self.assertEqual(got[0][:2], (t, "aom-master"))
            self.assertTrue(got[0][2].startswith("[claude-kitty disk]"))
            self.assertTrue((Path(t) / "claude-disk-watch" / "state.json").exists())

    def test_message_is_not_counted_as_human(self):
        from presence import is_human
        text = DW.message("/tmp", "inodes", 1, 2, 72.0, 70, None)
        self.assertFalse(is_human(text, None))
        self.assertFalse(is_human(f"<cross-session-message from=\"x\">{text}</cross-session-message>", None))


class Interval(Base):
    def test_due(self):
        w = self.watch(FakeFs(), interval_sec=60.0)
        self.assertTrue(w.due(100.0))  # 起動してすぐ 1 回
        self.assertFalse(w.due(130.0))
        self.assertTrue(w.due(160.0))
        w.conf["enabled"] = False
        self.assertFalse(w.due(1000.0))


class TopGroups(unittest.TestCase):
    def test_name_group(self):
        cases = {"tmp0": "tmp*", "tmpp1rtt87k": "tmp*", "tmp_k2abx9q": "tmp*", "tmp": "tmp", "tmp.Ab12": "tmp.*", "lua_ab12": "lua_*",
                 "lua_XyZq": "lua_*", "pytest-123": "pytest-*", "pytest-of-x-12": "pytest-of-x-*", "claude-1000": "claude-*",
                 "pressure-vessel-libs-kXaPFP": "pressure-vessel-libs-*", "abc123": "abc*", "12345": "*",
                 "systemd-private-3f2a-foo.service-AbC": "systemd-private-*", "v8-compile-cache-1000": "v8-compile-cache-*",
                 "node-compile-cache": "node-compile-cache", ".X11-unix": ".X11-unix", "a1": "a1",
                 "_MEI12345": "_MEI*", "tmpfiles.d": "tmpfiles.d", "fooab3xk": "fooab*"}
        for name, want in cases.items():
            with self.subTest(name=name):
                self.assertEqual(DW.name_group(name), want)

    def test_accident_shape_points_at_the_cause(self):
        """事故の形: 直下に小さな一時フォルダがたくさん。名前の頭でまとめた数で原因を指す（中には潜らない）。"""
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            for i in range(30):
                d = root / tempfile.mkdtemp(dir=t).rsplit("/", 1)[1]  # 本物の tempfile の名前（tmp + 乱数）
                (d / "x").write_text("")
            for i in range(5):
                (root / f"lua_{i}ab{i}").mkdir()
            (root / "big-but-alone").mkdir()
            for i in range(50):
                (root / "big-but-alone" / f"f{i}").write_text("")
            (root / "claude-1000").mkdir()
            out = DW.top_groups(t)
        self.assertEqual(out["total"], 37)
        self.assertEqual(out["groups"], [("tmp*", 30), ("lua_*", 5), ("big-but-alone", 1)])
        self.assertEqual(out["deep"][0], ("big-but-alone", 50))
        self.assertNotIn("claude-1000", dict(out["deep"]))  # 中が空のものは出さない
        self.assertFalse(out["partial"])

    def test_deep_shape_points_at_the_folder(self):
        """1 つのフォルダの下に積もる形（scratchpad の claude-<uid>/ など）: 中の数で指す。"""
        with tempfile.TemporaryDirectory() as t:
            root = Path(t) / "claude-1000"
            for i in range(20):
                d = root / f"proj{i}" / "tmp"
                d.mkdir(parents=True)
                for j in range(10):
                    (d / f"f{j}").write_text("")
            (Path(t) / "small").mkdir()
            (Path(t) / "small" / "x").write_text("")
            out = DW.top_groups(t)
            self.assertEqual(out["groups"], [("claude-*", 1), ("small", 1)])
            self.assertEqual(out["deep"], [("claude-1000", 20 * 12), ("small", 1)])
            text = DW.message(t, "inodes", 1, 2, 72.0, 70, out)
            self.assertIn("中が多いもの（下の全部の数）: claude-1000（240）、small（1）。", text)
            # 時間切れ: 数えたところまでを「以上」で出す
            calls = []

            def clock():
                calls.append(1)
                return 0.0 if len(calls) < 6 else 99.0  # 直下は数え終わり、中を数える途中で時間切れ
            out = DW.top_groups(t, budget=1.0, clock=clock, every=16)
            self.assertTrue(out["partial"])
            deep = dict(out["deep"])
            self.assertTrue(0 < deep.get("claude-1000", 0) < 240, out)  # 途中まで（直下を読む順は決まらない）
            self.assertIn("以上）", DW.message(t, "inodes", 1, 2, 72.0, 70, out))
            self.assertIn("時間切れで途中まで", DW.message(t, "inodes", 1, 2, 72.0, 70, out))

    def test_merge_loose_prefix_without_separator(self):
        """mktemp -d /tmp/fooXXXXXX の形（区切りの無い頭 + 英数字の乱数）が、何千ものグループに割れない。"""
        import random
        import string
        rnd = random.Random(1)
        chars = string.ascii_letters + string.digits
        counts = {}
        names = ["foo" + "".join(rnd.choice(chars) for _ in range(6)) for _ in range(3000)]
        names += ["steamhdBNOH", "node", "nodejs", "lua_ab12", "lua_cd34"]
        for n in names:
            k = DW.name_group(n)
            counts[k] = counts.get(k, 0) + 1
        self.assertGreater(len(counts), 1000)  # まとめ直す前は割れている
        out = DW.merge_loose(counts)
        self.assertEqual(out["foo*"], 3000)
        self.assertEqual((out["steamhdBNOH*"], out["node"], out["nodejs"], out["lua_*"]), (1, 1, 1, 2))
        self.assertEqual(len(out), 5)

    def test_empty_folder_message(self):
        with tempfile.TemporaryDirectory() as t:
            out = DW.top_groups(t)
            self.assertEqual(out, {"total": 0, "groups": [], "deep": [], "partial": False})
            text = DW.message(t, "inodes", 1, 2, 72.0, 70, out)
            self.assertNotIn("直下は", text)
            self.assertNotIn("中が多いもの", text)

    def test_symlinked_path_counts_the_target(self):
        with tempfile.TemporaryDirectory() as t:
            real = Path(t) / "real"
            real.mkdir()
            for i in range(3):
                (real / f"tmp{i}").mkdir()
            (real / "tmp0" / "x").write_text("")
            (Path(t) / "link").symlink_to(real)
            opened = []
            orig = os.scandir

            def scandir(p):
                opened.append(str(p))
                return orig(p)
            DW.os.scandir = scandir
            try:
                out = DW.top_groups(str(Path(t) / "link"))
            finally:
                DW.os.scandir = orig
            self.assertEqual(opened[0], os.path.realpath(real))  # リンクでなく、たどった先を開く
            self.assertEqual((out["total"], out["groups"], out["deep"]), (3, [("tmp*", 3)], [("tmp0", 1)]))

    def test_over_budget_is_omitted(self):
        with tempfile.TemporaryDirectory() as t:
            for i in range(1000):
                (Path(t) / f"f{i}").write_text("")
            calls = []

            def clock():
                calls.append(1)
                return float(len(calls))  # 見るたびに 1 秒進む
            self.assertIsNone(DW.top_groups(t, budget=2.0, clock=clock, every=256))
            self.assertLessEqual(len(calls), 4)  # 上限を超えたところで止まった（1000 個を全部は読まない）
            self.assertIsNone(DW.top_groups(t + "/none"))
            self.assertEqual(DW.top_groups(t)["total"], 1000)


class Config(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, section):
        self.path.write_text(json.dumps({"disk_watch": section}))
        return load_disk_watch_config({}, self.path)

    def test_defaults(self):
        self.assertEqual(load_disk_watch_config({}, self.path), DEFAULT_DISK_WATCH)  # ファイルが無い
        self.path.write_text('{"disk_watch": null}')  # null は書いていないのと同じ
        self.assertEqual(load_disk_watch_config({}, self.path), DEFAULT_DISK_WATCH)
        self.path.write_text("{}")
        self.assertEqual(load_disk_watch_config({}, self.path)["paths"], ["/tmp"])
        self.assertIsNone(load_disk_watch_config({}, self.path)["notify_role"])
        self.assertTrue(load_disk_watch_config({}, self.path)["enabled"])

    def test_example(self):
        c = self.load({"enabled": True, "paths": ["/tmp", "$XDG_RUNTIME_DIR", "~/.cache/cc-tmp"],
                       "thresholds": [85, 70, 70], "notify_role": "aom-master", "interval_sec": 60})
        self.assertEqual(c["thresholds"], [70, 85])
        self.assertEqual(c["notify_role"], "aom-master")
        self.assertEqual(c["interval_sec"], 60.0)

    def test_errors(self):
        bad = [{"enabled": "yes"}, {"paths": "/tmp"}, {"paths": [""]}, {"paths": [1]}, {"thresholds": []},
               {"thresholds": [0]}, {"thresholds": [101]}, {"thresholds": ["70"]}, {"thresholds": [True]},
               {"thresholds": 70}, {"notify_role": ""}, {"notify_role": "-x"}, {"notify_role": "a b"},
               {"notify_role": 3}, {"interval_sec": 0}, {"interval_sec": 1}, {"interval_sec": True},
               {"interval_sec": "60"}, {"thresholds": [10]}, {"thresholds": [19.5]}, {"threshold": [70]},
               {"path": ["/tmp"]}]
        for b in bad:
            with self.subTest(b=b):
                with self.assertRaises(ValueError):
                    self.load(b)
        self.path.write_text('{"disk_watch": []}')
        with self.assertRaises(ValueError):
            load_disk_watch_config({}, self.path)
        self.path.write_text("{broken")
        with self.assertRaises(ValueError):
            load_disk_watch_config({}, self.path)

    def test_example_file_is_valid(self):
        c = load_disk_watch_config({}, ROOT / "config.example.json")
        self.assertIn("/tmp", c["paths"])


def load_claude_state():
    p = ROOT / "bin" / "claude-state"
    loader = importlib.machinery.SourceFileLoader("claude_state_cli_disk", str(p))
    spec = importlib.util.spec_from_loader("claude_state_cli_disk", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class WatchCli(unittest.TestCase):
    def env(self, t, cfg=None):
        return {"XDG_RUNTIME_DIR": t, "CLAUDE_KITTY_CONFIG": str(cfg or Path(t) / "none.json"), "XDG_CONFIG_HOME": t,
                "HOME": t}

    def test_loop_runs_disk_check(self):
        with tempfile.TemporaryDirectory() as t:
            cs = load_claude_state()
            calls = []

            class Disk:
                def due(self, mono):
                    return True

                def check(self):
                    calls.append("check")
                    raise RuntimeError("boom")  # 落ちても色の見張りは続く

                def preview(self):
                    raise AssertionError("常駐では preview を使わない")

            class Stop(Exception):
                pass
            sleeps = []

            def sleep(sec):
                sleeps.append(sec)
                if len(sleeps) >= 2:
                    raise Stop
            with self.assertRaises(Stop):
                cs.cmd_watch(self.env(t), ["unix:" + t + "/none"], sleep=sleep, disk=Disk())
            self.assertEqual(calls, ["check", "check"])
            self.assertIn("disk: RuntimeError: boom", (Path(t) / "claude-state" / "errors.log").read_text())

    def test_once_only_measures(self):
        """--once は測った値だけ。知らせない・印を付けない（常駐の watch の知らせを横取りしない）。"""
        with tempfile.TemporaryDirectory() as t:
            watched = Path(t) / "w"
            watched.mkdir()
            cfg = Path(t) / "config.json"
            cfg.write_text(json.dumps({"disk_watch": {"paths": [str(watched), str(Path(t) / "none")],
                                                      "notify_role": "aom-master"}}))
            cs = load_claude_state()
            import diskwatch
            sent = []
            dw = diskwatch.DiskWatch(self.env(t, cfg), load_disk_watch_config({}, cfg),
                                     statvfs=lambda p: vfs(bytes_pct=20, inode_pct=90),
                                     send=lambda r, x: sent.append(x) or (True, "socket"),
                                     notify=lambda x: sent.append(x) or True, top=lambda p: None)
            out = io.StringIO()
            old = sys.stdout
            sys.stdout = out
            try:
                self.assertEqual(cs.cmd_watch(self.env(t, cfg), ["unix:" + t + "/none"], once=True, disk=dw), 0)
            finally:
                sys.stdout = old
            self.assertEqual(sent, [])
            self.assertFalse(dw.state_file.exists())
            self.assertEqual(dw.fired, {})
            text = out.getvalue()
            self.assertIn(f"disk {watched} bytes: 20%\n", text)
            self.assertIn(f"disk {watched} inodes: 90%（85% を超えている）", text)
            self.assertIn(f"disk {t}/none: 無い（飛ばす）", text)
            self.assertEqual(len(dw.check()), 1)  # 常駐の側では、まだ知らせていないので知らせる

    def test_broken_config_disables_only_disk(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = Path(t) / "config.json"
            cfg.write_text('{"disk_watch": {"thresholds": "x"}}')
            env = {"XDG_RUNTIME_DIR": t, "CLAUDE_KITTY_CONFIG": str(cfg), "XDG_CONFIG_HOME": t}
            cs = load_claude_state()
            self.assertIsNone(cs.make_disk_watch(env))
            self.assertIn("disk: 設定が正しくないので見張らない", (Path(t) / "claude-state" / "errors.log").read_text())
            cfg.write_text('{"disk_watch": {"enabled": false}}')
            self.assertIsNone(cs.make_disk_watch(env))
            cfg.write_text('{"disk_watch": {"paths": ["/nonexistent-x"]}}')
            self.assertIsNotNone(cs.make_disk_watch(env))
            import contextlib
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cs.cmd_watch(env, ["unix:" + t + "/none"], once=True), 0)  # 無い path だけ: 何もしない
            self.assertEqual(out.getvalue(), "disk /nonexistent-x: 無い（飛ばす）\n")


class Doctor(unittest.TestCase):
    def run_check(self, section, fs):
        import doctor as D
        with tempfile.TemporaryDirectory() as t:
            cfg = Path(t) / "config.json"
            cfg.write_text(json.dumps({"disk_watch": section}))
            ctx = D.Ctx(env={"HOME": t, "CLAUDE_KITTY_CONFIG": str(cfg), "XDG_RUNTIME_DIR": t}, os_name="Linux",
                        run=lambda a, timeout: (None, ""), which=lambda n: None, statvfs=fs)
            rep = D.Report()
            D.check_disk(ctx, rep)
            cfg_rep = D.Report()
            D.check_config(ctx, cfg_rep)
            return rep.items, {i["name"]: i for i in cfg_rep.items}

    def test_rows(self):
        with tempfile.TemporaryDirectory() as t:
            low, high = t + "/low", t + "/high"
            os.mkdir(low)
            os.mkdir(high)
            fs = FakeFs(**{low: vfs(bytes_pct=12, inode_pct=None), high: vfs(bytes_pct=20, inode_pct=88)})
            items, cfg = self.run_check({"paths": [low, high, t + "/none"], "notify_role": "aom-master"}, fs)
        by = {i["name"]: i for i in items}
        self.assertEqual(by[f"ディスク: {low}"]["status"], "ok")
        self.assertIn("容量 12%・inode は数を持たないので見ない", by[f"ディスク: {low}"]["detail"])
        self.assertIn("知らせる先: aom-master", by[f"ディスク: {low}"]["detail"])
        self.assertEqual(by[f"ディスク: {high}"]["status"], "warn")
        self.assertIn("inode 88%（85% を超えている）", by[f"ディスク: {high}"]["detail"])
        self.assertEqual(by[f"ディスク: {t}/none"]["status"], "skip")
        self.assertEqual(cfg["設定: disk_watch"]["status"], "ok")

    def test_disabled_and_broken(self):
        items, _ = self.run_check({"enabled": False}, FakeFs())
        self.assertEqual([i["status"] for i in items], ["skip"])
        items, cfg = self.run_check({"interval_sec": -1}, FakeFs())
        self.assertEqual(items, [])
        self.assertEqual(cfg["設定: disk_watch"]["status"], "ng")


if __name__ == "__main__":
    unittest.main()
