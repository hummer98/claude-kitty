"""claude-guard の中身。名前の付いた「ガード」が働いている間だけ、決めたパスへの書き込みと、決めたコマンドの形を止める。

PreToolUse hook（Bash / Edit / Write / NotebookEdit）から呼ぶ。判定は AI を使わず、ここだけで行う。
副作用は、設定と状態のファイルを読むこと、働く条件を調べること（止める候補があるときだけ）、errors.log に
書くことだけ。誤って止めない方に倒す: 読めない入力・設定の誤り・例外では通す。

設定（~/.config/claude-kitty/config.json の guard_hook の節）:

  {"guard_hook": {"guards": {
     "presenting": {
       "process": "^\\\\./slides-app",                 働く条件 1: pgrep -f の正規表現（リストも可）
       "command": ["test", "-e", "/run/lab/busy"],     働く条件 2: 終了コード 0 なら働く（timeout 付き）
       "command_timeout": 2,
                                                      働く条件 3: claude-guard on <名前> で立てた旗（常に有効）
       "paths": ["~/lab/config"],                      書き込みを止めるパス
       "commands": ["git push", {"regex": "^deploy\\\\b"},
                    {"program": "^svcctl(\\\\.py)?$", "value_options": ["--owner"],
                     "block": ["deploy", {"sub": "config", "if_any": ["--set"]}]}],
       "allow_commands": ["git push --dry-run"],       例外として通す形
       "message": "{name} が働いています（{reason}）。後で行ってください。"}}}}

条件は OR。commands / allow_commands の要素は、文字列（語の並びの頭が一致）、{"regex"}（単純なコマンド 1 つの
文字列に re.search）、{"program", "block"}（プログラム名の正規表現とサブコマンドの一覧）のどれか。

通す道（e2e など）:
  - 環境変数 CLAUDE_GUARD_ALLOW=<名前>（カンマ区切り可）か all。hook のプロセスの環境か、Bash のコマンドの前に付けた形
  - 許可のファイル $XDG_RUNTIME_DIR/claude-guard/<名前>.allow（claude-guard allow。期限まで有効）
"""
import json
import os
import re
import time
from pathlib import Path

TAG = "[claude-guard]"
ALLOW_ENV = "CLAUDE_GUARD_ALLOW"
MAX_MINUTES = 7 * 24 * 60
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
DEFAULT_MESSAGE = ("ガード「{name}」が働いている間（{condition}）は、この操作を止めています: {what}。"
                   "いまは行わず、ガードが外れてから行ってください。急ぐときは、依頼主に止められたことを伝えて指示を待ってください。")

# 書き込みのツールと、パスの入るフィールド
FILE_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}

# 作業の木を変える git のサブコマンド。stash は list / show 以外（push も木を変える）
GIT_WRITE = {"merge", "pull", "checkout", "switch", "restore", "reset", "commit", "rebase", "cherry-pick",
             "revert", "am", "apply", "clean", "mv", "rm", "stash", "clone", "worktree", "init", "bisect", "submodule",
             "sparse-checkout", "checkout-index", "read-tree", "archive", "format-patch"}
GIT_READ_STASH = {"list", "show"}
GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}

# 引数のどれかが保護したパスなら止めるもの
ANY_ARG_WRITERS = {"mkdir", "rm", "rmdir", "mv", "touch", "unlink", "truncate", "shred", "chmod", "chown",
                   "chgrp", "tee", "patch"}
# 行き先（最後の引数か -t）だけを見るもの。保護したパスから外へ写すのは読むだけなので通す
DEST_WRITERS = {"cp", "ln", "rsync", "install", "scp"}
# 前に付いて、後ろに本当のコマンドが続くもの。値は、そのラッパーの値をとるオプション（本体を値と取り違えないよう別々に持つ）
WRAPPERS = {
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "nohup": set(), "exec": {"-a"}, "time": {"-f", "--format", "-o", "--output"}, "command": set(), "builtin": set(),
    "nice": {"-n", "--adjustment"}, "ionice": {"-c", "--class", "-n", "--classdata", "-p"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "sudo": {"-u", "--user", "-g", "--group", "-C", "-D", "--chdir", "-h", "--host", "-p", "--prompt", "-U", "-r", "-t"},
    "doas": {"-u", "-C"}, "stdbuf": {"-i", "-o", "-e"}, "setsid": set(), "chronic": set(),
    "xargs": {"-n", "-L", "-P", "-d", "-I", "-E", "-s", "-a", "--max-args", "--max-procs", "--delimiter", "--arg-file"},
    "flock": {"-w", "--timeout", "-E", "--conflict-exit-code"},
}
PYTHON_VALUE_OPTS = {"-X", "-W", "-Q"}
# 複合文の予約語。後ろに本当のコマンドが続く
RESERVED = {"then", "do", "else", "elif", "if", "while", "until", "{", "}", "!", "fi", "done", "esac", "time"}
# cp などの、値をとるオプション（行き先を最後の引数とみるときに、その値を飛ばす）
DEST_VALUE_OPTS = {
    "cp": {"-S", "--suffix"}, "ln": {"-S", "--suffix"}, "install": {"-m", "--mode", "-o", "--owner", "-g", "--group", "-S"},
    "rsync": {"-e", "--rsh", "--exclude", "--include", "--filter", "-f", "--exclude-from", "--include-from",
              "--files-from", "--log-file", "--backup-dir", "--partial-dir", "--temp-dir", "-T", "--chmod", "--chown",
              "--suffix", "--compare-dest", "--copy-dest", "--link-dest"},
    "scp": {"-P", "-i", "-o", "-F", "-c", "-l", "-J", "-S"},
}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
PYTHON = re.compile(r"^(python|pypy)[0-9.]*$")
ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
VAR = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
HEREDOC = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\?([A-Za-z0-9_.-]+))")
OPCHARS = ";&|()<>\n"


