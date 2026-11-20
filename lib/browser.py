"""claude-browser が起動する Chrome（Chromium）と、プロファイルの置き場を決める（claude-browser と doctor 共通）。

探す順（最初に見つかったもの）:
  1. 設定の browser.command（["google-chrome", "--ozone-platform=wayland"] など。そのまま使う）
  2. 下の 3〜6 のうち、入っていて、その置き場（プロファイルのフォルダ）が既にあるもの
     （後から別の Chrome を入れても、ログイン済みのプロファイルから黙って移らないように。0.4 までの
     Flatpak の Chrome の ~/.var/app/com.google.Chrome/claude-profiles もこれで引き継ぐ）
  3. ネイティブの Chrome（google-chrome-stable・google-chrome、macOS は Google Chrome.app）
  4. Flatpak の Chrome（com.google.Chrome）
  5. ネイティブの Chromium（chromium・chromium-browser）
  6. Flatpak の Chromium（org.chromium.Chromium）

プロファイルの置き場（設定の browser.profile_root が優先）:
  - Flatpak: ~/.var/app/<アプリ ID>/claude-profiles（サンドボックスから書ける場所。Chrome は 0.4 までと同じ）
  - Snap: ~/snap/<名前>/common/claude-kitty-profiles（隠しフォルダはサンドボックスから書けないため）
  - それ以外: $XDG_DATA_HOME/claude-kitty/browser-profiles（既定 ~/.local/share/claude-kitty/browser-profiles）。
    ネイティブの Chromium は chromium-profiles（Chrome とは cookie の鍵が違うので、同じプロファイルを共有しない）

副作用は読むことだけ（which・exists・flatpak_has は呼ぶ側が渡す。テストでは偽物にする）。
"""
import os
from pathlib import Path

FLATPAK_CHROME = "com.google.Chrome"
FLATPAK_CHROMIUM = "org.chromium.Chromium"
NATIVE_CHROME = ("google-chrome-stable", "google-chrome")
NATIVE_CHROMIUM = ("chromium", "chromium-browser")
MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
FLATPAK_PROFILE_DIR = "claude-profiles"
SNAP_PROFILE_DIR = "claude-kitty-profiles"


# flatpak run の、値を次の語に取るオプション（--branch beta など。--branch=beta の形は 1 語）
FLATPAK_VALUE_OPTS = {"--arch", "--branch", "--command", "--cwd", "--runtime", "--runtime-version",
                      "--filesystem", "--nofilesystem", "--env", "--unset-env", "--socket", "--device",
                      "--share", "--talk-name", "--own-name", "--system-talk-name", "--system-own-name",
                      "--persist", "--app-path", "--usr-path"}


def flatpak_app(argv):
    """`flatpak run [オプション] <アプリ ID> …` のアプリ ID。Flatpak でなければ None。"""
    if not argv or os.path.basename(argv[0]) != "flatpak":
        return None
    rest = list(argv[1:])
    if "run" not in rest:
        return None
    skip = False
    for a in rest[rest.index("run") + 1:]:
        if skip:
            skip = False
        elif a in FLATPAK_VALUE_OPTS:
            skip = True
        elif not a.startswith("-"):
            return a
    return None


def snap_name(path):
    """Snap のコマンド（/snap/bin/<名前>）なら名前。そうでなければ None。"""
    return os.path.basename(path) if path and str(path).startswith("/snap/") else None


def default_profile_root(argv, env, home, resolved=None, family="chrome"):
    """そのコマンドで起動する Chrome のプロファイルの置き場（設定の profile_root が無いとき）。

    resolved は argv[0] を PATH で探した結果（Snap の見分けに使う）。family は "chrome" か "chromium"。"""
    home = Path(home)
    app = flatpak_app(argv or [])
    if app:
        return home / ".var/app" / app / FLATPAK_PROFILE_DIR
    snap = snap_name(resolved or (argv[0] if argv else None))
    if snap:
        return home / "snap" / snap / "common" / SNAP_PROFILE_DIR
    base = Path(env["XDG_DATA_HOME"]) if env.get("XDG_DATA_HOME") else home / ".local/share"
    return base / "claude-kitty" / ("chromium-profiles" if family == "chromium" else "browser-profiles")


def find_browser(conf, env, home, which, exists, flatpak_has, os_name="Linux"):
    """{"command": argv か None, "kind": 種類, "name": 表示名, "profile_root": Path, "configured": bool} を返す。

    conf は load_browser_config の結果 {"command", "profile_root"}。見つからなければ command は None
    （profile_root は台帳や list のために、ネイティブの置き場を返す）。
    which(name) は PATH で探した場所か None、exists(path) は bool、flatpak_has(app_id) は入っているか。
    """
    home = Path(home)

    def out(argv, kind, name, resolved=None, configured=False, family="chrome"):
        root = conf.get("profile_root")
        root = Path(os.path.expanduser(root)) if root else default_profile_root(argv, env, home, resolved, family)
        return {"command": argv, "kind": kind, "name": name, "profile_root": root, "configured": configured}

    if conf.get("command"):
        argv = list(conf["command"])
        app = flatpak_app(argv)
        kind = "flatpak" if app else ("snap" if snap_name(which(argv[0]) or argv[0]) else "config")
        return out(argv, kind, app or argv[0], which(argv[0]), configured=True)

    def flatpak(app):
        return bool(which("flatpak")) and flatpak_has(app)

    def native(names, family):
        for name in names:
            p = which(name)
            if p:
                return out([p], "snap" if snap_name(p) else "native", name, p, family=family)
        if family == "chrome" and os_name == "Darwin" and exists(MAC_CHROME):
            return out([MAC_CHROME], "native", "Google Chrome.app")
        return None

    def flat(app, family):
        # 置き場は入っているかを調べなくても決まるので、flatpak info は要るときだけ呼ぶ
        return lambda need_root: (None if need_root and not exists(str(home / ".var/app" / app / FLATPAK_PROFILE_DIR))
                                  else out(["flatpak", "run", app], "flatpak", app, family=family) if flatpak(app) else None)

    def nat(names, family):
        def pick(need_root):
            b = native(names, family)
            return None if b is None or (need_root and not exists(str(b["profile_root"]))) else b
        return pick

    candidates = [nat(NATIVE_CHROME, "chrome"), flat(FLATPAK_CHROME, "chrome"),
                  nat(NATIVE_CHROMIUM, "chromium"), flat(FLATPAK_CHROMIUM, "chromium")]
    for need_root in (True, False):  # まず置き場が既にあるもの、無ければ順に
        for c in candidates:
            b = c(need_root)
            if b:
                return b
    return {"command": None, "kind": None, "name": None, "configured": False,
            "profile_root": Path(os.path.expanduser(conf["profile_root"])) if conf.get("profile_root")
            else default_profile_root(None, env, home)}


# 見つからなかったときに出す「探したもの」（設定の browser.command を書いていれば、探さずにそれを使う）
SEARCHED = ("・".join(NATIVE_CHROME) + f"、Flatpak の {FLATPAK_CHROME}、"
            + "・".join(NATIVE_CHROMIUM) + f"、Flatpak の {FLATPAK_CHROMIUM}")
