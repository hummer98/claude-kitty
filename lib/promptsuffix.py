"""claude-prompt-suffix の中身。ユーザーの文の最後に付けた略号（`wdt`・`opt`・`desc 2` など）を拾い、
その意味を UserPromptSubmit hook の additionalContext としてモデルに渡す。

CLAUDE.md に「wdt は意見だけ」と書いてモデルに任せるだけだと、効いたり効かなかったりする。hook で毎回
同じ文を足せば確実に効き、略号の意味を設定ファイルで足す・変える・消すことができる。

混同を防ぐため、拾うのは次のときだけ（docs/prompt-suffix.md）:
- 貼り付けた文（Claude Code が `<pasted_content id="…">…</pasted_content id="…">` で包んだもの）は除いて見る。
- 最後の行の、最後の語（前が空白か改行、後ろに何もない。句読点も付かない）が略号のとき。
- 最後の行がコードブロックの中・コマンドのような形（`$ ` 始まり、`--` を含むなど）・引用（`>`）なら見ない。
- 拾ったら画面に 1 行（systemMessage）出し、足す文は「略号とみなした。明らかに文の一部なら無視してよい」と書く。

設定（~/.config/claude-kitty/config.json の prompt_suffix の節。どれも省ける）:

  {"prompt_suffix": {"enabled": true, "language": "ja", "suffixes": {}, "number_lists": false,
                     "notice": true, "screenshot_dir": null}}

環境変数 CLAUDE_PROMPT_SUFFIX=off で止める。副作用は設定を読むこと、ss のときにスクリーンショットの置き場を
見ること、設定の問題を errors.log に残すことだけ。
"""
import json
import os
import plistlib
import re
import tempfile
import time
from pathlib import Path

# 足す文の頭の目印（transcript に残ったとき、どの道具が足したか分かるように）
TAG = "[claude-prompt-suffix]"

# 略号の名前: 英字で始まる短い語。大文字小文字は区別しないので、設定の名前は小文字にそろえる
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,19}$")
# 番号: desc 2、desc 2,3、desc 1-3
NUMBER = re.compile(r"^\d{1,3}(?:[,\-]\d{1,3}){0,9}$")
MAX_SUFFIXES = 4          # 文の最後に並べられる略号の数（これを超える並びは最後の 4 つだけ見る）
MAX_TEXT = 2000           # 1 つの略号の文の上限（設定の誤りで毎ターン大きな文を足さないため）

# 既定の略号。文字列は番号を取らない略号、{"text", "with_number"} は番号を取れる略号（{n} が番号）。
# {screenshot} は、最新のスクリーンショットのパスと時刻（見つからなければ、その旨）に置き換える
BUILTIN = {
    "ja": {
        "wdt": "意見だけを聞いている。作業の指示ではない。ファイルの変更・コミット・他のセッションやサブエージェントへの"
               "変更の依頼はしない（読むだけの調べものはよい）。",
        "opt": "案を 2〜4 つ出し、番号を振って、それぞれに推奨度と信頼度を付け、おすすめを 1 つ選ぶ。選ばれるまでは実装しない。",
        "pre": "生成物を見せる。HTML なら HTML、画像なら画像を開く（kitty 上なら claude-show <file>）。"
               "デプロイしていればデプロイ先の URL を開く。",
        "desc": {"text": "解説だけをする。作業はしない（読むだけの調べものはよい）。直前の話題を解説する。",
                 "with_number": "解説だけをする。作業はしない（読むだけの調べものはよい）。直前の返答の {n} 番の項目を解説する。"
                                "直前の返答に番号の付いた項目が無ければ、どこを指すかユーザーに確認してから解説する。"},
        "next": "ユーザーが決めることだけを、1 行目にその数を書いて、番号を振って並べる。自分で決められるものは決めておく。",
        "ss": "最新のスクリーンショットを開いて見てから答える。{screenshot}",
    },
    "en": {
        "wdt": "The user is asking for an opinion only, not for work. Do not change files, commit, or ask other sessions or "
               "subagents to make changes (read-only investigation is fine).",
        "opt": "Give 2-4 numbered options, each with how strongly you recommend it and how confident you are, and pick one "
               "as your recommendation. Do not implement anything until the user chooses.",
        "pre": "Show the result: open the HTML if it is HTML, the image if it is an image (on kitty, claude-show <file>). "
               "If it is deployed, open the deployed URL.",
        "desc": {"text": "Explain only; do no work (read-only investigation is fine). Explain the topic just discussed.",
                 "with_number": "Explain only; do no work (read-only investigation is fine). Explain item {n} of your "
                                "previous reply. If that reply has no numbered items, ask the user what it refers to first."},
        "next": "List only the things the user has to decide, numbered, with their count on the first line. Decide "
                "whatever you can decide yourself.",
        "ss": "Open and look at the latest screenshot before answering. {screenshot}",
    },
}
LANGUAGES = tuple(BUILTIN)

