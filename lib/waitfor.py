"""claude-waitfor の中身。プロセスが無くなるまで・ファイルに文字列が現れるまで、上限時間付きで待つ。

Claude Code は Bash のコマンドを `bash -c '<コマンド>'` で動かす。その中で
`until ! pgrep -f "luajit tests/run.lua"; do sleep 2; done` と待つと、pgrep が待つシェル自身のコマンド行に
一致して永遠に終わらない（2026-11 に 15〜19 時間残った）。ここでは、自分・自分の祖先（呼び出したシェル）と、
祖先から fork しただけの子（パイプやサブシェルで、祖先と同じコマンド行のまま動いているもの）を数えない。

ほかの claude-waitfor と、それを呼んだシェルも数えない（同じものを待つ 2 つが、互いに一致して待ち合わないように）。

一致の決まりは pgrep -f と同じ: 引数を空白でつないだ完全なコマンド行に、パターンを正規表現として re.search
（lib/guardhook.py の allow_owner と同じ。Python の re で、ERE と細部は違いうる）。

テストでは processes・ppid・argv・clock・sleep を差し替える。
"""
import os
import re
import time

DEFAULT_TIMEOUT = 30 * 60
MAX_TIMEOUT = 24 * 60 * 60
DEFAULT_INTERVAL = 2.0
EXIT_DONE = 0
EXIT_TIMEOUT = 124       # timeout(1) と同じ番号（時間切れを区別できるように）
EXIT_USAGE = 2
_UNITS = {"s": 1, "m": 60, "h": 3600}


def parse_duration(text):
    """'30m'・'90s'・'2h'・'45'（秒）を秒にする。0 以下・上限（24 時間）より長い・読めないものは ValueError。

    0（無制限）は受けない: 待ちが残り続けるのを防ぐための道具なので、必ず終わるようにする。"""
    m = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([smh]?)\s*", str(text))
    if not m:
        raise ValueError(f"時間の形が読めない: {text!r}（例: 30m・90s・2h）")
    sec = float(m.group(1)) * _UNITS.get(m.group(2) or "s")
    if sec <= 0:
        raise ValueError("時間は 0 より大きく（無制限の待ちは受けない）")
    if sec > MAX_TIMEOUT:
        raise ValueError("時間は 24 時間まで")
    return sec


def duration_ja(sec):
    sec = int(round(sec))
    if sec < 60:
        return f"{sec}秒"
    if sec < 3600:
        return f"{sec // 60}分" + (f"{sec % 60}秒" if sec % 60 else "")
    return f"{sec // 3600}時間" + (f"{sec % 3600 // 60}分" if sec % 3600 // 60 else "")


class Sys:
    """プロセスの情報と時計。テストでは差し替える。"""

    @staticmethod
    def processes():
        import proc
        return proc.processes()

    @staticmethod
    def ppid(pid):
        import proc
        return proc.ppid(pid)

    @staticmethod
    def argv(pid):
        import proc
        return proc.argv(pid)

    clock = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)


def ancestors(pid, ppid_fn, limit=256):
    """pid から上の祖先の pid の集合（pid 自身を含む。1 と 0 は含めない）。"""
    out = set()
    p = pid
    while isinstance(p, int) and p > 1 and p not in out and len(out) < limit:
        out.add(p)
        p = ppid_fn(p)
    return out


def _forked_from_ancestor(pid, argv, mine, ppid_fn, argv_fn):
    """pid が祖先から fork しただけのもの（親をたどると、同じコマンド行のまま祖先に着く）か。"""
    seen = set()
    p = ppid_fn(pid)
    while isinstance(p, int) and p > 1 and p not in seen and len(seen) < 64:
        seen.add(p)
        if argv_fn(p) != argv:
            return False
        if p in mine:
            return True
        p = ppid_fn(p)
    return False


SHELLISH = {"bash", "sh", "zsh", "dash", "ksh", "timeout", "env", "nohup", "nice", "setsid", "stdbuf"}


def _is_waiter(argv):
    return any(os.path.basename(a) == "claude-waitfor" for a in argv[:2])


def _waiter_shells(waiter, ppid_fn, argv_fn):
    """ほかの claude-waitfor と、それを呼んだシェルの並び（シェル・timeout などが続く間だけ上る）。"""
    out = {waiter}
    p = ppid_fn(waiter)
    while isinstance(p, int) and p > 1 and p not in out and len(out) < 64:
        a = argv_fn(p) or []
        if not a or os.path.basename(a[0]).lstrip("-") not in SHELLISH:
            break
        out.add(p)
        p = ppid_fn(p)
    return out


