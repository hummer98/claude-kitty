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
                     "block": ["deploy", {"sub": "config", "if_any": ["--set"]}],
                     "allow_owner": {"file": "$XDG_RUNTIME_DIR/svcctl/launched.json",
                                     "option": "--owner", "env": "SVCCTL_OWNER"}}],   起動した担当だけ通す
       "allow_commands": ["git push --dry-run"],       例外として通す形
       "message": "{name} が働いています（{reason}）。後で行ってください。"}}}}

条件は OR。commands / allow_commands の要素は、文字列（語の並びの頭が一致）、{"regex"}（単純なコマンド 1 つの
文字列に re.search）、{"program", "block"}（プログラム名の正規表現とサブコマンドの一覧）のどれか。

通す道（e2e など）:
  - 環境変数 CLAUDE_GUARD_ALLOW=<名前>（カンマ区切り可）か all。hook のプロセスの環境か、Bash のコマンドの前に付けた形
  - 許可のファイル $XDG_RUNTIME_DIR/claude-guard/<名前>.allow（claude-guard allow。期限まで有効）
  - {"program"} の項目の allow_owner: 起動の記録のファイルの owner と、コマンドの --owner X（か前に付けた環境変数）が
    同じで、記録の pid がそのガードの process に当たるプロセスとして動いていて、セッションの役割も X なら、その項目では止めない
  - 共同作業（記録の coop: {"with": 担当かそのリスト, "reason", "since", "until"}）: 期限内で記録の pid が生きていれば、
    with の担当も owner と同じ照合（option / env / same_role）で通す。ユーザーと AI が同じゲームを一緒に動かすため。
    旗・ロックの間は使わない。paths（書き込み・git）は共同作業でも止める
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
# 語がまるごと $T / ${T} のもの。同じコマンドの中で代入した T なら、コマンドの位置に来たとき値を語に分ける
VARREF = re.compile(r"^\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))$")
# その語の目印（値の頭に付ける）。コマンドの位置なら _unwrap が語に分け、引数の位置なら外して値 1 語のままにする
SPLIT = "\x04"
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RUNTIME_PREFIXES = ("$XDG_RUNTIME_DIR/", "${XDG_RUNTIME_DIR}/")
LAUNCH_MAX_BYTES = 64 * 1024
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


def _allow_owner(v, where, problems):
    """{"program"} の項目の allow_owner を読む。誤りがあれば None（例外にしない = 今までどおり止める）。"""
    if not isinstance(v, dict):
        problems.append(f"{where} はオブジェクト。この例外は使わない")
        return None
    f = v.get("file")
    if not isinstance(f, str) or not f.strip():
        problems.append(f"{where}.file（起動の記録のファイル）が無い。この例外は使わない")
        return None
    rest = next((f[len(p):] for p in RUNTIME_PREFIXES if f.startswith(p)), None)
    if rest is not None:
        if "/" not in rest.strip("/") or "$" in rest:
            problems.append(f"{where}.file は $XDG_RUNTIME_DIR/<フォルダ>/<名前> の形（無いときは /tmp/<フォルダ>-<uid>/<名前>）: "
                            f"{f!r}。この例外は使わない")
            return None
    elif "$" in f or not (f.startswith("~") or os.path.isabs(f)):
        problems.append(f"{where}.file は絶対パス・~ で始める・$XDG_RUNTIME_DIR/ で始めるのどれか: {f!r}。この例外は使わない")
        return None
    opt, name = v.get("option"), v.get("env")
    if opt is not None and not (isinstance(opt, str) and re.fullmatch(r"--?[A-Za-z0-9][A-Za-z0-9_-]*", opt)):
        problems.append(f"{where}.option は - で始めるオプションの名前: {opt!r}。この例外は使わない")
        return None
    if name is not None and not (isinstance(name, str) and ENV_NAME.match(name)):
        problems.append(f"{where}.env は環境変数の名前: {name!r}。この例外は使わない")
        return None
    if opt is None and name is None:
        problems.append(f"{where} に option も env も無い（担当の名前をどこから読むか）。この例外は使わない")
        return None
    same = v.get("same_role", True)
    if not isinstance(same, bool):
        problems.append(f"{where}.same_role は true / false: {same!r}。この例外は使わない")
        return None
    return {"file": f, "option": opt, "env": name, "same_role": same}


def _compile_entry(e, where, problems, owner_ok=False):
    """commands / allow_commands の 1 つを ("prefix", [語]) / ("regex", re) / ("program", dict) にする。

    owner_ok は commands の項目（allow_owner を書いてよい所）。"""
    if isinstance(e, str) and e.strip():
        return ("prefix", e.split())
    if isinstance(e, dict) and "allow_owner" in e and not (owner_ok and isinstance(e.get("program"), str)
                                                           and not isinstance(e.get("regex"), str)):
        problems.append(f"{where}.allow_owner は commands の {{\"program\"}} の項目でだけ使える。この例外は使わない")
        e = {k: x for k, x in e.items() if k != "allow_owner"}
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
                block[b] = ((), (), ())
            elif isinstance(b, dict) and isinstance(b.get("sub"), str) and b["sub"]:
                if_any, unless_any = _strings(b.get("if_any", [])), _strings(b.get("unless_any", []))
                but_if_any = _strings(b.get("but_if_any", []))
                if if_any is None or unless_any is None or but_if_any is None:
                    problems.append(f"{where}.block[{i}] の if_any / unless_any / but_if_any は文字列のリスト")
                    continue
                if but_if_any and not unless_any:
                    # but_if_any は unless_any で通すものの例外。unless_any が無いと意味が無いので書き誤りとして知らせる。
                    # 項目は捨てずに残す（止める側に倒す: but_if_any で止めたかったものを素通りさせない）
                    problems.append(f"{where}.block[{i}] の but_if_any は unless_any と一緒に書く")
                block[b["sub"]] = (tuple(if_any), tuple(unless_any), tuple(but_if_any))
            else:
                problems.append(f"{where}.block[{i}] は文字列か {{\"sub\": ...}}")
        owner = _allow_owner(e["allow_owner"], f"{where}.allow_owner", problems) if "allow_owner" in e else None
        if owner and owner["option"]:
            vopts = list(vopts) + [owner["option"]]      # 担当の名前のオプションは値をとる（サブコマンドと取り違えない）
        return ("program", {"program": prog, "value_options": set(vopts), "block": block, "allow_owner": owner})
    problems.append(f"{where} は文字列・{{\"regex\"}}・{{\"program\", \"block\"}} のどれか")
    return None