# ---- 置き場所・記録 ----------------------------------------------------------

def config_path(env):
    """lib/config.py と同じ場所。hook を決して失敗させないよう、config.py（view 側）は読み込まずに持つ。"""
    if env.get("CLAUDE_KITTY_CONFIG"):
        return Path(env["CLAUDE_KITTY_CONFIG"])
    base = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else Path.home() / ".config"
    return base / "claude-kitty" / "config.json"


def state_dir(env):
    base = env.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        return Path(base) / "claude-guard"
    import tempfile
    return Path(tempfile.gettempdir()) / f"claude-guard-{os.getuid()}"


def log_error(env, message, once=False):
    """設定の問題や例外を残す（stdout・stderr には出さない）。once なら、最近残したもの（64 件）と同じときは書かない
    （設定の誤りは呼ばれるたびに起きるので、問題が複数あってもログが埋まらないように）。"""
    try:
        d = state_dir(env)
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        if once:
            import hashlib
            mark = d / "recent_problems"
            key = hashlib.sha1(message.encode()).hexdigest()[:16]
            try:
                seen = mark.read_text().split()
            except OSError:
                seen = []
            if key in seen:
                return
            mark.write_text("\n".join((seen + [key])[-64:]))
        with open(d / "errors.log", "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
    except OSError:
        pass


# ---- 設定 ------------------------------------------------------------------

def _strings(value):
    if isinstance(value, str):
        value = [value]
    if isinstance(value, list) and all(isinstance(x, str) and x for x in value):
        return value
    return None


def _compile_entry(e, where, problems):
    """commands / allow_commands の 1 つを ("prefix", [語]) / ("regex", re) / ("program", dict) にする。"""
    if isinstance(e, str) and e.strip():
        return ("prefix", e.split())
    if isinstance(e, dict) and isinstance(e.get("regex"), str):
        try:
            return ("regex", re.compile(e["regex"]))
        except re.error as err:
            problems.append(f"{where}.regex が正規表現として読めない: {err}")
            return None
    if isinstance(e, dict) and isinstance(e.get("program"), str):
        try:
            prog = re.compile(e["program"])
        except re.error as err:
            problems.append(f"{where}.program が正規表現として読めない: {err}")
            return None
        vopts = _strings(e.get("value_options", []))
        if vopts is None:
            problems.append(f"{where}.value_options は文字列のリスト")
            vopts = []
        entries = e.get("block")
        if not isinstance(entries, list) or not entries:
            problems.append(f"{where}.block が空か、リストでない")
            return None
        block = {}
        for i, b in enumerate(entries):
            if isinstance(b, str) and b:
                block[b] = ((), ())
            elif isinstance(b, dict) and isinstance(b.get("sub"), str) and b["sub"]:
                if_any, unless_any = _strings(b.get("if_any", [])), _strings(b.get("unless_any", []))
                if if_any is None or unless_any is None:
                    problems.append(f"{where}.block[{i}] の if_any / unless_any は文字列のリスト")
                    continue
                block[b["sub"]] = (tuple(if_any), tuple(unless_any))
            else:
                problems.append(f"{where}.block[{i}] は文字列か {{\"sub\": ...}}")
        return ("program", {"program": prog, "value_options": set(vopts), "block": block})
    problems.append(f"{where} は文字列・{{\"regex\"}}・{{\"program\", \"block\"}} のどれか")
    return None


def _entries(value, where, problems):
    if value is None:
        return []
    if not isinstance(value, list):
        problems.append(f"{where} はリスト")
        return []
    out = []
    for i, e in enumerate(value):
        c = _compile_entry(e, f"{where}[{i}]", problems)
        if c:
            out.append(c)
    return out


def _guard(name, sec, problems, home):
    """1 つのガードの設定を読む。読めない項目は外す（ほかの項目とほかのガードは生かす）。"""
    where = f"guard_hook.guards.{name}"
    g = {"name": name, "enabled": True, "process": [], "command": None, "command_timeout": 2.0,
         "paths": [], "commands": [], "allow_commands": [], "message": ""}
    enabled = sec.get("enabled", True)
    if not isinstance(enabled, bool):
        problems.append(f"{where}.enabled は true / false: {enabled!r}")
        enabled = True
    g["enabled"] = enabled
    if "process" in sec:
        procs = _strings(sec["process"])
        if not procs:
            problems.append(f"{where}.process（pgrep -f の正規表現）が正しくない。この条件は使わない")
            procs = []
        for p in list(procs):
            try:
                re.compile(p)
            except re.error as e:
                problems.append(f"{where}.process の正規表現が読めない: {p!r}: {e}。この条件は使わない")
                procs = []
                break
        g["process"] = procs
    if "command" in sec:
        c = sec["command"]
        if isinstance(c, str) and c.strip():
            g["command"] = ["sh", "-c", c]
        elif isinstance(c, list) and c and all(isinstance(x, str) for x in c):
            g["command"] = list(c)
        else:
            problems.append(f"{where}.command は文字列（sh -c で動かす）か文字列のリスト。この条件は使わない")
    t = sec.get("command_timeout", 2)
    if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 < t <= 30:
        problems.append(f"{where}.command_timeout は 0 より大きく 30 以下の秒数: {t!r}")
        t = 2
    g["command_timeout"] = float(t)
    paths = _strings(sec.get("paths", []))
    if paths is None:
        problems.append(f"{where}.paths は文字列のリスト")
        paths = []
    for p in paths:
        full = resolve(p, "/", home) if p.startswith("~") else p
        if not os.path.isabs(full):
            problems.append(f"{where}.paths は絶対パスか ~ で始める: {p!r}")
            continue
        g["paths"].append(os.path.normpath(full))
    g["commands"] = _entries(sec.get("commands"), f"{where}.commands", problems)
    g["allow_commands"] = _entries(sec.get("allow_commands"), f"{where}.allow_commands", problems)
    msg = sec.get("message", "")
    if not isinstance(msg, str):
        problems.append(f"{where}.message は文字列")
        msg = ""
    g["message"] = msg
    if not g["paths"] and not g["commands"]:
        problems.append(f"{where} に止めるもの（paths / commands）が無い")
    return g


def load_guard_config(env, path=None):
    """(guards, problems) を返す。guards は名前の順のリスト。

    hook の中で使うので例外にしない。壊れたガード・項目は外し、問題を problems に入れる（status で見せる）。
    """
    problems = []
    path = Path(path) if path else config_path(env)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return [], problems
    except (OSError, ValueError) as e:
        return [], [f"設定ファイルを読めない: {path}: {e}"]
    sec = data.get("guard_hook") if isinstance(data, dict) else None
    if sec is None:
        return [], problems
    guards = sec.get("guards") if isinstance(sec, dict) else None
    if not isinstance(guards, dict):
        return [], [f"{path} の guard_hook.guards がオブジェクトでない。何も止めない"]
    out = []
    for name in sorted(guards):
        if not NAME.match(name) or name == "all":
            problems.append(f"ガードの名前が使えない（英数字と _ . -、all は不可）: {name!r}")
            continue
        if not isinstance(guards[name], dict):
            problems.append(f"guard_hook.guards.{name} がオブジェクトでない")
            continue
        out.append(_guard(name, guards[name], problems, env.get("HOME") or os.path.expanduser("~")))
    return out, problems


# ---- パス -------------------------------------------------------------------

def _forms(path):
    """比べる形: 書かれたままを正規化したものと、シンボリックリンクを解いたもの（/home → /var/home など）。"""
    norm = os.path.normpath(path)
    try:
        real = os.path.realpath(norm)
    except (OSError, ValueError):
        real = norm
    return {norm, real}


class Roots:
    def __init__(self, roots):
        self.forms = set()
        for r in roots:
            self.forms |= _forms(r)

    def covers(self, path):
        """path が保護したパスそのものか、その下か。"""
        for f in _forms(path):
            for r in self.forms:
                if f == r or f.startswith(r.rstrip("/") + "/"):
                    return True
        return False


def resolve(word, cwd, home):
    """コマンドの語をパスにする（~ と相対パスを解く）。"""
    if word == "~" or word.startswith("~/"):
        word = home + word[1:]
    if not os.path.isabs(word):
        word = os.path.join(cwd, word)
    return os.path.normpath(word)


# ---- Bash のコマンドを読む ----------------------------------------------------

def strip_heredocs(text):
    """ヒアドキュメントの本文を除く（中身は入力の文字で、コマンドではない。中の ' で字句の解析が壊れないように）。"""
    out = []
    pending = []
    for line in text.split("\n"):
        if pending:
            strip_tabs, delim = pending[0]
            if (line.lstrip("\t") if strip_tabs else line) == delim:
                pending.pop(0)
            continue
        out.append(line)
        # 引用の中の << はヒアドキュメントでない。正確でなくてよいので、引用を外した行で探す
        bare = re.sub(r"'[^']*'|\"(?:\\.|[^\"\\])*\"", "", line)
        for m in HEREDOC.finditer(bare):
            if bare[m.start():m.start() + 3] == "<<<":
                continue
            pending.append((m.group(1) == "-", m.group(2) or m.group(3) or m.group(4)))
    return "\n".join(out)


def prepare(text):
    """shlex に渡す前に、bash と shlex の違いを埋める（引用の外だけ）。

    - 注釈: 語の頭の # だけ（shlex は語の途中の # でも行の残りを捨てる。a#b; rm ... を見逃さないため）
    - バッククォート: 中のコマンドも読むよう、区切りに変える
    - fd の番号: 2>/dev/null の 2 は引数でない（shlex は 2 と > に分けるので、引数の最後と取り違える）
    """
    out = []
    quote = None
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if quote:
            out.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        prev = text[i - 1] if i else "\n"
        if c in "'\"":
            quote = c
        elif c == "\\" and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        elif c == "#" and (prev in " \t\r\n;&|()"):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        elif c == "`":
            out.append("\n")
            i += 1
            continue
        elif c.isdigit() and prev in " \t\r\n;&|()":
            m = re.match(r"\d+(?=[<>])", text[i:])
            if m:
                i += m.end()
                continue
        out.append(c)
        i += 1
    return "".join(out)


def tokenize(text):
    """語と演算子に分ける。引用が閉じていないなど shlex で読めないときは、空白と演算子で粗く分ける。"""
    import shlex
    text = prepare(strip_heredocs(text).replace("\\\n", " "))
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=OPCHARS)
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        lex.commenters = ""
        return list(lex)
    except ValueError:
        return [t for t in re.split(r"[ \t\r]+|([;&|()<>\n]+)", text) if t]


