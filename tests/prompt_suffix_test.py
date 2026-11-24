#!/usr/bin/env python3
"""claude-prompt-suffix の単体テスト。python3 tests/prompt_suffix_test.py（標準ライブラリだけ）"""
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import promptsuffix as P  # noqa: E402

BIN = ROOT / "bin" / "claude-prompt-suffix"


def paste(body, pid="f280"):
    """Claude Code 2.1.331 が hook の prompt に入れる、貼り付けた文の形（実物を写したもの）。"""
    return f'\n\n<pasted_content id="{pid}">\n{body}\n</pasted_content id="{pid}">\n'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.cfg_path = self.d / "config.json"
        self.env = {"HOME": str(self.d / "home"), "CLAUDE_KITTY_CONFIG": str(self.cfg_path),
                    "XDG_RUNTIME_DIR": str(self.d), "XDG_CONFIG_HOME": str(self.d / "xdg")}
        (self.d / "home").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write_cfg(self, section):
        self.cfg_path.write_text(json.dumps({"prompt_suffix": section}))

    def cfg(self):
        return P.load_config(self.env)

    def find(self, text):
        return P.find(text, self.cfg()[0]["table"])

    def hook(self, prompt, **extra):
        data = dict({"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": prompt}, **extra)
        return P.decide(json.dumps(data), self.env, now=1_800_000_000, os_name="Linux")


class Find(Base):
    def test_each_default(self):
        for name in ("wdt", "opt", "pre", "desc", "next", "ss"):
            self.assertEqual(self.find(f"これはどう？ {name}"), [(name, None)], name)

    def test_case_insensitive_and_alone(self):
        self.assertEqual(self.find("WDT"), [("wdt", None)])
        self.assertEqual(self.find("Desc 2"), [("desc", "2")])

    def test_multiple(self):
        self.assertEqual(self.find("どれがいい？ opt wdt"), [("opt", None), ("wdt", None)])
        self.assertEqual(self.find("どれ opt wdt opt"), [("opt", None), ("wdt", None)])  # 同じものは 1 回

    def test_desc_with_number(self):
        self.assertEqual(self.find("ありがとう desc 2"), [("desc", "2")])
        self.assertEqual(self.find("desc 2,3"), [("desc", "2,3")])
        self.assertEqual(self.find("desc 1-3"), [("desc", "1-3")])
        self.assertEqual(self.find("opt desc 3"), [("opt", None), ("desc", "3")])

    def test_number_only_after_number_taking_suffix(self):
        self.assertEqual(self.find("案は 2"), [])
        self.assertEqual(self.find("opt 2"), [])            # opt は番号を取らない
        self.assertEqual(self.find("desc 2 3"), [])         # 最後の 3 の前は番号で、略号でない

    def test_needs_space_before(self):
        self.assertEqual(self.find("これでいい？wdt"), [])
        self.assertEqual(self.find("どう思う？　wdt"), [("wdt", None)])   # 全角の空白も区切り
        self.assertEqual(self.find("どう思う？\nwdt"), [("wdt", None)])

    def test_nothing_after_not_even_punctuation(self):
        for text in ("wdt。", "wdt.", "what comes next.", "next?", "(wdt)", "`wdt`"):
            self.assertEqual(self.find(text), [], text)
        self.assertEqual(self.find("どう思う wdt  \n\n"), [("wdt", None)])    # 後ろの空白と空行は許す

    def test_english_sentence_middle_next(self):
        self.assertEqual(self.find("do the next step and commit"), [])
        self.assertEqual(self.find("next we should fix it"), [])

    def test_word_part(self):
        for text in ("git log --pretty=desc", "--pretty=desc", "pre-commit", "wdt2", "a/next", "ss:"):
            self.assertEqual(self.find(text), [], text)

    def test_only_last_line(self):
        self.assertEqual(self.find("wdt\nこれを直して"), [])

    def test_empty(self):
        for text in ("", "   ", "\n\n", None, 3):
            self.assertEqual(P.find(text, self.cfg()[0]["table"]), [], repr(text))

    def test_code_block(self):
        self.assertEqual(self.find("これを見て\n```\nmake next"), [])            # 閉じていない
        self.assertEqual(self.find("これを見て\n~~~sh\nls\nopt"), [])
        self.assertEqual(self.find("```\nls\n```\nどう？ wdt"), [("wdt", None)])  # 閉じた後は見る
        self.assertEqual(self.find("````\n```\nx\n```\nopt"), [])                 # 長い区切りは同じ長さ以上で閉じる
        self.assertEqual(self.find("```\nx\n```"), [])

    def test_command_like(self):
        for text in ("$ make next", "% make next", "  $ run pre", "cargo build --release pre", "> 引用の wdt"):
            self.assertEqual(self.find(text), [], text)

    def test_slash_and_bang(self):
        self.assertEqual(self.find("/review next"), [])
        self.assertEqual(self.find("!ls next"), [])

    def test_paste_excluded(self):
        # 貼り付けた文の最後が略号でも拾わない（Claude Code が貼り付けの範囲を包んで渡す）
        self.assertEqual(self.find("これを校閲して" + paste("line four end next")), [])
        self.assertEqual(self.find("見て " + paste("a\nb\nc wdt") + "\n\n opt"), [("opt", None)])
        self.assertEqual(self.find("look at this \n" + paste("x\ny") + "\n wdt"), [("wdt", None)])
        self.assertEqual(self.find('見て\n<pasted_content id="abcd">\nnot closed wdt'), [])   # 閉じなければ最後まで

    def test_paste_like_text_with_bad_id_is_not_removed(self):
        self.assertEqual(self.find('<pasted_content id="zz">\nwdt'), [("wdt", None)])

    def test_cap(self):
        self.assertEqual(len(self.find("opt wdt pre ss next desc")), P.MAX_SUFFIXES)


