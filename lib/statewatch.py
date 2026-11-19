"""claude-state watch: kitty の外の常駐プロセスで、done（終わって、まだ見ていない）のペインを idle に戻す。

kitty の watcher（kitty/claude_state_watcher.py）は、kitty が窓を作ったときの設定で付くので、動いている kitty で
設定を読み直しても、前からあるペインには付かない（kitty を再起動しないと効かない）。こちらは kitty の外から
1 秒ごとに ls を読んで、同じ規則（前面の入力先のペインに done_dwell 秒続けてフォーカスが乗ったら idle）を行う。

- ls と get-colors は、kitty の socket にリモート操作の文（`ESC P @kitty-cmd {json} ESC \\`）を直接書いて読む
  （毎秒 kitten を起こすと、そのたびにプロセスが 1 つ立ち上がるため）。色を変える set-colors は、まれなので kitten で。
- idle にする処理は watcher の mark_seen をそのまま使う（同じロック .<session_id>.lock を待たずに取る。
  取れなければ次の回にやり直す）。watcher と二重に動いても、2 回目は done でないので何もしない。
- kitty が設定を読み直すと set-colors の色が全ペインで消える。ペインごとに、記録の色と get-colors の色が 2 回続けて
  違ったら、記録の色で塗り直す（1 回だけの違いは、hook が記録を書いてから色を変えるまでの間かもしれないので待つ）。
"""
import glob
import importlib.machinery
import importlib.util
import json
import os
import re
import socket
import stat
from pathlib import Path

from kstate import all_states, alive
from state import background_for

HERE = Path(__file__).resolve().parent.parent
WATCHER = HERE / "kitty" / "claude_state_watcher.py"
RC_VERSION = [0, 14, 2]  # kitty はこの番号を互換の確かめにしか使わない
_PREFIX, _SUFFIX = b"\x1bP@kitty-cmd", b"\x1b\\"


