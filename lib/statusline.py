"""Claude Code のステータス行（statusLine の command）の 1 行を作る（bin/claude-statusline）。

  名前 | モデル | ctx 380k | sub: 2 | wait: 1 | 2d4h 41.2%

- 名前: stdin の session_name（無ければ <設定の置き場>/sessions/*.json の name を session_id で探す）。
  claude-state の記録の状態の色で塗る（claude-state paint と同じ）。
- ctx: いまの会話の長さ（文脈に入っているトークンの数）。stdin の context_window から読む（current_usage の
  input + cache_creation + cache_read。無ければ total_input_tokens）。context_window の無い古い Claude Code では
  transcript_path の最後の usage から読む。
- sub: N: 動いているサブエージェントの数（claude-state subagents と同じ）。
- wait: N: 自分の役割（claude-state の記録の role）の claude-waits の active な待ちの数（0 なら省く。statusline.waits）。
- 週の使用量: rate_limits.seven_day の残り時間と %。設定 statusline.weekly_roles の役割だけ。

行とは別に、rate_limits の 7 日・5 時間の % と戻る時刻を usage.json に書き出す（write_usage。設定 statusline.usage_file）。
週の使用量はこの入力にしか来ないので、外のツール（claude-waits の --when-cmd "jq …" など）が読めるようにするため。
usage_check はそのファイルを読んで、しきい値（statusline.usage_warn_percent）以上かを判定する（claude-statusline usage）。

30 秒ごと（refreshInterval）と会話の出来事ごとに呼ばれるので、別のプロセスを起こさず、ここで直接読む。
どこかで失敗しても、その区切りを省くだけにして、何かしら 1 行は出す（空にすると何が起きたか分からない）。
"""
import json
import math
import os
import time
from pathlib import Path

