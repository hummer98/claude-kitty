#!/usr/bin/env python3
"""claude-kitty doctor: 入っているもの・動くものを 1 行ずつ調べ、足りなければ直し方を出す（読むだけ）。

  claude-kitty doctor [--json]

行の頭: OK（よい）・NG（問題。直し方を → の後に）・注意（無くても主な道具は動く）・--（この環境では調べない）。
終了コード: 0 = NG が無い、1 = NG がある。

kitty には `kitten @ ls`（読むだけ）しか送らない。systemd は is-enabled / is-active だけ。設定は各道具の読み込みの関数で読む。
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
HOOK_LABELS = {"state": "claude-state（状態の色）", "lang": "claude-lang-check", "guard": "claude-guard"}


class Ctx:
    """調べる環境。テストでは run / which / os_name / env を差し替える。"""

    def __init__(self, env=None, os_name=None, run=None, which=None, repo=REPO, python=None):
        self.env = dict(os.environ if env is None else env)
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
        rep.ng("kitty", f"版を読めない（{kitty}）", "kitty --version が動くか確かめる")
    else:
        rep.ok("kitty", f"{m.group(1)}（{kitty}）")
    kitten = find_kitten(ctx)
    socks = kitty_sockets(ctx)
    if not kitten:
        rep.ng("kitten @ ls", "kitten が見つからない", "kitty と一緒に入る。別の場所なら $CLAUDE_KITTY_KITTEN で指す")
        return
    if not socks:
        rep.warn("kitten @ ls", "動いている kitty のソケットが無い（確かめられない）",
                 "claude-kitty で kitty を起動してから、もう一度 claude-kitty doctor")
        rep.warn("layout_state.pairs", "kitty が動いていないので確かめられない")
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
        rep.ng("kitten @ ls", f"{len(failed)}/{len(socks)} の kitty で失敗（{failed[0]}）",
               "kitty.conf に allow_remote_control socket-only と listen_on unix:${XDG_RUNTIME_DIR}/kitty-{kitty_pid}"
               "（tools/install.sh --kitty）を入れ、kitty を起動し直す")
    else:
        rep.ok("kitten @ ls", f"{len(socks)} 個の kitty で通る")
    if splits == 0:
        if len(failed) < len(socks):
            rep.warn("layout_state.pairs", "splits のタブが無いので確かめられない",
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
    if tuple(ctx.python[:2]) >= (3, 10):
        rep.ok("python3", v)
    else:
        rep.ng("python3", f"{v}（3.10 より古い）", "python3 を 3.10 以降にする")
    node = ctx.which("node")
    if not node:
        rep.warn("node", "無い（claude-browser の shot などの CDP が使えない）", "Node.js 22 以降を入れる")
        return
    rc, out = ctx.run([node, "--version"])
    m = re.match(r"v(\d+)\.", (out or "").strip())
    if rc != 0 or not m:
        rep.warn("node", f"版を読めない（{node}）", "node --version が動くか確かめる")
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
        name = f"hook: {HOOK_LABELS[part]}"
        if state == "all":
            rep.ok(name, "入っている")
        elif state == "some":
            rep.ng(name, f"一部だけ（無い: {', '.join(missing)}）", f"tools/install.sh --hooks={part}")
        else:
            rep.warn(name, "入っていない", f"使うなら tools/install.sh --hooks={part}")
            continue
        if part == "lang" and state != "none":
            from langcheck import load_lang_config
            cfg, _problems = load_lang_config(ctx.env)
            if cfg["enabled"] and not cfg["language"]:
                rep.warn(f"{name} の言語", "lang.language が決まっていない（または読めない）ので、hook は何もしない"
                         "（hook は Claude Code の環境で動くので、CLAUDE_LANG_CHECK_LANGUAGE で決めているならそちらが効く）",
                         "設定の lang.language に ja・zh・ko のどれかを書く（docs/config.md）")


# ---- systemd ------------------------------------------------------------------

def check_systemd(ctx, rep):
    if ctx.os_name != "Linux":
        rep.skip("systemd", f"{ctx.os_name} では使わない（macOS は launchd。SETUP.md）")
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


# ---- デスクトップ・Chrome ------------------------------------------------------

def check_browser(ctx, rep):
    """claude-browser と同じ探し方（lib/browser.py）で、起動する Chrome とプロファイルの置き場を出す。"""
    from browser import FLATPAK_CHROME, SEARCHED, find_browser
    from config import load_browser_config
    try:
        conf = load_browser_config(ctx.env)
    except ValueError:
        conf = {"command": None, "profile_root": None}  # 壊れた設定は「設定: browser」の行で NG にする

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
    rep.ok("Chrome", f"{how}{'（設定の browser.command）' if b['configured'] else ''}、プロファイル {b['profile_root']}")


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
        rep.skip("KWin", "macOS では使わない")
        app = "/Applications/Google Chrome.app"
        if os.path.isdir(app) or os.path.isdir(str(ctx.home / app.lstrip("/"))):
            rep.ok("Chrome", app)
        else:
            rep.warn("Chrome", "Google Chrome.app が無い", "claude-browser を使うなら Chrome を入れる（窓の操作は SETUP.md の移植の手引き）")
    else:
        rep.skip("KWin / Chrome", f"{ctx.os_name} では調べない")


CHECKS = [check_kitty, check_kitty_conf, check_runtimes, check_links, check_hooks, check_systemd, check_config, check_desktop]
MARK = {"ok": "OK  ", "ng": "NG  ", "warn": "注意", "skip": "--  "}


def run_checks(ctx):
    rep = Report()
    for c in CHECKS:
        try:
            c(ctx, rep)
        except Exception as e:  # noqa: BLE001  1 つの確かめが落ちても残りは出す
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
