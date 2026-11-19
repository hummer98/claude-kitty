#!/usr/bin/env python3
"""tools/changelog_section.py（release の本文の取り出し）のテスト。python3 tests/changelog_test.py"""
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from changelog_section import headings, main, normalize, section  # noqa: E402

SAMPLE = """# 変更履歴

前書き。

## [Unreleased]

## [0.2.0] - 2026-11-20

### 追加

- 新しいもの

### 上げるときに手で行うこと

1. 手順

## [0.1.0] - 2026-11-01

最初の公開。

[Unreleased]: https://example.com/compare/v0.2.0...HEAD
[0.2.0]: https://example.com/compare/v0.1.0...v0.2.0
[0.1.0]: https://example.com/releases/tag/v0.1.0
"""


class Section(unittest.TestCase):
    def test_middle_section_stops_at_next_heading(self):
        self.assertEqual(section(SAMPLE, "0.2.0"), "### 追加\n\n- 新しいもの\n\n### 上げるときに手で行うこと\n\n1. 手順")

    def test_last_section_drops_link_definitions(self):
        self.assertEqual(section(SAMPLE, "0.1.0"), "最初の公開。")

    def test_tag_name_is_accepted(self):
        self.assertEqual(section(SAMPLE, "v0.2.0"), section(SAMPLE, "0.2.0"))
        self.assertEqual(normalize(" v1.0.0 "), "1.0.0")

    def test_missing_and_prefix_versions(self):
        self.assertIsNone(section(SAMPLE, "0.3.0"))
        self.assertIsNone(section(SAMPLE, "0.2"))  # 前方一致では当てない
        self.assertIsNone(section("", "0.1.0"))

    def test_empty_unreleased(self):
        self.assertEqual(section(SAMPLE, "Unreleased"), "")

    def test_headings(self):
        self.assertEqual(headings(SAMPLE), [("Unreleased", None), ("0.2.0", "2026-11-20"), ("0.1.0", "2026-11-01")])

    def test_crlf_and_heading_without_date(self):
        text = "## [1.0.0]\r\n\r\n- a\r\n## [0.9.0] - 2026-01-01\r\n- b\r\n"
        self.assertEqual(section(text, "1.0.0"), "- a")
        self.assertEqual(section(text, "0.9.0"), "- b")

    def test_real_changelog_has_every_released_version(self):
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        vers = [v for v, _ in headings(text)]
        self.assertEqual(vers[0], "Unreleased")
        for v in vers[1:]:
            with self.subTest(v=v):
                body = section(text, v)
                self.assertTrue(body and body.strip())
                self.assertIn(f"[{v}]: https://github.com/hummer98/claude-kitty/", text)  # 比較リンク


class Cli(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "tools/changelog_section.py"), *args],
                              capture_output=True, text=True, timeout=20)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "CHANGELOG.md"
            f.write_text(SAMPLE, encoding="utf-8")
            r = self.run_cli("v0.1.0", "--file", str(f))
            self.assertEqual((r.returncode, r.stdout), (0, "最初の公開。\n"))
            r = self.run_cli("0.9.9", "--file", str(f))
            self.assertEqual((r.returncode, r.stdout), (1, ""))
            self.assertIn("0.9.9", r.stderr)
            r = self.run_cli("Unreleased", "--file", str(f))  # 空の節は release にしない
            self.assertEqual(r.returncode, 1)
            self.assertEqual(self.run_cli("0.1.0", "--file", str(Path(d) / "none")).returncode, 2)
        self.assertEqual(self.run_cli().returncode, 2)
        self.assertEqual(self.run_cli("0.1.0", "--file").returncode, 2)
        self.assertEqual(self.run_cli("").returncode, 2)

    def test_main_default_file_is_repo_changelog(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["0.1.0"]), 0)
        self.assertIn("最初の公開", out.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=1)
