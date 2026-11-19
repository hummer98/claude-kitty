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


class Commands(Base):
    def test_program_rule(self):
        for c in ("svcctl.py --owner me deploy v1", "python3 ~/x/tools/svcctl.py --owner=me scale 4",
                  "svcctl backup s1", "svcctl config --set 1,2", "svcctl reset"):
            self.assertIsNotNone(self.bash(c), c)
        for c in ("svcctl.py --owner me status", "svcctl.py ping", "svcctl config", "svcctl reset --list",
                  "svcctl deploy --dry-run s1", "svcctl log --grep backup"):
            self.assertIsNone(self.bash(c), c)

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
