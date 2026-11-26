"""claude-waits: 待ちと定例の台帳。書くのは各セッション（add）、動かすのは LLM でない常駐（run）。

セッションの中で polling（/loop・ScheduleWakeup）すると、起きるたびに会話の全文を読み直してトークンが要る。
背景のシェルや Monitor での待ちは、claude の終了や kitty の再起動で消える。AI が自分で仕込んだ待ちを忘れることもある。
そこで、待ちを 1 か所の台帳に置き、常駐が条件を調べて、成立したら持ち主の役割の受信箱（claude-relay の receive()、
同じ端末の local 送り）に届ける。hook が登録中の待ちをセッションに見せて、忘れを防ぐ。

置き場:
  台帳     $XDG_STATE_HOME/claude-kitty/waits/<キー>.json（0600、フォルダ 0700。既定 ~/.local/state/…。macOS も同じ）
  出力     $XDG_STATE_HOME/claude-kitty/waits/out/<キー>-<日時>.txt（--prompt-file の claude -p の出力）
  hook     $XDG_RUNTIME_DIR/claude-waits/seen/<セッション id>.json（前に見せた台帳の印）・errors.log

1 件の記録（キー 1 つ = ファイル 1 つ。add は同じキーを上書きして冪等）:
  key, role（持ち主の役割。セッション id ではない: /clear・restore で id が変わっても続く）, why, then,
  kind = cmd | gh_pr | at | cloud と cond（条件）, action（deliver か prompt）, expires（エポック秒か None = never）,
  status = active | done | expired, rev（add のたびに変わる。調べている間に登録し直された結果を捨てる印）,
  outbox（まだ届けられていない知らせ。届いたら id で消す。届かなければ間を伸ばして届け直す）,
  running（--prompt-file の claude -p を切り離して動かしている間の pid・開始時刻・状態のファイル）, 調べた・発火した時刻と結果。
詳しくは docs/waits.md。
"""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from config import RELAY_NAME

KEY_RE = re.compile(RELAY_NAME)       # ファイル名に入るので、役割名と同じ形（英数字と . _ -、64 字まで）
KINDS = ("cmd", "gh_pr", "at", "cloud")
EVENT_KINDS = ("cmd", "gh_pr")
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WHY_MAX = 1000
THEN_MAX = 2000
DEFAULT_EVERY = 300
MIN_EVERY = 30
MAX_EXPIRES = 366 * 86400
VIA = "claude-waits"                  # relay の受信箱の記録に付ける印（枠の送り元を claude-waits、reply_note を「返信不要」にする）
HOOK_BUDGET = 3000                    # hook の additionalContext に入れる文の上限（毎回のトークンを抑える）
HOOK_LINE = 160                       # 1 件の行の上限
OUT_READ_MAX = 1024 * 1024            # 子の出力を読むのはこれだけ（知らせに入れるのは先頭だけなので）
KILL_WAIT = 5                         # 止めたあとに子の終わりを待つ秒（setsid した孫がいても常駐を止めない）
RETRY_MAX = 3600                      # 届け直しの間の上限（秒）
RUN_GRACE = 120                       # claude -p が prompt_timeout_seconds をこれだけ過ぎても動いていれば常駐が止める
BIN = Path(__file__).resolve().parent.parent / "bin" / "claude-waits"
_REPORTED_BAD = {}                    # 読めない記録を 1 度だけログに出す（名前 → mtime）
_RUNNERS = {}                         # この常駐が起こした runner（pid → Popen。終わったら片付ける）
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class Usage(Exception):
    """add などの引数の誤り（終了コード 2）。"""


# ---- 場所 ---------------------------------------------------------------------------

def home(env):
    return Path(env.get("HOME") or os.path.expanduser("~"))


def ledger_dir(env):
    base = Path(env["XDG_STATE_HOME"]) if env.get("XDG_STATE_HOME") else home(env) / ".local" / "state"
    return base / "claude-kitty" / "waits"


def out_dir(env):
    return ledger_dir(env) / "out"


def hook_dir(env):
    from rundir import runtime_dir
    return runtime_dir(env) / "claude-waits"


def record_path(env, key):
    return ledger_dir(env) / f"{check_key(key)}.json"


def check_key(key):
    if not isinstance(key, str) or not KEY_RE.match(key):
        raise Usage(f"キーの形が正しくない（英数字と . _ - で 64 字まで、頭は英数字）: {key!r}")
    return key


def check_role(role):
    if not isinstance(role, str) or not KEY_RE.match(role):
        raise Usage(f"役割の名前が正しくない（英数字と . _ - で 64 字まで）: {role!r}")
    return role


# ---- 小さな関数 -------------------------------------------------------------------------

def clean(text, limit=None):
    """記録や端末に出す文: 改行とタブ以外の制御文字を見える形にする。limit を超えたら切って … を付ける。"""
    text = _CTRL.sub(lambda m: f"\\x{ord(m.group()):02x}", str(text))
    return text if limit is None or len(text) <= limit else text[:limit] + "…"


def one_line(text, limit):
    return clean(" ".join(str(text).split()), limit)


def now_iso(t):
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


def when_ja(t):
    """台帳の一覧・知らせに出す日時（手元の時刻。年は今年なら省く）。"""
    d = datetime.fromtimestamp(t)
    return d.strftime("%m/%d %H:%M") if d.year == datetime.now().year else d.strftime("%Y/%m/%d %H:%M")


_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def parse_duration(text):
    """'90s'・'5m'・'2h'・'2d'・'1w'・'45'（秒）を秒にする。0 以下や読めないものは Usage。"""
    m = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([smhdw]?)\s*", str(text))
    if not m:
        raise Usage(f"時間の形が読めない: {text!r}（例: 90s・5m・2h・2d）")
    sec = float(m.group(1)) * _UNITS[m.group(2) or "s"]
    if sec <= 0:
        raise Usage(f"時間は 0 より大きく: {text!r}")
    return sec


def parse_expires(text, now):
    """--expires: 'never' は None、'2d' などは now からの長さ、ISO の日時（時刻帯が無ければ手元の時刻）。"""
    if text is None:
        raise Usage("--expires が要る（出来事は 2d などの期限、時刻は never か期限）")
    if str(text).strip().lower() == "never":
        return None
    try:
        sec = parse_duration(text)
        t = now + sec
    except Usage:
        s = str(text).strip()
        if s[-1:] in ("Z", "z"):
            s = s[:-1] + "+00:00"  # Python 3.9・3.10 の fromisoformat は末尾の Z を読めない
        try:
            d = datetime.fromisoformat(s)
        except ValueError:
            raise Usage(f"--expires が読めない: {text!r}（2d・12h・2026-12-01T09:00・never）")
        t = d.timestamp()  # 時刻帯の無い日時は手元の時刻として読む
    if t <= now:
        raise Usage(f"--expires が過去: {text!r}")
    if t - now > MAX_EXPIRES:
        raise Usage("--expires は 1 年まで（それより長いものは never にして、要らなくなったら rm）")
    return t