def _entries(value, where, problems, owner_ok=False):
    if value is None:
        return []
    if not isinstance(value, list):
        problems.append(f"{where} はリスト")
        return []
    out = []
    for i, e in enumerate(value):
        c = _compile_entry(e, f"{where}[{i}]", problems, owner_ok)
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
    g["commands"] = _entries(sec.get("commands"), f"{where}.commands", problems, owner_ok=True)
    if not g["process"] and any(k == "program" and s["allow_owner"] for k, s in g["commands"]):
        problems.append(f"{where}: allow_owner は process（記録の pid のプロセスを確認する正規表現）のあるガードでだけ効く。"
                        "この例外は使わない")
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

# 二重引用符の中とヒアドキュメントの本文から取り出したコマンド置換（$(...) と `...`）の目印。
# 二重引用符の中の置換は語の中に \x01番号\x01 で残し（その語を見るときに中身を読み、元の字に戻す）、
# ヒアドキュメントの本文の置換は、そのコマンドの次の行に \x02番号\x02 の語として置く（読んだあと捨てる）
SUB_IN_WORD = re.compile("\x01(\\d+)\x01")
SUB_LINE = re.compile("\x02(\\d+)\x02")
# ヒアドキュメントの本文そのもの。区切りの語に \x03番号\x03 を付けて残し、受け取るコマンドが
# シェル（bash <<'EOF' など、-c の無いもの）か ssh なら、本文をコマンドとして読む
HEREDOC_BODY = re.compile("\x03(\\d+)\x03")
# 本文を stdin から読んでコマンドとして動かすもの
STDIN_SHELLS = {"ssh"}


class _Scan:
    """bash の引用とヒアドキュメントを読み分ける。shlex に渡す前の文字列と、取り出したコマンド置換を作る。

    - ヒアドキュメントの本文は除く（入力の文字で、コマンドではない）。区切りを引用した <<'EOF' の本文は全部捨て、
      引用しない <<EOF の本文は、bash が実際に動かす $(...) と `...` だけを読む。
    - 二重引用符の中は文字列（echo や git commit -m の値）なのでコマンドとして見ないが、$(...) と `...` は読む。
    - 一重引用符と $'...' の中は何も読まない。注釈（語の頭の #）の中の引用符で読み違えない。
    bash -c の中身は二重引用符の文字列として残り、Analyzer が別に読む。
    """

    def __init__(self, text):
        self.t = text
        self.subs = []          # [(中身, 元の字)]
        self.pending = []       # [(タブを外すか, 区切り, 引用したか)]

    def run(self):
        out = []
        self.shell(0, len(self.t), None, out)
        return "".join(out), self.subs

    def shell(self, i, end, closer, out):
        """引用の外。closer が ")" なら $( の中で、対の ) の次の位置で返る。(位置, 閉じたか)"""
        t = self.t
        depth = 0
        while i < end:
            c = t[i]
            prev = t[i - 1] if i else "\n"
            if c == "\\" and i + 1 < end:
                out.append(t[i:i + 2])
                i += 2
            elif c == "'":
                j = t.find("'", i + 1, end)
                j = end if j < 0 else j + 1
                out.append(t[i:j])
                i = j
            elif c == "$" and t.startswith("$'", i):
                j = self._ansi_end(i + 2, end)
                out.append(t[i:j])
                i = j
            elif t.startswith("$((", i) or (t.startswith("((", i) and prev in " \t\r\n;&|()"):
                j = self._arith_end(i + (3 if c == "$" else 2), end)    # 算術式の << はヒアドキュメントでない
                out.append(t[i:j])
                i = j
            elif c == '"':
                out.append('"')
                i = self.dq(i + 1, end, '"', out)
            elif c == "#" and prev in " \t\r\n;&|()":
                j = t.find("\n", i, end)
                j = end if j < 0 else j
                out.append(t[i:j])
                i = j
            elif t.startswith("<<", i) and not t.startswith("<<<", i):
                i = self.heredoc(i, end, out)
            elif c == "\n":
                out.append(c)
                i += 1
                if self.pending:
                    i = self.bodies(i, end, out)
            else:
                if closer:
                    if c == "(":
                        depth += 1
                    elif c == ")":
                        if depth == 0:
                            return i + 1, True
                        depth -= 1
                out.append(c)
                i += 1
        return i, False

    def _arith_end(self, i, end):
        """$(( / (( の後から、対の )) の次の位置。"""
        t, depth = self.t, 2
        while i < end:
            if t[i] == "(":
                depth += 1
            elif t[i] == ")":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        return end

    def _ansi_end(self, i, end):
        t = self.t
        while i < end:
            if t[i] == "\\":
                i += 2
            elif t[i] == "'":
                return i + 1
            else:
                i += 1
        return end

    def dq(self, i, end, quote, out):
        """二重引用符の中（quote が None ならヒアドキュメントの本文）。閉じた次の位置を返す。"""
        t = self.t
        while i < end:
            c = t[i]
            if c == "\\" and i + 1 < end:
                out.append(t[i:i + 2])
                i += 2
            elif quote and c == quote:
                out.append(c)
                return i + 1
            elif t.startswith("$((", i):
                j = self._arith_end(i + 3, end)
                out.append(t[i:j])
                i = j
            elif t.startswith("$(", i):
                j, closed = self.shell(i + 2, end, ")", [])
                self._sub(t[i + 2:j - 1] if closed else t[i + 2:j], t[i:j], out)
                i = j
            elif c == "`":
                j = i + 1
                while j < end and t[j] != "`":
                    j += 2 if t[j] == "\\" else 1
                j = min(j, end)
                self._sub(t[i + 1:j], t[i:j + 1], out)
                i = j + 1
            else:
                out.append(c)
                i += 1
        return end

    def _sub(self, inner, orig, out):
        out.append(f"\x01{len(self.subs)}\x01")
        self.subs.append((inner, orig))

    def heredoc(self, i, end, out):
        """<<[-] 区切り。区切りを覚え（本文は次の改行の後）、字はそのまま残す（segments が << と区切りを飛ばす）。"""
        t = self.t
        j = i + 2
        strip = j < end and t[j] == "-"
        j += strip
        while j < end and t[j] in " \t":
            j += 1
        delim, quoted = [], False
        while j < end and t[j] not in " \t\r\n;&|()<>":
            c = t[j]
            if c in "'\"":
                k = t.find(c, j + 1, end)
                k = end if k < 0 else k
                delim.append(t[j + 1:k])
                quoted = True
                j = k + 1
            elif c == "\\" and j + 1 < end:
                delim.append(t[j + 1])
                quoted = True
                j += 2
            else:
                delim.append(c)
                j += 1
        out.append(t[i:j])
        if delim:
            out.append(f"\x03{len(self.subs)}\x03")
            self.subs.append(("", ""))        # 本文は bodies で入れる
            self.pending.append((strip, "".join(delim), quoted, len(self.subs) - 1))
        return j

    def bodies(self, i, end, out):
        """改行の後: 待っているヒアドキュメントの本文を順に読み飛ばす。"""
        t = self.t
        pending, self.pending = self.pending, []
        lines = []
        for strip, delim, quoted, slot in pending:
            start = i
            stop = end
            while i < end:
                j = t.find("\n", i, end)
                j = end if j < 0 else j
                line = t[i:j]
                if (line.lstrip("\t") if strip else line) == delim:
                    stop = i
                    i = min(j + 1, end)
                    break
                i = min(j + 1, end)
            else:
                stop = end
            self.subs[slot] = (t[start:stop], "")
            if not quoted:
                found = []
                before = len(self.subs)
                self.dq(start, stop, None, found)
                lines.extend(f"\x02{n}\x02" for n in range(before, len(self.subs)))
        if lines:
            out.append(" ".join(lines) + "\n")
        return i