def is_op(tok):
    return bool(tok) and all(c in OPCHARS for c in tok)


def segments(tokens):
    """単純なコマンドごとに [("cmd", words, redirects) | ("push",) | ("pop",)] にする。

    redirects は出力のリダイレクトの行き先。push / pop はサブシェル ( ... ) の出入り（中の cd を外に漏らさない）。"""
    out = []
    words, redirs = [], []

    def flush():
        nonlocal words, redirs
        if words or redirs:
            out.append(("cmd", words, redirs))
        words, redirs = [], []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if not is_op(t):
            words.append(t)
            i += 1
        elif ">" in t:
            if t.endswith("&") and nxt is not None and re.fullmatch(r"\d+-?|-", nxt):
                i += 2                          # >&2 などは fd の付け替え
            elif nxt is not None and not is_op(nxt):
                redirs.append(nxt)
                i += 2
            else:
                i += 1
        elif "<" in t:
            i += 2 if (nxt is not None and not is_op(nxt)) else 1
        else:
            flush()
            for c in t:
                if c == "(":
                    out.append(("push",))
                elif c == ")":
                    out.append(("pop",))
            i += 1
    flush()
    return out


def _expand(word, vars_):
    if "$" not in word:
        return word
    return VAR.sub(lambda m: vars_.get(m.group(1) or m.group(2), m.group(0)), word)