# 文の言い回し（言語ごと）
WORDS = {
    "ja": {
        "head": "ユーザーの文の最後の「{tokens}」を略号とみなした。明らかに文の一部なら無視してよい。",
        "notice": "略号 {tokens} を適用",
        "sep": "・",
        "numbering": "案・判断・指摘の一覧には番号を振る（ユーザーが「desc 2」のように番号で指せるように）。",
        "shot": "最新: {path}（{clock}、{age}）。",
        "shot_none": "スクリーンショットが見つからなかった（探した場所: {dirs}）。ユーザーに置き場を確認する。",
        "min": "{n} 分前", "hour": "{n} 時間前", "day": "{n} 日前", "now": "1 分以内",
    },
    "en": {
        "head": "Treated the trailing \"{tokens}\" in the user's message as shorthand. If it is clearly part of the "
                "sentence, ignore this.",
        "notice": "Shorthand {tokens} applied",
        "sep": ", ",
        "numbering": "Number the items of any list of options, decisions or findings (so the user can point at one "
                     "with \"desc 2\").",
        "shot": "Latest: {path} ({clock}, {age}).",
        "shot_none": "No screenshot found (looked in: {dirs}). Ask the user where it is.",
        "min": "{n} min ago", "hour": "{n} h ago", "day": "{n} d ago", "now": "under a minute ago",
    },
}

DEFAULTS = {"enabled": True, "language": "ja", "suffixes": {}, "number_lists": False, "notice": True,
            "screenshot_dir": None}

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".avif"}
SCAN_LIMIT = 5000          # 1 つの置き場で見るファイルの数の上限（大きなフォルダで hook を遅くしないため）


# ---- 設定 ------------------------------------------------------------------

def config_path(env):
    """lib/config.py と同じ場所（hook を決して失敗させないよう、config.py を読み込まずに持つ。langcheck と同じ）。"""
    if env.get("CLAUDE_KITTY_CONFIG"):
        return Path(env["CLAUDE_KITTY_CONFIG"])
    base = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else Path.home() / ".config"
    return base / "claude-kitty" / "config.json"


def _entry(value):
    """設定の 1 つの略号を {"text", "with_number"} にする。誤りなら ValueError（理由）。"""
    if isinstance(value, str):
        value = {"text": value}
    if not isinstance(value, dict):
        raise ValueError("文字列か {\"text\", \"with_number\"} か null")
    bad = set(value) - {"text", "with_number"}
    if bad:
        raise ValueError(f"知らないキー: {', '.join(sorted(bad))}")
    out = {}
    for key in ("text", "with_number"):
        v = value.get(key)
        if v is None:
            continue
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{key} が空でない文字列でない")
        if len(v) > MAX_TEXT:
            raise ValueError(f"{key} が長すぎる（{len(v)} 字。{MAX_TEXT} 字まで）")
        out[key] = v.strip()
    if "text" not in out:
        raise ValueError("text が無い")
    if "with_number" in out and "{n}" not in out["with_number"]:
        raise ValueError("with_number に {n}（番号の入る所）が無い")
    return out


