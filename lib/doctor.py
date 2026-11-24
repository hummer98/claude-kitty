#!/usr/bin/env python3
"""claude-kitty doctor: 入っているもの・動くものを 1 行ずつ調べ、足りなければ直し方を出す（読むだけ）。

  claude-kitty doctor [--json]

行の頭: OK（よい）・NG（問題。直し方を → の後に）・注意（無くても主な道具は動く）・--（この環境では調べない）。
終了コード: 0 = NG が無い、1 = NG がある。

kitty には `kitten @ ls`（読むだけ）しか送らない。systemd は is-enabled / is-active だけ、launchd は launchctl print
だけ。設定は各道具の読み込みの関数で読む。
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "tools"))

UNITS = ["claude-state-watch.service", "claude-roster-save.timer", "claude-session-stats.timer"]
AGENTS = ["claude-kitty.state-watch", "claude-kitty.roster-save", "claude-kitty.session-stats"]
MIN_PYTHON = (3, 9)  # macOS の Command Line Tools の /usr/bin/python3 が 3.9
HOOK_LABELS = {"state": "claude-state（状態の色）", "lang": "claude-lang-check", "guard": "claude-guard", "relay": "claude-relay",
               "model": "claude-presence（モデルの切り替え）"}


class Ctx:
    """調べる環境。テストでは run / which / os_name / env を差し替える。"""

    def __init__(self, env=None, os_name=None, run=None, which=None, repo=REPO, python=None, statvfs=None, procs=None):
        self.env = dict(os.environ if env is None else env)
        self.statvfs = statvfs or os.statvfs
        self.procs = procs  # lib/bgshell.py の Sys の代わり（テスト用）
        self.home = Path(self.env.get("HOME") or os.path.expanduser("~"))
        self.os_name = os_name or os.uname().sysname
        self._run = run
        self._which = which
        self.repo = Path(repo)
        self.python = python or sys.version_info[:3]

    def run(self, argv, timeout=5):
        """(終了コード, stdout)。動かせなければ (None, エラーの文)。"""
        if self._run:
            return self._run(list(argv), timeout)
        try:
            r = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
            return r.returncode, r.stdout
        except (OSError, subprocess.SubprocessError) as e:
            return None, str(e)

    def which(self, name):
        if self._which:
            return self._which(name)
        return shutil.which(name, path=self.env.get("PATH"))

    @property
    def bin_dir(self):
        return Path(self.env.get("CLAUDE_KITTY_BIN_DIR") or self.home / ".local" / "bin")

    @property
    def settings(self):
        return Path(self.env.get("CLAUDE_CONFIG_DIR") or self.home / ".claude") / "settings.json"

    @property
    def config_home(self):
        return Path(self.env.get("XDG_CONFIG_HOME") or self.home / ".config")

    @property
    def kitty_dir(self):
        return Path(self.env.get("KITTY_CONFIG_DIRECTORY") or self.config_home / "kitty")


class Report:
    def __init__(self):
        self.items = []

    def add(self, status, name, detail, fix=""):
        self.items.append({"status": status, "name": name, "detail": detail, "fix": fix})

    def ok(self, name, detail):
        self.add("ok", name, detail)

    def ng(self, name, detail, fix):
        self.add("ng", name, detail, fix)

    def warn(self, name, detail, fix=""):
        self.add("warn", name, detail, fix)

    def skip(self, name, detail):
        self.add("skip", name, detail)

    @property
    def problems(self):
        return sum(1 for i in self.items if i["status"] == "ng")

    @property
    def warnings(self):
        return sum(1 for i in self.items if i["status"] == "warn")


# ---- kitty ------------------------------------------------------------------

def find_kitty(ctx):
    for p in (ctx.env.get("CLAUDE_KITTY_KITTY"), str(ctx.home / ".local/kitty.app/bin/kitty"), ctx.which("kitty"),
              "/Applications/kitty.app/Contents/MacOS/kitty"):
        if p and os.path.exists(p):
            return p
    return None


def find_kitten(ctx):
    for p in (ctx.env.get("CLAUDE_KITTY_KITTEN"), str(ctx.home / ".local/kitty.app/bin/kitten"), ctx.which("kitten"),
              "/Applications/kitty.app/Contents/MacOS/kitten"):
        if p and os.path.exists(p):
            return p
    return None


def kitty_sockets(ctx):
    if ctx.env.get("KITTY_LISTEN_ON"):
        return [ctx.env["KITTY_LISTEN_ON"]]
    from statewatch import find_sockets
    return find_sockets(ctx.env)


def check_kitty(ctx, rep):
    kitty = find_kitty(ctx)
    if not kitty:
        rep.ng("kitty", "見つからない", "公式のインストーラで入れる（SETUP.md の 1.）か、$CLAUDE_KITTY_KITTY で場所を指す")
        return
    rc, out = ctx.run([kitty, "--version"])
    m = re.search(r"kitty (\d+(?:\.\d+)+)", out or "")
    if rc != 0 or not m:
        rep.ng("kitty", f"版を読めない（{kitty}）", "kitty --version が動くか確認する")
    else:
        rep.ok("kitty", f"{m.group(1)}（{kitty}）")
    kitten = find_kitten(ctx)
    socks = kitty_sockets(ctx)
    if not kitten:
        rep.ng("kitten @ ls", "kitten が見つからない", "kitty と一緒に入る。別の場所なら $CLAUDE_KITTY_KITTEN で指す")
        return
    if not socks:
        rep.warn("kitten @ ls", "動いている kitty のソケットが無い（確認できない）",
                 "claude-kitty で kitty を起動してから、もう一度 claude-kitty doctor")
        rep.warn("layout_state.pairs", "kitty が動いていないので確認できない")
        return
    pairs, splits, failed = 0, 0, []
    for s in socks:
        rc, out = ctx.run([kitten, "@", "--to", s, "ls"], timeout=5)
        try:
            data = json.loads(out) if rc == 0 else None
        except ValueError:
            data = None
        if not isinstance(data, list):
            failed.append(s)
            continue
        for osw in data:
            for t in osw.get("tabs", []) if isinstance(osw, dict) else []:
                st = t.get("layout_state") or {}
                if t.get("layout") == "splits" or st.get("class") == "Splits":
                    splits += 1
                    pairs += "pairs" in st
    if failed:
        where = "/tmp" if ctx.os_name == "Darwin" else "${XDG_RUNTIME_DIR}"
        rep.ng("kitten @ ls", f"{len(failed)}/{len(socks)} の kitty で失敗（{failed[0]}）",
               f"kitty.conf に allow_remote_control socket-only と listen_on unix:{where}/kitty-{{kitty_pid}}"
               "（tools/install.sh --kitty）を入れ、kitty を起動し直す")
    else:
        rep.ok("kitten @ ls", f"{len(socks)} 個の kitty で通る")
    if splits == 0:
        if len(failed) < len(socks):
            rep.warn("layout_state.pairs", "splits のタブが無いので確認できない",
                     "kitty.conf に enabled_layouts splits,stack（tools/install.sh --kitty）")
    elif pairs == splits:
        rep.ok("layout_state.pairs", f"splits の {splits} 個のタブで出る")
    else:
        rep.ng("layout_state.pairs", f"splits の {splits - pairs}/{splits} 個のタブで出ない",
               "kitty が古い。0.51 以降に上げる（claude-view は分割の木を読む）")


def read_kitty_conf(path, conf_dir, env, seen=None):
    """kitty.conf の (キー, 値) の列。include を辿る（相対パスは kitty の設定ディレクトリから）。"""
    seen = set() if seen is None else seen
    real = os.path.realpath(path)
    if real in seen:
        return []
    seen.add(real)
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, val = line.partition(" ")
        val = val.strip()
        if key == "include":
            target = os.path.expandvars(os.path.expanduser(val))
            if not os.path.isabs(target):
                target = os.path.join(conf_dir, target)
            import glob
            for p in sorted(glob.glob(target)) or [target]:
                out.extend(read_kitty_conf(p, conf_dir, env, seen))
        else:
            out.append((key, val))
    return out


def check_kitty_conf(ctx, rep):
    conf = ctx.kitty_dir / "kitty.conf"
    if not conf.exists():
        rep.ng("kitty.conf", f"無い（{conf}）", "tools/install.sh --kitty")
        return
    d = {}
    for k, v in read_kitty_conf(conf, str(ctx.kitty_dir), ctx.env):
        d.setdefault(k, []).append(v)       # watcher は何行でも足される。ほかは最後が効く
    arc = (d.get("allow_remote_control") or [""])[-1]
    lis = (d.get("listen_on") or [""])[-1]
    if arc in ("socket-only", "socket", "yes", "y", "true") and lis.startswith("unix:"):
        rep.ok("kitty.conf のリモート操作", f"allow_remote_control {arc}、listen_on {lis}")
    else:
        rep.ng("kitty.conf のリモート操作", f"allow_remote_control {arc or '（無い）'}、listen_on {lis or '（無い）'}",
               "tools/install.sh --kitty（kitty/claude-kitty.conf を include する）のあと kitty を起動し直す")
    watchers = [w for v in d.get("watcher", []) for w in v.split() if "claude_state_watcher" in w]
    if not watchers:
        rep.warn("kitty.conf の watcher", "claude_state_watcher.py が無い（完了の緑を戻すのは claude-state watch だけになる）",
                 "tools/install.sh --kitty")
    else:
        w = watchers[-1]
        p = w if os.path.isabs(w) else os.path.join(str(ctx.kitty_dir), w)
        if os.path.exists(p):
            rep.ok("kitty.conf の watcher", w)
        else:
            rep.ng("kitty.conf の watcher", f"{p} が無い（kitty が watcher を読めない）", "tools/install.sh --kitty（リンクを張る）")
    fn = d.get("filter_notification") or []
    if fn:
        rep.ok("kitty.conf の filter_notification", fn[-1])
    else:
        rep.warn("kitty.conf の filter_notification", "無い（入力待ちのたびにデスクトップ通知が出る）",
                 "tools/install.sh --kitty か、kitty.conf に filter_notification all")


# ---- python / node ------------------------------------------------------------

def check_runtimes(ctx, rep):
    v = ".".join(map(str, ctx.python))
    want = ".".join(map(str, MIN_PYTHON))
    if tuple(ctx.python[:2]) >= MIN_PYTHON:
        rep.ok("python3", v)
    else:
        fix = ("brew install python で 3.9 以降を入れ、PATH で /usr/bin より前にする" if ctx.os_name == "Darwin"
               else f"python3 を {want} 以降にする")
        rep.ng("python3", f"{v}（{want} より古い）", fix)
    if browser_off(ctx):  # node を使うのは claude-browser の CDP だけ
        rep.skip("node", "調べない（browser.enabled: false）")
        return
    node = ctx.which("node")
    if not node:
        rep.warn("node", "無い（claude-browser の shot などの CDP が使えない）", "Node.js 22 以降を入れる")
        return
    rc, out = ctx.run([node, "--version"])
    m = re.match(r"v(\d+)\.", (out or "").strip())
    if rc != 0 or not m:
        rep.warn("node", f"版を読めない（{node}）", "node --version が動くか確認する")
    elif int(m.group(1)) >= 22:
        rep.ok("node", out.strip())
    else:
        rep.warn("node", f"{out.strip()}（22 より古い。claude-browser の CDP は組み込みの WebSocket を使う）", "Node.js 22 以降にする")


# ---- links / hooks ------------------------------------------------------------

def commands(ctx):
    return sorted(p.name for p in (ctx.repo / "bin").iterdir()
                  if p.is_file() and os.access(p, os.X_OK) and not p.name.startswith("."))


def check_links(ctx, rep):
    missing, other = [], []
    for name in commands(ctx):
        dest = ctx.bin_dir / name
        if not os.path.lexists(dest):
            missing.append(name)
        elif os.path.realpath(dest) != os.path.realpath(ctx.repo / "bin" / name):
            other.append(name)
    total = len(commands(ctx))
    if not missing and not other:
        rep.ok("コマンドのリンク", f"{total} 個（{ctx.bin_dir}）")
    else:
        parts = ([f"無い: {', '.join(missing)}"] if missing else []) + ([f"別のファイル: {', '.join(other)}"] if other else [])
        rep.ng("コマンドのリンク", "、".join(parts),
               "tools/install.sh --links" + ("（別のファイルは自分で消してから）" if other else ""))
    path = ctx.env.get("PATH", "").split(os.pathsep)
    if str(ctx.bin_dir) not in path:
        rep.warn("PATH", f"{ctx.bin_dir} が PATH に無い", f'シェルの rc に export PATH="{ctx.bin_dir}:$PATH"')


def check_hooks(ctx, rep):
    import merge_hooks as M
    try:
        data = M.load(ctx.settings)
    except M.Broken as e:
        rep.ng("settings.json", str(e), "JSON を直す（tools/install.sh はこのままでは書き換えない）")
        return
    for part, (state, missing) in M.status(data).items():
        if part in ("relay", "model"):
            continue  # 使う人だけのもの。relay は check_relay、model は check_model_policy が調べる
        name = f"hook: {HOOK_LABELS[part]}"
        if state == "none":
            rep.warn(name, "入っていない", f"使うなら tools/install.sh --hooks={part}")
            continue
        details, fixes = [], []
        if state == "some":
            details.append(f"一部だけ（無い: {', '.join(missing)}）")
            fixes.append(f"tools/install.sh --hooks={part}")
        lost = hook_programs_missing(ctx, M, data, part)
        if lost:
            # 道具が見つからない hook は、Claude Code の中で黙って失敗する（色が変わらない・ガードが効かない）
            details.append(f"hook の道具が見つからない: {', '.join(lost)}")
            fixes.append("tools/install.sh --links でリンクを張り PATH に ~/.local/bin を入れるか、settings.json の command を"
                         f"このリポジトリの bin の絶対パス（{ctx.repo / 'bin'}/…）に書き直す")
        if details:
            rep.ng(name, "。".join(details), "。".join(fixes))
        else:
            rep.ok(name, "入っている")
        if part == "lang" and state != "none":
            from langcheck import load_lang_config
            cfg, _problems = load_lang_config(ctx.env)
            if cfg["enabled"] and not cfg["language"]:
                rep.warn(f"{name} の言語", "lang.language が決まっていない（または読めない）ので、hook は何もしない"
                         "（hook は Claude Code の環境で動くので、CLAUDE_LANG_CHECK_LANGUAGE で決めているならそちらが効く）",
                         "設定の lang.language に ja・zh・ko のどれかを書く（docs/config.md）")


def hook_programs_missing(ctx, M, data, part):
    """その部品の hook の command のうち、道具（最初の語）が絶対パスでも PATH でも見つからないもの。

    PATH は doctor を動かしたシェルのもの（Claude Code を別の環境から起動すると違うことがある。素の名前の hook が
    ここで見つかっても、Claude Code の PATH に無ければ失敗する）。"""
    lost = []
    for c in M.commands(data, part):
        prog = M.program(c)
        if not prog:
            continue
        p = prog.replace("${HOME}", str(ctx.home)).replace("$HOME", str(ctx.home))
        if p == "~" or p.startswith("~/"):
            p = str(ctx.home) + p[1:]       # 調べる環境の HOME（doctor を動かしたプロセスの HOME ではない）
        else:
            p = os.path.expanduser(p)       # ~user の形
        if "/" in p:
            ok = os.path.isfile(p) and os.access(p, os.X_OK)
        else:
            ok = bool(ctx.which(p))
        if not ok and prog not in lost:
            lost.append(prog)
    return lost


# ---- systemd / launchd ----------------------------------------------------------

def check_launchd(ctx, rep):
    """macOS: contrib/launchd の LaunchAgent が入っていて、launchctl に読み込まれているか。"""
    agent_dir = Path(ctx.env.get("CLAUDE_KITTY_LAUNCH_AGENTS") or ctx.home / "Library" / "LaunchAgents")
    lc = ctx.which("launchctl") or ("/bin/launchctl" if os.path.exists("/bin/launchctl") else None)
    uid = os.getuid()
    for label in AGENTS:
        name = f"launchd: {label}"
        if not (agent_dir / f"{label}.plist").exists():
            fix = "tools/install.sh --launchd"
            if label == "claude-kitty.state-watch":
                rep.warn(name, "入っていない（見た完了の緑が idle に戻らない・消えた色が塗り直されない）", fix)
            else:
                rep.warn(name, "入っていない", fix)
            continue
        if not lc:
            rep.warn(name, "launchctl が無いので読み込みを確認できない")
            continue
        rc, _out = ctx.run([lc, "print", f"gui/{uid}/{label}"])
        if rc == 0:
            rep.ok(name, "入っていて、読み込まれている")
        else:
            rep.ng(name, "入っているが、読み込まれていない",
                   f"launchctl bootstrap gui/{uid} {agent_dir / (label + '.plist')}（理由は ~/Library/Logs/claude-kitty/）")


def check_systemd(ctx, rep):
    if ctx.os_name == "Darwin":
        check_launchd(ctx, rep)
        return
    if ctx.os_name != "Linux":
        rep.skip("systemd", f"{ctx.os_name} では使わない")
        return
    sc = ctx.which("systemctl")
    if not sc:
        rep.skip("systemd", "systemctl が無い")
        return
    unit_dir = ctx.config_home / "systemd" / "user"
    for u in UNITS:
        if not (unit_dir / u).exists():
            rep.warn(f"systemd: {u}", "入っていない", "tools/install.sh --systemd")
            continue
        _rc, en = ctx.run([sc, "--user", "is-enabled", u])
        _rc, act = ctx.run([sc, "--user", "is-active", u])
        en, act = (en or "").strip(), (act or "").strip()
        if en != "enabled":
            rep.ng(f"systemd: {u}", f"enable されていない（{en or '?'}）", f"systemctl --user enable --now {u}")
        elif act != "active":
            rep.ng(f"systemd: {u}", f"動いていない（{act or '?'}）",
                   f"journalctl --user -u {u} で理由を見てから systemctl --user restart {u}")
        else:
            rep.ok(f"systemd: {u}", "enable 済み・動いている")


# ---- 設定 ---------------------------------------------------------------------

def check_config(ctx, rep):
    from config import (config_path, load_browser_config, load_kitty_config, load_roster_config, load_state_config,
                        load_view_config)
    path = config_path(ctx.env)
    if not path.exists():
        rep.ok("設定ファイル", f"無い（既定値で動く。例は config.example.json、説明は docs/config.md）: {path}")
        return
    fix = f"{path} を直す（docs/config.md）"

    def strict(name, fn):
        try:
            fn()
            rep.ok(f"設定: {name}", "読める")
        except ValueError as e:
            rep.ng(f"設定: {name}", str(e), fix)

    def lenient(name, fn):
        _out, problems = fn()
        if problems:
            rep.ng(f"設定: {name}", "; ".join(problems), fix)
        else:
            rep.ok(f"設定: {name}", "読める")
    strict("view", lambda: load_view_config(ctx.env, path))
    strict("state", lambda: load_state_config(ctx.env, path))
    from langcheck import load_lang_config
    lenient("lang", lambda: load_lang_config(ctx.env, path))
    from guardhook import load_guard_config
    lenient("guard_hook", lambda: load_guard_config(ctx.env, path))
    from sessionstats import load_settings
    strict("session_stats", lambda: load_settings(ctx.env, path))
    strict("roster", lambda: load_roster_config(ctx.env, path))
    strict("kitty", lambda: load_kitty_config(ctx.env, path))
    strict("browser", lambda: load_browser_config(ctx.env, path))
    if uses_relay(path):
        from config import load_relay_config
        strict("relay", lambda: load_relay_config(ctx.env, path))
    from config import load_model_policy
    strict("model_policy", lambda: load_model_policy(ctx.env, path))
    from config import load_disk_watch_config
    strict("disk_watch", lambda: load_disk_watch_config(ctx.env, path))
    from config import load_statusline_config
    strict("statusline", lambda: load_statusline_config(ctx.env, path))


# ---- デスクトップ・Chrome ------------------------------------------------------

def browser_off(ctx):
    """設定の browser.enabled が false か（壊れた設定は「設定: browser」の行で NG にするので、ここでは False）。"""
    from config import load_browser_config
    try:
        return not load_browser_config(ctx.env)["enabled"]
    except ValueError:
        return False


def check_browser(ctx, rep):
    """claude-browser と同じ探し方（lib/browser.py）で、起動する Chrome とプロファイルの置き場を出す。"""
    from browser import FLATPAK_CHROME, SEARCHED, find_browser
    from config import load_browser_config
    if browser_off(ctx):  # Chrome を探さない（flatpak info も呼ばない）
        rep.ok("browser", "オフ（browser.enabled: false）")
        return
    try:
        conf = load_browser_config(ctx.env)
    except ValueError:
        conf = {"enabled": True, "command": None, "profile_root": None}  # 壊れた設定は「設定: browser」の行で NG にする

    def flatpak_has(app):
        fp = ctx.which("flatpak")
        return bool(fp) and ctx.run([fp, "info", app], timeout=5)[0] == 0

    b = find_browser(conf, ctx.env, ctx.home, ctx.which, os.path.exists, flatpak_has, ctx.os_name)
    if b["command"] is None:
        rep.warn("Chrome", f"見つからない（探したもの: {SEARCHED}）",
                 "claude-browser を使うなら Chrome か Chromium を入れる（Flatpak なら flatpak install flathub "
                 f"{FLATPAK_CHROME}）。別の場所・別の名前なら設定の browser.command（docs/config.md）")
        return
    cmd = b["command"]
    if b["configured"] and not (ctx.which(cmd[0]) or os.path.exists(cmd[0])):
        rep.ng("Chrome", f"設定の browser.command の {cmd[0]} が見つからない", "browser.command を直す（docs/config.md）")
        return
    if b["kind"] == "flatpak":
        how = f"Flatpak の {b['name']}"
    elif b["kind"] == "snap":
        how = f"Snap の {b['name']}"
    else:
        how = cmd[0]
    note = ""
    if b["kind"] in ("flatpak", "snap"):  # 不具合ではないので ok のまま、開き方の違いを知らせる（lib/sandboxfs.py）
        note = "。サンドボックスなのでホームの一部しか見えない。見えない場所の file:// は、アプリから見える場所に写して開く"
    rep.ok("Chrome", f"{how}{'（設定の browser.command）' if b['configured'] else ''}、プロファイル {b['profile_root']}{note}")


def check_desktop(ctx, rep):
    if ctx.os_name == "Linux":
        from kwin import QDBUS_NAMES, qdbus_path
        q = qdbus_path(ctx.env, which=lambda n, path=None: ctx.which(n))
        rc = ctx.run([q, "org.kde.KWin", "/Scripting"], timeout=3)[0] if q else None
        if rc == 0:
            rep.ok("KWin", f"{os.path.basename(q)} で KWin のスクリプトを呼べる")
        else:
            why = "KDE Plasma の KWin が応答しない" if q else f"qdbus（{'・'.join(QDBUS_NAMES)}）が無い"
            rep.warn("KWin", f"使えない（{why}）",
                     "claude-browser の重ね置き・claude-kitty のデスクトップ移動・保護する窓（view.guard）の確認だけが"
                     "使えない（ほかは動く。窓の操作は KDE Plasma 専用）")
        check_browser(ctx, rep)
    elif ctx.os_name == "Darwin":
        rep.skip("KWin", "macOS では使わない（claude-browser の重ね置きは試験的に CoreGraphics と CDP で行う。"
                         "claude-kitty の最大化・デスクトップ移動はしない。SETUP.md の macOS の節）")
        lsappinfo = ctx.which("lsappinfo") or ("/usr/bin/lsappinfo" if os.path.exists("/usr/bin/lsappinfo") else None)
        if lsappinfo:
            rep.ok("前面のアプリ（view.guard）", f"{lsappinfo} で読める")
        else:
            rep.warn("前面のアプリ（view.guard）", "lsappinfo が無い（view.guard を設定すると、当たるプロセスが動いているだけで"
                     "並べ替えを控える）")
        check_browser(ctx, rep)
    else:
        rep.skip("KWin / Chrome", f"{ctx.os_name} では調べない")


# ---- relay（設定に relay の節があるときだけ） ------------------------------------------------

def uses_relay(path):
    try:
        data = json.loads(Path(path).read_text() or "{}")
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("relay") is not None


def check_sshd(ctx, rep):
    name = "relay: sshd"
    if ctx.os_name == "Linux":
        sc = ctx.which("systemctl")
        if not sc:
            rep.warn(name, "systemctl が無いので確認できない（受け取るには sshd が要る）")
            return
        for unit in ("sshd", "ssh", "sshd.socket", "ssh.socket"):  # Ubuntu などは socket で起動する
            rc, out = ctx.run([sc, "is-active", unit])
            if rc == 0 and (out or "").strip() == "active":
                rep.ok(name, f"{unit} が動いている")
                return
        rep.warn(name, "動いていない（このままでは受け取れない。送るだけなら要らない）",
                 "sudo systemctl enable --now sshd（Debian・Ubuntu は ssh。無ければ openssh-server を入れる）")
    elif ctx.os_name == "Darwin":
        # 「リモートログイン」の入り切りを確かめる systemsetup は root が要る。launchctl print で分かる範囲だけ見る
        lc = ctx.which("launchctl") or ("/bin/launchctl" if os.path.exists("/bin/launchctl") else None)
        rc = ctx.run([lc, "print", "system/com.openssh.sshd"])[0] if lc else None
        if rc == 0:
            rep.ok(name, "リモートログイン（com.openssh.sshd）が読み込まれている")
        else:
            rep.warn(name, "リモートログインが入っているか確認できない（受け取るには要る）",
                     "システム設定 → 一般 → 共有 → リモートログインを入れる（SETUP.md の「端末をまたぐ連絡」）")
    else:
        rep.skip(name, f"{ctx.os_name} では調べない")


def check_relay(ctx, rep):
    from config import config_path, load_relay_config
    if not uses_relay(config_path(ctx.env)):
        return  # relay を使っていない（行を出さない）
    import relay as R
    try:
        conf = load_relay_config(ctx.env)
    except ValueError:
        return  # 「設定: relay」の行で NG にする
    renv = dict(ctx.env, HOME=str(ctx.home))
    check_sshd(ctx, rep)
    key = R.key_path(renv)
    if not key.exists():
        if conf["peers"]:
            rep.ng("relay: 鍵", f"無い（{key}。送れない）", "claude-relay pair")
        else:
            rep.warn("relay: 鍵", f"無い（{key}。受け取るだけなら要らない）", "送るなら claude-relay pair")
    elif key.stat().st_mode & 0o077:
        rep.ng("relay: 鍵", f"ほかの人が読める（{key}）", f"chmod 600 {key}")
    else:
        rep.ok("relay: 鍵", str(key))
    kh = R.read_text_or_empty(R.known_hosts_path(renv))
    no_hk = [n for n, p in conf["peers"].items() if not R.kh_lines(kh, R.host_marker(p["host"], p["port"]))]
    if not conf["peers"]:
        rep.warn("relay: 送る相手", "relay.peers に誰もいない（送れない）", "相手の claude-relay pair の 1 行を claude-relay trust で")
    elif no_hk:
        rep.ng("relay: 送る相手", f"host key が無い: {', '.join(no_hk)}（送れない）",
               "相手の sshd が動いてから claude-relay trust をもう一度（host key を取る）")
    else:
        rep.ok("relay: 送る相手", f"{len(conf['peers'])} 人（{', '.join(sorted(conf['peers']))}）")
    ak = R.authorized_keys_path(renv)
    text = R.read_text_or_empty(ak)
    names = R.ak_names(text)
    if not names:
        rep.warn("relay: 受け取りの登録", f"{ak} に claude-relay の行が無い（誰からも受け取らない）",
                 "相手の claude-relay pair の 1 行を claude-relay trust で")
    else:
        bad = []
        for line in text.splitlines():
            if R.ak_is_ours(line):
                prog = (R.forced_command(line) or [None])[0]
                if not prog or not (os.path.isfile(prog) and os.access(prog, os.X_OK)):
                    bad.append(line.strip().split()[-1])
        problem = R.ssh_dir_problem(renv)
        if bad:
            rep.ng("relay: 受け取りの登録", f"強制コマンドの claude-relay が見つからない: {', '.join(bad)}",
                   "リポジトリを動かしたなら claude-relay trust をもう一度（同じ名前の行を置き換える）")
        elif problem:
            rep.ng("relay: 受け取りの登録", problem, "権限を直す")
        else:
            rep.ok("relay: 受け取りの登録", f"{len(names)} 人（{', '.join(names)}）。受け取りの役割: {', '.join(conf['receivers'])}"
                   + ("" if conf["enabled"] else "。ただし relay.enabled が false で、受け取りを止めている"))
    import merge_hooks as M
    try:
        data = M.load(ctx.settings)
    except M.Broken:
        return  # settings.json の行で NG にする
    state, missing = M.status(data)["relay"]
    if state == "all":
        rep.ok("hook: claude-relay", "入っている")
    elif state == "some" and missing == ["SessionStart"]:
        # 0.7.0 で入れたまま。渡すのと起こすのは UserPromptSubmit・Stop で足りる（始まったばかりのセッションを起こせないだけ）
        rep.warn("hook: claude-relay", "SessionStart が無い（始まってまだ 1 ターンもしていないセッションは起こせない）",
                 "tools/install.sh --hooks=relay")
    elif state == "some":
        rep.ng("hook: claude-relay", f"一部だけ（無い: {', '.join(missing)}）", "tools/install.sh --hooks=relay")
    else:
        rep.warn("hook: claude-relay", "入っていない（届いたものがセッションに渡らない）", "tools/install.sh --hooks=relay")


# ---- model_policy（離席中のサブエージェントのモデルの切り替え） ----------------------------------

def check_model_policy(ctx, rep):
    """有効か、hook（claude-presence の PreToolUse と、人の入力を記録する claude-state の UserPromptSubmit）が入っているか。"""
    from config import load_model_policy
    import merge_hooks as M
    name = "モデルの切り替え"
    try:
        pol = load_model_policy(ctx.env)
    except ValueError:
        return  # 「設定: model_policy」の行で NG にする
    try:
        data = M.load(ctx.settings)
    except M.Broken:
        return  # settings.json の行で NG にする
    st = M.status(data)
    hook = st["model"][0]
    lost = hook_programs_missing(ctx, M, data, "model") if hook != "none" else []
    if not pol["enabled"]:
        rep.ok(name, "無効（model_policy.enabled が false）" + ("。hook は入っているが何もしない" if hook != "none" else ""))
        return
    what = f"有効（{pol['away_minutes']:g} 分だれも打たなければ、実装のサブエージェントを {pol['away_model']} に）"
    if hook == "none":
        rep.ng(name, f"{what}。だが hook が入っていない（切り替わらない）", "tools/install.sh --hooks=model")
    elif lost:
        rep.ng(name, f"{what}。だが hook の道具が見つからない: {', '.join(lost)}", "tools/install.sh --links")
    elif "UserPromptSubmit" in st["state"][1]:
        rep.ng(name, f"{what}。だが claude-state の UserPromptSubmit の hook が無い（人が打った時刻を記録できず、切り替わらない）",
               "tools/install.sh --hooks=state")
    else:
        rep.ok(name, f"{what}。hook も入っている")


# ---- ディスクの見張り（claude-state watch の disk_watch） --------------------------------------------

def check_disk(ctx, rep):
    """各 path の容量と inode の使用率と、閾値を超えているか（statvfs を読むだけ。直下は数えない）。"""
    from config import load_disk_watch_config
    from diskwatch import KIND_LABEL, status_rows
    try:
        dc = load_disk_watch_config(ctx.env)
    except ValueError:
        return  # 「設定: disk_watch」の行で NG にする
    if not dc["enabled"]:
        rep.skip("ディスク", "見張らない（disk_watch.enabled が false）")
        return
    to = dc["notify_role"] or "デスクトップの通知だけ"
    by_path = {}
    for path, kind, used, total, pct, over in status_rows(ctx.env, dc, ctx.statvfs):
        by_path.setdefault(path, []).append((kind, used, total, pct, over))
    for path, items in by_path.items():
        name = f"ディスク: {path}"
        if items[0][0] is None:
            rep.skip(name, "無い（飛ばす）")
            continue
        parts, worst = [], None
        for kind, used, total, pct, over in items:
            parts.append(f"{KIND_LABEL[kind]} {pct:.0f}%" + (f"（{over:g}% を超えている）" if over is not None else ""))
            if over is not None:
                worst = over if worst is None else max(worst, over)
        if not any(k == "inodes" for k, *_ in items):
            parts.append("inode は数を持たないので見ない")
        detail = "・".join(parts) + f"。閾値 {', '.join(f'{t:g}' for t in dc['thresholds'])}%、知らせる先: {to}"
        if worst is None:
            rep.ok(name, detail)
        else:
            rep.warn(name, detail, f"{path} の中の要らないもの（テストの一時フォルダの残りなど）を確認して消す")


def check_bg_shells(ctx, rep):
    """各セッション（claude の pid）の子の背景のシェルのうち、2 時間を超えて続いているものを 1 行ずつ（読むだけ）。

    終わらない待ちのループ（until ! pgrep -f … など）が残ると、セッションの status が "shell" のままになり、
    claude-roster restart が断る。止めるかは人か担当が決める（ここでは止めない）。"""
    import active
    import bgshell
    dirs = []
    for d in (ctx.env.get("CLAUDE_CONFIG_DIR"), str(ctx.home / ".claude")):
        if d and d not in dirs:
            dirs.append(d)
    sessions = active.live_sessions([Path(d) / "sessions" for d in dirs])
    if not sessions:
        rep.skip("背景のシェル", "動いているセッションが無い")
        return
    sys_ = ctx.procs or bgshell.Sys
    kids = bgshell.child_map(sys_)
    found = 0
    for s in sessions:
        who = s.get("name") or str(s.get("sessionId", ""))[:8]
        for t in bgshell.long_shells(s["pid"], sys_=sys_, kids=kids):
            found += 1
            rep.warn(f"背景のシェル: {who}（claude pid {s['pid']}）", bgshell.brief(*t),
                     f"要らない待ちなら kill {t[0]}（終わらない待ちは claude-waitfor に書き換える）")
    if not found:
        rep.ok("背景のシェル", f"{bgshell.LONG_SECONDS // 3600} 時間を超えて続くものは無い（セッション {len(sessions)} 件）")


CHECKS = [check_kitty, check_kitty_conf, check_runtimes, check_links, check_hooks, check_systemd, check_config, check_desktop,
          check_relay, check_model_policy, check_disk, check_bg_shells]
MARK = {"ok": "OK  ", "ng": "NG  ", "warn": "注意", "skip": "--  "}


def run_checks(ctx):
    rep = Report()
    for c in CHECKS:
        try:
            c(ctx, rep)
        except Exception as e:  # noqa: BLE001  1 つの確認が落ちても残りは出す
            rep.ng(c.__name__.replace("check_", ""), f"調べる途中で失敗した: {type(e).__name__}: {e}",
                   "claude-kitty の不具合かもしれない（CONTRIBUTING.md の issue へ）")
    return rep


def main(argv=None, ctx=None, out=None):
    argv = sys.argv[1:] if argv is None else argv
    out = out or sys.stdout
    if argv not in ([], ["--json"]):
        print("usage: claude-kitty doctor [--json]", file=sys.stderr)
        return 2
    rep = run_checks(ctx or Ctx())
    if argv == ["--json"]:
        print(json.dumps({"ok": rep.problems == 0, "problems": rep.problems, "warnings": rep.warnings,
                          "checks": rep.items}, ensure_ascii=False, indent=1), file=out)
    else:
        for i in rep.items:
            line = f"{MARK[i['status']]} {i['name']}: {i['detail']}"
            if i["fix"] and i["status"] in ("ng", "warn"):
                line += f"  → {i['fix']}"
            print(line, file=out)
        print(f"\n問題 {rep.problems} 件、注意 {rep.warnings} 件" + ("" if rep.problems else "（主な道具は動く）"), file=out)
    return 1 if rep.problems else 0


if __name__ == "__main__":
    sys.exit(main())
