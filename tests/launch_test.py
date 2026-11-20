#!/usr/bin/env python3
"""bin/claude-kitty の起動の引数と、仮想デスクトップへの移動（偽の kitty と偽の qdbus だけを使う）。"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FAKE_KITTY = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_LOG/kitty.args"
"""
FAKE_QDBUS = """#!/bin/sh
echo "$*" >> "$FAKE_LOG/qdbus.calls"
case "$3" in
org.kde.kwin.Scripting.loadScript) cp "$4" "$FAKE_LOG/script.js"; echo 7 ;;
esac
"""


class KittyLaunch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log = self.root / "log"
        self.log.mkdir()
        fake = self.root / "fake"
        fake.mkdir()
        for name, body in (("kitty", FAKE_KITTY), ("qdbus", FAKE_QDBUS)):
            (fake / name).write_text(body)
            (fake / name).chmod(0o755)
        self.fake = fake
        # 本物の qdbus（KWin）に届かないよう、PATH は要るコマンドへのリンクだけにする（/usr/bin を入れない）
        tools = self.root / "tools"
        tools.mkdir()
        for name in ("python3", "dirname", "readlink", "env", "grep", "sed", "sleep", "rm", "cat", "cp"):
            found = shutil.which(name)
            if found:
                (tools / name).symlink_to(found)
        self.tools = tools
        self.cfg = self.root / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def launch(self, config=None, **extra):
        if config is not None:
            self.cfg.write_text(json.dumps(config))
        env = {"PATH": f"{self.fake}:{self.tools}", "HOME": str(self.root), "XDG_RUNTIME_DIR": str(self.root),
               "CLAUDE_KITTY_KITTY": str(self.fake / "kitty"), "CLAUDE_KITTY_CONFIG": str(self.cfg),
               "FAKE_LOG": str(self.log)}
        env.update(extra)
        q = shutil.which("qdbus", path=env["PATH"])
        assert q in (None, str(self.fake / "qdbus")), f"本物の qdbus に届く PATH: {q}"   # 本物の KWin を動かさない
        r = subprocess.run([str(ROOT / "bin/claude-kitty"), "/x/my.session"], env=env, capture_output=True,
                           text=True, timeout=30)
        args = (self.log / "kitty.args").read_text().splitlines()
        calls = (self.log / "qdbus.calls").read_text().splitlines() if (self.log / "qdbus.calls").exists() else []
        return r, args, calls

    def test_default_does_not_move_or_maximize(self):
        r, args, calls = self.launch()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(args, ["--detach", "--class", "kitty-claude", "--start-as", "normal", "--session", "/x/my.session"])
        self.assertEqual(calls, [])

    def test_maximize_without_desktop(self):
        _r, args, calls = self.launch({"kitty": {"maximize": True}})
        self.assertIn("maximized", args)
        self.assertEqual(calls, [])

    def test_desktop_and_maximize_like_before(self):
        # 0.2.0 までの動き（デスクトップ 2・最大化）は、設定で同じになる
        r, args, calls = self.launch({"view": {"left": "main"}, "kitty": {"desktop": 2, "maximize": True}})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(args[args.index("--start-as") + 1], "maximized")
        self.assertTrue(calls[0].startswith("org.kde.KWin /Scripting org.kde.kwin.Scripting.loadScript "))
        self.assertIn("/Scripting/Script7 org.kde.kwin.Script.run", calls[1])
        js = (self.log / "script.js").read_text()
        self.assertIn("const DESKTOP = 2;", js)
        self.assertIn("const MAXIMIZE = true;", js)
        self.assertNotIn("@@", js)
        self.assertFalse(list(self.root.glob("claude-kitty-desktop.*.js")))     # 一時のスクリプトは消す

    def test_desktop_without_maximize(self):
        _r, args, calls = self.launch({"kitty": {"desktop": 3, "maximize": False}})
        self.assertEqual(args[args.index("--start-as") + 1], "normal")
        js = (self.log / "script.js").read_text()
        self.assertIn("const DESKTOP = 3;", js)
        self.assertIn("const MAXIMIZE = false;", js)

    def test_desktop_without_qdbus_says_so(self):
        (self.fake / "qdbus").unlink()
        r, _args, calls = self.launch({"kitty": {"desktop": 2}})
        self.assertEqual(r.returncode, 0)
        self.assertIn("qdbus が無い", r.stderr)

    def test_broken_json_still_launches(self):
        self.cfg.write_text("{broken")
        r, args, _calls = self.launch()
        self.assertIn("設定ファイルを読めない", r.stderr)
        self.assertEqual(args[args.index("--start-as") + 1], "normal")

    def test_other_class_and_env_override(self):
        _r, args, calls = self.launch({"kitty": {"desktop": 3}}, CLAUDE_KITTY_CLASS="kitty-test",
                                      CLAUDE_KITTY_START_AS="fullscreen")
        self.assertEqual(args[args.index("--start-as") + 1], "fullscreen")
        self.assertEqual(calls, [])                                           # 試験用のクラスは動かさない

    def test_broken_kitty_section_still_launches_with_defaults(self):
        r, args, calls = self.launch({"kitty": {"desktop": "two"}})
        self.assertIn("kitty.desktop", r.stderr)
        self.assertEqual(args[args.index("--start-as") + 1], "normal")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
