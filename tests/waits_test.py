#!/usr/bin/env python3
"""claude-waits（lib/waits.py・bin/claude-waits）の単体テスト。

一時の HOME・XDG_STATE_HOME・XDG_RUNTIME_DIR・設定ファイルだけを使う。kitty・claude・gh は使わない（gh と claude は
一時のフォルダの偽物、セッションが動いているかは関数を差し替える）。届けるのは本物の relay.receive()（local 送り）で、
置き場は一時のフォルダ。claude-state の記録が無いので、ペインの色も起こす socket も触らない。
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tools"))
import relay as R  # noqa: E402
import waits as W  # noqa: E402

BIN = ROOT / "bin" / "claude-waits"
SID = "sess-1"

FAKE_GH = r'''#!{py}
# 偽の gh: gh api --paginate <path> --jq length に、world の数を 2 ページに分けて返す
import json, sys
a = sys.argv[1:]
open({calls!r}, "a").write(json.dumps(a) + "\n")
world = json.load(open({world!r}))
if world.get("fail"):
    print("HTTP 401: Bad credentials", file=sys.stderr); sys.exit(1)
assert a[0] == "api" and "--paginate" in a and a[a.index("--jq") + 1] == "length", a
path = [x for x in a[1:] if x.startswith("repos/")][0]
kind = path.rsplit("/", 1)[1]
if "/issues/" not in path and kind == "comments":
    kind = "review_comments"
n = world[kind]
print(n // 2); print(n - n // 2)
'''

FAKE_CLAUDE = r'''#!{py}
# 偽の claude -p: 標準入力のプロンプトを受け、cwd と頭を返す
import os, sys
p = sys.stdin.read()
print("cwd=" + os.getcwd())
print("prompt=" + p.strip())
'''


def at_local(days_from_today, h, m=0):
    d = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=days_from_today)
    return d.replace(hour=h, minute=m).timestamp()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.cfg = self.d / "config.json"
        self.bin = self.d / "bin"
        self.bin.mkdir()
        (self.d / "home").mkdir()
        self.env = {"HOME": str(self.d / "home"), "CLAUDE_KITTY_CONFIG": str(self.cfg),
                    "XDG_STATE_HOME": str(self.d / "state"), "XDG_RUNTIME_DIR": str(self.d / "run"),
                    "PATH": f"{self.bin}:/usr/bin:/bin", "LANG": "C.UTF-8"}
        (self.d / "run").mkdir()
        self.config()
        self.alive_roles = {"main", "dev-a"}
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def config(self, waits=None, relay=None):
        data = {"view": {"left": "main"}, "relay": relay or {"receivers": ["left", "dev-a"]}}
        if waits is not None:
            data["waits"] = waits
        self.cfg.write_text(json.dumps(data))

    def alive(self, env, role):
        return role in self.alive_roles

    def add(self, key="k1", **kw):
        kw.setdefault("role", "dev-a")
        kw.setdefault("why", "PR の返事を待つ")
        kw.setdefault("then", "返事を読んで直す。終わったら何も消さなくてよい")
        if not any(kw.get(x) for x in ("when_cmd", "gh_pr", "at", "cloud")):
            kw["when_cmd"] = "true"
        if "expires" not in kw:
            kw["expires"] = "never" if (kw.get("at") or kw.get("cloud")) else "2d"
        return W.add(self.env, key, **kw)

    def check(self, now):
        return W.check(self.env, now=now, alive=self.alive, log=self.logs.append)

    def inbox(self, role):
        return R.inbox_items(self.env, role)


class Ledger(Base):
    def test_empty_ledger(self):
        self.assertEqual(W.all_records(self.env), ([], []))
        self.assertTrue(self.check(time.time()))
        self.assertEqual(W.status_line(self.env, "dev-a"), "")
        self.assertEqual(W.status_line(self.env, None), "")
        self.assertIsNone(W.hook_output("SessionStart", {"session_id": SID}, self.env))

    def test_add_writes_private_file_and_dir(self):
        rec, replaced = self.add()
        self.assertFalse(replaced)
        p = W.record_path(self.env, "k1")
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertEqual(p.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads(p.read_text())["role"], "dev-a")
        self.assertEqual(W.status_line(self.env, "dev-a"), "wait: 1")
        self.assertEqual(W.status_line(self.env, "main"), "")

    def test_same_key_overwrites(self):
        r1, _ = self.add(why="一回目")
        W.set_status(self.env, "k1", "done")
        r2, replaced = self.add(why="二回目")
        self.assertTrue(replaced)
        self.assertNotEqual(r1["rev"], r2["rev"])
        recs, _ = W.all_records(self.env)
        self.assertEqual([(r["key"], r["why"], r["status"]) for r in recs], [("k1", "二回目", "active")])

    def test_broken_json_is_skipped(self):
        self.add()
        d = W.ledger_dir(self.env)
        (d / "bad.json").write_text("{not json")
        (d / "wrongname.json").write_text(json.dumps({"key": "other", "role": "x", "kind": "cmd", "cond": {}}))
        recs, bad = W.all_records(self.env)
        self.assertEqual([r["key"] for r in recs], ["k1"])
        self.assertEqual(bad, ["bad.json", "wrongname.json"])
        self.assertTrue(self.check(time.time()))  # 壊れた記録で止まらない
        self.assertEqual(len(self.inbox("dev-a")), 1)

    def test_usage_errors(self):
        with self.assertRaises(W.Usage):
            self.add(expires="never")                     # 出来事に never は無い
        with self.assertRaises(W.Usage):
            self.add(expires=None)                        # 期限は必須
        with self.assertRaises(W.Usage):
            self.add(at="09:00", expires=None)            # 時刻も never か期限を明示
        with self.assertRaises(W.Usage):
            self.add(when_cmd="true", at="09:00")         # 条件は 1 つ
        with self.assertRaises(W.Usage):
            self.add(at="25:00")
        with self.assertRaises(W.Usage):
            self.add(at="09:00", days="funday")
        with self.assertRaises(W.Usage):
            self.add(key="../x")
        with self.assertRaises(W.Usage):
            self.add(then="")
        with self.assertRaises(W.Usage):
            self.add(every="5s")
        with self.assertRaises(W.Usage):
            self.add(expires="2020-01-01T00:00")         # 過去
        self.assertEqual(W.all_records(self.env), ([], []))

    def test_unknown_role_needs_role_flag(self):
        with self.assertRaises(W.Usage) as cm:
            self.add(role=None)  # kitty の外・claude-state の記録なし
        self.assertIn("--role", str(cm.exception))

    def test_parsers(self):
        self.assertEqual(W.parse_days("mon-fri"), [0, 1, 2, 3, 4])
        self.assertEqual(W.parse_days("mon,wed"), [0, 2])
        self.assertEqual(W.parse_days("sat-mon"), [0, 5, 6])
        self.assertEqual(W.parse_days(None), list(range(7)))
        self.assertEqual(W.parse_pr("https://github.com/o/r/pull/12"), ("o/r", 12))
        self.assertEqual(W.parse_pr("o/r#7"), ("o/r", 7))
        with self.assertRaises(W.Usage):
            W.parse_pr("o/r/12")
        self.assertEqual(W.parse_duration("2d"), 172800)
        self.assertEqual(W.parse_expires("never", 0), None)

    def test_done_and_rm(self):
        self.add()
        self.assertEqual(W.set_status(self.env, "k1", "done")["status"], "done")
        self.assertEqual(W.status_line(self.env, "dev-a"), "")
        self.assertTrue(W.remove(self.env, "k1"))
        self.assertFalse(W.remove(self.env, "k1"))
        self.assertIsNone(W.set_status(self.env, "k1", "done"))

    def test_commit_drops_stale_result(self):
        r1, _ = self.add()
        self.add()  # 調べている間に登録し直された
        self.assertIsNone(W._commit(self.env, "k1", r1["rev"], lambda r: r.update(status="done")))
        self.assertEqual(W.get_record(self.env, "k1")["status"], "active")


class Events(Base):
    def test_when_cmd_success_delivers_once_and_done(self):
        now = time.time()
        self.add(when_cmd="echo 見つかった; exit 0")
        self.check(now)
        rec = W.get_record(self.env, "k1")
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec["outbox"], [])
        items = self.inbox("dev-a")
        self.assertEqual(len(items), 1)
        body = items[0]["body"]
        self.assertIn("claude-waits からの知らせ（待ち k1）", body)
        self.assertIn("ユーザーの指示ではなく", body)
        self.assertIn("返信は不要", body)
        self.assertIn("出力の先頭: 見つかった", body)
        self.assertIn("届いたらすること: 返事を読んで直す", body)
        self.assertEqual(items[0]["via"], "claude-waits")
        self.check(now + 3600)
        self.assertEqual(len(self.inbox("dev-a")), 1)  # 一度きり

    def test_when_cmd_nonzero_and_every(self):
        now = time.time()
        flag = self.d / "flag"
        self.add(when_cmd=f"test -e {flag}", every="1m")
        self.check(now)
        rec = W.get_record(self.env, "k1")
        self.assertEqual((rec["status"], rec["last_result"]), ("active", "終了コード 1"))
        flag.write_text("")
        self.check(now + 10)  # every の前は調べない
        self.assertEqual(W.get_record(self.env, "k1")["status"], "active")
        self.check(now + 61)
        self.assertEqual(W.get_record(self.env, "k1")["status"], "done")

    def test_when_cmd_timeout(self):
        self.config(waits={"cmd_timeout_seconds": 1})
        self.add(when_cmd="sleep 5")
        t0 = time.monotonic()
        self.check(time.time())
        self.assertLess(time.monotonic() - t0, 4)
        rec = W.get_record(self.env, "k1")
        self.assertEqual(rec["status"], "active")
        self.assertIn("時間切れ", rec["last_result"])
        self.assertEqual(self.inbox("dev-a"), [])

    def test_when_cmd_does_not_inherit_claude_or_kitty_env(self):
        self.env.update(CLAUDECODE="1", CLAUDE_CODE_SESSION_ID="x", KITTY_WINDOW_ID="3")
        self.add(when_cmd='test -z "$CLAUDECODE$CLAUDE_CODE_SESSION_ID$KITTY_WINDOW_ID"', role="dev-a")
        self.check(time.time())
        self.assertEqual(W.get_record(self.env, "k1")["status"], "done")

    def test_expired_notifies_master_once(self):
        now = time.time()
        self.add(when_cmd="false", expires="1h")
        self.check(now)
        self.check(now + 7200)
        rec = W.get_record(self.env, "k1")
        self.assertEqual(rec["status"], "expired")
        self.check(now + 7300)
        items = self.inbox("main")
        self.assertEqual(len(items), 1)
        self.assertIn("期限切れ", items[0]["body"])
        self.assertIn("claude-waits rm k1", items[0]["body"])
        self.assertEqual(self.inbox("dev-a"), [])
        self.assertEqual(W.status_line(self.env, "dev-a"), "")

    def test_absent_owner_goes_to_master(self):
        self.alive_roles = {"main"}
        self.add(when_cmd="true")
        self.check(time.time())
        self.assertEqual(self.inbox("dev-a"), [])
        items = self.inbox("main")
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["body"].startswith("宛先 dev-a 宛て。不在"))

    def test_role_not_in_receivers_goes_to_master(self):
        self.add(when_cmd="true", role="dev-b")
        self.alive_roles.add("dev-b")
        self.check(time.time())
        items = self.inbox("main")
        self.assertEqual(len(items), 1)
        self.assertIn("宛先 dev-b 宛て。受け取れない（relay.receivers に dev-b が無い）", items[0]["body"])

    def test_undeliverable_stays_in_outbox_and_retries(self):
        self.config(relay={"receivers": ["dev-a"]})  # 統括も受け取らない
        self.alive_roles = set()
        now = time.time()
        self.add(when_cmd="true")
        self.check(now)
        rec = W.get_record(self.env, "k1")
        self.assertEqual(rec["status"], "done")
        self.assertEqual(len(rec["outbox"]), 1)
        self.assertIn("受け取らない", rec["last_error"])
        self.config()
        self.check(now + 60)
        self.assertEqual(W.get_record(self.env, "k1")["outbox"], [])
        self.assertEqual(len(self.inbox("main")), 1)

    def test_done_while_evaluating_does_not_fire(self):
        # 調べている間（when-cmd の最中）に done にされたら、古い結果で発火しない
        self.add(when_cmd="true")
        orig = W.evaluate

        def slow(env, conf, rec, now, run=None):
            W.set_status(env, rec["key"], "done")
            return orig(env, conf, rec, now)
        W.evaluate = slow
        try:
            self.check(time.time())
        finally:
            W.evaluate = orig
        self.assertEqual(W.get_record(self.env, "k1")["outbox"], [])
        self.assertEqual(self.inbox("dev-a"), [])

    def test_readd_keeps_undelivered(self):
        self.config(relay={"receivers": ["dev-a"]})
        self.alive_roles = set()
        now = time.time()
        self.add(when_cmd="true")
        self.check(now)
        self.assertEqual(len(W.get_record(self.env, "k1")["outbox"]), 1)
        self.add(when_cmd="false")  # 届く前に登録し直す
        self.assertEqual(len(W.get_record(self.env, "k1")["outbox"]), 1)
        self.config()
        self.check(now + 60)
        self.assertEqual(len(self.inbox("main")), 1)
        self.assertEqual(W.get_record(self.env, "k1")["status"], "active")

    def test_same_delivery_error_logged_once(self):
        self.config(relay={"receivers": ["dev-a"]})
        self.alive_roles = set()
        now = time.time()
        self.add(when_cmd="true")
        for i in range(3):
            self.check(now + 60 * i)
        self.assertEqual(len([x for x in self.logs if "届かない" in x]), 1)

    def test_timeout_kills_grandchildren(self):
        self.config(waits={"cmd_timeout_seconds": 1})
        mark = self.d / "late"
        self.add(when_cmd=f"(sleep 3; touch {mark}) & wait")
        self.check(time.time())
        time.sleep(3.5)
        self.assertFalse(mark.exists())  # 孫の sleep も止めた

    def test_owner_is_master(self):
        self.alive_roles = set()
        self.add(when_cmd="true", role="main")
        self.check(time.time())
        items = self.inbox("main")
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0]["body"].startswith("宛先"))

    def test_check_lock_busy(self):
        with W.ledger_lock(self.env, name=".check.lock"):
            self.assertFalse(self.check(time.time()))
        self.assertTrue(self.check(time.time()))

    def test_prune_old_done(self):
        now = time.time()
        self.add(when_cmd="true")
        self.check(now)
        self.check(now + 8 * 86400)
        self.assertIsNone(W.get_record(self.env, "k1"))


class GhPr(Base):
    def setUp(self):
        super().setUp()
        self.world = self.d / "world.json"
        self.calls = self.d / "calls.jsonl"
        self.set_world(reviews=1, comments=2, review_comments=0)
        gh = self.bin / "gh"
        gh.write_text(FAKE_GH.format(py=sys.executable, world=str(self.world), calls=str(self.calls)))
        gh.chmod(0o755)

    def set_world(self, **kw):
        self.world.write_text(json.dumps(kw))

    def test_base_then_grows(self):
        now = time.time()
        rec, _ = self.add(gh_pr="https://github.com/o/r/pull/5")
        self.assertEqual(rec["cond"]["base"], {"reviews": 1, "comments": 2, "review_comments": 0})
        self.check(now)
        self.assertEqual(W.get_record(self.env, "k1")["status"], "active")
        self.set_world(reviews=1, comments=2, review_comments=3)
        self.check(now + 301)
        rec = W.get_record(self.env, "k1")
        self.assertEqual(rec["status"], "done")
        body = self.inbox("dev-a")[0]["body"]
        self.assertIn("o/r#5 に 行へのコメント 3 件が増えた", body)
        self.assertIn("https://github.com/o/r/pull/5", body)
        calls = [json.loads(x) for x in self.calls.read_text().splitlines()]
        self.assertIn("repos/o/r/issues/5/comments", calls[1])

    def test_gh_failure_at_add_and_check(self):
        self.set_world(fail=True)
        with self.assertRaises(OSError):
            self.add(gh_pr="o/r#5")
        self.assertEqual(W.all_records(self.env), ([], []))
        self.set_world(reviews=0, comments=0, review_comments=0)
        self.add(gh_pr="o/r#5")
        self.set_world(fail=True)
        self.check(time.time())
        rec = W.get_record(self.env, "k1")
        self.assertEqual(rec["status"], "active")
        self.assertIn("gh の失敗", rec["last_result"])

    def test_no_gh(self):
        (self.bin / "gh").unlink()
        self.env["PATH"] = str(self.bin)  # /usr/bin の本物の gh も見せない
        with self.assertRaises(OSError) as cm:
            self.add(gh_pr="o/r#5")
        self.assertIn("gh が見つからない", str(cm.exception))


class Times(Base):
    def test_next_and_prev(self):
        days = list(range(7))
        t = at_local(0, 8)
        self.assertEqual(W.next_occurrence(t, 9, 0, days), at_local(0, 9))
        self.assertEqual(W.prev_occurrence(t, 9, 0, days), at_local(-1, 9))
        self.assertEqual(W.next_occurrence(at_local(0, 9), 9, 0, days), at_local(1, 9))  # ちょうどは次へ
        wd = datetime.fromtimestamp(t).weekday()
        only_tomorrow = [(wd + 1) % 7]
        self.assertEqual(W.next_occurrence(t, 9, 0, only_tomorrow), at_local(1, 9))

    def test_catchup_fires_once(self):
        start = at_local(0, 8)
        self.add(at="09:00", now=start)
        self.check(at_local(0, 8, 30))
        self.assertEqual(self.inbox("dev-a"), [])
        self.check(at_local(0, 15))  # 6 時間遅れ（後追いの窓 12 時間の中）
        items = self.inbox("dev-a")
        self.assertEqual(len(items), 1)
        self.assertIn("後追い", items[0]["body"])
        self.assertIn("定例なので次は", items[0]["body"])
        self.check(at_local(0, 15, 1))
        self.assertEqual(len(self.inbox("dev-a")), 1)
        rec = W.get_record(self.env, "k1")
        self.assertEqual((rec["status"], rec["next_at"], rec["fired"]), ("active", at_local(1, 9), 1))

    def test_missed_days_fire_only_once(self):
        self.add(at="09:00", now=at_local(0, 8))
        self.check(at_local(3, 10))  # 3 日分止まっていた: いちばん近い予定（1 時間前）だけ
        self.assertEqual(len(self.inbox("dev-a")), 1)
        self.assertEqual(W.get_record(self.env, "k1")["next_at"], at_local(4, 9))

    def test_zero_catchup_still_fires_on_time(self):
        self.config(waits={"catchup_hours": 0})
        self.add(at="09:00", now=at_local(0, 8))
        self.check(at_local(0, 9, 1))  # 常駐の間隔ほどの遅れは定刻の分
        self.assertEqual(len(self.inbox("dev-a")), 1)
        self.check(at_local(1, 15))  # 6 時間遅れは見送り
        self.assertEqual(len(self.inbox("dev-a")), 1)

    def test_outside_catchup_window_is_skipped(self):
        self.config(waits={"catchup_hours": 2})
        self.add(at="09:00", now=at_local(0, 8))
        self.check(at_local(0, 15))
        self.assertEqual(self.inbox("dev-a"), [])
        rec = W.get_record(self.env, "k1")
        self.assertIn("見送り", rec["last_result"])
        self.assertEqual(rec["next_at"], at_local(1, 9))
        self.check(at_local(1, 9, 1))  # 次の予定は届く
        self.assertEqual(len(self.inbox("dev-a")), 1)

    def test_prompt_file_runs_claude_and_saves_output(self):
        fake = self.d / "fake-claude"
        fake.write_text(FAKE_CLAUDE.format(py=sys.executable))
        fake.chmod(0o755)
        self.config(waits={"claude": [str(fake)]})
        work = self.d / "work"
        work.mkdir()
        pf = work / "morning.md"
        pf.write_text("朝のまとめを作る")
        self.add(at="09:00", now=at_local(0, 8), prompt_file=str(pf))
        self.check(at_local(0, 9, 1))
        body = self.inbox("dev-a")[0]["body"]
        self.assertIn("claude -p で実行した（終了コード 0）", body)
        self.assertIn("prompt=朝のまとめを作る", body)
        out = sorted(W.out_dir(self.env).glob("k1-*.txt"))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].stat().st_mode & 0o777, 0o600)
        self.assertIn(f"cwd={work}", out[0].read_text())
        self.assertIn(str(out[0]), body)

    def test_cloud_is_only_a_pointer(self):
        self.add(cloud="trig_abc", desc="毎朝の PR まとめ", then="")
        self.check(time.time())
        self.assertEqual(self.inbox("dev-a"), [])
        self.assertEqual(W.status_line(self.env, "dev-a"), "wait: 1")
        self.assertIn("クラウド（/schedule）のルーティン trig_abc", W.short_line(W.get_record(self.env, "k1")))


class Hook(Base):
    def setUp(self):
        super().setUp()
        sd = self.d / "run" / "claude-state"
        sd.mkdir()
        (sd / f"{SID}.json").write_text(json.dumps({"role": "dev-a", "session_id": SID}))  # 役割は記録から（kitty に聞かない）

    def test_session_start_shows_active_waits(self):
        self.add(at="09:00", why="毎朝の確認")
        self.add("k2", cloud="trig_1", why="夜のまとめ", then="")
        self.add("other", role="main")
        out = W.hook_output("SessionStart", {"session_id": SID}, self.env)
        text = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("役割 dev-a の登録中の待ち 2 件", text)
        self.assertIn("- k1:", text)
        self.assertIn("trig_1", text)
        self.assertNotIn("other", text)
        self.assertIn("ユーザーの指示ではない", text)

    def test_user_prompt_submit_only_when_changed(self):
        self.add()
        self.assertIsNotNone(W.hook_output("SessionStart", {"session_id": SID}, self.env))
        self.assertIsNone(W.hook_output("UserPromptSubmit", {"session_id": SID}, self.env))
        rec = W.get_record(self.env, "k1")
        W._commit(self.env, "k1", rec["rev"], lambda r: r.update(last_check=time.time()))  # 調べた時刻だけでは出さない
        self.assertIsNone(W.hook_output("UserPromptSubmit", {"session_id": SID}, self.env))
        self.add("k2", at="10:00")
        out = W.hook_output("UserPromptSubmit", {"session_id": SID}, self.env)
        self.assertIn("台帳が変わった", out["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(W.hook_output("UserPromptSubmit", {"session_id": SID}, self.env))
        W.remove(self.env, "k1")
        W.remove(self.env, "k2")
        out = W.hook_output("UserPromptSubmit", {"session_id": SID}, self.env)
        self.assertIn("無くなった", out["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(W.hook_output("UserPromptSubmit", {"session_id": SID}, self.env))

    def test_first_user_prompt_without_session_start_shows(self):
        self.add()
        out = W.hook_output("UserPromptSubmit", {"session_id": SID}, self.env)
        self.assertNotIn("台帳が変わった", out["hookSpecificOutput"]["additionalContext"])

    def test_no_role_or_bad_session(self):
        self.add()
        self.assertIsNone(W.hook_output("SessionStart", {"session_id": "nobody"}, self.env))
        self.assertIsNone(W.hook_output("SessionStart", {"session_id": "../x"}, self.env))
        self.assertIsNone(W.hook_output("Stop", {"session_id": SID}, self.env))

    def load_bin(self):
        loader = importlib.machinery.SourceFileLoader("claude_waits_bin", str(BIN))
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("claude_waits_bin", loader))
        loader.exec_module(mod)
        return mod

    def test_hook_exception_exits_zero_with_empty_stdout(self):
        mod = self.load_bin()
        orig = W.hook_output
        W.hook_output = lambda *a: (_ for _ in ()).throw(RuntimeError("こわれた"))
        try:
            buf = io.StringIO()
            stdin, sys.stdin = sys.stdin, io.StringIO(json.dumps({"session_id": SID}))  # 端末や開いたパイプを読まない
            try:
                with contextlib.redirect_stdout(buf):
                    rc = mod.cmd_hook("UserPromptSubmit", self.env)
            finally:
                sys.stdin = stdin
        finally:
            W.hook_output = orig
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue(), "")
        self.assertIn("RuntimeError: こわれた", (W.hook_dir(self.env) / "errors.log").read_text())

    def test_hook_cli_with_garbage_stdin(self):
        self.add()
        for stdin in ("not json", "[1, 2]", ""):
            r = subprocess.run([sys.executable, str(BIN), "hook", "UserPromptSubmit"], input=stdin, env=self.env,
                               capture_output=True, text=True, timeout=30)
            self.assertEqual((r.returncode, r.stdout), (0, ""))
        r = subprocess.run([sys.executable, str(BIN), "hook", "SessionStart"], input=json.dumps({"session_id": SID}),
                           env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("登録中の待ち 1 件", json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"])


class Cli(Base):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(BIN), *args], env=self.env, capture_output=True, text=True, timeout=30)

    def test_add_list_done_rm(self):
        r = self.run_cli("add", "pr-1", "--role", "dev-a", "--why", "w", "--then", "t", "--when-cmd", "false",
                         "--expires", "2d")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("登録した: pr-1", r.stdout)
        r = self.run_cli("add", "pr-1", "--role", "dev-a", "--why", "w", "--then", "t", "--when-cmd", "false",
                         "--expires", "2d")
        self.assertIn("上書きした: pr-1", r.stdout)
        r = self.run_cli("list", "--all", "--json")
        self.assertEqual([w["key"] for w in json.loads(r.stdout)["waits"]], ["pr-1"])
        self.assertIn("pr-1  [dev-a] 待ち", self.run_cli("list", "--role", "dev-a").stdout)
        self.assertIn("待ちは無い", self.run_cli("list", "--role", "main").stdout)
        self.assertEqual(self.run_cli("done", "pr-1").returncode, 0)
        self.assertEqual(self.run_cli("rm", "pr-1").returncode, 0)
        r = self.run_cli("rm", "pr-1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("無いキー", r.stderr)

    def test_usage_exit_2(self):
        r = self.run_cli("add", "x", "--role", "dev-a", "--why", "w", "--then", "t", "--when-cmd", "true")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--expires", r.stderr)
        self.assertEqual(self.run_cli("bogus").returncode, 2)

    def test_status_is_quiet_outside_kitty(self):
        r = self.run_cli("status")
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_version(self):
        self.assertTrue(self.run_cli("--version").stdout.startswith("claude-waits "))


class Integration(Base):
    """relay の枠・設定・install・doctor・ステータス行との継ぎ目。"""

    def test_relay_frame_says_no_reply(self):
        self.add(when_cmd="true")
        self.check(time.time())
        text, rest = R.take(self.env, "dev-a")
        self.assertEqual(rest, 0)
        self.assertIn('<relay-message from="claude-waits"', text)
        self.assertIn("返信は不要", text)
        self.assertNotIn("claude-relay send", text)
        self.assertTrue(text.startswith("claude-relay: claude-waitsから 1 通"))

    def test_config_defaults_and_errors(self):
        from config import DEFAULT_WAITS, load_waits_config
        c = load_waits_config(self.env)
        self.assertEqual(c["master_role"], "main")
        self.config(waits={"master_role": "lead", "poll_seconds": 30})
        self.assertEqual(load_waits_config(self.env)["master_role"], "lead")
        for bad in ({"poll": 1}, {"poll_seconds": 1}, {"claude": []}, {"enabled": "yes"}, {"master_role": "a b"}):
            self.config(waits=bad)
            with self.assertRaises(ValueError):
                load_waits_config(self.env)
        doc = (ROOT / "docs/config.md").read_text().split("## waits", 1)[1].split("\n## ", 1)[0]
        for k, v in DEFAULT_WAITS.items():
            self.assertIn(f"| `{k}` | `{json.dumps(v)}` |", doc)
        ex = json.loads((ROOT / "config.example.json").read_text())
        self.assertEqual(ex["waits"], DEFAULT_WAITS)

    def test_merge_hooks_part(self):
        import merge_hooks as M
        _lines, new = M.install({}, "/r", ["waits"])
        self.assertEqual(sorted(new["hooks"]), ["SessionStart", "UserPromptSubmit"])
        self.assertEqual(new["hooks"]["SessionStart"][0]["hooks"][0]["command"], "/r/bin/claude-waits hook SessionStart")
        self.assertEqual(M.status(new)["waits"], ("all", []))
        self.assertNotIn("waits", M.parse_parts(""))  # --all には入れない
        text = (ROOT / "tools/install.sh").read_text()
        self.assertIn('"suffix","waits"}', text)
        self.assertIn("hook_parts=state,guard;", text)

    def test_units_exist(self):
        unit = (ROOT / "contrib/systemd/claude-waits.service").read_text()
        self.assertIn("ExecStart=/usr/bin/python3 %h/claude-kitty/bin/claude-waits run", unit)
        plist = (ROOT / "contrib/launchd/claude-kitty.waits.plist").read_text()
        self.assertIn("<string>@@REPO@@/bin/claude-waits</string>", plist)
        import doctor
        self.assertIn("claude-waits.service", doctor.UNITS)
        self.assertIn("claude-kitty.waits", doctor.AGENTS)

    def test_doctor_rows(self):
        import doctor
        rep = doctor.Report()
        ctx = type("Ctx", (), {"env": self.env})()
        doctor.check_waits(ctx, rep)
        self.assertEqual(rep.items[0]["status"], "ok")
        self.add()
        self.config(relay={"receivers": ["dev-a"]})
        rep = doctor.Report()
        doctor.check_waits(ctx, rep)
        st = {i["name"]: i["status"] for i in rep.items}
        self.assertEqual(st["待ちの台帳"], "ok")
        self.assertEqual(st["待ちの台帳: 届け先"], "ng")

    def test_statusline_segment(self):
        import statusline as SL
        from config import DEFAULT_STATUSLINE
        from state import merge_colors, merge_subagent_colors
        self.add()
        conf = json.loads(json.dumps(DEFAULT_STATUSLINE))
        conf["weekly_roles"] = []
        st = {"record": {"role": "dev-a", "state": "idle"}, "colors": merge_colors(None),
              "subagent_colors": merge_subagent_colors(None)}
        self.assertIn(" | wait: 1", SL.build({"session_name": "dev-a"}, self.env, time.time(), conf, st))
        conf["waits"] = False
        self.assertNotIn("wait:", SL.build({"session_name": "dev-a"}, self.env, time.time(), conf, st))

    def test_skill_file(self):
        text = (ROOT / "contrib/skills/claude-waits/SKILL.md").read_text()
        self.assertTrue(text.startswith("---\nname: claude-waits\ndescription: "))
        for word in ("/schedule", "claude-waits add", "--cloud", "--expires"):
            self.assertIn(word, text)


if __name__ == "__main__":
    unittest.main(verbosity=1)
