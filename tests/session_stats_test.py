#!/usr/bin/env python3
"""claude-session-stats の単体テスト。python3 tests/session_stats_test.py（標準ライブラリだけ）

会話の記録は、ここで組み立てる架空のもの（小さな jsonl）だけを使う。
"""
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import sessionstats as ss  # noqa: E402

BIN = ROOT / "bin" / "claude-session-stats"
_loader = importlib.machinery.SourceFileLoader("claude_session_stats_bin", str(BIN))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
cli = importlib.util.module_from_spec(_spec)
_loader.exec_module(cli)

HOME_PROJ = "-fake-home-tester"
MASTER = "lead"
ROLES = ["dev-a", "dev-b", "dev-c", "dev-env"]
PREFIX = "-team-"

# 架空の言い直しの表（利用者の設定に書く形と同じ）
REPHRASE = [
    ["again", "「もう一度」", r"^\s*(もう一度|(?i:again)(?![A-Za-z0-9\[]))"],
    ["next", "「次は？」", r"次は？|次の判断"],
    ["explain", "「説明して」", r"説明して|とは？"],
    ["progress", "進み具合の確認", r"^(?:(?:進捗|状況)は?)?どうですか[？?]?$|進捗|はまだ(?:ですか|？|\?)\s*$"],
]

# 架空の用語集のツール（設定の glossary.module に置く .py と同じ形）
FAKE_GLOSSARY_TOOL = '''
from dataclasses import dataclass

@dataclass
class Rule:
    bad: str

def parse_glossary(text):
    rules = [Rule(line[1:].strip()) for line in text.splitlines() if line.startswith("-")]
    if not rules:
        raise ValueError("no rules")
    return rules

def check_text(text, rules, scene="all"):
    assert scene == "reply"
    out = []
    for r in rules:
        out += [r.bad] * text.replace("「" + r.bad + "」", "").count(r.bad)
    return out
'''

JA_LONG = "これは日本語の返答です。" * 20  # 240 字、すべて日本語
EN_LONG = "This is an English reply that keeps going. " * 6  # 258 字


def settings(**over):
    base = {"master": MASTER, "roles": ROLES, "role_dir_prefix": PREFIX, "rephrase": REPHRASE,
            "home_project": HOME_PROJ, "timezone": "Asia/Tokyo"}
    base.update(over)
    return ss.make_settings(base, env={})


class Writer:
    """1つの jsonl を組み立てる。時刻は day の 10:00 JST（01:00Z）から1分ずつ進む。"""

    def __init__(self, path, session_id="s1", name=None, start_min=0, day="2026-03-15"):
        self.path = path
        self.sid = session_id
        self.n = 0
        self.minute = start_min
        self.day = day
        self.lines = []
        if name:
            self.raw({"type": "agent-name", "agentName": name, "sessionId": session_id})
            self.raw({"type": "custom-title", "customTitle": name, "sessionId": session_id})

    def ts(self, minute=None):
        m = self.minute if minute is None else minute
        return f"{self.day}T{1 + m // 60:02d}:{m % 60:02d}:00.000Z"

    def uuid(self):
        self.n += 1
        # 本物と同じ 16 進の形（続きの会話の判定は 16 進の uuid だけを拾う）
        return f"{hashlib.md5(self.sid.encode()).hexdigest()[:8]}-0000-0000-0000-{self.n:012d}"

    def raw(self, obj):
        # 本物の記録と同じ詰めた書き方（型の先読みが効く形）
        self.lines.append(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))

    def bad(self, text='{"type":"user", broken'):
        self.lines.append(text)

    def _base(self, typ, **kw):
        o = {"type": typ, "uuid": kw.pop("uuid", None) or self.uuid(), "timestamp": self.ts(),
             "sessionId": self.sid, "isSidechain": False}
        o.update(kw)
        self.minute += 1
        return o

    def user(self, text, **kw):
        self.raw(self._base("user", message={"role": "user", "content": text}, **kw))

    def user_at(self, ts, text, uuid=None):
        self.raw({"type": "user", "uuid": uuid or self.uuid(), "timestamp": ts, "isSidechain": False,
                  "message": {"role": "user", "content": text}})

    def tool_result(self):
        self.raw(self._base("user", message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "もう一度"}]}))

    def queued(self, text, **kw):
        self.raw(self._base("attachment", attachment={"type": "queued_command", "prompt": text,
                                                      "commandMode": "prompt"}, **kw))

    def reply(self, text, sends=(), **kw):
        content = [{"type": "text", "text": text}] if text else []
        for to, body in sends:
            content.append({"type": "tool_use", "name": "SendMessage",
                            "input": {"to": to, "message": body, "recipient_kind": "name"}})
        self.raw(self._base("assistant", message={"role": "assistant", "model": "m", "content": content}, **kw))

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.lines) + ("\n" if self.lines else ""))


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="session_stats_test_")
        self.root = self._tmp.name
        self.home = os.path.join(self.root, HOME_PROJ)
        os.makedirs(self.home)
        self.cfg = settings(projects=self.root)
        tool = os.path.join(self.root, "tool", "fake_glossary.py")
        os.makedirs(os.path.dirname(tool))
        Path(tool).write_text(FAKE_GLOSSARY_TOOL)
        words = os.path.join(self.root, "tool", "words.txt")
        Path(words).write_text("- 確かめ\n")
        self.tool, self.words = tool, words
        self.glossary = ss.Glossary.load(tool, words, "reply")

    def tearDown(self):
        self._tmp.cleanup()

    def writer(self, fname, sub=None, **kw):
        d = self.home if sub is None else os.path.join(self.root, HOME_PROJ + PREFIX + sub)
        return Writer(os.path.join(d, fname), **kw)

    def collect(self, lo=date(2026, 3, 14), hi=date(2026, 3, 21), cfg=None, glossary="default"):
        return ss.collect(cfg or self.cfg, lo, hi, self.glossary if glossary == "default" else glossary)

    def report(self, res, gstats, prev, cur, cfg=None):
        return ss.build_report(res, gstats, prev, cur, "x", cfg or self.cfg)


