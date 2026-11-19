"""「保護する窓（view.guard）が前面か」の判定（claude-view / claude-show 共通）。

全画面のアプリや発表中の画面など、キーやフォーカスを奪われたくない窓が前面にあるときは、
画面を動かす操作を控える。
"""
import subprocess


def guarded_in_front(patterns, run=subprocess.run, active_pid=None):
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
        from kwin import active_window_pid as active_pid
    return active_pid() in pids
