"""人がいるか（最後に人が打ってからの時間）と、離席中のサブエージェントのモデルの切り替え（claude-presence・claude-state）。

- 記録: claude-state の UserPromptSubmit の hook が、人の入力のときだけ `<置き場>/claude-presence/last.json` に
  時刻・セッション・役割を書く（全セッション共通の 1 つ。置き場は lib/rundir.py の runtime_dir）。
- 切り替え: claude-presence の PreToolUse の hook（Agent ツール）が、設定 model_policy が有効で、最後の人の入力から
  away_minutes 分以上たっていれば、実装の呼び出しを断り（permissionDecision: deny）、model を away_model にして
  出し直すよう理由の文で頼む。入力は書き換えない（updatedInput は auto mode の判定を無効にし、同じ返答の
  ほかの呼び出しまで断られるため）。断ったら `<置き場>/claude-presence/denials.jsonl` に、出し直し（model が
  away_model のもの）が通ったら `<置き場>/claude-presence/switches.jsonl` に残す（切り替えが実際に起きた記録）。
離席中の作業はゆっくりでよく（トークンが少ない方がよい）、人がいるときは速さを優先する、という利用者の要望から。
"""
import hashlib
import json
import os
import re
import tempfile
import time

# 人が打ったものでない入力の見分け方（実測した形）。
# 頭に来るもの: 背景の作業の完了の通知、Claude Code のセッション間メッセージ、サブエージェントの報告、システムの注記
MACHINE_PREFIXES = ("<task-notification", "<cross-session-message", "<agent-message", "<system-reminder")
# 中にあれば人でないもの。claude-kitty のツールが Claude Code の socket に直接送る文には、必ずこのどれかを入れる
# （claude-relay の起こす知らせ = lib/relay.py の wake_text、ディスクの見張り = lib/diskwatch.py の MARK。
# 足したら tests の照合も足す）。
# <cross-session-message は頭でなくても（前に空白・別の文があっても）人でないとみる（claude-state の判定と同じ）
MACHINE_MARKS = ("[relay-wake ids=", "<cross-session-message", "[claude-kitty disk]")

AGENT_TOOLS = ("Agent", "Task")  # Task は Agent ツールの古い名前
# 止めない subagent_type（調べもの・設計・ガイド・自分の写し）と model
SKIP_TYPES = ("claude-code-guide", "Explore", "Plan", "web-fetch", "fork")
SKIP_MODELS = ("fable",)
DEFAULT_KEEP = ["review", "レビュー", "評価", "verify", "検証", "調べ", "guide"]
DEFAULT_MODEL_POLICY = {"enabled": False, "away_minutes": 20, "away_model": "haiku", "keep": DEFAULT_KEEP}

DIR_NAME = "claude-presence"
LOG_MAX = 256 * 1024  # これを超えたら switches.jsonl.1（denials.jsonl.1）に回す
SWITCHES = "switches.jsonl"  # 出し直しが away_model で通った（切り替わった）記録。claude-presence が直近を見せる
DENIALS = "denials.jsonl"    # 断った記録。出し直しの見分けに使う
# 同じセッションの同じ prompt を、これだけの秒数の間に断っていたら、もう断らない。Claude が元の model のまま
# 出し直したとき（従わなかった・人が model を指定した）に、断りをくり返して作業を止めないため（2 回目は通す）。
# away_model での出し直しを切り替えとして記録するのも、この間に断ったものだけ
RETRY_SECONDS = 300


def presence_dir(env=None):
    from rundir import runtime_dir
    return runtime_dir(env) / DIR_NAME


def is_human(prompt, notice=None):
    """UserPromptSubmit の prompt が人の入力か。空・文字列でない・機械の印があるものは人でない。

    notice は claude-state の設定 state.notice（通知とみなす印）。当たれば人でない。"""
    if not isinstance(prompt, str) or not prompt.strip():
        return False
    if prompt.lstrip().startswith(MACHINE_PREFIXES) or any(m in prompt for m in MACHINE_MARKS):
        return False
    if notice:
        from state import is_notice
        if is_notice(prompt, notice):
            return False
    return True


def _write_private(path, text):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)  # 読む側（hook）が書きかけを読まない
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_human(env, session_id, role=None, cwd=None, now=None):
    """人が打った時刻を書く（前の記録は置き換える）。"""
    now = time.time() if now is None else now
    rec = {"v": 1, "at": now, "session_id": session_id, "role": role, "cwd": cwd}
    _write_private(presence_dir(env) / "last.json", json.dumps(rec, ensure_ascii=False))
    return rec