def week(start):
    return [start + timedelta(days=i) for i in range(7)]


class TestUserMessages(Base):
    def test_rephrase_counts_by_kind_and_day(self):
        w = self.writer("m.jsonl", name=MASTER)
        w.user("もう一度")
        w.user("again")
        w.user("AGAIN2 の表を直して")  # 英数字が続くので「again」の言い直しではない
        w.user("次は？")
        w.user("これを説明してください")
        w.user("ふつうの指示です")
        w.save()
        res, _ = self.collect()
        st = res[MASTER]
        d = date(2026, 3, 15)
        self.assertEqual(st.users[d], 6)
        self.assertEqual(st.rephrase["again"][d], 2)
        self.assertEqual(st.rephrase["next"][d], 1)
        self.assertEqual(st.rephrase["explain"][d], 1)
        self.assertEqual(ss.period_metrics(st, [d])["rephrase_total"], 4)

    def test_rephrase_table_comes_from_settings(self):
        cfg = settings(rephrase=REPHRASE + [["hello", "あいさつ", "こんにちは"]])
        self.assertEqual(ss.rephrase_kinds("もう一度、こんにちは", cfg), ["again", "hello"])
        self.assertEqual(ss.rephrase_kinds("もう一度", ss.make_settings({}, env={})), [])

    def test_queued_command_counts_as_user(self):
        w = self.writer("m.jsonl", name=MASTER)
        w.user("はい")
        w.queued("もう一度")
        w.queued("<task-notification>\n<task-id>x</task-id>")  # 通知は数えない
        w.raw(w._base("attachment", attachment={"type": "queued_command", "prompt": "/compact",
                                                "commandMode": "bash"}))  # prompt でない
        w.save()
        res, _ = self.collect()
        st = res[MASTER]
        d = date(2026, 3, 15)
        self.assertEqual(st.users[d], 2)
        self.assertEqual(st.queued[d], 1)
        self.assertEqual(st.rephrase["again"][d], 1)

    def test_cross_session_and_meta_are_excluded(self):
        w = self.writer("m.jsonl", name=MASTER)
        w.user('<cross-session-message from="x" from-name="b2">もう一度</cross-session-message>')
        w.queued('<cross-session-message from="x">次は？</cross-session-message>')
        w.user('  <agent-message from="a1">説明して</agent-message>')
        w.user("Another Claude session sent a message: もう一度", isMeta=True)
        w.user("もう一度", origin={"kind": "peer", "from": "uds:/x"})
        w.user("This session is being continued from a previous conversation. もう一度", isCompactSummary=True)
        w.tool_result()
        w.user("本物の発言")
        w.save()
        res, _ = self.collect()
        st = res[MASTER]
        self.assertEqual(st.users[date(2026, 3, 15)], 1)
        self.assertEqual(sum(sum(c.values()) for c in st.rephrase.values()), 0)

    def test_queued_from_peer_is_excluded(self):
        w = self.writer("m.jsonl", name=MASTER)
        w.raw(w._base("attachment", attachment={"type": "queued_command", "prompt": "もう一度",
                                                "commandMode": "prompt", "origin": {"kind": "peer"}}))
        w.queued("本物")
        w.save()
        res, _ = self.collect()
        self.assertEqual(sum(res[MASTER].users.values()), 1)

    def test_odd_content_shapes_are_skipped(self):
        w = self.writer("m.jsonl", name=MASTER)
        w.raw(w._base("user", message={"role": "user", "content": None}))
        w.raw(w._base("user", message=None))
        w.raw(w._base("user", message={"role": "user", "content": [{"type": "image"}, "x", None]}))
        w.raw(w._base("user", message={"role": "user", "content": [{"type": "text", "text": "画像つきの発言"}]}))
        w.raw(w._base("attachment", attachment={"type": "queued_command", "prompt": [{"text": "リストの発言"}]}))
        w.raw(w._base("attachment", attachment={"type": "queued_command", "prompt": 3}))
        w.raw(w._base("assistant", message={"role": "assistant", "content": None}))
        w.raw({"type": "user", "uuid": w.uuid(), "timestamp": "not a time", "message": {"content": "x"}})
        w.raw({"type": "user", "uuid": w.uuid(), "message": {"content": "時刻なし"}})
        w.save()
        res, _ = self.collect()
        self.assertEqual(sum(res[MASTER].users.values()), 2)


