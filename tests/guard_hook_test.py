#!/usr/bin/env python3
"""claude-guard の単体テスト。python3 tests/guard_hook_test.py（標準ライブラリだけ）"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import guardhook as gh  # noqa: E402

BIN = ROOT / "bin" / "claude-guard"


def fake_probe(process=False, command=False, calls=None):
    class P:
        @staticmethod
        def process(pats):
            if calls is not None:
                calls.append(("process", tuple(pats)))
            return process

        @staticmethod
        def command(argv, timeout):
            if calls is not None:
                calls.append(("command", tuple(argv)))
            return command
    return P


class Base(unittest.TestCase):
    """偽の HOME（中に /home → /var/home に当たるシンボリックリンク）、設定、状態の置き場所を作る。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.real_home = t / "var" / "home" / "u"
        self.shared_dir = self.real_home / "app" / "shared" / "files"
        (self.shared_dir / "m1").mkdir(parents=True)
        (self.real_home / "dev" / "m1").mkdir(parents=True)
        (t / "home").symlink_to(t / "var" / "home")       # /home → /var/home の形
        self.link_home = t / "home" / "u"
        self.run_dir = t / "run"
        self.run_dir.mkdir()
        self.cfg = t / "config.json"
        self.env = {"HOME": str(self.real_home), "XDG_RUNTIME_DIR": str(self.run_dir), "CLAUDE_KITTY_CONFIG": str(self.cfg)}
        self.write_config(self.default_guards())

    def tearDown(self):
        self.tmp.cleanup()

    def default_guards(self):
        return {
            "app-running": {
                "process": "^\\./fullscreen-app",
                "paths": ["~/app/shared/files"],
                "commands": [{"program": "^svcctl(\\.py)?$", "value_options": ["--owner"],
                              "block": ["deploy", "scale", "backup", {"sub": "config", "if_any": ["--set"]},
                                        {"sub": "reset", "unless_any": ["--list", "--dry-run"]}]}],
                "allow_commands": [{"regex": "^svcctl(\\.py)? .*deploy .*--dry-run"}],
                "message": "{name} の後で。",
            },
            "deploy": {
                "paths": ["~/svc/deploy"],
                "commands": ["git push", {"regex": "^deploy\\.sh\\b"}],
                "allow_commands": ["git push --dry-run"],
                "message": "デプロイ中: {reason}",
            },
            "device-test": {
                "command": ["test", "-e", "/nonexistent"],
                "paths": ["~/lab/config"],
            },
        }

    def write_config(self, guards, other=None):
        data = dict(other or {"view": {"left": "main"}})
        data["guard_hook"] = {"guards": guards}
        self.cfg.write_text(json.dumps(data))

    def hook_input(self, tool, tool_input, cwd=None, session="s-other"):
        return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input,
                "cwd": cwd or str(self.real_home), "session_id": session}

    def bash(self, command, cwd=None, probe=None, session="s-other", env=None):
        data = self.hook_input("Bash", {"command": command}, cwd, session)
        return gh.evaluate(data, env or self.env, probe or fake_probe(process=True))

    def write(self, path, tool="Write", cwd=None, probe=None):
        key = "notebook_path" if tool == "NotebookEdit" else "file_path"
        data = self.hook_input(tool, {key: path}, cwd)
        return gh.evaluate(data, self.env, probe or fake_probe(process=True))


class NotRunning(Base):
    """働いていなければ、どの例も止めない。"""

    CASES = ["git -C {m} merge main", "cd {m}/m1 && git pull", "rm -rf {m}/m1", "echo x > {m}/m1/a.cfg",
             "mkdir {m}/new", "svcctl.py --owner me deploy v1", "python3 tools/svcctl.py scale 4"]

    def test_bash_cases_pass(self):
        calls = []
        for c in self.CASES:
            self.assertIsNone(self.bash(c.format(m=self.shared_dir), probe=fake_probe(calls=calls)), c)
        self.assertTrue(calls)       # 候補があれば条件は調べる

    def test_write_passes(self):
        self.assertIsNone(self.write(str(self.shared_dir / "m1" / "a.cfg"), probe=fake_probe()))

    def test_no_candidate_no_probe(self):
        calls = []
        self.assertIsNone(self.bash("ls -la && git status", probe=fake_probe(process=True, calls=calls)))
        self.assertEqual(calls, [])  # 止める候補が無ければ pgrep を呼ばない


class Paths(Base):
    def test_edit_write_notebook_blocked(self):
        for tool in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
            r = self.write(str(self.shared_dir / "m1" / "a.cfg"), tool=tool)
            self.assertIsNotNone(r, tool)
            self.assertIn("[claude-guard]", r)
            self.assertIn("app-running", r)

    def test_write_elsewhere_passes(self):
        self.assertIsNone(self.write(str(self.real_home / "dev" / "m1" / "a.cfg")))

    def test_home_symlink_form(self):
        self.assertIsNotNone(self.write(str(self.link_home / "app" / "shared" / "files" / "m1" / "a.cfg")))

    def test_relative_path_with_cwd(self):
        self.assertIsNotNone(self.write("m1/a.cfg", cwd=str(self.shared_dir)))
        self.assertIsNotNone(self.write("../files/m1/a.cfg", cwd=str(self.shared_dir / "m1")))
        self.assertIsNone(self.write("m1/a.cfg", cwd=str(self.real_home / "dev")))

    def test_root_itself_and_prefix_sibling(self):
        self.assertIsNotNone(self.bash(f"rm -rf {self.shared_dir}"))
        self.assertIsNone(self.write(str(self.shared_dir) + "_backup/x"))   # files_backup は別のフォルダ


class BashWrites(Base):
    def test_git_under_files(self):
        for c in ("merge main", "pull", "commit -m x", "checkout .", "reset --hard", "rebase main",
                  "cherry-pick abc", "stash pop", "stash"):
            self.assertIsNotNone(self.bash(f"git -C {self.shared_dir}/m1 {c}"), c)
            self.assertIsNotNone(self.bash(f"cd {self.shared_dir}/m1 && git {c}"), c)

    def test_git_reads_pass(self):
        for c in ("status", "log --oneline", "diff", "stash list", "show HEAD", "add -n ."):
            self.assertIsNone(self.bash(f"cd {self.shared_dir}/m1 && git {c}"), c)

    def test_git_elsewhere_passes(self):
        self.assertIsNone(self.bash(f"cd {self.real_home}/dev/m1 && git pull && git merge x && git commit -m y"))
        self.assertIsNone(self.bash("cd ~/dev/m1 && git reset --hard"))

    def test_cd_tilde_and_home_var(self):
        self.assertIsNotNone(self.bash("cd ~/app/shared/files/m1 && git pull"))
        self.assertIsNotNone(self.bash("cd $HOME/app/shared/files && mkdir x"))
        self.assertIsNotNone(self.bash("D=~/app/shared/files; cd \"$D\" && rm a"))
        self.assertIsNotNone(self.bash(f"cd {self.link_home}/app/shared/files/m1; git merge x"))

    def test_relative_cd_chain(self):
        self.assertIsNotNone(self.bash("cd app && cd shared/files && mv a b"))
        self.assertIsNone(self.bash("cd app/shared/files && cd ~/dev && mv a b"))

    def test_file_ops(self):
        m = self.shared_dir
        for c in (f"mkdir -p {m}/new", f"mv {m}/m1 {m}/m2", f"rm {m}/m1/a", f"cp ~/dev/m1/a.cfg {m}/m1/",
                  f"ln -s ~/dev/m1 {m}/m3", f"rsync -a ~/dev/m1/ {m}/m1/", f"touch {m}/x",
                  f"tar xzf ~/a.tgz -C {m}", f"cd {m} && tar -xf ~/a.tar", f"unzip ~/a.zip -d {m}",
                  f"cd {m} && unzip ~/a.zip", f"sed -i s/a/b/ {m}/m1/a.cfg", f"find {m} -name '*.bak' -delete",
                  f"echo hi | tee {m}/x"):
            self.assertIsNotNone(self.bash(c), c)

    def test_copy_out_is_read(self):
        m = self.shared_dir
        for c in (f"cp -r {m}/m1 ~/dev/", f"rsync -a {m}/m1/ ~/dev/m1/", f"tar czf ~/b.tgz -C {m} m1",
                  f"unzip -l ~/a.zip", f"sed -n 1,5p {m}/m1/a.cfg", f"cat {m}/m1/a.cfg", f"ls {m}",
                  f"grep -r x {m}", f"find {m} -name '*.cfg'"):
            self.assertIsNone(self.bash(c), c)

    def test_redirects(self):
        m = self.shared_dir
        self.assertIsNotNone(self.bash(f"echo x > {m}/m1/a.cfg"))
        self.assertIsNotNone(self.bash(f"echo x >> {m}/m1/a.cfg"))
        self.assertIsNotNone(self.bash(f"cd {m}/m1 && echo x>a.cfg"))
        self.assertIsNotNone(self.bash(f"cat > {m}/m1/a.cfg <<'EOF'\ndon't\nEOF"))
        self.assertIsNone(self.bash(f"cd {m}/m1 && ls 2>&1 > /dev/null"))
        self.assertIsNone(self.bash(f"cat {m}/m1/a.cfg > ~/dev/out.txt 2>/dev/null"))

    def test_heredoc_body_is_not_a_command(self):
        self.assertIsNone(self.bash(f"cat <<EOF > ~/dev/x\nrm -rf {self.shared_dir}\nEOF\necho ok"))

    def test_bash_c_and_wrappers(self):
        self.assertIsNotNone(self.bash(f"bash -c 'cd {self.shared_dir} && git pull'"))
        self.assertIsNotNone(self.bash(f"timeout 30 env A=1 rm -r {self.shared_dir}/m1"))
        self.assertIsNotNone(self.bash(f"(cd {self.shared_dir}/m1; git stash pop)"))

    def test_broken_quotes_are_split_roughly(self):
        self.assertIsNotNone(self.bash(f"rm -rf {self.shared_dir}/m1 'unclosed"))
        self.assertIsNone(self.bash("echo 'unclosed"))


