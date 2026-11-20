#!/usr/bin/env python3
"""claude-kitty doctor（lib/doctor.py）と config.example.json の単体テスト（偽の環境だけを使う）。"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tools"))
import doctor as D  # noqa: E402
import merge_hooks as M  # noqa: E402

LS_OK = [{"tabs": [{"layout": "splits", "layout_state": {"class": "Splits", "pairs": {"one": 1, "two": 2}}, "windows": []}]}]
LS_OLD = [{"tabs": [{"layout": "splits", "layout_state": {"class": "Splits"}, "windows": []}]}]


class FakeWorld:
    """一時の HOME に「全部入った」状態を作り、外のコマンドの答えを決める。"""

    def __init__(self, root):
        self.root = Path(root)
        self.home = self.root / "home"
        kbin = self.home / ".local/kitty.app/bin"
        kbin.mkdir(parents=True)
        for n in ("kitty", "kitten"):
            (kbin / n).write_text("")
        self.bin = self.home / ".local/bin"
        self.bin.mkdir(parents=True)
        for p in (ROOT / "bin").iterdir():
            if p.is_file() and os.access(p, os.X_OK):
                (self.bin / p.name).symlink_to(p)
        (self.home / ".claude").mkdir()
        _l, data = M.install({"model": "x"}, str(ROOT), list(M.PARTS))
        (self.home / ".claude/settings.json").write_text(json.dumps(data))
        self.kdir = self.home / ".config/kitty"
        self.kdir.mkdir(parents=True)
        (self.kdir / "kitty.conf").write_text(f"font_size 11\ninclude {ROOT}/kitty/claude-kitty.conf\n")
        (self.kdir / "claude_state_watcher.py").symlink_to(ROOT / "kitty/claude_state_watcher.py")
        udir = self.home / ".config/systemd/user"
        udir.mkdir(parents=True)
        for u in D.UNITS:
            (udir / u).write_text("[Unit]\n")
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.answers = {"kitty": (0, "kitty 0.51.0 created by Kovid Goyal\n"), "ls": (0, json.dumps(LS_OK)),
                        "is-enabled": (0, "enabled\n"), "is-active": (0, "active\n"), "node": (0, "v22.3.0\n"),
                        "qdbus": (0, ""), "flatpak": (0, "")}
        self.which_map = {"systemctl": "/usr/bin/systemctl", "node": "/usr/bin/node", "qdbus": "/usr/bin/qdbus",
                          "flatpak": "/usr/bin/flatpak"}
        self.calls = []

    def env(self, **extra):
        e = {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin", "XDG_RUNTIME_DIR": str(self.run_dir),
             "KITTY_LISTEN_ON": "unix:/fake/kitty-1", "CLAUDE_KITTY_CONFIG": str(self.root / "config.json"),
             "CLAUDE_LANG_CHECK_LANGUAGE": "ja"}
        e.update(extra)
        return {k: v for k, v in e.items() if v is not None}

    def run(self, argv, timeout):
        self.calls.append(argv)
        name = os.path.basename(argv[0])
        if name == "kitten":
            return self.answers["ls"]
        if name == "systemctl":
            return self.answers[argv[2]]
        return self.answers.get(name, (None, "no such command"))

    def ctx(self, os_name="Linux", python=(3, 12, 1), **env):
        return D.Ctx(env=self.env(**env), os_name=os_name, run=self.run, which=self.which_map.get, repo=ROOT, python=python)


class Doctor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.w = FakeWorld(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def result(self, ctx, *args):
        out = io.StringIO()
        rc = D.main(list(args), ctx=ctx, out=out)
        return rc, out.getvalue()

    def by_name(self, ctx):
        return {i["name"]: i for i in D.run_checks(ctx).items}

    def test_all_good(self):
        rc, text = self.result(self.w.ctx())
        self.assertEqual(rc, 0, text)
        self.assertIn("問題 0 件、注意 0 件", text)
        self.assertNotIn("NG", text)
        self.assertIn("OK   kitty: 0.51.0", text)
        self.assertIn("OK   layout_state.pairs", text)
        self.assertIn("OK   hook: claude-guard: 入っている", text)
        self.assertIn("OK   設定ファイル: 無い", text)
        # kitty には ls だけ、systemd には is-enabled / is-active だけ
        for argv in self.w.calls:
            if os.path.basename(argv[0]) == "kitten":
                self.assertEqual(argv[1:], ["@", "--to", "unix:/fake/kitty-1", "ls"])
            if os.path.basename(argv[0]) == "systemctl":
                self.assertIn(argv[2], ("is-enabled", "is-active"))

    def test_json(self):
        rc, text = self.result(self.w.ctx(), "--json")
        data = json.loads(text)
        self.assertEqual((rc, data["ok"], data["problems"]), (0, True, 0))
        self.assertTrue(all(set(i) == {"status", "name", "detail", "fix"} for i in data["checks"]))
        self.assertEqual(self.result(self.w.ctx(), "--bogus")[0], 2)

    def test_problems_are_reported_with_fixes(self):
        os.unlink(self.w.bin / "claude-view")
        (self.w.bin / "claude-show").unlink()
        (self.w.bin / "claude-show").write_text("other")
        data = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "claude-state hook Stop"}]}]}}
        (self.w.home / ".claude/settings.json").write_text(json.dumps(data))
        (self.w.kdir / "kitty.conf").write_text("font_size 11\n")
        (self.w.kdir / "claude_state_watcher.py").unlink()
        self.w.answers.update({"ls": (1, "Error: remote control is disabled"), "is-enabled": (1, "disabled\n"),
                               "node": (0, "v18.0.0\n"), "qdbus": (1, ""), "flatpak": (1, "")})
        (self.w.root / "config.json").write_text('{"view": {"left": ""}, "lang": {"threshold": 5}, "roster": {"generations": 0}}')
        ctx = self.w.ctx(python=(3, 9, 0))
        rc, text = self.result(ctx)
        self.assertEqual(rc, 1)
        items = self.by_name(ctx)
        expect_ng = {"kitten @ ls": "install.sh --kitty", "kitty.conf のリモート操作": "install.sh --kitty",
                     "python3": "3.10", "コマンドのリンク": "install.sh --links",
                     "hook: claude-state（状態の色）": "--hooks=state",
                     "systemd: claude-state-watch.service": "enable --now", "設定: view": "docs/config.md",
                     "設定: lang": "docs/config.md", "設定: roster": "docs/config.md"}
        for name, fix in expect_ng.items():
            self.assertEqual(items[name]["status"], "ng", name)
            self.assertIn(fix, items[name]["fix"], name)
        self.assertIn("claude-view", items["コマンドのリンク"]["detail"])
        self.assertIn("別のファイル: claude-show", items["コマンドのリンク"]["detail"])
        for name in ("node", "KWin", "Chrome", "hook: claude-guard", "kitty.conf の watcher", "kitty.conf の filter_notification"):
            self.assertEqual(items[name]["status"], "warn", name)
        self.assertEqual(items["設定: state"]["status"], "ok")
        # 1 行ずつ、直し方は → の後
        line = next(x for x in text.splitlines() if x.startswith("NG   コマンドのリンク"))
        self.assertIn("  → tools/install.sh --links", line)

    def test_lang_is_only_a_warning(self):
        # lang は --hooks=lang で入れる人だけのもの。入っていなくても、言語が無くても NG にしない
        _l, data = M.install({}, str(ROOT), ["state", "guard"])
        (self.w.home / ".claude/settings.json").write_text(json.dumps(data))
        rc, text = self.result(self.w.ctx())
        self.assertEqual(rc, 0, text)
        self.assertIn("注意 hook: claude-lang-check: 入っていない  → 使うなら tools/install.sh --hooks=lang", text)
        _l, data = M.install({}, str(ROOT), list(M.PARTS))
        (self.w.home / ".claude/settings.json").write_text(json.dumps(data))
        items = self.by_name(self.w.ctx(CLAUDE_LANG_CHECK_LANGUAGE=None))
        self.assertEqual(items["hook: claude-lang-check の言語"]["status"], "warn")

    def test_old_kitty_without_pairs(self):
        self.w.answers["ls"] = (0, json.dumps(LS_OLD))
        items = self.by_name(self.w.ctx())
        self.assertEqual(items["layout_state.pairs"]["status"], "ng")
        self.assertIn("0.51", items["layout_state.pairs"]["fix"])

    def test_no_running_kitty_is_only_a_warning(self):
        items = self.by_name(self.w.ctx(KITTY_LISTEN_ON=None))
        self.assertEqual(items["kitten @ ls"]["status"], "warn")
        self.assertEqual(items["layout_state.pairs"]["status"], "warn")

    def test_missing_kitty(self):
        shutil.rmtree(self.w.home / ".local/kitty.app")
        items = self.by_name(self.w.ctx())
        self.assertEqual(items["kitty"]["status"], "ng")
        self.assertIn("SETUP.md", items["kitty"]["fix"])

    def test_broken_settings_json(self):
        (self.w.home / ".claude/settings.json").write_text("{nope")
        items = self.by_name(self.w.ctx())
        self.assertEqual(items["settings.json"]["status"], "ng")

    def test_service_enabled_but_failed(self):
        self.w.answers["is-active"] = (3, "failed\n")
        items = self.by_name(self.w.ctx())
        self.assertEqual(items["systemd: claude-state-watch.service"]["status"], "ng")
        self.assertIn("journalctl", items["systemd: claude-state-watch.service"]["fix"])

    def test_unit_not_installed_is_warning(self):
        (self.w.home / ".config/systemd/user/claude-session-stats.timer").unlink()
        items = self.by_name(self.w.ctx())
        self.assertEqual(items["systemd: claude-session-stats.timer"]["status"], "warn")

    def test_macos(self):
        os.makedirs(self.w.home / "Applications/Google Chrome.app")
        rc, text = self.result(self.w.ctx(os_name="Darwin"))
        self.assertEqual(rc, 0, text)
        items = self.by_name(self.w.ctx(os_name="Darwin"))
        self.assertEqual(items["systemd"]["status"], "skip")
        self.assertEqual(items["KWin"]["status"], "skip")
        self.assertEqual(items["Chrome"]["status"], "ok")
        self.assertFalse([a for a in self.w.calls if os.path.basename(a[0]) in ("systemctl", "qdbus", "flatpak")])

    def test_kitty_conf_include_and_relative_watcher(self):
        # include を辿る（相対パスは設定ディレクトリから）、最後の listen_on が効く
        (self.w.kdir / "extra.conf").write_text("listen_on none\n")
        (self.w.kdir / "kitty.conf").write_text(f"include {ROOT}/kitty/claude-kitty.conf\ninclude extra.conf\n")
        items = self.by_name(self.w.ctx())
        self.assertEqual(items["kitty.conf のリモート操作"]["status"], "ng")
        (self.w.kdir / "kitty.conf").write_text(f"include extra.conf\ninclude {ROOT}/kitty/claude-kitty.conf\n")
        items = self.by_name(self.w.ctx())
        self.assertEqual(items["kitty.conf のリモート操作"]["status"], "ok")
        self.assertEqual(items["kitty.conf の watcher"]["status"], "ok")
        (self.w.kdir / "kitty.conf").write_text("include kitty.conf\n")       # 循環しても止まる
        self.assertEqual(self.by_name(self.w.ctx())["kitty.conf のリモート操作"]["status"], "ng")

    def test_pid_check_without_proc(self):
        # /proc の無い macOS でもソケットの kitty が生きているかを見分ける（signal 0）
        import statewatch
        self.assertTrue(statewatch._proc_exists(os.getpid()))
        p = subprocess.Popen(["true"])
        p.wait()
        self.assertFalse(statewatch._proc_exists(p.pid))
        self.assertFalse(statewatch._proc_exists(2 ** 40))

    def test_a_crashing_check_does_not_hide_the_rest(self):
        def boom(ctx, rep):
            raise RuntimeError("x")
        old = D.CHECKS
        D.CHECKS = [boom] + old
        try:
            rep = D.run_checks(self.w.ctx())
        finally:
            D.CHECKS = old
        self.assertEqual(rep.items[0]["status"], "ng")
        self.assertGreater(len(rep.items), 5)

    def test_cli_entry(self):
        # claude-kitty doctor が lib/doctor.py を呼ぶ（偽の HOME。kitty が無いので NG で 1）
        # 本物の qdbus・flatpak・Chrome に届かないよう、PATH は起動に要るものへのリンクだけにする
        tools = self.w.root / "cli-tools"
        tools.mkdir()
        for name in ("python3", "dirname", "readlink"):
            (tools / name).symlink_to(shutil.which(name))
        env = {"HOME": str(self.w.root / "empty"), "PATH": str(tools), "XDG_RUNTIME_DIR": str(self.w.run_dir),
               "CLAUDE_KITTY_CONFIG": str(self.w.root / "none.json")}
        r = subprocess.run([str(ROOT / "bin/claude-kitty"), "doctor", "--json"], env=env, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertFalse(json.loads(r.stdout)["ok"])


class ConfigExample(unittest.TestCase):
    """config.example.json の全部の節が、各道具の読み込みで問題なく読め、docs/config.md に全部の項目がある。"""

    PATH = ROOT / "config.example.json"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {"HOME": self.tmp.name, "CLAUDE_KITTY_CONFIG": str(self.PATH)}
        self.data = json.loads(self.PATH.read_text())

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_section_present(self):
        self.assertEqual(set(self.data), {"view", "kitty", "browser", "state", "lang", "guard_hook", "session_stats", "roster"})

    def test_loaders_read_example(self):
        from config import load_roster_config, load_state_config, load_view_config
        from guardhook import load_guard_config
        from langcheck import load_lang_config
        from sessionstats import DEFAULTS, load_settings
        v = load_view_config(self.env, self.PATH)
        self.assertEqual((v["left"], v["restore"], v["guard"]), ("main", [["dev-a"], ["dev-b"]], []))
        s = load_state_config(self.env, self.PATH)
        self.assertEqual(s["base"], "#232627")
        self.assertEqual(s["colors"]["waiting"], {"background": "#2e2a22", "name": 208})
        self.assertEqual(s["notice"], {"contains": [], "prefix": []})  # 既定と同じ（印は例として docs/config.md に）
        from config import load_browser_config
        self.assertEqual(load_browser_config(self.env, self.PATH), {"command": None, "profile_root": None})
        lang, problems = load_lang_config(self.env, self.PATH)
        self.assertEqual(problems, [])
        self.assertEqual(lang, self.data["lang"])
        guards, problems = load_guard_config(self.env, self.PATH)
        self.assertEqual(problems, [])
        g = guards[0]
        self.assertEqual((g["name"], g["enabled"], len(g["commands"]), len(g["allow_commands"])), ("presenting", False, 3, 1))
        self.assertEqual(set(self.data["guard_hook"]["guards"]["presenting"]) - {"enabled"},
                         {k for k in g if k not in ("name", "enabled")})                # 全項目を書いている
        st = load_settings(self.env, self.PATH)
        self.assertEqual(st["master"], "main")
        self.assertEqual(set(self.data["session_stats"]), set(DEFAULTS))
        r = load_roster_config(self.env, self.PATH)
        self.assertEqual(r, self.data["roster"])
        from config import load_kitty_config
        self.assertEqual(load_kitty_config(self.env, self.PATH), self.data["kitty"])

    def test_example_guard_blocks_nothing_when_copied(self):
        import guardhook as gh
        data = {"tool_name": "Bash", "tool_input": {"command": "claude-view all"}, "cwd": "/", "session_id": "s"}
        self.assertIsNone(gh.evaluate(data, self.env, path=self.PATH))

    def test_docs_mention_every_key(self):
        doc = (ROOT / "docs/config.md").read_text()
        for section, body in self.data.items():
            self.assertIn(f"## {section}", doc)
            keys = list(body)
            if section == "guard_hook":
                keys = list(body["guards"]["presenting"])
            for k in keys:
                self.assertTrue(re.search(rf"`{re.escape(k)}[`.]", doc), f"{section}.{k} が docs/config.md に無い")
        for k in self.data["state"]["colors"]:
            self.assertIn(f"`{k}`", doc)

    def test_defaults_in_docs_match_code(self):
        # 表の「既定」の列が、コードの既定と食い違わない（よく変わるものだけ）
        from config import DEFAULT_KITTY, DEFAULT_ROSTER, DEFAULT_VIEW
        from langcheck import DEFAULTS as LANG
        doc = (ROOT / "docs/config.md").read_text()

        def default_of(key, section):
            part = doc.split(f"## {section}", 1)[1].split("\n## ", 1)[0]
            m = re.search(rf"^\| `{re.escape(key)}` \| ([^|]+) \|", part, re.M)
            return m.group(1).strip() if m else None
        for k, v in LANG.items():
            self.assertEqual(default_of(k, "lang"), f"`{json.dumps(v)}`", k)
        for k, v in DEFAULT_KITTY.items():
            self.assertEqual(default_of(k, "kitty"), f"`{json.dumps(v)}`", k)
        for k, v in DEFAULT_ROSTER.items():
            self.assertEqual(default_of(k, "roster"), f"`{json.dumps(v)}`", k)
        self.assertEqual(default_of("left", "view"), f"`{json.dumps(DEFAULT_VIEW['left'])}`")


if __name__ == "__main__":
    unittest.main(verbosity=1)
