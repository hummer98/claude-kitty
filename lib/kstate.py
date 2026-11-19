"""claude-state の副作用のある部分（state ファイル・ロック・kitten の呼び出し）。claude-view からも使う。

state ファイル: $XDG_RUNTIME_DIR/claude-state/<session_id>.json
  {"session_id", "state", "since", "updated", "reason", "waits", "agents", "main", "seen", "quiet", "seen_before",
   "window", "socket", "role", "pid", "cwd", "background", "idle_background", "done_dwell"}
  state は waits（待ち）・agents（サブエージェント）・main（本体が動いているか）・seen から state.derive() で決める。
"""
import contextlib
import fcntl
import json
import os
import subprocess
import time
from pathlib import Path

from state import is_claude_argv, tab_mark_ops, valid_session_id


def state_dir(env=None):
    env = os.environ if env is None else env
    return Path(env.get("XDG_RUNTIME_DIR") or "/tmp") / "claude-state"


def state_file(d, sid):
    if not valid_session_id(sid):
        raise ValueError(f"session id が正しくない: {sid!r}")
    return Path(d) / f"{sid}.json"


def load(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save(path, data):
    """書きかけを読ませないよう、別のファイルに書いてから rename する。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(tmp, path)


def all_states(d):
    out = []
    for p in sorted(Path(d).glob("*.json")):
        e = load(p)
        if e is not None:
            e.setdefault("session_id", p.stem)
            out.append(e)
    return out


def alive(entry):
    """state を書いた claude がまだ動いているか（落ちて SessionEnd が来なかった記録を除くため）。"""
    pid = entry.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return True  # pid が分からない記録は、消さずに残す（判断できない）
    try:
        argv = [a.decode(errors="replace") for a in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if a]
    except OSError:
        return False
    return is_claude_argv(argv)


@contextlib.contextmanager
def state_lock(d, timeout=2.0, name=".lock"):
    """タブの題の付け外しを 1 つずつにする。取れなければ False を渡す（待ち続けて hook を止めない）。"""
    Path(d).mkdir(parents=True, exist_ok=True)
    f = open(Path(d) / name, "w")
    got = False
    try:
        end = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    break
                time.sleep(0.05)
        yield got
    finally:
        f.close()


def turn_origin(transcript, limit=256 * 1024):
    """会話記録（JSONL）の末尾から、いまのターンを始めた記録の (turnOrigin, 本文) を返す。分からなければ (None, None)。

    ターンを始めた user の記録だけが turnOrigin（human / peer / task_notification など）を持つ。
    他のセッションからのメッセージ（peer）なら、本文は origin.body にある。
    """
    try:
        with open(transcript, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - limit))
            lines = f.read().split(b"\n")
    except (OSError, TypeError, ValueError):
        return None, None
    for raw in reversed(lines):
        if b'"turnOrigin"' not in raw:
            continue
        try:
            j = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(j, dict) or j.get("type") != "user" or "turnOrigin" not in j:
            continue
        origin = j.get("origin") if isinstance(j.get("origin"), dict) else {}
        body = origin.get("body") if isinstance(origin.get("body"), str) else None
        return j.get("turnOrigin"), body
    return None, None


def session_lock(d, sid, timeout=5.0):
    """1 つのセッションの記録を読んで書き直す間のロック（並行する hook と kitty の watcher が上書きし合わないよう）。

    watcher（kitty/claude_state_watcher.py）も同じ名前 .<session_id>.lock を使う。
    """
    if not valid_session_id(sid):
        raise ValueError(f"session id が正しくない: {sid!r}")
    return state_lock(d, timeout, f".{sid}.lock")


class Kitten:
    """kitten @ を短い timeout 付きで呼ぶ。失敗しても例外にせず None を返す（hook を止めないため）。

    使うのは ls・set-colors・set-tab-title・launch だけ（close 系と send-text は使わない）。
    """
    ALLOWED = {"ls", "set-colors", "set-tab-title", "launch"}

    def __init__(self, kitten, sock, timeout=2.0):
        self.kitten, self.sock, self.timeout = kitten, sock, timeout

    def __call__(self, *args):
        if args[0] not in self.ALLOWED:
            raise ValueError(f"kitten @ {args[0]} は使わない")
        try:
            r = subprocess.run([self.kitten, "@", "--to", self.sock, *args], capture_output=True, text=True,
                               timeout=self.timeout, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return r if r.returncode == 0 else None

    def ls(self):
        r = self("ls")
        if r is None:
            return None
        try:
            return json.loads(r.stdout)
        except ValueError:
            return None


def waiting_windows(entries, sock):
    return {e.get("window") for e in entries
            if e.get("state") == "waiting" and e.get("socket") == sock and alive(e)}


def refresh_marks(kit, d, left, clear=False, osw_list=None):
    """main 以外のタブで、waiting のペインがいるタブの題に「●」を付け、いなければ外す。"""
    osw_list = osw_list if osw_list is not None else kit.ls()
    if osw_list is None:
        return []
    ops = tab_mark_ops(osw_list, waiting_windows(all_states(d), kit.sock), left, clear=clear)
    for tab_id, title in ops:
        kit("set-tab-title", "--match", f"id:{tab_id}", title)
    return ops