class ShellForms(Base):
    """レビューで見つかった穴と誤検出の回帰テスト。"""

    def test_fd_numbers_are_not_arguments(self):
        m = self.shared_dir
        for c in (f"cp a {m}/ 2>&1", f"cp ~/dev/a {m}/ 2>/dev/null", f"rsync -a ~/dev/ {m}/ 2>&1"):
            self.assertIsNotNone(self.bash(c), c)
        for c in (f"cd {m} && rm ~/dev/x 2>/dev/null", f"cd {m} && mkdir -p ~/dev/x 2>&1",
                  f"cd {m} && cp ~/dev/a ~/dev/b 2>/dev/null", f"cd {m} && ls 2> ~/dev/err"):
            self.assertIsNone(self.bash(c), c)

    def test_hash_inside_word_is_not_comment(self):
        m = self.shared_dir
        for c in (f"echo a#b; rm -rf {m}/m1", f"curl http://x/#a; rm -rf {m}/m1", f"git log --format=%H#x; rm -rf {m}/m1"):
            self.assertIsNotNone(self.bash(c), c)
        self.assertIsNone(self.bash(f"ls  # rm -rf {m}/m1"))
        self.assertIsNone(self.bash(f"echo '# x'; ls {m}"))

    def test_compound_statements(self):
        m = self.shared_dir
        for c in (f"if true; then rm -rf {m}/m1; fi", f"for f in a b; do rm {m}/$f; done",
                  f"while false; do mkdir {m}/x; done", f"{{ rm -rf {m}/m1; }}", f"! rm -rf {m}/m1",
                  f"x=`rm -rf {m}/m1`", f"if [ -d x ]; then echo; else mv {m}/a {m}/b; fi"):
            self.assertIsNotNone(self.bash(c), c)
        self.assertIsNone(self.bash(f"for f in {m}/*; do echo $f; done"))

    def test_git_global_options_with_prefix_rule(self):
        gh.write_state(self.env, "deploy", "on", 10, "r")
        p = fake_probe()
        for c in ("git -C ~/dev push", "git -c x=y push origin", "git --no-pager push"):
            self.assertIsNotNone(self.bash(c, probe=p), c)
        self.assertIsNone(self.bash("git -C ~/dev push --dry-run", probe=p))

    def test_allow_commands_keep_path_reasons(self):
        gh.write_state(self.env, "deploy", "on", 10, "r")
        r = self.bash(f"git push --dry-run > {self.real_home}/svc/deploy/a", probe=fake_probe())
        self.assertIsNotNone(r)

    def test_shell_combined_flags(self):
        for c in (f"bash -lc 'rm -rf {self.shared_dir}/m1'", f"sh -ec 'cd {self.shared_dir}; git pull'"):
            self.assertIsNotNone(self.bash(c), c)

    def test_wrapper_flags(self):
        self.assertIsNotNone(self.bash(f"env -i rm -rf {self.shared_dir}/m1"))
        self.assertIsNotNone(self.bash("python3 -X dev svcctl.py deploy a"))
        self.assertIsNotNone(self.bash(f"sudo -u me rm {self.shared_dir}/x"))

    def test_more_git_writes(self):
        m = self.shared_dir
        for c in ("bisect start", "submodule update", "sparse-checkout set x", "checkout-index -a", "read-tree -u HEAD",
                  "format-patch HEAD~1", "archive -o x.zip HEAD"):
            self.assertIsNotNone(self.bash(f"cd {m}/m1 && git {c}"), c)
        self.assertIsNotNone(self.bash(f"GIT_WORK_TREE={m}/m1 git reset --hard"))
        for c in ("submodule status", "read-tree HEAD", "archive HEAD", "add -A", "branch -a", "tag"):
            self.assertIsNone(self.bash(f"cd {m}/m1 && git {c}"), c)

    def test_git_clone_value_options(self):
        # 回帰: clone の値をとるオプション（--branch master）の値を元、元を行き先と読み、保護した所から外へ写すのを止めていた
        m, out = self.shared_dir / "m1", self.real_home / "dev" / "wt"
        for opts in ("-q --branch master", "--branch=master", "-b master", "-bmaster", "-qb master", "-qbmaster",
                     "--depth 1", "--bra master", "-o up -c core.x=1 --filter blob:none", "--reference ~/dev/m1",
                     "-j 4 --shallow-since 2020-01-01 --revision v1", "--no-template --single-branch", "-- "):
            self.assertIsNone(self.bash(f"git clone {opts} {m} {out}"), opts)
            self.assertIsNotNone(self.bash(f"git clone {opts} ~/dev/m1 {m}/new"), opts)
        self.assertIsNone(self.bash(f"git clone {m} {out}"))
        self.assertIsNotNone(self.bash(f"cd {m} && git clone -b master ~/dev/m1"))   # 行き先なしは cwd の下
        self.assertIsNone(self.bash(f"cd {m} && git clone -b master ~/dev/m1 ~/dev/x"))

    def test_git_clone_init_git_dir_options(self):
        m = self.shared_dir
        for opts in (f"--separate-git-dir {m}/g", f"--separate-git-dir={m}/g", f"--sep {m}/g", f"--template {m}/t",
                     f"--template={m}/t"):
            self.assertIsNotNone(self.bash(f"git clone {opts} ~/dev/m1 ~/dev/wt"), opts)
            self.assertIsNotNone(self.bash(f"git init {opts} ~/dev/wt"), opts)
        self.assertIsNone(self.bash("git clone --separate-git-dir ~/dev/g ~/dev/m1 ~/dev/wt"))
        self.assertIsNotNone(self.bash(f"git clone --sep={m}/g ~/dev/m1 ~/dev/wt"))
        # GIT_DIR / --git-dir の場所にもリポジトリを作る
        self.assertIsNotNone(self.bash(f"GIT_DIR={m}/g git init ~/dev/x"))
        self.assertIsNotNone(self.bash(f"git --git-dir={m}/g init ~/dev/x"))
        self.assertIsNone(self.bash("GIT_DIR=~/dev/g git init ~/dev/x"))
        self.assertIsNone(self.bash(f"cd {m} && git init ~/dev/x"))     # cwd だけなら行き先で決める

    def test_git_clone_attached_and_unknown_values(self):
        m = self.shared_dir
        for opts in ("-ccore.x=1", "-nb x", "-qj4"):
            self.assertIsNone(self.bash(f"git clone {opts} {m} ~/dev/wt"), opts)
            self.assertIsNotNone(self.bash(f"git clone {opts} ~/dev/m1 {m}/x"), opts)
        # 知らない値をとるオプション（--future v）で行き先がずれても、後ろの引数を全部見る
        self.assertIsNotNone(self.bash(f"git clone --future v ~/dev/m1 {m}/x"))
        self.assertIsNotNone(self.bash(f"git init --future v {m}/x"))

    def test_git_format_patch_o_inside_other_short_options(self):
        # 回帰: -Sfoo / -Gbo の中の o を -o と読み、前の -o <保護> を上書きして通していた
        m = self.shared_dir
        for extra in ("-Sfoo", "-Gbo"):
            self.assertIsNotNone(self.bash(f"cd ~/dev/m1 && git format-patch -o {m}/p {extra} HEAD~1"), extra)
            self.assertIsNotNone(self.bash(f"cd ~/dev/m1 && git format-patch {extra} -o {m}/p HEAD~1"), extra)
            self.assertIsNone(self.bash(f"cd ~/dev/m1 && git format-patch -o ~/dev/p {extra} HEAD~1"), extra)
        self.assertIsNotNone(self.bash(f"cd ~/dev/m1 && git format-patch -ko {m}/p HEAD~1"))    # 本当の -o のまとまり
        self.assertIsNotNone(self.bash(f"cd ~/dev/m1 && git archive -9o{m}/a.tar HEAD"))
        self.assertIsNone(self.bash("cd ~/dev/m1 && git archive -9 HEAD"))                      # 標準出力へ

    def test_git_clone_init_work_tree_and_git_dir_separately(self):
        # 回帰: GIT_WORK_TREE / --work-tree が保護した所で GIT_DIR / --git-dir が外だと、最後の値だけを見て通していた
        m = self.shared_dir
        for sub in ("clone ~/dev/m1 ~/dev/out", "init ~/dev/out"):
            for pre in (f"GIT_WORK_TREE={m}/w GIT_DIR=~/dev/x git", f"GIT_DIR={m}/g GIT_WORK_TREE=~/dev/w git",
                        f"git --work-tree={m}/w --git-dir=~/dev/x", f"git --git-dir={m}/g --work-tree ~/dev/w"):
                self.assertIsNotNone(self.bash(f"{pre} {sub}"), f"{pre} {sub}")
            self.assertIsNone(self.bash(f"GIT_WORK_TREE=~/dev/w GIT_DIR=~/dev/x git {sub}"), sub)

    def test_git_clone_init_dash_c_only_moves_cwd(self):
        # 回帰: -C を GIT_DIR などと同じく「明示した場所」と数え、-C <保護> で外へ clone するのを止めていた
        m = self.shared_dir
        self.assertIsNone(self.bash(f"git -C {m} clone -b master ~/dev/src ~/dev/out"))
        self.assertIsNone(self.bash(f"git -C {m} init ~/dev/out"))
        self.assertIsNotNone(self.bash(f"git -C {m} clone -b master ~/dev/src out"))     # 相対は -C の下
        self.assertIsNotNone(self.bash(f"git -C {m} init"))
        self.assertIsNotNone(self.bash(f"git -C ~/dev clone ~/dev/src ../app/shared/files/x"))

    def test_git_worktree_prune_lock_unlock_in_protected_cwd(self):
        m = self.shared_dir
        for c in ("prune", "prune -n", "lock ~/dev/wt", "lock --reason r ~/dev/wt", "unlock ~/dev/wt"):
            self.assertIsNotNone(self.bash(f"cd {m}/m1 && git worktree {c}"), c)
            self.assertIsNone(self.bash(f"cd ~/dev/m1 && git worktree {c}"), c)
        self.assertIsNone(self.bash(f"cd {m}/m1 && git worktree list --porcelain"))

    def test_git_archive_format_patch_output_forms(self):
        m = self.shared_dir
        for c in (f"archive -o{m}/a.tar HEAD", f"archive --out={m}/a.tar HEAD", f"archive --output {m}/a.tar HEAD",
                  f"format-patch -o{m}/p HEAD~1", f"format-patch --output-dir {m}/p HEAD~1",
                  f"format-patch -o {m}/p HEAD~1"):
            self.assertIsNotNone(self.bash(f"cd ~/dev/m1 && git {c}"), c)
        self.assertIsNone(self.bash("cd ~/dev/m1 && git archive -o~/dev/a.tar HEAD"))
        self.assertIsNone(self.bash(f"cd {m}/m1 && git format-patch -o ~/dev/p HEAD~1"))

    def test_git_init_value_options(self):
        # init -b main <dir> の main を行き先と読むと、保護した <dir> を通してしまう
        m = self.shared_dir
        for opts in ("-b main", "--initial-branch main", "--initial-branch=main", "-qb main", "--object-format sha256"):
            self.assertIsNotNone(self.bash(f"git init {opts} {m}/new"), opts)
            self.assertIsNone(self.bash(f"git init {opts} ~/dev/new"), opts)
        self.assertIsNotNone(self.bash(f"cd {m} && git init -b main"))

    def test_git_worktree_branch_options(self):
        m = self.shared_dir
        for opts in ("-b feat", "-B feat", "--orphan feat", "--lock --reason r", "-f --detach"):
            self.assertIsNotNone(self.bash(f"cd ~/dev/m1 && git worktree add {opts} {m}/wt"), opts)
            self.assertIsNotNone(self.bash(f"cd {m}/m1 && git worktree add {opts} ~/dev/wt"), opts)   # cwd が保護した所
            self.assertIsNone(self.bash(f"cd ~/dev/m1 && git worktree add {opts} ~/dev/wt"), opts)
        self.assertIsNotNone(self.bash(f"cd ~/dev/m1 && git worktree repair {m}/wt"))
        self.assertIsNone(self.bash("cd ~/dev/m1 && git worktree list"))

    def test_subshell_cd_does_not_leak(self):
        self.assertIsNone(self.bash(f"(cd {self.shared_dir}; ls) ; mkdir ~/dev/x"))
        self.assertIsNone(self.bash(f"(cd {self.shared_dir}; ls) && mkdir x"))
        self.assertIsNotNone(self.bash(f"(cd {self.shared_dir}; mkdir x)"))

    def test_low_items(self):
        m = self.shared_dir
        self.assertIsNone(self.bash(f"cd {m} && unzip -l ~/a.zip"))
        self.assertIsNone(self.bash(f"cd {m}/m1 && git push --help"))
        self.assertIsNotNone(self.bash(f"rsync -a ~/dev/ {m}/ --exclude x"))
        self.assertIsNotNone(self.bash(f"cd {m} && ln -s ~/dev/a"))
        self.assertIsNotNone(self.bash(f"curl -o {m}/x.zip http://e/x"))
        self.assertIsNotNone(self.bash(f"wget -O {m}/x.zip http://e/x"))
        self.assertIsNone(self.bash("curl -o ~/dev/x.zip http://e/x"))


