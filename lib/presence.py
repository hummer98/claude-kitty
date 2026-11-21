"""人がいるか（最後に人が打ってからの時間）と、離席中のサブエージェントのモデルの切り替え（claude-presence・claude-state）。

- 記録: claude-state の UserPromptSubmit の hook が、人の入力のときだけ `<置き場>/claude-presence/last.json` に
  時刻・セッション・役割を書く（全セッション共通の 1 つ。置き場は lib/rundir.py の runtime_dir）。
- 切り替え: claude-presence の PreToolUse の hook（Agent ツール）が、設定 model_policy が有効で、最後の人の入力から
  away_minutes 分以上たっていれば、実装の呼び出しの model を away_model に書き換える（updatedInput）。
  書き換えたら `<置き場>/claude-presence/switches.jsonl` に残す。
離席中の作業はゆっくりでよく（トークンが少ない方がよい）、人がいるときは速さを優先する、という利用者の要望から。
"""
import json
import os
import tempfile
import time

# 人が打ったものでない入力の見分け方（実測した形）。
# 頭に来るもの: 背景の作業の完了の通知、Claude Code のセッション間メッセージ
MACHINE_PREFIXES = ("<task-notification", "<cross-session-message")
# 中にあれば人でないもの。claude-kitty の道具が Claude Code の socket に直接送る文には、必ずこのどれかを入れる
# （claude-relay の起こす知らせ = lib/relay.py の wake_text。足したら tests の照合も足す）。
# <cross-session-message は頭でなくても（前に空白・別の文があっても）人でないとみる（claude-state の判定と同じ）
MACHINE_MARKS = ("[relay-wake ids=", "<cross-session-message")

AGENT_TOOLS = ("Agent", "Task")  # Task は Agent ツールの古い名前
# 書き換えない subagent_type（調べもの・設計・ガイド・自分の写し）と model
SKIP_TYPES = ("claude-code-guide", "Explore", "Plan", "web-fetch", "fork")
SKIP_MODELS = ("fable",)
DEFAULT_KEEP = ["review", "レビュー", "評価", "verify", "検証", "調べ", "guide"]
DEFAULT_MODEL_POLICY = {"enabled": False, "away_minutes": 20, "away_model": "haiku", "keep": DEFAULT_KEEP}

DIR_NAME = "claude-presence"
LOG_MAX = 256 * 1024  # これを超えたら switches.jsonl.1 に回す


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
    """書き換えない理由（無ければ None）。"""
    st = tool_input.get("subagent_type")
    if isinstance(st, str) and st in SKIP_TYPES:
        return f"subagent_type {st}"
    model = tool_input.get("model")
    if isinstance(model, str) and model in SKIP_MODELS:
        return f"model {model}"
    if model == policy["away_model"]:
        return "already"
    text = " ".join(x for x in (tool_input.get("description"), tool_input.get("prompt")) if isinstance(x, str)).lower()
    for w in policy["keep"]:
        if w.lower() in text:
            return f"keep {w}"
    return None


def decide(payload, policy, last, now):
    """PreToolUse の入力から、書き換えるなら {"input", "from", "to", "idle_minutes", "description"}、しないなら None。"""
    if not policy or not policy.get("enabled"):
        return None
    if not isinstance(payload, dict) or payload.get("tool_name") not in AGENT_TOOLS:
        return None
    ti = payload.get("tool_input")
    if not isinstance(ti, dict) or not isinstance(ti.get("prompt"), str):
        return None  # Agent の形でない（同じ名前の別の道具など）
    if not is_away(last, now, policy):
        return None  # 在席か、一度も人が打っていない（記録が無い）
    if keep_reason(ti, policy):
        return None
    new = dict(ti, model=policy["away_model"])  # updatedInput は入力の全体を置き換えるので、ほかの項目も写す
    old = ti.get("model") if isinstance(ti.get("model"), str) else None
    return {"input": new, "from": old, "to": policy["away_model"], "idle_minutes": idle_minutes(last, now),
            "description": ti.get("description") if isinstance(ti.get("description"), str) else None}


def hook_output(d):
    """PreToolUse の hook の stdout（permissionDecision は付けない: 許可の判断は今までどおり）。"""
    msg = (f"claude-presence: 人の入力が {int(d['idle_minutes'])} 分ないので、このサブエージェントの model を "
           f"{d['from'] or '（指定なし）'} から {d['to']} に変えた（設定 model_policy。離席中の実装はトークンの少ない方で）。"
           f"auto mode で「a hook changed this call's input」と断られたら、同じ呼び出しを model: \"{d['to']}\" にして出し直す。")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": d["input"], "additionalContext": msg}}


def append_switch(env, entry):
    p = presence_dir(env) / "switches.jsonl"
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        if p.stat().st_size > LOG_MAX:
            os.replace(p, p.with_name(p.name + ".1"))
    except FileNotFoundError:
        pass
    fd = os.open(str(p), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def recent_switches(env, n=5):
    p = presence_dir(env) / "switches.jsonl"
    out = []
    for name in (p.name + ".1", p.name):
        try:
            lines = (p.parent / name).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            try:
                j = json.loads(line)
            except ValueError:
                continue
            if isinstance(j, dict):
                out.append(j)
    return out[-n:] if n > 0 else []


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


def run_hook(text, env, now=None, policy_loader=None):
    """PreToolUse の hook の本体。書き換えるときだけ出力の dict を返す。例外は呼ぶ側で握りつぶす。"""
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
        return None
    sid = payload.get("session_id")
    try:
        append_switch(env, {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)), "t": now,
                            "session_id": sid, "role": session_role(env, sid), "agent_id": payload.get("agent_id"),
                            "from": d["from"], "to": d["to"], "description": d["description"],
                            "idle_minutes": round(d["idle_minutes"], 1)})
    except Exception as e:  # noqa: BLE001 — ログが書けなくても切り替えは行う
        log_error(env, "hook", f"switches.jsonl に書けない: {type(e).__name__}: {e}")
    return hook_output(d)
