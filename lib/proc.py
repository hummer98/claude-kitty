"""プロセスの情報（生きているか・引数・起動時の環境・親・起動時刻・一覧）を OS を問わずに読む。

Linux は /proc、macOS は libSystem の sysctl(KERN_PROCARGS2) と proc_pidinfo(PROC_PIDTBSDINFO) を ctypes で呼ぶ
（外部のライブラリは使わない）。macOS で ctypes が使えないときは ps に倒す。読めないときは None（例外にしない）。

- alive(pid): signal 0 で調べる（OS を問わない。権限が無いのは生きている）。
- start_time(pid): 起動時刻の文字列。同じ OS・同じ方法で読んだもの同士でだけ比べる（pid の再利用を見分ける）。
  Linux は /proc/<pid>/stat の 22 番目（Claude Code の ~/.claude/sessions/<pid>.json の procStart と同じ値）、
  macOS は「秒.マイクロ秒」（ps に倒したときは ps -o lstart= の文字列）。
- proc_start_matches(recorded, start): procStart と start_time を比べてよいのは Linux だけ（macOS で Claude Code が
  何を書くかは分からないので、比べずに「同じ」とみなす。待つ側・止めない側に倒れる）。

テストでは PLATFORM・PROC・run（ps を呼ぶ関数）・_darwin（ctypes の層）を差し替える。
"""
import os
import struct
import subprocess
import sys

PLATFORM = sys.platform  # "linux" / "darwin" / ほか
PROC = "/proc"


def is_linux():
    return PLATFORM.startswith("linux")


def is_darwin():
    return PLATFORM == "darwin"


def alive(pid):
    """pid が生きているか。/proc の無い macOS でも効くよう signal 0 で調べる（権限が無いのは生きている）。

    Linux ではゾンビ（終わったが親がまだ片付けていない）は生きていないとみなす。init の無いコンテナ
    （CI の python:3.9-slim など）では、親の無いプロセスが止まってもゾンビのまま残り、signal 0 が通るため。"""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    if is_linux() and _linux_zombie(pid):
        return False
    return True


# ---- ps（macOS で ctypes が使えないとき） ------------------------------------

def run(argv, timeout=5):
    """(終了コード, stdout)。動かせなければ (None, "")。テストでは偽物にする。"""
    try:
        r = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, ""


def _ps(pid, field):
    rc, out = run(["ps", "-ww", "-o", f"{field}=", "-p", str(int(pid))])
    out = (out or "").strip()
    return out if rc == 0 and out else None


# ---- macOS の ctypes の層 -----------------------------------------------------

CTL_KERN, KERN_ARGMAX, KERN_PROCARGS2 = 1, 8, 49
PROC_PIDTBSDINFO = 3
BSDINFO_SIZE = 136  # struct proc_bsdinfo（pbi_ppid は 16 バイト目、pbi_start_tvsec / tvusec は 120 / 128 バイト目）


def parse_procargs2(buf):
    """KERN_PROCARGS2 の中身 → (argv, environ の dict)。形が違えば None。

    形: argc（int32）、実行ファイルのパス、NUL の詰め物、argc 個の引数、環境の「名前=値」…、空の文字列。
    """
    if not isinstance(buf, (bytes, bytearray)) or len(buf) < 4:
        return None
    argc = struct.unpack_from("=i", buf, 0)[0]
    if argc < 0 or argc > 1 << 16:
        return None
    rest = bytes(buf[4:])
    end = rest.find(b"\0")
    if end < 0:
        return None
    pos = end
    while pos < len(rest) and rest[pos:pos + 1] == b"\0":
        pos += 1
    parts = rest[pos:].split(b"\0")
    if len(parts) < argc:
        return None
    argv = [p.decode(errors="replace") for p in parts[:argc]]
    env = {}
    for p in parts[argc:]:
        if not p:
            break
        k, sep, v = p.partition(b"=")
        if sep:
            env[k.decode(errors="replace")] = v.decode(errors="replace")
    return argv, env