def load_config(env, path=None):
    """(設定, 問題のリスト)。設定の suffixes は、既定と設定を重ねた {名前: {"text", "with_number"?}}。

    hook の中で使うので、壊れた設定は例外にせず、その項目だけ既定に戻す（略号 1 つの誤りは、その略号だけ外す）。
    """
    out = dict(DEFAULTS)
    problems = []
    path = Path(path) if path else config_path(env)
    section = {}
    try:
        data = json.loads(path.read_text())
        section = data.get("prompt_suffix", {}) if isinstance(data, dict) else {}
        if not isinstance(section, dict):
            problems.append(f"{path} の prompt_suffix がオブジェクトでない")
            section = {}
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        problems.append(f"設定ファイルを読めない: {path}: {e}")

    for key in set(section) - set(DEFAULTS):
        problems.append(f"prompt_suffix.{key} は知らない項目")
    for key in ("enabled", "number_lists", "notice"):
        if key in section:
            if isinstance(section[key], bool):
                out[key] = section[key]
            else:
                problems.append(f"prompt_suffix.{key} が true / false でない: {section[key]!r}")
    if "language" in section:
        if section["language"] in LANGUAGES:
            out["language"] = section["language"]
        else:
            problems.append(f"prompt_suffix.language は {' / '.join(LANGUAGES)}: {section['language']!r}")
    if "screenshot_dir" in section:
        v = section["screenshot_dir"]
        if v is None or (isinstance(v, str) and v.strip()):
            out["screenshot_dir"] = v
        elif isinstance(v, list) and v and all(isinstance(x, str) and x.strip() for x in v):
            out["screenshot_dir"] = list(v)
        else:
            problems.append(f"prompt_suffix.screenshot_dir は null・文字列・文字列のリスト: {v!r}")

    table = {k: (dict(text=v) if isinstance(v, str) else dict(v)) for k, v in BUILTIN[out["language"]].items()}
    given = section.get("suffixes", {})
    if not isinstance(given, dict):
        problems.append(f"prompt_suffix.suffixes がオブジェクトでない: {given!r}")
        given = {}
    out["suffixes"] = given
    for name, value in given.items():
        if not isinstance(name, str) or not NAME.match(name):
            problems.append(f"prompt_suffix.suffixes の名前が正しくない（英字で始まる 20 字までの英数字）: {name!r}")
            continue
        key = name.lower()
        if value is None or value == "":
            table.pop(key, None)            # 既定の略号を消す
            continue
        try:
            table[key] = _entry(value)
        except ValueError as e:
            problems.append(f"prompt_suffix.suffixes.{name}: {e}（この略号は使わない）")
            table.pop(key, None)            # 誤った設定の略号は、既定のものも含めて効かせない（思っていない文を足さない）
    if env.get("CLAUDE_PROMPT_SUFFIX"):
        out["enabled"] = env["CLAUDE_PROMPT_SUFFIX"].lower() not in ("off", "0", "false", "no")
    out["table"] = table
    return out, problems


# ---- 略号を拾う ------------------------------------------------------------

# Claude Code は、まとめて貼り付けた文（[Pasted text #1 +N lines] に畳まれたもの）を hook の prompt の中で
# この形で包む（2.1.331 で確認。閉じが無ければ最後まで貼り付けとみなす）
PASTED = re.compile(r'<pasted_content id="([0-9a-f]{4})">.*?(?:</pasted_content id="\1">|\Z)', re.S)
FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})")


def strip_pastes(text):
    return PASTED.sub("\n", text)


