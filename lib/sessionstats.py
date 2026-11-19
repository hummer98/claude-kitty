"""Claude Code の会話の記録（~/.claude/projects の jsonl）から、期間ごとの数字を出す（claude-session-stats の中身）。

統括（設定の master）と担当（roles と、role_dir_prefix のフォルダ）について、日ごと・セッションごとに数え、
直近の期間とその前の期間を比べた Markdown を作る。標準ライブラリだけで動く。

記録のファイルは大きい（100MB を超える）ので、行ごとに読み、日ごとの集計と小さな値だけを持つ。
書き出す前に、メールアドレスと、設定の redact・実行時のユーザー名・ホームのフォルダ名を伏せる。

利用者ごとの値（役割名・言い直しの言葉・伏せる語・用語集の道具）はリポジトリに置かず、
設定ファイル（lib/config.py の config_path）の "session_stats" の節から読む。書き方は docs/session-stats.md。
"""
import difflib
import glob
import importlib.util
import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import config_path

# ---- 既定値（設定の "session_stats" で変える） -------------------------------

# 返答の冒頭に写ってしまう英語のシステム文。(key, 表に出す名前, 正規表現)。返答の先頭に re.match で当てる。
# Claude Code そのものの文なので、既定に入れておく
DEFAULT_ECHO = [
    ("task_tools", "\"The task tools haven't been used recently…\"", r"\s*The task tools haven't been used recently"),
]

DEFAULTS = {
    "master": "",            # 統括のセッション名（agentName / customTitle）。空なら統括を分けない
    "roles": [],             # 担当のセッション名。空なら、名前のある記録を全部、その名前で数える
    "role_dir_prefix": "",   # "<home_project><これ>*" のフォルダを担当とみなす（名前はフォルダの残り）。空なら使わない
    "rephrase": [],          # ユーザーの言い直し: [key, 表に出す名前, 正規表現]。発言の全文に re.search（re.M）
    "echo": [list(x) for x in DEFAULT_ECHO],
    "redact": [],            # 書き出しで伏せる語（実行時のユーザー名とホームのフォルダ名は必ず足す）
    "glossary": None,        # {"module": 道具の .py, "file": 用語集, "scene": "reply"}。統括の返答にかける
    "projects": "~/.claude/projects",
    "home_project": "",      # 統括の記録があるフォルダの名前。空ならホームのパスから作る
    "out_dir": "",           # 空なら $XDG_DATA_HOME/claude-kitty/stats
    "notes": [],             # 「数え方」の頭に足す文（手元の文書への言及など）
}

# 英語の返答: 長さが EN_MIN_LEN を超え、日本語の字（かな・カナ・漢字）の割合が EN_MAX_JA_RATIO 未満
EN_MIN_LEN = 150
EN_MAX_JA_RATIO = 0.1
JA_CHAR_RE = re.compile(r"[ぁ-んァ-ン一-龥]")

# 全員への同じ知らせ: 同じ本文を BROADCAST_WINDOW_SEC 秒の内に BROADCAST_MIN_RECIPIENTS 以上のセッションへ
BROADCAST_WINDOW_SEC = 300
BROADCAST_MIN_RECIPIENTS = 3
# 本文の似かた（difflib の ratio）がこれ以上なら同じ本文とみなす。先頭のこの字数で比べる
BROADCAST_SIMILARITY = 0.8
BROADCAST_COMPARE_CHARS = 200

# ユーザーの発言に数えない、先頭の印（他のセッション・サブエージェント・システムの文）
NOT_USER_PREFIXES = (
    "<cross-session-message", "<agent-message", "<task-notification", "<system-reminder",
    "<local-command", "<command-", "<bash-", "<user-prompt-submit", "<artifact-content",
    "[Request interrupted", "This session is being continued", "[Image:", "Caveat:",
    "Output token limit",
)

JST = timezone(timedelta(hours=9))
SUBAGENT_ID_RE = re.compile(r"a[0-9a-f]{12,}")
UUID_RE = re.compile(r'"uuid":"([0-9a-f-]{36})"')
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
EXAMPLE_MAX = 3
EXAMPLE_CHARS = 120

# 型を先頭で判定して、json を読まずに飛ばす行（api-request-blob などは数 MB になる）
_WANTED_TYPES = {"user", "assistant", "attachment"}


# ---- 設定 ------------------------------------------------------------------

