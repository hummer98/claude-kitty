"""claude のプロセスの子の背景のシェルのうち、長く続いているもの（claude-roster status と doctor が 1 行ずつ出す）。

Claude Code は Bash のコマンドを `bash -c 'source <snapshot> … && eval '<コマンド>' < /dev/null && pwd -P >| …'`
として claude の直下の子で動かす。終わらない待ちのループ（until ! pgrep -f … など）が背景に残ると、
~/.claude/sessions/<pid>.json の status が "shell" のままになり、claude-roster restart が断る。
前面の Bash は 10 分で打ち切られるので、2 時間を超えて続く子のシェルは背景のもの。止めるかは人か担当が決める
（ここは見せるだけで止めない）。
"""
import os
import re

LONG_SECONDS = 2 * 60 * 60
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
# Claude Code の包み: … && eval '<コマンド>' < /dev/null && pwd -P >| …（中の ' は '"'"' になる）
EVAL = re.compile(r"\beval '((?:[^']|'\"'\"')*)'")


class Sys:
    """プロセスの情報。テストでは差し替える。"""

    @staticmethod
    def pids():
        import proc
        return proc.pids()

    @staticmethod
    def ppid(pid):
        import proc
        return proc.ppid(pid)

    @staticmethod
    def argv(pid):
        import proc
        return proc.argv(pid)

    @staticmethod
    def age(pid):
        import proc
        return proc.age(pid)


def script_of(argv):
    """シェルの argv から、動かしているコマンドの字（Claude Code の包みは外す）。シェルの -c でなければ None。"""
    if not argv or os.path.basename(argv[0]).lstrip("-") not in SHELLS:
        return None
    script = None
    for i, a in enumerate(argv[1:], 1):
        if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", a):
            script = argv[i + 1] if i + 1 < len(argv) else ""
            break
    if script is None:
        return None
    m = EVAL.search(script)
    return m.group(1).replace("'\"'\"'", "'") if m else script


def head(text, n=80):
    line = " ".join(str(text).split())
    return line if len(line) <= n else line[:n - 1] + "…"


def child_map(sys_=Sys):
    """{親の pid: [子の pid]}。セッションがいくつあってもプロセスの一覧は 1 回だけ読む。"""
    kids = {}
    for p in sys_.pids():
        q = sys_.ppid(p)
        if isinstance(q, int) and q != p:
            kids.setdefault(q, []).append(p)
    return kids


def long_shells(claude_pid, min_seconds=LONG_SECONDS, sys_=Sys, kids=None):
    """[(pid, 経過の秒, コマンドの先頭)]。古い順。claude_pid の直下の子のシェル（-c）で min_seconds 以上のもの。

    kids は child_map() の結果（いくつものセッションを調べるときに渡す）。"""
    if not isinstance(claude_pid, int) or claude_pid <= 0:
        return []
    kids = child_map(sys_) if kids is None else kids
    out = []
    for pid in kids.get(claude_pid, []):
        script = script_of(sys_.argv(pid))
        if script is None:
            continue
        age = sys_.age(pid)
        if age is None or age < min_seconds:
            continue
        out.append((pid, age, head(script)))
    return sorted(out, key=lambda t: -t[1])


def age_text(sec):
    sec = int(sec)
    if sec >= 86400:
        return f"{sec // 86400}日{sec % 86400 // 3600}時間"
    return f"{sec // 3600}時間{sec % 3600 // 60}分"


def brief(pid, age, cmd):
    """pid・経過時間・コマンドの先頭。"""
    return f"pid {pid}（{age_text(age)}）: {cmd}"


def line(pid, age, cmd):
    """1 行（claude-roster status の役割の後ろ）。"""
    return f"背景のシェル {brief(pid, age, cmd)}"