def parse_hhmm(text):
    m = re.fullmatch(r"\s*([01]?[0-9]|2[0-3]):([0-5][0-9])\s*", str(text))
    if not m:
        raise Usage(f"--at は HH:MM（00:00〜23:59）: {text!r}")
    return int(m.group(1)), int(m.group(2))


def parse_days(text):
    """'daily'・'mon-fri'・'mon,wed'・'sat-sun' を曜日の番号（月 = 0）のリストにする。"""
    t = (text or "daily").strip().lower()
    if t in ("daily", "every", "*"):
        return list(range(7))
    if t == "weekdays":
        return list(range(5))
    if t == "weekends":
        return [5, 6]
    out = set()
    for part in t.split(","):
        part = part.strip()
        a, sep, b = part.partition("-")
        if a not in DAY_NAMES or (sep and b not in DAY_NAMES):
            raise Usage(f"--days が読めない: {text!r}（daily・mon-fri・mon,wed・sat-sun）")
        i, j = DAY_NAMES.index(a), DAY_NAMES.index(b) if sep else DAY_NAMES.index(a)
        k = i
        while True:  # sat-mon のように週をまたいでもよい
            out.add(k)
            if k == j:
                break
            k = (k + 1) % 7
    return sorted(out)


def days_ja(days):
    if days == list(range(7)):
        return "毎日"
    if days == list(range(5)):
        return "平日"
    return "・".join(["月", "火", "水", "木", "金", "土", "日"][d] for d in days)


def _at_on(day, h, m):
    return datetime(day.year, day.month, day.day, h, m).timestamp()  # 手元の時刻（夏時間は Python に任せる）


def next_occurrence(now, h, m, days):
    """now より後の、いちばん近い予定の時刻。"""
    base = datetime.fromtimestamp(now).date()
    for i in range(0, 9):
        d = base + timedelta(days=i)
        if d.weekday() in days:
            t = _at_on(d, h, m)
            if t > now:
                return t
    raise ValueError("予定の曜日が無い")


def prev_occurrence(now, h, m, days):
    """now 以前の、いちばん近い予定の時刻。"""
    base = datetime.fromtimestamp(now).date()
    for i in range(0, 9):
        d = base - timedelta(days=i)
        if d.weekday() in days:
            t = _at_on(d, h, m)
            if t <= now:
                return t
    raise ValueError("予定の曜日が無い")


def parse_pr(text):
    """https://github.com/OWNER/REPO/pull/N か OWNER/REPO#N を (owner/repo, N) にする。"""
    t = str(text).strip()
    m = (re.fullmatch(r"https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([0-9]+)(?:[/?#].*)?", t)
         or re.fullmatch(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)", t))
    if not m:
        raise Usage(f"--gh-pr は PR の URL か owner/repo#N: {text!r}")
    return m.group(1), int(m.group(2))


def clip_bytes(text, limit):
    """UTF-8 で limit バイトに収める（relay.max_bytes を超えて受信箱に断られないように）。"""
    b = text.encode("utf-8")
    if len(b) <= limit:
        return text
    note = "\n（長いので途中まで）"
    nb = len(note.encode("utf-8"))
    if limit <= nb:
        return b[:max(0, limit)].decode("utf-8", "ignore")
    return b[:limit - nb].decode("utf-8", "ignore") + note


# ---- 台帳の読み書き ------------------------------------------------------------------------

def mkdir_private(d):
    import relay
    relay.mkdir_private(d)


@contextlib.contextmanager
def ledger_lock(env, name=".lock", timeout=10.0, wait=True):
    """台帳のフォルダの flock。wait=False で取れなければ False を渡す（check の二重起動を避ける）。"""
    d = ledger_dir(env)
    mkdir_private(d)
    fd = os.open(d / name, os.O_WRONLY | os.O_CREAT, 0o600)
    f = os.fdopen(fd, "w")
    got = False
    try:
        end = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except BlockingIOError:
                if not wait or time.monotonic() >= end:
                    break
                time.sleep(0.05)
        if wait and not got:
            raise OSError(f"台帳のロックを取れない（{d / name}）")
        yield got
    finally:
        f.close()


def write_record(env, rec):
    import relay
    p = record_path(env, rec["key"])
    mkdir_private(p.parent)
    relay.write_private(p, json.dumps(rec, ensure_ascii=False, indent=1))


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def valid_record(rec, stem):
    """台帳の 1 件の形が正しいか。手で直した・古い・壊れた記録で check が途中で落ちないよう、使う型を全部見る。"""
    if not (isinstance(rec, dict) and rec.get("key") == stem and isinstance(rec.get("role"), str) and KEY_RE.match(rec["role"])
            and rec.get("kind") in KINDS and isinstance(rec.get("cond"), dict) and isinstance(rec.get("rev"), str)
            and rec.get("status") in ("active", "done", "expired") and isinstance(rec.get("why", ""), str)
            and isinstance(rec.get("then", ""), str) and (rec.get("expires") is None or _num(rec.get("expires")))
            and isinstance(rec.get("outbox", []), list) and (rec.get("running") is None or isinstance(rec["running"], dict))):
        return False
    a = rec.get("action")
    if not (isinstance(a, dict) and (a.get("kind") == "deliver"
                                     or (a.get("kind") == "prompt" and isinstance(a.get("prompt_file"), str)
                                         and isinstance(a.get("cwd"), (str, type(None)))))):
        return False
    if "fired" in rec and not _int(rec["fired"]):
        return False
    for k in ("last_check", "last_fired", "t_done", "t"):
        if rec.get(k) is not None and not _num(rec[k]):
            return False
    run_ = rec.get("running")
    if run_ is not None and not (_num(run_.get("started")) and (run_.get("pid") is None or _int(run_["pid"]))
                                 and (run_.get("timeout") is None or _num(run_["timeout"]))
                                 and all(isinstance(run_.get(k, ""), str) for k in ("state", "spec", "out"))
                                 and isinstance(run_.get("extra", []), list)):
        return False
    if not all(isinstance(x, dict) for x in rec.get("outbox", [])):
        return False
    c, k = rec["cond"], rec["kind"]
    if k == "cmd":
        return (isinstance(c.get("cmd"), str) and _num(c.get("every", DEFAULT_EVERY))
                and isinstance(c.get("cwd"), (str, type(None))))
    if k == "gh_pr":
        return (isinstance(c.get("repo"), str) and _int(c.get("number")) and _num(c.get("every", DEFAULT_EVERY))
                and isinstance(c.get("base", {}), dict))
    if k == "at":
        days = c.get("days")
        return (_int(c.get("h")) and 0 <= c["h"] <= 23 and _int(c.get("m")) and 0 <= c["m"] <= 59
                and isinstance(days, list) and days and all(_int(d) and 0 <= d <= 6 for d in days)
                and (rec.get("next_at") is None or _num(rec.get("next_at"))))
    return isinstance(c.get("routine"), str)


def load_record(path):
    """1 件を読む。読めない・形の違うものは None（壊れた 1 件で全部を止めない）。"""
    try:
        rec = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not valid_record(rec, Path(path).stem):
        return None
    rec.setdefault("outbox", [])
    return rec


def all_records(env):
    """(読めた記録のリスト, 読めなかったファイルの名前のリスト)。キーの順。"""
    d = ledger_dir(env)
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".json") and not n.startswith("."))
    except OSError:
        return [], []
    recs, bad = [], []
    for n in names:
        r = load_record(d / n)
        if r is None:
            bad.append(n)
        else:
            recs.append(r)
    return recs, bad


