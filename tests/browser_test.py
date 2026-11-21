#!/usr/bin/env python3
"""claude-browser が起動する Chrome の探し方・プロファイルの置き場・設定 browser・KWin が無いときの止まり方。

本物の Chrome・flatpak・qdbus（KWin）・kitty は呼ばない（PATH は偽物と要るものだけ、which / run は差し替え）。
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import browser as B  # noqa: E402
import doctor as D  # noqa: E402
from config import load_browser_config  # noqa: E402

HOME = Path("/home/u")
LEGACY = HOME / ".var/app/com.google.Chrome/claude-profiles"
NONE = {"enabled": True, "command": None, "profile_root": None}


def find(paths=(), flatpaks=(), dirs=(), conf=None, env=None, os_name="Linux"):
    """paths: PATH にあるコマンド {名前: 場所}、flatpaks: 入っている Flatpak のアプリ ID、dirs: 在るフォルダ。"""
    paths = dict(paths)
    asked = []

    def flatpak_has(app):
        asked.append(app)
        return app in flatpaks
    b = B.find_browser(conf or NONE, env or {}, HOME, paths.get, lambda p: p in {str(d) for d in dirs},
                       flatpak_has, os_name)
    return b, asked


FLATPAK = {"flatpak": "/usr/bin/flatpak"}


class Find(unittest.TestCase):
    def test_native_chrome_first(self):
        b, asked = find({"google-chrome-stable": "/usr/bin/google-chrome-stable", "google-chrome": "/usr/bin/google-chrome",
                         "chromium": "/usr/bin/chromium", **FLATPAK}, flatpaks={"com.google.Chrome"})
        self.assertEqual(b["command"], ["/usr/bin/google-chrome-stable"])
        self.assertEqual((b["kind"], b["configured"]), ("native", False))
        self.assertEqual(b["profile_root"], HOME / ".local/share/claude-kitty/browser-profiles")
        self.assertEqual(asked, [])  # 前からのプロファイルが無ければ flatpak info も呼ばない

    def test_google_chrome_without_stable(self):
        b, _ = find({"google-chrome": "/opt/bin/google-chrome", "chromium": "/usr/bin/chromium"})
        self.assertEqual(b["command"], ["/opt/bin/google-chrome"])

    def test_flatpak_chrome_before_chromium(self):
        b, _ = find({"chromium": "/usr/bin/chromium", **FLATPAK}, flatpaks={"com.google.Chrome"})
        self.assertEqual(b["command"], ["flatpak", "run", "com.google.Chrome"])
        self.assertEqual(b["kind"], "flatpak")
        self.assertEqual(b["profile_root"], LEGACY)  # Flatpak の Chrome は 0.4 までと同じ置き場

    def test_native_chromium(self):
        for name in ("chromium", "chromium-browser"):
            b, _ = find({name: f"/usr/bin/{name}", **FLATPAK}, flatpaks={"org.chromium.Chromium"})
            self.assertEqual(b["command"], [f"/usr/bin/{name}"], name)
            # Chrome とは cookie の鍵が違うので、Chrome のプロファイルと別の置き場
            self.assertEqual(b["profile_root"], HOME / ".local/share/claude-kitty/chromium-profiles")

    def test_flatpak_chromium_last(self):
        b, asked = find(FLATPAK, flatpaks={"org.chromium.Chromium"})
        self.assertEqual(b["command"], ["flatpak", "run", "org.chromium.Chromium"])
        self.assertEqual(b["profile_root"], HOME / ".var/app/org.chromium.Chromium/claude-profiles")
        self.assertEqual(asked, ["com.google.Chrome", "org.chromium.Chromium"])

    def test_flatpak_ignored_without_flatpak_command(self):
        b, asked = find(flatpaks={"com.google.Chrome"}, dirs=[LEGACY])
        self.assertIsNone(b["command"])
        self.assertEqual(asked, [])

    def test_nothing_found(self):
        b, _ = find(FLATPAK)
        self.assertIsNone(b["command"])
        # 台帳と list のための置き場は返す
        self.assertEqual(b["profile_root"], HOME / ".local/share/claude-kitty/browser-profiles")
        b, _ = find(conf={"command": None, "profile_root": "~/p"})
        self.assertEqual(b["profile_root"], Path(os.path.expanduser("~/p")))

    def test_existing_flatpak_profiles_win_over_native(self):
        # 0.4 までの利用者: 後からネイティブの Chrome を入れても、ログイン済みのプロファイルから黙って移らない
        b, _ = find({"google-chrome-stable": "/usr/bin/google-chrome-stable", **FLATPAK},
                    flatpaks={"com.google.Chrome"}, dirs=[LEGACY])
        self.assertEqual(b["command"], ["flatpak", "run", "com.google.Chrome"])
        self.assertEqual(b["profile_root"], LEGACY)
        self.assertEqual(b["profile_root"] / "ledger.json", LEGACY / "ledger.json")

    def test_existing_flatpak_profiles_without_flatpak_chrome(self):
        # Flatpak の Chrome を消したあとの残り物のフォルダでは、そちらを選ばない
        b, _ = find({"google-chrome": "/usr/bin/google-chrome", **FLATPAK}, dirs=[LEGACY])
        self.assertEqual(b["command"], ["/usr/bin/google-chrome"])

    def test_existing_profiles_of_any_candidate_win(self):
        # Flatpak の Chromium を使っていた人が、後から Chrome を入れても Chromium のまま
        fc = HOME / ".var/app/org.chromium.Chromium/claude-profiles"
        b, _ = find({"google-chrome": "/usr/bin/google-chrome", **FLATPAK}, flatpaks={"com.google.Chrome", "org.chromium.Chromium"},
                    dirs=[fc])
        self.assertEqual((b["command"], b["profile_root"]), (["flatpak", "run", "org.chromium.Chromium"], fc))
        # ネイティブの Chromium を使っていた人が、後から google-chrome を入れても Chromium のまま
        nc = HOME / ".local/share/claude-kitty/chromium-profiles"
        b, _ = find({"google-chrome": "/usr/bin/google-chrome", "chromium": "/usr/bin/chromium"}, dirs=[nc])
        self.assertEqual((b["command"], b["profile_root"]), (["/usr/bin/chromium"], nc))
        # 置き場が両方あれば、ふだんの順（ネイティブの Chrome が先）
        nb = HOME / ".local/share/claude-kitty/browser-profiles"
        b, _ = find({"google-chrome": "/usr/bin/google-chrome", "chromium": "/usr/bin/chromium"}, dirs=[nc, nb])
        self.assertEqual(b["command"], ["/usr/bin/google-chrome"])

    def test_configured_command_wins(self):
        conf = {"command": ["/opt/chrome/chrome", "--ozone-platform=wayland"], "profile_root": None}
        b, asked = find({"google-chrome-stable": "/usr/bin/google-chrome-stable", **FLATPAK},
                        flatpaks={"com.google.Chrome"}, dirs=[LEGACY], conf=conf)
        self.assertEqual(b["command"], ["/opt/chrome/chrome", "--ozone-platform=wayland"])
        self.assertTrue(b["configured"])
        self.assertEqual(b["profile_root"], HOME / ".local/share/claude-kitty/browser-profiles")
        self.assertEqual(asked, [])

    def test_configured_flatpak_keeps_flatpak_profile_dir(self):
        # 作者の前の動きを設定で固定する形: flatpak run com.google.Chrome → 前と同じ置き場
        b, _ = find(conf={"command": ["flatpak", "run", "com.google.Chrome"], "profile_root": None})
        self.assertEqual((b["kind"], b["name"]), ("flatpak", "com.google.Chrome"))
        self.assertEqual(b["profile_root"], LEGACY)
        b, _ = find(conf={"command": ["flatpak", "run", "--branch=beta", "com.google.ChromeDev"], "profile_root": None})
        self.assertEqual(b["profile_root"], HOME / ".var/app/com.google.ChromeDev/claude-profiles")

    def test_profile_root_from_config(self):
        b, _ = find({"google-chrome": "/usr/bin/google-chrome"}, conf={"command": None, "profile_root": "/data/prof"})
        self.assertEqual(b["profile_root"], Path("/data/prof"))
        b, _ = find(FLATPAK, flatpaks={"com.google.Chrome"}, conf={"command": None, "profile_root": "/data/prof"})
        self.assertEqual(b["profile_root"], Path("/data/prof"))

    def test_xdg_data_home(self):
        b, _ = find({"google-chrome": "/usr/bin/google-chrome"}, env={"XDG_DATA_HOME": "/x/data"})
        self.assertEqual(b["profile_root"], Path("/x/data/claude-kitty/browser-profiles"))

    def test_snap_chromium(self):
        # Snap はホームの隠しフォルダに書けないので、~/snap/<名前>/common に置く
        b, _ = find({"chromium": "/snap/bin/chromium"})
        self.assertEqual((b["command"], b["kind"]), (["/snap/bin/chromium"], "snap"))
        self.assertEqual(b["profile_root"], HOME / "snap/chromium/common/claude-kitty-profiles")

    def test_macos_app(self):
        b, _ = find(dirs=[B.MAC_CHROME], os_name="Darwin")
        self.assertEqual(b["command"], [B.MAC_CHROME])
        b, _ = find(dirs=[B.MAC_CHROME], os_name="Linux")
        self.assertIsNone(b["command"])

    def test_flatpak_app(self):
        self.assertEqual(B.flatpak_app(["flatpak", "run", "com.google.Chrome"]), "com.google.Chrome")
        self.assertEqual(B.flatpak_app(["/usr/bin/flatpak", "run", "--user", "org.chromium.Chromium", "--x"]),
                         "org.chromium.Chromium")
        self.assertEqual(B.flatpak_app(["flatpak", "run", "--branch", "beta", "com.google.Chrome"]), "com.google.Chrome")
        self.assertEqual(B.flatpak_app(["flatpak", "run", "--branch=beta", "--user", "com.google.Chrome"]), "com.google.Chrome")
        self.assertIsNone(B.flatpak_app(["flatpak", "info", "x"]))
        self.assertIsNone(B.flatpak_app(["flatpak", "run"]))
        self.assertIsNone(B.flatpak_app(["google-chrome"]))
        self.assertIsNone(B.flatpak_app([]))
        self.assertIsNone(B.flatpak_app(None))


class Config(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Path(self.tmp.name) / "c.json"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, data):
        self.p.write_text(data if isinstance(data, str) else json.dumps(data))
        return load_browser_config({}, self.p)

    def test_defaults(self):
        self.assertEqual(load_browser_config({}, self.p), NONE)  # ファイルが無い
        self.assertEqual(self.load({}), NONE)
        self.assertEqual(self.load({"browser": {}}), NONE)
        self.assertEqual(self.load({"browser": {"command": None, "profile_root": None}}), NONE)

    def test_values(self):
        got = self.load({"browser": {"command": ["flatpak", "run", "com.google.Chrome"], "profile_root": "~/p"}})
        self.assertEqual(got, {"enabled": True, "command": ["flatpak", "run", "com.google.Chrome"], "profile_root": "~/p"})
        self.assertFalse(self.load({"browser": {"enabled": False}})["enabled"])
        self.assertTrue(self.load({"browser": {"enabled": True}})["enabled"])

    def test_bad_enabled(self):
        # "false" や 0 を黙って「使う」「使わない」のどちらかに読み替えない
        for bad in ("false", "off", 0, 1, None, [], {}):
            with self.assertRaises(ValueError, msg=repr(bad)) as cm:
                self.load({"browser": {"enabled": bad}})
            self.assertIn("browser.enabled", str(cm.exception))

    def test_bad_shapes(self):
        for bad in ({"browser": []}, {"browser": {"command": "google-chrome"}}, {"browser": {"command": []}},
                    {"browser": {"command": [""]}}, {"browser": {"command": [1]}}, {"browser": {"profile_root": ""}},
                    {"browser": {"profile_root": 3}}, "{broken"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.load(bad)


FAKE_FLATPAK = """#!/bin/sh
echo "$*" >> "$FAKE_LOG/flatpak.calls"
[ "$1" = info ] && [ "$2" = com.google.Chrome ] && exit 0
exit 1
"""
FAKE_CHROME = """#!/bin/sh
exit 0
"""


class Cli(unittest.TestCase):
    """bin/claude-browser を偽の PATH と HOME で動かす（kitty・KWin まで行かないコマンドだけ）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.fake = self.root / "fake"
        self.fake.mkdir()
        self.log = self.root / "log"
        self.log.mkdir()
        self.cfg = self.root / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, name, body):
        (self.fake / name).write_text(body)
        (self.fake / name).chmod(0o755)

    def run_(self, *args, **extra):
        env = {"PATH": str(self.fake), "HOME": str(self.home), "XDG_RUNTIME_DIR": str(self.root / "run"),
               "CLAUDE_KITTY_CONFIG": str(self.cfg), "FAKE_LOG": str(self.log)}
        env.update(extra)
        for name in ("flatpak", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "qdbus", "kitten"):
            q = shutil.which(name, path=env["PATH"])
            assert q is None or q.startswith(str(self.root)), f"本物の {name} に届く PATH: {q}"
        return subprocess.run([sys.executable, str(ROOT / "bin/claude-browser"), *args], env=env,
                              capture_output=True, text=True, timeout=20)

    def status(self, **extra):
        r = self.run_("--key", "role-x", "status", **extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_flatpak_user_keeps_profiles_and_ledger(self):
        legacy = self.home / ".var/app/com.google.Chrome/claude-profiles"
        (legacy / "role-dev-a").mkdir(parents=True)
        (legacy / "ledger.json").write_text('{"role-dev-a": {"note": ["github: ログイン済み"]}}')
        self.put("flatpak", FAKE_FLATPAK)
        self.put("google-chrome-stable", FAKE_CHROME)  # 後から入れたネイティブの Chrome があっても
        st = self.status()
        self.assertEqual(st["browser"], ["flatpak", "run", "com.google.Chrome"])
        self.assertEqual(st["profile_root"], str(legacy))
        r = self.run_("list")
        self.assertEqual([json.loads(l)["key"] for l in r.stdout.splitlines()], ["role-dev-a"])
        self.assertIn("ログイン済み", self.run_("profiles").stdout)

    def test_native_chrome_profile_dir(self):
        self.put("google-chrome", FAKE_CHROME)
        st = self.status()
        self.assertEqual(st["browser"], [str(self.fake / "google-chrome")])
        self.assertEqual(st["profile_root"], str(self.home / ".local/share/claude-kitty/browser-profiles"))
        st = self.status(XDG_DATA_HOME=str(self.root / "data"))
        self.assertEqual(st["profile_root"], str(self.root / "data/claude-kitty/browser-profiles"))

    def test_config_command_and_profile_root(self):
        self.put("google-chrome", FAKE_CHROME)
        self.cfg.write_text(json.dumps({"browser": {"command": ["flatpak", "run", "com.google.Chrome"]}}))
        st = self.status()
        self.assertEqual(st["browser"], ["flatpak", "run", "com.google.Chrome"])
        self.assertEqual(st["profile_root"], str(self.home / ".var/app/com.google.Chrome/claude-profiles"))
        self.cfg.write_text(json.dumps({"browser": {"profile_root": str(self.root / "p")}}))
        self.assertEqual(self.status()["profile_root"], str(self.root / "p"))
        self.assertFalse((self.log / "flatpak.calls").exists())

    def test_broken_config_stops(self):
        self.put("google-chrome", FAKE_CHROME)
        self.cfg.write_text(json.dumps({"browser": {"command": "google-chrome"}}))
        r = self.run_("--key", "role-x", "status")  # 読むだけのコマンドは知らせて既定で続ける
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("browser.command", r.stderr)
        for cmd in (["open", "https://example.com"], ["move"]):  # 別の Chrome・別のプロファイルで開かない
            r = self.run_("--key", "role-x", *cmd)
            self.assertEqual(r.returncode, 1, cmd)
            self.assertIn("browser.command", r.stderr)

    def test_broken_other_section_does_not_block_close_or_list(self):
        # 設定ファイルの書き損じで、動いている Chrome を close・list できなくならない（止めるのは open / move）
        self.put("google-chrome", FAKE_CHROME)
        self.cfg.write_text("{broken")
        r = self.run_("list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("既定で続ける", r.stderr)
        r = self.run_("--key", "role-x", "close")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_("--key", "role-x", "open", "https://example.com")
        self.assertEqual(r.returncode, 1)
        self.assertIn("設定ファイルを読めない", r.stderr)

    def test_open_without_browser_says_what_to_do(self):
        # kitty や KWin を呼ぶ前に、何を探したかと設定の書き方を出して止まる
        r = self.run_("--key", "role-x", "open", "https://example.com")
        self.assertEqual(r.returncode, 1)
        self.assertIn("見つからない", r.stderr)
        self.assertIn("google-chrome", r.stderr)
        self.assertIn("browser.command", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_list_without_browser_still_works(self):
        r = self.run_("list")
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)

    LOGGING_CHROME = """#!/bin/sh
echo "$*" >> "$FAKE_LOG/chrome.calls"
exit 0
"""

    def off(self):
        self.put("google-chrome", self.LOGGING_CHROME)
        self.put("flatpak", FAKE_FLATPAK)
        self.cfg.write_text(json.dumps({"browser": {"enabled": False}}))

    def test_off_every_subcommand_but_close_exits_3(self):
        self.off()
        for cmd in (["open", "https://example.com"], ["open", "https://e", "--mode", "show"], ["move"],
                    ["shot", str(self.root / "s.png")], ["url"], ["status"], ["list"], ["profiles"], ["note", "x"]):
            r = self.run_("--key", "role-x", *cmd)
            self.assertEqual(r.returncode, 3, (cmd, r.stderr))
            self.assertEqual(r.stdout, "", cmd)
            lines = r.stderr.strip().splitlines()
            self.assertEqual(len(lines), 1, (cmd, r.stderr))
            self.assertIn("browser.enabled が false なのでオフ", lines[0])
            self.assertIn(str(self.cfg), lines[0])
        r = self.run_("list")  # --key 無し（kitty の外）でも同じ
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertFalse((self.log / "chrome.calls").exists())  # Chrome を起動しない
        self.assertFalse((self.log / "flatpak.calls").exists())  # 探しもしない
        self.assertFalse((self.root / "run/claude-browser").exists())  # 状態も作らない
        self.assertFalse((self.home / ".local/share/claude-kitty").exists())  # note で台帳も作らない

    def test_off_still_allows_close_help_version(self):
        self.off()
        r = self.run_("--key", "role-x", "close")  # 前に開いた Chrome の片づけ（動いていなければ何もしない）
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "closed")
        self.assertFalse((self.log / "chrome.calls").exists())
        for args in (["--help"], ["open", "--help"]):
            r = self.run_(*args)
            self.assertEqual(r.returncode, 0, args)
            self.assertIn("usage", r.stdout)
            self.assertNotIn("オフ", r.stderr)
        r = self.run_("--version")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("claude-browser", r.stdout)

    def test_enabled_true_or_missing_is_as_before(self):
        self.put("google-chrome", FAKE_CHROME)
        for conf in ({"browser": {"enabled": True}}, {"browser": {}}, {}):
            self.cfg.write_text(json.dumps(conf))
            self.assertEqual(self.status()["browser"], [str(self.fake / "google-chrome")], conf)
            r = self.run_("list")
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_bad_enabled_value_is_a_broken_config(self):
        # 文字列の "false" はオフでもオンでもなく設定の誤り: 今までの壊れた設定と同じく open / move は止め、ほかは知らせて続ける
        self.put("google-chrome", self.LOGGING_CHROME)
        self.cfg.write_text(json.dumps({"browser": {"enabled": "false"}}))
        r = self.run_("--key", "role-x", "status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("browser.enabled", r.stderr)
        for cmd in (["open", "https://example.com"], ["move"]):
            r = self.run_("--key", "role-x", *cmd)
            self.assertEqual(r.returncode, 1, cmd)
            self.assertIn("browser.enabled", r.stderr)
        self.assertFalse((self.log / "chrome.calls").exists())

    def test_launch_argv_keeps_class_for_native(self):
        import importlib.machinery as m
        import importlib.util as u
        loader = m.SourceFileLoader("claude_browser_t", str(ROOT / "bin/claude-browser"))
        b = u.module_from_spec(u.spec_from_loader("claude_browser_t", loader))
        loader.exec_module(b)
        for cmd in (["/usr/bin/google-chrome"], ["flatpak", "run", "com.google.Chrome"], ["chromium", "--ozone-platform=wayland"]):
            argv = b.launch_argv(cmd, Path("/p/role-x"), 9222, "claude-browser-role-x", "https://e")
            self.assertEqual(argv[:len(cmd)], cmd)
            self.assertIn("--class=claude-browser-role-x", argv)  # KWin は resourceClass でこの窓を探す
            self.assertIn("--user-data-dir=/p/role-x", argv)
            self.assertEqual(argv[-1], "https://e")


class KWinErrors(unittest.TestCase):
    """KWin（KDE Plasma）が無い環境では、traceback でなく何が足りないかを出して止まる。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        import kwin
        self.kwin = kwin

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, name, body):
        (self.bin / name).write_text(body)
        (self.bin / name).chmod(0o755)

    def call(self):
        err = io.StringIO()
        env = {"PATH": str(self.bin), "XDG_RUNTIME_DIR": self.tmp.name}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(self.kwin, "STATE_DIR", Path(self.tmp.name)), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            self.kwin.kwin('@@OUT@@"END");')
        self.assertEqual(cm.exception.code, 1)
        return err.getvalue()

    def test_no_qdbus(self):
        msg = self.call()
        self.assertIn("qdbus", msg)
        self.assertIn("KDE Plasma", msg)

    def test_no_journalctl(self):
        self.put("qdbus", "#!/bin/sh\nexit 1\n")
        self.assertIn("journalctl", self.call())

    def test_kwin_not_running(self):
        self.put("qdbus", "#!/bin/sh\necho \"Service 'org.kde.KWin' does not exist.\" >&2\nexit 1\n")
        self.put("journalctl", "#!/bin/sh\nexit 0\n")
        msg = self.call()
        self.assertIn("KWin が動いていない", msg)
        self.assertIn("does not exist", msg)
        self.assertFalse(list(Path(self.tmp.name).glob("*.js")))  # 一時のスクリプトは消す

    def test_qdbus_path(self):
        which = {"qdbus6": "/usr/bin/qdbus6", "qdbus-qt6": "/usr/bin/qdbus-qt6"}
        w = lambda n, path=None: which.get(n)  # noqa: E731
        self.assertEqual(self.kwin.qdbus_path({}, w), "/usr/bin/qdbus6")
        self.assertEqual(self.kwin.qdbus_path({"CLAUDE_KITTY_QDBUS": "/x/q"}, w), "/x/q")
        self.assertIsNone(self.kwin.qdbus_path({}, lambda n, path=None: None))
        self.assertEqual(self.kwin.qdbus_path({}, lambda n, path=None: "/usr/bin/" + n), "/usr/bin/qdbus")


class Doctor(unittest.TestCase):
    """doctor の「Chrome」「KWin」の行は claude-browser と同じ探し方で出す。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.cfg = self.home / "config.json"
        self.which_map = {}
        self.flatpaks = set()
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def fake_run(self, argv, timeout):
        self.calls.append(argv)
        name = os.path.basename(argv[0])
        if name == "flatpak":
            return (0 if argv[1:3] == ["info", argv[2]] and argv[2] in self.flatpaks else 1), ""
        if name.startswith("qdbus"):
            return 0, ""
        return None, "no such command"

    def items(self):
        ctx = D.Ctx(env={"HOME": str(self.home), "PATH": "/nowhere", "CLAUDE_KITTY_CONFIG": str(self.cfg)},
                    os_name="Linux", run=self.fake_run, which=self.which_map.get, repo=ROOT)
        rep = D.Report()
        D.check_desktop(ctx, rep)
        return {i["name"]: i for i in rep.items}

    def test_native_chrome(self):
        self.which_map["google-chrome"] = "/usr/bin/google-chrome"
        it = self.items()["Chrome"]
        self.assertEqual(it["status"], "ok")
        self.assertIn("/usr/bin/google-chrome", it["detail"])
        self.assertIn(".local/share/claude-kitty/browser-profiles", it["detail"])
        self.assertNotIn("写して開く", it["detail"])

    def test_flatpak_chrome(self):
        self.which_map["flatpak"] = "/usr/bin/flatpak"
        self.flatpaks.add("com.google.Chrome")
        it = self.items()["Chrome"]
        self.assertEqual(it["status"], "ok")
        self.assertIn("Flatpak の com.google.Chrome", it["detail"])
        self.assertIn(".var/app/com.google.Chrome/claude-profiles", it["detail"])
        self.assertIn("写して開く", it["detail"])  # 注意でなく情報（ok のまま）

    def test_nothing(self):
        it = self.items()["Chrome"]
        self.assertEqual(it["status"], "warn")
        self.assertIn("browser.command", it["fix"])

    def test_configured_but_missing(self):
        self.cfg.write_text(json.dumps({"browser": {"command": ["/nowhere/chrome"]}}))
        it = self.items()["Chrome"]
        self.assertEqual(it["status"], "ng")
        self.assertIn("/nowhere/chrome", it["detail"])

    def test_broken_browser_config_is_ng_in_config_check(self):
        self.cfg.write_text(json.dumps({"browser": {"command": "chrome"}}))
        self.assertEqual(self.items()["Chrome"]["status"], "warn")  # 探し方は既定で続ける
        ctx = D.Ctx(env={"HOME": str(self.home), "PATH": "/nowhere", "CLAUDE_KITTY_CONFIG": str(self.cfg)},
                    os_name="Linux", run=self.fake_run, which=self.which_map.get, repo=ROOT)
        rep = D.Report()
        D.check_config(ctx, rep)
        self.assertEqual({i["name"]: i for i in rep.items}["設定: browser"]["status"], "ng")

    def ctx(self, os_name="Linux"):
        return D.Ctx(env={"HOME": str(self.home), "PATH": "/nowhere", "CLAUDE_KITTY_CONFIG": str(self.cfg)},
                     os_name=os_name, run=self.fake_run, which=self.which_map.get, repo=ROOT)

    def test_off_does_not_look_for_chrome(self):
        self.which_map.update({"flatpak": "/usr/bin/flatpak", "google-chrome": "/usr/bin/google-chrome",
                               "node": "/usr/bin/node"})
        self.flatpaks.add("com.google.Chrome")
        self.cfg.write_text(json.dumps({"browser": {"enabled": False}}))
        items = self.items()
        self.assertNotIn("Chrome", items)
        self.assertEqual((items["browser"]["status"], items["browser"]["detail"]), ("ok", "オフ（browser.enabled: false）"))
        self.assertFalse([c for c in self.calls if os.path.basename(c[0]) == "flatpak"])  # flatpak info も呼ばない
        rep = D.Report()
        D.check_runtimes(self.ctx(), rep)
        self.assertEqual({i["name"]: i for i in rep.items}["node"]["status"], "skip")  # node は claude-browser だけが使う
        self.assertFalse([c for c in self.calls if os.path.basename(c[0]) == "node"])
        rep = D.Report()
        D.check_config(self.ctx(), rep)
        self.assertEqual({i["name"]: i for i in rep.items}["設定: browser"]["status"], "ok")
        rep = D.Report()
        D.check_desktop(self.ctx("Darwin"), rep)  # macOS でも同じ
        self.assertEqual({i["name"]: i for i in rep.items}["browser"]["status"], "ok")
        self.assertNotIn("Chrome", {i["name"] for i in rep.items})

    def test_enabled_true_still_looks_for_chrome(self):
        self.which_map.update({"google-chrome": "/usr/bin/google-chrome", "node": "/usr/bin/node"})
        self.cfg.write_text(json.dumps({"browser": {"enabled": True}}))
        items = self.items()
        self.assertEqual(items["Chrome"]["status"], "ok")
        self.assertNotIn("browser", items)
        rep = D.Report()
        D.check_runtimes(self.ctx(), rep)
        self.assertNotEqual({i["name"]: i for i in rep.items}["node"]["status"], "skip")

    def test_bad_enabled_is_ng_in_config_and_chrome_still_checked(self):
        self.which_map["google-chrome"] = "/usr/bin/google-chrome"
        self.cfg.write_text(json.dumps({"browser": {"enabled": "no"}}))
        self.assertIn("Chrome", self.items())  # オフとは読まない
        rep = D.Report()
        D.check_config(self.ctx(), rep)
        it = {i["name"]: i for i in rep.items}["設定: browser"]
        self.assertEqual(it["status"], "ng")
        self.assertIn("browser.enabled", it["detail"])

    def test_qdbus6_counts_as_kwin(self):
        self.which_map["qdbus6"] = "/usr/bin/qdbus6"
        it = self.items()["KWin"]
        self.assertEqual(it["status"], "ok")
        self.assertIn("qdbus6", it["detail"])

    def test_no_qdbus_is_warning_with_reason(self):
        it = self.items()["KWin"]
        self.assertEqual(it["status"], "warn")
        self.assertIn("qdbus", it["detail"])
        self.assertIn("KDE Plasma", it["fix"])


# ---------- サンドボックスの Chrome から見えない file:// を写して開く（lib/sandboxfs.py） ----------
import sandboxfs as S  # noqa: E402
from types import SimpleNamespace  # noqa: E402

PERMS = """[Context]
shared=ipc;network;
filesystems=xdg-config/gtk-3.0:ro;host-etc;xdg-pictures;xdg-documents;~/.config/dconf:ro;~/research:ro;xdg-download;/run/.heim:ro;

[Session Bus Policy]
filesystems=home;
"""


class FakeRun:
    """run の偽物。answers は (argv の頭のタプル) → (returncode, stdout)。無いものは「入っていない」（OSError）。"""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        for head, (code, out) in self.answers.items():
            if tuple(argv[:len(head)]) == head:
                return SimpleNamespace(returncode=code, stdout=out, stderr="")
        raise FileNotFoundError(argv[0])


class SandboxTmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))
        self.home = self.root / "home"
        self.home.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_for(self, perms=PERMS, user="", system="", docs=None):
        docs = str(self.home / "Documents") if docs is None else docs
        return FakeRun({("flatpak", "info", "--show-permissions"): (0, perms),
                        ("flatpak", "override", "--user", "--show"): (0, user),
                        ("flatpak", "override", "--system", "--show"): (0, system),
                        ("xdg-user-dir", "DOCUMENTS"): (0, docs + "\n"),
                        ("xdg-user-dir", "PICTURES"): (0, str(self.home / "Pictures") + "\n"),
                        ("xdg-user-dir", "DOWNLOAD"): (0, str(self.home) + "\n")})  # 設定が無いとホームが返る