def _kinds(value, where):
    """[[key, label, regex]] を [(key, label, compiled)] にする。形が違えば ValueError。"""
    if not isinstance(value, list):
        raise ValueError(f"{where} は [key, 表の名前, 正規表現] のリスト: {value!r}")
    out, keys = [], set()
    for item in value:
        if (not isinstance(item, (list, tuple)) or len(item) != 3
                or not all(isinstance(x, str) and x for x in item)):
            raise ValueError(f"{where} の項目は [key, 表の名前, 正規表現]: {item!r}")
        k, label, pat = item
        if k in keys:
            raise ValueError(f"{where} の key が重なっている: {k!r}")
        keys.add(k)
        try:
            out.append((k, label, pat))
            re.compile(pat)
        except re.error as e:
            raise ValueError(f"{where} の {k} の正規表現が正しくない: {e}")
    return out


def _names(value, where):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
        raise ValueError(f"{where} は文字列のリスト: {value!r}")
    return list(value)


def make_settings(section=None, env=None):
    """設定の節（dict）から、数えるのに使う値をそろえる。形が違えば ValueError。"""
    env = os.environ if env is None else env
    sec = dict(DEFAULTS)
    if section:
        unknown = set(section) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"session_stats に知らない項目がある: {sorted(unknown)}")
        sec.update(section)
    for key in ("master", "role_dir_prefix", "projects", "home_project", "out_dir"):
        if not isinstance(sec[key], str):
            raise ValueError(f"session_stats.{key} は文字列: {sec[key]!r}")
    s = {
        "master": sec["master"],
        "roles": _names(sec["roles"], "session_stats.roles"),
        "role_dir_prefix": sec["role_dir_prefix"],
        "rephrase": _kinds(sec["rephrase"], "session_stats.rephrase"),
        "echo": _kinds(sec["echo"], "session_stats.echo"),
        "redact": _names(sec["redact"], "session_stats.redact"),
        "notes": _names(sec["notes"], "session_stats.notes"),
        "projects": os.path.expanduser(sec["projects"]),
        "home_project": sec["home_project"] or default_home_project(),
        "out_dir": os.path.expanduser(sec["out_dir"]) if sec["out_dir"] else default_out_dir(env),
        "glossary": None,
    }
    g = sec["glossary"]
    if g is not None:
        if (not isinstance(g, dict) or set(g) - {"module", "file", "scene"}
                or not all(isinstance(g.get(k), str) and g.get(k) for k in ("module", "file"))
                or not isinstance(g.get("scene", "reply"), str)):
            raise ValueError(f"session_stats.glossary は {{\"module\", \"file\", \"scene\"}}: {g!r}")
        s["glossary"] = {"module": os.path.expanduser(g["module"]), "file": os.path.expanduser(g["file"]),
                         "scene": g.get("scene", "reply")}
    s["_rephrase_res"] = [(k, label, re.compile(p, re.M)) for k, label, p in s["rephrase"]]
    s["_echo_res"] = [(k, label, re.compile(p)) for k, label, p in s["echo"]]
    return s


def load_settings(env, path=None, required=False):
    """設定ファイルの "session_stats" の節を読む。ファイルが無ければ既定値（required なら ValueError）。
    壊れていれば ValueError。"""
    path = Path(path) if path else config_path(env)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        if required:  # 引数で名指ししたファイルが無いのは打ち間違いなので、黙って既定に戻さない
            raise ValueError(f"設定ファイルが無い: {path}")
        data = {}
    except (OSError, ValueError) as e:
        raise ValueError(f"設定ファイルを読めない: {path}: {e}")
    sec = data.get("session_stats", {}) if isinstance(data, dict) else None
    if not isinstance(sec, dict):
        raise ValueError(f"設定ファイルの session_stats がオブジェクトでない: {path}")
    return make_settings(sec, env)


def default_home_project():
    home = os.path.realpath(os.path.expanduser("~"))
    return home.replace("/", "-")


def default_out_dir(env):
    base = Path(env["XDG_DATA_HOME"]) if env.get("XDG_DATA_HOME") else Path.home() / ".local" / "share"
    return str(base / "claude-kitty" / "stats")


