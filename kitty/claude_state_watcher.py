"""kitty の watcher: claude-state の done（終わって、まだ見ていない）のペインにユーザーが来て、そのまま
2 秒（記録の done_dwell。設定 state.done_dwell_seconds）フォーカスが続いたら idle に戻す。
通りすがりにフォーカスが乗っただけでは、見たことにしない。kitty の OS 窓が前面でなければ数えない。

kitty.conf に `watcher claude_state_watcher.py`（kitty の設定ディレクトリからの相対パス）と書く。
kitty の中（メインスレッド）で動くので、外部コマンドを待たず、state ファイルの更新と
set-colors（boss.call_remote_control。kitty の中の呼び出しで、プロセスを起こさない）だけを行う。
例外は kitty に投げない（ここで止まると kitty の入力が止まる）。

フォーカスの変化は 2 つの経路で受ける:
  on_focus_change   ペインごとの watcher。kitty が窓を作ったときに写し取るので、設定を読み直す前からある
                    ペインには付かない
  on_tab_bar_dirty  OS 窓ごとに、その時点の設定の watcher を呼ぶ。設定を読み直したあとは、前からある
                    ペインでもフォーカスの移動（タブの切り替え・OS 窓の前面化を含む）で呼ばれる

kitty が設定を読み直すと、set-colors で付けた背景が全ペインで kitty.conf の色に戻る。読み直すと
get_options() が別のものになるので、tab_bar_dirty でそれに気づいたら、記録の色（background）で塗り直す。
"""
import fcntl
import json
import os
import time
from pathlib import Path

_opts = [None]  # 最後に見た get_options() の id（設定を読み直すと変わる）
_gen = {}  # OS 窓 id → フォーカスが動いた回数（2 秒の間に一度でも離れたら、前のタイマーを無効にする）
_last = {}  # OS 窓 id → 最後に見た (前面か, ペイン id)。tab_bar_dirty は題が変わるたびに来るので、同じなら何もしない


def state_dir(env=None):
    """lib/kstate.py の state_dir と同じ場所（kitty の中では lib を読めないので、lib/rundir.py の決まりを写して持つ）。"""
    env = os.environ if env is None else env
    base = env.get("XDG_RUNTIME_DIR")
    return (Path(base) if base else Path("/tmp") / f"claude-kitty-{os.getuid()}") / "claude-state"


def _load(p):
    try:
        data = json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def mark_seen(d, wid, sock, now=None):
    """そのペイン（wid, sock）の done の記録を idle にする。[(session id, 戻す背景色 or None)] を返す。

    hook と同時に書かないよう、セッションごとのロックを待たずに取る（取れなければ今回は見送る）。
    """
    now = time.time() if now is None else now
    out = []
    try:
        paths = sorted(Path(d).glob("*.json"))
    except OSError:
        return out
    for p in paths:
        e = _load(p)
        if not e or e.get("window") != wid or e.get("socket") != sock or e.get("state") != "done":
            continue
        try:
            lock = open(Path(d) / f".{p.stem}.lock", "w")
        except OSError:
            continue
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue
            e = _load(p)  # ロックを取ったあとに読み直す（その間に hook が変えたかもしれない）
            if not e or e.get("state") != "done":
                continue
            e.update(state="idle", since=now, updated=now, reason=None, seen=True)
            if isinstance(e.get("idle_background"), str):
                e["background"] = e["idle_background"]
            tmp = p.with_name(f".{p.name}.{os.getpid()}.watcher.tmp")
            try:
                tmp.write_text(json.dumps(e, ensure_ascii=False))
                os.replace(tmp, p)
            except OSError:
                continue
            bg = e.get("idle_background")
            out.append((e.get("session_id") or p.stem, bg if isinstance(bg, str) else None))
        finally:
            lock.close()
    return out


def _sock(boss):
    s = getattr(boss, "listening_on", None)
    return s if isinstance(s, str) and s else None


DEFAULT_DWELL = 2.0  # done のペインにフォーカスがこれだけ続いたら見たとする（記録の done_dwell で変わる）


def pending_dwell(d, wid, sock):
    """そのペインに done の記録があれば、見たとするまでの秒数（無ければ None）。"""
    try:
        paths = sorted(Path(d).glob("*.json"))
    except OSError:
        return None
    out = None
    for p in paths:
        e = _load(p)
        if e and e.get("window") == wid and e.get("socket") == sock and e.get("state") == "done":
            v = e.get("done_dwell")
            v = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else DEFAULT_DWELL
            out = v if out is None else max(out, v)
    return out


def _still_focused(boss, window):
    """window が、前面の OS 窓の入力先のままか。"""
    if getattr(window, "destroyed", False):
        return False
    active = getattr(boss, "active_window", None)
    if getattr(active, "id", None) != getattr(window, "id", None):
        return False
    osw = getattr(window, "os_window_id", None)
    return osw is None or _os_window_focused(osw)