class FlatpakParse(SandboxTmp):
    def test_filesystems_only_from_context(self):
        self.assertEqual(S.filesystems_of(PERMS),
                         ["xdg-config/gtk-3.0:ro", "host-etc", "xdg-pictures", "xdg-documents", "~/.config/dconf:ro",
                          "~/research:ro", "xdg-download", "/run/.heim:ro"])
        self.assertEqual(S.filesystems_of(""), [])
        self.assertEqual(S.filesystems_of(None), [])

    def test_modes_and_negation(self):
        self.assertEqual(S.apply_entries(["home:ro", "~/a:create", "/x/:rw", "!home", "xdg-documents"]),
                         ["~/a", "/x", "xdg-documents"])
        self.assertEqual(S.apply_entries(["!home", "home"]), ["home"])  # 否定のあとに許せば効く
        self.assertEqual(S.apply_entries(["host", "!host:ro", ""]), [])
        self.assertEqual(S.apply_entries(["home", "!~"]), [])          # ~ と home は同じ場所
        self.assertEqual(S.apply_entries(["host", "!host:reset"]), [])  # Flatpak 1.14 以降の書き方

    def test_argv_filesystems(self):
        self.assertEqual(S.argv_filesystems(["flatpak", "run", "--filesystem=home", "--branch", "beta",
                                             "--nofilesystem", "~/x", "com.google.Chrome", "--filesystem=host"]),
                         ["home", "!~/x"])  # アプリ ID より後ろはアプリへの引数
        self.assertEqual(S.argv_filesystems(["google-chrome"]), [])
        self.assertEqual(S.argv_filesystems(None), [])

    def test_entry_path_kinds(self):
        env = {"XDG_RUNTIME_DIR": "/run/user/1"}
        xdg = S.XdgDirs(self.home, env, self.run_for())
        h = str(self.home)
        self.assertEqual(S.entry_path("host", self.home, env, xdg), "/")
        self.assertEqual(S.entry_path("home", self.home, env, xdg), h)
        self.assertEqual(S.entry_path("~", self.home, env, xdg), h)
        self.assertEqual(S.entry_path("~/research", self.home, env, xdg), h + "/research")
        self.assertEqual(S.entry_path("/data/x", self.home, env, xdg), "/data/x")
        self.assertEqual(S.entry_path("xdg-documents", self.home, env, xdg), h + "/Documents")
        self.assertEqual(S.entry_path("xdg-documents/sub", self.home, env, xdg), h + "/Documents/sub")
        self.assertIsNone(S.entry_path("xdg-download", self.home, env, xdg))  # ホームと同じなら無いもの
        self.assertIsNone(S.entry_path("xdg-music", self.home, env, xdg))     # xdg-user-dir が答えない
        self.assertEqual(S.entry_path("xdg-config/gtk-3.0", self.home, env, xdg), h + "/.config/gtk-3.0")
        self.assertEqual(S.entry_path("xdg-config", self.home, {"XDG_CONFIG_HOME": "/cfg"}, xdg), "/cfg")
        self.assertEqual(S.entry_path("xdg-run/pipewire-0", self.home, env, xdg), "/run/user/1/pipewire-0")
        for unknown in ("host-os", "host-etc", "xdg-unknown", "weird"):
            self.assertIsNone(S.entry_path(unknown, self.home, env, xdg), unknown)

    def test_xdg_user_dirs_file_when_command_missing(self):
        (self.home / ".config").mkdir()
        (self.home / ".config/user-dirs.dirs").write_text('XDG_DOCUMENTS_DIR="$HOME/ドキュメント"\n')
        xdg = S.XdgDirs(self.home, {}, FakeRun({}))
        self.assertEqual(xdg.user_dir("DOCUMENTS"), str(self.home / "ドキュメント"))
        self.assertIsNone(xdg.user_dir("MUSIC"))

    def test_xdg_cached_within_one_call(self):
        run = self.run_for()
        xdg = S.XdgDirs(self.home, {}, run)
        xdg.user_dir("DOCUMENTS"); xdg.user_dir("DOCUMENTS")
        self.assertEqual(sum(1 for c in run.calls if c[0] == "xdg-user-dir"), 1)


