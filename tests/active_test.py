#!/usr/bin/env python3
"""claude-active（lib/active.py・bin/claude-active）のテスト。偽の会話の記録とセッションのファイルを一時フォルダに置く。"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import active as A  # noqa: E402
import proc  # noqa: E402

BIN = ROOT / "bin" / "claude-active"
NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z
SID1 = "11111111-1111-4111-8111-111111111111"
SID2 = "22222222-2222-4222-8222-222222222222"
SID3 = "33333333-3333-4333-8333-333333333333"


def ts(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def human(t, text="テストを足して", source="typed"):
    return {"type": "user", "timestamp": ts(t), "message": {"role": "user", "content": text},
            "origin": {"kind": "human"}, "turnOrigin": "human", "promptSource": source}


def peer(t):
    return {"type": "user", "timestamp": ts(t), "isMeta": True, "origin": {"kind": "peer"}, "turnOrigin": "peer",
            "promptSource": "system",
            "message": {"role": "user", "content": 'Another Claude session sent a message: <cross-session-message from="x">hi</cross-session-message>'}}


def cron(t):
    return {"type": "user", "timestamp": ts(t), "isMeta": True, "turnOrigin": "scheduled", "scheduledTaskId": "c1",
            "message": {"role": "user", "content": "見回り: 担当の様子を見る"}}


def task_note(t):
    return {"type": "user", "timestamp": ts(t), "origin": {"kind": "task-notification"},
            "message": {"role": "user", "content": "<task-notification><task-id>a</task-id></task-notification>"}}


def tool_result(t):
    return {"type": "user", "timestamp": ts(t), "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}, "origin": {"kind": "human"}}


def companion(t):
    return {"type": "user", "timestamp": ts(t), "isMeta": True, "turnCompanion": True, "origin": {"kind": "human"},
            "turnOrigin": "human", "message": {"role": "user", "content": "[Image #1]"}}


def legacy(t, text="人かもしれない"):
    return {"type": "user", "timestamp": ts(t), "message": {"role": "user", "content": text}}


def enqueue(t, text, op="enqueue"):
    return {"type": "queue-operation", "operation": op, "timestamp": ts(t), "sessionId": SID1, "content": text}


def plain(t, text, turn=None):
    """origin の無い行（! コマンド・スラッシュコマンドの行の形）。"""
    d = {"type": "user", "timestamp": ts(t), "parentUuid": "p", "message": {"role": "user", "content": text}}
    if turn:
        d["turnOrigin"] = turn
    return d


def assistant(t):
    return {"type": "assistant", "timestamp": ts(t), "message": {"role": "assistant", "content": [{"type": "text", "text": "はい"}]}}


class Lines(unittest.TestCase):
    def test_human_kinds(self):
        for src in ("typed", "suggestion_accepted", "queued"):
            self.assertIs(A.is_human_line(human(NOW, source=src)), True)
        only_turn = human(NOW)
        del only_turn["origin"]
        self.assertIs(A.is_human_line(only_turn), True)  # turnOrigin だけでもよい

    def test_not_human(self):
        for d in (peer(NOW), cron(NOW), task_note(NOW), tool_result(NOW), companion(NOW), assistant(NOW),
                  human(NOW, "<task-notification>x</task-notification>"),
                  human(NOW, "<system-reminder>x</system-reminder>"),
                  human(NOW, "<agent-message from='a'>done</agent-message>"),
                  human(NOW, "claude-relay: 届いた [relay-wake ids=r1]"),
                  human(NOW, "[claude-kitty disk] /tmp 90%"),
                  dict(human(NOW), turnOrigin="peer"),            # 片方が人でなければ人でない
                  dict(human(NOW), origin={"kind": "peer"}),
                  "x", None, {"type": "user"}):
            self.assertIs(A.is_human_line(d), False, repr(d)[:80])

    def test_compact_summary_and_launch_prompt(self):
        # 実際の記録の形: 自動の要約は turnOrigin が human で origin 無し・promptSource 無し
        summary = {"type": "user", "timestamp": ts(NOW), "parentUuid": "p", "isCompactSummary": True,
                   "isVisibleInTranscriptOnly": True, "turnOrigin": "human", "turnPosition": {"promptIndex": 200},
                   "message": {"role": "user", "content": "This session is being continued from a previous conversation"}}
        self.assertIs(A.is_human_line(summary), False)
        self.assertIs(A.is_human_line(dict(summary, isCompactSummary=None)), False)
        # 統括が `claude "指示"` で起こした担当の最初の発話（human typed と記録される）は数えない
        launch = dict(human(NOW, "あなたは aom-probe の 2 人目です"), parentUuid=None,
                      turnPosition={"promptIndex": 1, "turnIndex": 1})
        self.assertIs(A.is_human_line(launch), False)
        self.assertIs(A.is_human_line(dict(launch, parentUuid="p", turnPosition={"promptIndex": 2})), True)
        # 前のある会話の 1 番目は人とみなす。`claude --resume <ID> "指示"` の引数の指示もこの形（promptIndex は 1 に
        # 戻るが parentUuid がある）で人と数えてしまうが、いまのツールは resume に指示を付けない（docs/active.md の限界）
        self.assertIs(A.is_human_line(dict(launch, parentUuid="p")), True)
        # 新しい会話の最初でも、親がスラッシュコマンド（local_command の system の行）なら人が打ったもの。
        # Claude Code の形が変わって数えなくなったら、ここで気づく
        after_slash = dict(human(NOW, "続けて"), parentUuid="uuid-of-local-command",
                           turnPosition={"promptIndex": 1, "turnIndex": 1})
        self.assertIs(A.is_human_line(after_slash), True)

    def test_command_lines(self):
        # 人の ! コマンドの結果（turnOrigin が human）は数える
        self.assertIs(A.is_human_line(plain(NOW, "<bash-stdout>ok</bash-stdout><bash-stderr></bash-stderr>", "human")), True)
        # origin も turnOrigin も無い ! の入力・スラッシュコマンドの行は「古い形式」にせず黙って飛ばす
        for text in ("<bash-input>ls</bash-input>", "<command-name>/clear</command-name>",
                     "<local-command-stdout>Added allow rule</local-command-stdout>"):
            self.assertIs(A.is_human_line(plain(NOW, text)), False, text)
        best, legacy = A.scan_lines([json.dumps(plain(NOW, "<bash-input>ls</bash-input>")),
                                     json.dumps(plain(NOW, "<command-name>/clear</command-name>"))])
        self.assertEqual((best, legacy), (None, False))

    def test_queued_uses_enqueue_time(self):
        # 実例: 04:45:28Z に enqueue、05:01:12Z に queued として会話に入った（15.7 分の遅れ）
        q = human(NOW, "/,pde;", source="queued")
        lines = [json.dumps(x) for x in (enqueue(NOW - 944, "/,pde;"), enqueue(NOW - 10, "<task-notification>x"), q)]
        self.assertEqual(A.scan_lines(lines), (NOW - 944, False))
        # 同じ本文が 2 度待たされたら、先に入れたものから
        lines = [json.dumps(x) for x in (enqueue(NOW - 900, "a"), enqueue(NOW - 600, "a"),
                                         human(NOW - 300, "a", "queued"), human(NOW, "a", "queued"))]
        self.assertEqual(A.scan_lines(lines)[0], NOW - 600)
        # enqueue が見つからない（読んだ範囲より前・取り除かれた）なら数えない（away 側）
        self.assertEqual(A.scan_lines([json.dumps(q)]), (None, False))
        lines = [json.dumps(x) for x in (enqueue(NOW - 900, "b"), enqueue(NOW - 800, "b", "remove"),
                                         human(NOW, "b", "queued"))]
        self.assertEqual(A.scan_lines(lines), (None, False))
        # 待たせた発話より前の、ふつうに打った発話はそのまま
        lines = [json.dumps(x) for x in (human(NOW - 1200, "x"), q)]
        self.assertEqual(A.scan_lines(lines)[0], NOW - 1200)

    def test_legacy_is_none(self):
        self.assertIsNone(A.is_human_line(legacy(NOW)))

    def test_parse_ts(self):
        self.assertEqual(A.parse_ts(ts(NOW)), NOW)
        for bad in (None, "", "yesterday", 3, "2026-11-22T22:13:04"):  # 時差の無い時刻も使わない
            self.assertIsNone(A.parse_ts(bad), repr(bad))


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.cfg = self.d / "cfg"
        (self.cfg / "sessions").mkdir(parents=True)
        (self.cfg / "projects").mkdir(parents=True)
        self.rt = self.d / "rt"
        (self.rt / "claude-state").mkdir(parents=True)
        self.roster = self.d / "roster"
        self.roster.mkdir()
        self.roster_sessions = {}
        self.pid = os.getpid()
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def session(self, sid, role=None, via="state", folder="-var-home-u", lines=(), live=True, config=None):
        """生きたセッション（pid はこのテストのプロセス）と会話の記録を置く。"""
        base = Path(config) if config else self.cfg
        self.n += 1
        st = proc.start_time(self.pid) if live else "0"
        (base / "sessions").mkdir(parents=True, exist_ok=True)
        (base / "sessions" / f"{self.n}.json").write_text(json.dumps(
            {"pid": self.pid, "sessionId": sid, "cwd": "/x", "procStart": st, "kind": "interactive"}))
        if role and via == "state":
            (self.rt / "claude-state" / f"{sid}.json").write_text(json.dumps({"session_id": sid, "role": role}))
        elif role:
            self.roster_sessions[role] = {"session_id": sid, **({"config_dir": str(config)} if config else {})}
            (self.roster / "roster.json").write_text(json.dumps({"version": 1, "sessions": self.roster_sessions}))
        if lines is not None:
            p = base / "projects" / folder / f"{sid}.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines))
            if lines:  # 更新時刻は最後の行の時刻（Claude Code が書いたときと同じ。NOW は未来なので合わせる）
                t = max(A.parse_ts(x["timestamp"]) for x in lines)
                os.utime(p, (t, t))
            return p
        return None

    def env(self):
        e = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_", "XDG_"))}
        e.update(HOME=str(self.d / "home"), CLAUDE_CONFIG_DIR=str(self.cfg), XDG_RUNTIME_DIR=str(self.rt),
                 CLAUDE_ROSTER_DIR=str(self.roster), LANG="C.UTF-8")
        return e

    def judge(self, minutes=20, now=NOW, env=None):
        sys.path.insert(0, str(ROOT / "bin"))
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("claude_active_bin", str(BIN))
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("claude_active_bin", loader))
        loader.exec_module(mod)
        return mod.run(env or self.env(), minutes, now=now)

    def cli(self, *args):
        return subprocess.run([sys.executable, str(BIN), *args], env=self.env(), capture_output=True, text=True,
                              timeout=30)


class Judge(Fixture):
    def test_right_after_human(self):
        self.session(SID1, "aom-master", lines=[assistant(NOW - 100), human(NOW)])
        j = self.judge()
        self.assertEqual((j["status"], j["code"], j["minutes"], j["role"]), ("active", 0, 0.0, "aom-master"))
        self.assertEqual(A.format_line(j), "active 0分前 aom-master（17:00 JST）")

    def test_cron_does_not_advance(self):
        self.session(SID1, "aom-master", lines=[human(NOW - 47 * 60), cron(NOW - 60), cron(NOW)])
        j = self.judge()
        self.assertEqual((j["status"], j["code"], int(j["minutes"])), ("away", 1, 47))
        self.assertEqual(A.format_line(j), "away 47分前 aom-master（16:13 JST）")

    def test_peer_and_machine_lines_do_not_advance(self):
        self.session(SID1, "aom-probe", lines=[human(NOW - 30 * 60), peer(NOW - 5), task_note(NOW - 4),
                                               tool_result(NOW - 3), companion(NOW - 2),
                                               human(NOW - 1, "<cross-session-message from='a'>x</cross-session-message>")])
        j = self.judge()
        self.assertEqual((j["status"], int(j["minutes"])), ("away", 30))

    def test_legacy_lines_not_counted(self):
        self.session(SID1, "aom-master", lines=[legacy(NOW - 60)])
        j = self.judge()
        self.assertEqual((j["status"], j["code"], j["legacy"]), ("unknown", 2, 1))
        self.assertIn("古い形式", A.format_line(j))
        # 新しい形式の人の発話があれば、それより新しい古い形式の行は数えない（起きていると取り違えない）
        self.session(SID2, "aom-ui", lines=[human(NOW - 50 * 60), legacy(NOW)])
        j = self.judge()
        self.assertEqual((j["status"], int(j["minutes"]), j["role"]), ("away", 50, "aom-ui"))

    def test_no_transcript_is_unknown(self):
        self.session(SID1, "aom-master", lines=None)
        j = self.judge()
        self.assertEqual((j["status"], j["code"], j["missing"]), ("unknown", 2, 1))
        self.assertEqual(A.format_line(j), "unknown 人の発話が見つからない（見たセッション 1、記録が無い 1）")

    def test_unreadable_transcript_is_unknown(self):
        p = self.session(SID1, "aom-master", lines=[human(NOW)])
        os.chmod(p, 0)
        try:
            if os.access(p, os.R_OK):
                self.skipTest("root では読めてしまう")
            j = self.judge()
            self.assertEqual((j["status"], j["code"], j["unreadable"]), ("unknown", 2, 1))
        finally:
            os.chmod(p, 0o600)

    def test_no_sessions_is_unknown(self):
        j = self.judge()
        self.assertEqual((j["status"], j["code"], j["seen"]), ("unknown", 2, 0))
        # 死んだセッション・役割の無いセッションは見ない
        self.session(SID1, "aom-master", lines=[human(NOW)], live=False)
        self.session(SID2, None, lines=[human(NOW)])
        j = self.judge()
        self.assertEqual((j["status"], j["seen"]), ("unknown", 0))

    def test_queued_in_file(self):
        self.session(SID1, "aom-master", lines=[enqueue(NOW - 25 * 60, "見て"), assistant(NOW - 60),
                                                human(NOW - 30, "見て", "queued")])
        j = self.judge()
        self.assertEqual((j["status"], int(j["minutes"])), ("away", 25))

    def test_future_line_ignored(self):
        # 時計のずれ・壊れた timestamp の先の行で active が続かないように
        self.session(SID1, "aom-master", lines=[human(NOW - 50 * 60), human(NOW + 3600)])
        j = self.judge()
        self.assertEqual((j["status"], int(j["minutes"])), ("away", 50))
        self.session(SID2, "aom-ui", lines=[human(NOW + 60)])  # 少し先（FUTURE_SLACK 以内）は 0 分
        self.assertEqual((self.judge()["role"], self.judge()["minutes"]), ("aom-ui", 0.0))

    def test_reads_home_from_env_only(self):
        # 本物の ~/.claude を読まない（HOME の置き場に置いたセッションは見え、CLAUDE_CONFIG_DIR が無くても見る）
        h = self.d / "home" / ".claude"
        self.session(SID1, "aom-master", config=h, lines=[human(NOW - 60)])
        e = self.env()
        del e["CLAUDE_CONFIG_DIR"]
        self.assertEqual(self.judge(env=e)["role"], "aom-master")

    def test_minutes_option(self):
        self.session(SID1, "aom-master", lines=[human(NOW - 25 * 60)])
        self.assertEqual(self.judge(20)["code"], 1)
        self.assertEqual(self.judge(30)["code"], 0)
        self.assertEqual(self.judge(25)["code"], 0)  # ちょうどは「以内」

    def test_newest_of_many_sessions(self):
        self.session(SID1, "aom-master", lines=[human(NOW - 40 * 60)])
        self.session(SID2, "aom-probe", via="roster", lines=[human(NOW - 3 * 60)])  # 役割は名簿から
        self.session(SID3, "aom-ui", lines=[human(NOW - 10 * 60), peer(NOW)])
        j = self.judge()
        self.assertEqual((j["role"], j["session_id"], int(j["minutes"]), j["seen"]), ("aom-probe", SID2, 3, 3))

    def test_skips_files_older_than_found(self):
        # 更新時刻が見つかった発話より古い記録は読まない（その中にもっと新しい行は無い）
        self.session(SID1, "aom-master", lines=[human(NOW - 60)])
        p = self.session(SID2, "aom-ui", lines=[human(NOW - 600)])
        seen = []
        real = A.last_human
        res = A.find_last(A.live_sessions([self.cfg / "sessions"]), {SID1: "aom-master", SID2: "aom-ui"}.get,
                          [self.cfg / "projects"], scan=lambda f, **k: (seen.append(f), real(f, **k))[1])
        self.assertEqual((res["role"], res["at"]), ("aom-master", NOW - 60))
        self.assertNotIn(p, seen)

    def test_transcript_in_other_folder_and_config_dir(self):
        # フォルダをまたいで再開した記録は元のフォルダの側。名簿の config_dir の置き場も見る
        self.session(SID1, "aom-master", folder="-var-home-u-elsewhere", lines=[human(NOW - 9 * 60)])
        other = self.d / "proj-cfg"
        self.session(SID2, "aom-text", via="roster", config=other, lines=[human(NOW - 2 * 60)])
        j = self.judge()
        self.assertEqual((j["role"], int(j["minutes"])), ("aom-text", 2))


class Tail(unittest.TestCase):
    def test_reads_from_the_end_of_a_large_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "big.jsonl"
            pad = json.dumps(peer(NOW - 3600)) + "\n"
            with open(p, "w") as f:
                f.write(json.dumps(human(NOW - 7200)) + "\n")
                f.write(pad * (3 * 1024 * 1024 // len(pad)))  # 末尾の 2 MB には人がいない
                f.write(json.dumps(human(NOW - 600)) + "\n")
                f.write(pad * 100)
            # 末尾だけで見つかる
            self.assertEqual(A.last_human(p, spans=(64 * 1024,)), (NOW - 600, False))
            # 読んだ量を数える: 小さな窓で見つかれば、それより前は読まない
            reads = []
            real_open = open

            class Spy:
                def __init__(self, fh):
                    self.fh = fh

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    self.fh.close()

                def seek(self, n):
                    return self.fh.seek(n)

                def read(self, n):
                    reads.append(n)
                    return self.fh.read(n)
            import builtins
            orig = builtins.open
            builtins.open = lambda *a, **k: Spy(real_open(*a, **k))
            try:
                got = A.last_human(p, spans=(64 * 1024, 1024 * 1024))
            finally:
                builtins.open = orig
            self.assertEqual(got[0], NOW - 600)
            self.assertEqual(reads, [64 * 1024])
            # 末尾に無ければ前へ広げ、最後は全体を読む
            with open(p, "a") as f:
                f.write(pad * (1024 * 1024 // len(pad)))
            self.assertEqual(A.last_human(p, spans=(64 * 1024,))[0], NOW - 600)

    def test_stops_when_older_than_found(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.jsonl"
            pad = json.dumps(peer(NOW - 3600)) + "\n"
            p.write_text(json.dumps(human(NOW - 7200)) + "\n" + pad * 2000)
            self.assertEqual(A.last_human(p, spans=(4096,))[0], NOW - 7200)
            # ほかで NOW - 60 が見つかっていれば、窓の頭がそれより古い時点で前は読まない
            self.assertEqual(A.last_human(p, spans=(4096,), not_before=NOW - 60), (None, False))


class Cli(Fixture):
    def test_exit_codes_and_json(self):
        r = self.cli()
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertTrue(r.stdout.startswith("unknown "), r.stdout)
        now = time.time()
        self.session(SID1, "aom-master", lines=[human(now - 3 * 60)])
        r = self.cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout.strip(), r"^active 3分前 aom-master（\d\d:\d\d JST）$")
        r = self.cli("--minutes", "2", "--json")
        self.assertEqual(r.returncode, 1, r.stderr)
        j = json.loads(r.stdout)
        self.assertEqual((j["status"], j["code"], j["role"], j["threshold_minutes"]), ("away", 1, "aom-master", 2))
        self.assertTrue(j["at_jst"].endswith("+09:00"))

    def test_bad_minutes(self):
        for v in ("inf", "nan", "-1", "0"):
            r = self.cli("--minutes", v)
            self.assertEqual(r.returncode, 2, v)
            self.assertEqual(r.stdout, "", v)

    def test_version(self):
        r = self.cli("--version")
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stdout.startswith("claude-active "))


if __name__ == "__main__":
    unittest.main()
