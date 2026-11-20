"""「保護する窓（view.guard）が前面か」の判定（claude-view / claude-show 共通）。

全画面のアプリや発表中の画面など、キーやフォーカスを奪われたくない窓が前面にあるときは、
画面を動かす操作を控える。

前面の窓の pid は、Linux（KDE Plasma）は KWin、macOS は `lsappinfo`（OS に付いてくる。許可は要らない）で読む。
macOS で前面のアプリを読めないときは、当たるプロセスが動いているだけで「前面にある」とみなす（動かさない側に倒す）。
"""
import subprocess
import sys

PLATFORM = sys.platform


def mac_front_pid(run=subprocess.run):
    """macOS の前面のアプリの pid（lsappinfo front → lsappinfo info -only pid）。読めなければ None。"""
    try:
        r = run(["lsappinfo", "front"], capture_output=True, text=True, timeout=3)
        asn = (r.stdout or "").strip()
        if r.returncode != 0 or not asn:
            return None
        r = run(["lsappinfo", "info", "-only", "pid", asn], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_lsappinfo_pid(r.stdout if r.returncode == 0 else "")


def parse_lsappinfo_pid(text):
    """`"pid"=1234` → 1234。形が違えば None。"""
    import re
    m = re.search(r'"?pid"?\s*=\s*(\d+)', text or "")
    return int(m.group(1)) if m else None


def guarded_in_front(patterns, run=subprocess.run, active_pid=None, platform=None):
    """patterns（pgrep -f の正規表現）に当たるプロセスの窓が前面なら True。

    patterns が空なら前面の窓を調べない（KWin を呼ばない）で False。pgrep が失敗したら ValueError
    （正規表現の誤りなどを「当たらない」と見なすと、保護が黙って外れるため）。
    """
    pids = set()
    for pat in patterns:
        r = run(["pgrep", "-f", "--", pat], capture_output=True, text=True)
        if r.returncode > 1:
            raise ValueError(f"view.guard の {pat!r} で pgrep が失敗: {r.stderr.strip()}")
        pids |= {int(p) for p in r.stdout.split()}
    if not pids:
        return False
    if active_pid is None:
        if (PLATFORM if platform is None else platform) == "darwin":
            front = mac_front_pid(run)
            return True if front is None else front in pids  # 読めなければ動かさない側
        from kwin import active_window_pid as active_pid
    return active_pid() in pids