def scan(text):
    """(shlex に渡す字, コマンド置換の [(中身, 元の字)])。"""
    return _Scan(text).run()


def strip_heredocs(text):
    """ヒアドキュメントの本文を除いた字（前の版の名前。中身は scan）。"""
    return scan(text)[0]


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


def tokenize(text, subs=None):
    """語と演算子に分ける。引用が閉じていないなど shlex で読めないときは、空白と演算子で粗く分ける。

    subs にリストを渡すと、二重引用符の中とヒアドキュメントの本文のコマンド置換を足す（語の中の目印の番号で引く）。"""
    import shlex
    text, found = scan(text)
    if subs is not None:
        subs.extend(found)
    text = prepare(text.replace("\\\n", " "))
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
            if nxt is not None and not is_op(nxt):
                words.extend(m.group(0) for m in HEREDOC_BODY.finditer(nxt))   # ヒアドキュメントの本文の目印
                i += 2
            else:
                i += 1
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


def _split_value(value):
    """コマンドの位置に来た変数の値を語に分ける（シェルの引用の規則。読めなければ空白で）。"""
    import shlex
    try:
        words = shlex.split(value)
    except ValueError:
        words = value.split()
    return [w.replace(SPLIT, "") for w in words]


def _unmark(words):
    return [w[len(SPLIT):] if w.startswith(SPLIT) else w for w in words]


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


def _var_word(word, split):
    """コマンドの位置の $T の値。split なら語に分け、そうでなければ 1 語（"$T" の形。空なら語にならないとみる）。"""
    value = word[len(SPLIT):]
    if split:
        return _split_value(value)
    value = value.replace(SPLIT, "")
    return [value] if value else []


def _unwrap(words, split=True):
    """予約語（then / do / ! など）と env / nohup / timeout などを外し、python3 x.py の形は x.py をプログラムとみる。
    (words, assigns)。for / case の頭は語の並びでコマンドでないので、空にする。

    split はコマンドの位置の $T（目印 SPLIT の付いた語）を語に分けるか。引用の有無（$T か "$T" か）は shlex の後で
    読めないので、呼ぶ側が両方で読む。"""
    assigns = {}
    while words:
        if words[0].startswith(SPLIT):          # T="python3 x.py"; $T quit の $T
            words = _var_word(words[0], split) + words[1:]
            continue
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
                w = words[i][len(SPLIT):] if words[i].startswith(SPLIT) else words[i]   # timeout $D の $D は値で見る
                m = ASSIGN.match(w)
                if name == "env" and m:
                    assigns[m.group(1)] = m.group(2)
                    i += 1
                elif name == "env" and w in ("-u", "--unset") and i + 1 < len(words):
                    assigns[_unmark([words[i + 1]])[0]] = ""     # 消した変数は空とみる（担当の名乗りを消す）
                    i += 2
                elif name == "env" and w.startswith("--unset="):
                    assigns[w.split("=", 1)[1]] = ""
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
                if words[i].startswith(SPLIT):      # python3 $T quit の $T（値に引数が入っていれば分ける）
                    words = words[:i] + _var_word(words[i], split) + words[i + 1:]
                    continue
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


# git clone / init の、値をとるオプション。_positional では読めない -bmaster・-qb master・--branch=master・
# 長いオプションの略（--bra master。git は一意な頭なら受ける）も読み、元と行き先を取り違えないようにする
GIT_CLONE_SHORT = {"b", "o", "c", "u", "j"}
GIT_CLONE_LONG = {"--branch", "--origin", "--config", "--depth", "--reference", "--reference-if-able",
                  "--separate-git-dir", "--template", "--upload-pack", "--jobs", "--filter", "--shallow-since",
                  "--shallow-exclude", "--server-option", "--bundle-uri", "--ref-format", "--revision"}