class FlatpakVisible(SandboxTmp):
    def visible(self, path, run=None, argv=None):
        box = S.Sandbox({"type": "flatpak", "id": "com.google.Chrome"}, self.home, {}, run or self.run_for(), argv)
        return box.visible(path)

    def test_allowed_and_not(self):
        h = self.home
        self.assertTrue(self.visible(h / "research/a.html"))
        self.assertTrue(self.visible(h / "Documents/r.html"))
        self.assertTrue(self.visible(h / ".var/app/com.google.Chrome/claude-show/x/a.html"))  # アプリの場所
        self.assertFalse(self.visible(h / "claude-kitty/docs/relay-spec.html"))
        self.assertFalse(self.visible(h / "researchX/a.html"))  # 名前の頭が同じだけの別のフォルダ
        self.assertFalse(self.visible(h / ".var/app/org.other/a.html"))

    def test_permissions_cached_within_one_call(self):
        run = self.run_for()
        box = S.Sandbox({"type": "flatpak", "id": "com.google.Chrome"}, self.home, {}, run)
        box.visible(self.home / "a"); box.visible(self.home / "b")
        self.assertEqual(sum(1 for c in run.calls if c[:2] == ["flatpak", "info"]), 1)

    def test_home_and_host(self):
        run = self.run_for(perms="[Context]\nfilesystems=home:ro;\n")
        self.assertTrue(self.visible(self.home / "x/a.html", run))
        self.assertFalse(self.visible("/data/a.html", run))
        run = self.run_for(perms="[Context]\nfilesystems=host;\n")
        self.assertTrue(self.visible("/data/a.html", run))
        self.assertTrue(self.visible(self.home / "x/a.html", run))
        self.assertFalse(self.visible("/tmp/a.html", run))  # /tmp はサンドボックスのもの
        self.assertFalse(self.visible("/usr/share/doc/a.html", run))
        self.assertFalse(self.visible("/var/lib/x/a.html", run))  # /var はサンドボックスのもの（ホームは別に足す）

    def test_override_negation(self):
        run = self.run_for(perms="[Context]\nfilesystems=home;\n", user="[Context]\nfilesystems=!home;~/ok;\n")
        self.assertFalse(self.visible(self.home / "x/a.html", run))
        self.assertTrue(self.visible(self.home / "ok/a.html", run))

    def test_command_line_filesystem(self):
        run = self.run_for(perms="[Context]\nfilesystems=\n")
        argv = ["flatpak", "run", "--filesystem=~/proj:ro", "com.google.Chrome"]
        self.assertTrue(self.visible(self.home / "proj/a.html", run, argv))
        self.assertFalse(self.visible(self.home / "proj/a.html", run))

    def test_unreadable_permissions_mean_not_visible(self):
        for run in (FakeRun({}), FakeRun({("flatpak", "info"): (1, "")})):
            self.assertFalse(self.visible(self.home / "research/a.html", run))
            self.assertTrue(self.visible(self.home / ".var/app/com.google.Chrome/a.html", run))

    def test_paths_compared_by_realpath(self):
        # Fedora Atomic 系: /home → /var/home の symlink。どちらの書き方でも同じ判定
        link = self.root / "homelink"
        link.symlink_to(self.home)
        (self.home / "research").mkdir()
        (self.home / "research/a.html").write_text("x")
        box = S.Sandbox({"type": "flatpak", "id": "com.google.Chrome"}, link, {}, self.run_for())
        self.assertTrue(box.visible(link / "research/a.html"))
        self.assertTrue(box.visible(self.home / "research/a.html"))
        self.assertFalse(box.visible(link / "other.html"))


