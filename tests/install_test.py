#!/usr/bin/env python3
"""tools/merge_hooks.py と tools/install.sh の単体テスト（一時の HOME と偽の systemctl だけを使う）。"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import merge_hooks as M  # noqa: E402


def main_quiet(argv):
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        return M.main(argv)

INSTALL = ROOT / "tools" / "install.sh"
REPO = Path(os.path.realpath(ROOT))
COMMANDS = sorted(p.name for p in (ROOT / "bin").iterdir() if p.is_file() and os.access(p, os.X_OK))

FAKE_SYSTEMCTL = """#!/bin/sh
# 偽の systemctl: enable の状態を $FAKE_SYSTEMD/en.<unit> で持ち、呼ばれた引数を calls に残す
d=${FAKE_SYSTEMD:?}; mkdir -p "$d"; echo "$*" >> "$d/calls"
[ "$1" = --user ] && shift
case "$1" in
show-environment|daemon-reload|restart) exit 0 ;;
is-enabled) [ -e "$d/en.$2" ] && { echo enabled; exit 0; }; echo disabled; exit 1 ;;
is-active) [ "$2" = --quiet ] && shift; [ -e "$d/en.$2" ] && exit 0; exit 3 ;;
enable) shift; [ "$1" = --now ] && shift; touch "$d/en.$1" ;;
disable) shift; [ "$1" = --now ] && shift; rm -f "$d/en.$1" ;;
esac
"""


def snapshot(root):
    """root の下の全部（リンクは指す先、ファイルは中身）。バックアップと偽の systemctl の記録は除く。"""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
            p = os.path.join(dirpath, name)
            rel = os.path.relpath(p, root)
            if ".bak-" in name or rel.startswith(("fake", "sd")):
                continue
            out[rel] = ("link", os.readlink(p)) if os.path.islink(p) else ("file", Path(p).read_bytes())
    return out


class MergeHooks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.settings = self.dir / "settings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_identity_ignores_path_form(self):
        a = M.identity("claude-state hook Stop")
        self.assertEqual(a, M.identity("~/.local/bin/claude-state hook Stop"))
        self.assertEqual(a, M.identity("'/x y/claude-kitty/bin/claude-state' hook Stop"))
        self.assertNotEqual(a, M.identity("claude-state hook StopFailure"))
        self.assertEqual(a, M.identity("FOO=1 claude-state hook Stop"))
        self.assertIsNone(M.identity("'unclosed"))
        self.assertIsNone(M.identity(None))
        self.assertIsNone(M.identity(""))

    def test_install_keeps_existing_and_does_not_duplicate(self):
        data = {"model": "opus", "permissions": {"allow": ["Bash(ls)"]},
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "~/.local/bin/claude-state hook Stop"},
                                              {"type": "command", "command": "my-own-hook"}]}],
                          "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "other"}]}]}}
        lines, new = M.install(data, "/r", ["state", "lang", "guard"])
        self.assertEqual(new["model"], "opus")
        self.assertEqual(new["permissions"], {"allow": ["Bash(ls)"]})
        self.assertEqual(new["hooks"]["Stop"][0], data["hooks"]["Stop"][0])        # 既存の組はそのまま
        self.assertEqual(new["hooks"]["PreToolUse"][0], data["hooks"]["PreToolUse"][0])
        stops = [h["command"] for g in new["hooks"]["Stop"] for h in g["hooks"]]
        self.assertEqual(sum(M.identity(c) == ("claude-state", ("hook", "Stop")) for c in stops), 1)
        self.assertIn("/r/bin/claude-lang-check", stops)
        self.assertIn("= Stop: claude-state hook Stop（入っている）", lines)
        self.assertEqual(sum(1 for line in lines if line.startswith("+ ")), 13)
        guard = [g for g in new["hooks"]["PreToolUse"] if g.get("matcher") == "Bash|Edit|Write|NotebookEdit"]
        self.assertEqual(guard[0]["hooks"][0], {"type": "command", "command": "/r/bin/claude-guard hook", "timeout": 10})
        self.assertNotIn("matcher", next(g for g in new["hooks"]["SessionStart"]))
        # 2 回目は何も足さない
        lines2, again = M.install(new, "/other", ["state", "lang", "guard"])
        self.assertEqual(again, new)
        self.assertTrue(all(line.startswith("= ") for line in lines2))
        self.assertEqual(M.status(new), dict({p: ("all", []) for p in M.PARTS}, relay=("none", ["UserPromptSubmit", "Stop", "SessionStart"])))

    def test_parts_are_selectable(self):
        _lines, new = M.install({}, "/r", ["guard"])
        self.assertEqual(list(new["hooks"]), ["PreToolUse"])
        st = M.status(new)
        self.assertEqual((st["guard"][0], st["state"][0], st["lang"][0]), ("all", "none", "none"))
        with self.assertRaises(ValueError):
            M.parse_parts("state,bogus")
        self.assertEqual(M.parse_parts(""), ["state", "lang", "guard"])

    def test_partial_status(self):
        data = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "claude-state hook Stop"}]}]}}
        state, missing = M.status(data)["state"]
        self.assertEqual(state, "some")
        self.assertIn("SessionEnd", missing)
        self.assertNotIn("Stop", missing)

    def test_uninstall_removes_only_ours(self):
        base = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "my-own-hook"}]}]}, "theme": "dark"}
        _l, installed = M.install(base, "/r", ["state", "lang", "guard"])
        # 手で入れた形の同じ hook も外す
        installed["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": "~/.local/bin/claude-lang-check"}]})
        lines, out = M.uninstall(installed, ["state", "lang", "guard"])
        self.assertEqual(out, base)
        self.assertIn("- Stop: claude-lang-check", lines)
        lines2, out2 = M.uninstall(out, ["state", "lang", "guard"])
        self.assertEqual(out2, base)
        self.assertTrue(all(line.startswith("= ") for line in lines2))
        # 全部が ours なら hooks のキーも消える
        _l, only = M.install({}, "/r", ["lang"])
        self.assertEqual(M.uninstall(only, ["lang"])[1], {})
        # 選んだ部品だけを外す
        _l, both = M.install({}, "/r", ["lang", "guard"])
        self.assertEqual(M.status(M.uninstall(both, ["lang"])[1])["guard"][0], "all")

    def test_broken_json_stops_without_writing(self):
        for text in ("{not json", "[1, 2]", '{"hooks": []}', '{"hooks": {"Stop": {}}}', '{"hooks": {"Stop": [1]}}'):
            with self.subTest(text=text):
                self.settings.write_text(text)
                with self.assertRaises(M.Broken):
                    M.load(self.settings)
                rc = main_quiet(["--settings", str(self.settings), "--repo", "/r"])
                self.assertEqual(rc, 2)
                self.assertEqual(self.settings.read_text(), text)
                self.assertEqual(list(self.dir.glob("*.bak-*")), [])

    def test_missing_and_empty_file(self):
        self.assertEqual(M.load(self.dir / "none.json"), {})
        self.settings.write_text("  \n")
        self.assertEqual(M.load(self.settings), {})

    def test_write_backs_up_and_keeps_symlink_and_mode(self):
        real = self.dir / "real" / "settings.json"
        real.parent.mkdir()
        real.write_text('{"a": 1}')
        os.chmod(real, 0o640)
        self.settings.symlink_to(real)
        rc = main_quiet(["--settings", str(self.settings), "--repo", "/r", "--parts", "lang"])
        self.assertEqual(rc, 0)
        self.assertTrue(self.settings.is_symlink())
        data = json.loads(real.read_text())
        self.assertEqual(data["a"], 1)
        self.assertEqual(M.status(data)["lang"][0], "all")
        self.assertEqual(stat.S_IMODE(real.stat().st_mode), 0o640)
        backups = list(real.parent.glob("settings.json.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), '{"a": 1}')
        self.assertEqual([p.name for p in real.parent.iterdir() if p.name.startswith(".")], [])  # 一時ファイルが残らない
        # 変えるものが無ければ書かない（バックアップも増えない）
        main_quiet(["--settings", str(self.settings), "--repo", "/r", "--parts", "lang"])
        self.assertEqual(len(list(real.parent.glob("settings.json.bak-*"))), 1)

    def test_dry_run_writes_nothing(self):
        self.settings.write_text("{}")
        main_quiet(["--settings", str(self.settings), "--repo", "/r", "--dry-run"])
        self.assertEqual(self.settings.read_text(), "{}")

    def test_quotes_repo_path_with_space(self):
        _l, new = M.install({}, "/a b", ["guard"])
        cmd = new["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertEqual(cmd, "'/a b/bin/claude-guard' hook")
        self.assertEqual(M.identity(cmd), ("claude-guard", ("hook",)))


class Install(unittest.TestCase):
    """install.sh を一時の HOME で流す。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))
        self.home = self.root / "home"
        self.home.mkdir()
        fake = self.root / "fake"
        fake.mkdir()
        self.systemctl = fake / "systemctl"
        self.systemctl.write_text(FAKE_SYSTEMCTL)
        self.systemctl.chmod(0o755)
        self.sd = self.root / "sd"
        self.bin = self.home / ".local" / "bin"
        self.settings = self.home / ".claude" / "settings.json"
        self.kdir = self.home / ".config" / "kitty"
        self.udir = self.home / ".config" / "systemd" / "user"

    def tearDown(self):
        self.tmp.cleanup()

    def run_install(self, *args, os_name="Linux", stdin=subprocess.DEVNULL):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home), "LANG": "C.UTF-8",
               "CLAUDE_KITTY_UNAME": os_name, "CLAUDE_KITTY_SYSTEMCTL": str(self.systemctl),
               "FAKE_SYSTEMD": str(self.sd), "TMPDIR": str(self.root)}
        return subprocess.run(["sh", str(INSTALL), *args], env=env, stdin=stdin, capture_output=True, text=True, timeout=120)

    def calls(self):
        try:
            return (self.sd / "calls").read_text().splitlines()
        except FileNotFoundError:
            return []

    def prepare_existing(self):
        """手で入れた設定がある状態: 別の hook、手で入れた同じ hook、自分の kitty.conf、同じ名前の別のファイル。"""
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps({
            "model": "opus", "statusLine": {"type": "command", "command": "x"},
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "~/.local/bin/claude-state hook Stop", "timeout": 10}]}],
                      "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "my-check"}]}]}}, indent=2))
        self.kdir.mkdir(parents=True)
        self.kitty_conf = "font_size 12.0\nbackground #101010"   # 末尾の改行なし
        (self.kdir / "kitty.conf").write_text(self.kitty_conf)
        self.bin.mkdir(parents=True)
        (self.bin / "claude-view").write_text("#!/bin/sh\necho mine\n")

    def test_all_then_again_then_uninstall(self):
        self.prepare_existing()
        before = snapshot(self.home)
        r = self.run_install("--all", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # links: 別のファイルは上書きしない
        self.assertEqual((self.bin / "claude-view").read_text(), "#!/bin/sh\necho mine\n")
        self.assertIn("claude-view に別のファイルがある", r.stdout)
        for name in COMMANDS:
            if name != "claude-view":
                self.assertEqual(os.path.realpath(self.bin / name), str(REPO / "bin" / name), name)
        # hooks: 既存のキーと hook は残り、手で入れた claude-state hook Stop は二重にしない
        data = json.loads(self.settings.read_text())
        self.assertEqual(data["model"], "opus")
        self.assertEqual(data["statusLine"], {"type": "command", "command": "x"})
        self.assertEqual(data["hooks"]["PreToolUse"][0], {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-check"}]})
        stops = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
        self.assertEqual(stops, ["~/.local/bin/claude-state hook Stop"])      # lang は --all に入らない
        st = M.status(data)
        self.assertEqual((st["state"][0], st["guard"][0], st["lang"][0]), ("all", "all", "none"))
        self.assertEqual(len(list(self.settings.parent.glob("settings.json.bak-*"))), 1)
        # kitty: 自分の設定の後ろに include の 1 行
        conf = (self.kdir / "kitty.conf").read_text()
        self.assertTrue(conf.startswith(self.kitty_conf + "\n# claude-kitty:"), conf)
        self.assertTrue(conf.endswith(f"\ninclude {REPO}/kitty/claude-kitty.conf\n"))
        self.assertEqual(os.path.realpath(self.kdir / "claude_state_watcher.py"), str(REPO / "kitty/claude_state_watcher.py"))
        self.assertTrue((self.kdir / "claude3.session").is_symlink())
        # systemd: ユニットはリポジトリの場所を指し、enable --now
        watch = (self.udir / "claude-state-watch.service").read_text()
        self.assertIn(f"ExecStart=/usr/bin/python3 {REPO}/bin/claude-state watch", watch)
        self.assertNotIn("%h/claude-kitty", watch)
        self.assertTrue(watch.startswith("# claude-kitty: tools/install.sh"))
        for u in ("claude-state-watch.service", "claude-roster-save.timer", "claude-session-stats.timer"):
            self.assertIn(f"--user enable --now {u}", self.calls())
        self.assertIn("--user daemon-reload", self.calls())

        # 2 回目: 何も変えない
        after = snapshot(self.home)
        n_calls = len(self.calls())
        r2 = self.run_install("--all", "--yes")
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertIn("変えるものは無い", r2.stdout)
        self.assertEqual(snapshot(self.home), after)
        self.assertFalse([c for c in self.calls()[n_calls:] if not c.startswith(("--user is-", "--user show-"))])

        # 外す: 入れる前と同じになる（kitty.conf の末尾の改行だけは足したまま）
        r3 = self.run_install("--all", "--uninstall", "--yes")
        self.assertEqual(r3.returncode, 0, r3.stdout + r3.stderr)
        now = snapshot(self.home)
        data = json.loads(self.settings.read_text())
        self.assertEqual(data["model"], "opus")
        self.assertEqual(data["hooks"], {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "my-check"}]}]})
        self.assertEqual((self.kdir / "kitty.conf").read_text(), self.kitty_conf + "\n")
        self.assertEqual((self.bin / "claude-view").read_text(), "#!/bin/sh\necho mine\n")
        self.assertEqual(sorted(p.name for p in self.bin.iterdir()), ["claude-view"])
        self.assertEqual(sorted(p.name for p in self.udir.iterdir()), [])
        self.assertFalse(list(self.sd.glob("en.*")))
        for k in before:
            if k not in (".claude/settings.json", ".config/kitty/kitty.conf"):
                self.assertEqual(now.get(k), before[k], k)
        r4 = self.run_install("--all", "--uninstall", "--yes")
        self.assertIn("変えるものは無い", r4.stdout)

    def test_dry_run_and_no_tty_change_nothing(self):
        self.prepare_existing()
        before = snapshot(self.home)
        r = self.run_install("--all", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--dry-run", r.stdout)
        self.assertIn("+ ", r.stdout)
        self.assertEqual(snapshot(self.home), before)
        r = self.run_install()                       # 部品なし = 全部。端末でなく --yes も無い
        self.assertEqual(r.returncode, 1)
        self.assertIn("--yes", r.stderr)
        self.assertEqual(snapshot(self.home), before)
        self.assertFalse([c for c in self.calls() if not c.startswith(("--user is-", "--user show-"))])

    def test_confirmation_from_terminal_answer(self):
        # 端末の代わりに、答えを渡しても端末でなければ確認しない（誤って y を流し込まない）
        r = self.run_install("--links", stdin=subprocess.PIPE)
        self.assertEqual(r.returncode, 1)
        self.assertFalse(self.bin.exists())

    def test_broken_settings_stops_everything(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text("{broken")
        r = self.run_install("--all", "--yes")
        self.assertEqual(r.returncode, 1)
        self.assertIn("JSON として読めない", r.stdout)
        self.assertEqual(self.settings.read_text(), "{broken")
        self.assertFalse(self.bin.exists())           # ほかの部品も行わない
        self.assertFalse((self.kdir / "kitty.conf").exists())

    def test_lang_only_when_named_and_uninstall_all_removes_it(self):
        r = self.run_install("--hooks", "--yes")
        self.assertEqual(M.status(json.loads(self.settings.read_text()))["lang"][0], "none")
        self.assertNotIn("claude-lang-check", r.stdout)
        r = self.run_install("--hooks=lang", "--yes")
        self.assertEqual(M.status(json.loads(self.settings.read_text()))["lang"][0], "all")
        self.assertIn("lang.language が無いので、claude-lang-check は何もしない", r.stdout)   # 入れるときに気づける
        cfg = self.home / ".config/claude-kitty/config.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text('{"lang": {"language": "ja"}}')
        self.run_install("--hooks=lang", "--uninstall", "--yes")
        r = self.run_install("--hooks=lang", "--yes")
        self.assertNotIn("lang.language が無い", r.stdout)
        self.assertEqual(self.run_install("--hooks=", "--yes").returncode, 2)       # 空の指定は誤り
        self.run_install("--all", "--uninstall", "--yes")                 # 外すときの --all は lang も外す
        self.assertEqual(json.loads(self.settings.read_text()), {})

    def test_select_parts(self):
        r = self.run_install("--hooks=state,guard", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        st = M.status(json.loads(self.settings.read_text()))
        self.assertEqual((st["state"][0], st["guard"][0], st["lang"][0]), ("all", "all", "none"))
        self.assertFalse(self.bin.exists())
        self.assertFalse(self.kdir.exists())
        self.assertEqual(self.calls(), [])
        r = self.run_install("--hooks=bogus", "--yes")
        self.assertEqual(r.returncode, 2)
        r = self.run_install("--uninstall", "--hooks=guard", "--yes")
        st = M.status(json.loads(self.settings.read_text()))
        self.assertEqual((st["state"][0], st["guard"][0]), ("all", "none"))

    def test_kitty_overrides_are_shown_and_new_conf_is_readable(self):
        self.kdir.mkdir(parents=True)
        (self.kdir / "kitty.conf").write_text("listen_on unix:/tmp/mykitty\nenabled_layouts tall\n")
        r = self.run_install("--kitty", "--dry-run")
        self.assertIn("「listen_on unix:/tmp/mykitty」は claude-kitty.conf の値で上書きされる", r.stdout)
        self.assertIn("「enabled_layouts tall」", r.stdout)
        self.assertNotIn("「allow_remote_control", r.stdout)           # 書いていないキーは出さない
        # 新しく作る kitty.conf は 0644。同じ秒に 2 回書いてもバックアップを消さない
        shutil.rmtree(self.kdir)
        self.run_install("--kitty", "--yes")
        self.assertEqual(stat.S_IMODE((self.kdir / "kitty.conf").stat().st_mode), 0o644)
        self.run_install("--kitty", "--uninstall", "--yes")
        self.run_install("--kitty", "--yes")
        self.run_install("--kitty", "--uninstall", "--yes")
        self.assertGreaterEqual(len(list(self.kdir.glob("kitty.conf.bak-*"))), 3)

    def test_include_with_spaces_counts_as_present(self):
        self.kdir.mkdir(parents=True)
        (self.kdir / "kitty.conf").write_text(f"  include {REPO}/kitty/claude-kitty.conf  \n")
        r = self.run_install("--kitty", "--dry-run")
        self.assertIn("を読み込んでいる", r.stdout)
        self.run_install("--kitty", "--uninstall", "--yes")
        self.assertEqual((self.kdir / "kitty.conf").read_text(), "")

    def test_kitty_conf_linked_to_repo_is_left_alone(self):
        self.kdir.mkdir(parents=True)
        (self.kdir / "kitty.conf").symlink_to(REPO / "kitty" / "kitty.conf")
        r = self.run_install("--kitty", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("リポジトリの kitty/kitty.conf へのリンク", r.stdout)
        self.assertEqual(os.readlink(self.kdir / "kitty.conf"), str(REPO / "kitty" / "kitty.conf"))
        self.assertTrue((self.kdir / "claude_state_watcher.py").is_symlink())
        r = self.run_install("--kitty", "--uninstall", "--yes")
        self.assertEqual(os.readlink(self.kdir / "kitty.conf"), str(REPO / "kitty" / "kitty.conf"))
        self.assertFalse((self.kdir / "claude_state_watcher.py").exists())

    def test_kitty_conf_symlink_elsewhere_edits_target(self):
        dot = self.root / "dotfiles"
        dot.mkdir()
        (dot / "kitty.conf").write_text("font_size 10\n")
        self.kdir.mkdir(parents=True)
        (self.kdir / "kitty.conf").symlink_to(dot / "kitty.conf")
        self.run_install("--kitty", "--yes")
        self.assertTrue((self.kdir / "kitty.conf").is_symlink())
        self.assertIn(f"include {REPO}/kitty/claude-kitty.conf", (dot / "kitty.conf").read_text())
        self.run_install("--kitty", "--uninstall", "--yes")
        self.assertEqual((dot / "kitty.conf").read_text(), "font_size 10\n")

    def test_macos_skips_systemd_and_uses_tmp_socket(self):
        r = self.run_install("--all", "--yes", os_name="Darwin")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("launchd", r.stdout)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.udir.exists())
        conf = (self.kdir / "kitty.conf").read_text()
        self.assertIn(f"include {REPO}/kitty/claude-kitty-macos.conf", conf)
        r = self.run_install("--all", "--uninstall", "--yes", os_name="Darwin")
        self.assertNotIn("include", (self.kdir / "kitty.conf").read_text())

    def test_hand_made_unit_is_not_overwritten(self):
        self.udir.mkdir(parents=True)
        mine = "[Service]\nExecStart=/opt/mine/claude-state watch\n"
        (self.udir / "claude-state-watch.service").write_text(mine)
        r = self.run_install("--systemd", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.udir / "claude-state-watch.service").read_text(), mine)
        self.assertIn("手で入れたもの", r.stdout)
        r = self.run_install("--systemd", "--uninstall", "--yes")
        self.assertEqual((self.udir / "claude-state-watch.service").read_text(), mine)   # 外すときも残す
        self.assertFalse((self.udir / "claude-roster-save.timer").exists())

    def test_unit_written_by_install_is_updated(self):
        self.run_install("--systemd", "--yes")
        p = self.udir / "claude-roster-save.timer"
        p.write_text(p.read_text().replace("5min", "9min"))      # 古い版が書いたもの（印あり）
        r = self.run_install("--systemd", "--yes")
        self.assertIn(f"~ {p}", r.stdout)
        self.assertNotIn("9min", p.read_text())

    def test_repo_under_home_uses_percent_h(self):
        # リポジトリがホームの下なら %h のまま書く（/home と /var/home のような別名に左右されない）
        import shutil
        copy = self.home / "src" / "ck"
        for d in ("bin", "contrib", "kitty", "lib", "tools"):
            shutil.copytree(ROOT / d, copy / d, ignore=shutil.ignore_patterns("__pycache__"))
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "CLAUDE_KITTY_UNAME": "Linux",
               "CLAUDE_KITTY_SYSTEMCTL": str(self.systemctl), "FAKE_SYSTEMD": str(self.sd), "TMPDIR": str(self.root)}
        r = subprocess.run(["sh", str(copy / "tools/install.sh"), "--systemd", "--yes"], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        text = (self.udir / "claude-state-watch.service").read_text()
        self.assertIn("ExecStart=/usr/bin/python3 %h/src/ck/bin/claude-state watch", text)
        self.assertIn("ExecStart=%h/src/ck/bin/claude-roster save --auto", (self.udir / "claude-roster-save.service").read_text())

    def test_kitty_include_files_match(self):
        # macOS 用は listen_on の行だけが違う
        def directives(name):
            return [x for x in (ROOT / "kitty" / name).read_text().splitlines() if x and not x.startswith("#")]
        a, b = directives("claude-kitty.conf"), directives("claude-kitty-macos.conf")
        self.assertEqual([x for x in a if not x.startswith("listen_on")], [x for x in b if not x.startswith("listen_on")])
        self.assertIn("listen_on unix:/tmp/kitty-{kitty_pid}", b)
        self.assertIn("listen_on unix:${XDG_RUNTIME_DIR}/kitty-{kitty_pid}", a)


if __name__ == "__main__":
    unittest.main(verbosity=1)