def _split_assignments(words):
    """先頭の NAME=VALUE を外す。(assignments, rest)"""
    assigns = {}
    i = 0
    while i < len(words):
        m = ASSIGN.match(words[i])
        if not m:
            break
        assigns[m.group(1)] = m.group(2)
        i += 1
    return assigns, words[i:]


def _unwrap(words):
    """予約語（then / do / ! など）と env / nohup / timeout などを外し、python3 x.py の形は x.py をプログラムとみる。
    (words, assigns)。for / case の頭は語の並びでコマンドでないので、空にする。"""
    assigns = {}
    while words:
        name = os.path.basename(words[0])
        if words[0] in RESERVED:
            words = words[1:]
            continue
        if words[0] in ("for", "case", "select", "function"):
            return [], assigns
        more, rest = _split_assignments(words)
        if more:                                # then X=1 cmd の形
            assigns.update(more)
            words = rest
            continue
        if name in WRAPPERS:
            vopts = WRAPPERS[name]
            i = 1
            while i < len(words):
                w = words[i]
                m = ASSIGN.match(w)
                if name == "env" and m:
                    assigns[m.group(1)] = m.group(2)
                    i += 1
                elif w.startswith("-"):
                    i += 2 if (w in vopts and "=" not in w) else 1
                elif name == "timeout" and re.fullmatch(r"[0-9.]+[smhd]?", w):
                    i += 1
                elif name == "flock" and i == 1:
                    i += 1                        # flock の最初の引数は錠のファイル
                else:
                    break
            words = words[i:]
            continue
        if PYTHON.match(name):
            i = 1
            while i < len(words) and words[i].startswith("-"):
                if words[i] in ("-m", "-c"):
                    return words, assigns       # -m mod や -c '...' は中身を読まない
                i += 2 if words[i] in PYTHON_VALUE_OPTS else 1
            if i < len(words):
                return words[i:], assigns
        break
    return words, assigns


def _positional(args, value_opts=()):
    """オプションでない引数。value_opts は値をとるオプション（その値も飛ばす）。"""
    out = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-" or not a.startswith("-"):
            out.append(a)
        elif a in value_opts:
            i += 1
        i += 1
    return out


def allow_names(value):
    """CLAUDE_GUARD_ALLOW の値を名前の集合にする（all / 1 は全部）。"""
    if not isinstance(value, str) or not value.strip():
        return set()
    names = {v.strip() for v in value.split(",") if v.strip()}
    return {"all"} if names & {"all", "1"} else names


def _git_split(args):
    """git の全体のオプションを読み、(-C などで変えた場所の列, サブコマンドから後) を返す。"""
    dirs = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-C", "--work-tree", "--git-dir") and i + 1 < len(args):
            dirs.append(args[i + 1])
            i += 2
        elif a.startswith("--work-tree=") or a.startswith("--git-dir="):
            dirs.append(a.split("=", 1)[1])
            i += 1
        elif a in GIT_VALUE_OPTS and i + 1 < len(args):
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            break
    return dirs, args[i:]


def normalize(words):
    """比べるための形: 最初の語はファイル名。git は全体のオプション（-C x、-c k=v、--no-pager）を外す。"""
    prog = os.path.basename(words[0])
    args = words[1:]
    if prog == "git":
        args = _git_split(args)[1]
    return [prog] + args