def get_record(env, key):
    return load_record(record_path(env, key))


def active_for(env, role):
    return [r for r in all_records(env)[0] if r["role"] == role and r["status"] == "active"]


# ---- 役割 ---------------------------------------------------------------------------------

def own_role(env):
    """呼んだセッションの役割（claude-state の記録、無ければ kitty のペインの user var。relay の send と同じ）。"""
    import relay
    return relay.own_role(env)


def role_alive(env, role):
    """その役割に、受信箱を受け取れるセッションがいるか。"socket"（いる）・"state"（いない）・None（いない）を返す。

    「いる」は relay の起こすための socket の記録（relay の hook が入っていて、claude が生きているセッション）が
    あるときだけ。claude-state の記録だけあるときは "state": 動いているが relay の hook が無いかもしれず、
    受信箱に置いても渡らないので、統括へ回して理由に添える。止まって入力を待っているセッションも「いる」
    （起こすのは receive() に任せる）。"""
    import relay
    from kstate import alive, all_states, state_dir
    if relay.wake_records(env, role):
        return "socket"
    d = state_dir(relay.runtime_env(env))
    if d.is_dir():
        for e in all_states(d):
            if e.get("role") == role and alive(e):
                return "state"
    return None


# ---- add ---------------------------------------------------------------------------------

def build_record(env, key, now, role, why, then, kind, cond, expires_text, prompt_file=None, cwd=None):
    """add の引数から記録を作る（書かない）。誤りは Usage。"""
    check_key(key)
    check_role(role)
    why = (why or "").strip()
    then = (then or "").strip()
    if kind != "cloud":
        if not why:
            raise Usage("--why が要る（なぜ待つか。届いた本文はこれだけで分かるように）")
        if not then:
            raise Usage("--then が要る（届いたら何をするか・終わったら何を消すか）")
    elif not why:
        raise Usage("--why が要る（クラウドに何を任せたか）")
    if len(why) > WHY_MAX:
        raise Usage(f"--why は {WHY_MAX} 字まで")
    if len(then) > THEN_MAX:
        raise Usage(f"--then は {THEN_MAX} 字まで（長い手順はファイルに書いて、そのパスを書く）")
    expires = parse_expires(expires_text, now)
    if kind in EVENT_KINDS and expires is None:
        raise Usage("出来事の待ち（--when-cmd・--gh-pr）は期限が要る（--expires 2d など。never は使えない）")
    action = {"kind": "deliver"}
    if prompt_file:
        if kind == "cloud":
            raise Usage("--cloud には --prompt-file を付けられない（クラウドのルーティンが動かす）")
        pf = Path(prompt_file).expanduser()
        pf = pf if pf.is_absolute() else Path(cwd or os.getcwd()) / pf
        if not pf.is_file():
            raise Usage(f"--prompt-file が無い: {pf}")
        action = {"kind": "prompt", "prompt_file": str(pf), "cwd": str(Path(cwd).expanduser()) if cwd else str(pf.parent)}
    rec = {"key": key, "role": role, "why": why, "then": then, "kind": kind, "cond": cond, "action": action,
           "expires": expires, "status": "active", "rev": uuid.uuid4().hex[:12], "created_at": now_iso(now), "t": now,
           "outbox": [], "fired": 0}
    if kind == "at":
        rec["next_at"] = next_occurrence(now, cond["h"], cond["m"], cond["days"])
    return rec


def _read_head(f):
    f.seek(0)
    return f.read(OUT_READ_MAX).decode("utf-8", "replace")


def killpg(pid):
    with contextlib.suppress(OSError):
        os.killpg(pid, signal.SIGKILL)


def run_group(argv, timeout=None, input=None, cwd=None, env=None, **_kw):
    """subprocess.run（text=True）と同じ形の結果を返すが、終わりは直接の子の終了（wait）で見て、出力は一時ファイルに
    受ける。パイプで受けると、背景に回した孫（`sleep 30 & true`）がパイプを握っている間 communicate が返らず、
    成功が時間切れに見えるため。子は新しいセッションで動かし、終わったら・時間切れなら残ったグループごと止める
    （孫が毎回の poll で残らないように）。止めたあとの待ちにも上限を付ける（setsid した孫がいても常駐を止めない）。"""
    with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe, tempfile.TemporaryFile() as fi:
        if input is not None:
            fi.write(input.encode("utf-8"))
            fi.seek(0)
        p = subprocess.Popen(argv, stdin=fi if input is not None else subprocess.DEVNULL, stdout=fo, stderr=fe,
                             cwd=cwd, env=env, start_new_session=True)
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            killpg(p.pid)
            with contextlib.suppress(subprocess.TimeoutExpired):
                p.wait(timeout=KILL_WAIT)
            raise subprocess.TimeoutExpired(argv, timeout, output=_read_head(fo), stderr=_read_head(fe))
        killpg(p.pid)  # 終わった子のグループに残った背景の孫
        return subprocess.CompletedProcess(argv, rc, _read_head(fo), _read_head(fe))


def gh_counts(env, repo, number, conf=None, run=run_group):
    """PR のレビュー・コメント（会話）・レビューコメント（行への）の数。gh が無い・失敗したら OSError。"""
    import shutil
    gh = shutil.which("gh", path=env.get("PATH"))
    if not gh:
        raise OSError("gh が見つからない（GitHub CLI を入れて gh auth login する）")
    timeout = (conf or {}).get("cmd_timeout_seconds", 60)
    out = {}
    for name, api in (("reviews", f"repos/{repo}/pulls/{number}/reviews"),
                      ("comments", f"repos/{repo}/issues/{number}/comments"),
                      ("review_comments", f"repos/{repo}/pulls/{number}/comments")):
        try:
            r = run([gh, "api", "--paginate", api, "--jq", "length"], timeout=timeout, env=child_env(env))
        except subprocess.TimeoutExpired:
            raise OSError(f"gh api {api} が {timeout:g} 秒で終わらない")
        if r.returncode != 0:
            raise OSError(f"gh api {api} が失敗した（{r.returncode}）: {one_line(r.stderr or r.stdout, 200)}")
        try:
            out[name] = sum(int(x) for x in r.stdout.split())  # --paginate はページごとに 1 行
        except ValueError:
            raise OSError(f"gh api {api} の出力が数でない: {one_line(r.stdout, 100)}")
    return out