class SnapVisible(SandboxTmp):
    CONNECTED = "Interface  Plug           Slot   Notes\nhome       chromium:home  :home  -\n"
    DISCONNECTED = "Interface  Plug           Slot   Notes\nhome       chromium:home  -      -\n"

    def visible(self, path, out):
        run = FakeRun({("snap", "connections"): (0, out)} if out is not None else {})
        return S.Sandbox({"type": "snap", "id": "chromium"}, self.home, {}, run).visible(path)

    def test_home_interface(self):
        self.assertTrue(self.visible(self.home / "docs/a.html", self.CONNECTED))
        self.assertFalse(self.visible(self.home / "docs/a.html", self.DISCONNECTED))
        self.assertFalse(self.visible(self.home / "docs/a.html", None))   # snap を動かせない
        self.assertFalse(self.visible(self.home / ".hidden/a.html", self.CONNECTED))
        self.assertFalse(self.visible(self.home / "docs/.cache/a.html", self.CONNECTED))  # 保守的に
        self.assertFalse(self.visible("/data/a.html", self.CONNECTED))
        self.assertTrue(self.visible(self.home / "snap/chromium/common/a.html", None))  # 自分の場所


class Resources(SandboxTmp):
    def make_site(self):
        site = self.home / "proj/docs"
        (site / "img").mkdir(parents=True)
        (site / "css").mkdir()
        (site / "img/a.png").write_bytes(b"A")
        (site / "img/b.png").write_bytes(b"B")
        (site / "img/c 1.png").write_bytes(b"C")
        (site / "img/2x.png").write_bytes(b"D")
        (site / "bg.png").write_bytes(b"E")
        (site / "app.js").write_text("1")
        (site / "css/s.css").write_text('@import "t.css"; body{background:url(../bg.png)} .x{background:url("/abs.png")}')
        (site / "css/t.css").write_text(".y{background:url('../img/b.png')}")
        (self.home / "proj/up.png").write_bytes(b"U")
        (site / "index.html").write_text(
            '<html><head><link rel="stylesheet" href="css/s.css?v=2"><script src="app.js"></script>'
            '<style>.z{background:url(img/a.png)}</style></head><body>'
            '<img src="img/a.png#frag"><img src="img/c%201.png" srcset="img/2x.png 2x, data:x 3x">'
            '<img src="../up.png"><img src="/etc/passwd"><img src="file:///x.png"><img src="https://e.org/a.png">'
            '<img src="data:image/png;base64,AA"><img src="missing.png"><a href="other.html">x</a>'
            '<div style="background:url(img/a.png)"></div></body></html>')
        return site

    def test_collect(self):
        site = self.make_site()
        files, outside = S.collect_resources(str(site / "index.html"))
        self.assertEqual(sorted(files), ["app.js", "bg.png", "css/s.css", "css/t.css", "img/2x.png", "img/a.png",
                                         "img/b.png", "img/c 1.png"])
        self.assertEqual(sorted(outside), sorted(["../up.png", "/etc/passwd", "file:///x.png", "/abs.png"]))

    def test_classify_ref(self):
        r = "/s"
        self.assertEqual(S.classify_ref("", r, r), ("skip", None))
        self.assertEqual(S.classify_ref("#top", r, r), ("skip", None))
        self.assertEqual(S.classify_ref("//cdn/x.js", r, r), ("skip", None))
        self.assertEqual(S.classify_ref("mailto:a@b", r, r), ("skip", None))
        self.assertEqual(S.classify_ref("a/../../x", r, r), ("outside", "a/../../x"))

    def test_broken_html_still_parses(self):
        site = self.home / "s"
        site.mkdir()
        (site / "a.png").write_bytes(b"x")
        (site / "i.html").write_text('<img src="a.png"><img src="<<<"><div')
        self.assertEqual(S.collect_resources(str(site / "i.html"))[0], ["a.png"])