def seen(boss, window):
    """window にフォーカスが来た。done なら、dwell 秒たってもまだそこにフォーカスがあれば idle にする。

    kitty の中なので待たない: kitty のタイマー（add_timer）で後から確認する。
    """
    sock = _sock(boss)
    if window is None or sock is None:
        return []
    wid = getattr(window, "id", None)
    if not isinstance(wid, int):
        return []
    dwell = pending_dwell(state_dir(), wid, sock)
    if dwell is None:
        return []
    if dwell <= 0:
        return _mark(boss, window)
    osw = getattr(window, "os_window_id", None)
    gen = _gen.get(osw, 0)
    # 「続けて」dwell 秒: その間にフォーカスが一度でも動けば（離れてすぐ戻っても）、このタイマーは使わない
    _later(lambda: _mark(boss, window) if _gen.get(osw, 0) == gen and _still_focused(boss, window) else None, dwell)
    return []


RETRY_DELAY = 0.5


def _mark(boss, window, retry=True):
    """window の done の記録を idle にして、背景を戻す。hook がロックを持っていて見送ったら、一度だけやり直す。"""
    sock = _sock(boss)
    if window is None or sock is None:
        return []
    wid = getattr(window, "id", None)
    if not isinstance(wid, int):
        return []
    done = mark_seen(state_dir(), wid, sock)
    if retry and pending_dwell(state_dir(), wid, sock) is not None:
        _later(lambda: _mark(boss, window, False) if _still_focused(boss, window) else None, RETRY_DELAY)
    for _sid, bg in done[-1:]:
        if bg:
            boss.call_remote_control(window, ("set-colors", f"--match=id:{wid}", f"background={bg}"))
    return done


def _alive(e):
    """記録を書いた claude がまだいるか。/proc の無い macOS でも効くよう signal 0 で調べる（権限が無いのは生きている）。"""
    pid = e.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def repaint_ops(d, sock):
    """記録の色で塗り直す [(window id, 背景色)]。同じペインに記録が 2 つあれば新しい方。"""
    best = {}
    try:
        paths = sorted(Path(d).glob("*.json"))
    except OSError:
        return []
    for p in paths:
        e = _load(p)
        if not e or e.get("socket") != sock or not isinstance(e.get("window"), int):
            continue
        if not isinstance(e.get("background"), str) or not _alive(e):
            continue
        w = e["window"]
        if w not in best or (e.get("updated") or 0) > (best[w].get("updated") or 0):
            best[w] = e
    return [(w, e["background"]) for w, e in sorted(best.items())]


def repaint(boss):
    sock = _sock(boss)
    if sock is None:
        return []
    windows = getattr(boss, "window_id_map", None) or {}
    done = []
    for wid, bg in repaint_ops(state_dir(), sock):
        w = windows.get(wid)
        if w is not None:
            boss.call_remote_control(w, ("set-colors", f"--match=id:{wid}", f"background={bg}"))
            done.append((wid, bg))
    return done


REPAINT_DELAY = 0.3  # tab_bar_dirty は、読み直しで色が戻されるより前にも来るので、少し後に塗る


def _later(fn, delay=REPAINT_DELAY):
    """kitty のタイマーで delay 秒後に呼ぶ（待たない）。タイマーが無ければ（kitty の外）今呼ぶ。"""
    def run(*_):
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass
    try:
        from kitty.fast_data_types import add_timer
    except ImportError:
        return run()
    add_timer(run, delay, False)


def _options_changed():
    try:
        from kitty.fast_data_types import get_options
    except ImportError:
        return False
    o = id(get_options())
    if _opts[0] == o:
        return False
    _opts[0] = o
    return True


def _os_window_focused(os_window_id):
    try:
        from kitty.fast_data_types import current_focused_os_window_id
    except ImportError:
        return True
    return current_focused_os_window_id() == os_window_id


def _bump(osw, key):
    if _last.get(osw) != key:
        _gen[osw] = _gen.get(osw, 0) + 1
    _last[osw] = key


def on_focus_change(boss, window, data):
    try:
        if not data.get("focused"):
            osw = getattr(window, "os_window_id", None)
            _bump(osw, (_os_window_focused(osw), None))  # 離れた
            return
        osw = getattr(window, "os_window_id", None)
        if osw is not None and not _os_window_focused(osw):
            return  # kitty が前面でない（OS 窓のフォーカスが外れた直後など）
        _bump(osw, (True, getattr(window, "id", None)))
        seen(boss, window)
    except Exception:  # noqa: BLE001 — kitty に例外を投げない
        pass


def on_tab_bar_dirty(boss, window, data):
    try:
        if _options_changed():
            _later(lambda: repaint(boss))
        tm = data.get("tab_manager")
        if tm is None:
            return
        osw = getattr(tm, "os_window_id", None)
        w = tm.active_window
        key = (_os_window_focused(osw), getattr(w, "id", None))
        if _last.get(osw) == key:
            return
        _bump(osw, key)
        if key[0] and w is not None:
            seen(boss, w)
    except Exception:  # noqa: BLE001
        pass
