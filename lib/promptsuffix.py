"""claude-prompt-suffix の中身。ユーザーの文の最後に付けた略号（`wdt`・`opt`・`desc 2` など）を拾い、
その意味を UserPromptSubmit hook の additionalContext としてモデルに渡す。

CLAUDE.md に「wdt は意見だけ」と書いてモデルに任せるだけだと、効いたり効かなかったりする。hook で毎回
同じ文を足せば確実に効き、略号の意味を設定ファイルで足す・変える・消すことができる。

混同を防ぐため、拾うのは次のときだけ（docs/prompt-suffix.md）:
- 貼り付けた文（Claude Code が `<pasted_content id="…">…</pasted_content id="…">` で包んだもの）は除いて見る。
- 最後の行の、最後の語（前が空白・改行・ASCII 以外の字、後ろに何もない。句読点も付かない）が略号のとき。
- 別のセッション・背景の仕事・claude-relay からの知らせ（打った文でないもの）は見ない。
- 最後の行がコードブロックの中・コマンドのような形（`$ ` 始まり、`--` を含むなど）・引用（`>`）なら見ない。
- 拾ったら画面に 1 行（systemMessage）出し、足す文は「略号とみなした。明らかに文の一部なら無視してよい」と書く。

設定（~/.config/claude-kitty/config.json の prompt_suffix の節。どれも省ける）:

  {"prompt_suffix": {"enabled": true, "language": "ja", "suffixes": {}, "number_lists": false,
                     "notice": true, "screenshot_dir": null, "screenshot_by_cwd": {}, "screenshot_names": null,
                     "screenshot_max_age_min": 120}}

環境変数 CLAUDE_PROMPT_SUFFIX=off で止める。副作用は設定を読むこと、ss のときにスクリーンショットの置き場を
見ること、設定の問題を errors.log に残すことだけ。
"""
import fnmatch
import glob
import json
import unicodedata
import os
import plistlib
import re
import tempfile
import time
from pathlib import Path

# 足す文の頭の目印（transcript に残ったとき、どのツールが足したか分かるように）
TAG = "[claude-prompt-suffix]"

# 略号の名前: 英字で始まる短い語。大文字小文字は区別しないので、設定の名前は小文字にそろえる
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,19}$")
# 番号: desc 2、desc 2,3、desc 1-3
# 番号は ASCII の数字だけ（\d は全角の数字にも当たる）。区切りは , - と全角の 、 ，（「desc 2、3」）
NUMBER = re.compile(r"^[0-9]{1,3}(?:[,\-、，][0-9]{1,3}){0,9}$")
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
        "aq": "作業を始める前に、分からない点や決めておきたい前提を番号を振って聞き、答えを待つ。"
              "自分で決められる小さな選択は決めて、その内容を書いておく。聞くことが無ければ「質問なし」と書いて進める。"
              "ほかの略号の指示は、答えを受けてから従う。",
        "sub": "作業はサブエージェントにさせ、自分は依頼・確認・報告にとどめる。コードを書かせたら、別のサブエージェントに"
               "差分と目的だけを渡してレビューさせる。wdt・desc と並べたときは、読むだけの調べものをさせる。",
        "htpre": "結果を人が読める HTML のページにまとめてファイルに保存し、開いて見せる（kitty 上なら claude-show <file>）。"
                 "置き場は作業中のリポジトリのコミットに紛れない所（作業ツリーの外など）。"
                 "見せるための HTML の保存は、wdt・desc の「変更しない」に数えない。",
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
        # next は英語の文の終わりの語（what comes next）と重なりやすいので、en の既定には入れない（使うなら suffixes で足す）
        "ss":"Open and look at the latest screenshot before answering. {screenshot}",
        "aq": "Before starting, ask numbered questions about anything unclear or any assumption worth settling, and wait "
              "for the answers. Settle minor choices yourself and state them. If there is nothing to ask, say \"No questions\" "
              "and go ahead. Follow any other shorthand only after the answers.",
        "sub": "Have subagents do the work; limit yourself to briefing, checking, and reporting. When a subagent writes "
               "code, have a separate subagent review it, given only the diff and the goal. Combined with wdt or desc, "
               "have them do read-only investigation only.",
        "htpre": "Put the result into a human-readable HTML page, save it to a file, and open it for the user (on kitty, "
                 "claude-show <file>). Save it where it will not slip into a commit of the repository you are working in "
                 "(e.g. outside the working tree). Saving this HTML does not count as a change under wdt or desc.",
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
        "shot_old": "新しいスクリーンショットは無い（{limit}以内のもの。最新は {path}、{clock}、{age}）。ユーザーに確認する。",
        "shot_none": "スクリーンショットが見つからなかった（探した場所: {dirs}）。ユーザーに置き場を確認する。",
        "min": "{n} 分前", "hour": "{n} 時間前", "day": "{n} 日前", "now": "1 分以内", "limit": "{n} 分",
    },
    "en": {
        "head": "Treated the trailing \"{tokens}\" in the user's message as shorthand. If it is clearly part of the "
                "sentence, ignore this.",
        "notice": "Shorthand {tokens} applied",
        "sep": ", ",
        "numbering": "Number the items of any list of options, decisions or findings (so the user can point at one "
                     "with \"desc 2\").",
        "shot": "Latest: {path} ({clock}, {age}).",
        "shot_old": "No recent screenshot (none within {limit}; the latest is {path}, {clock}, {age}). Ask the user.",
        "shot_none": "No screenshot found (looked in: {dirs}). Ask the user where it is.",
        "min": "{n} min ago", "hour": "{n} h ago", "day": "{n} d ago", "now": "under a minute ago", "limit": "{n} min",
    },
}