class PrepareUrl(SandboxTmp):
    FP = {"type": "flatpak", "id": "com.google.Chrome"}

    def prepare(self, path, sandbox=FP, run=None, **kw):
        notes = []
        url = path if isinstance(path, str) else Path(path).as_uri()
        got = S.prepare_url(url, sandbox, self.home, {}, run=run or self_run(self), warn=notes.append, **kw)
        return got, notes

    def base(self):
        return self.home / ".var/app/com.google.Chrome/claude-show"

    def test_native_chrome_does_not_copy(self):
        f = self.home / "x/a.html"; f.parent.mkdir(); f.write_text("x")
        run = FakeRun({})
        (url, copied), notes = self.prepare(f, sandbox=None, run=run)
        self.assertEqual((url, copied, notes), (f.as_uri(), None, []))
        self.assertEqual(run.calls, [])  # flatpak も呼ばない
        self.assertFalse((self.home / ".var").exists())

    def test_visible_is_unchanged(self):
        f = self.home / "research/a.html"; f.parent.mkdir(); f.write_text("x")
        (url, copied), _ = self.prepare(f)
        self.assertEqual((url, copied), (f.as_uri(), None))
        self.assertFalse(self.base().exists())

    def test_not_file_or_missing_is_unchanged(self):
        for u in ("https://example.com/a.html", "about:blank", (self.home / "nope.html").as_uri()):
            (url, copied), _ = self.prepare(u)
            self.assertEqual((url, copied), (u, None))

    def test_copies_html_and_relative_resources(self):
        site = Resources.make_site(self)
        u = (site / "index.html").as_uri() + "#sec"
        (url, copied), notes = self.prepare(u)
        dest = Path(copied["to"])
        self.assertTrue(str(dest).startswith(str(self.base()) + "/"))
        self.assertEqual(url, dest.as_uri() + "#sec")  # 見出しへのリンクは残す
        self.assertEqual(copied["from"], str(site / "index.html"))
        self.assertTrue((site / "index.html").is_file())  # 元は動かさない
        self.assertIn("元のファイル", copied["note"])
        self.assertEqual(copied["resources"], 8)
        for rel in ("img/a.png", "img/c 1.png", "css/s.css", "css/t.css", "bg.png", "app.js"):
            self.assertTrue((dest.parent / rel).is_file(), rel)
        self.assertFalse((dest.parent.parent / "up.png").exists())
        self.assertEqual(len(notes), 1)  # 写さなかったものは 1 行で
        self.assertIn("../up.png", notes[0])
        self.assertIn("/etc/passwd", notes[0])

    def test_copy_dest_uses_home_as_given(self):
        # 開く場所は $HOME の形（/home → /var/home の symlink でも、プロファイルの置き場と同じ書き方）
        link = self.root / "homelink"
        link.symlink_to(self.home)
        f = self.home / "x/a.html"; f.parent.mkdir(); f.write_text("x")
        url, copied = S.prepare_url(f.as_uri(), self.FP, link, {}, run=self_run(self))
        self.assertTrue(copied["to"].startswith(str(link / ".var/app/com.google.Chrome/claude-show") + "/"))
        self.assertEqual(copied["from"], str(f))
        self.assertTrue(Path(copied["to"]).is_file())

    def test_image_file_is_copied_alone(self):
        f = self.home / "pics/a.png"; f.parent.mkdir(); f.write_bytes(b"P")
        (url, copied), notes = self.prepare(f)
        self.assertEqual(Path(copied["to"]).read_bytes(), b"P")
        self.assertEqual((copied["resources"], notes), (0, []))

    def test_limit_copies_html_only(self):
        site = Resources.make_site(self)
        (url, copied), notes = self.prepare(site / "index.html", limit=3)
        dest = Path(copied["to"])
        self.assertEqual([p.name for p in dest.parent.iterdir()], ["index.html"])
        self.assertEqual(copied["resources"], 0)
        self.assertTrue(any("上限" in n for n in notes), notes)

    def test_recopy_drops_stale_and_cleans_old(self):
        f = self.home / "x/a.html"; f.parent.mkdir(); f.write_text("<img src='i.png'>")
        (f.parent / "i.png").write_bytes(b"1")
        (_, copied), _ = self.prepare(f)
        dest = Path(copied["to"]).parent
        (f.parent / "i.png").unlink()
        old = self.base() / "0123456789ab"; old.mkdir(); (old / "z.html").write_text("z")
        young = self.base() / "aaaaaaaaaaaa"; young.mkdir()
        os.utime(old, (time.time() - 2 * 86400,) * 2)
        (_, copied2), _ = self.prepare(f)
        self.assertEqual(Path(copied2["to"]).parent, dest)  # 同じ元は同じ場所
        self.assertFalse((dest / "i.png").exists())  # 前の写しの残りは消える
        self.assertFalse(old.exists())
        self.assertTrue(young.exists())

    def test_recall_recopies_new_content(self):
        # 元を直して呼び直したら、必ず写し直す（古いコピーを見せない）
        f = self.home / "x/a.html"; f.parent.mkdir(); f.write_text("v1")
        (f.parent / "s.css").write_text("old")
        f.write_text('<link rel="stylesheet" href="s.css">v1')
        (_, c1), _ = self.prepare(f)
        f.write_text('<link rel="stylesheet" href="s.css">v2')
        (f.parent / "s.css").write_text("new")
        (_, c2), _ = self.prepare(f)
        self.assertEqual(c1["to"], c2["to"])
        self.assertIn("v2", Path(c2["to"]).read_text())
        self.assertEqual((Path(c2["to"]).parent / "s.css").read_text(), "new")
        self.assertEqual(c2["from"], str(f))

    def test_copy_failure_opens_original(self):
        f = self.home / "x/a.html"; f.parent.mkdir(); f.write_text("x")
        (self.home / ".var").write_text("not a dir")  # 写す先を作れない
        (url, copied), notes = self.prepare(f)
        self.assertEqual((url, copied), (f.as_uri(), None))
        self.assertIn("写せなかった", notes[0])

    def test_snap_copies_under_common(self):
        f = self.home / ".hidden/a.html"; f.parent.mkdir(); f.write_text("x")
        (url, copied), _ = self.prepare(f, sandbox={"type": "snap", "id": "chromium"}, run=FakeRun({}))
        self.assertTrue(copied["to"].startswith(str(self.home / "snap/chromium/common/claude-show") + "/"))