def last_line(text):
    """貼り付けを除いた文の、最後の空でない行。コードブロックの中・コマンド・引用なら None。"""
    lines = strip_pastes(text).rstrip().split("\n")
    if not lines or not lines[-1].strip():
        return None
    fence = None
    for line in lines[:-1]:
        m = FENCE.match(line)
        if not m:
            continue
        mark = m.group(1)
        if fence is None:
            fence = mark
        elif mark[0] == fence[0] and len(mark) >= len(fence) and not line.strip()[len(mark):].strip():
            fence = None
    last = lines[-1]
    if fence is not None or FENCE.match(last):
        return None                      # 閉じていないコードブロックの中（か、その区切りの行）
    s = last.strip()
    if s.startswith(("$ ", "% ", ">")) or "--" in s:
        return None                      # シェルの入力・オプション付きのコマンド・引用
    return last


def find(text, table):
    """文の最後の略号の並び [(名前, 番号か None), ...]（書いた順）。無ければ []。"""
    if not isinstance(text, str) or not text.strip():
        return []
    if text.lstrip().startswith(("/", "!")):
        return []                        # スラッシュコマンドとシェルのモード（引数の最後の語は略号でない）
    line = last_line(text)
    if line is None:
        return []
    words = line.split()                 # 全角の空白も区切りに数える
    found = []
    i = len(words) - 1
    while i >= 0 and len(found) < MAX_SUFFIXES:
        w = words[i].lower()
        if NUMBER.match(w) and i >= 1 and "with_number" in table.get(words[i - 1].lower(), {}):
            found.append((words[i - 1].lower(), w))
            i -= 2
        elif w in table:
            found.append((w, None))
            i -= 1
        else:
            break
    found.reverse()
    seen, out = set(), []
    for item in found:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


# ---- スクリーンショット ----------------------------------------------------

def _xdg_pictures(env, home):
    """~/.config/user-dirs.dirs の XDG_PICTURES_DIR（日本語の環境では ~/画像 など）。無ければ ~/Pictures。"""
    base = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else home / ".config"
    try:
        for line in (base / "user-dirs.dirs").read_text().splitlines():
            m = re.match(r'^\s*XDG_PICTURES_DIR\s*=\s*"(.*)"\s*$', line)
            if m:
                return Path(m.group(1).replace("$HOME", str(home)))
    except (OSError, UnicodeDecodeError):
        pass
    return home / "Pictures"


def screenshot_dirs(cfg, env, os_name=None):
    """スクリーンショットを探す場所（書いた順・重なりなし）。設定 screenshot_dir が無ければ OS の普通の場所。"""
    home = Path(env.get("HOME") or Path.home())
    v = cfg.get("screenshot_dir")
    if v:
        given = [v] if isinstance(v, str) else v
        return list(dict.fromkeys(Path(os.path.expanduser(x.replace("$HOME", str(home)))) for x in given))
    os_name = os_name or os.uname().sysname
    if os_name == "Darwin":
        # macOS の置き場は「スクリーンショット」の設定（defaults write com.apple.screencapture location）。既定はデスクトップ
        dirs = []
        try:
            with open(home / "Library/Preferences/com.apple.screencapture.plist", "rb") as f:
                loc = plistlib.load(f).get("location")
            if isinstance(loc, str) and loc:
                dirs.append(Path(os.path.expanduser(loc)))
        except (OSError, ValueError, plistlib.InvalidFileException):
            pass
        return list(dict.fromkeys(dirs + [home / "Desktop"]))
    # Linux: GNOME は <ピクチャ>/Screenshots（日本語の GNOME は「スクリーンショット」）、KDE の Spectacle は
    # <ピクチャ>/Screenshots（古い版は <ピクチャ> の直下）
    pics = _xdg_pictures(env, home)
    dirs = [pics / "Screenshots", pics / "スクリーンショット", pics, home / "Pictures/Screenshots", home / "Pictures"]
    return list(dict.fromkeys(dirs))