class Glossary:
    """用語集の道具（設定の glossary.module）を包む。道具は parse_glossary(text) と
    check_text(text, rules, scene=...) を持つこと。読めなければ load が例外を投げる。"""

    def __init__(self, module, rules, scene):
        self.module, self.rules, self.scene = module, rules, scene
        self.errors = 0
        self.last_error = None

    def __len__(self):
        return len(self.rules)

    def count(self, text):
        """1 つの返答の件数。道具の中で例外が出たら 0 と数え、数を errors に残す（残りの数字は出すため）。"""
        try:
            return len(self.module.check_text(text, self.rules, scene=self.scene))
        except Exception as e:  # noqa: BLE001  差し替えられる道具なので、何が出ても止まらない
            self.errors += 1
            self.last_error = f"{type(e).__name__}: {e}"
            return 0

    @classmethod
    def load(cls, module_path, file_path, scene="reply"):
        spec = importlib.util.spec_from_file_location("_claude_session_stats_glossary", module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"道具を読めない: {module_path}")
        mod = importlib.util.module_from_spec(spec)
        # dataclass などは読み込み中に sys.modules から自分のモジュールを引くので、先に登録する
        sys.modules[spec.name] = mod
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            sys.modules.pop(spec.name, None)
            raise
        with open(file_path, encoding="utf-8") as f:
            rules = mod.parse_glossary(f.read())
        return cls(mod, rules, scene)


# ---- 判定 ------------------------------------------------------------------

def ja_ratio(text):
    return len(JA_CHAR_RE.findall(text)) / max(1, len(text))


def is_english_reply(text):
    return len(text) > EN_MIN_LEN and ja_ratio(text) < EN_MAX_JA_RATIO


def is_long_reply(text):
    return len(text) > EN_MIN_LEN


def system_echo_kind(text, cfg):
    for k, _label, rx in cfg["_echo_res"]:
        if rx.match(text):
            return k
    return None


def rephrase_kinds(text, cfg):
    return [k for k, _label, rx in cfg["_rephrase_res"] if rx.search(text)]


def _not_user_text(s):
    return s.lstrip().startswith(NOT_USER_PREFIXES)


def user_text(obj):
    """type:"user" の行がユーザーの発言なら本文を、そうでなければ None を返す。"""
    if obj.get("isMeta") or obj.get("isCompactSummary") or obj.get("isSidechain"):
        return None
    origin = obj.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return None
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, str):
        s = content.strip()
    elif isinstance(content, list):
        # tool_result を含む行は道具の結果。text だけの行（画像つきの発言）は発言とみなす
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        s = "\n".join(b.get("text", "") for b in content
                      if isinstance(b, dict) and b.get("type") == "text").strip()
    else:
        return None
    if not s or _not_user_text(s):
        return None
    return s


def queued_text(obj):
    """作業中に送った発言（attachment の queued_command）なら本文を返す。"""
    a = obj.get("attachment")
    if not isinstance(a, dict) or a.get("type") != "queued_command":
        return None
    if a.get("commandMode") not in (None, "prompt"):
        return None
    origin = a.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return None
    p = a.get("prompt")
    if isinstance(p, list):
        p = "\n".join(b.get("text", "") for b in p if isinstance(b, dict))
    if not isinstance(p, str):
        return None
    s = p.strip()
    if not s or _not_user_text(s):
        return None
    return s


def assistant_parts(obj):
    """返答の本文（text をつないだもの）と、他のセッションへの送信の一覧 [(宛先, 本文)] を返す。"""
    msg = obj.get("message") or {}
    if msg.get("model") == "<synthetic>":
        return "", []
    content = msg.get("content")
    if not isinstance(content, list):
        return (content.strip() if isinstance(content, str) else ""), []
    texts, sends = [], []
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            texts.append(b.get("text", ""))
        elif b.get("type") == "tool_use" and b.get("name") in ("SendMessage", "CrossSessionMessage"):
            inp = b.get("input") or {}
            to = str(inp.get("to") or inp.get("recipient") or inp.get("target") or "")
            kind = inp.get("recipient_kind")
            # agent = 自分のサブエージェント。kind が無い古い記録は宛先の形で見分ける
            if kind == "agent" or (kind is None and SUBAGENT_ID_RE.fullmatch(to)):
                continue
            body = inp.get("message") or inp.get("content") or ""
            if not isinstance(body, str):
                body = json.dumps(body, ensure_ascii=False)
            if to:
                sends.append((to, body))
    return "\n".join(texts).strip(), sends