DEFAULTS = {"enabled": True, "language": "ja", "suffixes": {}, "number_lists": False, "notice": True,
            "screenshot_dir": None, "screenshot_by_cwd": {}, "screenshot_names": None, "screenshot_max_age_min": 120}

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".avif"}
# スクリーンショットらしい名前（大文字小文字は区別しない。置き場の直下の、写真などほかの画像を選ばないため）。
# OS のスクリーンショットのツールの既定の名前（GNOME・KDE の Spectacle・macOS・Windows の各言語）と、Steam の YYYYMMDDhhmmss_N.jpg
DEFAULT_NAMES = ["screenshot*", "screen shot*", "スクリーンショット*", "bildschirmfoto*", "capture d’écran*",
                 "capture d'écran*", "captura de pantalla*", "captura de tela*", "schermafbeelding*", "schermata*",
                 "skärmbild*", "zrzut ekranu*", "снимок экрана*", "屏幕截图*", "螢幕截圖*", "截屏*", "스크린샷*",
                 "[0-9]" * 14 + "_*"]
SCAN_LIMIT = 20000         # 1 つの置き場で見る、名前の合う画像の数の上限（並びは OS 任せなので、超えると取りこぼしうる）


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


def _dirs_value(v):
    """置き場の設定の値を None・文字列のリストにする。誤りなら False。"""
    if v is None:
        return None
    if isinstance(v, str) and v.strip():
        return [v]
    if isinstance(v, list) and v and all(isinstance(x, str) and x.strip() for x in v):
        return list(v)
    return False


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
        v = _dirs_value(section["screenshot_dir"])
        if v is False:
            problems.append(f"prompt_suffix.screenshot_dir は null・文字列・文字列のリスト: {section['screenshot_dir']!r}")
        else:
            out["screenshot_dir"] = v
    if "screenshot_by_cwd" in section:
        v = section["screenshot_by_cwd"]
        if not isinstance(v, dict):
            problems.append(f"prompt_suffix.screenshot_by_cwd がオブジェクトでない: {v!r}")
        else:
            table_cwd = {}
            for k, dirs in v.items():
                d = _dirs_value(dirs)
                if not (isinstance(k, str) and k.strip()) or d in (False, None):
                    problems.append(f"prompt_suffix.screenshot_by_cwd の {k!r} は、フォルダ → 文字列か文字列のリスト")
                elif any(c in k for c in "*?["):
                    problems.append(f"prompt_suffix.screenshot_by_cwd のキー {k!r} に glob の字（* ? [）は使えない（フォルダをそのまま書く）")
                else:
                    table_cwd[k] = d
            out["screenshot_by_cwd"] = table_cwd
    if "screenshot_names" in section:
        v = section["screenshot_names"]
        if v is None or (isinstance(v, list) and v and all(isinstance(x, str) and x.strip() for x in v)):
            out["screenshot_names"] = v
        else:
            problems.append(f"prompt_suffix.screenshot_names は null か、空でない文字列のリスト: {v!r}")
    if "screenshot_max_age_min" in section:
        v = section["screenshot_max_age_min"]
        if v is None or (isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 525600):
            out["screenshot_max_age_min"] = v
        else:
            problems.append(f"prompt_suffix.screenshot_max_age_min は 1〜525600 の整数か null: {v!r}")

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
# この形で包む（2.1.331 で確認。id は 4 桁の 16 進だったが、変わっても外れないよう広く取る。閉じが無ければ最後まで貼り付け）
PASTED = re.compile(r'<pasted_content id="([^"\n]{1,64})">.*?(?:</pasted_content id="\1">|\Z)', re.S)
FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
# 画像を貼ると、文の最後に [Image #1] が付く（打った略号の後ろに付くことがあるので、除いてから見る）
IMAGES = re.compile(r"(?:[ \t]*\[Image #\d+\])+\s*$")