def load_last(env):
    """最後の人の入力の記録。無い・壊れていれば None。"""
    try:
        rec = json.loads((presence_dir(env) / "last.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(rec, dict):
        return None
    at = rec.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    return rec


def idle_minutes(last, now):
    """最後の人の入力からの分数。記録が無ければ None（時計が戻って未来の記録なら 0）。"""
    if not last:
        return None
    return max(0.0, (now - last["at"]) / 60.0)


def is_away(last, now, policy):
    m = idle_minutes(last, now)
    return m is not None and m >= policy["away_minutes"]


def keep_reason(tool_input, policy):
    """止めない理由（無ければ None）。"""
    st = tool_input.get("subagent_type")
    if isinstance(st, str) and st in SKIP_TYPES:
        return f"subagent_type {st}"
    model = tool_input.get("model")
    if isinstance(model, str) and model in SKIP_MODELS:
        return f"model {model}"
    if model == policy["away_model"]:
        return "already"
    text = " ".join(x for x in (tool_input.get("description"), tool_input.get("prompt")) if isinstance(x, str))
    for w in policy["keep"]:
        if keep_hit(w, text):
            return f"keep {w}"
    return None


def keep_hit(word, text):
    """keep の語が text にあるか。英数字だけの語は単語として（大文字・小文字を問わず。guide は guideline に当てない）、
    ほかは部分一致（日本語には単語の区切りが無いため）。"""
    if re.fullmatch(r"[A-Za-z0-9_-]+", word):
        return re.search(rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])", text, re.I) is not None
    return word.lower() in text.lower()


def prompt_key(prompt):
    return hashlib.sha256(prompt.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _recent_match(records, sid, key, now):
    """records（古い順）のうち、同じセッションの同じ prompt で RETRY_SECONDS 以内の最後のもの（無ければ None）。"""
    for r in reversed(records):
        t = r.get("t")
        if not isinstance(t, (int, float)) or isinstance(t, bool) or now - t > RETRY_SECONDS:
            continue
        if r.get("session_id") == sid and r.get("prompt_key") == key:
            return r
    return None


def recent_denial(env, sid, prompt, now):
    """同じセッションの同じ prompt を RETRY_SECONDS 以内に断ったなら、その記録（出し直しの呼び出し）。

    件数でなく時間で絞る（ほかのセッションの断りが多くても見失わない）。記録があるのに読めなければ OSError を出す
    （呼ぶ側は断らずに通す。読めないまま断ると、2 度目を見分けられず断り続けるため）。"""
    return _recent_match(read_log(env, DENIALS, None, since=now - RETRY_SECONDS, strict=True), sid, prompt_key(prompt), now)


def decide(payload, policy, last, now):
    """PreToolUse の入力から、断るなら {"from", "to", "idle_minutes", "description"}、通すなら None。"""
    if not policy or not policy.get("enabled"):
        return None
    if not isinstance(payload, dict) or payload.get("tool_name") not in AGENT_TOOLS:
        return None
    ti = payload.get("tool_input")
    if not isinstance(ti, dict) or not isinstance(ti.get("prompt"), str):
        return None  # Agent の形でない（同じ名前の別のツールなど）
    if not is_away(last, now, policy):
        return None  # 在席か、一度も人が打っていない（記録が無い）
    if keep_reason(ti, policy):
        return None
    old = ti.get("model") if isinstance(ti.get("model"), str) else None
    return {"from": old, "to": policy["away_model"], "idle_minutes": idle_minutes(last, now),
            "description": ti.get("description") if isinstance(ti.get("description"), str) else None}


def hook_output(d):
    """PreToolUse の hook の stdout。入力は書き換えず（updatedInput は付けない）、断りの判定と理由だけを返す。"""
    msg = (f"claude-presence: 人の入力が {int(d['idle_minutes'])} 分なく離席中なので、この呼び出しは止めた。"
           f"model を {d['to']} にして同じ呼び出しを出し直してください（ほかの項目はそのまま。"
           f"今の model: {d['from'] or '（指定なし）'}。設定 model_policy: 離席中の実装はトークンの少ない方で）。")
    # 「元の model のまま出し直せば通る」とは書かない（毎回そうされると切り替わらない。2 度目を通すのは止めないための安全弁）
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": msg}}


def append_log(env, name, entry):
    p = presence_dir(env) / name
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        if p.stat().st_size > LOG_MAX:
            os.replace(p, p.with_name(p.name + ".1"))
    except FileNotFoundError:
        pass
    try:
        fd, readable = os.open(str(p), os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600), True
    except PermissionError:  # 書けるが読めない: 末尾を確かめずに書く（書けるのに書かない、にしない）
        fd, readable = os.open(str(p), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), False
    with os.fdopen(fd, "r+b" if readable else "ab") as f:
        # 前の書き込みが改行の前で切れていたら、この記録がその行につながって読めなくならないよう区切る
        head = b""
        if readable and f.seek(0, os.SEEK_END) > 0:
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                head = b"\n"
        f.write(head + (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8"))


def append_switch(env, entry):
    append_log(env, SWITCHES, entry)


def read_log(env, name, n, since=None, strict=False):
    """記録（古い順。回した .1 も含む）の最後の n 件（None なら全部）。since があれば t がそれ以後のものだけ。
    strict なら、無いのでなく読めないファイルで OSError を出す。"""
    p = presence_dir(env) / name
    out = []
    for name in (p.name + ".1", p.name):
        try:
            # 書きかけで切れた行が 1 つあっても、ほかの行は読む（断りの記録を見失うと断りがくり返される）
            lines = (p.parent / name).read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            continue
        except OSError:
            if strict:
                raise
            continue
        for line in lines:
            try:
                j = json.loads(line)
            except ValueError:
                continue
            if not isinstance(j, dict):
                continue
            if since is not None:
                t = j.get("t")
                if isinstance(t, bool) or not isinstance(t, (int, float)) or t < since:
                    continue
            out.append(j)
    if n is None:
        return out
    return out[-n:] if n > 0 else []


def denial_counts(env):
    """記録にある断りの数と、そのうち away_model での出し直しが通った（切り替わった）数。"""
    denials = read_log(env, DENIALS, None)
    done = {(r.get("session_id"), r.get("prompt_key"), r.get("denied_at")) for r in read_log(env, SWITCHES, None)}
    switched = sum(1 for d in denials if (d.get("session_id"), d.get("prompt_key"), d.get("at")) in done)
    return {"denied": len(denials), "switched": switched}


def recent_switches(env, n=5):
    return read_log(env, SWITCHES, n)


def log_error(env, where, msg):
    try:
        log = presence_dir(env) / "errors.log"
        log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not log.exists() or log.stat().st_size < 256 * 1024:
            with open(log, "a") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {where}: {msg}\n")
    except BaseException:  # noqa: BLE001
        pass


def session_role(env, sid):
    """claude-state の記録の役割（無ければ None）。"""
    from kstate import load, state_dir, state_file
    from state import valid_session_id
    if not valid_session_id(sid):
        return None
    rec = load(state_file(state_dir(env), sid))
    r = (rec or {}).get("role")
    return r if isinstance(r, str) else None


def _entry(env, payload, now, sid, **kw):
    return dict({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)), "t": now,
                 "session_id": sid, "role": session_role(env, sid), "agent_id": payload.get("agent_id")}, **kw)


def note_retry(env, payload, policy, now):
    """away_model での出し直しなら、切り替えとして switches.jsonl に残す（前に断った記録から元の model を写す）。"""
    ti = payload.get("tool_input")
    if not isinstance(ti, dict) or ti.get("model") != policy["away_model"] or not isinstance(ti.get("prompt"), str):
        return
    sid = payload.get("session_id")
    d = recent_denial(env, sid, ti["prompt"], now)
    if d is None:
        return
    key = prompt_key(ti["prompt"])
    if _recent_match(read_log(env, SWITCHES, None, since=d["t"]), sid, key, now):
        return  # この断りの出し直しは記録済み（同じものを 2 度出し直した）
    append_switch(env, _entry(env, payload, now, sid, **{k: d.get(k) for k in ("from", "description", "idle_minutes")},
                              to=ti["model"], prompt_key=key, denied_at=d.get("at")))


def run_hook(text, env, now=None, policy_loader=None):
    """PreToolUse の hook の本体。断るときだけ出力の dict を返す。例外は呼ぶ側で握りつぶす。"""
    now = time.time() if now is None else now
    if policy_loader is None:
        from config import load_model_policy
        policy_loader = load_model_policy
    try:
        payload = json.loads(text) if text and text.strip() else {}
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("tool_name") not in AGENT_TOOLS:
        return None  # 設定を読む前に帰る（matcher が広く書かれていても速く）
    try:
        policy = policy_loader(env)
    except ValueError as e:
        log_error(env, "hook", f"設定を読めないので何もしない: {e}")
        return None
    if not policy["enabled"]:
        return None
    d = decide(payload, policy, load_last(env), now)
    if d is None:
        try:
            note_retry(env, payload, policy, now)
        except Exception as e:  # noqa: BLE001 — 記録が読めない・書けなくても呼び出しは通す
            log_error(env, "hook", f"出し直しを {SWITCHES} に残せない（{DENIALS} が読めないか、{SWITCHES} に書けない）: "
                                   f"{type(e).__name__}: {e}")
        return None
    sid = payload.get("session_id")
    try:
        if recent_denial(env, sid, payload["tool_input"]["prompt"], now):
            return None  # 断ったのに元の model のまま出し直した: もう断らない
    except OSError as e:
        log_error(env, "hook", f"{DENIALS} が読めないので断らない: {type(e).__name__}: {e}")
        return None
    try:
        append_log(env, DENIALS, _entry(env, payload, now, sid, **{k: d[k] for k in ("from", "to", "description")},
                                        idle_minutes=round(d["idle_minutes"], 1),
                                        prompt_key=prompt_key(payload["tool_input"]["prompt"])))
    except Exception as e:  # noqa: BLE001
        # 記録が書けないと 2 度目を見分けられず、断りがくり返しうる。それよりは切り替えずに通す
        log_error(env, "hook", f"{DENIALS} に書けないので断らない: {type(e).__name__}: {e}")
        return None
    return hook_output(d)
