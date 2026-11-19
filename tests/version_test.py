#!/usr/bin/env python3
"""版の番号（VERSION・各コマンドの --version・CHANGELOG との一致）のテスト。python3 tests/version_test.py"""
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tools"))
import version as V  # noqa: E402
from changelog_section import headings  # noqa: E402

SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$")


def bare_env():
    """kitty や Claude Code の中でなくても同じに動くことを見るため、KITTY_* / CLAUDE* を外す。HOME も空の場所にする。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KITTY_", "CLAUDE"))}
    env["HOME"] = tempfile.gettempdir() + "/claude-kitty-version-test-nohome"
    return env


class VersionFile(unittest.TestCase):
    def test_version_is_semver(self):
        self.assertRegex(V.version(), SEMVER)

    def test_version_matches_a_changelog_heading(self):
        # release.sh と同じ約束: VERSION の版の節が CHANGELOG に日付付きである
        vers = dict(headings((ROOT / "CHANGELOG.md").read_text(encoding="utf-8")))
        self.assertIn(V.version(), vers)
        self.assertRegex(vers[V.version()] or "", r"^\d{4}-\d{2}-\d{2}$")

    def test_missing_or_empty_file_is_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(V.version(Path(d) / "none"), "unknown")
            (Path(d) / "VERSION").write_text("\n\n")
            self.assertEqual(V.version(Path(d) / "VERSION"), "unknown")
            (Path(d) / "VERSION").write_text(" 1.2.3 \nextra\n")
            self.assertEqual(V.version(Path(d) / "VERSION"), "1.2.3")

    def test_handle_version_only_for_exact_flag(self):
        out = io.StringIO()
        self.assertTrue(V.handle_version(["--version"], "claude-x", out))
        self.assertEqual(out.getvalue(), f"claude-x {V.version()}\n")
        for argv in ([], ["--version", "x"], ["x", "--version"], ["-V"], ["hook"]):
            out = io.StringIO()
            self.assertFalse(V.handle_version(argv, "claude-x", out))
            self.assertEqual(out.getvalue(), "")


class CommandsVersion(unittest.TestCase):
    """bin/ のすべてのコマンドが --version で "名前 版" を出して 0 で終わる（kitty・設定・stdin を読まない）。"""

    def test_every_command(self):
        cmds = sorted(p for p in (ROOT / "bin").iterdir() if p.is_file() and not p.name.startswith("."))
        self.assertGreaterEqual(len(cmds), 10)
        for p in cmds:
            with self.subTest(cmd=p.name):
                # stdin は書き手を開いたままの pipe（EOF が来ない）: hook として stdin を読みに行ったら、ここで止まって timeout になる
                rfd, wfd = os.pipe()
                try:
                    r = subprocess.run([str(p), "--version"], env=bare_env(), stdin=rfd,
                                       capture_output=True, text=True, timeout=20)
                finally:
                    os.close(rfd)
                    os.close(wfd)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, f"{p.name} {V.version()}\n", ""))

    def test_hooks_still_read_stdin_without_args(self):
        # --version を足しても、hook の既定の経路（引数なし・hook EVENT）は stdin を読み、exit 0 で stdout を汚さない
        with tempfile.TemporaryDirectory() as d:
            env = dict(bare_env(), XDG_RUNTIME_DIR=d, CLAUDE_KITTY_CONFIG=str(Path(d) / "none.json"))
            for argv in (["claude-guard"], ["claude-guard", "hook"], ["claude-lang-check"], ["claude-state", "hook", "Stop"]):
                with self.subTest(argv=argv):
                    r = subprocess.run([str(ROOT / "bin" / argv[0]), *argv[1:]], env=env, input="{}",
                                       capture_output=True, text=True, timeout=20)
                    self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_version_does_not_shadow_other_args(self):
        # --version が他の引数と一緒なら、いつもの引数の解釈に任せる（どれも argparse が知らない引数として exit 2）
        for argv in (["claude-show", "--version", "x"], ["claude-view", "--version", "status"],
                     ["claude-browser", "status", "--version"], ["claude-guard", "check", "--version"]):
            with self.subTest(argv=argv):
                r = subprocess.run([str(ROOT / "bin" / argv[0]), *argv[1:]], env=bare_env(), stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, timeout=20)
                self.assertNotEqual(r.returncode, 0)
                self.assertNotIn(f"{argv[0]} {V.version()}", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=1)