def match_entry(entry, words):
    """commands / allow_commands の 1 つが、単純なコマンド words に当たれば、その印（文字列）を返す。"""
    kind, spec = entry
    norm = normalize(words)
    prog = norm[0]
    if kind == "prefix":
        want = [os.path.basename(spec[0])] + spec[1:]
        return " ".join(want) if norm[:len(want)] == want else None
    if kind == "regex":
        text = " ".join(norm)
        return text if spec.search(text) else None
    if not spec["program"].search(prog):
        return None
    args = words[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a in spec["value_options"]:
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            break
    if i >= len(args) or args[i] not in spec["block"]:
        return None
    sub, rest = args[i], args[i + 1:]
    if_any, unless_any = spec["block"][sub]

    def has(flag):
        return any(r == flag or r.startswith(flag + "=") for r in rest)
    if if_any and not any(has(f) for f in if_any):
        return None
    if unless_any and any(has(f) for f in unless_any):
        return None
    return f"{prog} {sub}"


def _shell_c(args):
    """bash -c / -lc / -ec などの中身の文字列。無ければ None。"""
    for i, a in enumerate(args):
        if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", a):
            return args[i + 1] if i + 1 < len(args) else None
        if not a.startswith("-"):
            return None
    return None


class Analyzer:
    """1 回分のコマンドを 1 つのガードについて読み、止める理由を集める。cwd は cd を追う（cd X && ... の形のため）。"""

    def __init__(self, guard, cwd, env, depth=0):
        self.guard = guard
        self.roots = Roots(guard["paths"])
        self.cwd = cwd or os.getcwd()
        self.home = env.get("HOME") or os.path.expanduser("~")
        self.vars = {k: v for k, v in env.items() if isinstance(v, str)}
        self.vars.setdefault("HOME", self.home)
        self.allowed = set()                      # export CLAUDE_GUARD_ALLOW=... の後
        self.reasons = []
        self.depth = depth
        self._paths, self._cmds = [], []          # 単純なコマンド 1 つ分の理由（書き込み / コマンドの形）

    def path(self, word, base=None):
        return resolve(_expand(word, self.vars), base or self.cwd, self.home)

    def protected(self, word, base=None):
        return bool(self.roots.forms) and self.roots.covers(self.path(word, base))

    def add(self, what):
        if what not in self._paths:
            self._paths.append(what)

    def lets_through(self, names):
        return bool(names & {"all", self.guard["name"]})

    def run(self, command):
        stack = []
        for item in segments(tokenize(command)):
            if item[0] == "push":
                stack.append((self.cwd, dict(self.vars), set(self.allowed)))
            elif item[0] == "pop":
                if stack:
                    self.cwd, self.vars, self.allowed = stack.pop()
            else:
                self.segment(item[1], item[2])
        return self.reasons

    def segment(self, words, redirs):
        words = [_expand(w, self.vars) for w in words]
        assigns, rest = _split_assignments(words)
        if not rest:
            self.vars.update(assigns)          # X=val だけの行は、後のコマンドの変数になる
            self.allowed |= allow_names(assigns.get(ALLOW_ENV))
            return
        rest, more = _unwrap(rest)
        assigns.update(more)
        self._paths, self._cmds = [], []
        for r in redirs:
            if self.protected(r):
                self.add(f"保護したパスへのリダイレクト（> {r}）")
        if rest:
            self.command(rest, assigns)
        if self.lets_through(self.allowed | allow_names(assigns.get(ALLOW_ENV))):
            return
        # allow_commands はコマンドの形の理由だけを消す（git push --dry-run > <保護したパス> は止める）
        if self._cmds and rest and any(match_entry(e, rest) for e in self.guard["allow_commands"]):
            self._cmds = []
        for r in self._paths + self._cmds:
            if r not in self.reasons:
                self.reasons.append(r)

    def command(self, words, assigns):
        name = os.path.basename(words[0])
        args = words[1:]
        if "--help" in args:
            return                              # 使い方を見るだけ
        if name in ("cd", "pushd"):
            pos = _positional(args)
            target = pos[0] if pos else "~"
            if target != "-":
                self.cwd = self.path(target)
            return
        if name == "export":
            for a in args:
                m = ASSIGN.match(a)
                if m:
                    self.vars[m.group(1)] = m.group(2)
                    if m.group(1) == ALLOW_ENV:
                        self.allowed |= allow_names(m.group(2))
            return
        if name in SHELLS:
            inner = _shell_c(args)
            if inner is not None and self.depth < 3:
                sub = Analyzer(self.guard, self.cwd, {}, self.depth + 1)
                sub.vars, sub.home, sub.allowed = dict(self.vars), self.home, set(self.allowed)
                for r in sub.run(inner):
                    self.add(r)
                return
        for e in self.guard["commands"]:
            hit = match_entry(e, words)
            if hit and hit not in self._cmds:
                self._cmds.append(hit)
        if self.roots.forms:
            self.writes(name, args, assigns)

    def writes(self, name, args, assigns):
        if name == "git":
            self.git(args, assigns)
        elif name in ANY_ARG_WRITERS:
            for a in _positional(args):
                if a != "-" and self.protected(a):
                    self.add(f"{name} {a}")
                    break
        elif name in DEST_WRITERS:
            dest = self._dest(name, args)
            if dest and self.protected(dest):
                self.add(f"{name} の行き先 {dest}")
        elif name in ("sed", "perl"):
            inplace = any(a.startswith("--in-place") or (a.startswith("-") and not a.startswith("--") and "i" in a[1:])
                          for a in args) if name == "perl" else any(
                a.startswith("--in-place") or re.fullmatch(r"-[A-Za-z]*i.*", a) for a in args)
            if inplace:
                for a in _positional(args, ("-e", "-f")):
                    if self.protected(a):
                        self.add(f"{name} -i {a}")
                        break
        elif name == "dd":
            for a in args:
                if a.startswith("of=") and self.protected(a[3:]):
                    self.add(f"dd {a}")
        elif name in ("tar", "bsdtar"):
            self.tar(args)
        elif name == "unzip":
            if any(a in ("-l", "-t", "-v", "-Z", "-p", "-c") for a in args):
                return                          # 一覧・確かめ・標準出力へは書き込まない
            d = None
            for i, a in enumerate(args):
                if a == "-d" and i + 1 < len(args):
                    d = args[i + 1]
                elif a.startswith("-d") and len(a) > 2:
                    d = a[2:]
            if self.protected(d or "."):
                self.add(f"unzip の展開先 {d or self.cwd}")
        elif name in ("curl", "wget"):
            for i, a in enumerate(args):
                nxt = args[i + 1] if i + 1 < len(args) else None
                target = None
                if a in ("-o", "--output", "-O" if name == "wget" else "", "--output-document",
                         "-P", "--directory-prefix") and a and nxt:
                    target = nxt
                elif a.startswith(("--output=", "--output-document=", "--directory-prefix=")):
                    target = a.split("=", 1)[1]
                elif name == "curl" and a in ("-O", "--remote-name"):
                    target = "."
                if target and target != "-" and self.protected(target):
                    self.add(f"{name} の出力 {target}")
                    break
        elif name == "find":
            if any(a in ("-delete", "-exec", "-execdir", "-ok", "-okdir") for a in args):
                pos = [a for a in args if not a.startswith("-")][:1] or ["."]
                if self.protected(pos[0]):
                    self.add(f"find {pos[0]} -delete / -exec")

    def _dest(self, name, args):
        for i, a in enumerate(args):
            if a in ("-t", "--target-directory") and i + 1 < len(args):
                return args[i + 1]
            if a.startswith("--target-directory="):
                return a.split("=", 1)[1]
        pos = _positional(args, DEST_VALUE_OPTS.get(name, ()))
        if len(pos) > 1:
            return pos[-1]
        if len(pos) == 1 and name == "ln":
            return "."                          # ln -s x は cwd に作る
        return None

    def git(self, args, assigns):
        where = self.cwd
        for env_dir in ("GIT_WORK_TREE", "GIT_DIR"):
            if assigns.get(env_dir):
                where = self.path(assigns[env_dir], where)
        dirs, rest = _git_split(args)
        for d in dirs:
            where = self.path(d, where)
        if not rest:
            return
        sub, rest = rest[0], rest[1:]
        if sub not in GIT_WRITE:
            return
        pos = _positional(rest)
        if sub == "stash" and pos and pos[0] in GIT_READ_STASH:
            return
        if sub == "submodule" and pos and pos[0] in ("status", "summary", "foreach"):
            return
        if sub == "read-tree" and not any(a in ("-u", "--reset") for a in rest):
            return
        if sub == "clone":
            target = pos[1] if len(pos) > 1 else "."    # 行き先が無ければ cwd の下に作る
            if self.roots.covers(self.path(target, where)):
                self.add(f"git clone の行き先 {target}")
            return
        if sub == "worktree":
            if pos and pos[0] in ("add", "move", "remove") and (
                    self.roots.covers(where) or any(self.roots.covers(self.path(p, where)) for p in pos[1:])):
                self.add(f"git worktree {pos[0]}")
            return
        if sub == "init":
            target = pos[0] if pos else "."
            if self.roots.covers(self.path(target, where)):
                self.add(f"git init {target}")
            return
        if sub in ("archive", "format-patch"):
            out = None
            for i, a in enumerate(rest):
                if a in ("-o", "--output", "--output-directory") and i + 1 < len(rest):
                    out = rest[i + 1]
                elif a.startswith(("--output=", "--output-directory=")):
                    out = a.split("=", 1)[1]
            if out is None and sub == "archive":
                return                          # 標準出力へ
            if self.roots.covers(self.path(out or ".", where)):
                self.add(f"git {sub} の出力 {out or where}")
            return
        if self.roots.covers(where):
            self.add(f"git {sub}（{where}）")

    def tar(self, args):
        if not args:
            return
        first = args[0]
        mode = set() if first.startswith("-") else set(first)    # tar xzf 形
        for o in args:
            if o in ("--extract", "--get"):
                mode.add("x")
            elif o in ("--create", "--append", "--update"):
                mode.add("c")
            elif o.startswith("-") and not o.startswith("--"):
                mode |= set(o[1:])
        d, f = None, None
        for i, a in enumerate(args):
            nxt = args[i + 1] if i + 1 < len(args) else None
            if a in ("-C", "--directory") and nxt:
                d = nxt
            elif a.startswith("--directory="):
                d = a.split("=", 1)[1]
            elif a.startswith("--file="):
                f = a.split("=", 1)[1]
            elif (a == "--file" or re.fullmatch(r"-[A-Za-z]*f", a)) and nxt:
                f = nxt
        if not f and not first.startswith("-") and "f" in first and len(args) > 1:
            f = args[1]
        if "x" in mode and self.protected(d or "."):
            self.add(f"tar の展開先 {d or self.cwd}")
        if mode & set("cruA") and f and self.protected(f):
            self.add(f"tar で {f} を作る")


# ---- 旗と許可（期限付きの状態のファイル） ---------------------------------------

def _state_file(env, name, kind):
    if not NAME.match(name):
        raise ValueError(f"名前が使えない（英数字と _ . -）: {name!r}")
    return state_dir(env) / f"{name}.{kind}"


def read_state(env, name, kind, now=None):
    """期限内の旗（kind="on"）か許可（kind="allow"）があれば dict を返す。無い・期限切れ・壊れていれば None。"""
    try:
        data = json.loads(_state_file(env, name, kind).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    until = data.get("until")
    if isinstance(until, bool) or not isinstance(until, (int, float)):
        return None
    now = time.time() if now is None else now
    return data if now < until else None


def write_state(env, name, kind, minutes, reason, by=None, now=None):
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not 0 < minutes <= MAX_MINUTES:
        raise ValueError(f"--minutes は 0 より大きく {MAX_MINUTES} 以下")
    reason = (reason or "").strip()
    if kind == "allow" and not reason:
        raise ValueError("--reason が要る（誰が何のために止めを外したかを残すため）")
    now = time.time() if now is None else now
    path = _state_file(env, name, kind)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = {"until": now + minutes * 60, "reason": reason, "created": now}
    if by:
        data["by"] = by
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(tmp, path)
    return data


def remove_state(env, name, kind):
    try:
        _state_file(env, name, kind).unlink()
        return True
    except FileNotFoundError:
        return False


# ---- 排他のロック（持ち主のある旗） -------------------------------------------

def owner_id(env, explicit=None):
    """持ち主の印。Claude Code のセッションならその ID、ほかは呼んだシェルの pid と host。"""
    import socket
    if explicit:
        return explicit
    if env.get("CLAUDE_CODE_SESSION_ID"):
        return env["CLAUDE_CODE_SESSION_ID"]
    return f"pid {os.getppid()}@{socket.gethostname()}"


class _Mutex:
    """旗を調べてから書くまでを、ほかの acquire / release / on / off と重ねないための flock。"""

    def __init__(self, env, name):
        self.path = state_dir(env) / f".{name}.mutex"

    def __enter__(self):
        import fcntl
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        import fcntl
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def acquire(env, name, minutes, reason, owner, now=None):
    """旗を持ち主つきで立てる。(True, 中身) か、先に持っている人がいれば (False, その中身)。

    期限の切れた旗・壊れた旗は取り直す。新しく作るときは O_CREAT|O_EXCL で、flock の外の書き手
    （古い版や手で置いたファイル）とも二重にならないようにする。"""
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not 0 < minutes <= MAX_MINUTES:
        raise ValueError(f"--minutes は 0 より大きく {MAX_MINUTES} 以下")
    now = time.time() if now is None else now
    path = _state_file(env, name, "on")
    data = {"until": now + minutes * 60, "reason": (reason or "").strip(), "created": now, "owner": owner}
    with _Mutex(env, name):
        held = read_state(env, name, "on", now)
        if held:
            return False, held
        try:
            path.unlink()                  # 期限切れか壊れたもの
        except FileNotFoundError:
            pass
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False, read_state(env, name, "on", now) or {}
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, ensure_ascii=False))
    return True, data