GIT_INIT_SHORT = {"b"}
GIT_INIT_LONG = {"--template", "--separate-git-dir", "--initial-branch", "--object-format", "--ref-format"}
# 値がパスで、そこにも書く（--template は読む側だが、迷ったら止める側に倒す）
GIT_DIR_OPTS = ("--separate-git-dir", "--template")


def _git_opts(args, short_vals, long_vals):
    """git の 1 つのサブコマンドの引数を (オプションでない引数, [(長い名前か -x, 値)]) にする。
    略した長いオプションは、当たる候補がどれも値をとるなら値をとるとみる（git は曖昧な略を断るので、通るのは一意のときだけ）。"""
    pos, vals = [], []
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            pos += args[i:]
            break
        if a == "-" or not a.startswith("-"):
            pos.append(a)
        elif a.startswith("--"):
            name, eq, value = a.partition("=")
            hits = [name] if name in long_vals else sorted(o for o in long_vals if o.startswith(name) and len(name) > 2)
            if not hits:
                continue                                # 値をとらないもの（--no-template なども）
            if not eq:
                if i >= len(args):
                    continue
                value, i = args[i], i + 1
            vals += [(h, value) for h in hits]          # 略が複数に当たれば全部の意味で見る（止める側）
        else:
            for k, ch in enumerate(a[1:], 1):
                if ch in short_vals:                    # -qb master / -bmaster
                    if k + 1 < len(a):
                        vals.append(("-" + ch, a[k + 1:]))
                    elif i < len(args):
                        vals.append(("-" + ch, args[i]))
                        i += 1
                    break
    return pos, vals


def allow_names(value):
    """CLAUDE_GUARD_ALLOW の値を名前の集合にする（all / 1 は全部）。"""
    if not isinstance(value, str) or not value.strip():
        return set()
    names = {v.strip() for v in value.split(",") if v.strip()}
    return {"all"} if names & {"all", "1"} else names


def _git_split(args):
    """git の全体のオプションを読み、(-C などで変えた場所の [(オプション, 値)], サブコマンドから後) を返す。"""
    dirs = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-C", "--work-tree", "--git-dir") and i + 1 < len(args):
            dirs.append((a, args[i + 1]))
            i += 2
        elif a.startswith("--work-tree=") or a.startswith("--git-dir="):
            dirs.append(tuple(a.split("=", 1)))
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


def has_flag_prefix(word):
    """長いオプションの省略形になりうる語か（"--x" 以上。"--" だけは除く）。"""
    return word.startswith("--") and len(word) > 2


def match_entry(entry, words, abbrev=True):
    """commands / allow_commands の 1 つが、単純なコマンド words に当たれば、その印（文字列）を返す。
    abbrev は if_any / but_if_any を省略形にも当てるか。commands（止める側）は True、
    allow_commands（通す側）は False（前方一致にすると例外として通すものが広がるため）。"""
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
    if_any, unless_any, but_if_any = spec["block"][sub]

    def has(flag):
        return any(r == flag or r.startswith(flag + "=") for r in rest)

    def has_abbrev(flag):
        # 止める側（if_any / but_if_any）は省略形にも当てる: argparse は既定で --eng を --engine-check と読むため。
        # 長さの下限は付けない（止める側に倒す）。"--" だけはオプションの終わりの印なので当てない
        for r in rest:
            key = r.split("=", 1)[0]
            if abbrev and has_flag_prefix(key) and flag.startswith(key):
                return True
        return has(flag)
    if if_any and not any(has_abbrev(f) for f in if_any):
        return None
    # unless_any で通すものでも、but_if_any のどれかがあれば止める（読むだけの形に危ないフラグを足した場合）。
    # unless_any は完全一致のまま（省略形の --dry は通さない＝止める側）
    if unless_any and any(has(f) for f in unless_any) and not any(has_abbrev(f) for f in but_if_any):
        return None
    return f"{prog} {sub}"


def option_value(args, option, value_opts=()):
    """サブコマンドより前の全体のオプションから option の値（--owner X か --owner=X。最後のもの）。無ければ None。
    match_entry がサブコマンドを探すのと同じ読み方（value_opts は値をとるオプション）。"""
    val = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == option:
            val = args[i + 1] if i + 1 < len(args) else None
            i += 2
        elif a.startswith(option + "="):
            val = a.split("=", 1)[1]
            i += 1
        elif a in value_opts:
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            break
    return val


# ---- 起動した担当だけ通す例外（allow_owner） -----------------------------------

def launch_file(raw, env):
    """allow_owner.file を実際のパスにする。$XDG_RUNTIME_DIR/<フォルダ>/… は、XDG_RUNTIME_DIR が無い（か空）なら
    /tmp/<フォルダ>-<uid>/…（tf3ctl など書く側と同じ決まり）。~ は HOME。"""
    for pre in RUNTIME_PREFIXES:
        if raw.startswith(pre):
            rest = raw[len(pre):].lstrip("/")
            base = env.get("XDG_RUNTIME_DIR")
            if base:
                return os.path.join(base, rest)
            folder, _, tail = rest.partition("/")
            return os.path.join("/tmp", f"{folder}-{os.getuid()}", tail)
    if raw == "~" or raw.startswith("~/"):
        return (env.get("HOME") or os.path.expanduser("~")) + raw[1:]
    return raw