def self_run(t):
    return t.run_for(perms="[Context]\nfilesystems=~/research:ro;\n")


import time  # noqa: E402


class SandboxOfBrowser(unittest.TestCase):
    def test_sandbox_field(self):
        b, _ = find({"google-chrome": "/usr/bin/google-chrome"})
        self.assertIsNone(b["sandbox"])
        b, _ = find(FLATPAK, flatpaks={"com.google.Chrome"})
        self.assertEqual(b["sandbox"], {"type": "flatpak", "id": "com.google.Chrome"})
        b, _ = find({"chromium": "/snap/bin/chromium"})
        self.assertEqual(b["sandbox"], {"type": "snap", "id": "chromium"})
        b, _ = find(conf={"command": ["flatpak", "run", "--branch=beta", "org.chromium.Chromium"], "profile_root": None})
        self.assertEqual(b["sandbox"], {"type": "flatpak", "id": "org.chromium.Chromium"})
        self.assertIsNone(find()[0]["sandbox"])


FAKE_FLATPAK_PERMS = """#!/bin/sh
echo "$*" >> "$FAKE_LOG/flatpak.calls"
if [ "$1" = info ] && [ "$2" = --show-permissions ]; then
  printf '[Context]\\nfilesystems=xdg-download;~/research:ro;\\n'; exit 0
fi
[ "$1" = override ] && exit 0
[ "$1" = info ] && [ "$2" = com.google.Chrome ] && exit 0
exit 1
"""


