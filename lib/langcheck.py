"""claude-lang-check の中身。最後の返答が決めた言語（既定は日本語）で書かれているかを、AI を使わずに判定する。

副作用は、設定と transcript を読むことと、「この返答ではもう止めた」の記録を書くことだけ。
判定は誤って止めない方に倒す: 読めないもの・短いもの・コードや表だけのものは通す。

設定（~/.config/claude-kitty/config.json の lang の節。どれも省ける）:

  {"lang": {"enabled": true, "language": "ja", "threshold": 0.3, "head": 200, "min": 20}}

環境変数が設定ファイルより優先する: CLAUDE_LANG_CHECK=off（止める）、CLAUDE_LANG_CHECK_LANGUAGE、
CLAUDE_LANG_CHECK_THRESHOLD、CLAUDE_LANG_CHECK_HEAD、CLAUDE_LANG_CHECK_MIN。
"""
import json
import os
import re
import tempfile
import time
from pathlib import Path

DEFAULTS = {"enabled": True, "language": "ja", "threshold": 0.3, "head": 200, "min": 20}

# 対象の言語の字。日本語は漢字も含める（漢字だけの語句も日本語の文として数えるため）
SCRIPTS = {
    "ja": r"぀-ゟ゠-ヿㇰ-ㇿ㐀-䶿一-鿿豈-﫿ｦ-ﾟ",
    "zh": r"㐀-䶿一-鿿豈-﫿",
    "ko": r"가-힯ᄀ-ᇿ㄰-㆏",
}
NAMES = {"ja": "日本語", "zh": "中文", "ko": "한국어"}

# 英単語 1 語を何字分に数えるか。日本語で同じことを書くとおよそ 2 字になるので 2。
# 字の数のまま比べると、日本語の本文に英語の用語が混ざっただけで割合が下がりすぎる
WORD_WEIGHT = 2

# 書き直させる理由に付ける目印。止めた理由が transcript にユーザーの入力として残っても、
# それを新しい返答の始まりと数えないために使う
TAG = "[claude-lang-check]"


# ---- 設定 ------------------------------------------------------------------

def config_path(env):
    """lib/config.py と同じ場所。hook は決して失敗させたくないので、config.py（view 側）を読み込まずに持つ。"""
    if env.get("CLAUDE_KITTY_CONFIG"):
        return Path(env["CLAUDE_KITTY_CONFIG"])
    base = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else Path.home() / ".config"
    return base / "claude-kitty" / "config.json"


def _num(value, lo, hi, cast):
    try:
        v = cast(value)
    except (TypeError, ValueError):
        return None
    if isinstance(value, bool) or not (lo <= v <= hi):
        return None
    return v


def load_lang_config(env, path=None):
    """{"enabled", "language", "threshold", "head", "min"} を返す。

    hook の中で使うので、壊れた設定は例外にせず、その項目だけ既定値に戻す（問題は 2 つ目の返り値に入れる）。
    """
    out = dict(DEFAULTS)
    problems = []
    path = Path(path) if path else config_path(env)
    section = {}
    try:
        data = json.loads(path.read_text())
        section = data.get("lang", {}) if isinstance(data, dict) else {}
        if not isinstance(section, dict):
            problems.append(f"{path} の lang がオブジェクトでない")
            section = {}
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        problems.append(f"設定ファイルを読めない: {path}: {e}")

    def put(key, value, where):
        if key == "enabled":
            v = value if isinstance(value, bool) else None
        elif key == "language":
            v = value if isinstance(value, str) and value in SCRIPTS else None
        elif key == "threshold":
            v = _num(value, 0.0, 1.0, float)
        else:
            v = _num(value, 1, 100000, int)
        if v is None:
            problems.append(f"{where} が正しくない: {value!r}")
        else:
            out[key] = v

    for key in DEFAULTS:
        if key in section:
            put(key, section[key], f"lang.{key}")
    if env.get("CLAUDE_LANG_CHECK"):
        out["enabled"] = env["CLAUDE_LANG_CHECK"].lower() not in ("off", "0", "false", "no")
    for key, name in (("language", "CLAUDE_LANG_CHECK_LANGUAGE"), ("threshold", "CLAUDE_LANG_CHECK_THRESHOLD"),
                      ("head", "CLAUDE_LANG_CHECK_HEAD"), ("min", "CLAUDE_LANG_CHECK_MIN")):
        if env.get(name):
            put(key, env[name], name)
    return out, problems


# ---- 本文の取り出し ----------------------------------------------------------

FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]*\1[ \t]*$|\Z)", re.M | re.S)
INLINE = re.compile(r"(`+)[^`]*?\1")
URL = re.compile(r"(?:https?|file|ftp)://\S+|<[a-z]+://[^>]*>|\bwww\.\S+", re.I)
# /abs/path、~/path、./rel、../rel、a/b/c.py（スラッシュを 1 つ以上含む語）。日本語の字はパスに含めない
PATH = re.compile(r"(?:~|\.{1,2})?/[A-Za-z0-9_.\-~/@+%:]+|\b[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-@+%]+)+/?")
TAGS = re.compile(r"</?[A-Za-z][\w:-]*(?:\s[^<>]*)?/?>")
TABLE = re.compile(r"^[ \t]*\|.*$", re.M)
# 引用の行。理由の文で「引用はそのままでよい」としているので、判定にも数えない
QUOTE = re.compile(r"^[ \t]*>.*$", re.M)
# 見出しの行。英語の見出し＋日本語の本文を「最初の段落が英語」と数えないよう、段落の規則では飛ばす
HEADING = re.compile(r"^[ \t]*#{1,6}[ \t].*$", re.M)
LATIN = re.compile(r"[A-Za-z][A-Za-z0-9_'’.\-]*")


def is_identifier(word):
    """英語の文の語ではなく、識別子・定数・オプション・ファイル名らしい語か。"""
    w = word.rstrip(".'’-")
    if not w:
        return True
    if any(c in w for c in "_0123456789") or "." in w or "-" in w.strip("-"):
        return True                      # snake_case、v2、file.py、kebab-case
    if len(w) >= 2 and w.isupper():
        return True                      # JSON、API、README
    if any(c.isupper() for c in w[1:]):
        return True                      # camelCase、SubagentStop
    return False


def strip_noise(text):
    """コードブロック・インラインコード・URL・HTML のタグ・パス・表の行を除く。"""
    text = FENCE.sub("\n", text)
    text = INLINE.sub(" ", text)
    text = URL.sub(" ", text)
    text = TAGS.sub(" ", text)
    text = TABLE.sub("", text)
    text = QUOTE.sub("", text)
    text = PATH.sub(" ", text)
    return text


def tokens(text, language):
    """(重み, 対象の言語の字か) の列。対象の字は 1 字 1、英語の文の語は 1 語 WORD_WEIGHT。それ以外は数えない。"""
    native = re.compile(f"[{SCRIPTS[language]}]")
    out = []
    # 3 つ目は、対象でもラテン文字でもない字（対象を ko にしたときの日本語など）。1 字 1 で対象外に数える
    for m in re.finditer(f"[{SCRIPTS[language]}]|{LATIN.pattern}|[^\\W\\d_A-Za-z]", text):
        s = m.group(0)
        if native.match(s):
            out.append((1, True))
        elif not s.isascii():
            out.append((1, False))
        elif not is_identifier(s):
            out.append((WORD_WEIGHT, False))
    return out


def ratio(toks):
    total = sum(w for w, _ in toks)
    return (sum(w for w, n in toks if n) / total if total else 1.0), total


def head(toks, limit):
    out, total = [], 0
    for t in toks:
        if total >= limit:
            break
        out.append(t)
        total += t[0]
    return out


def judge(text, cfg):
    """(止めるか, 理由の説明) を返す。

    規則: コード・URL・パス・表・識別子を除いた本文が min 字未満なら通す。
    そうでなければ (1) 冒頭の head 字、(2) 最初の段落（min 字以上あるとき）の、どちらかで
    対象の言語の字の割合が threshold 未満なら止める。(3) 対象の字が出てくるまでに、対象外の語が
    min の 2 倍の字分（既定で英語の約 20 語）続いたときも止める。(2) (3) は英語の前置き＋日本語の本文を
    捕まえるため（前置きが段落を分けずに続くと (1) (2) の割合が閾値を超えることがある）。
    """
    if not isinstance(text, str) or not text.strip():
        return False, "空"
    lang = cfg["language"]
    body = strip_noise(text)
    toks = tokens(body, lang)
    _, total = ratio(toks)
    if total < cfg["min"]:
        return False, f"短い（{total} 字分）"
    r_head, _ = ratio(head(toks, cfg["head"]))
    if r_head < cfg["threshold"]:
        return True, f"冒頭の {NAMES[lang]} の割合 {r_head:.2f} < {cfg['threshold']}"
    for para in re.split(r"\n[ \t]*\n", HEADING.sub("", body)):
        ptoks = tokens(para, lang)
        if not ptoks:
            continue
        r_para, ptotal = ratio(head(ptoks, cfg["head"]))
        if ptotal >= cfg["min"] and r_para < cfg["threshold"]:
            return True, f"最初の段落の {NAMES[lang]} の割合 {r_para:.2f} < {cfg['threshold']}"
        break
    lead = 0
    for w, n in toks:
        if n:
            break
        lead += w
    if cfg["threshold"] > 0 and lead >= 2 * cfg["min"]:    # threshold 0 は「割合では止めない」
        return True, f"{NAMES[lang]} の字の前に対象外の語が {lead} 字分続く"
    return False, f"{NAMES[lang]} の割合 {r_head:.2f}"