class QuotedText(Base):
    """引用符の中とヒアドキュメントの本文は文字列。止める対象の名前が入っていただけで止めない（回帰: README に
    <<'EOF' で使い方を追記したら、本文の `svcctl deploy` で止められた）。bash が実際に動かすもの（bash -c の中身、
    二重引用符の中と引用しない <<EOF の本文の $(...) と `...`）は今までどおり読む。"""

    def test_quoted_heredoc_body_with_backticks_and_apostrophe(self):
        # 実際に起きた形: 区切りを引用したヒアドキュメント、本文の行頭のバッククォートとアポストロフィ
        cmd = "cd ~/dev && cat >> README.md <<'EOF'\n## 使い方\n`svcctl.py deploy v1` で出す。it's\nEOF\ngit status"
        self.assertIsNone(self.bash(cmd))
        for delim in ("'EOF'", '"EOF"', "\\EOF", "E'O'F"):
            self.assertIsNone(self.bash(f"cat > ~/dev/x <<{delim}\n$(svcctl deploy a)\n`svcctl deploy b`\nEOF"), delim)
        self.assertIsNone(self.bash(f"cat <<'EOF'\nrm -rf {self.shared_dir}\nEOF"))

    def test_strings_are_not_commands(self):
        for c in ('echo "svcctl deploy a"', "printf '%s\\n' 'svcctl deploy a; git push'",
                  'git commit -m "fix: svcctl deploy a; svcctl scale 2"', "echo '$(svcctl deploy a)'",
                  "echo 'it''s'  # don't svcctl deploy a", f'echo "rm -rf {self.shared_dir}"',
                  'git commit -m "$(cat <<\'EOF\'\nrun `svcctl deploy a`\n1) step\nEOF\n)"'):
            self.assertIsNone(self.bash(c), c)

    def test_multiline_string_with_heredoc_marker_hides_nothing(self):
        # 二重引用符の中の << はヒアドキュメントでない。後ろの行のコマンドを本文と取り違えて見逃さない
        self.assertIsNotNone(self.bash('git commit -m "a\nsee <<EOF\n"\nsvcctl deploy a'))
        self.assertIsNone(self.bash('git commit -m "a\nsee <<EOF\n"\necho ok'))

    def test_commands_after_heredoc_still_read(self):
        self.assertIsNotNone(self.bash("cat <<'EOF' > ~/dev/x\nhello\nEOF\nsvcctl deploy a"))
        self.assertIsNotNone(self.bash(f"cat <<'A' <<B\na\nA\nb\nB\nrm -rf {self.shared_dir}/m1"))
        self.assertIsNotNone(self.bash(f"cat <<-EOF\n\tx\n\tEOF\nrm -rf {self.shared_dir}/m1"))

    def test_shell_c_is_still_read(self):
        for c in ('bash -c "svcctl deploy a"', "sh -c 'cd ~ && svcctl deploy a'", 'bash -lc "echo x; svcctl deploy a"',
                  f'bash -c "cat <<\'EOF\'\nx\nEOF\nrm -rf {self.shared_dir}/m1"'):
            self.assertIsNotNone(self.bash(c), c)

    def test_substitutions_inside_strings_are_read(self):
        m = self.shared_dir
        for c in ('echo "$(svcctl deploy a)"', 'echo "`svcctl deploy a`"', 'git commit -m "x $(svcctl deploy a) y"',
                  'echo "$(echo "$(svcctl deploy a)")"', "cat <<EOF\n$(svcctl deploy a)\nEOF",
                  "cat <<EOF\n`svcctl deploy a`\nEOF", "cat <<'A' <<B\na\nA\n$(svcctl deploy a)\nB",
                  "x=$(svcctl deploy a)", f'echo "$(cd {m} && git pull)"'):
            self.assertIsNotNone(self.bash(c), c)
        # 置換の中の cd は外に持ち出さない。置換の後の語は元の字に戻る（パスの判定は前と同じ）
        self.assertIsNone(self.bash(f'echo "$(cd {m}; ls)" && mkdir ~/dev/x'))
        self.assertIsNotNone(self.bash(f'echo x > "{m}/$(date +%s)"'))
        # 引用しない本文の置換は、そのコマンドの cwd で読む
        self.assertIsNotNone(self.bash(f"cd {m}/m1 && cat <<EOF\n$(git pull)\nEOF"))

    def test_heredoc_fed_to_a_shell_is_read(self):
        # 本文を stdin から動かすもの（-c の無いシェル、ssh）は、本文をコマンドとして読む（レビューの指摘）
        m = self.shared_dir
        for c in ("bash <<'EOF'\nsvcctl deploy a\nEOF", "bash -s <<'EOF'\nsvcctl deploy a\nEOF",
                  "sh <<EOF\nsvcctl deploy a\nEOF", "ssh host <<'EOF'\nsvcctl deploy a\nEOF",
                  f"cd {m} && bash <<'EOF'\ngit pull\nEOF", "timeout 5 bash <<'EOF'\nsvcctl deploy a\nEOF"):
            self.assertIsNotNone(self.bash(c), c)
        for c in ("cat <<'EOF'\nsvcctl deploy a\nEOF", "bash -c 'echo hi' <<'EOF'\nsvcctl deploy a\nEOF",
                  "python3 - <<'EOF'\nprint('svcctl deploy a')\nEOF"):
            self.assertIsNone(self.bash(c), c)

    def test_arithmetic_shift_is_not_a_heredoc(self):
        # $((1<<2)) の << をヒアドキュメントとみて、後ろの行を読み飛ばさない（レビューの指摘）
        for c in ('echo "$((1<<2))"\nsvcctl deploy a', "echo $((1<<2))\nsvcctl deploy a",
                  "((x = 1 << 2))\nsvcctl deploy a", 'echo "$(( (1+2) << 1 ))"; svcctl deploy a'):
            self.assertIsNotNone(self.bash(c), c)
        self.assertIsNone(self.bash("echo $((1<<2))"))

    def test_scan_keeps_text_outside_strings(self):
        text, subs = gh.scan("a \"b $(c) d\" <<'E' && f\nbody\nE\ng")
        self.assertEqual(subs, [("c", "$(c)"), ("body\n", "")])      # 置換と、ヒアドキュメントの本文
        self.assertNotIn("body", text)
        self.assertIn("&& f", text)
        self.assertTrue(text.endswith("\ng"))