def strip_pastes(text):
    return PASTED.sub("\n", text)


def last_line(text):
    """貼り付けを除いた文の、最後の空でない行。コードブロックの中・コマンド・引用なら None。"""
    lines = IMAGES.sub("", strip_pastes(text).rstrip()).rstrip().split("\n")
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


# 打った文でないもの: 別のセッションからの知らせ・背景の仕事の知らせ・claude-relay のメッセージなど。
# Claude Code がユーザーの入力として渡すことがあるが、最後の語はユーザーの略号ではない
NOT_TYPED = ("<cross-session-message", "<agent-message", "<task-notification", "<relay-message",
             "<teammate-message", "[SYSTEM NOTIFICATION")
SLASH = re.compile(r"^/[A-Za-z][\w:-]*(?:\s|$)")
# 語の末尾の ASCII の英字の並び（その前が ASCII 以外の字のときだけ区切りとみなす。「…についてwdt」）
# 全角の英数字（ＡＢＣ・１２３）も ASCII と同じく語の一部とみなす
TAIL = re.compile(r"[^\x00-\x7f０-９Ａ-Ｚａ-ｚ]([A-Za-z][A-Za-z0-9_-]*)$")


def is_typed(text):
    return not any(m in text for m in NOT_TYPED)


def _match(word, prev, table):
    """word（と、番号ならその前の語 prev）が略号か。(名前, 番号) か None。"""
    w = word.lower()
    if NUMBER.match(w) and prev is not None and "with_number" in table.get(prev.lower(), {}):
        return prev.lower(), w
    if w in table:
        return w, None
    return None


def find(text, table):
    """文の最後の略号の並び [(名前, 番号か None), ...]（書いた順）。無ければ []。"""
    if not isinstance(text, str) or not text.strip():
        return []
    if not is_typed(strip_pastes(text)):     # 貼り付けた文の中の印（ログを貼ったなど）では除かない
        return []
    head = text.lstrip()
    if SLASH.match(head) or head.startswith("!"):
        return []                        # スラッシュコマンドとシェルのモード（引数の最後の語は略号でない）
    line = last_line(text)
    if line is None:
        return []
    words = line.split()                 # 全角の空白も区切りに数える
    found = []
    i = len(words) - 1
    while i >= 0 and len(found) < MAX_SUFFIXES:
        w = words[i]
        if NUMBER.match(w.lower()) and i >= 1:
            prev = words[i - 1]
            hit = _match(w, prev, table)
            if hit:
                found.append(hit)
                i -= 2
                continue
            m = TAIL.search(prev)        # 「…についてdesc 2」
            hit = _match(w, m.group(1), table) if m else None
            if hit:
                found.append(hit)
            break
        hit = _match(w, None, table)
        if hit:
            found.append(hit)
            i -= 1
            continue
        m = TAIL.search(w)               # 「…についてwdt」「…は？wdt」。ASCII の英数字の直後（xwdt）は拾わない
        hit = _match(m.group(1), None, table) if m else None
        if hit:
            found.append(hit)
        break
    found.reverse()
    seen, out = set(), []
    for item in found:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


