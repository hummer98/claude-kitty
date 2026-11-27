"""後から数えるための記録（1 行 1 件の JSON）。hook から書くので、決して失敗させず、軽く保つ。

  置き場  $XDG_STATE_HOME/claude-kitty/（既定 ~/.local/state/claude-kitty/。フォルダ 0700、ファイル 0600）
    guard/blocks.jsonl        claude-guard が止めた（deny した）呼び出し
    state/transitions.jsonl   claude-state の状態の移り（working / waiting / done / idle / end）
    state/messages.jsonl      ほかのセッションから届いたメッセージ（UserPromptSubmit で見えたもの）
    state/tasks.jsonl         タスクリストの札の作成・更新（PostToolUse の TaskCreate / TaskUpdate）
  1 つが LOG_MAX を超えたら <名前>.1 に回す（前の .1 は消える）。読み方と数え方は docs/records.md。

置き場は env の XDG_STATE_HOME か HOME からだけ決める（どちらも無ければ書かない）。hook を偽の環境で動かすテストが、
手元の本物の置き場に書かないようにするため。
"""
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

LOG_MAX = 1024 * 1024
FILES = {
    "guard-block": ("guard", "blocks.jsonl"),
    "transition": ("state", "transitions.jsonl"),
    "msg-in": ("state", "messages.jsonl"),
    "task": ("state", "tasks.jsonl"),
}
CMD_MAX = 200      # guard-block の cmd（コマンドの頭）
SUBJECT_MAX = 120  # task の subject（札を数えるのに頭があれば足りる）
TASK_TOOLS = ("TaskCreate", "TaskUpdate")


def root(env):
    """記録の置き場（claude-kitty のフォルダ）か None。"""
    if env.get("XDG_STATE_HOME"):
        return Path(env["XDG_STATE_HOME"]) / "claude-kitty"
    if env.get("HOME"):
        return Path(env["HOME"]) / ".local" / "state" / "claude-kitty"
    return None


def path_of(env, kind):
    r = root(env)
    return r / FILES[kind][0] / FILES[kind][1] if r else None


def local_time(t):
    """ローカル時刻の ISO 8601（秒まで、+09:00 の形の時差付き）。"""
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


def _mkdir_private(d):
    """無いフォルダを 1 つずつ 0700 で作る（parents=True の親は umask のまま 755 になるため）。"""
    missing = []
    while not d.is_dir():
        missing.append(d)
        if d.parent == d:
            break
        d = d.parent
    for m in reversed(missing):
        try:
            m.mkdir(mode=0o700)
        except FileExistsError:
            if not m.is_dir():
                raise


def append(env, kind, entry, now=None):
    """記録に 1 行足す。"at" が無ければ now（既定は今）のローカル時刻を頭に入れる。書けたら True。

    何があっても例外を出さない（置き場が書けない・ファイルでないなどは黙って飛ばす）。"""
    try:
        p = path_of(env, kind)
        if p is None:
            return False
        if "at" not in entry:
            entry = dict({"at": local_time(time.time() if now is None else now)}, **entry)
        _mkdir_private(p.parent)
        try:
            if p.stat().st_size > LOG_MAX:
                os.replace(p, p.with_name(p.name + ".1"))
        except FileNotFoundError:
            pass
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except Exception:  # noqa: BLE001  hook の中で呼ぶので、記録の失敗で hook を落とさない
        return False


def note_transition(env, sid, role, before, after, reason=None, now=None):
    """状態の移りを state/transitions.jsonl に 1 行。waiting なら理由（manual・ask・permission・notify・relay）も。"""
    entry = {"at": local_time(time.time() if now is None else now), "role": role, "sid": sid, "from": before, "to": after}
    if after == "waiting" and reason:
        entry["reason"] = reason
    return append(env, "transition", entry)


def clip(text, n):
    return text if len(text) <= n else text[:n]


# ---- ほかのセッションからのメッセージ ------------------------------------------------

_PEER = re.compile(r"<cross-session-message\b([^>]*)>(.*?)</cross-session-message>", re.S)
_ATTR = re.compile(r'([A-Za-z][\w-]*)="([^"]*)"')
_WAKE = re.compile(r"\[relay-wake ids=([0-9A-Za-z._,-]{1,8192})\]")
_WAKE_FROM = re.compile(r"claude-relay: (.+?) ?から \d+ 通")


def message_entries(prompt):
    """prompt に入っている、ほかのセッションからのメッセージの [{"from", "via", "bytes"[, "n"]}]（無ければ空）。

    <cross-session-message> の 1 つずつ（from-name、無ければ from）。中に claude-relay の起こす知らせの印
    （[relay-wake ids=…]）があれば via は relay で、from は知らせの「<送り元>から」（無ければ枠の送り元）、
    n は印の id の数（知らせ 1 つで何通届いたか）、bytes は知らせの文の長さ（本文は受信箱にある）。
    枠の無い印だけのものも relay として数える。"""
    if not isinstance(prompt, str) or ("<cross-session-message" not in prompt and "[relay-wake ids=" not in prompt):
        return []
    out = []
    for m in _PEER.finditer(prompt):
        attrs = dict(_ATTR.findall(m.group(1)))
        body = m.group(2)
        out.append(_relay_entry(body, attrs.get("from-name") or attrs.get("from")) or
                   {"from": attrs.get("from-name") or attrs.get("from"), "via": "cross-session",
                    "bytes": len(body.strip().encode())})
    if not out:
        e = _relay_entry(prompt, None)
        if e:
            out.append(e)
    return out


def _relay_entry(text, frame_from):
    m = _WAKE.search(text)
    if not m:
        return None
    w = _WAKE_FROM.search(text)
    return {"from": (w.group(1) if w else None) or frame_from, "via": "relay", "bytes": len(text.strip().encode()),
            "n": len([x for x in m.group(1).split(",") if x])}


# ---- タスクリストの札 ------------------------------------------------------------------

def _s(v):
    return v if isinstance(v, str) and v else None


def task_entry(payload):
    """PostToolUse の TaskCreate / TaskUpdate から {"id", "subject", "status", "op"}。読めなければ None。

    tool_input と tool_response の両方を見る（create の id は tool_response.task.id にしか無い。update の
    status は tool_input.status か tool_response.statusChange.to。変えていない項目は null）。"""
    tool = payload.get("tool_name")
    if tool not in TASK_TOOLS:
        return None
    ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    tr = payload.get("tool_response") if isinstance(payload.get("tool_response"), dict) else {}
    if tr.get("success") is False:
        return None  # 変わらなかった（無い id など）
    if tool == "TaskCreate":
        task = tr.get("task") if isinstance(tr.get("task"), dict) else {}
        tid = _s(task.get("id")) or _s(ti.get("taskId"))
        subject = _s(ti.get("subject")) or _s(task.get("subject"))
        status = _s(ti.get("status")) or _s(task.get("status")) or "pending"  # 作ったばかりの札は pending
        op = "create"
    else:
        sc = tr.get("statusChange") if isinstance(tr.get("statusChange"), dict) else {}
        tid = _s(ti.get("taskId")) or _s(tr.get("taskId"))
        subject = _s(ti.get("subject"))
        status = _s(ti.get("status")) or _s(sc.get("to"))
        op = "update"
    if tid is None and subject is None:
        return None
    return {"id": tid, "subject": clip(subject, SUBJECT_MAX) if subject else None, "status": status, "op": op}
