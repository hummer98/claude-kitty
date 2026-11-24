"""Claude Code のステータス行（statusLine の command）の 1 行を作る（bin/claude-statusline）。

  名前 | モデル | ctx 380k | sub: 2 | 2d4h 41.2%

- 名前: stdin の session_name（無ければ <設定の置き場>/sessions/*.json の name を session_id で探す）。
  claude-state の記録の状態の色で塗る（claude-state paint と同じ）。
- ctx: いまの会話の長さ（文脈に入っているトークンの数）。stdin の context_window から読む（current_usage の
  input + cache_creation + cache_read。無ければ total_input_tokens）。context_window の無い古い Claude Code では
  transcript_path の最後の usage から読む。
- sub: N: 動いているサブエージェントの数（claude-state subagents と同じ）。
- 週の使用量: rate_limits.seven_day の残り時間と %。設定 statusline.weekly_roles の役割だけ。

30 秒ごと（refreshInterval）と会話の出来事ごとに呼ばれるので、別のプロセスを起こさず、ここで直接読む。
どこかで失敗しても、その区切りを省くだけにして、何かしら 1 行は出す（空にすると何が起きたか分からない）。
"""
import json
import os
from pathlib import Path

SEP = " | "
TRANSCRIPT_TAIL = 256 * 1024  # 最後の usage を探すのに読む末尾の大きさ（長い会話でも 30 秒ごとに全部は読まない）


def ansi(text, color):
    return text if color is None else f"\033[38;5;{color}m{text}\033[0m"


def step_color(value, thresholds, colors, strict=False):
    """value が越えた一番大きいしきい値の色（越えなければ None）。strict なら「超え」、でなければ「以上」。"""
    out = None
    for t, c in zip(thresholds, colors):
        if value > t or (not strict and value == t):
            out = c
    return out


def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def parse_input(text):
    """stdin の中身を dict にする。空・壊れた JSON・オブジェクトでないものは {}。"""
    try:
        data = json.loads(text) if text and text.strip() else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def config_dir(env):
    return Path(env["CLAUDE_CONFIG_DIR"]) if env.get("CLAUDE_CONFIG_DIR") else Path(env.get("HOME") or Path.home()) / ".claude"


def session_name(data, env):
    """session_name。無ければ（古い Claude Code・名前の無いセッション）sessions/<pid>.json の name を session_id で探す。"""
    name = data.get("session_name")
    if isinstance(name, str) and name:
        return name
    sid = data.get("session_id")
    if not isinstance(sid, str) or not sid:
        return None
    try:
        files = sorted((config_dir(env) / "sessions").glob("*.json"))
    except OSError:
        return None
    for f in files:
        try:
            s = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(s, dict) and s.get("sessionId") == sid and isinstance(s.get("name"), str) and s["name"]:
            return s["name"]
    return None


def _usage_sum(u):
    if not isinstance(u, dict):
        return None
    parts = [_num(u.get(k)) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
    if all(p is None for p in parts):
        return None
    return int(sum(p or 0 for p in parts))


def transcript_tokens(path, tail=TRANSCRIPT_TAIL):
    """会話の記録（jsonl）の最後の usage の input + cache_creation + cache_read。読めなければ None。
    サブエージェント（isSidechain）の行は本体の文脈ではないので飛ばす。"""
    if not isinstance(path, str) or not path:
        return None
    try:
        with open(os.path.expanduser(path), "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - tail))
            chunk = f.read()
    except OSError:
        return None
    lines = chunk.split(b"\n")
    if size > tail:
        lines = lines[1:]  # 途中から読んだ最初の行は欠けている
    for raw in reversed(lines):
        if b'"usage"' not in raw:
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("isSidechain"):
            continue
        msg = rec.get("message")
        n = _usage_sum(msg.get("usage")) if isinstance(msg, dict) else None
        if n is not None:
            return n
    return None


def context_tokens(data):
    """(トークン数か None, 出どころ "context_window" / "transcript" / None)。

    context_window があればそれだけを使う。current_usage は最初の応答の前と /compact の直後に null（か 0）で、
    そのとき記録の最後の usage は圧縮の前の大きさなので、記録には戻らない（出さない）。"""
    cw = data.get("context_window")
    if isinstance(cw, dict):
        n = _usage_sum(cw.get("current_usage"))
        if not n:
            t = _num(cw.get("total_input_tokens"))
            n = int(t) if t else None
        return (n, "context_window") if n else (None, "context_window")
    n = transcript_tokens(data.get("transcript_path"))
    return (n, "transcript") if n else (None, None)


def format_tokens(n):
    if n < 1000:
        return str(n)
    k = round(n / 1000)
    if k < 1000:
        return f"{k}k"
    return f"{n / 1e6:.1f}M"


def context_segment(data, conf):
    n, _src = context_tokens(data)
    if not n:
        return ""
    return ansi(f"ctx {format_tokens(n)}", step_color(n, conf["context_thresholds"], conf["context_colors"]))


def left_text(sec):
    sec = max(0, int(sec))
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    return f"{d}d{h}h" if d else f"{h}h" if h else f"{m}m"


def weekly_segment(data, now, conf):
    """週の使用量「2d4h 41.2%」。色は整数部がしきい値を超えたら（前の sh 版の ${pct%%.*} -gt 85 と同じ）。"""
    rl = data.get("rate_limits")
    sd = rl.get("seven_day") if isinstance(rl, dict) else None
    pct = _num(sd.get("used_percentage")) if isinstance(sd, dict) else None
    if pct is None:
        return ""
    reset = _num(sd.get("resets_at"))
    text = f"{pct:g}%"
    if reset is not None:
        text = f"{left_text(reset - int(now))} {text}"  # 秒の端数を切る（date +%s と同じ）
    return ansi(text, step_color(int(pct), conf["weekly_thresholds"], conf["weekly_colors"], strict=True))


def wants_weekly(roles, name, record_role):
    if "*" in roles:
        return True
    return any(x and x in roles for x in (name, record_role))


def build(data, env, now, conf, state=None, config_error=False):
    """1 行を作る。state は claude-state の {"record", "colors", "subagent_colors"}（無ければ色も sub も無し）。"""
    segs = []
    try:
        name = session_name(data, env)
    except Exception:  # noqa: BLE001 — 名前が読めなくても残りは出す
        name = None
    rec = (state or {}).get("record")
    shown = name or "?"
    if rec and isinstance(rec.get("state"), str):
        from state import paint
        try:
            shown = paint(shown, rec["state"], state["colors"])
        except Exception:  # noqa: BLE001
            pass
    segs.append(shown)
    model = data.get("model")
    model = model.get("display_name") if isinstance(model, dict) else None
    if isinstance(model, str) and model:
        segs.append(model)

    def add(fn):
        try:
            s = fn()
        except Exception:  # noqa: BLE001 — 1 つの区切りの失敗で行を空にしない
            s = ""
        if s:
            segs.append(s)
    if conf["context"]:
        add(lambda: context_segment(data, conf))
    if rec:
        from state import subagent_summary
        add(lambda: subagent_summary(rec, now, state.get("subagent_colors")))
    role = rec.get("role") if rec and isinstance(rec.get("role"), str) else None
    if wants_weekly(conf["weekly_roles"], name, role):
        add(lambda: weekly_segment(data, now, conf))
    if config_error:
        segs.append("設定の誤り（claude-kitty doctor）")
    return SEP.join(segs)