def set_flag(env, name, minutes, reason, by=None, now=None):
    """持ち主の無い旗（全員を止める）を立てる。持ち主のあるロックが生きていれば上書きしない。(True, 中身) / (False, ロック)"""
    with _Mutex(env, name):
        held = read_state(env, name, "on", now)
        if held and held.get("owner"):
            return False, held
        return True, write_state(env, name, "on", minutes, reason, by=by, now=now)


def clear_flag(env, name, force=False, now=None):
    """持ち主の無い旗を外す。("removed" | "none" | "locked", 中身)。持ち主のあるロックは release で外す。"""
    with _Mutex(env, name):
        held = read_state(env, name, "on", now)
        if held and held.get("owner") and not force:
            return "locked", held
        return ("removed" if remove_state(env, name, "on") and held else "none"), held


def release(env, name, owner, force=False, now=None):
    """持ち主（か force）なら旗を外す。("released" | "none" | "not-owner", 中身)"""
    with _Mutex(env, name):
        held = read_state(env, name, "on", now)
        if not held:
            remove_state(env, name, "on")  # 期限切れの残りは誰でも片付けてよい
            return "none", None
        if not force and held.get("owner") != owner:
            return "not-owner", held
        remove_state(env, name, "on")
        return "released", held


# ---- 働く条件 ---------------------------------------------------------------