class Config(Base):
    def test_defaults_without_file(self):
        cfg, problems = self.cfg()
        self.assertEqual(problems, [])
        self.assertEqual(set(cfg["table"]), {"wdt", "opt", "pre", "desc", "next", "ss"})
        self.assertTrue(cfg["enabled"] and cfg["notice"])
        self.assertFalse(cfg["number_lists"])

    def test_add_replace_remove(self):
        self.write_cfg({"suffixes": {"TLDR": "3 行で要約する。", "wdt": "意見だけ。", "pre": None, "ss": "",
                                     "pick": {"text": "1 つ選ぶ。", "with_number": "{n} 番で進める。"}}})
        cfg, problems = self.cfg()
        self.assertEqual(problems, [])
        self.assertEqual(cfg["table"]["tldr"], {"text": "3 行で要約する。"})
        self.assertEqual(cfg["table"]["wdt"], {"text": "意見だけ。"})
        self.assertNotIn("pre", cfg["table"])
        self.assertNotIn("ss", cfg["table"])
        self.assertEqual(self.find("まとめて tldr"), [("tldr", None)])
        self.assertEqual(self.find("pick 3"), [("pick", "3")])
        self.assertEqual(self.find("見せて pre"), [])

    def test_errors_only_drop_that_item(self):
        self.write_cfg({"enabled": "yes", "language": "fr", "number_lists": 1, "bogus": 1, "screenshot_dir": 5,
                        "suffixes": {"1bad": "x", "wdt": 3, "desc": {"text": "a", "with_number": "no placeholder"},
                                     "pre": {"text": "a", "extra": 1}, "ok": "fine", "long": "x" * 3000}})
        cfg, problems = self.cfg()
        text = "\n".join(problems)
        for word in ("enabled", "language", "number_lists", "bogus", "screenshot_dir", "1bad", "suffixes.wdt",
                     "suffixes.desc", "{n}", "suffixes.pre", "extra", "suffixes.long"):
            self.assertIn(word, text, word)
        self.assertTrue(cfg["enabled"])                       # 誤った値は既定に戻す
        self.assertEqual(cfg["language"], "ja")
        self.assertNotIn("wdt", cfg["table"])                 # 誤った略号は既定のものも使わない
        self.assertNotIn("desc", cfg["table"])
        self.assertIn("ok", cfg["table"])
        self.assertIn("opt", cfg["table"])                    # ほかの既定はそのまま

    def test_broken_file_and_section(self):
        self.cfg_path.write_text("{broken")
        cfg, problems = self.cfg()
        self.assertIn("読めない", problems[0])
        self.assertIn("wdt", cfg["table"])
        self.write_cfg([1, 2])
        self.assertIn("オブジェクトでない", self.cfg()[1][0])
        self.write_cfg({"suffixes": ["wdt"]})
        cfg, problems = self.cfg()
        self.assertIn("suffixes", problems[0])
        self.assertIn("wdt", cfg["table"])

    def test_env_off(self):
        self.env["CLAUDE_PROMPT_SUFFIX"] = "off"
        self.assertFalse(self.cfg()[0]["enabled"])
        self.assertIsNone(self.hook("どう？ wdt"))

    def test_english(self):
        self.write_cfg({"language": "en"})
        out = self.hook("thoughts? wdt")
        self.assertIn("Treated the trailing \"wdt\"", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(out["systemMessage"], "Shorthand wdt applied")


class Hook(Base):
    def test_output_shape(self):
        out = self.hook("これどう思う？ opt wdt")
        self.assertEqual(out["systemMessage"], "略号 opt・wdt を適用")
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "UserPromptSubmit")
        ctx = hso["additionalContext"]
        lines = ctx.splitlines()
        self.assertTrue(lines[0].startswith(P.TAG))
        self.assertIn("「opt wdt」を略号とみなした。明らかに文の一部なら無視してよい", lines[0])
        self.assertTrue(lines[1].startswith("- opt: 案を 2〜4 つ"))
        self.assertTrue(lines[2].startswith("- wdt: 意見だけ"))

    def test_desc_number_text(self):
        out = self.hook("desc 2")
        self.assertEqual(out["systemMessage"], "略号 desc 2 を適用")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("- desc 2: ", ctx)
        self.assertIn("直前の返答の 2 番の項目", ctx)
        self.assertIn("どこを指すかユーザーに確認", ctx)
        self.assertIn("直前の話題", self.hook("ここ desc")["hookSpecificOutput"]["additionalContext"])

    def test_nothing_when_no_suffix(self):
        self.assertIsNone(self.hook("テストを直して"))
        self.assertIsNone(self.hook(""))

    def test_broken_input(self):
        for raw in ("", "{broken", "[]", "null", json.dumps({"prompt": 3}),
                    json.dumps({"hook_event_name": "Stop", "prompt": "wdt"})):
            self.assertIsNone(P.decide(raw, self.env), raw)

    def test_subagent_prompt_ignored(self):
        self.assertIsNone(self.hook("報告して next", agent_id="a1", agent_type="general-purpose"))

    def test_notice_off(self):
        self.write_cfg({"notice": False})
        out = self.hook("どう？ wdt")
        self.assertNotIn("systemMessage", out)
        self.assertIn("wdt", out["hookSpecificOutput"]["additionalContext"])

    def test_number_lists(self):
        self.write_cfg({"number_lists": True})
        out = self.hook("テストを直して")
        self.assertNotIn("systemMessage", out)               # 毎回の決まりは画面に出さない
        self.assertIn("番号を振る", out["hookSpecificOutput"]["additionalContext"])
        ctx = self.hook("どう？ wdt")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("- wdt:", ctx)
        self.assertIn("番号を振る", ctx)

    def test_config_problem_logged_not_printed(self):
        self.write_cfg({"suffixes": {"wdt": 3}})
        self.assertIsNone(self.hook("どう？ wdt"))
        log = (self.d / "claude-prompt-suffix" / "errors.log").read_text()
        self.assertIn("suffixes.wdt", log)

    def test_cli_hook_and_check(self):
        env = dict(os.environ, **self.env)
        inp = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "どう？ wdt"})
        r = subprocess.run([str(BIN)], input=inp, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout)["systemMessage"], "略号 wdt を適用")
        r = subprocess.run([str(BIN)], input="{broken", env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        r = subprocess.run([str(BIN), "check"], input="直して", env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 1)
        r = subprocess.run([str(BIN), "check"], input="desc 2", env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("略号 desc 2 を適用", r.stdout)
        r = subprocess.run([str(BIN), "list"], env=env, capture_output=True, text=True, timeout=30)
        self.assertIn("desc <番号>:", r.stdout)

    def test_cli_never_fails_on_internal_error(self):
        # lib が例外を投げても、入力は止めない（exit 0、出力なし）。errors.log に残す
        env = dict(os.environ, **self.env)
        fake = self.d / "fake"
        (fake / "bin").mkdir(parents=True)
        (fake / "lib").mkdir()
        (fake / "bin/claude-prompt-suffix").write_text(BIN.read_text())
        real = (ROOT / "lib/promptsuffix.py").read_text()
        (fake / "lib/promptsuffix.py").write_text(real + "\n\ndef decide(t, e):\n    raise RuntimeError('boom')\n")
        r = subprocess.run([sys.executable, str(fake / "bin/claude-prompt-suffix")], input='{"prompt": "wdt"}', env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        self.assertIn("RuntimeError: boom", (self.d / "claude-prompt-suffix" / "errors.log").read_text())


class Screenshot(Base):
    def touch(self, path, mtime):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        os.utime(path, (mtime, mtime))

    def test_latest_in_configured_dir(self):
        shots = self.d / "shots"
        self.touch(shots / "old.png", 1_799_990_000)
        self.touch(shots / "new.PNG", 1_799_999_820)
        self.touch(shots / "newer.txt", 1_799_999_900)      # 画像でないものは数えない
        self.write_cfg({"screenshot_dir": str(shots)})
        ctx = self.hook("これ ss")["hookSpecificOutput"]["additionalContext"]
        self.assertIn(str(shots / "new.PNG"), ctx)
        self.assertIn("3 分前", ctx)
        self.assertNotIn("{screenshot}", ctx)

    def test_list_of_dirs_and_tilde(self):
        self.touch(self.d / "home/a/one.png", 1_799_000_000)
        self.touch(self.d / "home/b/two.jpg", 1_799_500_000)
        self.write_cfg({"screenshot_dir": ["~/a", "$HOME/b", "~/missing"]})
        cfg = self.cfg()[0]
        self.assertEqual(P.latest_screenshot(P.screenshot_dirs(cfg, self.env, "Linux"))[0], self.d / "home/b/two.jpg")

    def test_none_found(self):
        self.write_cfg({"screenshot_dir": str(self.d / "nowhere")})
        ctx = self.hook("ss")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("見つからなかった", ctx)
        self.assertIn(str(self.d / "nowhere"), ctx)

    def test_linux_default_uses_xdg_pictures(self):
        xdg = self.d / "xdg"
        xdg.mkdir()
        (xdg / "user-dirs.dirs").write_text('XDG_PICTURES_DIR="$HOME/画像"\n')
        dirs = P.screenshot_dirs(self.cfg()[0], self.env, "Linux")
        home = self.d / "home"
        self.assertEqual(dirs[:3], [home / "画像/Screenshots", home / "画像/スクリーンショット", home / "画像"])
        self.assertIn(home / "Pictures/Screenshots", dirs)
        self.touch(home / "画像/Screenshots/s.png", 1_799_999_000)
        self.assertEqual(P.latest_screenshot(dirs)[0], home / "画像/Screenshots/s.png")

    def test_linux_default_without_user_dirs(self):
        dirs = P.screenshot_dirs(self.cfg()[0], self.env, "Linux")
        self.assertEqual(dirs[0], self.d / "home/Pictures/Screenshots")
        self.assertEqual(len(dirs), len(set(dirs)))

    def test_macos_location_setting(self):
        home = self.d / "home"
        prefs = home / "Library/Preferences"
        prefs.mkdir(parents=True)
        with open(prefs / "com.apple.screencapture.plist", "wb") as f:
            plistlib.dump({"location": "~/Shots"}, f)
        dirs = P.screenshot_dirs(self.cfg()[0], dict(self.env, HOME=str(home)), "Darwin")
        self.assertEqual(dirs[0], Path(os.path.expanduser("~/Shots")))
        self.assertEqual(dirs[-1], home / "Desktop")
        (prefs / "com.apple.screencapture.plist").write_bytes(b"broken")
        self.assertEqual(P.screenshot_dirs(self.cfg()[0], self.env, "Darwin"), [home / "Desktop"])

    def test_placeholder_in_custom_suffix(self):
        shots = self.d / "shots"
        self.touch(shots / "a.webp", 1_799_999_990)
        self.write_cfg({"screenshot_dir": str(shots), "suffixes": {"look": "画面を見る。{screenshot}"}})
        ctx = self.hook("ここ look")["hookSpecificOutput"]["additionalContext"]
        self.assertIn(str(shots / "a.webp"), ctx)
        self.assertIn("1 分以内", ctx)


class Install(unittest.TestCase):
    def test_merge_hooks_part_is_optional(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import merge_hooks as M
        self.assertIn("suffix", M.OPTIONAL)
        self.assertNotIn("suffix", M.parse_parts(""))
        _l, data = M.install({}, "/r", ["suffix"])
        self.assertEqual(data["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"], "/r/bin/claude-prompt-suffix")
        self.assertEqual(M.status(data)["suffix"][0], "all")
        self.assertEqual(M.uninstall(data, ["suffix"])[1], {})


if __name__ == "__main__":
    unittest.main()