def add(env, key, now=None, role=None, why=None, then=None, when_cmd=None, every=None, gh_pr=None, at=None,
        days=None, cloud=None, desc=None, expires=None, prompt_file=None, cwd=None, run=run_group):
    """登録する（同じキーは上書き）。(記録, 上書きしたか)。誤りは Usage、gh の失敗は OSError。"""
    from config import load_waits_config
    now = time.time() if now is None else now
    try:
        conf = load_waits_config(env)
    except ValueError as e:
        raise Usage(str(e))
    picked = [x for x in (when_cmd, gh_pr, at, cloud) if x]
    if len(picked) != 1:
        raise Usage("条件は --when-cmd・--gh-pr・--at・--cloud のどれか 1 つ")
    if days and not at:
        raise Usage("--days は --at と一緒に")
    if every and not (when_cmd or gh_pr):
        raise Usage("--every は --when-cmd・--gh-pr と一緒に")
    if desc and not cloud:
        raise Usage("--desc は --cloud と一緒に")
    role = role or own_role(env)
    if not role:
        raise Usage("役割が分からない（kitty の外・claude-state の記録が無い）。--role で持ち主の役割を書く")
    ev = parse_duration(every) if every else DEFAULT_EVERY
    if (when_cmd or gh_pr) and ev < MIN_EVERY:
        raise Usage(f"--every は {MIN_EVERY} 秒以上")
    base_cwd = str(Path(cwd).expanduser()) if cwd else os.getcwd()
    if when_cmd:
        kind, cond = "cmd", {"cmd": when_cmd, "every": ev, "cwd": base_cwd}
    elif gh_pr:
        repo, number = parse_pr(gh_pr)
        kind, cond = "gh_pr", {"repo": repo, "number": number, "every": ev}
    elif at:
        h, m = parse_hhmm(at)
        kind, cond = "at", {"h": h, "m": m, "at": f"{h:02d}:{m:02d}", "days": parse_days(days)}
    else:
        if not KEY_RE.match(str(cloud)):
            raise Usage(f"--cloud はルーティンの ID: {cloud!r}")
        kind, cond = "cloud", {"routine": cloud, "desc": (desc or "").strip()[:WHY_MAX]}
    rec = build_record(env, key, now, role, why, then, kind, cond, expires, prompt_file, cwd)
    if kind == "gh_pr":
        # 登録した時点の数を基準にする（それより増えたら成立）。gh が無い・認証が無いなら、ここで分かるように失敗する
        cond["base"] = gh_counts(env, cond["repo"], cond["number"], conf, run=run)
    with ledger_lock(env):
        p = record_path(env, key)
        replaced = p.exists()
        old = get_record(env, key) if replaced else None
        if old and old.get("outbox"):
            # まだ届いていない知らせ（前の成立・期限切れ）を登録し直しで捨てない。rev が変わっても届け直す
            rec["outbox"] = list(old["outbox"])
        if old and old.get("running"):
            rec["running"] = old["running"]  # 動いている claude -p の結果も、登録し直しのあとで届ける
        write_record(env, rec)
    return rec, replaced


def set_status(env, key, status, now=None):
    now = time.time() if now is None else now
    with ledger_lock(env):
        rec = get_record(env, key)
        if rec is None:
            return None
        rec.update(status=status, finished_at=now_iso(now), t_done=now)
        write_record(env, rec)
    return rec


