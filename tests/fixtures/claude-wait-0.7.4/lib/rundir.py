# v0.7.4（8ab658a）から写した。claude-roster sleep の試験で、「終わる」の指示を知らない前の版の claude-wait として動かすため（CI は履歴を 1 つしか取らないので git から取り出せない）
"""道具の記録の置き場と、kitty の socket を探す場所（claude-state・claude-browser・claude-wait・claude-view・claude-roster 共通）。

- runtime_dir: $XDG_RUNTIME_DIR。無ければ（macOS、cron・ssh など）/tmp/claude-kitty-<uid>（0700 で作る）。
  /tmp はほかのユーザーと共有なので、名前に uid を入れて分ける。$TMPDIR は使わない（macOS では起動のしかたで
  変わりうるので、hook・launchd の常駐・kitty の watcher で場所がずれないよう、どこからでも同じになる名前にする）。
  kitty の watcher（kitty/claude_state_watcher.py）は kitty の中で動いてこのファイルを読めないので、同じ決まりを写して持つ。
- socket_dirs: kitty の socket（kitty-<pid>）を探すフォルダ。$XDG_RUNTIME_DIR、無ければ /tmp（kitty の
  listen_on と同じ所。kitty/claude-kitty-macos.conf は /tmp）。macOS では $XDG_RUNTIME_DIR があっても /tmp も見る
  （claude-kitty-macos.conf は /tmp に置くが、シェルの rc で XDG_RUNTIME_DIR を決めている人もいるため）。
"""
import os
import sys
from pathlib import Path

PLATFORM = sys.platform


def fallback_dir(uid=None):
    return Path("/tmp") / f"claude-kitty-{os.getuid() if uid is None else uid}"


def runtime_dir(env=None, create=True):
    env = os.environ if env is None else env
    base = env.get("XDG_RUNTIME_DIR")
    if base:
        return Path(base)
    d = fallback_dir()
    if create:
        try:
            d.mkdir(mode=0o700, exist_ok=True)
        except OSError:
            pass  # 作れなくても場所は返す（書く側が失敗を扱う）
    if not owned_private(d):
        # ほかのユーザーが先に同じ名前（リンクを含む）を作っていた: 使わずに、自分だけの別の名前にする
        d = d.with_name(d.name + "-own")
        if create:
            try:
                d.mkdir(mode=0o700, exist_ok=True)
            except OSError:
                pass
    return d


def owned_private(d):
    """d が無いか、リンクでない自分のフォルダで、ほかのユーザーが書けないこと。"""
    import stat
    try:
        st = os.lstat(d)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not (st.st_mode & 0o022)


def socket_dirs(env=None, platform=None):
    env = os.environ if env is None else env
    platform = PLATFORM if platform is None else platform
    out = [env.get("XDG_RUNTIME_DIR") or "/tmp"]
    if platform == "darwin" and "/tmp" not in out:
        out.append("/tmp")
    return out


def live_kitty_sockets(env=None, platform=None, alive=None):
    """生きている kitty の socket（'unix:/path'）。名前が kitty-<pid> で、その pid が生きているものだけ
    （kitty が落ちて残った socket を拾わない。claude-view・claude-browser・claude-roster が使う）。"""
    import re
    if alive is None:
        from proc import alive
    out = []
    for p in kitty_socket_paths(env, platform):
        m = re.search(r"kitty-(\d+)$", p)
        if m and alive(int(m.group(1))):
            out.append("unix:" + p)
    return out


def kitty_socket_paths(env=None, platform=None):
    """socket_dirs の kitty-* のパス（重なりなし、名前順）。生きているかは呼ぶ側が見る。"""
    out = []
    for d in socket_dirs(env, platform):
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if n.startswith("kitty-"):
                p = os.path.join(d, n)
                if p not in out:
                    out.append(p)
    return out