def parse_bsdinfo(buf):
    """struct proc_bsdinfo → (ppid, 起動時刻の文字列 "秒.マイクロ秒")。短ければ None。"""
    if not isinstance(buf, (bytes, bytearray)) or len(buf) < BSDINFO_SIZE:
        return None
    ppid = struct.unpack_from("=I", buf, 16)[0]
    sec, usec = struct.unpack_from("=QQ", buf, 120)
    return int(ppid), f"{sec}.{usec:06d}"


class _Darwin:
    """libSystem を ctypes で呼ぶ。使えなければ各関数が None を返す（呼ぶ側が ps に倒す）。"""

    def __init__(self):
        self._lib = None
        self._failed = False

    def lib(self):
        if self._lib is None and not self._failed:
            try:
                import ctypes
                try:
                    self._lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
                except OSError:
                    self._lib = ctypes.CDLL(None, use_errno=True)
                self._ctypes = ctypes
            except Exception:  # noqa: BLE001 — ctypes が無い・読めない
                self._failed = True
        return self._lib

    def _sysctl(self, mib, size):
        lib = self.lib()
        if lib is None:
            return None
        c = self._ctypes
        arr = (c.c_int * len(mib))(*mib)
        if getattr(self, "_buf_size", 0) < size:   # pid ごとに大きな buffer を作り直さない（processes() は数百回呼ぶ）
            self._buf, self._buf_size = c.create_string_buffer(size), size
        buf = self._buf
        n = c.c_size_t(size)
        try:
            if lib.sysctl(arr, c.c_uint(len(mib)), buf, c.byref(n), None, c.c_size_t(0)) != 0:
                return None
        except Exception:  # noqa: BLE001
            return None
        return c.string_at(buf, n.value)

    def procargs(self, pid):
        raw = self._sysctl([CTL_KERN, KERN_ARGMAX], 4)
        argmax = struct.unpack("=i", raw[:4])[0] if raw and len(raw) >= 4 else 0
        if argmax <= 0:
            argmax = 1 << 20
        return self._sysctl([CTL_KERN, KERN_PROCARGS2, int(pid)], min(argmax, 4 << 20))

    def bsdinfo(self, pid):
        lib = self.lib()
        if lib is None:
            return None
        c = self._ctypes
        buf = c.create_string_buffer(BSDINFO_SIZE)
        try:
            n = lib.proc_pidinfo(c.c_int(int(pid)), c.c_int(PROC_PIDTBSDINFO), c.c_uint64(0), buf, c.c_int(BSDINFO_SIZE))
        except Exception:  # noqa: BLE001
            return None
        return buf.raw if n == BSDINFO_SIZE else None

    def pids(self):
        lib = self.lib()
        if lib is None:
            return None
        c = self._ctypes
        try:
            n = lib.proc_listallpids(None, c.c_int(0))
            if n <= 0:
                return None
            cap = n + 256
            arr = (c.c_int * cap)()
            n = lib.proc_listallpids(arr, c.c_int(c.sizeof(arr)))
        except Exception:  # noqa: BLE001
            return None
        if n <= 0:
            return None
        return [p for p in arr[:min(n, cap)] if p > 0]


_darwin = _Darwin()


# ---- 共通の入り口 -------------------------------------------------------------