class TestReplies(Base):
    def test_english_judgement(self):
        self.assertTrue(ss.is_english_reply(EN_LONG))
        self.assertFalse(ss.is_english_reply(JA_LONG))
        self.assertFalse(ss.is_english_reply("Short English."))  # 150 字以下は長い返答でない
        mixed = "The task tools haven't been used recently. " + "日本語の本文がこのあとに続きます。" * 10
        self.assertFalse(ss.is_english_reply(mixed))
        self.assertEqual(ss.system_echo_kind(mixed, self.cfg), "task_tools")
        self.assertIsNone(ss.system_echo_kind(JA_LONG, self.cfg))
        self.assertIsNone(ss.system_echo_kind(mixed, settings(echo=[])))

    def test_english_counts_ratio_and_median(self):
        w = self.writer("m.jsonl", name=MASTER)
        w.reply(EN_LONG)
        w.user("もう一度")              # 直前の返答 = EN_LONG（末尾の空白を除いて 257 字）
        w.reply("短い返答")
        w.reply(JA_LONG)
        w.user("はい")                  # 直前の返答 = JA_LONG（240 字）
        w.reply("The task tools haven't been used recently. " + "本文です。" * 40)
        w.user("つぎ")                  # 直前の返答 = 43 + 200 字
        w.raw(w._base("assistant", message={"role": "assistant", "model": "<synthetic>",
                                             "content": [{"type": "text", "text": "No response requested."}]}))
        w.save()
        res, _ = self.collect()
        st = res[MASTER]
        m = ss.period_metrics(st, [date(2026, 3, 15)])
        self.assertEqual(m["replies"], 4)
        self.assertEqual(m["long_replies"], 3)
        self.assertEqual(m["english"], 1)
        self.assertAlmostEqual(m["english_pct"], 100.0 / 3)
        self.assertEqual(m["echo:task_tools"], 1)
        self.assertEqual(sorted(st.reply_len[date(2026, 3, 15)]), [240, 243, len(EN_LONG.strip())])
        self.assertEqual(m["reply_median"], 243)

    def test_glossary_hits_only_for_master(self):
        w = self.writer("m.jsonl", name=MASTER)
        w.reply("確かめます。あとで確かめます。")
        w.reply("「確かめ」は引用なので外す")
        w.save()
        r = self.writer("r.jsonl", sub="dev-a")
        r.reply("確かめます")
        r.save()
        res, _ = self.collect()
        self.assertEqual(res[MASTER].glossary[date(2026, 3, 15)], 2)
        self.assertIsNone(ss.period_metrics(res["dev-a"], [date(2026, 3, 15)])["glossary"])

    def test_no_glossary_shows_dash(self):
        w = self.writer("m.jsonl", name=MASTER)
        w.reply("確かめます")
        w.save()
        res, gstats = self.collect(glossary=None)
        self.assertIsNone(ss.period_metrics(res[MASTER], [date(2026, 3, 15)])["glossary"])
        md = self.report(res, gstats, week(date(2026, 3, 8)), week(date(2026, 3, 15)))
        self.assertIn("用語集が無いか読めなかった", md)

    def test_glossary_tool_errors(self):
        broken = os.path.join(self.root, "tool", "broken.py")
        Path(broken).write_text("raise RuntimeError('boom')\n")
        with self.assertRaises(RuntimeError):
            ss.Glossary.load(broken, self.words)
        self.assertNotIn("_claude_session_stats_glossary", sys.modules)  # 壊れたツールを残さない
        with self.assertRaises(OSError):
            ss.Glossary.load(self.tool, os.path.join(self.root, "missing.txt"))
        empty = os.path.join(self.root, "tool", "empty.txt")
        Path(empty).write_text("")
        with self.assertRaises(ValueError):
            ss.Glossary.load(self.tool, empty)


class TestSends(Base):
    def test_broadcast_same_body_three_sessions(self):
        w = self.writer("m.jsonl", name=MASTER)
        body = "全員へ、新しい決まりです。担当は実装しない。コードの変更はサブエージェントに任せる。"
        w.reply("", sends=[("dev-a", body), ("dev-b", body)])
        # 宛先の名前だけ違う本文も同じ知らせ
        w.reply("", sends=[("dev-c", body.replace("全員へ", "dev-c へ"))])
        w.minute += 30
        # 2人だけ: 知らせに数えない
        w.reply("", sends=[("dev-a", "別の件です。" * 10), ("dev-b", "別の件です。" * 10)])
        # サブエージェントへの送信は数えない（recipient_kind も、古い記録の宛先の形も）
        w.raw(w._base("assistant", message={"role": "assistant", "content": [
            {"type": "tool_use", "name": "SendMessage",
             "input": {"to": "a0123456789abcdef", "message": body, "recipient_kind": "agent"}},
            {"type": "tool_use", "name": "SendMessage", "input": {"to": "a0123456789abcdef", "message": body}}]}))
        w.minute += 30
        # 同じ本文でも、窓（5 分）を過ぎた3通目は別
        w.reply("", sends=[("x-a", "遅い知らせ" * 10)])
        w.minute += 3
        w.reply("", sends=[("x-b", "遅い知らせ" * 10)])
        w.minute += 3
        w.reply("", sends=[("x-c", "遅い知らせ" * 10)])
        w.save()
        res, _ = self.collect()
        st = res[MASTER]
        b = st.broadcasts()
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0][1], 3)
        m = ss.period_metrics(st, [date(2026, 3, 15)])
        self.assertEqual(m["sends"], 8)
        self.assertEqual(m["broadcasts"], 1)

    def test_same_recipient_twice_is_not_one_broadcast(self):
        w = self.writer("m.jsonl", name=MASTER)
        for d in ("D-1", "D-2"):
            body = f"共通の決まりを直しました（{d}）。読み直してください。" * 3
            w.reply("", sends=[("dev-a", body), ("dev-b", body), ("dev-c", body)])
        w.save()
        res, _ = self.collect()
        self.assertEqual(len(res[MASTER].broadcasts()), 2)

    def test_broadcast_personalized_bodies_of_different_length(self):
        w = self.writer("m.jsonl", name=MASTER)
        base = MASTER + " から {to} へ。担当のフォルダへの移行の下書きのお願いです。依頼の全文はファイルにあります。"
        extra = {"dev-env": "追加: hook の件も見てください。" * 4, "dev-c": "追加: 撮影の件も。" * 5}
        sends = [(to, base.format(to=to) + extra.get(to, "")) for to in ["dev-a", "dev-b", "x-d", "dev-c", "dev-env"]]
        w.reply("", sends=sends)
        w.save()
        res, _ = self.collect()
        self.assertEqual([x[1] for x in res[MASTER].broadcasts()], [5])

    def test_empty_body_and_blank_recipient(self):
        w = self.writer("m.jsonl", name=MASTER)
        w.reply("", sends=[("dev-a", ""), ("dev-b", ""), ("dev-c", ""), ("", "宛先なし")])
        w.raw(w._base("assistant", message={"role": "assistant", "content": [
            {"type": "tool_use", "name": "SendMessage", "input": {"to": "   ", "message": {"x": 1}}}]}))
        w.save()
        res, _ = self.collect()
        st = res[MASTER]
        self.assertEqual(len(st.sends), 4)  # 宛先が空の送信は数えない。空白だけの宛先は数える
        self.assertEqual(st.broadcasts(), [])