class Commands(Base):
    def test_program_rule(self):
        for c in ("svcctl.py --owner me deploy v1", "python3 ~/x/tools/svcctl.py --owner=me scale 4",
                  "svcctl backup s1", "svcctl config --set 1,2", "svcctl reset"):
            self.assertIsNotNone(self.bash(c), c)
        for c in ("svcctl.py --owner me status", "svcctl.py ping", "svcctl config", "svcctl reset --list",
                  "svcctl deploy --dry-run s1", "svcctl log --grep backup"):
            self.assertIsNone(self.bash(c), c)

    def road_config(self, entry):
        g = self.default_guards()
        g["app-running"]["commands"].append({"program": "^gamectl(\\.py)?$", "value_options": ["--owner"], "block": [entry]})
        self.write_config(g)
        _, problems = gh.load_guard_config(self.env)
        return problems

    def test_but_if_any(self):
        # 読むだけ（--inspect-node）か --dry-run なら通すが、--engine-check が付けば止める
        problems = self.road_config({"sub": "road", "unless_any": ["--dry-run", "--inspect-node"],
                                     "but_if_any": ["--engine-check"]})
        self.assertEqual(problems, [])
        for c in ("gamectl road --dry-run", "gamectl road --inspect-node 5", "gamectl.py --owner me road --dry-run",
                  "gamectl road --inspect-node=5", "gamectl status --engine-check"):
            self.assertIsNone(self.bash(c), c)
        for c in ("gamectl road --dry-run --engine-check", "gamectl road --engine-check --dry-run",
                  "gamectl road --from 1,2 --to 3,4", "gamectl road --inspect-node 5 --engine-check",
                  "gamectl road --dry-run --engine-check=1", "gamectl road --engine-check=1 --inspect-node=5",
                  "gamectl road --engine-check", "gamectl road"):
            self.assertIsNotNone(self.bash(c), c)
        # --engine-checker のような別のフラグは but_if_any に当てない
        self.assertIsNone(self.bash("gamectl road --dry-run --engine-checker"))

    def test_abbrev_flags_block(self):
        # argparse の省略形（allow_abbrev）: 止める側の if_any / but_if_any は前方一致で当てる
        self.road_config({"sub": "road", "unless_any": ["--dry-run", "--inspect-node"],
                          "but_if_any": ["--engine-check"]})
        for c in ("gamectl road --dry-run --engine", "gamectl road --dry-run --eng", "gamectl road --inspect-node 5 --e",
                  "gamectl road --dry-run --eng=1", "gamectl road --e --dry-run",
                  "gamectl road --dry",                     # unless_any は完全一致のまま（省略形では通さない）
                  "gamectl road --inspect 5"):
            self.assertIsNotNone(self.bash(c), c)
        for c in ("gamectl road --dry-run", "gamectl road --dry-run -- --x",   # "--" だけは省略形とみない
                  "gamectl road --dry-run --engine-checker", "gamectl road --dry-run -e"):
            self.assertIsNone(self.bash(c), c)
        # 既存の if_any も省略形で当てる（svcctl config --se 1 は argparse では --set）
        for c in ("svcctl config --se 1", "svcctl config --s=1", "svcctl config --set"):
            self.assertIsNotNone(self.bash(c), c)
        for c in ("svcctl config --get x", "svcctl config --setx 1", "svcctl config --", "svcctl config -s 1"):
            self.assertIsNone(self.bash(c), c)

    def test_abbrev_not_in_allow_commands(self):
        # allow_commands（通す側）の if_any は完全一致のまま: 省略形で例外が広がらない
        g = self.default_guards()
        g["app-running"]["allow_commands"] = [{"program": "^svcctl$", "block": [{"sub": "deploy", "if_any": ["--dry-run"]}]}]
        self.write_config(g)
        self.assertIsNone(self.bash("svcctl deploy --dry-run"))
        self.assertIsNotNone(self.bash("svcctl deploy --d"))
        self.assertIsNotNone(self.bash("svcctl deploy --dry"))
        entry = gh._compile_entry({"program": "^svcctl$", "block": [{"sub": "deploy", "if_any": ["--dry-run"]}]}, "x", [])
        self.assertTrue(gh.match_entry(entry, ["svcctl", "deploy", "--d"]))
        self.assertIsNone(gh.match_entry(entry, ["svcctl", "deploy", "--d"], abbrev=False))

    def test_but_if_any_with_if_any(self):
        # if_any を満たさなければ but_if_any があっても通す（if_any が先）
        self.road_config({"sub": "road", "if_any": ["--apply"], "unless_any": ["--dry-run"],
                          "but_if_any": ["--engine-check"]})
        self.assertIsNone(self.bash("gamectl road --engine-check"))
        self.assertIsNone(self.bash("gamectl road --apply --dry-run"))
        self.assertIsNotNone(self.bash("gamectl road --apply"))
        self.assertIsNotNone(self.bash("gamectl road --apply --dry-run --engine-check"))

    def test_but_if_any_config_problems(self):
        for bad in ({"sub": "road", "unless_any": ["--dry-run"], "but_if_any": [3]},
                    {"sub": "road", "unless_any": ["--dry-run"], "but_if_any": {"x": 1}},
                    {"sub": "road", "unless_any": ["--dry-run"], "but_if_any": [""]}):
            problems = self.road_config(bad)
            self.assertTrue(any("app-running.commands[1].block[0]" in p and "but_if_any" in p for p in problems), bad)
            self.assertIsNone(self.bash("gamectl road --from 1"), bad)     # 型の誤りは今と同じく項目ごと読まない
        problems = self.road_config({"sub": "road", "but_if_any": ["--engine-check"]})
        self.assertTrue(any("but_if_any は unless_any と一緒に" in p for p in problems), problems)
        # 項目は止める側で残す
        self.assertIsNotNone(self.bash("gamectl road --dry-run"))
        self.assertIsNotNone(self.bash("gamectl road --engine-check"))
        problems = self.road_config({"sub": "road", "unless_any": [], "but_if_any": ["--engine-check"]})
        self.assertTrue(any("but_if_any は unless_any と一緒に" in p for p in problems), problems)
        # 既存の if_any / unless_any の誤りも今と同じに出る
        problems = self.road_config({"sub": "road", "unless_any": [1]})
        self.assertTrue(any("block[0] の if_any / unless_any" in p for p in problems), problems)

    def test_existing_entries_unchanged(self):
        # but_if_any の無い項目は今までと同じ（unless_any があれば通す・if_any が無ければ通す）
        self.assertIsNone(self.bash("svcctl reset --list --dry-run"))
        self.assertIsNone(self.bash("svcctl reset --dry-run=1"))
        self.assertIsNotNone(self.bash("svcctl reset --force"))
        self.assertIsNone(self.bash("svcctl config --get x"))
        self.assertIsNotNone(self.bash("svcctl config --set=1"))
        self.assertIsNotNone(self.bash("svcctl deploy v1 --force"))         # 文字列の項目は無条件
        _, problems = gh.load_guard_config(self.env)
        self.assertEqual(problems, [])

    def test_env_prefix_allow(self):
        self.assertIsNone(self.bash("CLAUDE_GUARD_ALLOW=app-running svcctl.py deploy s1"))
        self.assertIsNone(self.bash("CLAUDE_GUARD_ALLOW=all svcctl.py deploy s1"))
        self.assertIsNone(self.bash("CLAUDE_GUARD_ALLOW=1 svcctl.py deploy s1"))
        self.assertIsNone(self.bash("export CLAUDE_GUARD_ALLOW=app-running; svcctl deploy a && svcctl restart"))
        self.assertIsNotNone(self.bash("CLAUDE_GUARD_ALLOW=deploy svcctl.py deploy s1"))    # 別のガードの名前
        self.assertIsNotNone(self.bash("CLAUDE_GUARD_ALLOW=app-running svcctl deploy a && svcctl scale 2"))

    def test_hook_env_allow(self):
        env = dict(self.env, CLAUDE_GUARD_ALLOW="app-running,deploy")
        self.assertIsNone(self.bash("svcctl deploy a", env=env))

    def test_allow_file(self):
        now = time.time()
        gh.write_state(self.env, "app-running", "allow", 30, "e2e", now=now)
        self.assertIsNone(self.bash("svcctl deploy a"))
        gh.write_state(self.env, "app-running", "allow", 1, "e2e", now=now - 120)   # 期限切れ
        self.assertIsNotNone(self.bash("svcctl deploy a"))
        gh.write_state(self.env, "all", "allow", 5, "e2e")
        self.assertIsNone(self.bash("svcctl deploy a"))
        gh.remove_state(self.env, "all", "allow")
        self.assertIsNotNone(self.bash("svcctl deploy a"))

    def test_allow_needs_reason_and_bounds(self):
        with self.assertRaises(ValueError):
            gh.write_state(self.env, "x", "allow", 30, "")
        with self.assertRaises(ValueError):
            gh.write_state(self.env, "x", "allow", 0, "r")
        with self.assertRaises(ValueError):
            gh.write_state(self.env, "../x", "allow", 5, "r")


class Conditions(Base):
    def test_flag_on_off_and_expiry(self):
        probe = fake_probe()               # process も command も働いていない
        self.assertIsNone(self.bash("git push origin main", probe=probe))
        gh.write_state(self.env, "deploy", "on", 10, "v1.2 を出している")
        r = self.bash("cd ~/dev && git push origin main", probe=probe)
        self.assertIsNotNone(r)
        self.assertIn("v1.2 を出している", r)        # {reason} が入る
        self.assertIsNone(self.bash("git push --dry-run origin main", probe=probe))  # allow_commands
        self.assertIsNotNone(self.write(str(self.real_home / "svc" / "deploy" / "a.yml"), probe=probe))
        gh.remove_state(self.env, "deploy", "on")
        self.assertIsNone(self.bash("git push origin main", probe=probe))
        gh.write_state(self.env, "deploy", "on", 1, "old", now=time.time() - 120)
        self.assertIsNone(self.bash("git push origin main", probe=probe))

    def test_command_condition(self):
        p = str(self.real_home / "lab" / "config" / "a.ini")
        self.assertIsNone(self.write(p, probe=fake_probe(command=False)))
        self.assertIsNotNone(self.write(p, probe=fake_probe(command=True)))

    def test_command_condition_real(self):
        self.assertFalse(gh.command_true(["false"], 2))
        self.assertTrue(gh.command_true(["true"], 2))
        self.assertIsNone(gh.command_true(["sleep", "5"], 0.2))
        self.assertIsNone(gh.command_true(["/nonexistent-cmd"], 1))

    def test_or_of_conditions(self):
        g = self.default_guards()
        g["device-test"]["process"] = "^never"
        self.write_config(g)
        p = str(self.real_home / "lab" / "config" / "a.ini")
        self.assertIsNotNone(self.write(p, probe=fake_probe(process=True, command=False)))
        self.assertIsNotNone(self.write(p, probe=fake_probe(process=False, command=True)))
        self.assertIsNone(self.write(p, probe=fake_probe(process=False, command=False)))
        gh.write_state(self.env, "device-test", "on", 5, "")
        self.assertIsNotNone(self.write(p, probe=fake_probe(process=False, command=False)))

    def test_guards_are_independent(self):
        gh.write_state(self.env, "deploy", "on", 10, "r")
        probe = fake_probe()                 # app-running は働いていない
        self.assertIsNone(self.bash("svcctl deploy a", probe=probe))
        self.assertIsNotNone(self.bash("git push", probe=probe))
        r = self.bash("svcctl deploy a && git push", probe=fake_probe(process=True))
        self.assertIn("app-running", r)
        self.assertIn("deploy", r)

    def test_disabled_guard(self):
        g = self.default_guards()
        g["app-running"]["enabled"] = False
        self.write_config(g)
        self.assertIsNone(self.bash("svcctl deploy a"))

    def test_real_pgrep(self):
        self.assertFalse(gh.process_running(["^no-such-process-for-claude-guard$"]))
        self.assertTrue(gh.process_running([f"^{sys.executable}", "^python"]) in (True, False))