def read_launch_record(path, patterns):
    """(記録の dict, None) か、例外にならないとき (読めた分の dict か None, 理由)。

    通すのは: 自分の普通のファイルで、{"v": 1, "owner": 空でない文字列, "pid": 正の整数} の形で、その pid が動いていて、
    引数（pgrep -f と同じく空白でつないだもの）が patterns のどれかに当たるときだけ（pid の使い回し・古い記録を効かせない）。
    owner が無い（か null）記録は、coop（オブジェクト）があるときだけ読む（人が --owner なしで起動して共同作業にした形）。
    そのとき owner は None で、owner としては誰とも合わない。"""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None, "記録のファイルが無い"
    except OSError as e:
        return None, f"記録のファイルを読めない（{e.strerror or e}）"
    try:
        import stat
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            return None, "記録のファイルが自分の普通のファイルでない"
        raw = os.read(fd, LAUNCH_MAX_BYTES + 1)
    except OSError as e:
        return None, f"記録のファイルを読めない（{e.strerror or e}）"
    finally:
        os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8")) if len(raw) <= LAUNCH_MAX_BYTES else None
    except (ValueError, UnicodeDecodeError):
        data = None
    if not isinstance(data, dict) or data.get("v") != 1:
        return None, "記録のファイルの形が違う"
    owner, pid = data.get("owner"), data.get("pid")
    owner_ok = (isinstance(owner, str) and owner) or (owner is None and isinstance(data.get("coop"), dict))
    if not owner_ok or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None, "記録のファイルの形が違う"
    import proc
    argv = proc.argv(pid)
    if argv is None:
        if proc.alive(pid):
            return data, f"pid {pid} の引数を読めない"
        return data, f"pid {pid} が動いていない"
    line = " ".join(argv)
    if not any(re.search(p, line) for p in patterns):
        return data, f"pid {pid} は別のプロセス（引数が process に当たらない）"
    return data, None


def coop_of(rec, now=None):
    """記録の共同作業の印を読む。{"with": [担当], "reason", "since", "until", "changes"} か None。

    None は: coop が無い・形が違う・until を過ぎた（pid の照合は read_launch_record の側）。changes は
    プレイ用のセーブを変えた回数（表示だけに使う。modified / changes が無い・形が違えば None）。"""
    c = rec.get("coop") if isinstance(rec, dict) else None
    if not isinstance(c, dict):
        return None
    w = c.get("with")
    names = [w] if isinstance(w, str) else w
    if not isinstance(names, list) or not names or not all(isinstance(x, str) and x for x in names):
        return None
    until = c.get("until")
    if isinstance(until, bool) or not isinstance(until, (int, float)) or until != until:
        return None
    now = time.time() if now is None else now
    # NaN・Infinity（json は読む）や桁外れの期限は、書き手の誤りで共同作業が終わらなくなるので印とみなさない。
    # 上限は旗と同じ 7 日
    if not now < until <= now + MAX_MINUTES * 60:
        return None
    reason = c.get("reason") if isinstance(c.get("reason"), str) else ""
    since = c.get("since")
    since = since if isinstance(since, (int, float)) and not isinstance(since, bool) and since == since else None
    n = c.get("changes")
    changes = n if c.get("modified") is True and isinstance(n, int) and not isinstance(n, bool) and n > 0 else None
    return {"with": names, "reason": reason, "since": since, "until": until, "changes": changes}


def jst_hhmm(t):
    """epoch 秒 → JST の HH:MM（統括・ユーザーは JST で読む。マシンの時間帯に左右されないよう固定）。"""
    from datetime import datetime, timedelta, timezone
    return datetime.fromtimestamp(t, timezone(timedelta(hours=9))).strftime("%H:%M")


def coop_text(coop):
    """status の「共同作業: <担当>、HH:MM（JST）まで（理由）」。"""
    text = f"共同作業: {', '.join(coop['with'])}、{jst_hhmm(coop['until'])}（JST）まで"
    if coop["reason"]:
        text += f"（{coop['reason']}）"
    if coop["changes"]:
        text += f"、プレイ用のセーブを {coop['changes']} 回変えた"
    return text


def window_role(env):
    """このペイン（KITTY_WINDOW_ID と KITTY_LISTEN_ON）の役割。claude-state の記録から読む（kitty には聞かない）。
    claude-show のようにセッション id を持たないコマンド用。分からなければ None。"""
    wid, sock = env.get("KITTY_WINDOW_ID"), env.get("KITTY_LISTEN_ON")
    if not wid or not wid.isdigit() or not sock:     # ペインの id は kitty ごとなので、socket が無ければ決められない
        return None
    try:
        from rundir import runtime_dir
        files = sorted((runtime_dir(env, create=False) / "claude-state").glob("*.json"))
    except (OSError, ImportError):
        return None
    best = None
    for f in files:
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("window") != int(wid) or data.get("socket") != sock:
            continue
        role = data.get("role")
        if isinstance(role, str) and role and (best is None or (data.get("updated") or 0) > best[0]):
            best = (data.get("updated") or 0, role)
    return best[1] if best else None


def session_role(env, session):
    """セッションの役割（kitty のペインの user var role）。claude-state の記録（ファイル 1 つ）から読む。
    kitty には聞かない（hook を遅くしない）。分からなければ None。"""
    if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session):
        return None
    try:
        from rundir import runtime_dir
        data = json.loads((runtime_dir(env, create=False) / "claude-state" / f"{session}.json").read_text())
    except (OSError, ValueError, ImportError):
        return None
    role = data.get("role") if isinstance(data, dict) else None
    return role if isinstance(role, str) and role else None


def known_roles(env):
    """役割の名前の集合: 名簿（claude-roster の roster.json の sessions）と、claude-state の記録の role。
    読めないものは飛ばす（無ければ空。空なら誰も役割とみなさない）。"""
    roles = set()
    if env.get("CLAUDE_ROSTER_DIR"):
        roster = Path(env["CLAUDE_ROSTER_DIR"]) / "roster.json"
    else:
        home = Path(env["HOME"]) if env.get("HOME") else Path.home()
        base = Path(env["XDG_DATA_HOME"]) if env.get("XDG_DATA_HOME") else home / ".local/share"
        roster = base / "claude-kitty" / "roster.json"
    try:
        sessions = json.loads(roster.read_text()).get("sessions")
        if isinstance(sessions, dict):
            roles.update(k for k in sessions if isinstance(k, str) and k)
    except (OSError, ValueError, AttributeError):
        pass
    try:
        from rundir import runtime_dir
        files = sorted((runtime_dir(env, create=False) / "claude-state").glob("*.json"))
    except (OSError, ImportError):
        files = []
    for f in files:
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        role = data.get("role") if isinstance(data, dict) else None
        if isinstance(role, str) and role:
            roles.add(role)
    return roles