class TestFiles(Base):
    def test_empty_file_and_broken_lines(self):
        e = self.writer("empty.jsonl", sub="dev-b")
        e.save()
        w = self.writer("m.jsonl", name=MASTER)
        w.bad()
        w.user("もう一度")
        w.bad("not json at all")
        w.bad("")
        w.bad("[1, 2]")  # JSON だが dict でない
        w.reply(JA_LONG)
        w.save()
        res, gstats = self.collect()
        self.assertEqual(gstats["bad_lines"], 2)
        self.assertEqual(res[MASTER].users[date(2026, 3, 15)], 1)
        self.assertIn("dev-b", res)
        self.assertEqual(sum(res["dev-b"].users.values()), 0)
        md = self.report(res, gstats, week(date(2026, 3, 8)), week(date(2026, 3, 15)))
        self.assertIn("読めなかった行: 2 行", md)

    def test_no_files_at_all(self):
        res, gstats = self.collect()
        self.assertEqual(res, {})
        md = self.report(res, gstats, [date(2026, 3, 8)], [date(2026, 3, 15)])
        self.assertIn("比べられない", md)
        self.assertIn("- なし", md)

    def test_missing_projects_folder(self):
        res, _ = self.collect(cfg=settings(projects=os.path.join(self.root, "nope")))
        self.assertEqual(res, {})

    def test_predecessor_merged_and_deduped_others_ignored(self):
        # 前身（名前なし）: uuid の1つが統括に写っている → 統括として足す。重なった行は1回だけ
        p = self.writer("pre.jsonl", session_id="pre")
        p.user("前身での発言")
        shared = p.uuid()
        p.user_at(p.ts(), "重なった発言", uuid=shared)
        p.save()
        m = self.writer("m.jsonl", session_id="main", name=MASTER, start_min=10)
        m.user_at(m.ts(), "重なった発言", uuid=shared)
        m.user("統括での発言")
        m.save()
        o = self.writer("other.jsonl", session_id="other")
        o.user("関係ない会話")
        o.save()
        n = self.writer("named.jsonl", session_id="named", name="visitor")
        n.user("名前が担当でない")
        n.save()
        env = self.writer("env.jsonl", session_id="env", name="dev-env")
        env.user("env での発言")
        env.save()
        sub = self.writer("x.jsonl", sub="dev-c")
        sub.user("dev-c での発言")
        sub.save()
        res, _ = self.collect()
        self.assertEqual(sorted(res), ["dev-c", "dev-env", MASTER])
        self.assertEqual(list(res)[0], MASTER)  # 統括が先頭
        self.assertEqual(sum(res[MASTER].users.values()), 3)
        self.assertEqual(sum(res["dev-env"].users.values()), 1)

    def test_predecessor_chain(self):
        a = self.writer("a_first.jsonl", session_id="aaaa")
        a.user("いちばん古い発言")
        ab = a.uuid()
        a.user_at(a.ts(), "A と B に写った発言", uuid=ab)
        a.save()
        b = self.writer("b_second.jsonl", session_id="bbbb", start_min=10)
        b.user_at(b.ts(), "A と B に写った発言", uuid=ab)
        bm = b.uuid()
        b.user_at(b.ts(), "B と統括に写った発言", uuid=bm)
        b.save()
        m = self.writer("z_main.jsonl", session_id="cccc", name=MASTER, start_min=20)
        m.user_at(m.ts(), "B と統括に写った発言", uuid=bm)
        m.save()
        res, _ = self.collect()
        self.assertEqual(sum(res[MASTER].users.values()), 3)

    def test_cyclic_uuid_sharing_terminates(self):
        # 名前の無い 2 つが互いに uuid を共有し、どの名前つきとも重ならない: 足さずに終わる
        a = self.writer("a.jsonl", session_id="aaaa")
        b = self.writer("b.jsonl", session_id="bbbb")
        u1, u2 = a.uuid(), b.uuid()
        for w in (a, b):
            w.user_at(w.ts(), "共有1", uuid=u1)
            w.user_at(w.ts(), "共有2", uuid=u2)
            w.save()
        m = self.writer("m.jsonl", session_id="cccc", name=MASTER)
        m.user("統括")
        m.save()
        res, _ = self.collect()
        self.assertEqual(sum(res[MASTER].users.values()), 1)

    def test_out_of_range_not_counted_and_sidechain_skipped(self):
        w = self.writer("m.jsonl", name=MASTER, day="2026-03-01")
        w.user("もう一度")
        w.save()
        w2 = self.writer("m2.jsonl", session_id="m2", name=MASTER)
        w2.user("サブエージェントの中", isSidechain=True)
        w2.user("範囲の中")
        w2.save()
        res, _ = self.collect()
        st = res[MASTER]
        self.assertEqual(sum(st.users.values()), 1)
        self.assertEqual(sum(st.rephrase["again"].values()), 0)

    def test_old_files_are_not_read(self):
        w = self.writer("m.jsonl", name=MASTER)
        w.user("古いファイルの発言")
        w.save()
        old = datetime(2026, 1, 1).timestamp()
        os.utime(w.path, (old, old))
        res, _ = self.collect()
        self.assertEqual(res, {})

    def test_jst_day_boundary_and_range_ends(self):
        w = self.writer("m.jsonl", name=MASTER)
        for ts, text in [("2026-03-13T14:59:59.000Z", "lo の前の日"),      # JST 03-13 23:59
                         ("2026-03-13T15:00:00.000Z", "lo の日"),          # JST 03-14 00:00
                         ("2026-03-20T14:59:59.000Z", "hi の前の日"),      # JST 03-20 23:59
                         ("2026-03-20T15:00:00.000Z", "hi の日")]:         # JST 03-21 00:00
            w.user_at(ts, text)
        w.save()
        res, _ = self.collect(lo=date(2026, 3, 14), hi=date(2026, 3, 21))
        self.assertEqual(dict(res[MASTER].users), {date(2026, 3, 14): 1, date(2026, 3, 20): 1})