def latest_screenshot(dirs):
    """置き場の直下の画像のうち、一番新しいもの (パス, 更新時刻)。無ければ None。"""
    best = None
    for d in dirs:
        try:
            with os.scandir(d) as it:
                for n, e in enumerate(it):
                    if n >= SCAN_LIMIT:
                        break
                    if os.path.splitext(e.name)[1].lower() not in IMAGE_EXT:
                        continue
                    try:
                        if not e.is_file():
                            continue
                        t = e.stat().st_mtime
                    except OSError:
                        continue
                    if best is None or t > best[1]:
                        best = (Path(e.path), t)
        except OSError:
            continue
    return best


def _age(seconds, w):
    m = int(max(0, seconds) // 60)
    if m < 1:
        return w["now"]
    if m < 120:
        return w["min"].format(n=m)
    if m < 48 * 60:
        return w["hour"].format(n=m // 60)
    return w["day"].format(n=m // (24 * 60))


def screenshot_text(cfg, env, now=None, os_name=None):
    w = WORDS[cfg["language"]]
    dirs = screenshot_dirs(cfg, env, os_name)
    found = latest_screenshot(dirs)
    if not found:
        return w["shot_none"].format(dirs=", ".join(str(d) for d in dirs))
    path, mtime = found
    now = time.time() if now is None else now
    return w["shot"].format(path=path, clock=time.strftime("%m-%d %H:%M", time.localtime(mtime)), age=_age(now - mtime, w))


# ---- 足す文 ----------------------------------------------------------------

def render(found, cfg, env, now=None, os_name=None):
    """(モデルに足す文, 画面の 1 行)。found が空なら (number_lists の文か None, None)。"""
    w = WORDS[cfg["language"]]
    if not found:
        return (f"{TAG} {w['numbering']}" if cfg["number_lists"] else None), None
    tokens = " ".join(name if n is None else f"{name} {n}" for name, n in found)
    lines = [f"{TAG} {w['head'].format(tokens=tokens)}"]
    shot = None
    for name, n in found:
        entry = cfg["table"][name]
        text = entry["text"] if n is None else entry["with_number"].replace("{n}", n)
        if "{screenshot}" in text:
            if shot is None:
                shot = screenshot_text(cfg, env, now, os_name)
            text = text.replace("{screenshot}", shot)
        lines.append(f"- {name if n is None else f'{name} {n}'}: {text.strip()}")
    if cfg["number_lists"]:
        lines.append(f"- {w['numbering']}")
    shown = w["sep"].join(name if n is None else f"{name} {n}" for name, n in found)
    return "\n".join(lines), (w["notice"].format(tokens=shown) if cfg["notice"] else None)


# ---- 記録 ------------------------------------------------------------------

def state_dir(env):
    base = env.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        return Path(base) / "claude-prompt-suffix"
    return Path(tempfile.gettempdir()) / f"claude-prompt-suffix-{os.getuid()}"


def log_error(env, message):
    """設定の問題や例外を残す（stdout・stderr には出さない。claude-kitty doctor の「設定: prompt_suffix」でも見える）。"""
    try:
        d = state_dir(env)
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(d / "errors.log", "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
    except OSError:
        pass


# ---- hook の 1 回分 ---------------------------------------------------------

def decide(stdin_text, env, now=None, os_name=None):
    """hook の入力（文字列）から、出す JSON（dict）か None（何も足さない）を返す。例外は投げない前提で呼ぶ。"""
    try:
        data = json.loads(stdin_text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("hook_event_name", "UserPromptSubmit") != "UserPromptSubmit":
        return None
    if data.get("agent_id"):
        return None                       # サブエージェントへの指示（親のモデルが書いた文）は対象外
    prompt = data.get("prompt")
    if not isinstance(prompt, str):
        return None
    cfg, problems = load_config(env)
    for p in problems:
        log_error(env, p)
    if not cfg["enabled"]:
        return None
    found = find(prompt, cfg["table"])
    context, notice = render(found, cfg, env, now, os_name)
    if not context:
        return None
    out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}
    if notice:
        out["systemMessage"] = notice
    return out