def playtest_state(env, name, running=None, path=None, role=None, now=None):
    """claude-show 用: ガード name のプロセス（ゲームなど）を、人がテストプレイしているか。(種類, 説明) を返す。

    種類は "config"（ガードが無い・無効・process が無い → 今までどおりだが、設定の誤りかもしれないので知らせる）、
    None（プロセスが動いていない → 今までどおり）、
    "coop"（記録が生きていて共同作業の印が効いていて、呼んだペインの役割 role が with にある = ユーザーと一緒に
    操作中。見せるのが目的なので開いてよい。ゲームの窓が前面かは呼び手が見る）、
    "e2e"（記録が生きていて共同作業の印が無く（期限切れも含む）、owner が役割 = 担当の e2e）、
    "human"（それ以外 = 人がプレイ中。印が効いていて role が with に無ければ、owner が役割でもこれ）。
    記録・pid の照合は allow_owner と同じ read_launch_record。running は patterns → True / False / None（pgrep）。"""
    guards, _ = load_guard_config(env, path)
    g = next((x for x in guards if x["name"] == name), None)
    if g is None:
        return "config", f"view.playtest_guard のガード {name} が guard_hook.guards に無い（か設定の誤りで外された）"
    if not g["enabled"] or not g["process"]:
        return "config", f"view.playtest_guard のガード {name} が無効か、process が無い"
    if not (running or process_running)(g["process"]):
        return None, f"ガード {name} のプロセスが動いていない"
    files = []
    for kind, spec in g["commands"]:
        o = spec.get("allow_owner") if kind == "program" else None
        if o and launch_file(o["file"], env) not in files:
            files.append(launch_file(o["file"], env))
    if not files:
        return "human", "起動の記録（allow_owner の file）が設定に無い"
    roles, whys, e2e, coop_seen = None, [], None, False
    for f in files:
        rec, why = read_launch_record(f, g["process"])
        if why:
            whys.append(why)
            continue
        # 共同作業の印を owner より先に見る。launch --coop は owner が担当でも人がゲームを操作しているので、
        # owner の判定（e2e）に進むと誰が呼んでも開いてしまう。印が効いている間は with の担当だけが開ける
        # （どれかの記録に印があれば、ほかの記録の owner でも e2e にしない）
        coop = coop_of(rec, now)
        if coop:
            if role and role in coop["with"]:
                return "coop", coop_text(coop)
            coop_seen = True
            whys.append(f"共同作業の担当（{', '.join(coop['with'])}）は呼んだペインの役割（{role or '不明'}）でない")
            continue
        roles = known_roles(env) if roles is None else roles
        if rec.get("owner") in roles:
            e2e = e2e or f"{rec['owner']} が起動した（pid {rec['pid']}）"
            continue
        whys.append(f"記録の owner {rec['owner']} は役割でない" if rec.get("owner") else "記録に owner が無い")
    if e2e and not coop_seen:
        return "e2e", e2e
    return "human", "、".join(whys)


class Context:
    """1 回の判定で共有するもの（hook の環境・セッション）と、記録・役割の読みの控え（同じコマンドの中で 1 度だけ読む）。"""

    def __init__(self, env, session=None, owner_ok=True, now=None):
        self.env = env
        self.session = session
        self.now = now
        self.owner_ok = owner_ok                  # allow_owner を使うか（旗・ロックで働いている間は使わない）
        self._launch = {}
        self._role = None
        self._role_read = False

    def launch(self, guard, owner_spec):
        path = launch_file(owner_spec["file"], self.env)
        key = (guard["name"], path)
        if key not in self._launch:
            self._launch[key] = (read_launch_record(path, guard["process"]) if guard["process"]
                                 else (None, "ガードに process が無い"))
        return self._launch[key]

    def role(self):
        if not self._role_read:
            self._role, self._role_read = session_role(self.env, self.session), True
        return self._role