class TestTimezone(unittest.TestCase):
    """日付の区切り: 既定はこのマシンのローカルの時刻、session_stats.timezone で固定できる。"""

    def test_default_is_local_and_setting_fixes_zone(self):
        cfg = ss.make_settings({}, env={})
        self.assertEqual((cfg["timezone"], cfg["tz"]), ("", None))
        dt = datetime(2026, 3, 13, 15, 30, tzinfo=timezone.utc)
        self.assertEqual(ss.local_day(dt, None), dt.astimezone().date())
        tokyo = ss.make_settings({"timezone": "Asia/Tokyo"}, env={})
        self.assertEqual(ss.local_day(dt, tokyo["tz"]), date(2026, 3, 14))
        utc = ss.make_settings({"timezone": "UTC"}, env={})
        self.assertEqual(ss.local_day(dt, utc["tz"]), date(2026, 3, 13))
        with self.assertRaises(ValueError):
            ss.make_settings({"timezone": "Mars/Base"}, env={})
        with self.assertRaises(ValueError):
            ss.make_settings({"timezone": 9}, env={})


class TestDiscovery(Base):
    def test_no_roles_means_every_named_session(self):
        cfg = settings(projects=self.root, master="", roles=[], role_dir_prefix="")
        for sid, name in (("s1", "alpha"), ("s2", "beta")):
            w = self.writer(f"{sid}.jsonl", session_id=sid, name=name)
            w.user("発言")
            w.save()
        renamed = self.writer("s3.jsonl", session_id="s3", name="old")
        renamed.raw({"type": "custom-title", "customTitle": "gamma", "sessionId": "s3"})
        renamed.user("名前を変えた")
        renamed.save()
        sub = self.writer("x.jsonl", sub="dev-a")  # role_dir_prefix が空なら、別のフォルダは見ない
        sub.user("外")
        sub.save()
        res, gstats = self.collect(cfg=cfg)
        self.assertEqual(sorted(res), ["alpha", "beta", "gamma"])
        self.assertFalse(any(st.is_master for st in res.values()))
        md = self.report(res, gstats, week(date(2026, 3, 8)), week(date(2026, 3, 15)), cfg)
        self.assertNotIn("- 統括:", md)
        self.assertIn("名前のある記録（その名前で数える）", md)

    def test_master_not_counted_as_role_when_roles_empty(self):
        cfg = settings(projects=self.root, roles=[])
        w = self.writer("m.jsonl", name=MASTER)
        w.raw({"type": "custom-title", "customTitle": "other", "sessionId": "s1"})
        w.user("統括")
        w.save()
        res, _ = self.collect(cfg=cfg)
        self.assertEqual(list(res), [MASTER])

    def test_project_name_with_glob_characters(self):
        cfg = settings(projects=self.root, home_project="-home-[x]")
        w = Writer(os.path.join(self.root, "-home-[x]", "m.jsonl"), name=MASTER)
        w.user("発言")
        w.save()
        r = Writer(os.path.join(self.root, "-home-[x]" + PREFIX + "dev-a", "r.jsonl"))
        r.user("担当")
        r.save()
        res, _ = self.collect(cfg=cfg)
        self.assertEqual(sorted(res), ["dev-a", MASTER])