class Lock(Base):
    def test_acquire_conflict_only_one(self):
        results = []
        barrier = threading.Barrier(8)

        def worker(i):
            barrier.wait()
            results.append(gh.acquire(self.env, "deploy", 10, "r", f"s{i}")[0])
        ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(results.count(True), 1)

    def test_acquire_conflict_processes(self):
        code = ("import sys; sys.path.insert(0, %r); import guardhook as gh, os;"
                "print(gh.acquire(dict(os.environ), 'deploy', 10, 'r', sys.argv[1])[0])") % str(ROOT / "lib")
        env = dict(os.environ, **self.env)
        ps = [subprocess.Popen([sys.executable, "-c", code, f"p{i}"], env=env, stdout=subprocess.PIPE, text=True)
              for i in range(6)]
        outs = [p.communicate()[0].strip() for p in ps]
        self.assertEqual(outs.count("True"), 1, outs)

    def test_second_acquire_fails_shows_owner(self):
        self.assertTrue(gh.acquire(self.env, "deploy", 10, "v1", "sA")[0])
        ok, held = gh.acquire(self.env, "deploy", 10, "v2", "sB")
        self.assertFalse(ok)
        self.assertEqual(held["owner"], "sA")

    def test_expired_can_be_retaken(self):
        gh.acquire(self.env, "deploy", 1, "old", "sA", now=time.time() - 120)
        ok, held = gh.acquire(self.env, "deploy", 10, "new", "sB")
        self.assertTrue(ok)
        self.assertEqual(held["owner"], "sB")

    def test_owner_passes_others_blocked(self):
        gh.acquire(self.env, "deploy", 10, "v1", "sA")
        probe = fake_probe()
        self.assertIsNone(self.bash("git push origin main", probe=probe, session="sA"))
        r = self.bash("git push origin main", probe=probe, session="sB")
        self.assertIsNotNone(r)
        self.assertIn("sA", r)

    def test_release_only_owner(self):
        gh.acquire(self.env, "deploy", 10, "v1", "sA")
        self.assertEqual(gh.release(self.env, "deploy", "sB")[0], "not-owner")
        self.assertIsNotNone(gh.read_state(self.env, "deploy", "on"))
        self.assertEqual(gh.release(self.env, "deploy", "sA")[0], "released")
        self.assertIsNone(gh.read_state(self.env, "deploy", "on"))
        gh.acquire(self.env, "deploy", 10, "v1", "sA")
        self.assertEqual(gh.release(self.env, "deploy", "sB", force=True)[0], "released")
        self.assertEqual(gh.release(self.env, "deploy", "sB")[0], "none")

    def test_on_off_respect_lock(self):
        gh.acquire(self.env, "deploy", 10, "v1", "sA")
        self.assertFalse(gh.set_flag(self.env, "deploy", 10, "x")[0])
        self.assertEqual(gh.read_state(self.env, "deploy", "on")["owner"], "sA")
        self.assertEqual(gh.clear_flag(self.env, "deploy")[0], "locked")
        self.assertEqual(gh.clear_flag(self.env, "deploy", force=True)[0], "removed")
        self.assertTrue(gh.set_flag(self.env, "deploy", 10, "x")[0])
        self.assertEqual(gh.clear_flag(self.env, "deploy")[0], "removed")
        self.assertEqual(gh.clear_flag(self.env, "deploy")[0], "none")

    def test_on_flag_blocks_everyone_and_blocks_acquire(self):
        gh.write_state(self.env, "deploy", "on", 10, "maintenance")
        self.assertIsNotNone(self.bash("git push", probe=fake_probe(), session="sA"))
        self.assertFalse(gh.acquire(self.env, "deploy", 10, "", "sA")[0])


class VarCommand(Base):
    """同じコマンドの中で代入した変数をコマンドの位置で使った形（T=...; $T quit）。値を語に分けて読む。"""

    def test_assignment_forms_at_command_position(self):
        for c in ('T="python3 /x/tools/svcctl.py --owner a"; $T deploy s1',
                  "T='python3 /x/tools/svcctl.py'; $T deploy s1",
                  "T=svcctl; $T deploy s1",
                  "T=svcctl && ${T} deploy s1",
                  'export T="python3 /x/svcctl.py --owner a"; $T deploy s1',
                  'export T="python3 /x/svcctl.py" && $T --owner=a deploy s1',
                  'T="python3 /x/svcctl.py"\n$T deploy s1',
                  'T="python3 /x/svcctl.py"; timeout 5 $T deploy s1',
                  'T="python3 /x/svcctl.py"; X=1 nohup $T deploy s1 &',
                  'T="python3 \'/x y/svcctl.py\'"; $T deploy s1',          # 値の中の引用
                  'T=/x/svcctl.py; python3 $T --owner a deploy s1',        # 前からあった引数の展開
                  'T="python3 /x/svcctl.py"; if true; then $T deploy s1; fi',
                  'T="python3 /x/svcctl.py"; bash -c "$T deploy s1"'):
            self.assertIsNotNone(self.bash(c), c)

    def test_wrapper_arguments_from_variables(self):
        # 前に付けたものの引数（timeout の時間など）に代入した変数を使っても、後ろのコマンドを見失わない（レビューの指摘）
        for c in ("D=5; timeout $D svcctl deploy s1", "D=5; timeout -s KILL $D svcctl deploy s1",
                  "D=1h; timeout ${D} svcctl deploy s1", "T=/x/svcctl.py; D=5; timeout $D python3 $T deploy s1",
                  "F=/tmp/l; flock $F svcctl deploy s1", "N=5; nice -n $N svcctl deploy s1",
                  "U=me; sudo -u $U svcctl deploy s1", "V=A=1; env $V svcctl deploy s1"):
            self.assertIsNotNone(self.bash(c), c)

    def test_several_assignments(self):
        self.assertIsNotNone(self.bash("A=python3; B=/x/svcctl.py; $A $B deploy s1"))
        self.assertIsNotNone(self.bash("P=python3 B=/x/svcctl.py; $P $B --owner a deploy s1"))
        self.assertIsNotNone(self.bash("T=ls; T=svcctl; $T deploy s1"))          # 後の代入が効く
        self.assertIsNone(self.bash("T=svcctl; T=ls; $T deploy s1"))

    def test_not_expanded(self):
        for c in ("$T deploy s1",                                  # 外の変数（このコマンドで代入していない）
                  "T=$(echo svcctl); $T deploy s1",                # コマンド置換
                  'f() { svcctl "$@"; }; f deploy s1',              # 関数の呼び出しは見えない
                  "(T=svcctl); $T deploy s1",                       # サブシェルの代入は外に出ない
                  "T=svcctl; echo $T deploy s1",                    # 引数の位置は値 1 語のまま
                  'T="svcctl deploy"; printf %s "$T"',
                  "T=svcctl ls; $T deploy s1"):                    # T=… cmd は代入でなく cmd の環境
            self.assertIsNone(self.bash(c), c)
        # hook の環境の変数は、前からどおり値 1 語として置き換えるだけ（語に分けない）
        self.assertIsNotNone(self.bash("$T deploy s1", env=dict(self.env, T="svcctl")))
        self.assertIsNone(self.bash("$T deploy s1", env=dict(self.env, T="python3 /x/svcctl.py --owner a")))

    def test_quoted_value_with_spaces_at_command_position(self):
        # "$S" は bash では分けずに 1 語で動く。引用の有無は読めないので、分けた形と分けない形の両方で読む（退行の修正）
        tf3 = self.default_guards()
        for c in ('S="/opt/my tools/svcctl"; "$S" deploy s1',
                  "S='/opt/my tools/svcctl'; \"${S}\" deploy s1",
                  'S="/opt/my tools/svcctl"; timeout 5 "$S" deploy s1',
                  'S="/opt/my tools/svcctl.py"; python3 "$S" deploy s1',
                  'S=/opt/svcctl; $S deploy s1',
                  'S=/opt/svcctl; "$S" deploy s1',
                  'D=5; timeout $D svcctl deploy s1',
                  'S="/opt/my tools/bash"; "$S" -c "svcctl deploy s1"',
                  'S="/opt/my tools/bash"; "$S" <<\'EOF\'\nsvcctl deploy s1\nEOF'):   # 本文を動かす（レビューの指摘）
            self.assertIsNotNone(self.bash(c), c)
        tf3["app-running"]["commands"].append({"program": "^tf3ctl(\\.py)?$", "value_options": ["--owner"],
                                               "block": ["quit"]})
        self.write_config(tf3)
        for c in ('T="python3 /p/tf3ctl.py --owner x"; $T quit',
                  'T="python3 /p/tf3ctl.py --owner x"; timeout 5 $T quit',
                  'T="python3 /p/tf3ctl.py --owner x"; env $T quit',
                  'T="/p/tf3ctl.py --owner x"; python3 $T quit',
                  'T="python3 /p/tf3ctl.py --owner x"; if true; then $T quit; fi'):
            self.assertIsNotNone(self.bash(c), c)
        # 分けない形で読んでも、cd などの状態は分けた形のものだけが残る / 通すものは通す
        self.assertIsNone(self.bash('S="/opt/my tools/svcctl"; "$S" status'))
        self.assertIsNone(self.bash('T="python3 /p/tf3ctl.py --owner x"; $T status'))

    def test_quoted_argument_with_spaces_unchanged(self):
        # 引数の位置の "$Q" は今までどおり値 1 語（空白入りのパス）
        d = self.shared_dir / "a b"
        d.mkdir()
        self.assertIsNotNone(self.bash(f'Q="{d}"; rm "$Q"'))
        self.assertIsNone(self.bash(f'Q="{self.real_home}/dev/a b"; rm "$Q"'))
        self.assertIsNone(self.bash(f'Q="{self.real_home}/dev {self.shared_dir}"; rm "$Q"'))

    def test_empty_value_and_paths_still_expand(self):
        self.assertIsNone(self.bash('T=""; $T ls'))
        self.assertIsNotNone(self.bash(f"D={self.shared_dir}/m1; rm -rf $D"))
        self.assertIsNotNone(self.bash(f'R="rm -rf"; $R {self.shared_dir}/m1'))