def parse_ts(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def jst_day(dt):
    return dt.astimezone(JST).date()


# ---- 読む ------------------------------------------------------------------

def iter_records(path, stats=None):
    """jsonl を1行ずつ読み、要る型の dict だけを返す。壊れた行は数えて飛ばす。"""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith('{"type":"'):
                end = line.find('"', 9)
                if end > 0 and line[9:end] not in _WANTED_TYPES:
                    continue
            if '"type":"attachment"' in line and '"queued_command"' not in line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                if line.strip() and stats is not None:
                    stats["bad_lines"] += 1
                continue
            if isinstance(obj, dict) and obj.get("type") in _WANTED_TYPES:
                yield obj


_NAME_RE = re.compile(r'"(?:agentName|customTitle)"\s*:\s*"((?:[^"\\]|\\.)*)"')


def file_names(path):
    """agentName / customTitle の一覧（出た順、重複なし）。json を読まずに取る。"""
    names = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            head = line[:40]
            if '"agent-name"' in head or '"custom-title"' in head:
                m = _NAME_RE.search(line)
                if m:
                    try:
                        n = json.loads('"' + m.group(1) + '"')
                    except ValueError:
                        continue
                    if n not in names:
                        names.append(n)
    return names


def file_shares_uuid(path, uuids):
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = UUID_RE.search(line)
            if m and m.group(1) in uuids:
                return True
    return False


def _broadcast_key(body, to, cfg):
    """宛先の名前を伏せ、空白を詰めた先頭 BROADCAST_COMPARE_CHARS 字。

    宛先ごとに「統括から担当 A へ」のように名前だけ変えた本文や、後ろに付け足しのある
    本文も同じ知らせとして束ねるため、名前を消して先頭だけを比べる。
    """
    s = re.sub(r"\s+", " ", body).strip()
    names = {to, to.split()[0] if to.split() else ""} | set(cfg["roles"]) | {cfg["master"]}
    for n in sorted((n for n in names if n), key=len, reverse=True):
        s = s.replace(n, "")
    return s[:BROADCAST_COMPARE_CHARS]


class SessionStats:
    """1つのセッション（名前）の、日ごとの集計。"""

    def __init__(self, name, is_master, cfg, glossary=None):
        self.name = name
        self.is_master = is_master
        self.cfg = cfg
        self.rules = glossary  # Glossary か None（統括でなければ使わない）
        self.files = []
        self.seen = set()
        self.users = Counter()
        self.queued = Counter()
        self.rephrase = defaultdict(Counter)  # kind -> day -> n
        self.rephrase_texts = defaultdict(list)  # kind -> [(day, text)]
        self.replies = Counter()
        self.long_replies = Counter()
        self.english = Counter()
        self.echo = defaultdict(Counter)  # kind -> day -> n
        self.reply_len = defaultdict(list)  # day -> [長さ]（ユーザーの発言の直前の返答）
        self.sends = []  # [(datetime, 宛先, 本文)]
        self.glossary = Counter()
        self._broadcasts = None

    def add_file(self, path, lo, hi, gstats):
        """lo <= 日 < hi のものだけ数える。返答の長さの状態はファイルごとに始め直す。"""
        self.files.append(path)
        self._broadcasts = None
        last_reply = None
        for obj in iter_records(path, gstats):
            if obj.get("isSidechain"):
                continue
            uid = obj.get("uuid")
            if uid:
                if uid in self.seen:
                    continue  # 続きの会話にそのまま写った行
                self.seen.add(uid)
            dt = parse_ts(obj.get("timestamp"))
            if dt is None:
                continue
            day = jst_day(dt)
            in_range = lo <= day < hi
            t = obj["type"]
            if t == "assistant":
                text, sends = assistant_parts(obj)
                if in_range:
                    for to, body in sends:
                        self.sends.append((dt, to, body))
                if not text:
                    continue
                last_reply = text
                if not in_range:
                    continue
                self.replies[day] += 1
                if is_long_reply(text):
                    self.long_replies[day] += 1
                if is_english_reply(text):
                    self.english[day] += 1
                ek = system_echo_kind(text, self.cfg)
                if ek:
                    self.echo[ek][day] += 1
                if self.is_master and self.rules is not None:
                    self.glossary[day] += self.rules.count(text)
                continue
            if t == "user":
                s = user_text(obj)
                queued = False
            else:
                s = queued_text(obj)
                queued = True
            if s is None:
                continue
            if in_range:
                self.users[day] += 1
                if queued:
                    self.queued[day] += 1
                for k in rephrase_kinds(s, self.cfg):
                    self.rephrase[k][day] += 1
                    self.rephrase_texts[k].append((day, s))
                if last_reply is not None:
                    self.reply_len[day].append(len(last_reply))
            last_reply = None

    def broadcasts(self):
        """全員への同じ知らせ: [(datetime, 宛先の数, 本文の頭)]。

        宛先ごとに名前だけ変えた本文も同じ知らせとみなすため、
        完全な一致ではなく、先頭 BROADCAST_COMPARE_CHARS 字の似かた（difflib）で束ねる。
        """
        if self._broadcasts is not None:
            return self._broadcasts
        active, closed = [], []  # 束 = [開始の時刻, 基準の本文, 宛先の集合]
        for dt, to, body in sorted(self.sends, key=lambda x: x[0]):
            key = _broadcast_key(body, to, self.cfg)
            if not key:
                continue
            # 窓を過ぎた束は比べる相手から外す（比べる数を小さく保つ）
            closed.extend(c for c in active if (dt - c[0]).total_seconds() > BROADCAST_WINDOW_SEC)
            active = [c for c in active if (dt - c[0]).total_seconds() <= BROADCAST_WINDOW_SEC]
            hit = None
            for c in active:
                if to in c[2]:
                    continue  # 同じ宛先への2通目は、続けて出した別の知らせ
                # 後ろに付け足しのある本文も束ねるため、短い方の長さにそろえて比べる（短すぎる本文は全体で）
                n = min(len(c[1]), len(key))
                x, y = (c[1][:n], key[:n]) if n >= 40 else (c[1], key)
                sm = difflib.SequenceMatcher(None, x, y, autojunk=False)
                if (sm.real_quick_ratio() >= BROADCAST_SIMILARITY and sm.quick_ratio() >= BROADCAST_SIMILARITY
                        and sm.ratio() >= BROADCAST_SIMILARITY):
                    hit = c
                    break
            if hit is None:
                active.append([dt, key, {to}])
            else:
                hit[2].add(to)
        out = [(c[0], len(c[2]), c[1][:60]) for c in closed + active if len(c[2]) >= BROADCAST_MIN_RECIPIENTS]
        out.sort()
        self._broadcasts = out
        return out


def discover(cfg, lo_epoch):
    """{セッション名: [ファイル]}、統括の名前の集合、名前の無いファイルの一覧を返す。

    統括: ホームのフォルダで agentName か customTitle が master のファイル。
    担当: <home><role_dir_prefix>*/ の直下のファイル（名前はフォルダの残り）と、ホームのフォルダで名前が担当のもの
    （roles が空なら、名前のあるもの全部）。名前の無いファイルは collect で、uuid の重なりから続きとして足す。
    更新が lo_epoch より前のファイルは、期間の記録を持たないので読まない。
    """
    projects, home_project, master_name = cfg["projects"], cfg["home_project"], cfg["master"]
    roles = cfg["roles"]
    sessions = defaultdict(list)
    master = set()
    unnamed = []
    home_dir = os.path.join(projects, home_project)
    for f in sorted(glob.glob(os.path.join(glob.escape(home_dir), "*.jsonl"))):
        if os.path.getmtime(f) < lo_epoch:
            continue
        names = file_names(f)
        if master_name and master_name in names:
            sessions[master_name].append(f)
            master.add(master_name)
        else:
            mine = [n for n in names if n in roles] if roles else [n for n in names if n != master_name]
            if mine:
                sessions[mine[-1]].append(f)
            elif not names:
                unnamed.append(f)
    prefix = cfg["role_dir_prefix"]
    if prefix:
        head = home_project + prefix
        for d in sorted(glob.glob(os.path.join(glob.escape(projects), glob.escape(head) + "*"))):
            role = os.path.basename(d)[len(head):]
            for f in sorted(glob.glob(os.path.join(glob.escape(d), "*.jsonl"))):
                if os.path.getmtime(f) >= lo_epoch:
                    sessions[role].append(f)
    return sessions, master, unnamed


def collect(cfg, lo, hi, glossary=None):
    """lo <= 日 < hi の記録を数える。({名前: SessionStats}, 全体の Counter) を返す。"""
    lo_epoch = datetime(lo.year, lo.month, lo.day, tzinfo=JST).timestamp()
    sessions, master, unnamed = discover(cfg, lo_epoch)
    gstats = Counter()
    result = {}
    home_dir = os.path.join(cfg["projects"], cfg["home_project"])
    for name in sorted(sessions, key=lambda n: (n != cfg["master"], n)):
        st = SessionStats(name, name in master, cfg, glossary)
        for f in sessions[name]:
            st.add_file(f, lo, hi, gstats)
        result[name] = st
    # 名前の無いファイル: ホームのフォルダの名前つきセッションと uuid が重なれば、その続き（圧縮の前・分岐）
    # 前身の前身（名前なし → 名前なし → 統括）も拾えるよう、足すものが無くなるまで繰り返す
    pending = list(unnamed)
    added = True
    while pending and added:
        added = False
        for f in list(pending):
            for name, st in result.items():
                if os.path.dirname(st.files[0]) != home_dir:
                    continue
                if file_shares_uuid(f, st.seen):
                    st.add_file(f, lo, hi, gstats)
                    pending.remove(f)
                    added = True
                    break
    return result, gstats


# ---- まとめる --------------------------------------------------------------

def _sum(counter, days):
    return sum(counter.get(d, 0) for d in days)


def _median(xs):
    return int(statistics.median(xs)) if xs else None


def period_metrics(st, days):
    cfg = st.cfg
    days = list(days)
    m = {"users": _sum(st.users, days), "queued": _sum(st.queued, days)}
    for k, _l, _p in cfg["rephrase"]:
        m["re:" + k] = _sum(st.rephrase[k], days)
    m["rephrase_total"] = sum(m["re:" + k] for k, _l, _p in cfg["rephrase"])
    m["replies"] = _sum(st.replies, days)
    m["long_replies"] = _sum(st.long_replies, days)
    m["english"] = _sum(st.english, days)
    m["english_pct"] = (100.0 * m["english"] / m["long_replies"]) if m["long_replies"] else None
    for k, _l, _p in cfg["echo"]:
        m["echo:" + k] = _sum(st.echo[k], days)
    m["reply_median"] = _median([x for d in days for x in st.reply_len.get(d, [])])
    dayset = set(days)
    m["sends"] = sum(1 for dt, _t, _b in st.sends if jst_day(dt) in dayset)
    m["broadcasts"] = sum(1 for dt, _n, _b in st.broadcasts() if jst_day(dt) in dayset)
    # 用語集を読めなかったときは検査していないので、0 でなく — にする
    m["glossary"] = _sum(st.glossary, days) if st.is_master and st.rules is not None else None
    return m


def metric_rows(cfg):
    return ([("users", "ユーザーの発言（うち作業中に送った分は下）"), ("queued", "　うち queued_command")]
            + [("re:" + k, "言い直し: " + label) for k, label, _p in cfg["rephrase"]]
            + [("rephrase_total", "言い直しの計"),
               ("replies", "返答の数"),
               ("long_replies", f"長い返答（{EN_MIN_LEN} 字を超える）"),
               ("english", "英語の返答"),
               ("english_pct", "英語の返答の割合（長い返答のうち）")]
            + [("echo:" + k, "冒頭の英語のシステム文: " + label) for k, label, _p in cfg["echo"]]
            + [("reply_median", "返答の長さの中央値（字、発言の直前の返答）"),
               ("broadcasts", f"全員への同じ知らせ（{BROADCAST_MIN_RECIPIENTS} 人以上）"),
               ("sends", "他のセッションへのメッセージ"),
               ("glossary", "用語集の検査の件数（統括の返答、場面 reply）")])


def watch_keys(cfg):
    """増えたら悪い向きの項目（「増えた項目」に出す）。返答の数などは量なので出さない。"""
    return (["re:" + k for k, _l, _p in cfg["rephrase"]] + ["rephrase_total", "english", "english_pct"]
            + ["echo:" + k for k, _l, _p in cfg["echo"]]
            + ["reply_median", "broadcasts", "sends", "glossary"])


def fmt(v, key=""):
    if v is None:
        return "—"
    if key == "english_pct":
        return f"{v:.0f}%"
    return f"{v:,}"


def diff(a, b, key=""):
    if a is None or b is None:
        return "—"
    d = b - a
    if key == "english_pct":
        return f"{d:+.0f} pt"
    return f"{d:+,}"


def total_metrics(result, per_session, days, cfg):
    """セッションを足し合わせた値。中央値と割合は足せないので、元の値から計算し直す。"""
    tot = {}
    for key, _label in metric_rows(cfg):
        vals = [m[key] for m in per_session.values() if m[key] is not None]
        tot[key] = sum(vals) if vals else None
    tot["english_pct"] = (100.0 * tot["english"] / tot["long_replies"]) if tot.get("long_replies") else None
    tot["reply_median"] = _median([x for st in result.values() for d in days for x in st.reply_len.get(d, [])])
    return tot


def redact(text, words):
    text = EMAIL_RE.sub("<email>", text)
    for w in sorted({w for w in words if w}, key=len, reverse=True):
        # セッション名「<名前>-a1」は名前を落として「a1」にする。残りは <user> に置き換える
        # 英数字の語の一部（例: 短い名前が別の単語の中に出る）は壊さない
        b0, b1 = r"(?<![A-Za-z0-9])", r"(?![A-Za-z])"
        text = re.sub(b0 + re.escape(w) + r"-(?=[0-9a-f]{2}\b)", "", text, flags=re.IGNORECASE)
        text = re.sub(b0 + re.escape(w) + b1, "<user>", text, flags=re.IGNORECASE)
    return text


def _one_line(s, n=EXAMPLE_CHARS):
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def build_report(result, gstats, prev_days, cur_days, generated, cfg):
    prev_days, cur_days = list(prev_days), list(cur_days)
    rows = metric_rows(cfg)
    labels = dict(rows)
    master = cfg["master"]
    per_prev = {n: period_metrics(st, prev_days) for n, st in result.items()}
    per_cur = {n: period_metrics(st, cur_days) for n, st in result.items()}
    tot_prev = total_metrics(result, per_prev, prev_days, cfg)
    tot_cur = total_metrics(result, per_cur, cur_days, cfg)
    p0, p1, c0, c1 = prev_days[0], prev_days[-1], cur_days[0], cur_days[-1]
    L = []
    L.append(f"# セッションの数字 {c1.isoformat()}")
    L.append("")
    L.append(f"作成: {generated}（JST）。`claude-session-stats` が自動で書いたもの。日付はすべて JST。")
    L.append(f"直近の期間 = {c0}〜{c1}（{len(cur_days)} 日）、前の期間 = {p0}〜{p1}（{len(prev_days)} 日）。")
    if generated[:10] == c1.isoformat():
        L.append(f"直近の期間の最後の日（{c1}）は、作成の時刻までの分だけ。量の項目は少なめに出る。")
    L.append("数はすべて【計測】（記録を正規表現と決まりで数えたもの）。言い回しの揺れで数件のずれがありえる。")
    L.append("")

    L.append("## 1. 全体の比べ（全セッションの計）")
    L.append("")
    L.append("| 項目 | 前の期間 | 直近 | 差 |")
    L.append("|---|---|---|---|")
    for key, label in rows:
        L.append(f"| {label} | {fmt(tot_prev[key], key)} | {fmt(tot_cur[key], key)} | {diff(tot_prev[key], tot_cur[key], key)} |")
    L.append("")

    L.append("## 2. セッションごとの比べ（前の期間 → 直近）")
    L.append("")
    cols = [("users", "発言"), ("rephrase_total", "言い直し"), ("english", "英語の返答"),
            ("english_pct", "英語の割合"), ("reply_median", "長さの中央値"),
            ("broadcasts", "全員への知らせ"), ("sends", "送ったメッセージ"), ("glossary", "用語集")]
    L.append("| セッション | " + " | ".join(c for _k, c in cols) + " |")
    L.append("|---|" + "---|" * len(cols))
    for n in result:
        cells = [f"{fmt(per_prev[n][k], k)} → {fmt(per_cur[n][k], k)}" for k, _c in cols]
        L.append(f"| {n} | " + " | ".join(cells) + " |")
    L.append("")
    if master and master in result:
        L.append(f"「送ったメッセージ」の {master} の行が、統括から担当へのメッセージの数"
                 "（担当でない宛先も含む。サブエージェントへの送信は含まない）。")
        L.append("")

    L.append("## 3. 増えた項目")
    L.append("")
    inc = []
    if not tot_prev["users"] and not tot_prev["replies"]:
        inc.append("- 前の期間に記録が無いので、比べられない（直近の数だけを上の表に出した）。")
    for scope, a, b in [("全体", tot_prev, tot_cur)] + [(n, per_prev[n], per_cur[n]) for n in result]:
        for key in watch_keys(cfg):
            va, vb = a.get(key), b.get(key)
            if va is None or vb is None or vb <= va or (not a["users"] and not a["replies"]):
                continue
            tag = "（前の期間は 0）" if va == 0 else ""
            inc.append(f"- {scope}: {labels[key]} {fmt(va, key)} → {fmt(vb, key)}（{diff(va, vb, key)}）{tag}")
    L.extend(inc or ["- なし"])
    L.append("")

    L.append(f"## 4. 新しく現れた言い直しの例（種類ごとに原文を {EXAMPLE_MAX} つまで）")
    L.append("")
    L.append("前の期間に同じ文が無かったもの。")
    L.append("")
    cur_set, prev_set = set(cur_days), set(prev_days)
    any_ex = False
    for k, label, _p in cfg["rephrase"]:
        seen_prev = set()
        cur_items = []
        for n, st in result.items():
            for day, text in st.rephrase_texts[k]:
                key = _one_line(text, 10 ** 6)
                if day in prev_set:
                    seen_prev.add(key)
                elif day in cur_set:
                    cur_items.append((day, n, text, key))
        picked, keys = [], set()
        # 新しい順に取る（古い順だと期間の最初の日に偏る）
        for day, n, text, key in sorted(cur_items, key=lambda x: x[0], reverse=True):
            if key in seen_prev or key in keys:
                continue
            keys.add(key)
            picked.append(f"  - {day.strftime('%m-%d')} {n}:「{_one_line(text)}」")
            if len(picked) >= EXAMPLE_MAX:
                break
        if picked:
            any_ex = True
            L.append(f"- {label}")
            L.extend(picked)
    if not any_ex:
        L.append("- なし")
    L.append("")

    L.append("## 5. 日ごと（直近の期間）")
    L.append("")
    L.append("| セッション | 日 | 発言 | 言い直し | 返答 | 英語の返答 | 冒頭のシステム文 | 長さの中央値 | 送ったメッセージ |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for n, st in result.items():
        for d in cur_days:
            m = period_metrics(st, [d])
            if not (m["users"] or m["replies"] or m["sends"]):
                continue
            echo = sum(m["echo:" + k] for k, _l, _p in cfg["echo"])
            L.append(f"| {n} | {d.strftime('%m-%d')} | {m['users']} | {m['rephrase_total']} | {m['replies']} | "
                     f"{m['english']} | {echo} | {fmt(m['reply_median'])} | {m['sends']} |")
    L.append("")

    L.append("## 6. 数え方")
    L.append("")
    for note in cfg["notes"]:
        L.append(note)
        L.append("")
    if master:
        L.append(f"- 統括: ホームのフォルダで agentName か customTitle が `{master}` の記録。名前の無い記録で uuid が重なるもの（圧縮の前の会話・分岐）も足す。重なった行は uuid で1回だけ数える。")
    where = "ホームのフォルダで名前が担当（設定の roles）の記録" if cfg["roles"] else "ホームのフォルダで名前のある記録（その名前で数える）"
    if cfg["role_dir_prefix"]:
        where = f"`{cfg['role_dir_prefix']}*` のフォルダの記録と、" + where
    L.append(f"- 担当: {where}。サブエージェントの記録（subagents/）と isSidechain の行は数えない。")
    L.append("- ユーザーの発言: `type:\"user\"` の本文（tool_result・isMeta・圧縮の要約を除く）と、作業中に送った queued_command。`<cross-session-message`・`<agent-message`・`<task-notification` で始まるものは数えない。")
    L.append(f"- 英語の返答: {EN_MIN_LEN} 字を超え、かな・カナ・漢字の割合が {EN_MAX_JA_RATIO:.0%} 未満の返答。冒頭の英語のシステム文は、返答の先頭が表の文で始まるものを別に数える。")
    L.append("- 返答の長さ: ユーザーが次に発言する直前の、最後の返答の字数（その発言の日に数える）。")
    L.append(f"- 全員への同じ知らせ: 宛先の名前を伏せた本文の先頭 {BROADCAST_COMPARE_CHARS} 字の似かた（difflib の ratio）が "
             f"{BROADCAST_SIMILARITY} 以上のものを、{BROADCAST_WINDOW_SEC // 60} 分の内に {BROADCAST_MIN_RECIPIENTS} つ以上のセッションへ"
             "送ったもの（1回と数える）。")
    L.append("- 他のセッションへのメッセージ: SendMessage のうち、宛先がサブエージェント（recipient_kind が agent）でないもの。")
    nrules = next((len(st.rules) for st in result.values() if st.rules is not None), None)
    L.append("- 用語集: 設定の用語集の道具（`check_text(…, scene=…)`）を統括の返答ごとにかけた件数。"
             + (f"作成の時点の用語集（{nrules} 語）で、前の期間も数え直している。用語集が変わると前の週の md とはずれる。"
                if nrules is not None else "用語集が無いか読めなかったので数えていない（—）。"))
    gerr = next((st.rules.errors for st in result.values() if st.rules is not None), 0)
    if gerr:
        L.append(f"- 用語集の道具が {gerr} 件の返答で例外を出した（その返答は 0 件と数えた）。")
    files = sum(len(st.files) for st in result.values())
    L.append(f"- 読んだ記録: {files} ファイル。読めなかった行: {gstats.get('bad_lines', 0)} 行。")
    L.append("")
    return "\n".join(L)