def reason_text(cfg):
    name = NAMES[cfg["language"]]
    return (f"{TAG} 直前の返答の本文が{name}になっていません。同じ内容を{name}で書き直して、もう一度返答してください。"
            "コード・コマンド・識別子・引用はそのままでかまいません。"
            f"ユーザーが{name}以外の言語を求めていた場合は、そのままでかまいません（この確認は 1 回の返答につき 1 回だけです）。")


# ---- transcript -------------------------------------------------------------

def reverse_lines(path, chunk=1 << 20, limit=256 << 20):
    """JSONL を末尾から 1 行ずつ返す（大きな transcript を全部読まないため）。limit バイトで打ち切る。"""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        rest = b""
        read = 0
        while pos > 0 and read < limit:
            n = min(chunk, pos)
            pos -= n
            f.seek(pos)
            buf = f.read(n) + rest
            read += n
            lines = buf.split(b"\n")
            rest = lines[0]
            for line in reversed(lines[1:]):
                if line.strip():
                    yield line
        if pos == 0 and rest.strip():
            yield rest


def _content(entry):
    msg = entry.get("message")
    return msg.get("content") if isinstance(msg, dict) else None


def is_prompt(entry):
    """返答の始まりになる user の行か（ツールの結果と、この hook が止めた理由は除く）。"""
    if entry.get("type") != "user" or entry.get("isSidechain"):
        return False
    c = _content(entry)
    if isinstance(c, str):
        return TAG not in c
    if isinstance(c, list):
        texts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c) or not texts:
            return False
        return not any(TAG in t for t in texts)
    return False


def read_transcript(path):
    """(返答の鍵, 最後の返答の本文) を返す。鍵は、いまの返答を始めたユーザーの入力の uuid。

    本文は、最後の user の行（ツールの結果を含む）より後の、アシスタントの text のつなぎ。
    読めなければ OSError / ValueError。
    """
    texts = []
    collecting = True
    for raw in reverse_lines(path):
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("isSidechain"):
            continue
        t = entry.get("type")
        if t == "assistant" and collecting:
            c = _content(entry)
            if isinstance(c, list):
                for b in reversed(c):
                    if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
                        texts.append(b["text"])
        elif t == "user":
            collecting = False
            if is_prompt(entry):
                key = entry.get("uuid") or entry.get("promptId") or entry.get("timestamp")
                return (str(key) if key else None), "\n\n".join(reversed(texts))
    raise ValueError("transcript にユーザーの入力が見つからない")


# ---- 1 回の返答につき 1 回 --------------------------------------------------

SESSION_ID = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")


def state_dir(env):
    base = env.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        return Path(base) / "claude-lang-check"
    return Path(tempfile.gettempdir()) / f"claude-lang-check-{os.getuid()}"


def log_error(env, message):
    """設定の問題や例外を残す（claude-state と同じく、stdout・stderr には出さず、気づけるようにだけする）。"""
    try:
        d = state_dir(env)
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(d / "errors.log", "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
    except OSError:
        pass


def already_blocked(env, session_id, key):
    try:
        data = json.loads((state_dir(env) / f"{session_id}.json").read_text())
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("turn") == key


def remember_block(env, session_id, key):
    d = state_dir(env)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = d / f".{session_id}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps({"turn": key}))
    os.replace(tmp, d / f"{session_id}.json")


# ---- hook の 1 回分 ---------------------------------------------------------

def decide(stdin_text, env):
    """hook の入力（文字列）から、出す JSON（dict）か None（通す）を返す。例外は投げない。"""
    try:
        data = json.loads(stdin_text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("hook_event_name", "Stop") != "Stop":
        return None                       # SubagentStop などは対象外
    if data.get("stop_hook_active"):
        return None
    cfg, problems = load_lang_config(env)
    for p in problems:
        log_error(env, p)
    if not cfg["enabled"]:
        return None
    sid = data.get("session_id")
    tpath = data.get("transcript_path")
    if not (isinstance(sid, str) and SESSION_ID.match(sid) and isinstance(tpath, str) and tpath):
        return None
    text = data.get("last_assistant_message")
    if isinstance(text, str) and not judge(text, cfg)[0]:
        return None                       # 通すときは transcript を読まない
    try:
        key, transcript_text = read_transcript(os.path.expanduser(tpath))
    except (OSError, ValueError):
        return None
    if not key:
        return None
    if not isinstance(text, str):
        text = transcript_text            # 古い版では last_assistant_message が無い
        if not judge(text, cfg)[0]:
            return None
    if already_blocked(env, sid, key):
        return None
    try:
        remember_block(env, sid, key)
    except OSError:
        return None                       # 記録できなければ 1 回きりを守れないので止めない
    return {"decision": "block", "reason": reason_text(cfg)}
