#!/usr/bin/env python3
"""tools/release.sh と release.yml の本文の取り出しのテスト。python3 tests/release_test.py

一時フォルダに git の作業ツリーと、origin 役の bare リポジトリを作って試す（本物の origin には触れない）。
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE = ROOT / "tools" / "release.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"

CHANGELOG = """# 変更履歴

## [Unreleased]

## [0.2.0] - 2026-11-20

### 追加

- 二つめ

## [0.1.0] - 2026-11-01

最初の公開。

[Unreleased]: https://example.com/compare/v0.2.0...HEAD
[0.2.0]: https://example.com/compare/v0.1.0...v0.2.0
[0.1.0]: https://example.com/releases/tag/v0.1.0
"""


def git_env():
    """一時のリポジトリの commit 用。利用者の git の設定（~/.gitconfig）は読まない。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "KITTY_", "CLAUDE"))}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com", GIT_TERMINAL_PROMPT="0")
    return env


class Repo:
    def __init__(self, base, tests_ok=True):
        self.env = git_env()
        self.origin = base / "origin.git"
        self.work = base / "work"
        self.git_at(base, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.git_at(base, "init", "-q", "-b", "main", str(self.work))
        self.git("remote", "add", "origin", str(self.origin))
        (self.work / "tools").mkdir()
        (self.work / "tests").mkdir()
        shutil.copy(RELEASE, self.work / "tools" / "release.sh")
        shutil.copy(ROOT / "tools" / "changelog_section.py", self.work / "tools" / "changelog_section.py")
        (self.work / "a.txt").write_text("1\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "first")  # 0.1.0 の頃（VERSION も CHANGELOG も無い）
        self.first = self.git("rev-parse", "HEAD").strip()
        (self.work / "VERSION").write_text("0.2.0\n")
        (self.work / "CHANGELOG.md").write_text(CHANGELOG)
        (self.work / "tests" / "run.py").write_text(
            "import sys\nopen(__file__ + '.ran', 'w').close()\nsys.exit(%d)\n" % (0 if tests_ok else 1))
        (self.work / ".gitignore").write_text("*.ran\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "second")
        self.git("push", "-q", "origin", "main")
        # 公開しないタグ（別の履歴の commit にも、main の commit にも）
        self.git("tag", "private-history-x", self.first)
        self.git("tag", "-a", "private-note", "-m", "private")  # 注釈付き・main から届く（followTags で出ていきうる）
        self.git("tag", "other-tag")

    def git_at(self, cwd, *args, ok=(0,)):
        r = subprocess.run(["git", *args], cwd=cwd, env=self.env, capture_output=True, text=True, timeout=60)
        assert r.returncode in ok, (args, r.stdout, r.stderr)
        return r.stdout

    def git(self, *args, ok=(0,)):
        return self.git_at(self.work, *args, ok=ok)

    def release(self, *args):
        return subprocess.run(["bash", str(self.work / "tools" / "release.sh"), *args], cwd=self.work, env=self.env,
                              capture_output=True, text=True, timeout=120)

    def remote_refs(self):
        out = self.git_at(self.origin, "for-each-ref", "--format=%(refname)")
        return sorted(out.split())


class ReleaseScript(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = Repo(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_release_pushes_only_main_and_the_tag(self):
        # followTags が有効でも、注釈付きの private-* が一緒に出ていかないこと
        self.r.git("config", "push.followTags", "true")
        (self.r.work / "b.txt").write_text("x\n")
        self.r.git("add", "-A")
        self.r.git("commit", "-q", "-m", "third")
        res = self.r.release("0.2.0")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.r.remote_refs(), ["refs/heads/main", "refs/tags/v0.2.0"])
        head = self.r.git("rev-parse", "HEAD").strip()
        self.assertEqual(self.r.git_at(self.r.origin, "rev-parse", "main").strip(), head)
        self.assertEqual(self.r.git_at(self.r.origin, "rev-parse", "v0.2.0").strip(), head)
        self.assertEqual(self.r.git("cat-file", "-t", "v0.2.0").strip(), "commit")  # 軽量タグ
        self.assertTrue((self.r.work / "tests" / "run.py.ran").exists())
        self.assertIn("二つめ", res.stdout)  # release の本文を見せる

    def test_dry_run_changes_nothing(self):
        before = self.r.git("for-each-ref").strip()
        res = self.r.release("v0.2.0", "--dry-run")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.r.git("for-each-ref").strip(), before)
        self.assertEqual(self.r.remote_refs(), ["refs/heads/main"])
        self.assertIn("[dry-run] git tag v0.2.0", res.stdout)
        self.assertIn("[dry-run] git push --no-follow-tags origin refs/heads/main:refs/heads/main", res.stdout)
        self.assertIn("[dry-run] git push --no-follow-tags origin refs/tags/v0.2.0:refs/tags/v0.2.0", res.stdout)
        pushes = [line for line in res.stdout.splitlines() if "git push" in line]
        self.assertEqual(len(pushes), 2)
        for line in pushes:
            self.assertNotRegex(line, r"--all|--tags|--mirror|private")

    def test_past_version_at_commit(self):
        # 手元の main が origin より先にあっても、--at はテストしていない main を push しない
        (self.r.work / "b.txt").write_text("x\n")
        self.r.git("add", "-A")
        self.r.git("commit", "-q", "-m", "untested")
        origin_main = self.r.git_at(self.r.origin, "rev-parse", "main").strip()
        res = self.r.release("0.1.0", "--at", self.r.first)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.r.remote_refs(), ["refs/heads/main", "refs/tags/v0.1.0"])
        self.assertEqual(self.r.git_at(self.r.origin, "rev-parse", "main").strip(), origin_main)
        self.assertEqual(self.r.git_at(self.r.origin, "rev-parse", "v0.1.0").strip(), self.r.first)
        self.assertFalse((self.r.work / "tests" / "run.py.ran").exists())  # 過去の commit にはいまのテストを当てない
        self.assertIn("gh workflow run release.yml -f tag=v0.1.0", res.stdout)  # push では workflow が起動しないため

    def test_at_needs_changelog_on_origin(self):
        # origin/main の CHANGELOG に節が無ければ（main をまだ push していない）、Actions が本文を作れないので止める
        text = CHANGELOG.replace("## [0.1.0] - 2026-11-01\n\n最初の公開。\n", "")
        (self.r.work / "CHANGELOG.md").write_text(text)
        self.r.git("commit", "-qam", "drop 0.1.0")
        self.r.git("push", "-q", "origin", "main")
        (self.r.work / "CHANGELOG.md").write_text(CHANGELOG)
        self.r.git("commit", "-qam", "add 0.1.0 back, not pushed")
        self.refused("0.1.0", "--at", self.r.first, msg="origin/main の CHANGELOG.md")

    def test_failed_push_removes_the_local_tag(self):
        # main の push が拒まれたら（origin が先に進んでいる）、手元のタグを残さない（そのまま再実行できる）
        other = Path(self.tmp.name) / "other"
        self.r.git_at(Path(self.tmp.name), "clone", "-q", str(self.r.origin), str(other))
        (other / "c.txt").write_text("c\n")
        self.r.git_at(other, "add", "-A")
        self.r.git_at(other, "commit", "-q", "-m", "elsewhere")
        self.r.git_at(other, "push", "-q", "origin", "main")
        (self.r.work / "b.txt").write_text("x\n")
        self.r.git("add", "-A")
        self.r.git("commit", "-q", "-m", "local")
        res = self.r.release("0.2.0")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("main を push できない", res.stderr)
        self.assertEqual(self.r.git("tag", "-l", "v0.2.0").strip(), "")
        self.assertEqual(self.r.remote_refs(), ["refs/heads/main"])

    def refused(self, *args, msg=None):
        before_local = self.r.git("for-each-ref").strip()
        res = self.r.release(*args)
        self.assertNotEqual(res.returncode, 0, res.stdout)
        if msg:
            self.assertIn(msg, res.stderr)
        self.assertEqual(self.r.git("for-each-ref").strip(), before_local)
        self.assertEqual(self.r.remote_refs(), ["refs/heads/main"])
        return res

    def test_refuses_version_mismatch(self):
        self.refused("0.3.0", msg="VERSION")

    def test_refuses_missing_heading(self):
        (self.r.work / "VERSION").write_text("0.3.0\n")
        self.r.git("commit", "-qam", "bump")
        self.refused("0.3.0", msg="見出しが無い")

    def test_refuses_heading_without_date(self):
        text = CHANGELOG.replace("## [0.2.0] - 2026-11-20", "## [0.2.0]")
        (self.r.work / "CHANGELOG.md").write_text(text)
        self.r.git("commit", "-qam", "no date")
        self.refused("0.2.0", msg="見出しが無い")

    def test_refuses_existing_tag(self):
        self.r.git("tag", "v0.2.0")
        res = self.r.release("0.2.0")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("手元にもうある", res.stderr)
        self.assertEqual(self.r.remote_refs(), ["refs/heads/main"])

    def test_refuses_tag_on_origin(self):
        self.r.git("push", "-q", "origin", "main:refs/tags/v0.2.0")
        res = self.r.release("0.2.0")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("origin にもうある", res.stderr)

    def test_refuses_dirty_tree_and_other_branch(self):
        (self.r.work / "a.txt").write_text("changed\n")
        self.refused("0.2.0", msg="変更がある")
        self.r.git("checkout", "-q", "--", "a.txt")
        self.r.git("checkout", "-q", "-b", "topic")
        self.refused("0.2.0", msg="main にいない")

    def test_refuses_bad_versions(self):
        for v in ("private-1", "0.2", "v", "0.2.0;rm", "../0.2.0", "01.2.3"):
            with self.subTest(v=v):
                self.refused(v, msg="SemVer")
        self.assertEqual(self.r.release().returncode, 2)
        self.assertEqual(self.r.release("0.2.0", "--tags").returncode, 2)

    def test_refuses_failing_tests(self):
        (self.r.work / "tests" / "run.py").write_text("import sys\nsys.exit(1)\n")
        self.r.git("commit", "-qam", "red")
        self.refused("0.2.0", msg="テストが通らない")

    def test_refuses_at_outside_main_and_mirror(self):
        self.refused("0.1.0", "--at", "private-history-x~0^{tree}", msg="commit が無い")
        self.r.git("checkout", "-q", "--orphan", "side")
        self.r.git("commit", "-q", "-m", "side")
        side = self.r.git("rev-parse", "HEAD").strip()
        self.r.git("checkout", "-q", "main")
        self.refused("0.1.0", "--at", side, msg="origin/main の祖先でない")
        self.r.git("config", "remote.origin.mirror", "true")
        self.refused("0.2.0", msg="mirror")


def workflow_script(name):
    """release.yml の、name の付いた step の run: | の本文（インデントを外したもの）。"""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {name}")
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    body = []
    indent = None
    for line in lines[run + 1:]:
        if line.strip() and indent is None:
            indent = len(line) - len(line.lstrip())
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line[indent:] if line.strip() else "")
    return "\n".join(body) + "\n"


class ReleaseWorkflow(unittest.TestCase):
    """release.yml の本文を取り出す step を、Actions の代わりに手元の一時リポジトリで走らせる。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.r = Repo(base)
        # Actions の checkout と同じく、origin から clone した作業ツリー（origin/main がある）
        self.clone = base / "clone"
        self.r.git_at(base, "clone", "-q", str(self.r.origin), str(self.clone))
        # 既定の枝の CHANGELOG と tools は origin/main に入れておく（release.sh が main を先に push する形）
        self.r.git("push", "-q", "origin", "main")
        self.script = workflow_script("Extract the release notes from CHANGELOG.md")
        self.runner = base / "runner"
        self.runner.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_step(self, tag, sha):
        self.r.git_at(self.clone, "fetch", "-q", "--tags", "origin")
        self.r.git_at(self.clone, "checkout", "-q", "--detach", sha)
        envfile = self.runner / "env"
        envfile.write_text("")
        env = dict(self.r.env, TAG=tag, RUNNER_TEMP=str(self.runner), GITHUB_ENV=str(envfile), DEFAULT_BRANCH="main")
        r = subprocess.run(["bash", "-e", "-c", self.script], cwd=self.clone, env=env, capture_output=True, text=True,
                           timeout=60)
        out = dict(line.split("=", 1) for line in envfile.read_text().split())
        notes = self.runner / "notes.md"
        return r, out, notes.read_text() if notes.exists() else None

    def test_current_version(self):
        head = self.r.git("rev-parse", "HEAD").strip()
        self.r.git("tag", "v0.2.0")
        self.r.git("push", "-q", "origin", "refs/tags/v0.2.0")
        r, out, notes = self.run_step("v0.2.0", head)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(notes, "### 追加\n\n- 二つめ\n")
        self.assertEqual(out, {"latest": "true", "prerelease": "false"})

    def test_past_version_uses_main_changelog_and_is_not_latest(self):
        # v0.1.0 の commit には VERSION も CHANGELOG も無い
        for tag, sha in (("v0.2.0", "HEAD"), ("v0.1.0", self.r.first)):
            self.r.git("tag", tag, sha)
            self.r.git("push", "-q", "origin", f"refs/tags/{tag}")
        r, out, notes = self.run_step("v0.1.0", self.r.first)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(notes, "最初の公開。\n")
        self.assertEqual(out, {"latest": "false", "prerelease": "false"})

    def push_tag(self, tag, sha):
        self.r.git("tag", tag, sha)
        self.r.git("push", "-q", "origin", f"refs/tags/{tag}")

    def test_tag_not_matching_version_fails(self):
        head = self.r.git("rev-parse", "HEAD").strip()
        self.push_tag("v0.1.0", head)
        r, _, _ = self.run_step("v0.1.0", head)  # VERSION は 0.2.0
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("does not match VERSION", r.stdout + r.stderr)

    def test_tag_outside_default_branch_fails(self):
        self.r.git("checkout", "-q", "--orphan", "side")
        self.r.git("commit", "-q", "-m", "side")
        side = self.r.git("rev-parse", "HEAD").strip()
        self.r.git("push", "-q", "origin", "side")
        self.push_tag("v9.9.9", side)
        r, _, _ = self.run_step("v9.9.9", side)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("is not on main", r.stdout + r.stderr)

    def test_missing_section_fails(self):
        (self.r.work / "VERSION").write_text("0.3.0\n")
        self.r.git("commit", "-qam", "bump")
        self.r.git("push", "-q", "origin", "main")
        head = self.r.git("rev-parse", "HEAD").strip()
        self.push_tag("v0.3.0", head)
        r, _, _ = self.run_step("v0.3.0", head)
        self.assertNotEqual(r.returncode, 0)

    def test_dispatch_input_must_be_a_version_tag(self):
        # workflow_dispatch の入力は人が打つので、v で始まる版のタグだけを受ける
        self.push_tag("private-x", "HEAD")
        r, _, notes = self.run_step("private-x", self.r.git("rev-parse", "HEAD").strip())
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not a version tag", r.stdout + r.stderr)
        self.assertIsNone(notes)

    def test_prerelease_when_no_plain_version_tag_exists(self):
        # v* がプレリリースだけでも（grep が何も返さなくても）落ちない
        text = CHANGELOG.replace("## [0.2.0] - 2026-11-20", "## [0.2.0-rc1] - 2026-11-20")
        (self.r.work / "CHANGELOG.md").write_text(text)
        (self.r.work / "VERSION").write_text("0.2.0-rc1\n")
        self.r.git("commit", "-qam", "rc")
        self.r.git("push", "-q", "origin", "main")
        self.push_tag("v0.2.0-rc1", "HEAD")
        r, out, notes = self.run_step("v0.2.0-rc1", self.r.git("rev-parse", "HEAD").strip())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out, {"latest": "false", "prerelease": "true"})
        self.assertEqual(notes, "### 追加\n\n- 二つめ\n")

    def test_workflow_uses_expected_actions(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertRegex(text, r"tags: \[\"v\*\"\]")
        self.assertIn("fetch-depth: 0", text)
        self.assertIn("contents: write", text)
        self.assertIn("workflow_dispatch:", text)
        self.assertTrue(re.search(r"gh release create \"\$TAG\"", text))


if __name__ == "__main__":
    unittest.main(verbosity=1)