from config import DEFAULT_STATUSLINE, USAGE_WINDOWS  # 窓の名前と既定のしきい値は設定の検査と同じものを使う

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
    そのとき記録の最後の usage は圧縮の前の大きさなので、記録には戻らない（出さない）。total_input_tokens も
    そのとき前の値が残りうるので、使うのは current_usage の鍵そのものが無い版のときだけ。"""
    cw = data.get("context_window")
    if isinstance(cw, dict):
        if "current_usage" in cw:
            n = _usage_sum(cw.get("current_usage"))
        else:
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
    """週の使用量「2d4h 41.2%」（% は小数 1 桁まで）。色は丸める前の整数部がしきい値を超えたら
    （前の sh 版の ${pct%%.*} -gt 85 と同じ。95.97 は「96%」と見えても赤にしない）。"""
    rl = data.get("rate_limits")
    sd = rl.get("seven_day") if isinstance(rl, dict) else None
    pct = _num(sd.get("used_percentage")) if isinstance(sd, dict) else None
    if pct is None:
        return ""
    reset = _num(sd.get("resets_at"))
    text = f"{round(pct, 1):g}%"  # 小数 1 桁まで（41.2%。API の 41.23456 をそのまま並べない）
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
    if conf.get("waits", True) and role:
        from waits import status_line
        add(lambda: status_line(env, role))
    if wants_weekly(conf["weekly_roles"], name, role):
        add(lambda: weekly_segment(data, now, conf))
    if config_error:
        segs.append("設定の誤り（claude-kitty doctor）")
    return SEP.join(segs)


# ---- 使用量のファイル（usage.json） -------------------------------------------------

USAGE_KEEP_SEC = 60      # 中身が前と同じなら、この秒数のあいだは書き直さない（30 秒ごと・出来事ごとに書かないため）
LOG_MAX = 256 * 1024     # errors.log の大きさの上限。超えたら errors.log.1 に回す


def _home(env):
    return Path(env.get("HOME") or os.path.expanduser("~"))


def usage_path(env, conf):
    """usage.json の場所（書かない設定なら None）。

    既定は $XDG_STATE_HOME/claude-kitty/usage.json。CLAUDE_CONFIG_DIR が ~/.claude 以外なら別のアカウントでありうるので、
    usage-<その置き場の実パスの sha1 の先頭 8 桁>.json にして、上書きし合わないようにする（名前だと同じ名前の置き場がぶつかる）。"""
    v = conf.get("usage_file", True)
    if v is False or v is None:
        return None
    if isinstance(v, str):
        return _home(env) / v[2:] if v == "~" or v.startswith("~/") else Path(v)
    base = Path(env["XDG_STATE_HOME"]) if env.get("XDG_STATE_HOME") else _home(env) / ".local" / "state"
    name = "usage.json"
    cd = env.get("CLAUDE_CONFIG_DIR")
    if cd:
        import hashlib
        real = os.path.realpath(cd)
        if real != os.path.realpath(_home(env) / ".claude"):
            name = f"usage-{hashlib.sha1(real.encode()).hexdigest()[:8]}.json"
    return base / "claude-kitty" / name


def _pct(x):
    x = _num(x)
    return x if x is not None and math.isfinite(x) and x >= 0 else None  # -1 は「無い」の印


def usage_record(data):
    """rate_limits から書き出す中身（written_at を除く）。7 日も 5 時間も無ければ None。

    resets_at は入力の値そのまま（Claude Code は epoch 秒の数）。数か空でない文字列でなければ省く。"""
    rl = data.get("rate_limits")
    if not isinstance(rl, dict):
        return None
    out = {}
    for key in USAGE_WINDOWS:
        w = rl.get(key)
        pct = _pct(w.get("used_percentage")) if isinstance(w, dict) else None
        if pct is None:
            continue
        out[key] = pct
        r = w.get("resets_at")
        if (_num(r) is not None and math.isfinite(r) and r >= 0) or (isinstance(r, str) and r):
            out[f"{key}_resets_at"] = r
    return out or None


def log_error(env, message):
    """例外を $XDG_RUNTIME_DIR/claude-statusline/errors.log に残す（行には出さない）。直前の行と同じ文なら書かない
    （書けない置き場は 30 秒ごとに同じ失敗を出すので、ログを埋めないため）。"""
    try:
        from rundir import runtime_dir
        d = runtime_dir(env) / "claude-statusline"
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        log = d / "errors.log"
        try:
            size = log.stat().st_size
            with open(log, "rb") as f:
                f.seek(max(0, size - 4096))
                last = f.read().decode("utf-8", "replace").rstrip("\n").rsplit("\n", 1)[-1]
            if last.split(" ", 1)[-1] == message:
                return
            if size > LOG_MAX:
                os.replace(log, d / "errors.log.1")
        except FileNotFoundError:
            pass
        with open(log, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
    except Exception:  # noqa: BLE001 — ログが書けなくても行は出す
        pass


SAME_WINDOW_SEC = 3600  # 戻る時刻の差がこれより小さければ同じ窓（応答ごとに秒の端数がずれても別の窓にしないため）


def _read_old(path):
    try:
        old = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return old if isinstance(old, dict) else {}


def merge_usage(old, rec, now):
    """前のファイル old と今の入力 rec を窓ごとに合わせる（書く中身。何も無ければ None）。

    rate_limits はセッションごとの最後の応答の値なので、止まっているセッションは古い値を 30 秒ごとに送ってくる。
    そのまま「最後に書いたもの」にすると、動いているセッションの値と行き来したり、戻る時刻を過ぎた前の窓の
    値で書き直して新しく見せたりする。そこで窓ごとに:
    - 戻る時刻を過ぎた値（入力・前のファイルとも）は採らない
    - 同じ窓（戻る時刻の差が SAME_WINDOW_SEC 未満）なら % の大きい方（窓の中では減らない）
    - 違う窓なら戻る時刻の新しい方。戻る時刻の分からない値は比べられないので、入力を採る
    - 入力に無い窓は前の値を残す"""
    out = {}
    for key in USAGE_WINDOWS:
        rk = f"{key}_resets_at"
        cands = []
        for src in (old, rec):
            pct = _pct(src.get(key))
            if pct is None:
                continue
            reset = _epoch(src.get(rk))
            if reset is not None and (not math.isfinite(reset) or reset < now):
                continue
            cands.append((pct, reset, src.get(rk)))
        if not cands:
            continue
        pick = cands[-1]  # 入力（無ければ前の値）
        if len(cands) == 2:
            (op, orr, _), (np_, nr, _) = cands
            if orr is not None and nr is not None:
                if abs(orr - nr) < SAME_WINDOW_SEC:
                    pick = cands[0] if op > np_ else cands[1]
                elif orr > nr:
                    pick = cands[0]
        out[key] = pick[0]
        if pick[2] is not None and pick[1] is not None:
            out[rk] = pick[2]
    return out or None


def write_usage(data, env, now, conf):
    """usage.json を書く。書いたら path、書かなければ None。失敗は errors.log に残すだけで、例外を投げない。

    前のファイルと窓ごとに合わせ（merge_usage）、中身（written_at を除く）が前と同じで USAGE_KEEP_SEC 以内なら書かない。
    同じフォルダの一時ファイルに書いてから os.replace（読む側が途中の中身を見ないため）。0600、フォルダは 0700。"""
    try:
        rec = usage_record(data)
        path = usage_path(env, conf) if rec else None
        if path is None:
            return None
        old = _read_old(path)
        rec = merge_usage(old, rec, now)
        if rec is None:  # 入力も前の値も過ぎた窓だけ: 前のファイルを残す（判定の側が前の窓の値として扱う）
            return None
        at = _num(old.get("written_at"))
        same = {k: v for k, v in old.items() if k not in ("written_at", "written_at_iso")} == rec
        if same and at is not None and 0 <= now - at < USAGE_KEEP_SEC:  # 時計が戻ったとき（未来の written_at）は書き直す
            return None
        import tempfile
        from datetime import datetime
        out = dict(rec, written_at=int(now), written_at_iso=datetime.fromtimestamp(int(now)).astimezone().isoformat())
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))  # mkstemp は 0600
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(out, f, ensure_ascii=False)
                f.write("\n")
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return path
    except Exception as e:  # noqa: BLE001 — 書けなくてもステータス行は変えない
        log_error(env, " ".join(f"usage_file: {type(e).__name__}: {e}".split()))  # 1 行に（同じ失敗を重ねて書かないため）
        return None


# ---- 使用量の判定（claude-statusline usage） ----------------------------------------

def warn_percent(conf, window):
    wp = conf.get("usage_warn_percent", DEFAULT_STATUSLINE["usage_warn_percent"])
    return wp.get(window, DEFAULT_STATUSLINE["usage_warn_percent"]) if isinstance(wp, dict) else wp


def _epoch(x):
    """resets_at を epoch 秒に（数はそのまま、ISO 8601 の文字列は読む）。読めなければ None。"""
    if _num(x) is not None:
        return x
    if isinstance(x, str):
        from datetime import datetime
        try:
            d = datetime.fromisoformat(x[:-1] + "+00:00" if x.endswith("Z") else x)
        except ValueError:
            return None
        return (d if d.tzinfo else d.astimezone()).timestamp()
    return None


def usage_check(path, now, window, over):
    """(終了コード, 1 行)。0 = window の % が over 以上、1 = 未満・前の窓の値・値が無い・ファイルが無い。

    いまが戻る時刻を過ぎていれば、ファイルの値は前の窓のものなので未満とする（戻ったあと誰も新しい応答を
    受けていないと、96 % が残ったまま止め続けるため。新しい窓の値なら戻る時刻は先にある）。"""
    def fmt(x):
        return f"{round(x, 1):g}%"
    try:
        rec = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return 1, f"{window}: ファイルが無い（{path}）"
    except (OSError, ValueError) as e:
        return 1, f"{window}: ファイルが読めない（{path}: {type(e).__name__}）"
    pct = _pct(rec.get(window)) if isinstance(rec, dict) else None
    if pct is None:
        return 1, f"{window}: 値が無い（{path}）"
    reset = _epoch(rec.get(f"{window}_resets_at"))
    when = ""
    if reset is not None and math.isfinite(reset):
        try:
            when = time.strftime("%m/%d %H:%M", time.localtime(reset))
        except (OverflowError, OSError, ValueError):
            when = str(rec.get(f"{window}_resets_at"))
        if now > reset:
            return 1, f"{window} {fmt(pct)} は前の窓の値（{when} に戻った）"
        when = f"（戻る {when}）"
    if pct >= over:
        return 0, f"{window} {fmt(pct)} ≥ {over:g}%{when}"
    return 1, f"{window} {fmt(pct)} < {over:g}%{when}"