class TestReport(Base):
    def test_redact(self):
        self.assertEqual(ss.redact("to tester-a1 and tester", ["tester"]), "to a1 and <user>")
        self.assertEqual(ss.redact("a.b@c.io", []), "<email>")
        self.assertEqual(ss.redact("ann annotation ann-a1 Ann", ["ann"]), "<user> annotation a1 <user>")
        self.assertEqual(ss.redact("x", ["", None]), "x")

    def test_increase_section_and_new_examples(self):
        w = self.writer("m.jsonl", name=MASTER, day="2026-03-10")
        w.user("もう一度")
        w.reply(JA_LONG)
        w.save()
        w2 = self.writer("m2.jsonl", session_id="m2", name=MASTER, day="2026-03-17")
        w2.user("もう一度")           # 前の期間にもあった文 → 新しい例に出さない
        w2.user("もう一度お願いします")
        w2.user("もう一度書いて")
        w2.reply(EN_LONG)
        w2.save()
        res, gstats = self.collect(lo=date(2026, 3, 7), hi=date(2026, 3, 21))
        md = self.report(res, gstats, week(date(2026, 3, 7)), week(date(2026, 3, 14)))
        self.assertIn(f"{MASTER}: 言い直し: 「もう一度」 1 → 3（+2）", md)
        self.assertIn("英語の返答 0 → 1（+1）（前の期間は 0）", md)
        ex = md.split("## 4.")[1].split("## 5.")[0]
        self.assertIn("「もう一度お願いします」", ex)
        self.assertNotIn(":「もう一度」\n", ex)
        self.assertIn(f"| {MASTER} | 03-17 |", md)

    def test_every_kind_has_a_report_row(self):
        keys = {k for k, _l in ss.metric_rows(self.cfg)}
        for k, _l, _p in self.cfg["rephrase"]:
            self.assertIn("re:" + k, keys)
        for k, _l, _p in self.cfg["echo"]:
            self.assertIn("echo:" + k, keys)
        self.assertTrue(set(ss.watch_keys(self.cfg)) <= keys)

    def test_notes_go_to_method_section(self):
        cfg = settings(projects=self.root, notes=["手元の文書に合わせた。"])
        md = self.report({}, {}, [date(2026, 3, 8)], [date(2026, 3, 15)], cfg)
        self.assertIn("## 6. 数え方\n\n手元の文書に合わせた。\n", md)

    def test_progress_does_not_take_proposals(self):
        self.assertIn("progress", ss.rephrase_kinds("どうですか", self.cfg))
        self.assertIn("progress", ss.rephrase_kinds("進捗どうですか？", self.cfg))
        self.assertIn("progress", ss.rephrase_kinds("テストはまだですか", self.cfg))
        self.assertNotIn("progress", ss.rephrase_kinds("アイコンをつけるのはどうですか", self.cfg))
        self.assertNotIn("progress", ss.rephrase_kinds("テストはまだやる暇がない", self.cfg))


class TestSettings(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="session_stats_cfg_")
        self.path = Path(self._tmp.name) / "config.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_defaults_are_generic(self):
        cfg = ss.load_settings({"XDG_DATA_HOME": "/data"}, self.path)  # ファイルが無い
        self.assertEqual((cfg["master"], cfg["roles"], cfg["rephrase"], cfg["redact"], cfg["glossary"]),
                         ("", [], [], [], None))
        self.assertEqual(cfg["out_dir"], "/data/claude-kitty/stats")
        self.assertEqual([k for k, _l, _p in cfg["echo"]], ["task_tools"])
        self.assertTrue(cfg["projects"].endswith("/.claude/projects"))

    def test_reads_only_its_section(self):
        self.path.write_text(json.dumps({"view": {"left": "x"}, "session_stats": {
            "master": "m", "roles": "r", "rephrase": [["k", "K", "k+"]], "redact": ["n"],
            "glossary": {"module": "~/t.py", "file": "~/g.md"}, "out_dir": "~/o"}}))
        before = self.path.read_text()
        cfg = ss.load_settings({}, self.path)
        self.assertEqual(self.path.read_text(), before)  # 読むだけで書かない
        self.assertEqual(cfg["roles"], ["r"])
        self.assertEqual(cfg["glossary"]["scene"], "reply")
        self.assertFalse(cfg["glossary"]["module"].startswith("~"))
        self.assertFalse(cfg["out_dir"].startswith("~"))

    def test_env_selects_config_file(self):
        self.path.write_text(json.dumps({"session_stats": {"master": "via-env"}}))
        self.assertEqual(ss.load_settings({"CLAUDE_KITTY_CONFIG": str(self.path)})["master"], "via-env")

    def test_broken_settings_raise(self):
        bad = [
            "not json",
            json.dumps([1]),
            json.dumps({"session_stats": []}),
            json.dumps({"session_stats": {"unknown": 1}}),
            json.dumps({"session_stats": {"master": 3}}),
            json.dumps({"session_stats": {"roles": [""]}}),
            json.dumps({"session_stats": {"rephrase": [["k", "K"]]}}),
            json.dumps({"session_stats": {"rephrase": [["k", "K", "("]]}}),
            json.dumps({"session_stats": {"rephrase": [["k", "K", "a"], ["k", "L", "b"]]}}),
            json.dumps({"session_stats": {"echo": None}}),
            json.dumps({"session_stats": {"glossary": {"module": "x"}}}),
            json.dumps({"session_stats": {"glossary": "x"}}),
            json.dumps({"session_stats": {"notes": [None]}}),
        ]
        for text in bad:
            self.path.write_text(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                ss.load_settings({}, self.path)


class TestCli(Base):
    def run_cli(self, args, env=None, now=datetime(2026, 3, 21, 7, 30, tzinfo=ss.JST)):
        conf = os.path.join(self.root, "config.json")
        if not os.path.exists(conf):
            Path(conf).write_text(json.dumps({"session_stats": {
                "master": MASTER, "roles": ROLES, "role_dir_prefix": PREFIX, "rephrase": REPHRASE,
                "redact": ["tester"], "home_project": HOME_PROJ, "projects": self.root, "timezone": "Asia/Tokyo",
                "glossary": {"module": self.tool, "file": self.words}}}))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(args, env=dict(env or {}, CLAUDE_KITTY_CONFIG=conf), now=now)
        return rc, out.getvalue(), err.getvalue()

    def test_writes_file_with_end_placeholder_and_redacts(self):
        w = self.writer("m.jsonl", name=MASTER, day="2026-03-19")
        w.user("もう一度。連絡は someone.name@example.com の tester さんへ")
        body = "全員へのお知らせです。" * 5
        w.reply(JA_LONG + "確かめます", sends=[("tester-a1", body), ("tester-b2", body), ("tester-c3", body)])
        w.save()
        rc, out, err = self.run_cli(["--end", "yesterday", "--out", os.path.join(self.root, "o", "s_{end}.md")])
        self.assertEqual((rc, err), (0, ""))
        path = os.path.join(self.root, "o", "s_2026-03-20.md")
        self.assertEqual(out.strip(), path)
        md = Path(path).read_text()
        self.assertNotIn("@example.com", md)
        self.assertNotIn("tester", md)
        self.assertIn("<email>", md)
        self.assertIn("2026-03-14〜2026-03-20", md)
        self.assertNotIn("作成の時刻までの分だけ", md)  # 昨日までなので、途中の日は無い
        self.assertIn(f"| {MASTER} |", md)
        self.assertIn("全員への同じ知らせ（3 人以上） | 0 | 1 |", md)
        self.assertIn("用語集の検査の件数（統括の返答、場面 reply） | 0 | 1 |", md)
        self.assertEqual(os.listdir(os.path.join(self.root, "o")), ["s_2026-03-20.md"])  # .tmp を残さない

    def test_out_dir_default_from_settings_and_today_note(self):
        rc, out, _ = self.run_cli(["--out-dir", os.path.join(self.root, "d")])
        self.assertEqual(rc, 0)
        self.assertTrue(out.strip().endswith("/d/session_stats_2026-03-21.md"))
        self.assertIn("作成の時刻までの分だけ", Path(out.strip()).read_text())
        rc, out, _ = self.run_cli([], env={"XDG_DATA_HOME": os.path.join(self.root, "xdg")})
        self.assertEqual(out.strip(), os.path.join(self.root, "xdg", "claude-kitty", "stats", "session_stats_2026-03-21.md"))

    def test_period_arguments(self):
        def line(args):
            rc, out, _ = self.run_cli(args + ["--stdout"])
            self.assertEqual(rc, 0)
            return out.split("\n")[3]
        self.assertIn("直近の期間 = 2026-03-18〜2026-03-20（3 日）、前の期間 = 2026-03-15〜2026-03-17（3 日）",
                      line(["--end", "2026-03-20", "--days", "3"]))
        self.assertIn("直近の期間 = 2026-03-10〜2026-03-20（11 日）、前の期間 = 2026-02-27〜2026-03-09（11 日）",
                      line(["--start", "2026-03-10", "--end", "2026-03-20"]))

    def test_bad_arguments_return_2(self):
        self.assertEqual(self.run_cli(["--end", "2026-13-01", "--stdout"])[0], 2)
        self.assertEqual(self.run_cli(["--start", "2026-03-22", "--end", "2026-03-20", "--stdout"])[0], 2)
        self.assertEqual(self.run_cli(["--days", "0", "--stdout"])[0], 2)
        Path(self.root, "config.json").write_text('{"session_stats": {"roles": 1}}')
        rc, _, err = self.run_cli(["--stdout"])
        self.assertEqual(rc, 2)
        self.assertIn("roles", err)

    def test_unreadable_glossary_still_reports(self):
        rc, out, err = self.run_cli(["--stdout", "--glossary", os.path.join(self.root, "missing.md")])
        self.assertEqual(rc, 0)
        self.assertIn("用語集を読めない", err)
        self.assertIn("用語集が無いか読めなかった", out)
        rc, out, err = self.run_cli(["--stdout", "--no-glossary"])
        self.assertEqual((rc, err), (0, ""))

    def test_unwritable_output_returns_1(self):
        blocker = os.path.join(self.root, "file")
        Path(blocker).write_text("")
        rc, _, err = self.run_cli(["--out", os.path.join(blocker, "x.md")])
        self.assertEqual(rc, 1)
        self.assertIn("書けない", err)

    def test_runs_as_a_command(self):
        import subprocess
        conf = os.path.join(self.root, "c.json")
        Path(conf).write_text(json.dumps({"session_stats": {"timezone": "Asia/Tokyo", "projects": self.root, "home_project": HOME_PROJ}}))
        p = subprocess.run([sys.executable, str(BIN), "--end", "2026-03-20", "--stdout"], capture_output=True, text=True,
                           env=dict(os.environ, CLAUDE_KITTY_CONFIG=conf))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.startswith("# セッションの数字 2026-03-20"))