class AllowOwner(Base):
    """起動した担当だけ通す例外（commands の {"program"} の項目の allow_owner）。"""

    def setUp(self):
        super().setUp()
        self.rec_dir = self.run_dir / "svcctl"
        self.rec_dir.mkdir()
        self.rec = self.rec_dir / "launched.json"
        self.write_owner_config()
        self.argv = {4242: ["./fullscreen-app", "--x"]}
        p1 = mock.patch("proc.argv", side_effect=lambda pid: self.argv.get(pid))
        p2 = mock.patch("proc.alive", side_effect=lambda pid: pid in self.argv)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def write_owner_config(self, **extra):
        g = self.default_guards()
        ao = {"file": "$XDG_RUNTIME_DIR/svcctl/launched.json", "option": "--owner", "env": "SVCCTL_OWNER"}
        ao.update(extra)
        g["app-running"]["commands"] = [
            {"program": "^svcctl(\\.py)?$", "block": ["deploy", "quit"], "allow_owner": ao}]
        self.write_config(g)

    def launched(self, owner="probe", pid=4242, **kw):
        self.rec.write_text(json.dumps(dict({"v": 1, "owner": owner, "pid": pid, "started_at": 1, "cmd": "launch"}, **kw)))

    def role(self, session, role):
        d = self.run_dir / "claude-state"
        d.mkdir(exist_ok=True)
        (d / f"{session}.json").write_text(json.dumps({"session_id": session, "role": role}))

    def test_owner_passes(self):
        self.launched()
        self.role("s-probe", "probe")
        for c in ("svcctl --owner probe quit", "python3 /x/svcctl.py --owner=probe quit",
                  "SVCCTL_OWNER=probe svcctl quit", "env SVCCTL_OWNER=probe svcctl.py quit",
                  "export SVCCTL_OWNER=probe; svcctl quit",
                  'T="python3 /x/svcctl.py --owner probe"; $T quit'):
            self.assertIsNone(self.bash(c, session="s-probe"), c)
        env = dict(self.env, SVCCTL_OWNER="probe")                    # セッションの環境にある担当の名前
        self.assertIsNone(self.bash("svcctl quit", session="s-probe", env=env))

    def test_owner_mismatch_or_missing(self):
        self.launched()
        self.role("s-probe", "probe")
        for c in ("svcctl --owner other quit", "svcctl quit", "svcctl --owner= quit",
                  "SVCCTL_OWNER=probe svcctl --owner other quit",           # --owner が環境変数より先
                  "svcctl quit --owner probe",                              # サブコマンドの後は全体のオプションでない
                  "SVCCTL_OWNER=probe; svcctl quit",                        # export していない代入は子に渡らない
                  "export SVCCTL_OWNER=probe; env -u SVCCTL_OWNER svcctl quit",   # 消した名乗り
                  "export SVCCTL_OWNER=probe; env --unset=SVCCTL_OWNER svcctl quit",
                  "export SVCCTL_OWNER=probe; unset SVCCTL_OWNER; svcctl quit"):
            self.assertIsNotNone(self.bash(c, session="s-probe"), c)
        # 同じコマンドの中で、例外に当たるものと当たらないものが並んでいれば止める
        self.assertIsNotNone(self.bash("svcctl --owner probe quit && svcctl --owner x deploy", session="s-probe"))

    def test_flag_or_lock_disables_owner(self):
        # 旗・ロックは「全員を止める」。process も動いていて owner が合っていても、起動した担当を止める
        self.launched()
        self.role("s-probe", "probe")
        c = "svcctl --owner probe quit"
        self.assertIsNone(self.bash(c, session="s-probe"))                 # 旗なし + process 動作中 → 通す
        gh.write_state(self.env, "app-running", "on", 10, "maintenance")
        self.assertIsNotNone(self.bash(c, session="s-probe"))
        self.assertIsNotNone(self.bash('T="python3 /x/svcctl.py --owner probe"; $T quit', session="s-probe"))
        line = next(l for l in gh.status_lines(self.env, fake_probe(process=True))[0] if l.startswith("app-running:"))
        self.assertIn("旗（ロック）の間は使わない", line)
        gh.remove_state(self.env, "app-running", "on")
        self.assertTrue(gh.acquire(self.env, "app-running", 10, "deploy", "s-deployer")[0])   # 別のセッションのロック
        self.assertIsNotNone(self.bash(c, session="s-probe"))
        gh.release(self.env, "app-running", "s-deployer")
        self.assertIsNone(self.bash(c, session="s-probe"))

    def test_pid_dead_or_other_process(self):
        self.role("s-probe", "probe")
        self.launched(pid=999)                                              # 動いていない
        self.assertIsNotNone(self.bash("svcctl --owner probe quit", session="s-probe"))
        self.argv[999] = ["/usr/bin/sleep", "100"]                         # pid の使い回し（別のプロセス）
        self.assertIsNotNone(self.bash("svcctl --owner probe quit", session="s-probe"))

    def test_file_missing_or_broken(self):
        self.role("s-probe", "probe")
        self.assertIsNotNone(self.bash("svcctl --owner probe quit", session="s-probe"))
        for text in ("{", "[]", "null", json.dumps({"owner": "probe", "pid": 4242}),          # v が無い
                     json.dumps({"v": 2, "owner": "probe", "pid": 4242}),
                     json.dumps({"v": 1, "owner": "", "pid": 4242}),
                     json.dumps({"v": 1, "owner": "probe", "pid": "4242"}),
                     json.dumps({"v": 1, "owner": "probe", "pid": True}),
                     json.dumps({"v": 1, "owner": "probe", "pid": -1})):
            self.rec.write_text(text)
            self.assertIsNotNone(self.bash("svcctl --owner probe quit", session="s-probe"), text)
        self.rec.unlink()
        self.rec.mkdir()                                                    # ファイルでない
        self.assertIsNotNone(self.bash("svcctl --owner probe quit", session="s-probe"))

    def test_symlinked_record_is_not_trusted(self):
        self.role("s-probe", "probe")
        real = self.run_dir / "elsewhere.json"
        real.write_text(json.dumps({"v": 1, "owner": "probe", "pid": 4242}))
        self.rec.symlink_to(real)
        self.assertIsNotNone(self.bash("svcctl --owner probe quit", session="s-probe"))

    def test_role_mismatch_or_unknown(self):
        self.launched()
        self.role("s-model", "model")
        self.assertIsNotNone(self.bash("svcctl --owner probe quit", session="s-model"))   # 役割が違う
        self.assertIsNotNone(self.bash("svcctl --owner probe quit", session="s-none"))    # 記録が無い
        self.role("s-blank", None)
        self.assertIsNotNone(self.bash("svcctl --owner probe quit", session="s-blank"))   # ペインに role が無い
        self.assertIsNotNone(gh.evaluate({"tool_name": "Bash", "tool_input": {"command": "svcctl --owner probe quit"},
                                          "cwd": str(self.real_home)}, self.env, fake_probe(process=True)))
        self.write_owner_config(same_role=False)                            # 役割を見ない設定
        self.assertIsNone(self.bash("svcctl --owner probe quit", session="s-none"))
        self.assertIsNotNone(self.bash("svcctl --owner model quit", session="s-model"))

    def test_only_that_entry_and_paths_still_blocked(self):
        self.launched()
        self.role("s-probe", "probe")
        g = json.loads(self.cfg.read_text())["guard_hook"]["guards"]
        g["app-running"]["commands"].append(                               # 例外の無い別の項目
            {"program": "^svcctl$", "value_options": ["--owner"], "block": ["deploy"]})
        self.write_config(g)
        self.assertIsNone(self.bash("svcctl --owner probe quit", session="s-probe"))
        self.assertIsNotNone(self.bash("svcctl --owner probe deploy", session="s-probe"))
        # 例外は項目の block 全部に効く（setUp の項目は deploy と quit）。絞るなら項目を分ける（この形）
        self.write_owner_config()
        self.assertIsNone(self.bash("svcctl --owner probe deploy", session="s-probe"))
        self.assertIsNotNone(self.bash(f"svcctl --owner probe quit > {self.shared_dir}/x", session="s-probe"))

    def test_without_xdg_runtime_dir(self):
        env = {k: v for k, v in self.env.items() if k != "XDG_RUNTIME_DIR"}
        self.assertEqual(gh.launch_file("$XDG_RUNTIME_DIR/svcctl/launched.json", env),
                         f"/tmp/svcctl-{os.getuid()}/launched.json")
        self.assertEqual(gh.launch_file("${XDG_RUNTIME_DIR}/svcctl/a/b.json", self.env),
                         str(self.run_dir / "svcctl" / "a" / "b.json"))
        self.assertEqual(gh.launch_file("~/r/launched.json", self.env), f"{self.real_home}/r/launched.json")

    def test_value_option_added_for_subcommand(self):
        # value_options に --owner を書かなくても、--owner の値をサブコマンドと取り違えない
        self.assertIsNotNone(self.bash("svcctl --owner x quit"))
        self.assertIsNone(self.bash("svcctl --owner quit status"))

    def test_config_problems(self):
        bad = [{"file": 3}, {"file": "rel/launched.json", "option": "--owner"},
               {"file": "$HOME/x.json", "option": "--owner"}, {"file": "$XDG_RUNTIME_DIR/launched.json", "option": "--owner"},
               {"file": "/r/x.json"}, {"file": "/r/x.json", "option": "owner"}, {"file": "/r/x.json", "env": "A-B"},
               {"file": "/r/x.json", "option": "--owner", "same_role": "yes"}, "x"]
        for ao in bad:
            g = self.default_guards()
            g["app-running"]["commands"] = [{"program": "^svcctl$", "value_options": ["--owner"], "block": ["quit"],
                                             "allow_owner": ao}]
            self.write_config(g)
            _, problems = gh.load_guard_config(self.env)
            self.assertTrue(any("allow_owner" in p for p in problems), ao)
            self.assertIsNotNone(self.bash("svcctl --owner probe quit", session="s-probe"), ao)
        g = self.default_guards()
        ao = {"file": "/r/x.json", "option": "--owner"}
        g["app-running"]["commands"] = [{"regex": "^svcctl quit", "allow_owner": ao}]
        g["app-running"]["allow_commands"] = [{"program": "^svcctl$", "block": ["x"], "allow_owner": ao}]
        g["deploy"]["commands"] = [{"program": "^svcctl$", "block": ["quit"], "allow_owner": ao}]    # process が無い
        self.write_config(g)
        _, problems = gh.load_guard_config(self.env)
        text = "\n".join(problems)
        self.assertIn("app-running.commands[0].allow_owner は commands の", text)
        self.assertIn("app-running.allow_commands[0].allow_owner", text)
        self.assertIn("deploy: allow_owner は process", text)

    def test_status_shows_record(self):
        lines, bad = gh.status_lines(self.env, fake_probe(process=True))
        self.assertFalse(bad)
        line = next(l for l in lines if l.startswith("app-running:"))
        self.assertIn("記録のファイルが無い → 例外にならない", line)
        self.launched()
        line = next(l for l in gh.status_lines(self.env, fake_probe(process=True))[0] if l.startswith("app-running:"))
        self.assertIn("owner probe、pid 4242 が動いている", line)
        self.launched(pid=999)
        line = next(l for l in gh.status_lines(self.env, fake_probe(process=True))[0] if l.startswith("app-running:"))
        self.assertIn("owner probe、pid 999 が動いていない", line)
        self.rec.write_text("{")
        line = next(l for l in gh.status_lines(self.env, fake_probe(process=True))[0] if l.startswith("app-running:"))
        self.assertIn("形が違う", line)

    def test_real_process(self):
        mock.patch.stopall()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "claude-guard-owner-test"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        g = self.default_guards()
        g["app-running"]["process"] = "claude-guard-owner-test"
        g["app-running"]["commands"] = [{"program": "^svcctl$", "block": ["quit"],
                                         "allow_owner": {"file": "$XDG_RUNTIME_DIR/svcctl/launched.json",
                                                         "option": "--owner"}}]
        self.write_config(g)
        self.launched(pid=child.pid)
        self.role("s-probe", "probe")
        self.assertIsNone(self.bash("svcctl --owner probe quit", session="s-probe"))
        self.launched(pid=os.getpid())                                       # 生きているが別のプロセス
        self.assertIsNotNone(self.bash("svcctl --owner probe quit", session="s-probe"))
        # 本物の hook（proc・pgrep を差し替えない）で、process で働いているときは例外が効き、速い
        self.launched(pid=child.pid)
        e = dict(os.environ, **self.env)
        e.pop("CLAUDE_GUARD_ALLOW", None)

        def run(c, sid):
            return subprocess.run([sys.executable, str(BIN), "hook"], text=True, capture_output=True, env=e,
                                  input=json.dumps(self.hook_input("Bash", {"command": c}, session=sid)))
        t0 = time.monotonic()
        r = run("svcctl --owner probe quit", "s-probe")
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        r = run("svcctl --owner probe quit", "s-other")
        self.assertIn("deny", r.stdout)
        # 旗が立てば「全員を止める」。起動した担当も止める
        gh.write_state(self.env, "app-running", "on", 5, "")
        r = run("svcctl --owner probe quit", "s-probe")
        self.assertIn("deny", r.stdout)