def stop_running(run_):
    """動いている claude -p（runner と、別のグループの claude）を止め、runner の spec と状態のファイルを消す。"""
    if not isinstance(run_, dict):
        return
    try:
        st = json.loads(Path(run_["state"]).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        st = {}
    if isinstance(st, dict) and _int(st.get("child")) and not st.get("done"):
        killpg(st["child"])
    if _int(run_.get("pid")):
        import relay
        if relay.same_process(run_["pid"], run_.get("start")):
            killpg(run_["pid"])
    for k in ("spec", "state"):
        with contextlib.suppress(OSError, TypeError, KeyError):
            os.unlink(run_[k])


def remove(env, key):
    with ledger_lock(env):
        p = record_path(env, key)
        rec = get_record(env, key)
        try:
            p.unlink()
        except FileNotFoundError:
            return False
    if rec and rec.get("running"):
        stop_running(rec["running"])  # 結果を拾う記録が無くなるので、最後まで動かさない
    return True


# ---- 説明の文 -----------------------------------------------------------------------------

def cond_text(rec):
    c, k = rec["cond"], rec["kind"]
    if k == "cmd":
        return f"コマンド `{one_line(c.get('cmd', ''), 80)}` が成功したら（{int(c.get('every', DEFAULT_EVERY))} 秒ごと）"
    if k == "gh_pr":
        return f"GitHub PR {c.get('repo')}#{c.get('number')} にレビュー・コメントが増えたら（{int(c.get('every', DEFAULT_EVERY))} 秒ごと）"
    if k == "at":
        return f"{days_ja(c.get('days') or list(range(7)))} {c.get('at')}"
    return f"クラウド（/schedule）のルーティン {c.get('routine')}" + (f": {one_line(c['desc'], 80)}" if c.get("desc") else "")


def expires_text(rec):
    return "期限なし" if rec.get("expires") is None else f"期限 {when_ja(rec['expires'])}"


def short_line(rec):
    act = "、claude -p で実行" if (rec.get("action") or {}).get("kind") == "prompt" else ""
    s = (f"- {rec['key']}: {cond_text(rec)}{act}、{expires_text(rec)}。なぜ: {one_line(rec.get('why', ''), 60)}"
         + (f" / 届いたら: {one_line(rec['then'], 60)}" if rec.get("then") else ""))
    return clean(s, HOOK_LINE)


# ---- 届ける ---------------------------------------------------------------------------------

def fire_body(rec, what, extra=None, tail=None):
    # 発信元（claude-waits）は relay の枠の from が、ユーザーの指示でない・返信不要は枠と末尾の注意書きが言う
    lines = [f"待ち {rec['key']} が成立: {what}", f"なぜ待っていた: {rec.get('why', '')}"]
    if rec.get("then"):
        lines.append(f"届いたらすること: {rec['then']}")
    lines += list(extra or [])
    if tail:
        lines.append(tail)
    return "\n".join(lines)


def expired_body(rec):
    if rec["kind"] in EVENT_KINDS:
        what = (f"待ち {rec['key']} の期限切れ: 役割 {rec['role']} の条件（{cond_text(rec)}）が、"
                f"成立しないまま期限（{when_ja(rec['expires'])}）を過ぎた。")
    else:  # 時刻・クラウドは繰り返しなので「成立しない」ではない
        what = (f"待ち {rec['key']} の期限切れ: 役割 {rec['role']} の定例（{cond_text(rec)}）は、"
                f"期限（{when_ja(rec['expires'])}）が来たので終えた。")
    return "\n".join([
        what,
        f"なぜ待っていた: {rec.get('why', '')}",
        f"届いたらすること（だった）: {rec.get('then', '')}",
        f"続けるなら claude-waits add {rec['key']} … で期限を延ばして登録し直す。要らなければ claude-waits rm {rec['key']}。"
        f"持ち主の役割 {rec['role']} にも伝えるかを決める。",
    ])


def deliver(env, conf, owner, body, now=None, alive=None):
    """知らせを owner（役割。None なら統括）の受信箱へ。持ち主が relay.receivers に無い・動いているセッションが
    無いときは、統括へ理由を添えて回す。(届いたか, 1 行の結果)。"""
    import relay
    from config import load_relay_config
    alive = alive or role_alive
    master = conf["master_role"]
    try:
        rconf = load_relay_config(env)
    except ValueError as e:
        return False, f"relay の設定が読めない: {e}"
    target, note = owner or master, None
    if owner and owner != master:
        if owner not in rconf["receivers"]:
            target, note = master, f"宛先 {owner} 宛て。受け取れない（relay.receivers に {owner} が無い）ので統括へ回した"
        else:
            here = alive(env, owner)
            if here == "state":
                target, note = master, (f"宛先 {owner} 宛て。セッションは動いているが relay の起こすための記録が無い"
                                        "（relay の hook が無い可能性。tools/install.sh --hooks=relay）ので統括へ回した")
            elif not here:
                target, note = master, f"宛先 {owner} 宛て。不在（動いているセッションが無い）なので統括へ回した"
        if note:
            note += "。起こして渡すか、代わりに扱うかを決める。"
    # 受け取りが断ると分かっている（設定のこと）なら receive() を呼ばない（relay のログに同じ拒否を積まない）
    if not rconf["enabled"]:
        return False, "relay.enabled が false（受け取りを止めている）"
    if target not in rconf["receivers"]:
        return False, f"統括 {target} が relay.receivers に無い（受け取らない）"
    text = (note + "\n" + body) if note else body
    text = clip_bytes(text, rconf["max_bytes"])
    data = json.dumps({"body": text, "via": VIA}, ensure_ascii=False).encode("utf-8")
    line, rc = relay.receive(env, relay.LOCAL, f"recv {target}", data, now=now, local=True)
    return rc == 0, (f"{line}（{note}）" if note else line)


# ---- 調べる ---------------------------------------------------------------------------------

def child_env(env):
    """子のプロセス（when-cmd・gh・claude -p）の環境。Claude Code の中から check を呼んでも、子の claude が
    入れ子のセッション扱いにならず、kitty のペインにも触れないように、CLAUDE_CODE_*・CLAUDECODE・KITTY_* を外す。"""
    return {k: v for k, v in env.items()
            if not (k.startswith("CLAUDE_CODE_") or k == "CLAUDECODE" or k.startswith("KITTY_"))}


def run_cmd(env, cmd, cwd, timeout, run=run_group):
    """(成立したか, stdout, 説明)。終了コード 0 で成立。時間切れ・起動の失敗は成立しない。"""
    try:
        r = run(["/bin/sh", "-c", cmd], cwd=cwd if cwd and os.path.isdir(cwd) else None, timeout=timeout,
                env=child_env(env))
    except subprocess.TimeoutExpired:
        return False, "", f"{timeout:g} 秒で終わらない（時間切れ）"
    except OSError as e:
        return False, "", f"起動できない: {e}"
    return r.returncode == 0, r.stdout or "", f"終了コード {r.returncode}"


# ---- claude -p を切り離して動かす ----------------------------------------------------------------
#
# 常駐（check）は claude -p を待たない: .check.lock を持ったまま 30 分待つと、ほかの待ちの判定も届け直しも止まるため。
# check は「claude-waits _prompt-run <spec>」を新しいセッションで起こし、記録に running（pid・開始時刻・状態の
# ファイル）を書いて次へ進む。runner が claude を上限付きで動かし、出力を out/ に、結果を状態のファイルに書く。
# 次の回の check がそれを拾って届ける。runner が状態を書かずに居なくなっていたら（常駐ごと止まったなど）、
# 「中断した」と届け、再実行はしない（時刻の待ちは動かす前に next_at を進めてあるので、多くても 1 回）。

def prompt_runner(spec_path):
    """runner の本体（bin/claude-waits _prompt-run）。claude を新しいセッションで動かし、上限を超えたらグループごと止める。"""
    import relay
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    state = Path(spec["state"])
    result, rc = None, None
    try:
        with open(spec["prompt_file"], "rb") as fi, open(spec["out"], "ab") as fo:
            os.chmod(spec["out"], 0o600)
            p = subprocess.Popen(spec["argv"], stdin=fi, stdout=fo, stderr=fo, cwd=spec.get("cwd"), start_new_session=True)
            relay.write_private(state, json.dumps({"done": False, "child": p.pid}))
            try:
                rc = p.wait(timeout=spec["timeout"])
                result = f"終了コード {rc}"
            except subprocess.TimeoutExpired:
                killpg(p.pid)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    p.wait(timeout=KILL_WAIT)
                result = f"時間切れ（{spec['timeout']:g} 秒で終わらず止めた）"
            killpg(p.pid)
    except (OSError, ValueError) as e:
        result = f"{' '.join(spec['argv'])} を起動できない: {e}"
    relay.write_private(state, json.dumps({"done": True, "rc": rc, "result": result}))
    return 0


def start_prompt(env, conf, rec, now):
    """runner を起こして running（記録に書く dict）を返す。起こせなければ (None, 理由)。"""
    import proc
    import relay
    act = rec["action"]
    od = out_dir(env)
    mkdir_private(od)
    base = od / f"{rec['key']}-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}-{uuid.uuid4().hex[:6]}"
    out, state, spec_p = Path(f"{base}.txt"), Path(f"{base}.state.json"), Path(f"{base}.run.json")
    if not Path(act["prompt_file"]).is_file():
        return None, f"プロンプトのファイルが無い: {act['prompt_file']}"
    cwd = act.get("cwd") if os.path.isdir(act.get("cwd") or "") else None
    spec = {"argv": list(conf["claude"]), "prompt_file": act["prompt_file"], "cwd": cwd, "out": str(out),
            "state": str(state), "timeout": conf["prompt_timeout_seconds"]}
    relay.write_private(spec_p, json.dumps(spec, ensure_ascii=False))
    relay.write_private(out, "")
    try:
        p = subprocess.Popen([sys.executable, str(BIN), "_prompt-run", str(spec_p)], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=child_env(env),
                             start_new_session=True)
    except OSError as e:
        return None, f"runner を起動できない: {e}"
    _RUNNERS[p.pid] = p
    return {"pid": p.pid, "start": proc.start_time(p.pid), "started": now, "out": str(out), "state": str(state),
            "spec": str(spec_p), "timeout": conf["prompt_timeout_seconds"]}, None


def _reap(pid):
    """runner が常駐の子なら、終わったものを片付ける（ゾンビを生きていると見ないように）。"""
    p = _RUNNERS.get(pid)
    if p is not None:
        if p.poll() is not None:
            _RUNNERS.pop(pid, None)
        return
    with contextlib.suppress(ChildProcessError, OSError):
        os.waitpid(pid, os.WNOHANG)


def poll_running(env, conf, run_, now):
    """running の様子。("running", None) / ("done", 結果の 1 行) / ("interrupted", 理由) / ("timeout", 理由)。"""
    import relay
    try:
        st = json.loads(Path(run_["state"]).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        st = {}
    if isinstance(st, dict) and st.get("done"):
        return "done", str(st.get("result") or "?")
    pid = run_.get("pid")
    if not _int(pid):
        # 発火を書いてから runner を起こすまでの印（pending）。そのまま残っていれば、起こす前に常駐が止まった
        if now - float(run_.get("started") or now) > max(60, 2 * conf["poll_seconds"]):
            return "interrupted", "claude -p を起こす前に常駐が止まった。再実行はしない"
        return "running", None
    _reap(pid)
    if not relay.same_process(pid, run_.get("start")):
        try:  # 読んだ直後に runner が結果を書いて終わったのかもしれない
            st = json.loads(Path(run_["state"]).read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, TypeError):
            st = {}
        if isinstance(st, dict) and st.get("done"):
            return "done", str(st.get("result") or "?")
        return "interrupted", "claude -p の実行が中断した（常駐か runner が途中で止まった）。再実行はしない"
    limit = float(run_.get("timeout") or conf["prompt_timeout_seconds"]) + RUN_GRACE
    if now - float(run_.get("started") or now) > limit:
        if isinstance(st, dict) and _int(st.get("child")):
            killpg(st["child"])
        killpg(pid)
        return "timeout", f"時間切れ（{limit:g} 秒たっても終わらず、常駐が止めた）"
    return "running", None


def prompt_lines(conf, result, out):
    try:
        with open(out, "rb") as f:
            text = f.read(OUT_READ_MAX).decode("utf-8", "replace")
    except (OSError, TypeError):  # 起こす前に止まった（pending）なら出力のファイルは無い
        text = ""
    if not out:
        return [f"claude -p は動かなかった（{result}）"]
    return [f"claude -p で実行した（{result}）。出力: {out}",
            f"出力の先頭: {clean(text.strip(), conf['body_max'])}" if text.strip() else "出力は空"]


def evaluate(env, conf, rec, now, run=run_group):
    """成立したかを調べる（台帳は書かない）。(成立したか, 成立の説明, 添える行, 調べた結果の 1 行, 新しい条件 or None)。"""
    c = rec["cond"]
    if rec["kind"] == "cmd":
        ok, out, how = run_cmd(env, c["cmd"], c.get("cwd"), conf["cmd_timeout_seconds"], run=run)
        if not ok:
            return False, None, [], how, None
        extra = [f"出力の先頭: {clean(out.strip(), conf['body_max'])}"] if out.strip() else []
        return True, f"コマンド `{one_line(c['cmd'], 120)}` が成功した（終了コード 0）", extra, how, None
    if rec["kind"] == "gh_pr":
        try:
            now_counts = gh_counts(env, c["repo"], c["number"], conf, run=run)
        except OSError as e:
            return False, None, [], f"gh の失敗: {e}", None
        base = c.get("base") or {}
        grew = {k: v - int(base.get(k) or 0) for k, v in now_counts.items() if v > int(base.get(k) or 0)}
        if not grew:
            return False, None, [], "増えていない", None
        label = {"reviews": "レビュー", "comments": "コメント", "review_comments": "行へのコメント"}
        what = (f"GitHub PR {c['repo']}#{c['number']} に " + "・".join(f"{label[k]} {v}" for k, v in grew.items())
                + " 件が増えた")
        return True, what, [f"PR: https://github.com/{c['repo']}/pull/{c['number']}"], "増えた", None
    return False, None, [], "", None


def _outbox_item(owner, body, now, what):
    return {"id": uuid.uuid4().hex[:12], "owner": owner, "body": body, "t": now, "what": what}


def _commit(env, key, rev, fn, active=False):
    """台帳のロックの中で、記録を読み直して rev が同じなら fn(rec) を当てて書く（調べている間に add し直された・
    消された記録に、古い結果を書かない）。rev が None なら rev を比べない（outbox の id・running のように、
    登録し直しでも引き継ぐものを扱うとき）。書いたら記録、書かなければ None。"""
    with ledger_lock(env):
        rec = get_record(env, key)
        if rec is None or (rev is not None and rec.get("rev") != rev):
            return None
        if active and rec.get("status") != "active":
            return None  # 調べている間に done にされた（止めた待ちを発火・期限切れにしない）
        fn(rec)
        write_record(env, rec)
        return rec


def flush_outbox(env, conf, rec, now, alive=None, log=None):
    """記録の outbox を届ける。届いたものは id で消す（登録し直されて rev が変わっていても。二重に届けない）。
    届かなければ、その item の間を倍々に伸ばして（上限 RETRY_MAX）次に回す。同じ理由の失敗はログに 1 度だけ。"""
    for item in list(rec.get("outbox") or []):
        if not isinstance(item, dict) or not isinstance(item.get("body"), str):
            _commit(env, rec["key"], None, lambda r, x=item: r.update(outbox=[y for y in r.get("outbox") or [] if y != x]))
            continue
        if _num(item.get("next_try")) and now < item["next_try"]:
            continue
        try:
            ok, line = deliver(env, conf, item.get("owner"), item["body"], alive=alive)
        except Exception as e:  # noqa: BLE001 — 1 件の失敗で残りを止めない
            ok, line = False, f"{type(e).__name__}: {e}"
        line = clean(line, 300)
        if log and (ok or line != item.get("error")):
            log(f"{rec['key']}: {item.get('what') or '知らせ'} → {'届けた' if ok else '届かない'}: {line}")
        iid = item.get("id")
        if ok:
            _commit(env, rec["key"], None,
                    lambda r: r.update(outbox=[y for y in r.get("outbox") or [] if not (isinstance(y, dict) and y.get("id") == iid)],
                                       last_error=None))
            continue
        tries = int(item.get("tries") or 0) + 1
        wait = min(conf["poll_seconds"] * 2 ** min(tries, 20), RETRY_MAX)

        def failed(r):
            for y in r.get("outbox") or []:
                if isinstance(y, dict) and y.get("id") == iid:
                    y.update(tries=tries, next_try=now + wait, error=line)
            r["last_error"] = line
        _commit(env, rec["key"], None, failed)


def _add_outbox(r, owner, body, now, what):
    r["outbox"] = list(r.get("outbox") or []) + [_outbox_item(owner, body, now, what)]


def finish_running(env, conf, rec, now, log=None):
    """running の claude -p が終わったら（中断・時間切れも）知らせを outbox に積んで running を外す。"""
    run_ = rec["running"]
    state, why = poll_running(env, conf, run_, now)
    if state == "running":
        return
    result = why
    body = fire_body(rec, run_.get("what") or "?", list(run_.get("extra") or []) + prompt_lines(conf, result, run_.get("out")),
                     run_.get("tail"))

    def fin(r):
        if (r.get("running") or {}).get("pid") != run_.get("pid"):
            return
        r.pop("running", None)
        r["last_result"] = clean(result, 300)
        _add_outbox(r, r["role"], body, now, run_.get("label") or "実行")
    if _commit(env, rec["key"], None, fin) and log:
        log(f"{rec['key']}: claude -p が終わった（{result}）")
    for k in ("spec", "state"):  # 出力（.txt）は知らせが指すので残す（prune が keep_days で消す）
        with contextlib.suppress(OSError, TypeError, KeyError):
            os.unlink(run_[k])


def fire_action(env, conf, key, what, extra, tail, label, now, log=None):
    """発火のあとの動作: prompt なら runner を起こして running を書く（起こせなければその理由を届ける）。
    発火の commit で running に pending の印を書いてあるので、ここまでに常駐が止まっても「中断した」が届く。"""
    rec = get_record(env, key)
    if rec is None or (rec.get("action") or {}).get("kind") != "prompt":
        return
    try:
        run_, err = start_prompt(env, conf, rec, now)
    except Exception as e:  # noqa: BLE001 — 起こせなかった理由を届ける（pending のまま残さない）
        run_, err = None, f"{type(e).__name__}: {e}"
    if run_ is None:
        body = fire_body(rec, what, list(extra) + [f"claude -p を動かせなかった: {err}"], tail)

        def failed(r):
            r.pop("running", None)
            _add_outbox(r, r["role"], body, now, label)
        _commit(env, key, None, failed)
        return
    run_.update(what=what, extra=list(extra), tail=tail, label=label)
    if _commit(env, key, None, lambda r: r.update(running=run_)) is None:
        stop_running(run_)  # 記録が消された（rm）
    elif log:
        log(f"{key}: claude -p を動かし始めた（pid {run_['pid']}）")


def check_one(env, conf, rec, now, run=run_group, log=None):
    """1 件を調べ、要るなら発火・期限切れにして outbox に積む（届けるのは flush_outbox）。"""
    key, rev = rec["key"], rec["rev"]
    if rec.get("running"):
        finish_running(env, conf, rec, now, log=log)
        return  # 動いている間は、同じ待ちを重ねて発火させない
    if rec["status"] != "active":
        return
    if rec.get("expires") is not None and now >= rec["expires"]:
        def expire(r):
            r.update(status="expired", finished_at=now_iso(now), t_done=now)
            _add_outbox(r, None, expired_body(r), now, "期限切れ")
        if _commit(env, key, rev, expire, active=True) and log:
            log(f"{key}: 期限切れ（統括に知らせる）")
        return
    kind = rec["kind"]
    prompt = rec["action"].get("kind") == "prompt"
    if kind == "cloud":
        return  # クラウドのルーティンが動かす。台帳はポインタ（一覧と hook に出すだけ）
    if kind == "at":
        c = rec["cond"]
        nxt = rec.get("next_at")
        if not _num(nxt):
            _commit(env, key, rev, lambda r: r.update(next_at=next_occurrence(now, c["h"], c["m"], c["days"])))
            return
        if now < nxt:
            return
        latest = prev_occurrence(now, c["h"], c["m"], c["days"])
        upcoming = next_occurrence(now, c["h"], c["m"], c["days"])
        late = now - latest
        window = max(conf["catchup_hours"] * 3600, 2 * conf["poll_seconds"])  # 0 でも、間隔の遅れの分は定刻として届ける
        if late > window:
            # 止まっていた間の分が後追いの窓より古い: 発火せずに次の予定へ（何日分も溜めて一度に流さない）
            _commit(env, key, rev, lambda r: r.update(next_at=upcoming, last_result=f"見送り（予定 {when_ja(latest)} から "
                                                                                      f"{int(late // 60)} 分遅れ。後追いの窓の外）"))
            if log:
                log(f"{key}: 予定 {when_ja(latest)} を見送った（後追いの窓 {conf['catchup_hours']:g} 時間の外）")
            return
        what = f"予定の時刻 {days_ja(c['days'])} {c['at']}（{when_ja(latest)}）"
        if late > max(2 * conf["poll_seconds"], 300):
            what += f"。止まっていた間の分を 1 回だけ後追いで（{int(late // 60)} 分遅れ）"
        tail = f"定例なので次は {when_ja(upcoming)}。やめるなら claude-waits rm {key}"

        def fire(r):
            # 動かす前に next_at を進めて書く（常駐が途中で止まっても、同じ回を 2 度動かさない）
            r.update(next_at=upcoming, last_fired=now, fired=int(r.get("fired") or 0) + 1, last_result="発火")
            if prompt:
                r["running"] = {"pending": True, "started": now, "what": what, "extra": [], "tail": tail, "label": "発火"}
            else:
                _add_outbox(r, r["role"], fire_body(r, what, [], tail), now, "発火")
        if _commit(env, key, rev, fire, active=True):
            if log:
                log(f"{key}: 発火（{what}）")
            if prompt:
                fire_action(env, conf, key, what, [], tail, "発火", now, log=log)
        return
    # 出来事: every ごとに調べる
    every = float(rec["cond"].get("every") or DEFAULT_EVERY)
    last = rec.get("last_check")
    if _num(last) and now - last < every:
        return
    ok, what, extra, how, _ = evaluate(env, conf, rec, now, run=run)
    if not ok:
        _commit(env, key, rev, lambda r: r.update(last_check=now, last_result=clean(how, 300)))
        return
    tail = f"一度きりの待ちなので done にした（台帳から消すなら claude-waits rm {key}）"

    def fire(r):
        r.update(status="done", last_check=now, last_fired=now, fired=int(r.get("fired") or 0) + 1,
                 last_result="成立", finished_at=now_iso(now), t_done=now)
        if prompt:
            r["running"] = {"pending": True, "started": now, "what": what, "extra": list(extra), "tail": tail, "label": "成立"}
        else:
            _add_outbox(r, r["role"], fire_body(r, what, extra, tail), now, "成立")
    if _commit(env, key, rev, fire, active=True):
        if log:
            log(f"{key}: 成立（{what}）")
        if prompt:
            fire_action(env, conf, key, what, extra, tail, "成立", now, log=log)


def prune(env, conf, now, recs):
    """done・expired で、届け残りも動いているものも無く、keep_days より前に終わったものを消す（台帳が積もらないように）。"""
    keep = conf["keep_days"] * 86400
    for r in recs:
        if (r["status"] in ("done", "expired") and not r.get("outbox") and not r.get("running")
                and now - float(r.get("t_done") or now) > keep):
            with ledger_lock(env):
                cur = get_record(env, r["key"])
                if cur and cur.get("rev") == r.get("rev") and cur["status"] == r["status"] and not cur.get("running"):
                    with contextlib.suppress(FileNotFoundError):
                        record_path(env, r["key"]).unlink()
    # claude -p の出力も同じだけ残す（毎日の定例で 1 日 1 つずつ積もるため）。届いた知らせが指す間は消さないよう 1 日は残す
    live = set()
    for r in recs:
        for k in ("spec", "state"):
            if isinstance(r.get("running"), dict) and isinstance(r["running"].get(k), str):
                live.add(r["running"][k])
    try:
        # runner の spec・状態は、どの記録の running も指していなければ（rm・常駐が止まった）同じだけ残して消す
        outs = list(out_dir(env).glob("*.txt")) + [f for f in out_dir(env).glob("*.json") if str(f) not in live]
    except OSError:
        outs = []
    for f in outs:
        with contextlib.suppress(OSError):
            if now - f.stat().st_mtime > max(keep, 86400):
                f.unlink()


def report_bad(env, names, log):
    """読めない記録を、そのファイルが変わるまで 1 度だけログに出す（毎回の poll で同じ行を積まない）。"""
    d = ledger_dir(env)
    for n in names:
        try:
            m = (d / n).stat().st_mtime
        except OSError:
            continue
        if _REPORTED_BAD.get(str(d / n)) != m:
            _REPORTED_BAD[str(d / n)] = m
            if log:
                log(f"読めない記録を飛ばした（直すか消す）: {d / n}")


def check(env, now=None, run=run_group, alive=None, log=None):
    """台帳を 1 回調べて、成立・定刻・期限切れを届ける。別の check が動いていれば何もしないで False。"""
    from config import load_waits_config
    now = time.time() if now is None else now
    conf = load_waits_config(env)
    if not conf["enabled"]:
        return True
    with ledger_lock(env, name=".check.lock", wait=False) as got:
        if not got:
            if log:
                log("別の check が動いている（この回は飛ばす）")
            return False
        for pid in list(_RUNNERS):
            _reap(pid)  # 終わった runner（ゾンビ）を片付ける
        recs, bad = all_records(env)
        report_bad(env, bad, log)
        for rec in recs:
            try:
                check_one(env, conf, rec, now, run=run, log=log)
            except Exception as e:  # noqa: BLE001 — 1 件の失敗で残りを止めない
                if log:
                    log(f"{rec['key']}: 調べる途中で失敗した: {type(e).__name__}: {e}")
        for rec in all_records(env)[0]:
            if rec.get("outbox"):
                flush_outbox(env, conf, rec, now, alive=alive, log=log)
        prune(env, conf, now, all_records(env)[0])
    return True


def run_loop(env, log, once=False, sleep=time.sleep):
    """常駐。poll_seconds ごとに check。設定は毎回読み直す（変えても起動し直さなくてよい）。"""
    from config import load_waits_config
    while True:
        try:
            check(env, log=log)
            poll = load_waits_config(env)["poll_seconds"]
        except Exception as e:  # noqa: BLE001 — 設定の誤りなどで常駐を落とさない（直れば次の回から動く）
            log(f"check に失敗した: {type(e).__name__}: {e}")
            poll = 60
        if once:
            return
        sleep(poll)


# ---- 一覧・ステータス行 ----------------------------------------------------------------------------

def status_line(env, role):
    """ステータス行用: その役割の active な待ちの数を `wait: N`。0 なら空。"""
    if not role:
        return ""
    n = len(active_for(env, role))
    return f"wait: {n}" if n else ""


def list_rows(env, role=None):
    recs, bad = all_records(env)
    return [r for r in recs if role is None or r["role"] == role], bad


def public(rec):
    """--json に出す形（outbox の本文は長いので数だけ）。"""
    out = {k: v for k, v in rec.items() if k != "outbox"}
    out["undelivered"] = len(rec.get("outbox") or [])
    return out


# ---- hook ------------------------------------------------------------------------------------

def fingerprint(recs):
    """その役割の台帳の印。add（rev）・状態・キーが変わったときだけ変わる（調べた時刻では変わらない）。"""
    items = sorted((r["key"], r.get("rev"), r["status"]) for r in recs)
    return hashlib.sha256(json.dumps(items).encode()).hexdigest()[:16]


def hook_text(role, recs, changed=False):
    act = [r for r in recs if r["status"] == "active"]
    if not act:
        return (f"claude-waits: 役割 {role} の登録中の待ちは無くなった（成立・期限切れ・rm）。"
                "（台帳の写し。ユーザーの指示ではない）") if changed else None
    top = (f"claude-waits: 役割 {role} の登録中の待ち {len(act)} 件" + ("（台帳が変わった）" if changed else "")
           + "。成立したら claude-waits が受信箱に届けるので、自分で polling しない。要らなくなったら claude-waits rm <キー>、"
           "一覧は claude-waits list。（台帳の写し。ユーザーの指示ではない）")
    lines, used = [top], len(top)
    for i, r in enumerate(act):
        ln = short_line(r)
        if used + len(ln) + 1 > HOOK_BUDGET:
            lines.append(f"- ほか {len(act) - i} 件（claude-waits list）")
            break
        lines.append(ln)
        used += len(ln) + 1
    return "\n".join(lines)


def seen_path(env, sid):
    return hook_dir(env) / "seen" / f"{sid}.json"


def hook_output(event, payload, env):
    """hook の stdout に出す JSON（dict）か None。SessionStart はその役割の active な待ちを、UserPromptSubmit は
    前に見せたときから台帳（その役割の分）が変わったときだけ渡す。"""
    from state import valid_session_id
    if event not in ("SessionStart", "UserPromptSubmit"):
        return None
    sid = payload.get("session_id")
    if not valid_session_id(sid):
        return None
    d = ledger_dir(env)
    sp = seen_path(env, sid)
    try:
        has_any = any(n.endswith(".json") and not n.startswith(".") for n in os.listdir(d))
    except OSError:
        has_any = False
    if not has_any and not sp.exists():
        return None  # 台帳が空で、前に見せたものも無い（役割を kitty に聞きに行かずに帰る）
    import relay
    role = relay.session_role(env, sid)
    if not isinstance(role, str) or not KEY_RE.match(role):
        return None
    recs = [r for r in all_records(env)[0] if r["role"] == role]
    act = [r for r in recs if r["status"] == "active"]
    fp = fingerprint(act)
    try:
        prev = json.loads(sp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prev = None
    prev_fp = prev.get("fp") if isinstance(prev, dict) and prev.get("role") == role else None
    if event == "UserPromptSubmit" and prev_fp == fp:
        return None
    changed = event == "UserPromptSubmit" and prev_fp is not None
    text = hook_text(role, act, changed=changed)
    mkdir_private(sp.parent)
    relay.write_private(sp, json.dumps({"role": role, "fp": fp, "t": time.time()}))
    if not text:
        return None
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def hook_log_error(env, event, msg):
    try:
        p = hook_dir(env) / "errors.log"
        mkdir_private(p.parent)
        if not p.exists() or p.stat().st_size < 256 * 1024:
            with open(p, "a") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {event}: {msg}\n")
    except BaseException:  # noqa: BLE001
        pass