def _linux_stat(pid):
    try:
        with open(f"{PROC}/{int(pid)}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()
    except (OSError, IndexError, ValueError):
        return None


def _linux_zombie(pid):
    """/proc/<pid>/stat の状態が Z（ゾンビ）か X（終わった）か。読めなければ False。"""
    st = _linux_stat(pid)
    return bool(st) and st[0] in ("Z", "X")


def argv(pid):
    """プロセスの引数のリスト。読めなければ None（終わった・権限が無い）。"""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if is_linux():
        try:
            with open(f"{PROC}/{pid}/cmdline", "rb") as f:
                raw = f.read()
        except OSError:
            return None
        return [a.decode(errors="replace") for a in raw.split(b"\0") if a]
    if is_darwin():
        got = parse_procargs2(_darwin.procargs(pid) or b"")
        if got is not None:
            return got[0]
        out = _ps(pid, "args")
        return out.split() if out else None  # 空白の区切りしか分からない（引数の中の空白は切れる）
    return None


def environ(pid):
    """プロセスが起動したときの環境の dict。読めなければ None。"""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if is_linux():
        try:
            with open(f"{PROC}/{pid}/environ", "rb") as f:
                raw = f.read()
        except OSError:
            return None
        return dict(kv.decode(errors="replace").split("=", 1) for kv in raw.split(b"\0") if b"=" in kv)
    if is_darwin():
        got = parse_procargs2(_darwin.procargs(pid) or b"")
        return got[1] if got is not None else None
    return None


def ppid(pid):
    """親の pid。分からなければ None。"""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if is_linux():
        st = _linux_stat(pid)
        try:
            return int(st[1]) if st else None
        except (IndexError, ValueError):
            return None
    if is_darwin():
        got = parse_bsdinfo(_darwin.bsdinfo(pid) or b"")
        if got is not None:
            return got[0]
        out = _ps(pid, "ppid")
        try:
            return int(out) if out else None
        except ValueError:
            return None
    return None


def start_time(pid):
    """起動時刻の文字列（同じ OS で読んだもの同士でだけ比べる）。プロセスが無ければ None。"""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if is_linux():
        st = _linux_stat(pid)
        if st and st[0] in ("Z", "X"):
            return None  # ゾンビは終わったものとして扱う（alive と同じ）
        try:
            return st[19] if st else None
        except IndexError:
            return None
    if is_darwin():
        got = parse_bsdinfo(_darwin.bsdinfo(pid) or b"")
        if got is not None:
            return got[1]
        return _ps(pid, "lstart")
    return None


def proc_start_matches(recorded, start):
    """Claude Code が書いた procStart（recorded）と start_time() が同じプロセスを指すか。

    Linux は文字列で比べる（同じ値）。それ以外は Claude Code が何を書くか分からないので比べない（True）。
    """
    if recorded is None or not is_linux():
        return True
    return str(recorded) == start


def pids():
    """いまのプロセスの pid の一覧。"""
    if is_linux():
        try:
            return [int(n) for n in os.listdir(PROC) if n.isdigit()]
        except OSError:
            return []
    if is_darwin():
        got = _darwin.pids()
        if got is not None:
            return got
        rc, out = run(["ps", "-A", "-o", "pid="])
        if rc != 0:
            return []
        return [int(x) for x in out.split() if x.isdigit()]
    return []


def processes():
    """[(pid, argv)]。読めないプロセスは飛ばす。

    macOS: ctypes が使えれば sysctl で 1 つずつ読み（ほかのユーザーのプロセスは読めないので飛ばす。1 つずつ ps に
    倒すと数百回 ps を起こすので倒さない）、使えなければ ps を 1 回だけ呼ぶ（引数は空白の区切りでしか分からない）。"""
    if is_darwin():
        got = _darwin.pids()
        if got is None:
            rc, out = run(["ps", "-A", "-ww", "-o", "pid=,args="])
            res = []
            for line in (out or "").splitlines() if rc == 0 else []:
                parts = line.split()
                if len(parts) >= 2 and parts[0].isdigit():
                    res.append((int(parts[0]), parts[1:]))
            return res
        res = []
        for p in got:
            a = parse_procargs2(_darwin.procargs(p) or b"")
            if a is not None:
                res.append((p, a[0]))
        return res
    out = []
    for p in pids():
        a = argv(p)
        if a is not None:
            out.append((p, a))
    return out
