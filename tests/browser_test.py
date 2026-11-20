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
NONE = {"command": None, "profile_root": None}


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
        self.assertEqual(got, {"command": ["flatpak", "run", "com.google.Chrome"], "profile_root": "~/p"})

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

    def test_flatpak_chrome(self):
        self.which_map["flatpak"] = "/usr/bin/flatpak"
        self.flatpaks.add("com.google.Chrome")
        it = self.items()["Chrome"]
        self.assertEqual(it["status"], "ok")
        self.assertIn("Flatpak の com.google.Chrome", it["detail"])
        self.assertIn(".var/app/com.google.Chrome/claude-profiles", it["detail"])

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


if __name__ == "__main__":
    unittest.main(verbosity=1)