def load_watcher(path=WATCHER):
    """kitty の watcher（mark_seen・DEFAULT_DWELL）を読み込む。kitty が読む同じファイルを使い、規則を 1 か所に保つ。"""
    loader = importlib.machinery.SourceFileLoader("claude_state_watcher", str(path))
    spec = importlib.util.spec_from_loader("claude_state_watcher", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class RcError(Exception):
    pass


def _unix_address(sock):
    """'unix:/path' → '/path'、'unix:@name' → '\\0name'。unix でなければ None。"""
    if not isinstance(sock, str) or not sock.startswith("unix:"):
        return None
    p = sock[len("unix:"):]
    return "\0" + p[1:] if p.startswith("@") else p


def rc_call(sock, cmd, payload=None, timeout=2.0):
    """kitty のリモート操作を socket で 1 回呼び、data を返す。失敗は RcError（接続できない・ok でない・壊れた応答）。"""
    addr = _unix_address(sock)
    if addr is None:
        raise RcError(f"unix の socket でない: {sock}")
    msg = {"cmd": cmd, "version": RC_VERSION, "no_response": False}
    if payload is not None:
        msg["payload"] = payload
    buf = b""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.settimeout(timeout)
            c.connect(addr)
            c.sendall(_PREFIX + json.dumps(msg).encode() + _SUFFIX)
            while not buf.endswith(_SUFFIX):
                chunk = c.recv(1 << 16)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > 64 << 20:
                    raise RcError("応答が大きすぎる")
    except OSError as e:
        raise RcError(f"{type(e).__name__}: {e}") from None
    if not (buf.startswith(_PREFIX) and buf.endswith(_SUFFIX)):
        raise RcError(f"応答の形が違う: {buf[:60]!r}")
    try:
        r = json.loads(buf[len(_PREFIX):-len(_SUFFIX)])
    except ValueError as e:
        raise RcError(f"応答が JSON でない: {e}") from None
    if not isinstance(r, dict) or not r.get("ok"):
        raise RcError(f"{cmd} に失敗: {(r or {}).get('error') if isinstance(r, dict) else r!r}")
    return r.get("data")


def rc_ls(sock, timeout=2.0):
    data = rc_call(sock, "ls", {}, timeout)
    try:
        out = json.loads(data) if isinstance(data, str) else data
    except ValueError as e:
        raise RcError(f"ls が JSON でない: {e}") from None
    if not isinstance(out, list):
        raise RcError("ls が一覧でない")
    return out


def rc_background(sock, wid, timeout=2.0):
    """そのペインの今の背景色（#rrggbb、小文字）。分からなければ None。"""
    data = rc_call(sock, "get-colors", {"match": f"id:{wid}", "configured": False}, timeout)
    for line in str(data or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "background":
            return parts[1].lower()
    return None


def _proc_exists(pid):
    return os.path.exists(f"/proc/{pid}")


def find_sockets(env, configured=None, exists=_proc_exists):
    """見る kitty の socket（'unix:/path'）。設定があればそれ、無ければ $XDG_RUNTIME_DIR/kitty-* の生きているもの。

    名前が kitty-<pid> で、その pid が無いもの（kitty が落ちて残った socket）は除く。
    """
    if configured:
        return list(dict.fromkeys(s for s in configured if isinstance(s, str) and s))
    base = env.get("XDG_RUNTIME_DIR") or "/tmp"
    out = []
    for p in sorted(glob.glob(os.path.join(base, "kitty-*"))):
        m = re.search(r"kitty-(\d+)$", p)
        if m and not exists(int(m.group(1))):
            continue
        try:
            if not stat.S_ISSOCK(os.stat(p).st_mode):
                continue
        except OSError:
            continue
        out.append("unix:" + p)
    return out


def focused_pane(osw_list):
    """ユーザーがいま見ているペインの id: 前面の OS 窓（is_focused）の、表示中のタブ（is_active）の、入力先のペイン。

    kitty が前面でなければ（別のアプリ・別のデスクトップ）、どの OS 窓も is_focused でないので None。
    ペインの is_focused はタブごとの「最後の入力先」なので、タブが表示中かも確かめる（state.viewing と同じ規則）。
    """
    for osw in osw_list or []:
        if not osw.get("is_focused"):
            continue
        for t in osw.get("tabs", []):
            if not t.get("is_active"):
                continue
            for w in t.get("windows", []):
                if w.get("is_focused") and isinstance(w.get("id"), int):
                    return w["id"]
    return None


def window_ids(osw_list):
    return {w.get("id") for osw in osw_list or [] for t in osw.get("tabs", []) for w in t.get("windows", [])}


def done_info(entries, wid, sock, default_dwell):
    """そのペインの done の記録の (dwell 秒, done になった時刻)。無ければ None。"""
    out = None
    for e in entries:
        if e.get("window") != wid or e.get("socket") != sock or e.get("state") != "done":
            continue
        v = e.get("done_dwell")
        dwell = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else default_dwell
        s = e.get("since")
        since = float(s) if isinstance(s, (int, float)) and not isinstance(s, bool) else 0.0
        out = (dwell, since) if out is None else (max(out[0], dwell), max(out[1], since))
    return out


class Watch:
    """1 回の tick で、全部の kitty の前面のペインを見て、done_dwell 秒続いた done を idle にする。

    ls・get_bg・set_bg・log・alive は差し替えられる（テストでは偽物）。
    """

    def __init__(self, d, colors, base, ls=rc_ls, get_bg=rc_background, set_bg=None, log=None,
                 alive=alive, watcher=None, repaint=True):
        self.d, self.colors, self.base = Path(d), colors, base
        self.ls, self.get_bg, self.set_bg = ls, get_bg, set_bg or (lambda sock, wid, bg: True)
        self.log = log or (lambda msg: None)
        self.alive = alive
        self.w = watcher or load_watcher()
        self.repaint = repaint
        self.focus = {}  # socket → (ペイン id or None, そのペインに来た時刻)
        self.failing = set()  # ls に失敗し続けている socket（errors.log には最初の 1 回だけ書く）
        self.mismatch = {}  # (socket, ペイン id) → 前の回に色が違った (記録の色, 実際の色)

    def tick(self, sockets, now):
        """[(socket, ペイン id, session id)] を返す（idle にしたもの）。例外は socket ごとに errors.log へ。"""
        marked = []
        for sock in sockets:
            try:
                marked += self._tick_one(sock, now)
            except Exception as e:  # noqa: BLE001 — 1 つの kitty の失敗で全体を止めない
                self.log(f"{sock}: {type(e).__name__}: {e}")
        now_socks = set(sockets)
        for gone in set(self.focus) - now_socks:
            self.focus.pop(gone, None)
        self.failing &= now_socks  # 消えた socket がまた現れて失敗したら、また書く
        for k in [k for k in self.mismatch if k[0] not in now_socks]:
            self.mismatch.pop(k)
        return marked

    def _tick_one(self, sock, now):
        try:
            osw_list = self.ls(sock)
        except RcError as e:
            if sock not in self.failing:
                self.log(f"{sock}: ls に失敗（続くあいだは書かない）: {e}")
                self.failing.add(sock)
            self.focus.pop(sock, None)  # 見えなかった間にフォーカスが動いたかもしれないので数え直す
            return []
        self.failing.discard(sock)
        wid = focused_pane(osw_list)
        prev = self.focus.get(sock)
        if prev is None or prev[0] != wid:
            self.focus[sock] = prev = (wid, now)  # 別のペインへ動いた（kitty が前面でなくなったのも含む）: 数え直す
        entries = all_states(self.d)
        out = []
        if wid is not None:
            info = done_info(entries, wid, sock, self.w.DEFAULT_DWELL)
            # 続けて見ていた時間は、そこへ来た時か done になった時の遅い方から（緑が dwell 秒見えていたこと）
            if info is not None and now - max(prev[1], info[1]) >= info[0]:
                done = self.w.mark_seen(self.d, wid, sock, now)  # ロックが取れなければ [] → 次の回にやり直す
                for sid, bg in done[-1:]:
                    if bg:
                        self._set(sock, wid, bg)  # hook が割り込んで食い違っても、色の確かめで記録の色に直る
                out += [(sock, wid, sid) for sid, _bg in done]
                if done:
                    entries = all_states(self.d)
        if self.repaint:
            self._check_repaint(sock, osw_list, entries)
        return out

    def _check_repaint(self, sock, osw_list, entries):
        """記録のあるペインごとに実際の色（get-colors、1 回約 1 ms）と比べ、2 回続けて違ったペインを記録の色で塗り直す。

        期待する色は hook が書いた記録の background（watch が起動時に読んだ設定ではない。設定を変えても取り合わない）。
        1 回だけの違いは、hook が記録を書いてから set-colors するまでの間かもしれないので待つ。
        load-config で消えた色、idle にしたあと hook が割り込んで食い違った色、set-colors の失敗も、これで直る。
        """
        present = window_ids(osw_list)
        seen = {}
        for w, bg in record_backgrounds(entries, sock, self.colors, self.base, self.alive):
            if w not in present:
                continue
            try:
                actual = self.get_bg(sock, w)
            except RcError:
                continue
            if actual is None or actual == bg:
                continue
            key = (bg, actual)
            if self.mismatch.get((sock, w)) != key:
                seen[(sock, w)] = key  # 1 回目は待つ
                continue
            self.log(f"{sock}: ペイン {w} の背景が記録（{bg}）と違う（{actual}）ので塗り直す（設定の読み直しなど）")
            self._set(sock, w, bg)
        for k in [k for k in self.mismatch if k[0] == sock]:
            self.mismatch.pop(k)
        self.mismatch.update(seen)

    def _set(self, sock, wid, bg):
        ok = self.set_bg(sock, wid, bg)
        if ok is False:
            self.log(f"{sock}: ペイン {wid} を {bg} に塗れなかった（次の回の色の確かめで塗り直す）")
        return ok


def record_backgrounds(entries, sock, colors, base, alive_fn):
    """[(ペイン id, 記録の背景色（小文字）)]。同じペインに記録が 2 つあれば新しい方。記録に色が無ければ設定から求める。"""
    best = {}
    for e in entries:
        w = e.get("window")
        if e.get("socket") != sock or not isinstance(w, int) or not alive_fn(e):
            continue
        if w not in best or (e.get("updated") or 0) > (best[w].get("updated") or 0):
            best[w] = e
    out = []
    for w, e in sorted(best.items()):
        bg = e.get("background")
        bg = bg if isinstance(bg, str) and bg else background_for(e.get("state"), colors, base)
        out.append((w, bg.lower()))
    return out