class BrowserVisibleUrl(unittest.TestCase):
    """claude-browser の visible_url（open が使う）を、偽の flatpak を PATH に置いて動かす。"""

    def setUp(self):
        import importlib.machinery as m
        import importlib.util as u
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))
        self.home, self.fake, self.log = self.root / "home", self.root / "fake", self.root / "log"
        for d in (self.home, self.fake, self.log):
            d.mkdir()
        (self.fake / "flatpak").write_text(FAKE_FLATPAK_PERMS)
        (self.fake / "flatpak").chmod(0o755)
        loader = m.SourceFileLoader("claude_browser_v", str(ROOT / "bin/claude-browser"))
        self.b = u.module_from_spec(u.spec_from_loader("claude_browser_v", loader))
        loader.exec_module(self.b)
        self.env = {"PATH": str(self.fake), "HOME": str(self.home), "FAKE_LOG": str(self.log)}

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, url):
        for name in ("flatpak", "xdg-user-dir", "snap"):
            q = shutil.which(name, path=self.env["PATH"])
            assert q is None or q.startswith(str(self.root)), f"本物の {name} に届く PATH: {q}"
        err = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stderr(err):
            self.b.setup_browser(self.env, strict=True)
            return self.b.visible_url(url, self.env), err.getvalue()

    def test_flatpak_copies_invisible_file(self):
        f = self.home / "claude-kitty/docs/relay-spec.html"; f.parent.mkdir(parents=True)
        f.write_text('<img src="../x.png">')
        (url, copied), err = self.call(f.as_uri())
        self.assertEqual(self.b.BROWSER["kind"], "flatpak")
        self.assertTrue(copied["to"].startswith(str(self.home / ".var/app/com.google.Chrome/claude-show") + "/"))
        self.assertEqual(url, Path(copied["to"]).as_uri())
        self.assertIn("../x.png", err)
        self.assertIn("info --show-permissions com.google.Chrome", (self.log / "flatpak.calls").read_text())

    def test_flatpak_visible_file_unchanged(self):
        f = self.home / "research/a.html"; f.parent.mkdir(); f.write_text("x")
        (url, copied), err = self.call(f.as_uri())
        self.assertEqual((url, copied, err), (f.as_uri(), None, ""))

    def test_native_chrome_unchanged(self):
        (self.fake / "flatpak").unlink()
        (self.fake / "google-chrome").write_text(FAKE_CHROME)
        (self.fake / "google-chrome").chmod(0o755)
        f = self.home / "x/a.html"; f.parent.mkdir(); f.write_text("x")
        (url, copied), _ = self.call(f.as_uri())
        self.assertEqual((url, copied), (f.as_uri(), None))
        self.assertFalse((self.home / ".var").exists())