def process_running(patterns):
    """pgrep -f を 1 回だけ呼ぶ。動いていれば True、いなければ False、pgrep が使えなければ None。"""
    # subprocess.run より posix_spawn の方が、この機械で 30ms ほど速かった（hook は毎回呼ばれるため）
    pat = "|".join(f"({p})" for p in patterns) if len(patterns) > 1 else patterns[0]
    try:
        quiet = [(os.POSIX_SPAWN_OPEN, fd, os.devnull, os.O_WRONLY, 0) for fd in (1, 2)]
        pid = os.posix_spawnp("pgrep", ["pgrep", "-f", pat], os.environ, file_actions=quiet)
        deadline = time.monotonic() + 5
        while True:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            if time.monotonic() > deadline:
                os.kill(pid, 9)
                os.waitpid(pid, 0)
                return None
            time.sleep(0.002)
    except OSError:
        return None
    return {0: True, 1: False}.get(os.waitstatus_to_exitcode(status))


def command_true(argv, timeout):
    """判定のコマンドの終了コードが 0 なら True。動かせない・時間切れは None（働かないと数える）。"""
    import subprocess
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=timeout).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None


class Probe:
    """働く条件を調べる。テストでは差し替える。"""
    process = staticmethod(process_running)
    command = staticmethod(command_true)


def _minutes_left(until, now):
    import math
    return max(1, math.ceil((until - now) / 60))


def active(guard, env, probe=Probe, now=None, assume=False):
    """ガードが働いていれば、その条件の説明（文字列）と旗の理由を返す。働いていなければ (None, "")。

    軽い順に調べる: 旗（ファイル 1 つ）→ process（pgrep 1 回）→ command。"""
    if assume:
        return "--assume-active", ""
    flag = read_state(env, guard["name"], "on", now)
    if flag:
        left = _minutes_left(flag["until"], time.time() if now is None else now)
        who = f"、持ち主 {flag['owner']}" if flag.get("owner") else ""
        return f"旗（残り {left} 分{who}）", flag.get("reason", "")
    if guard["process"]:
        r = probe.process(guard["process"])
        if r is None:
            log_error(env, f"{guard['name']}: pgrep を呼べない。process の条件は働かないと数える", once=True)
        elif r:
            return f"プロセス {', '.join(guard['process'])}", ""
    if guard["command"]:
        r = probe.command(guard["command"], guard["command_timeout"])
        if r is None:
            log_error(env, f"{guard['name']}: 判定のコマンドを動かせないか時間切れ。働かないと数える", once=True)
        elif r:
            return "判定のコマンド", ""
    return None, ""