def claimed_owner(owner_spec, words, assigns, vars_, exported, value_opts):
    """コマンドが名乗る担当。option（--owner X / --owner=X）が先、無ければ env（前に付けた X=…、export したもの・
    hook の環境にあるもの）。名乗らなければ None。"""
    if owner_spec["option"]:
        v = option_value(words[1:], owner_spec["option"], value_opts)
        if v is not None:
            return v or None
    name = owner_spec["env"]
    if name:
        if name in assigns:
            return assigns[name] or None
        if name in exported:
            return vars_.get(name) or None
    return None


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

    def __init__(self, guard, cwd, env, depth=0, ctx=None):
        self.guard = guard
        self.ctx = ctx or Context(env)
        self.roots = Roots(guard["paths"])
        self.cwd = cwd or os.getcwd()
        self.home = env.get("HOME") or os.path.expanduser("~")
        self.vars = {k: v for k, v in env.items() if isinstance(v, str)}
        self.vars.setdefault("HOME", self.home)
        self.exported = set(self.vars)            # 子のプロセスに渡る変数（hook の環境にあるもの + export したもの）
        self.local = set()                        # このコマンドの中で代入した変数（コマンドの位置の $T を語に分ける）
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

    def child(self):
        """bash -c の中身・コマンド置換・ヒアドキュメントの本文を読む Analyzer（変数と cwd を引き継ぐ）。"""
        sub = Analyzer(self.guard, self.cwd, {}, self.depth + 1, self.ctx)
        sub.vars, sub.home, sub.allowed = dict(self.vars), self.home, set(self.allowed)
        sub.exported, sub.local = set(self.exported), set(self.local)
        return sub

    def expand_word(self, w):
        m = VARREF.match(w)
        if m and (m.group(1) or m.group(2)) in self.local:
            return SPLIT + self.vars.get(m.group(1) or m.group(2), "")
        return _expand(w, self.vars)

    def lets_through(self, names):
        return bool(names & {"all", self.guard["name"]})

    def run(self, command):
        stack = []
        self.subs = []
        for item in segments(tokenize(command, self.subs)):
            if item[0] == "push":
                stack.append((self.cwd, dict(self.vars), set(self.allowed), set(self.exported), set(self.local)))
            elif item[0] == "pop":
                if stack:
                    self.cwd, self.vars, self.allowed, self.exported, self.local = stack.pop()
            else:
                self.segment(item[1], item[2])
        return self.reasons

    def substitutions(self, words):
        """語の中のコマンド置換（二重引用符の中・ヒアドキュメントの本文から取り出したもの）を、いまの cwd で読む。

        置換は、そのコマンドより先に、同じシェルで動く（中の cd は外に持ち出さない）。読んだあと、語は元の字に戻し、
        ヒアドキュメントの目印だけの語は捨てる。"""
        out = []
        for w in words:
            for m in list(SUB_IN_WORD.finditer(w)) + list(SUB_LINE.finditer(w)):
                n = int(m.group(1))
                if n < len(self.subs) and self.depth < 4:
                    sub = self.child()
                    for r in sub.run(self.subs[n][0]):
                        if r not in self.reasons:
                            self.reasons.append(r)
            if SUB_LINE.fullmatch(w) or (SUB_LINE.search(w) and not SUB_LINE.sub("", w).strip()):
                continue
            out.append(SUB_IN_WORD.sub(lambda m: self.subs[int(m.group(1))][1] if int(m.group(1)) < len(self.subs)
                                       else m.group(0), w))
        return out

    def segment(self, words, redirs):
        bodies = [int(m.group(1)) for w in words for m in HEREDOC_BODY.finditer(w)]
        if bodies:
            words = [w for w in words if not HEREDOC_BODY.fullmatch(w)]
        if any("\x01" in w or "\x02" in w for w in words + redirs):
            words, redirs = self.substitutions(words), self.substitutions(redirs)
            if not words and not redirs:
                return
        words = [self.expand_word(w) for w in words]
        assigns, rest = _split_assignments(words)
        if not rest:
            self.vars.update(assigns)          # X=val だけの行は、後のコマンドの変数になる
            self.local |= set(assigns)
            self.allowed |= allow_names(assigns.get(ALLOW_ENV))
            return
        raw = rest
        rest, more = _unwrap(raw)
        rest = _unmark(rest)                   # 引数の位置の $T は値 1 語のまま（今までどおり）
        assigns.update(more)
        # コマンドの位置の $T は、"$T"（分けない）でも bash は動く。引用の有無は読めないので、分けない形でも読み、
        # どちらかが止める形なら止める（S="/opt/my tools/svcctl"; "$S" deploy を見逃さない）
        alt, alt_more = _unwrap(raw, split=False)
        alt = _unmark(alt)
        self._paths, self._cmds = [], []
        for r in redirs:
            if self.protected(r):
                self.add(f"保護したパスへのリダイレクト（> {r}）")
        cmds = []
        if alt != rest and alt:
            saved = (self.cwd, dict(self.vars), set(self.allowed), set(self.exported), set(self.local))
            self.command(alt, dict(assigns, **alt_more))
            self.stdin_bodies(alt, bodies)     # "$S" <<'EOF'（S が空白入りの bash）の本文も読む
            self.cwd, self.vars, self.allowed, self.exported, self.local = saved   # 状態（cd・export）は分けた形のだけ残す
            cmds = self.allowed_cmds(alt)
        self._cmds = []
        if rest:
            self.command(rest, assigns)
            self.stdin_bodies(rest, bodies)
        cmds += [c for c in self.allowed_cmds(rest) if c not in cmds]
        if self.lets_through(self.allowed | allow_names(assigns.get(ALLOW_ENV))):
            return
        for r in self._paths + cmds:
            if r not in self.reasons:
                self.reasons.append(r)

    def allowed_cmds(self, rest):
        """いまの self._cmds から、allow_commands に当たる形の理由を外したもの。

        allow_commands はコマンドの形の理由だけを消す（git push --dry-run > <保護したパス> は止める）。"""
        if self._cmds and rest and any(match_entry(e, rest, abbrev=False) for e in self.guard["allow_commands"]):
            return []
        return list(self._cmds)

    def stdin_bodies(self, words, bodies):
        """bash <<'EOF' … や ssh host <<'EOF' … は、本文をコマンドとして動かす。本文を bash -c の中身と同じに読む。"""
        if not bodies or self.depth >= 3:
            return
        name = os.path.basename(words[0])
        if not ((name in SHELLS and _shell_c(words[1:]) is None) or name in STDIN_SHELLS):
            return
        for n in bodies:
            if n < len(self.subs) and self.subs[n][0].strip():
                sub = self.child()
                for r in sub.run(self.subs[n][0]):
                    self.add(r)

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
                    self.local.add(m.group(1))
                    self.exported.add(m.group(1))
                    if m.group(1) == ALLOW_ENV:
                        self.allowed |= allow_names(m.group(2))
            return
        if name == "unset":
            for a in args:
                if ENV_NAME.match(a):
                    self.vars.pop(a, None)
                    self.exported.discard(a)
                    self.local.discard(a)
        if name in SHELLS:
            inner = _shell_c(args)
            if inner is not None and self.depth < 3:
                sub = self.child()
                for r in sub.run(inner):
                    self.add(r)
                return
        for e in self.guard["commands"]:
            hit = match_entry(e, words)
            if hit and hit not in self._cmds and not self.owner_passes(e, words, assigns):
                self._cmds.append(hit)
        if self.roots.forms:
            self.writes(name, args, assigns)

    def owner_passes(self, entry, words, assigns):
        """allow_owner の例外に当たるか（起動した担当が、自分で起動したものに対して使うとき）。"""
        kind, spec = entry
        o = spec.get("allow_owner") if kind == "program" else None
        if not o or not self.ctx.owner_ok:
            return False
        claimed = claimed_owner(o, words, assigns, self.vars, self.exported, spec["value_options"])
        if not claimed:
            return False
        rec, why = self.ctx.launch(self.guard, o)
        if why or not rec:
            return False
        if rec.get("owner") != claimed:
            # 共同作業の印の担当も、owner と同じ照合で通す（旗・ロックの間は上の owner_ok で使わない）
            coop = coop_of(rec, self.ctx.now)
            if not coop or claimed not in coop["with"]:
                return False
        if o["same_role"]:
            return self.ctx.role() == claimed     # 役割が分からないセッションは通さない（今までどおり止める）
        return True

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
                return                          # 一覧・確認・標準出力へは書き込まない
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
        base = self.cwd                         # -C だけで決まる場所（clone / init の相対の行き先はここから）
        for opt, d in dirs:
            where = self.path(d, where)
            if opt == "-C":
                base = self.path(d, base)
        # clone / init は、行き先と別に、作業の木と .git を明示した場所へも書く。-C は場所を変えるだけなので数えない。
        # 相対のときの基準は -C の前か後か迷うので、両方で見る（止める側）
        places = [d for opt, d in dirs if opt != "-C"] + [assigns[e] for e in ("GIT_WORK_TREE", "GIT_DIR") if assigns.get(e)]
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
        if sub in ("clone", "init"):
            sv, lv = (GIT_CLONE_SHORT, GIT_CLONE_LONG) if sub == "clone" else (GIT_INIT_SHORT, GIT_INIT_LONG)
            pos, vals = _git_opts(rest, sv, lv)
            for opt, value in vals:
                if opt in GIT_DIR_OPTS and self.roots.covers(self.path(value, base)):
                    self.add(f"git {sub} {opt} {value}")
                    return
            for d in places:
                if self.roots.covers(self.path(d, base)) or self.roots.covers(self.path(d, self.cwd)):
                    self.add(f"git {sub}（{d}）")
                    return
            # clone の行き先は 2 つ目（無ければ cwd の下に作る）、init は 1 つ目（無ければ cwd）。
            # 引数が多いのは知らない値をとるオプションの値が混ざったときなので、元より後ろを全部見る（止める側）
            targets = (pos[1:] or ["."]) if sub == "clone" else (pos or ["."])
            for target in targets:
                if self.roots.covers(self.path(target, base)):
                    self.add(f"git clone の行き先 {target}" if sub == "clone" else f"git init {target}")
                    return
            return
        if sub == "worktree":
            # list 以外（prune / lock / unlock も）は cwd の .git に書くので、cwd が保護した所なら止める。
            # add などは行き先も見る。-b / -B / --orphan / --reason の値もパスとして見る（取り違えても止める側に倒れるだけ）。
            # repair も行き先の .git を書く
            if pos and pos[0] != "list" and (self.roots.covers(where) or (
                    pos[0] in ("add", "move", "remove", "repair")
                    and any(self.roots.covers(self.path(p, where)) for p in pos[1:]))):
                self.add(f"git worktree {pos[0]}")
            return
        if sub in ("archive", "format-patch"):
            # 出力先は -o <dir>・-o<dir>・--output(-directory)・略した --out。-Sfoo の o のような、まとまりの中の o は
            # 別のオプションの値のことが多いので出力先とはみない。ただ -ko<dir> のように本当に -o のこともあるので、
            # その読み方の値も候補に足し、どれかが保護した所なら止める（止める側）
            outs = [v for _o, v in _git_opts(rest, set(), {"--output", "--output-directory"})[1]]
            for i, a in enumerate(rest):
                if a == "--":
                    break
                if a == "-o" and i + 1 < len(rest):
                    outs.append(rest[i + 1])
                elif a.startswith("-o") and len(a) > 2:
                    outs.append(a[2:])
            loose = [v for _o, v in _git_opts(rest, {"o"}, set())[1]]
            if not outs and not loose and sub == "archive":
                return                          # 標準出力へ
            outs = (outs or ["."]) + loose      # format-patch は既定で cwd に書く
            for out in outs:
                if self.roots.covers(self.path(out, where)):
                    self.add(f"git {sub} の出力 {out}")
                    return
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

