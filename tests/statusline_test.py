#!/usr/bin/env python3
"""claude-statusline（lib/statusline.py・config の statusline 節・tools/merge_statusline.py）の単体テスト。

一時の HOME・XDG_RUNTIME_DIR・設定ファイルだけを使う（本物の settings.json や claude-state の記録は読まない）。
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
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
import statusline as SL  # noqa: E402
from config import DEFAULT_STATUSLINE, load_statusline_config  # noqa: E402

BIN = ROOT / "bin" / "claude-statusline"
NOW = 1_800_000_000
SID = "sess-1"


def conf(**kw):
    c = json.loads(json.dumps(DEFAULT_STATUSLINE))
    c["weekly_roles"] = ["lead"]
    c.update(kw)
    return c


def col(text, n):
    return f"\033[38;5;{n}m{text}\033[0m"


def weekly(pct, reset=NOW + 2 * 86400 + 4 * 3600 + 59):
    return {"seven_day": {"used_percentage": pct, "resets_at": reset}}


def usage(inp=0, read=0, create=0):
    return {"input_tokens": inp, "cache_read_input_tokens": read, "cache_creation_input_tokens": create, "output_tokens": 9}


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.cfg = self.d / "config.json"
        self.run_dir = self.d / "run"
        (self.run_dir / "claude-state").mkdir(parents=True)
        (self.d / "home").mkdir()
        self.env = {"HOME": str(self.d / "home"), "CLAUDE_KITTY_CONFIG": str(self.cfg),
                    "XDG_RUNTIME_DIR": str(self.run_dir), "KITTY_CONFIG_DIRECTORY": str(self.d / "nokitty"),
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin")}

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, sid=SID, **rec):
        (self.run_dir / "claude-state" / f"{sid}.json").write_text(json.dumps(rec))  # pid が無い記録は生きている扱い

    def cli(self, stdin, env=None):
        r = subprocess.run([str(BIN)], input=stdin, env=env or self.env, capture_output=True, timeout=20)
        return r.returncode, r.stdout.decode()


class Segments(unittest.TestCase):
    def test_representative_line(self):
        data = {"session_name": "lead", "model": {"display_name": "Opus"},
                "context_window": {"current_usage": usage(10, 379_000, 990)}, "rate_limits": weekly(41.2)}
        self.assertEqual(SL.build(data, {}, NOW, conf()), f"lead | Opus | {col('ctx 380k', 186)} | 2d4h 41.2%")

    def test_context_thresholds_boundaries(self):
        c = conf()
        for n, color in ((199_999, None), (200_000, 186), (399_999, 186), (400_000, 173), (1_200_000, 173)):
            with self.subTest(n=n):
                s = SL.context_segment({"context_window": {"current_usage": usage(read=n)}}, c)
                self.assertEqual(s, f"ctx {SL.format_tokens(n)}" if color is None else col(f"ctx {SL.format_tokens(n)}", color))

    def test_context_colors_differ_from_weekly(self):
        # 会話の長さの色は、週の使用量の色（と待受中の名前の 208）と見分けられること
        d = DEFAULT_STATUSLINE
        self.assertFalse(set(d["context_colors"]) & (set(d["weekly_colors"]) | {208}))

    def test_format_tokens(self):
        self.assertEqual([SL.format_tokens(n) for n in (0, 999, 1000, 1499, 380_400, 999_499, 999_600, 1_250_000)],
                         ["0", "999", "1k", "1k", "380k", "999k", "1.0M", "1.2M"])

    def test_context_falls_back_to_total_input_tokens_only_without_current_usage_key(self):
        self.assertEqual(SL.context_tokens({"context_window": {"total_input_tokens": 5000}}), (5000, "context_window"))
        # 鍵があって null / 0（最初の応答の前・/compact の直後）なら、total_input_tokens に前の値が残っていても出さない
        for cu in (None, usage(), {}):
            with self.subTest(cu=cu):
                cw = {"total_input_tokens": 480_000, "current_usage": cu}
                self.assertEqual(SL.context_tokens({"context_window": cw}), (None, "context_window"))
                self.assertEqual(SL.build({"session_name": "x", "context_window": cw}, {}, NOW, conf()), "x")

    def test_context_after_compact_shows_nothing_even_with_transcript(self):
        # /compact の直後は current_usage が null / 0。記録の最後の usage は圧縮の前の大きさなので使わない
        with tempfile.TemporaryDirectory() as d:
            t = Path(d) / "t.jsonl"
            t.write_text(json.dumps({"message": {"usage": usage(read=500_000)}}) + "\n")
            for cw in ({"current_usage": None, "total_input_tokens": 0}, {"current_usage": usage()},
                       {"current_usage": None, "total_input_tokens": 300_000}):
                with self.subTest(cw=cw):
                    self.assertEqual(SL.context_tokens({"context_window": cw, "transcript_path": str(t)}),
                                     (None, "context_window"))
                    self.assertEqual(SL.context_segment({"context_window": cw, "transcript_path": str(t)}, conf()), "")

    def test_context_from_transcript_when_no_context_window(self):
        with tempfile.TemporaryDirectory() as d:
            t = Path(d) / "t.jsonl"
            lines = [{"type": "assistant", "message": {"usage": usage(1, 100_000, 2)}},
                     {"type": "assistant", "isSidechain": True, "message": {"usage": usage(read=900_000)}},
                     {"type": "user", "message": {"content": "x"}},
                     "not json {\"usage\"",
                     ]
            t.write_text("\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines) + "\n")
            self.assertEqual(SL.context_tokens({"transcript_path": str(t)}), (100_003, "transcript"))

    def test_transcript_only_tail_is_read(self):
        with tempfile.TemporaryDirectory() as d:
            t = Path(d) / "t.jsonl"
            last = json.dumps({"message": {"usage": usage(read=250_000)}})
            t.write_text(json.dumps({"message": {"usage": usage(read=7)}}) + "\n" + "x" * 5000 + "\n" + last + "\n")
            self.assertEqual(SL.transcript_tokens(str(t), tail=len(last) + 10), 250_000)
            self.assertIsNone(SL.transcript_tokens(str(t), tail=20))  # 欠けた行しか無い

    def test_transcript_missing_or_empty(self):
        self.assertEqual(SL.context_tokens({"transcript_path": "/nonexistent/x.jsonl"}), (None, None))
        self.assertEqual(SL.context_tokens({"transcript_path": None}), (None, None))
        self.assertEqual(SL.context_tokens({}), (None, None))
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "e.jsonl").write_text("")
            self.assertEqual(SL.context_tokens({"transcript_path": str(Path(d) / "e.jsonl")}), (None, None))

    def test_context_disabled(self):
        data = {"session_name": "x", "context_window": {"current_usage": usage(read=300_000)}}
        self.assertEqual(SL.build(data, {}, NOW, conf(context=False)), "x")

    def test_weekly_thresholds_are_strict_on_integer_part(self):
        # 前の sh 版（${pct%%.*} -gt 85）と同じ: 整数部が 85 を超えたら橙、95 を超えたら赤
        c = conf()
        for pct, color in ((85, None), (85.9, None), (86, 208), (95, 208), (95.99, 208), (96, 196), (100, 196)):
            with self.subTest(pct=pct):
                s = SL.weekly_segment({"rate_limits": weekly(pct)}, NOW, c)
                text = f"2d4h {round(pct, 1):g}%"
                self.assertEqual(s, text if color is None else col(text, color))

    def test_weekly_percent_one_decimal(self):
        c = conf(weekly_thresholds=[], weekly_colors=[])
        for pct, text in ((41.23456, "41.2%"), (41.0, "41%"), (7, "7%"), (99.96, "100%"), (0.04, "0%")):
            with self.subTest(pct=pct):
                self.assertEqual(SL.weekly_segment({"rate_limits": {"seven_day": {"used_percentage": pct}}}, NOW, c), text)
        # 色は丸める前の整数部（95.97 は 96.0% と見えても 95 を超えない = 橙のまま）
        s = SL.weekly_segment({"rate_limits": {"seven_day": {"used_percentage": 95.97}}}, NOW, conf())
        self.assertEqual(s, col("96%", 208))

    def test_weekly_left_time_forms(self):
        c = conf(weekly_thresholds=[], weekly_colors=[])
        for reset, text in ((NOW + 3 * 3600 + 5, "3h 10%"), (NOW + 59 * 60, "59m 10%"), (NOW - 100, "0m 10%")):
            with self.subTest(text=text):
                self.assertEqual(SL.weekly_segment({"rate_limits": weekly(10, reset)}, NOW + 0.9, c), text)
        self.assertEqual(SL.weekly_segment({"rate_limits": {"seven_day": {"used_percentage": 10}}}, NOW, c), "10%")

    def test_weekly_absent(self):
        c = conf()
        for rl in (None, {}, {"five_hour": {"used_percentage": 50}}, {"seven_day": None},
                   {"seven_day": {"used_percentage": None}}, {"seven_day": {"used_percentage": "50"}}, "x"):
            with self.subTest(rl=rl):
                self.assertEqual(SL.weekly_segment({"rate_limits": rl}, NOW, c), "")

    def test_weekly_roles(self):
        data = {"session_name": "dev-a", "rate_limits": weekly(10)}
        self.assertEqual(SL.build(data, {}, NOW, conf(weekly_roles=["lead"])), "dev-a")
        self.assertEqual(SL.build(data, {}, NOW, conf(weekly_roles=["*"])), "dev-a | 2d4h 10%")
        self.assertEqual(SL.build(data, {}, NOW, conf(weekly_roles=[])), "dev-a")
        # セッションの名前が役割と違っても、claude-state の記録の role で当てる
        st = {"record": {"state": "working", "role": "lead"}, "colors": {}, "subagent_colors": None}
        self.assertEqual(SL.build(data, {}, NOW, conf(weekly_roles=["lead"]), st), "dev-a | 2d4h 10%")

    def test_no_name_and_no_model(self):
        self.assertEqual(SL.build({}, {}, NOW, conf()), "?")
        self.assertEqual(SL.build({"session_name": "", "model": "Opus"}, {}, NOW, conf()), "?")

    def test_name_from_sessions_file(self):
        with tempfile.TemporaryDirectory() as d:
            s = Path(d) / "sessions"
            s.mkdir()
            (s / "1.json").write_text("{broken")
            (s / "2.json").write_text(json.dumps({"sessionId": "other", "name": "no"}))
            (s / "3.json").write_text(json.dumps({"sessionId": SID, "name": "dev-b"}))
            env = {"CLAUDE_CONFIG_DIR": d}
            self.assertEqual(SL.session_name({"session_id": SID}, env), "dev-b")
            self.assertIsNone(SL.session_name({"session_id": "nope"}, env))
            self.assertIsNone(SL.session_name({"session_id": SID}, {"CLAUDE_CONFIG_DIR": d + "/none"}))

    def test_paint_and_subagents_from_state(self):
        st = {"record": {"state": "waiting", "agents": {"a": {"end": None}, "b": {"end": 5}}},
              "colors": {"waiting": {"name": 208}}, "subagent_colors": {"running": None}}
        self.assertEqual(SL.build({"session_name": "x"}, {}, NOW, conf(), st), f"{col('x', 208)} | sub: 1")

    def test_failing_segment_is_dropped_not_the_line(self):
        c = conf()
        c["context_thresholds"] = None  # 壊れた設定を渡されても、その区切りだけを省く
        data = {"session_name": "x", "context_window": {"current_usage": usage(read=5)}}
        self.assertEqual(SL.build(data, {}, NOW, c), "x")

    def test_parse_input(self):
        for text in ("", "  ", "{bad", "[1,2]", "null", "3"):
            with self.subTest(text=text):
                self.assertEqual(SL.parse_input(text), {})


class Config(Env):
    def write(self, obj):
        self.cfg.write_text(json.dumps(obj))

    def test_defaults_and_left(self):
        c = load_statusline_config(self.env)
        self.assertEqual(c["weekly_roles"], ["main"])  # "left" は view.left（既定 main）
        self.write({"view": {"left": "lead"}, "statusline": {"weekly_roles": ["left", "ops", "lead"]}})
        self.assertEqual(load_statusline_config(self.env)["weekly_roles"], ["lead", "ops"])
        self.assertEqual(load_statusline_config(dict(self.env, CLAUDE_VIEW_LEFT="boss"))["weekly_roles"], ["boss", "ops", "lead"])
        self.write({"statusline": None})
        self.assertEqual(load_statusline_config(self.env)["context_colors"], [186, 173])

    def test_errors(self):
        bad = [{"statusline": []}, {"statusline": {"weekly_role": ["x"]}}, {"statusline": {"context": "yes"}},
               {"statusline": {"weekly_roles": "lead"}}, {"statusline": {"weekly_roles": [""]}},
               {"statusline": {"context_thresholds": [400000, 200000]}},
               {"statusline": {"context_thresholds": [200000, 200000]}},
               {"statusline": {"context_thresholds": [0, 1]}, "_": 0},
               {"statusline": {"context_colors": [186]}},
               {"statusline": {"context_colors": [186, 256]}},
               {"statusline": {"weekly_colors": [208, True]}},
               {"statusline": {"weekly_thresholds": [85, 101]}}]
        for obj in bad:
            with self.subTest(obj=obj):
                self.write(obj)
                with self.assertRaises(ValueError):
                    load_statusline_config(self.env)
        self.cfg.write_text("{broken")
        with self.assertRaises(ValueError):
            load_statusline_config(self.env)

    def test_custom_steps(self):
        self.write({"statusline": {"context_thresholds": [100000], "context_colors": [33],
                                   "weekly_thresholds": [], "weekly_colors": []}})
        c = load_statusline_config(self.env)
        self.assertEqual(SL.context_segment({"context_window": {"current_usage": usage(read=100000)}}, c), col("ctx 100k", 33))
        c["weekly_roles"] = ["*"]
        self.assertEqual(SL.weekly_segment({"rate_limits": weekly(99)}, NOW, c), "2d4h 99%")


class Cli(Env):
    def test_bad_inputs_still_print_one_line(self):
        for stdin in (b"", b"{bad", b"[]", b"\xff\xfe", b'{"session_id": "../../etc"}'):
            with self.subTest(stdin=stdin):
                self.assertEqual(self.cli(stdin), (0, "?"))

    def test_full_line_with_state_record(self):
        self.cfg.write_text(json.dumps({"view": {"left": "lead"}}))
        self.record(state="waiting", role="lead", agents={"a": {"end": None}})
        data = {"session_id": SID, "session_name": "lead", "model": {"display_name": "Opus"},
                "context_window": {"current_usage": usage(read=420_000)}, "rate_limits": weekly(97, 4_000_000_000)}
        rc, out = self.cli(json.dumps(data).encode())
        self.assertEqual(rc, 0)
        parts = out.split(" | ")
        self.assertEqual(parts[:3], [col("lead", 208), "Opus", col("ctx 420k", 173)])
        self.assertEqual(parts[3], col("sub: 1", 110))
        self.assertTrue(parts[4].startswith("\033[38;5;196m") and parts[4].endswith(" 97%\033[0m"), parts[4])

    def test_broken_config_uses_defaults_and_says_so(self):
        self.cfg.write_text(json.dumps({"statusline": {"context": 1}}))
        data = {"session_name": "main", "rate_limits": weekly(10, None)}
        self.assertEqual(self.cli(json.dumps(data).encode()), (0, "main | 10% | 設定の誤り（claude-kitty doctor）"))
        self.cfg.write_text("{broken")
        self.assertEqual(self.cli(b'{"session_name": "x"}'), (0, "x | 設定の誤り（claude-kitty doctor）"))

    def test_broken_state_config_keeps_line(self):
        self.cfg.write_text(json.dumps({"state": {"colors": {"waiting": {"name": 999}}}}))
        self.record(state="waiting")
        self.assertEqual(self.cli(json.dumps({"session_id": SID, "session_name": "x"}).encode()),
                         (0, f"{col('x', 208)} | 設定の誤り（claude-kitty doctor）"))

    def test_missing_transcript_in_cli(self):
        data = {"session_name": "x", "transcript_path": str(self.d / "none.jsonl")}
        self.assertEqual(self.cli(json.dumps(data).encode()), (0, "x"))


class MergeStatusline(unittest.TestCase):
    def setUp(self):
        import merge_statusline as MS
        self.MS = MS
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Path(self.tmp.name) / "settings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def main(self, *args):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self.MS.main(["--settings", str(self.settings), "--repo", "/r", *args])
        return rc, buf.getvalue()

    def test_install_and_uninstall(self):
        self.settings.write_text(json.dumps({"model": "x"}))
        rc, out = self.main("--dry-run")
        self.assertEqual((rc, json.loads(self.settings.read_text())), (0, {"model": "x"}))
        self.assertTrue(out.startswith("+ statusLine: /r/bin/claude-statusline"))
        self.main()
        self.assertEqual(json.loads(self.settings.read_text())["statusLine"],
                         {"type": "command", "command": "/r/bin/claude-statusline", "refreshInterval": 30})
        rc, out = self.main()
        self.assertTrue(out.startswith("= "))
        self.main("--uninstall")
        self.assertEqual(json.loads(self.settings.read_text()), {"model": "x"})

    def test_keeps_someone_elses_statusline(self):
        other = {"model": "x", "statusLine": {"type": "command", "command": "~/.claude/my-line.sh"}}
        self.settings.write_text(json.dumps(other))
        for args in ((), ("--uninstall",)):
            with self.subTest(args=args):
                rc, out = self.main(*args)
                self.assertEqual(rc, 0)
                self.assertEqual(json.loads(self.settings.read_text()), other)
        self.assertTrue(self.main()[1].startswith("! 別の statusLine がある"))

    def test_same_tool_by_other_path_counts(self):
        self.settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "~/.local/bin/claude-statusline"}}))
        self.assertTrue(self.main()[1].startswith("= "))
        self.main("--uninstall")
        self.assertEqual(json.loads(self.settings.read_text()), {})

    def test_broken_settings_untouched(self):
        self.settings.write_text("{broken")
        self.assertEqual(self.main()[0], 2)
        self.assertEqual(self.settings.read_text(), "{broken")



class InstallSh(unittest.TestCase):
    """tools/install.sh: statusLine は --statusline と明示したときだけ入れ、外すのは --statusline か --all --uninstall。"""

    def setUp(self):
        import install_test as IT  # 偽の systemctl / launchctl（本物の常駐に触らない）
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.settings = self.home / ".claude/settings.json"
        self.settings.write_text("{}")
        fake = self.root / "fake"
        fake.mkdir()
        for name, body in (("systemctl", IT.FAKE_SYSTEMCTL), ("launchctl", IT.FAKE_LAUNCHCTL)):
            (fake / name).write_text(body)
            (fake / name).chmod(0o755)
        self.fake = fake

    def tearDown(self):
        self.tmp.cleanup()

    def sh(self, *args, os_name="Linux"):
        env = {"HOME": str(self.home), "PATH": f"{self.home}/.local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
               "CLAUDE_KITTY_UNAME": os_name, "TMPDIR": str(self.root),
               "CLAUDE_KITTY_SYSTEMCTL": str(self.fake / "systemctl"), "FAKE_SYSTEMD": str(self.root / "sd"),
               "CLAUDE_KITTY_LAUNCHCTL": str(self.fake / "launchctl"), "FAKE_LAUNCHD": str(self.root / "launchd")}
        return subprocess.run(["sh", str(ROOT / "tools/install.sh"), "--yes", *args], env=env, capture_output=True,
                              text=True, timeout=120)

    def line(self):
        return json.loads(self.settings.read_text()).get("statusLine")

    def test_only_when_named(self):
        self.assertEqual(self.sh("--hooks").returncode, 0)
        self.assertIsNone(self.line())
        r = self.sh("--statusline")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(Path(self.line()["command"]).name, "claude-statusline")
        self.sh("--uninstall", "--hooks")  # hook だけを外すときは残す
        self.assertIsNotNone(self.line())
        self.sh("--uninstall", "--statusline")
        self.assertIsNone(self.line())

    def test_all_and_no_args_do_not_install(self):
        for args in (("--all",), ()):
            with self.subTest(args=args):
                r = self.sh(*args)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn("claude-state", self.settings.read_text())  # ほかの部品は入った（何もしないで終わったのではない）
                self.assertIsNone(self.line())
                self.assertNotIn("statusLine", r.stdout)

    def test_uninstall_all_and_no_args_remove_only_ours(self):
        for args in (("--all",), ()):
            with self.subTest(args=args):
                self.assertEqual(self.sh("--statusline").returncode, 0)
                self.assertIsNotNone(self.line())
                r = self.sh("--uninstall", *args)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIsNone(self.line())
        other = {"type": "command", "command": "~/.claude/mine.sh"}
        data = json.loads(self.settings.read_text())
        data["statusLine"] = other
        self.settings.write_text(json.dumps(data))
        self.sh("--uninstall")
        self.assertEqual(self.line(), other)

    def test_other_statusline_is_kept(self):
        other = {"type": "command", "command": "~/.claude/mine.sh"}
        self.settings.write_text(json.dumps({"statusLine": other}))
        r = self.sh("--statusline")
        self.assertIn("! 別の statusLine がある", r.stdout)
        self.assertEqual(self.line(), other)


if __name__ == "__main__":
    unittest.main(verbosity=1)