class ShowStaged(unittest.TestCase):
    """claude-show は target を元のファイルのままにし、claude-browser の staged・note を足す。"""

    def setUp(self):
        import importlib.machinery as m
        import importlib.util as u
        loader = m.SourceFileLoader("claude_show_c", str(ROOT / "bin/claude-show"))
        self.show = u.module_from_spec(u.spec_from_loader("claude_show_c", loader))
        loader.exec_module(self.show)
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        (self.d / "c.json").write_text(json.dumps({"view": {"guard": []}}))
        self.html = self.d / "r.html"; self.html.write_text("<p>x")
        self.env = {"CLAUDE_KITTY_CONFIG": str(self.d / "c.json"), "KITTY_WINDOW_ID": "4",
                    "KITTY_LISTEN_ON": "unix:/fake/kitty-1"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_staged_passed_through_and_target_kept(self):
        info = {"from": str(self.html), "to": "/h/.var/app/x/claude-show/abc/r.html", "resources": 2,
                "note": S.staged_note("Flatpak の x")}
        line = json.dumps({"key": "k", "target": self.html.as_uri(), **S.staged_fields(info)})
        run = lambda argv, **kw: SimpleNamespace(returncode=0, stdout="noise\n" + line + "\n", stderr="")  # noqa: E731
        code, out = self.show.show(str(self.html), self.env, None, run=run)
        self.assertEqual(code, 0)
        self.assertEqual(out["target"], self.html.as_uri())  # 元のファイルのまま（AI が見失わない）
        self.assertEqual((out["staged"], out["staged_resources"]), (info["to"], 2))
        self.assertIn("元のファイル", out["note"])
        self.assertIn("もう一度", out["note"])
        run = lambda argv, **kw: SimpleNamespace(returncode=0, stdout=json.dumps({"key": "k"}), stderr="")  # noqa: E731
        out = self.show.show(str(self.html), self.env, None, run=run)[1]
        self.assertFalse(set(out) & {"staged", "note"})
        run = lambda argv, **kw: SimpleNamespace(returncode=0, stdout=line, stderr="claude-browser: ../x.png は写していない\n")  # noqa: E731
        out = self.show.show(str(self.html), self.env, None, run=run)[1]
        self.assertEqual(out["warnings"], ["claude-browser: ../x.png は写していない"])  # 呼び手に届く
        self.assertEqual(self.show.browser_staged(""), {})
        self.assertEqual(self.show.browser_staged("[1]"), {})


class ShowOff(unittest.TestCase):
    """browser.enabled が false なら、kitty の中でも HTML・画像・URL を OS の既定のアプリで開く。"""

    def setUp(self):
        import importlib.machinery as m
        import importlib.util as u
        loader = m.SourceFileLoader("claude_show_off", str(ROOT / "bin/claude-show"))
        self.show = u.module_from_spec(u.spec_from_loader("claude_show_off", loader))
        loader.exec_module(self.show)
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.cfg = self.d / "c.json"
        self.html = self.d / "r.html"; self.html.write_text("<p>x")
        self.img = self.d / "a.png"; self.img.write_bytes(b"\x89PNG")
        self.env = {"CLAUDE_KITTY_CONFIG": str(self.cfg), "KITTY_WINDOW_ID": "4",
                    "KITTY_LISTEN_ON": "unix:/fake/kitty-1"}
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def conf(self, browser):
        self.cfg.write_text(json.dumps({"view": {"guard": []}, **({"browser": browser} if browser is not None else {})}))

    def fake_run(self, argv, **kw):
        self.calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def no_kitten(self, *a, **kw):
        raise AssertionError("オフなのに kitty の overlay を使った")

    def test_html_image_url_go_to_xdg_open(self):
        self.conf({"enabled": False})
        with mock.patch.object(self.show, "Kitten", self.no_kitten), mock.patch.object(self.show, "PLATFORM", "linux"):
            for target, want in ((str(self.html), str(self.html)), (str(self.img), str(self.img)),
                                 (self.html.as_uri(), str(self.html)), ("https://example.com/", "https://example.com/")):
                self.calls.clear()
                code, out = self.show.show(target, self.env, None, run=self.fake_run)
                self.assertEqual(code, 0, target)
                self.assertEqual(self.calls, [["xdg-open", want]], target)  # claude-browser は呼ばない
                self.assertEqual(out, {"shown": "xdg-open", "target": want, "via": "browser.enabled: false"})

    def test_other_kind_goes_to_xdg_open_like_outside_kitty(self):
        pdf = self.d / "a.pdf"; pdf.write_bytes(b"%PDF")
        self.conf({"enabled": False})
        with mock.patch.object(self.show, "PLATFORM", "linux"):
            code, out = self.show.show(str(pdf), self.env, None, run=self.fake_run)
        self.assertEqual((code, self.calls), (0, [["xdg-open", str(pdf)]]))
        self.conf({})  # オンなら今まで通り「開き方が分からない種類」
        self.calls.clear()
        code, out = self.show.show(str(pdf), self.env, None, run=self.fake_run)
        self.assertEqual((code, self.calls), (1, []))
        self.assertIn("開き方が分からない", out["error"])

    def test_macos_uses_open(self):
        self.conf({"enabled": False})
        with mock.patch.object(self.show, "Kitten", self.no_kitten), mock.patch.object(self.show, "PLATFORM", "darwin"):
            code, out = self.show.show(str(self.img), self.env, None, run=self.fake_run)
        self.assertEqual((code, self.calls, out["shown"]), (0, [["open", str(self.img)]], "open"))

    def test_opener_failure_is_reported(self):
        self.conf({"enabled": False})
        run = lambda argv, **kw: SimpleNamespace(returncode=4, stdout="", stderr="no handler\n")  # noqa: E731
        with mock.patch.object(self.show, "PLATFORM", "linux"):
            code, out = self.show.show(str(self.html), self.env, None, run=run)
        self.assertEqual((code, out["shown"], out["error"], out["via"]), (1, "xdg-open", "no handler", "browser.enabled: false"))

        def boom(argv, **kw):
            raise OSError("no xdg-open")
        code, out = self.show.show(str(self.html), self.env, None, run=boom)
        self.assertEqual(code, 1)
        self.assertIn("no xdg-open", out["error"])

    def test_missing_file_and_guard_still_checked(self):
        self.conf({"enabled": False})
        code, out = self.show.show(str(self.d / "nope.html"), self.env, None, run=self.fake_run)
        self.assertEqual(code, 1)
        self.assertIn("ファイルが無い", out["error"])
        with mock.patch.object(self.show, "is_guarded", lambda env, **kw: (True, "保護する窓")):
            code, out = self.show.show(str(self.html), self.env, None, run=self.fake_run)
        self.assertEqual(code, 2)
        self.assertEqual(self.calls, [])

    def test_enabled_default_and_bad_value_keep_browser_and_overlay(self):
        # キー無し・true・壊れた値（"false" はオフと読まない）は今まで通り: HTML は claude-browser、画像は overlay
        launched = []

        class FakeKitten:
            def __init__(self, *a):
                pass

            def ls(self):
                return [{"tabs": [{"is_active": True, "windows": [{"id": 4}]}]}]

            def __call__(self, *args):
                launched.append(args)
                return ""
        for browser in (None, {}, {"enabled": True}, {"enabled": "false"}):
            self.conf(browser)
            self.calls.clear()
            launched.clear()
            code, out = self.show.show(str(self.html), self.env, None, run=self.fake_run)
            self.assertEqual(self.calls[0][:2], [str(self.show.BROWSER), "open"], browser)
            self.assertEqual(out["shown"], "browser")
            self.assertNotIn("via", out)
            with mock.patch.object(self.show, "Kitten", FakeKitten), \
                    mock.patch.object(self.show, "find_window", lambda ls, wid: (ls[0], ls[0]["tabs"][0], {"id": wid})):
                code, out = self.show.show(str(self.img), self.env, None, run=self.fake_run)
            self.assertEqual((code, out["shown"]), (0, "overlay"), browser)
            self.assertEqual(len(launched), 1)
            self.assertEqual(len(self.calls), 1)  # 画像で xdg-open は呼ばない

    def test_outside_kitty_unchanged(self):
        self.conf({"enabled": False})
        env = {"CLAUDE_KITTY_CONFIG": str(self.cfg)}
        with mock.patch.object(self.show, "PLATFORM", "linux"):
            code, out = self.show.show(str(self.img), env, None, run=self.fake_run)
        self.assertEqual((code, out), (0, {"shown": "xdg-open", "target": str(self.img)}))

    def test_cli_with_fake_xdg_open(self):
        # 実物の bin/claude-show を、偽の xdg-open を置いた PATH で動かす（呼ばれた引数を記録する）
        self.conf({"enabled": False})
        fake = self.d / "fake"; fake.mkdir()
        log = self.d / "xdg.calls"
        (fake / "xdg-open").write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nexit 0\n')
        (fake / "xdg-open").chmod(0o755)
        env = {**self.env, "PATH": str(fake), "HOME": str(self.d), "XDG_RUNTIME_DIR": str(self.d / "run")}
        for target in (self.html, self.img):
            r = subprocess.run([sys.executable, str(ROOT / "bin/claude-show"), str(target)], env=env,
                               capture_output=True, text=True, timeout=20)
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)
            self.assertEqual((out["shown"], out["target"], out["via"]), ("xdg-open", str(target), "browser.enabled: false"))
        self.assertEqual(log.read_text().splitlines(), [str(self.html), str(self.img)])


if __name__ == "__main__":
    unittest.main(verbosity=1)