# ---- スクリーンショット ----------------------------------------------------

def _home(env):
    return Path(env.get("HOME") or Path.home())


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


def _spectacle_dirs(env, home, pics):
    """KDE の Spectacle の保存先（spectaclerc の [ImageSave] imageSaveLocation。無ければ
    <ピクチャ>/<translatedScreenshotsFolder>）。読めなければ []。"""
    base = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else home / ".config"
    try:
        text = (base / "spectaclerc").read_text()
    except (OSError, UnicodeDecodeError):
        return []
    out, section = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
            continue
        if section != "[ImageSave]" or "=" not in line:
            continue
        key, value = (x.strip() for x in line.split("=", 1))
        if key == "imageSaveLocation" and value:
            out.insert(0, Path(re.sub(r"^file://", "", value)))
        elif key == "translatedScreenshotsFolder" and value and "/" not in value:
            out.append(pics / value)
    return out


STEAM = "userdata/*/760/remote/*/screenshots"


def default_dirs(env, os_name):
    home = _home(env)
    if os_name == "Darwin":
        # macOS の置き場は「スクリーンショット」の設定（defaults write com.apple.screencapture location）。既定はデスクトップ
        dirs = []
        try:
            with open(home / "Library/Preferences/com.apple.screencapture.plist", "rb") as f:
                loc = plistlib.load(f).get("location")
            if isinstance(loc, str) and loc:
                dirs.append(os.path.expanduser(loc))
        except (OSError, ValueError, plistlib.InvalidFileException):
            pass
        return dirs + [str(home / "Desktop"), str(home / "Pictures/Screenshots"),
                       str(home / "Library/Application Support/Steam" / STEAM)]
    # Linux: GNOME は <ピクチャ>/Screenshots（日本語の GNOME は「スクリーンショット」）、KDE の Spectacle は
    # spectaclerc の保存先（既定は <ピクチャ>/<その言語の Screenshots>）。Steam はゲームごとのフォルダ
    pics = _xdg_pictures(env, home)
    dirs = [pics / "Screenshots", pics / "スクリーンショット", *_spectacle_dirs(env, home, pics), pics,
            home / "Pictures/Screenshots", home / "Pictures",
            home / ".local/share/Steam" / STEAM, home / ".steam/steam" / STEAM,
            home / ".var/app/com.valvesoftware.Steam/.local/share/Steam" / STEAM]
    return [str(d) for d in dirs]


def _expand(pattern, home):
    """置き場の書き方（~・$HOME・glob）を、あるフォルダのリストにする。"""
    p = os.path.expanduser(pattern.replace("$HOME", str(home)))
    if pattern.startswith("~/") or pattern == "~":
        p = str(home) + pattern[1:]
    if any(c in p for c in "*?["):
        return sorted(Path(x) for x in glob.glob(p) if os.path.isdir(x))
    return [Path(p)]


def _for_cwd(table, cwd, home):
    """screenshot_by_cwd のうち、cwd に前方一致する（フォルダの区切りで）一番長いもの。無ければ None。"""
    if not cwd or not table:
        return None
    best = None
    # 両方を realpath にしてから比べる（/home が /var/home へのリンクの環境、macOS の /tmp → /private/tmp など）
    cwd = os.path.realpath(cwd)
    for prefix, dirs in table.items():
        root = os.path.realpath(_expand(prefix, home)[0]) if not any(c in prefix for c in "*?[") else None
        if root and (cwd == root or cwd.startswith(root.rstrip("/") + "/")):
            if best is None or len(root) > len(best[0]):
                best = (root, dirs)
    return best[1] if best else None