def find_reasons(guard, tool, tool_input, cwd, env, session=None, owner_ok=True, now=None):
    """1 つのガードについて、止める候補の理由のリスト（働いているかは見ない）。owner_ok は allow_owner を使うか。"""
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
        return Analyzer(guard, cwd, env, ctx=Context(env, session, owner_ok, now)).run(cmd)
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
        flag = read_state(env, g["name"], "on", now)
        if session and flag and flag.get("owner") == session:
            continue                      # ロックの持ち主（デプロイしている本人）は、このガードでは止めない
        # 旗・ロックは「全員を止める」。allow_owner（起動した担当の例外）は process だけで働いているときに使う
        reasons = find_reasons(g, tool, tool_input, cwd, env, session, owner_ok=not flag, now=now)
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

def owner_status(guard, env, now=None):
    """allow_owner の例外ごとに（記録のファイルごとに 1 つ）、記録の owner と pid が動いているか。"""
    out, seen = [], set()
    flagged = bool(read_state(env, guard["name"], "on", now))
    ctx = Context(env)
    for kind, spec in guard["commands"]:
        o = spec.get("allow_owner") if kind == "program" else None
        if not o:
            continue
        path = launch_file(o["file"], env)
        if path in seen:
            continue
        seen.add(path)
        rec, why = ctx.launch(guard, o)
        head = f"起動した担当の例外（{path}）: "
        if flagged:
            out.append(head + "旗（ロック）の間は使わない（全員を止める）")
        elif rec and not why:
            if rec.get("owner"):
                out.append(head + f"owner {rec['owner']}、pid {rec['pid']} が動いている → {rec['owner']} は通す"
                           + ("（役割も同じとき）" if o["same_role"] else ""))
            else:
                out.append(head + f"owner なし、pid {rec['pid']} が動いている")
        elif rec:
            out.append(head + f"owner {rec.get('owner') or 'なし'}、{why} → 例外にならない")
        else:
            out.append(head + f"{why} → 例外にならない")
        coop = coop_of(rec, now) if rec and not why else None
        if coop:
            out.append(coop_text(coop) + ("（旗（ロック）の間は使わない）" if flagged else ""))
    return out


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
        parts.extend(owner_status(g, env, now))
        lines.append("、".join(parts))
    for p in problems:
        lines.append(f"問題: {p}")
    return lines, bool(problems)