class Coop(Base):
    """共同作業（起動の記録の coop）: 期限内で pid が生きていれば、with の担当も owner と同じ照合で通す。"""
    NOW = 1_800_000_000          # 2027-01-15 17:00 JST

    def setUp(self):
        Base.setUp(self)
        self.rec_dir = self.run_dir / "svcctl"
        self.rec_dir.mkdir()
        self.rec = self.rec_dir / "launched.json"
        self.write_owner_config()
        self.argv = {4242: ["./fullscreen-app", "--x"]}
        for name, fn in (("argv", lambda pid: self.argv.get(pid)), ("alive", lambda pid: pid in self.argv)):
            pt = mock.patch(f"proc.{name}", side_effect=fn)
            pt.start()
            self.addCleanup(pt.stop)

    write_owner_config = AllowOwner.write_owner_config
    role = AllowOwner.role

    def launched(self, owner="aom-master", pid=4242, coop=None, **kw):
        rec = {"v": 1, "pid": pid, "started_at": 1, "cmd": "launch"}
        if owner is not None:
            rec["owner"] = owner
        if coop is not None:
            rec["coop"] = coop
        rec.update(kw)
        self.rec.write_text(json.dumps(rec))

    def coop(self, who="probe", **kw):
        return dict({"with": who, "reason": "一緒に列車を走らせる", "since": self.NOW - 60, "until": self.NOW + 3600}, **kw)

    def bash(self, command, session="s-probe", env=None):
        data = self.hook_input("Bash", {"command": command}, None, session)
        return gh.evaluate(data, env or self.env, fake_probe(process=True), now=self.NOW)

    def status(self):
        return next(l for l in gh.status_lines(self.env, fake_probe(process=True), now=self.NOW)[0]
                    if l.startswith("app-running:"))

    def test_coop_member_passes(self):
        self.launched(coop=self.coop())
        self.role("s-probe", "probe")
        for c in ("svcctl --owner probe quit", "SVCCTL_OWNER=probe svcctl deploy"):
            self.assertIsNone(self.bash(c), c)

    def test_without_coop_unchanged(self):
        self.launched()                                                     # owner は別の担当、印なし
        self.role("s-probe", "probe")
        self.assertIsNotNone(self.bash("svcctl --owner probe quit"))

    def test_other_member_or_role_blocked(self):
        self.launched(coop=self.coop())
        self.role("s-probe", "probe")
        self.role("s-ui", "ui")
        self.assertIsNotNone(self.bash("svcctl --owner ui quit", session="s-ui"))       # with に無い担当
        self.assertIsNotNone(self.bash("svcctl --owner probe quit", session="s-ui"))    # 名乗りと役割が違う
        self.assertIsNotNone(self.bash("svcctl quit"))                                  # 名乗らない

    def test_expired_or_broken_coop_blocked(self):
        self.role("s-probe", "probe")
        for c in (self.coop(until=self.NOW), self.coop(until=self.NOW - 1), self.coop(until="later"),
                  self.coop(until=True), self.coop(who=""), self.coop(who=[]), self.coop(who=["probe", 3]),
                  self.coop(until=self.NOW + 8 * 24 * 3600), self.coop(until=1e20),
                  {"with": "probe"}, "probe", ["probe"]):
            self.launched(coop=c)
            self.assertIsNotNone(self.bash("svcctl --owner probe quit"), c)

    def test_nan_or_infinite_until(self):
        # json は NaN・Infinity を読む。印とみなさず（hook で期限切れにならないのを防ぐ）、status も落ちない
        self.role("s-probe", "probe")
        for v in ("NaN", "Infinity", "-Infinity"):
            self.rec.write_text('{"v": 1, "owner": "m", "pid": 4242, "coop": {"with": "probe", "until": %s, "since": NaN}}' % v)
            self.assertIsNotNone(self.bash("svcctl --owner probe quit"), v)
            self.assertNotIn("共同作業", self.status(), v)
        self.rec.write_text('{"v": 1, "owner": "m", "pid": 4242, "coop": {"with": "probe", "until": %d, "since": NaN}}'
                            % (self.NOW + 60))
        self.assertIn("共同作業: probe", self.status())                     # since が NaN でも表示だけの欄

    def test_pid_dead_or_other_process_blocked(self):
        self.role("s-probe", "probe")
        self.launched(pid=999, coop=self.coop())
        self.assertIsNotNone(self.bash("svcctl --owner probe quit"))
        self.argv[999] = ["/usr/bin/sleep", "100"]
        self.assertIsNotNone(self.bash("svcctl --owner probe quit"))

    def test_owner_less_record_with_coop(self):
        # 人が --owner なしで起動して、共同作業の印だけを付けた形
        self.role("s-probe", "probe")
        self.launched(owner=None, coop=self.coop())
        self.assertIsNone(self.bash("svcctl --owner probe quit"))
        self.rec.write_text(json.dumps({"v": 1, "owner": None, "pid": 4242, "coop": self.coop()}))   # owner: null も同じ
        self.assertIsNone(self.bash("svcctl --owner probe quit"))
        self.launched(owner=None)                                           # owner も coop も無い → 形が違う
        self.assertIsNotNone(self.bash("svcctl --owner probe quit"))
        self.assertIn("形が違う", self.status())
        self.launched(owner=None, coop=self.coop(until=self.NOW - 1))       # 期限切れ: 誰も通さない
        self.assertIsNotNone(self.bash("svcctl --owner probe quit"))

    def test_with_list(self):
        self.launched(owner=None, coop=self.coop(who=["ui", "probe"]))
        self.role("s-probe", "probe")
        self.role("s-ui", "ui")
        self.role("s-text", "text")
        self.assertIsNone(self.bash("svcctl --owner probe quit"))
        self.assertIsNone(self.bash("svcctl --owner ui quit", session="s-ui"))
        self.assertIsNotNone(self.bash("svcctl --owner text quit", session="s-text"))

    def test_owner_still_passes_with_coop(self):
        self.launched(owner="master", coop=self.coop())
        self.role("s-master", "master")
        self.assertIsNone(self.bash("svcctl --owner master quit", session="s-master"))

    def test_paths_and_git_still_blocked(self):
        self.launched(coop=self.coop())
        self.role("s-probe", "probe")
        for c in (f"echo x > {self.shared_dir}/a", f"cp /tmp/x {self.shared_dir}/m1/",
                  f"cd {self.shared_dir}/m1 && git pull", f"git -C {self.shared_dir}/m1 checkout main",
                  f"svcctl --owner probe quit > {self.shared_dir}/log"):
            self.assertIsNotNone(self.bash(c), c)
        data = self.hook_input("Write", {"file_path": f"{self.shared_dir}/m1/a.lua"}, None, "s-probe")
        self.assertIsNotNone(gh.evaluate(data, self.env, fake_probe(process=True), now=self.NOW))

    def test_flag_or_lock_disables_coop(self):
        self.launched(coop=self.coop())
        self.role("s-probe", "probe")
        gh.write_state(self.env, "app-running", "on", 10, "maintenance", now=self.NOW)
        self.assertIsNotNone(self.bash("svcctl --owner probe quit"))
        self.assertIn("（旗（ロック）の間は使わない）", self.status())
        gh.remove_state(self.env, "app-running", "on")
        self.assertTrue(gh.acquire(self.env, "app-running", 10, "deploy", "s-deployer", now=self.NOW)[0])
        self.assertIsNotNone(self.bash("svcctl --owner probe quit"))

    def test_status_line(self):
        self.launched(coop=self.coop())
        line = self.status()
        self.assertIn("共同作業: probe、18:00（JST）まで（一緒に列車を走らせる）", line)
        self.assertNotIn("セーブ", line)
        self.launched(owner=None, coop=self.coop(who=["ui", "probe"], reason=""))
        line = self.status()
        self.assertIn("owner なし、pid 4242 が動いている", line)
        self.assertIn("共同作業: ui, probe、18:00（JST）まで", line)
        self.launched(coop=self.coop(until=self.NOW - 1))
        self.assertNotIn("共同作業", self.status())
        self.launched(pid=999, coop=self.coop())
        self.assertNotIn("共同作業", self.status())

    def test_status_save_changes(self):
        self.launched(coop=self.coop(modified=True, changes=3))
        self.assertIn("（一緒に列車を走らせる）、プレイ用のセーブを 3 回変えた", self.status())
        # 欄が無い・形が違うときは黙って省く（判定にも使わない）
        self.role("s-probe", "probe")
        for extra in ({"modified": True}, {"changes": 3}, {"modified": "yes", "changes": 3},
                      {"modified": True, "changes": "3"}, {"modified": True, "changes": True},
                      {"modified": True, "changes": 0}, {"modified": False, "changes": 2}):
            self.launched(coop=self.coop(**extra))
            line = self.status()
            self.assertIn("共同作業: probe", line, extra)
            self.assertNotIn("セーブ", line, extra)
            self.assertIsNone(self.bash("svcctl --owner probe quit"), extra)