def screenshot_dirs(cfg, env, os_name=None, cwd=None):
    """スクリーンショットを探すフォルダ（重なりなし）。作業フォルダの設定 > screenshot_dir > OS の普通の場所。"""
    home = _home(env)
    given = _for_cwd(cfg.get("screenshot_by_cwd") or {}, cwd and os.path.normpath(cwd), home) or cfg.get("screenshot_dir")
    if isinstance(given, str):
        given = [given]
    patterns = given or default_dirs(env, os_name or os.uname().sysname)
    out, seen = [], set()
    for pat in patterns:
        for d in _expand(pat, home):
            key = os.path.realpath(d)        # ~/.steam/steam は ~/.local/share/Steam へのリンクのことが多い
            if key not in seen:
                seen.add(key)
                out.append(d)
    return out


def latest_screenshot(dirs, names=None):
    """置き場の直下の、名前の合う画像のうち、一番新しいもの (パス, 更新時刻)。無ければ None。"""
    # macOS のファイル名は NFD（濁点が分かれる）なので、名前も型も NFC にそろえてから比べる
    pats = [unicodedata.normalize("NFC", n).lower() for n in (names or DEFAULT_NAMES)]
    best = None
    for d in dirs:
        try:
            with os.scandir(d) as it:
                n = 0
                for e in it:
                    low = unicodedata.normalize("NFC", e.name).lower()
                    if os.path.splitext(low)[1] not in IMAGE_EXT or not any(fnmatch.fnmatchcase(low, p) for p in pats):
                        continue
                    n += 1
                    if n > SCAN_LIMIT:
                        break
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


def screenshot_text(cfg, env, now=None, os_name=None, cwd=None):
    w = WORDS[cfg["language"]]
    dirs = screenshot_dirs(cfg, env, os_name, cwd)
    found = latest_screenshot(dirs, cfg.get("screenshot_names"))
    if not found:
        return w["shot_none"].format(dirs=", ".join(str(d) for d in dirs) or "-")
    path, mtime = found
    now = time.time() if now is None else now
    args = dict(path=path, clock=time.strftime("%m-%d %H:%M", time.localtime(mtime)), age=_age(now - mtime, w))
    limit = cfg.get("screenshot_max_age_min")
    if limit and now - mtime > limit * 60:
        return w["shot_old"].format(limit=w["limit"].format(n=limit), **args)
    return w["shot"].format(**args)


# ---- 足す文 ----------------------------------------------------------------

def render(found, cfg, env, now=None, os_name=None, cwd=None):
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
                shot = screenshot_text(cfg, env, now, os_name, cwd)
            text = text.replace("{screenshot}", shot)
        lines.append(f"- {name if n is None else f'{name} {n}'}: {text.strip()}")
    if cfg["number_lists"]:
        lines.append(f"- {w['numbering']}")
    shown = w["sep"].join(name if n is None else f"{name} {n}" for name, n in found)
    return "\n".join(lines), (w["notice"].format(tokens=shown) if cfg["notice"] else None)


# ---- 記録 ------------------------------------------------------------------

LOG_MAX = 256 * 1024     # errors.log の大きさの上限。超えたら errors.log.1 に回す（設定の誤りは毎ターン出るため）


def state_dir(env):
    base = env.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        return Path(base) / "claude-prompt-suffix"
    return Path(tempfile.gettempdir()) / f"claude-prompt-suffix-{os.getuid()}"


def log_error(env, message):
    """設定の問題や例外を残す（stdout・stderr には出さない。claude-kitty doctor の「設定: prompt_suffix」でも見える）。

    直前の行と同じ文なら書かない（壊れた設定は毎ターン同じ問題を出すので、ログを埋めないため）。"""
    try:
        d = state_dir(env)
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
        return None                       # 保険: サブエージェントの入力に hook が呼ばれる版があれば、それは親のモデルの文
    prompt = data.get("prompt")
    if not isinstance(prompt, str):
        return None
    cfg, problems = load_config(env)
    for p in problems:
        log_error(env, p)
    if not cfg["enabled"]:
        return None
    found = find(prompt, cfg["table"])
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else None
    context, notice = render(found, cfg, env, now, os_name, cwd)
    if not context:
        return None
    out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}
    if notice:
        out["systemMessage"] = notice
    return out