class TestReviewFixes(TestCli):
    def test_last_sunday(self):
        for today, want in [(date(2026, 3, 23), date(2026, 3, 22)),   # 月 → 前の日
                            (date(2026, 3, 24), date(2026, 3, 22)),   # 火（逃して後で走った）→ 同じ週
                            (date(2026, 3, 22), date(2026, 3, 15))]:  # 日 → 今日は途中なので前の日曜
            self.assertEqual(cli._day("last-sunday", today), want)
        rc, out, _ = self.run_cli(["--end", "last-sunday", "--out", os.path.join(self.root, "w_{end}.md")],
                                  now=datetime(2026, 3, 24, 9, 0, tzinfo=ss.JST))
        self.assertEqual((rc, out.strip()), (0, os.path.join(self.root, "w_2026-03-22.md")))

    def test_out_dir_and_projects_from_settings_and_overrides(self):
        other = os.path.join(self.root, "elsewhere")
        Writer(os.path.join(other, "-p", "m.jsonl"), name=MASTER).save()
        w = Writer(os.path.join(other, "-p", "n.jsonl"), session_id="n", name="dev-a", day="2026-03-20")
        w.user("別の場所の発言")
        w.save()
        Path(self.root, "config.json").write_text(json.dumps({"session_stats": {"timezone": "Asia/Tokyo",
            "master": MASTER, "roles": ROLES, "projects": self.root, "home_project": HOME_PROJ,
            "out_dir": os.path.join(self.root, "from-config")}}))
        rc, out, _ = self.run_cli(["--projects", other, "--home-project=-p", "--end", "2026-03-20"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), os.path.join(self.root, "from-config", "session_stats_2026-03-20.md"))
        self.assertIn("| dev-a | 03-20 | 1 |", Path(out.strip()).read_text())

    def test_cli_default_is_local_time_with_dst(self):
        # timezone を書かなければ、このマシンのローカルの時刻（TZ）で日を区切る。夏時間の切り替わりも含める
        w = Writer(os.path.join(self.home, "d.jsonl"), session_id="d", name="dev-a")
        for ts in ("2026-03-08T03:00:00.000Z",    # NY 03-07 22:00 / UTC 03-08
                   "2026-03-08T04:59:00.000Z",    # NY 03-07 23:59
                   "2026-03-08T05:00:00.000Z",    # NY 03-08 00:00（EST）
                   "2026-03-09T03:30:00.000Z",    # NY 03-08 23:30（EDT）/ UTC 03-09
                   "2026-03-09T04:30:00.000Z"):   # NY 03-09 00:30（EDT。EST のままなら 03-08 と数え違える）
            w.user_at(ts, "発言")
        w.save()
        conf = Path(self.root, "local.json")

        def run(tz, setting=None):
            sec = {"master": MASTER, "roles": ROLES, "projects": self.root, "home_project": HOME_PROJ}
            if setting:
                sec["timezone"] = setting
            conf.write_text(json.dumps({"session_stats": sec}))
            env = {"PATH": "/usr/bin:/bin", "HOME": self.root, "TZ": tz, "CLAUDE_KITTY_CONFIG": str(conf)}
            r = subprocess.run([sys.executable, str(ROOT / "bin" / "claude-session-stats"), "--end", "2026-03-08",
                                "--days", "1", "--stdout", "--no-glossary"], env=env, capture_output=True, text=True,
                               timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout
        ny = run("America/New_York")
        self.assertIn("| dev-a | 03-08 | 2 |", ny)
        self.assertIn("このマシンのローカルの時刻 E", ny)          # EST / EDT とずれを添える
        self.assertIn("| dev-a | 03-08 | 3 |", run("UTC"))
        self.assertIn("| dev-a | 03-08 | 3 |", run("America/New_York", "UTC"))   # 設定がローカルより優先
        self.assertIn("日付はすべて UTC", run("America/New_York", "UTC"))

    def test_glossary_flag_without_tool_warns(self):
        Path(self.root, "config.json").write_text(json.dumps({"session_stats": {"timezone": "Asia/Tokyo",
            "master": MASTER, "projects": self.root, "home_project": HOME_PROJ}}))
        rc, _, err = self.run_cli(["--stdout", "--glossary", self.words])
        self.assertEqual(rc, 0)
        self.assertIn("glossary.module", err)

    def test_no_master_does_not_run_glossary_tool(self):
        marker = os.path.join(self.root, "ran")
        tool = os.path.join(self.root, "tool", "marking.py")
        Path(tool).write_text(f"open({marker!r}, 'w').close()\n" + FAKE_GLOSSARY_TOOL)
        Path(self.root, "config.json").write_text(json.dumps({"session_stats": {"timezone": "Asia/Tokyo",
            "projects": self.root, "home_project": HOME_PROJ, "glossary": {"module": tool, "file": self.words}}}))
        rc, out, _ = self.run_cli(["--stdout"])
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(marker))
        self.assertIn("用語集が無いか読めなかった", out)

    def test_glossary_tool_raising_per_reply(self):
        tool = os.path.join(self.root, "tool", "raising.py")
        Path(tool).write_text(FAKE_GLOSSARY_TOOL + "\n_c = [0]\n_orig = check_text\n"
                              "def check_text(text, rules, scene='all'):\n"
                              "    if 'こわす' in text:\n        raise KeyError('x')\n"
                              "    return _orig(text, rules, scene)\n")
        Path(self.root, "config.json").write_text(json.dumps({"session_stats": {"timezone": "Asia/Tokyo",
            "master": MASTER, "projects": self.root, "home_project": HOME_PROJ,
            "glossary": {"module": tool, "file": self.words}}}))
        w = self.writer("m.jsonl", name=MASTER, day="2026-03-20")
        w.reply("こわす")
        w.reply("確かめます")
        w.save()
        rc, out, err = self.run_cli(["--stdout", "--end", "2026-03-20"])
        self.assertEqual(rc, 0)
        self.assertIn("1 件の返答で例外", err)
        self.assertIn("用語集のツールが 1 件の返答で例外を出した", out)
        self.assertIn("用語集の検査の件数（統括の返答、場面 reply） | 0 | 1 |", out)

    def test_missing_explicit_config_returns_2(self):
        rc, _, err = self.run_cli(["--stdout", "--config", os.path.join(self.root, "typo.json")])
        self.assertEqual(rc, 2)
        self.assertIn("設定ファイルが無い", err)

    def test_failed_write_leaves_no_tmp(self):
        target = os.path.join(self.root, "outdir", "x.md")
        os.makedirs(target)  # 置き換える先がフォルダなので os.replace が失敗する
        rc, _, err = self.run_cli(["--out", target])
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(target + ".tmp"))


class TestPublicDefaults(unittest.TestCase):
    """公開する既定値に、利用者ごとの値（役割名・言い直しの言葉・伏せる語・ツールの場所）が入っていないこと。"""

    def test_defaults_are_empty_or_generic(self):
        d = ss.DEFAULTS
        self.assertEqual((d["master"], d["roles"], d["role_dir_prefix"], d["rephrase"], d["redact"],
                          d["glossary"], d["notes"]), ("", [], "", [], [], None, []))

    def test_sources_have_no_home_paths(self):
        for p in (ROOT / "lib" / "sessionstats.py", BIN):
            text = p.read_text()
            self.assertIsNone(re.search(r"/(?:var/)?home/[a-z]", text), p)


if __name__ == "__main__":
    unittest.main()