class Robustness(Base):
    def run_hook(self, stdin, env=None):
        e = dict(os.environ, **(env or self.env))
        e.pop("CLAUDE_GUARD_ALLOW", None)
        return subprocess.run([sys.executable, str(BIN), "hook"], input=stdin, text=True, capture_output=True, env=e)

    def test_broken_json_exit0(self):
        for s in ("", "{", "[]", "null", json.dumps({"tool_name": 3}), json.dumps({"tool_name": "Bash", "tool_input": "x"})):
            r = self.run_hook(s)
            self.assertEqual(r.returncode, 0, s)
            self.assertEqual(r.stdout, "")

    def test_bad_configs_pass(self):
        for text in ("{", "[]", json.dumps({"guard_hook": []}), json.dumps({"guard_hook": {"guards": []}}),
                     json.dumps({"guard_hook": {"guards": {"x": {"process": "(", "paths": [str(self.shared_dir)]}}}}),
                     json.dumps({"guard_hook": {"guards": {"x": {"process": 3, "paths": 5}}}}),
                     json.dumps({"guard_hook": {"guards": {"x": {"process": "^a", "commands": [{"regex": "("}]}}}})):
            self.cfg.write_text(text)
            r = self.run_hook(json.dumps(self.hook_input("Bash", {"command": f"rm -rf {self.shared_dir}"})))
            self.assertEqual(r.returncode, 0, text)
            self.assertEqual(r.stdout, "", text)
        self.assertTrue((self.run_dir / "claude-guard" / "errors.log").exists())

    def test_bad_entry_keeps_other_guards(self):
        g = self.default_guards()
        g["broken"] = {"process": "(", "paths": ["relative/path"]}
        self.write_config(g)
        self.assertIsNotNone(self.bash("svcctl deploy a"))
        lines, bad = gh.status_lines(self.env, fake_probe())
        self.assertTrue(bad)
        self.assertTrue(any(l.startswith("問題:") and "broken" in l for l in lines))

    def test_status_shows_each_guard(self):
        gh.acquire(self.env, "deploy", 10, "v1", "sA")
        gh.write_state(self.env, "app-running", "allow", 30, "e2e")
        lines, bad = gh.status_lines(self.env, fake_probe(process=True))
        self.assertFalse(bad)
        text = "\n".join(lines)
        self.assertIn("app-running: 働いている（プロセス", text)
        self.assertIn("許可あり", text)
        self.assertIn("deploy: 働いている（旗", text)
        self.assertIn("持ち主 sA", text)
        self.assertIn("device-test: 働いていない", text)

    def test_status_no_config(self):
        self.cfg.unlink()
        lines, bad = gh.status_lines(self.env, fake_probe())
        self.assertIn("ガードなし", lines[0])

    def test_hook_output_shape_and_time(self):
        # 本物の pgrep で、動いていない名前のガードは何も出さない（そして速い）
        g = self.default_guards()
        g["app-running"]["process"] = "^no-such-process-for-claude-guard$"
        self.write_config(g)
        inp = json.dumps(self.hook_input("Bash", {"command": f"git -C {self.shared_dir}/m1 pull"}))
        t0 = time.monotonic()
        r = self.run_hook(inp)
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0)
        gh.write_state(self.env, "app-running", "on", 5, "")
        r = self.run_hook(inp)
        out = json.loads(r.stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertIn("[claude-guard]", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_other_event_ignored(self):
        d = self.hook_input("Bash", {"command": f"rm -rf {self.shared_dir}"})
        d["hook_event_name"] = "PostToolUse"
        gh.write_state(self.env, "app-running", "on", 5, "")
        self.assertIsNone(gh.decide(json.dumps(d), self.env, fake_probe(process=True)))

    def test_cli_acquire_release(self):
        e = dict(os.environ, **self.env)
        e["CLAUDE_CODE_SESSION_ID"] = "sA"
        run = lambda *a, env=e: subprocess.run([sys.executable, str(BIN), *a], env=env, capture_output=True, text=True)
        self.assertEqual(run("acquire", "deploy", "--minutes", "5", "--reason", "v1").returncode, 0)
        eb = dict(e, CLAUDE_CODE_SESSION_ID="sB")
        r = run("acquire", "deploy", env=eb)
        self.assertEqual(r.returncode, 1)
        self.assertIn("sA", r.stderr)
        self.assertEqual(run("release", "deploy", env=eb).returncode, 1)
        self.assertEqual(run("release", "deploy").returncode, 0)
        r = run("check", "--assume-active", f"rm -r {self.shared_dir}/m1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("block:", r.stdout)
        self.assertEqual(run("check", "--assume-active", "ls").returncode, 0)

    def test_errors_log_does_not_grow_with_many_problems(self):
        self.cfg.write_text(json.dumps({"guard_hook": {"guards": {
            "a": {"process": "(", "paths": ["rel"]}, "b": {"process": 3, "commands": [{"regex": "("}]}}}}))
        inp = json.dumps(self.hook_input("Bash", {"command": "ls"}))
        for _ in range(4):
            gh.decide(inp, self.env)
        n1 = len((self.run_dir / "claude-guard" / "errors.log").read_text().splitlines())
        for _ in range(4):
            gh.decide(inp, self.env)
        n2 = len((self.run_dir / "claude-guard" / "errors.log").read_text().splitlines())
        self.assertGreaterEqual(n1, 4)
        self.assertEqual(n1, n2)

    def test_cli_assume_active_flag_only_guard(self):
        e = dict(os.environ, **self.env)
        r = subprocess.run([sys.executable, str(BIN), "check", "--assume-active", "git push origin main"],
                           env=e, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("deploy", r.stdout)

    def test_cli_on_off_refuse_lock(self):
        e = dict(os.environ, **self.env, CLAUDE_CODE_SESSION_ID="sA")
        run = lambda *a: subprocess.run([sys.executable, str(BIN), *a], env=e, capture_output=True, text=True)
        self.assertEqual(run("acquire", "deploy").returncode, 0)
        self.assertEqual(run("on", "deploy").returncode, 1)
        self.assertEqual(run("off", "deploy").returncode, 1)
        self.assertEqual(run("off", "deploy", "--force").returncode, 0)

    def test_allow_skips_probe(self):
        calls = []
        gh.write_state(self.env, "app-running", "allow", 30, "e2e")
        self.assertIsNone(self.bash("svcctl deploy a", probe=fake_probe(process=True, calls=calls)))
        self.assertEqual(calls, [])

    def test_config_other_sections_untouched_by_reading(self):
        before = self.cfg.read_text()
        gh.load_guard_config(self.env)
        self.assertEqual(self.cfg.read_text(), before)


if __name__ == "__main__":
    unittest.main(verbosity=1)