# ---- 判定 ------------------------------------------------------------------

def find_reasons(guard, tool, tool_input, cwd, env):
    """1 つのガードについて、止める候補の理由のリスト（働いているかは見ない）。"""
    if tool in FILE_TOOLS:
        roots = Roots(guard["paths"])
        p = tool_input.get(FILE_TOOLS[tool]) or tool_input.get("file_path") or tool_input.get("notebook_path")
        if isinstance(p, str) and p and roots.forms:
            home = env.get("HOME") or os.path.expanduser("~")
            if roots.covers(resolve(p, cwd or os.getcwd(), home)):
                return [f"{tool} {p}"]
        return []
    if tool == "Bash":
        cmd = tool_input.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            return []
        return Analyzer(guard, cwd, env).run(cmd)
    return []


class _Blank(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def reason_text(guard, condition, flag_reason, reasons):
    what = "、".join(reasons[:3]) + (" ほか" if len(reasons) > 3 else "")
    fields = _Blank(name=guard["name"], condition=condition, reason=flag_reason or "理由の記載なし", what=what)
    text = DEFAULT_MESSAGE.format_map(fields)
    if guard["message"]:
        try:
            text += " " + guard["message"].format_map(fields)
        except (ValueError, IndexError):
            text += " " + guard["message"]
    return f"{TAG} {text}"


def evaluate(data, env, probe=Probe, now=None, path=None, assume=False):
    """hook の入力（dict）から、止める理由（文字列）か None（通す）を返す。"""
    tool = data.get("tool_name")
    tool_input = data.get("tool_input")
    if not isinstance(tool, str) or not isinstance(tool_input, dict):
        return None
    guards, problems = load_guard_config(env, path)
    for p in problems:
        log_error(env, p, once=True)
    allowed = allow_names(env.get(ALLOW_ENV))
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else None
    session = data.get("session_id") if isinstance(data.get("session_id"), str) else None
    texts = []
    for g in guards:
        if not g["enabled"] or allowed & {"all", g["name"]}:
            continue
        if session:
            flag = read_state(env, g["name"], "on", now)
            if flag and flag.get("owner") == session:
                continue                  # ロックの持ち主（デプロイしている本人）は、このガードでは止めない
        reasons = find_reasons(g, tool, tool_input, cwd, env)
        if not reasons:
            continue                      # 止める候補が無ければ、働く条件も調べない（pgrep を呼ばない）
        if read_state(env, g["name"], "allow", now) or read_state(env, "all", "allow", now):
            continue                      # 許可を先に見る（許可の間は pgrep を呼ばない）
        condition, flag_reason = active(g, env, probe, now, assume)
        if not condition:
            continue
        texts.append(reason_text(g, condition, flag_reason, reasons))
    return "\n".join(texts) if texts else None


def decide(stdin_text, env, probe=Probe, now=None, path=None):
    """hook の 1 回分。stdout に出す dict か None（通す）を返す。"""
    try:
        data = json.loads(stdin_text)
    except ValueError:
        log_error(env, "hook の入力が JSON として読めない。通す", once=True)
        return None
    if not isinstance(data, dict):
        return None
    if data.get("hook_event_name", "PreToolUse") != "PreToolUse":
        return None
    reason = evaluate(data, env, probe, now, path)
    if not reason:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


# ---- status ----------------------------------------------------------------

def status_lines(env, probe=Probe, now=None, path=None):
    """統括が読む用に、ガードごとに 1 行。設定の問題は最後に「問題:」で出す（保護が外れたことに気づけるよう）。"""
    now = time.time() if now is None else now
    guards, problems = load_guard_config(env, path)
    lines = []
    if not guards:
        lines.append(f"ガードなし（{path or config_path(env)} の guard_hook.guards）: 何も止めない")
    env_allowed = allow_names(env.get(ALLOW_ENV))
    for g in guards:
        if not g["enabled"]:
            lines.append(f"{g['name']}: 無効（enabled: false）")
            continue
        condition, flag_reason = active(g, env, probe, now)
        conds = (["process"] if g["process"] else []) + (["command"] if g["command"] else []) + ["旗"]
        parts = [f"{g['name']}: " + (f"働いている（{condition}" + (f"、{flag_reason}" if flag_reason else "") + "）"
                                      if condition else "働いていない")]
        parts.append("条件 " + "/".join(conds))
        parts.append(f"止めるもの paths {len(g['paths'])}・commands {len(g['commands'])}"
                     + (f"・例外 {len(g['allow_commands'])}" if g["allow_commands"] else ""))
        allow = read_state(env, g["name"], "allow", now) or read_state(env, "all", "allow", now)
        if allow:
            parts.append(f"許可あり（残り {_minutes_left(allow['until'], now)} 分、{allow.get('reason', '')}）")
        if env_allowed & {"all", g["name"]}:
            parts.append(f"{ALLOW_ENV} で通す")
        lines.append("、".join(parts))
    for p in problems:
        lines.append(f"問題: {p}")
    return lines, bool(problems)