def matching(regex, me, sys_=Sys):
    """パターンに一致する、自分と祖先（とそこから fork しただけのもの）以外のプロセスの [(pid, コマンド行)]。

    ほかの claude-waitfor と、それを呼んだシェルも数えない（2 つのセッションが同じものを待つと、互いの待ちの
    コマンド行に一致して、どちらも時間切れまで待ってしまうため）。"""
    procs = sys_.processes()
    mine = ancestors(me, sys_.ppid)
    for pid, argv in procs:
        if pid not in mine and argv and _is_waiter(argv):
            mine |= _waiter_shells(pid, sys_.ppid, sys_.argv)
    out = []
    for pid, argv in procs:
        if pid in mine or not argv:
            continue
        line = " ".join(argv)
        if not regex.search(line):
            continue
        if _forked_from_ancestor(pid, argv, mine, sys_.ppid, sys_.argv):
            continue
        out.append((pid, line))
    return out


def wait_proc(pattern, timeout=DEFAULT_TIMEOUT, interval=DEFAULT_INTERVAL, me=None, sys_=Sys, out=None):
    """(終了コード, 1 行の文)。パターンが正規表現として読めなければ ValueError。"""
    try:
        regex = re.compile(pattern)
    except re.error as e:
        raise ValueError(f"パターンが正規表現として読めない: {e}") from None
    me = os.getpid() if me is None else me
    start = sys_.clock()
    first = True
    while True:
        found = matching(regex, me, sys_)
        if not found:
            return EXIT_DONE, ("一致するプロセスは無い" if first
                               else f"一致するプロセスが無くなった（{duration_ja(sys_.clock() - start)}）")
        first = False
        left = timeout - (sys_.clock() - start)
        if left <= 0:
            pid, line = found[0]
            more = f" ほか {len(found) - 1} 件" if len(found) > 1 else ""
            return EXIT_TIMEOUT, (f"時間切れ（{duration_ja(timeout)}）: まだ動いている pid {pid}{more}: "
                                  f"{_head(line)}")
        sys_.sleep(min(interval, left))


def _head(line, n=100):
    line = " ".join(str(line).split())
    return line if len(line) <= n else line[:n - 1] + "…"


class _Reader:
    """ファイルを前回の続きから読み、文字列が現れたかを調べる（大きなログを毎回頭から読まない）。

    境目で切れた文字列も見つけるよう、前の読みの末尾（文字列の長さ - 1 バイト）を残す。ファイルが縮んだ・
    置き換わった（inode が変わった）ときは頭から読み直す。"""

    CHUNK = 1 << 20

    def __init__(self, path, needle):
        self.path = path
        self.needle = needle.encode("utf-8")
        self.pos = 0
        self.tail = b""
        self.ino = None

    def found(self):
        try:
            with open(self.path, "rb") as f:
                st = os.fstat(f.fileno())
                if st.st_ino != self.ino or st.st_size < self.pos:
                    self.ino, self.pos, self.tail = st.st_ino, 0, b""
                f.seek(self.pos)
                while True:
                    chunk = f.read(self.CHUNK)
                    if not chunk:
                        return False
                    buf = self.tail + chunk
                    if self.needle in buf:
                        return True
                    self.pos += len(chunk)
                    keep = len(self.needle) - 1
                    self.tail = buf[-keep:] if keep else b""
        except (FileNotFoundError, NotADirectoryError):
            return False
        except IsADirectoryError:
            raise ValueError(f"フォルダで、ファイルでない: {self.path}") from None
        except OSError:
            return False   # 読めない間（権限が一時的に違うなど）は「まだ」と数え、時間切れで終わる


def wait_file(path, text, timeout=DEFAULT_TIMEOUT, interval=DEFAULT_INTERVAL, sys_=Sys):
    """(終了コード, 1 行の文)。ファイルがまだ無くてもよい（できるまで待つ）。"""
    if not text:
        raise ValueError("待つ文字列が空")
    r = _Reader(path, text)
    start = sys_.clock()
    first = True
    while True:
        if r.found():
            return EXIT_DONE, ("もう現れている" if first else f"現れた（{duration_ja(sys_.clock() - start)}）")
        first = False
        left = timeout - (sys_.clock() - start)
        if left <= 0:
            state = "ファイルがまだ無い" if not os.path.exists(path) else "文字列がまだ現れない"
            return EXIT_TIMEOUT, f"時間切れ（{duration_ja(timeout)}）: {state}: {path}"
        sys_.sleep(min(interval, left))
