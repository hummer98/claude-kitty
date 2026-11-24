#!/usr/bin/env python3
"""claude-lang-check の単体テスト。python3 tests/lang_check_test.py（標準ライブラリだけ）"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from langcheck import (DEFAULTS, TAG, decide, judge, load_lang_config, read_transcript,  # noqa: E402
                       reverse_lines, strip_noise)

CFG = dict(DEFAULTS, language="ja")   # 既定は言語なし（何もしない）なので、テストは日本語を決めて試す
BIN = ROOT / "bin" / "claude-lang-check"

JA = "実装を終えました。単体テストはすべて通っています。レビューの結果は、届いたらまとめて報告します。"
EN = ("I've fixed the slider bug and committed it. All tests pass. A review subagent is now checking "
      "the change, and I'll send the final report once its results are in.")
NUDGE = ("The task tools haven't been used recently. If you're working on tasks that would benefit from "
         "tracking progress, consider using TaskCreate to add new tasks.")


def blocked(text, **over):
    return judge(text, dict(CFG, **over))[0]


class Judge(unittest.TestCase):
    def test_japanese_passes(self):
        self.assertFalse(blocked(JA))

    def test_english_blocks(self):
        self.assertTrue(blocked(EN))

    def test_english_nudge_then_japanese_blocks(self):
        self.assertTrue(blocked(NUDGE + "\n\n" + JA + JA))

    def test_nudge_on_same_paragraph_as_japanese_blocks(self):
        # 段落を分けずに日本語が続いても、冒頭が英語なら止まる
        self.assertTrue(blocked(NUDGE + " Let me continue with the work now. " + JA))

    def test_japanese_with_many_identifiers_passes(self):
        text = ("Stop hook の入力にある `stop_hook_active` を見てから、transcript_path の JSONL を読みます。"
                "設定は ~/.config/claude-kitty/config.json の lang の節で、CLAUDE_LANG_CHECK_THRESHOLD でも変えられます。"
                "SubagentStop は対象外で、PreToolUse や UserPromptSubmit にも関係しません。"
                "結果は lib/langcheck.py と bin/claude-lang-check、tests/lang_check_test.py にあります（commit 1a2b3c4）。")
        self.assertFalse(blocked(text))

    def test_japanese_quoting_english_phrase_passes(self):
        text = ("英語のシステムの文（\"The task tools haven't been used recently\"）は、ToDo のツールをしばらく"
                "使っていないという促しです。その後に返答が英語になることがあるので、Stop hook で確認します。")
        self.assertFalse(blocked(text))

    def test_code_block_only_passes(self):
        self.assertFalse(blocked("```bash\ngit status --short && python3 tests/run.py\necho done\n```"))

    def test_unclosed_code_block_passes(self):
        self.assertFalse(blocked("```\n" + EN * 3))

    def test_empty_and_short_pass(self):
        for t in ("", "   \n", None, "Done.", "OK, committed (a1b2c3d).", "All 42 tests pass."):
            self.assertFalse(blocked(t), t)

    def test_table_only_passes(self):
        table = ("| Step | Commit | Tests | Notes |\n|---|---|---|---|\n"
                 "| curvature slider | b40a3ff | +4 | symmetric end slopes |\n"
                 "| road type | e6c0d63 | +4 | default index pinned and all indices covered |\n")
        self.assertFalse(blocked(table))

    def test_japanese_with_english_table_passes(self):
        table = "| name | value |\n|---|---|\n| threshold | lower than this is blocked by the hook |\n"
        self.assertFalse(blocked("閾値の一覧です。\n\n" + table * 4 + "\n以上です。"))

    def test_long_english_log_in_code_block_passes(self):
        log = "\n".join(f"[INFO] Loading the module number {i} from the cache and checking the signature"
                        for i in range(80))
        self.assertFalse(blocked("ログです。\n\n```\n" + log + "\n```"))
        self.assertFalse(blocked("```\n" + log + "\n```"))

    def test_urls_and_paths_only_pass(self):
        self.assertFalse(blocked("https://code.claude.com/docs/en/hooks\n/var/tmp/some/long/path/file.txt\n"
                                 "~/projects/foo/bar.py ./run.sh ../x/y"))

    def test_html_tags_do_not_count(self):
        self.assertFalse(blocked(JA + "\n\n" + "<br>\n\n" * 40))

    def test_japanese_slash_lists_are_not_paths(self):
        # \w が日本語の字にも当たると「成功/失敗」から空白までが 1 文ごと消え、残りの英語だけで判定されていた
        text = ("Stop hook の判定は通す/止めるの2つで、対象は日本語/中国語/韓国語の3つです。設定ファイルの読み込み/検証/"
                "既定値への戻しはload lang config がまとめて行い、壊れた値は項目ごとに既定値/警告として扱います。"
                "the first paragraph rule and the lead rule are explained below.")
        self.assertFalse(blocked(text))
        self.assertIn("作業中", strip_noise("状態は作業中/待受中/完了/idleの4つです。"))

    def test_quote_or_heading_before_japanese_passes(self):
        quote = "> Error: could not open the configuration file because the permission was denied by the system\n\n"
        heading = "## Summary of the review and the results of the tests\n\n"
        self.assertFalse(blocked(quote + JA + JA))
        self.assertFalse(blocked(heading + JA + JA))

    def test_threshold_and_language_settings(self):
        self.assertTrue(blocked(JA, language="ko"))
        self.assertFalse(blocked(EN, threshold=0.0))
        self.assertFalse(blocked(EN, min=1000))


def entry(kind, content, **extra):
    role = "assistant" if kind == "assistant" else "user"
    return dict({"type": kind, "isSidechain": False, "message": {"role": role, "content": content}}, **extra)


def prompt(uuid, text="お願いします"):
    return entry("user", text, uuid=uuid)


def say(text):
    return entry("assistant", [{"type": "text", "text": text}])


def tool_use():
    return entry("assistant", [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}])


def tool_result():
    return entry("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}])


class Hook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.runtime = d / "run"
        self.runtime.mkdir()
        self.transcript = d / "s.jsonl"
        self.env = {"XDG_RUNTIME_DIR": str(self.runtime), "CLAUDE_KITTY_CONFIG": str(d / "none.json"),
                    "CLAUDE_LANG_CHECK_LANGUAGE": "ja"}

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, entries, garbage=False):
        lines = [json.dumps(e, ensure_ascii=False) for e in entries]
        if garbage:
            lines.insert(1, "{not json")
        self.transcript.write_text("\n".join(lines) + "\n")

    def hook(self, last=None, active=False, event="Stop", sid="sess-1"):
        data = {"session_id": sid, "transcript_path": str(self.transcript), "hook_event_name": event,
                "stop_hook_active": active}
        if last is not None:
            data["last_assistant_message"] = last
        return decide(json.dumps(data), self.env)

    def test_blocks_english_and_reason_asks_for_japanese(self):
        self.write([prompt("p1"), tool_use(), tool_result(), say(EN)])
        out = self.hook(last=EN)
        self.assertEqual(out["decision"], "block")
        self.assertIn("日本語で書き直", out["reason"])
        self.assertIn(TAG, out["reason"])

    def test_passes_japanese(self):
        self.write([prompt("p1"), say(JA)])
        self.assertIsNone(self.hook(last=JA))

    def test_stop_hook_active_passes(self):
        self.write([prompt("p1"), say(EN)])
        self.assertIsNone(self.hook(last=EN, active=True))

    def test_subagent_stop_is_ignored(self):
        self.write([prompt("p1"), say(EN)])
        self.assertIsNone(self.hook(last=EN, event="SubagentStop"))

    def test_only_once_per_reply(self):
        self.write([prompt("p1"), say(EN)])
        self.assertIsNotNone(self.hook(last=EN))
        # 止めた理由が user の行として残り、書き直しがまた英語でも、同じ返答の中では 2 回目は止めない
        self.write([prompt("p1"), say(EN), entry("user", "Stop hook feedback:\n" + TAG + " ..."), tool_use(),
                    tool_result(), say(EN)])
        self.assertIsNone(self.hook(last=EN))
        # 次のユーザーの入力に対する返答なら、また止める
        self.write([prompt("p1"), say(EN), prompt("p2"), say(EN)])
        self.assertIsNotNone(self.hook(last=EN))

    def test_sessions_are_separate(self):
        self.write([prompt("p1"), say(EN)])
        self.assertIsNotNone(self.hook(last=EN, sid="a"))
        self.assertIsNotNone(self.hook(last=EN, sid="b"))

    def test_falls_back_to_transcript_text(self):
        self.write([prompt("p1"), tool_use(), tool_result(), say(EN)])
        self.assertIsNotNone(self.hook())
        self.write([prompt("p2"), say(JA)])
        self.assertIsNone(self.hook())

    def test_transcript_text_is_only_the_final_reply(self):
        # ツールの前の英語の独り言ではなく、最後のツールの結果より後の本文で判定する
        self.write([prompt("p1"), say(EN), tool_use(), tool_result(), say(JA)], garbage=True)
        key, text = read_transcript(self.transcript)
        self.assertEqual((key, text), ("p1", JA))

    def test_reverse_lines_across_chunks(self):
        lines = [("行" * (i % 7)) + f"line {i} " + "x" * (i * 13 % 50) for i in range(120)]
        self.transcript.write_text("\n".join(lines) + "\n\n")
        want = [ln.encode() for ln in reversed(lines)]
        for chunk in (1, 3, 7, 64, 1 << 20):
            self.assertEqual(list(reverse_lines(self.transcript, chunk=chunk)), want, chunk)

    def test_unreadable_transcript_passes(self):
        self.assertIsNone(self.hook(last=EN))                 # ファイルが無い
        self.transcript.write_text("{broken\nnot json either\n")
        self.assertIsNone(self.hook(last=EN))                 # ユーザーの入力が見つからない
        self.write([tool_result(), say(EN)])
        self.assertIsNone(self.hook(last=EN))

    def test_bad_input_passes(self):
        for text in ("", "{", "[]", "null", json.dumps({"session_id": "../x", "transcript_path": "/x"})):
            self.assertIsNone(decide(text, self.env), text)

    def test_bad_language_in_config_still_checks(self):
        cfgfile = Path(self.tmp.name) / "c.json"
        cfgfile.write_text(json.dumps({"lang": {"language": ["ja"]}}))
        self.env["CLAUDE_KITTY_CONFIG"] = str(cfgfile)
        self.write([prompt("p1"), say(EN)])
        self.assertIsNotNone(self.hook(last=EN))
        self.assertIn("lang.language", (self.runtime / "claude-lang-check" / "errors.log").read_text())

    def test_disabled_by_env(self):
        self.write([prompt("p1"), say(EN)])
        self.env["CLAUDE_LANG_CHECK"] = "off"
        self.assertIsNone(self.hook(last=EN))


class Config(unittest.TestCase):
    def test_file_and_env(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text(json.dumps({"view": {"left": "main"},
                                     "lang": {"threshold": 0.5, "head": 100, "min": 10, "language": "zh"}}))
            cfg, problems = load_lang_config({"CLAUDE_KITTY_CONFIG": str(p)})
            self.assertEqual((cfg["threshold"], cfg["head"], cfg["min"], cfg["language"], problems),
                             (0.5, 100, 10, "zh", []))
            cfg, _ = load_lang_config({"CLAUDE_KITTY_CONFIG": str(p), "CLAUDE_LANG_CHECK_THRESHOLD": "0.2",
                                       "CLAUDE_LANG_CHECK_LANGUAGE": "ja"})
            self.assertEqual((cfg["threshold"], cfg["language"]), (0.2, "ja"))

    def test_broken_values_fall_back(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text(json.dumps({"lang": {"threshold": 7, "head": "x", "language": "xx", "enabled": "yes"}}))
            cfg, problems = load_lang_config({"CLAUDE_KITTY_CONFIG": str(p)})
            self.assertEqual(cfg, DEFAULTS)
            self.assertEqual(len(problems), 4)
            for bad in (["ja"], {"ja": 1}, 3):
                p.write_text(json.dumps({"lang": {"language": bad}}))
                cfg, problems = load_lang_config({"CLAUDE_KITTY_CONFIG": str(p)})
                self.assertEqual((cfg, len(problems)), (DEFAULTS, 1), bad)
            p.write_text("{broken")
            cfg, problems = load_lang_config({"CLAUDE_KITTY_CONFIG": str(p)})
            self.assertEqual((cfg, len(problems)), (DEFAULTS, 1))
            p.write_text(json.dumps({"lang": []}))
            self.assertEqual(load_lang_config({"CLAUDE_KITTY_CONFIG": str(p)})[0], DEFAULTS)


class NoLanguage(unittest.TestCase):
    """既定（言語を決めていない）では、hook を入れても何もしない（日本語を使わない人の返答を書き直させない）。"""

    def test_default_language_is_unset_and_does_nothing(self):
        self.assertIsNone(DEFAULTS["language"])
        with tempfile.TemporaryDirectory() as d:     # 既定と同じ null・空文字を書いても、誤りと数えない
            for v in (None, ""):
                p = Path(d) / "c.json"
                p.write_text(json.dumps({"lang": {"language": v}}))
                self.assertEqual(load_lang_config({"CLAUDE_KITTY_CONFIG": str(p)}), (DEFAULTS, []), v)
        self.assertEqual(judge(EN, dict(DEFAULTS))[0], False)
        with tempfile.TemporaryDirectory() as d:
            env = {"XDG_RUNTIME_DIR": d, "CLAUDE_KITTY_CONFIG": str(Path(d) / "none.json")}
            t = Path(d) / "s.jsonl"
            t.write_text("")
            payload = json.dumps({"hook_event_name": "Stop", "session_id": "abc", "transcript_path": str(t),
                                  "last_assistant_message": EN})
            self.assertIsNone(decide(payload, env))


class Command(unittest.TestCase):
    def judge_bin(self, args, stdin=""):
        env = dict(os.environ, CLAUDE_KITTY_CONFIG="/nonexistent/config.json", CLAUDE_LANG_CHECK_LANGUAGE="ja")
        return subprocess.run([sys.executable, str(BIN), "judge", *args], input=stdin, capture_output=True,
                              text=True, timeout=30, env=env)

    def test_judge_subcommand(self):
        r = self.judge_bin([], EN)
        self.assertEqual(r.returncode, 1)
        self.assertTrue(r.stdout.startswith("block:"))
        r = self.judge_bin([], JA)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stdout.startswith("pass:"))
        r = self.judge_bin(["/nonexistent/file.txt"])
        self.assertEqual((r.returncode, r.stdout), (2, ""))


    def run_bin(self, stdin, env=None):
        e = dict(os.environ, CLAUDE_KITTY_CONFIG="/nonexistent/config.json", CLAUDE_LANG_CHECK_LANGUAGE="ja")
        e.update(env or {})
        return subprocess.run([sys.executable, str(BIN)], input=stdin, capture_output=True, text=True,
                              env=e, timeout=30)

    def test_broken_json_exits_zero_silently(self):
        r = self.run_bin("{not json")
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_block_prints_decision_json(self):
        with tempfile.TemporaryDirectory() as d:
            t = Path(d) / "t.jsonl"
            t.write_text("\n".join(json.dumps(e) for e in (prompt("p1"), say(EN))) + "\n")
            data = {"session_id": "s", "transcript_path": str(t), "hook_event_name": "Stop",
                    "stop_hook_active": False, "last_assistant_message": EN}
            env = {"XDG_RUNTIME_DIR": d, "CLAUDE_KITTY_CONFIG": str(Path(d) / "none.json")}
            r = self.run_bin(json.dumps(data), env)
            self.assertEqual(r.returncode, 0)
            self.assertEqual(json.loads(r.stdout)["decision"], "block")
            r = self.run_bin(json.dumps(data), env)     # 同じ返答の 2 回目
            self.assertEqual((r.returncode, r.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main(verbosity=1)
