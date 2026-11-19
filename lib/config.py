"""利用者ごとの設定（役割名・プリセット・前面のときに並べ替えを控える窓）。副作用は読むことだけ。

優先順: コマンドの引数 > 環境変数 > 設定ファイル > 既定値。
設定ファイルは $CLAUDE_KITTY_CONFIG、無ければ $XDG_CONFIG_HOME/claude-kitty/config.json
（既定 ~/.config/claude-kitty/config.json）。リポジトリの外に置き、手元の役割名はそこに書く。

  {"view": {"left": "main", "restore": [["dev-a"], ["dev-b"]], "guard": ["^\\\\./fullscreen-app"]}}

restore は段のリストか "a,b/c" の形の文字列。guard は前面にあったら並べ替えない窓のプロセスの
正規表現（pgrep -f に渡す）。既定は空で、そのときは前面の窓を調べない（KWin を呼ばない）。
"""
import json
import os
import shutil
from pathlib import Path

from view import parse_rows

# 既定値は kitty/example.session に合わせる
DEFAULT_VIEW = {"left": "main", "restore": [["dev-a"], ["dev-b"]], "guard": []}


def config_path(env):
    if env.get("CLAUDE_KITTY_CONFIG"):
        return Path(env["CLAUDE_KITTY_CONFIG"])
    base = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else Path.home() / ".config"
    return base / "claude-kitty" / "config.json"


def _rows(value, where):
    """段の指定を [[str]] にそろえる。形が違えば ValueError（黙って既定に戻すと別の人を前に出すため）。"""
    rows = parse_rows(value) if isinstance(value, str) else value
    if (not isinstance(rows, list) or not rows
            or not all(isinstance(r, list) and r and all(isinstance(x, str) and x for x in r) for r in rows)):
        raise ValueError(f"{where} の段の指定が正しくない: {value!r}")
    return rows


def load_view_config(env, path=None):
    """claude-view の設定 {"left", "restore", "guard"} を返す。設定ファイルが壊れていれば ValueError。"""
    path = Path(path) if path else config_path(env)
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULT_VIEW.items()}
    try:
        text = path.read_text()
    except FileNotFoundError:
        text = None
    except OSError as e:
        raise ValueError(f"設定ファイルを読めない: {path}: {e}")
    if text is not None:
        try:
            data = json.loads(text)
        except ValueError as e:
            raise ValueError(f"設定ファイルが JSON として読めない: {path}: {e}")
        view = data.get("view", {}) if isinstance(data, dict) else None
        if not isinstance(view, dict):
            raise ValueError(f"設定ファイルの view がオブジェクトでない: {path}")
        if "left" in view:
            if not isinstance(view["left"], str) or not view["left"]:
                raise ValueError(f"{path} の view.left が正しくない: {view['left']!r}")
            out["left"] = view["left"]
        if "restore" in view:
            out["restore"] = _rows(view["restore"], f"{path} の view.restore")
        if "guard" in view:
            g = view["guard"]
            if isinstance(g, str):
                g = [g]
            if not isinstance(g, list) or not all(isinstance(x, str) and x for x in g):
                raise ValueError(f"{path} の view.guard が正しくない: {view['guard']!r}")
            out["guard"] = g
    if env.get("CLAUDE_VIEW_LEFT"):
        out["left"] = env["CLAUDE_VIEW_LEFT"]
    if env.get("CLAUDE_VIEW_RESTORE"):
        out["restore"] = _rows(env["CLAUDE_VIEW_RESTORE"], "CLAUDE_VIEW_RESTORE")
    # 空のまま export されただけで設定ファイルの guard（保護する窓の指定）が黙って外れないよう、止めるのは "none" のときだけ
    if env.get("CLAUDE_VIEW_GUARD"):
        out["guard"] = [] if env["CLAUDE_VIEW_GUARD"] == "none" else [env["CLAUDE_VIEW_GUARD"]]
    return out


def kitten_path(env, exists=os.path.exists, which=shutil.which):
    """kitten の場所: $CLAUDE_KITTY_KITTEN > 公式インストーラの置き場所 > PATH > macOS の kitty.app。"""
    if env.get("CLAUDE_KITTY_KITTEN"):
        return env["CLAUDE_KITTY_KITTEN"]
    for p in (os.path.expanduser("~/.local/kitty.app/bin/kitten"),):
        if exists(p):
            return p
    found = which("kitten")
    if found:
        return found
    mac = "/Applications/kitty.app/Contents/MacOS/kitten"
    return mac if exists(mac) else "kitten"


def kitty_conf_path(env):
    """kitty.conf の場所: $KITTY_CONFIG_DIRECTORY > $XDG_CONFIG_HOME/kitty > ~/.config/kitty。"""
    if env.get("KITTY_CONFIG_DIRECTORY"):
        return Path(env["KITTY_CONFIG_DIRECTORY"]) / "kitty.conf"
    base = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else Path.home() / ".config"
    return base / "kitty" / "kitty.conf"


def load_state_config(env, path=None):
    """claude-state の設定 {"colors", "notice", "base", "done_dwell", "watch_sockets", "left"} を返す。

      {"state": {"colors": {"waiting": {"background": "#2e2a22", "name": 208}},
                 "base_background": "#232627",
                 "notice": {"contains": ["返事は不要"], "prefix": ["【通知】"]}}}

    base_background を書かなければ kitty.conf の background（ペインの元の色）を使う。
    設定ファイルが壊れていれば ValueError（呼ぶ側が既定に戻すか決める）。
    """
    from state import DEFAULT_BASE, kitty_conf_background, merge_colors, merge_notice, merge_subagent_colors
    path = Path(path) if path else config_path(env)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as e:
        raise ValueError(f"設定ファイルを読めない: {path}: {e}")
    st = data.get("state", {}) if isinstance(data, dict) else None
    if not isinstance(st, dict):
        raise ValueError(f"設定ファイルの state がオブジェクトでない: {path}")
    base = st.get("base_background")
    if base is None:
        try:
            base = kitty_conf_background(kitty_conf_path(env).read_text())
        except OSError:
            base = None
    elif not (isinstance(base, str) and len(base) == 7 and base.startswith("#")):
        raise ValueError(f"{path} の state.base_background は #rrggbb: {base!r}")
    dwell = st.get("done_dwell_seconds", 2)
    if isinstance(dwell, bool) or not isinstance(dwell, (int, float)) or not 0 <= dwell <= 3600:
        raise ValueError(f"{path} の state.done_dwell_seconds は 0〜3600 の数: {dwell!r}")
    socks = st.get("watch_sockets")
    if socks is not None and not (isinstance(socks, list) and all(isinstance(x, str) and x for x in socks)):
        raise ValueError(f"{path} の state.watch_sockets は socket（unix:/…）の文字列の一覧: {socks!r}")
    view = data.get("view") if isinstance(data, dict) else None
    left = view.get("left") if isinstance(view, dict) and isinstance(view.get("left"), str) else DEFAULT_VIEW["left"]
    return {"colors": merge_colors(st.get("colors")), "notice": merge_notice(st.get("notice")), "base": base or DEFAULT_BASE,
            "done_dwell": float(dwell), "watch_sockets": socks or None, "subagent_colors": merge_subagent_colors(st.get("subagent_colors")),
            "left": env.get("CLAUDE_VIEW_LEFT") or left}
