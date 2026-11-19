"""利用者ごとの設定（役割名・プリセット・前面のときに並べ替えを控える窓）。副作用は読むことだけ。

優先順: コマンドの引数 > 環境変数 > 設定ファイル > 既定値。
設定ファイルは $CLAUDE_KITTY_CONFIG、無ければ $XDG_CONFIG_HOME/claude-kitty/config.json
（既定 ~/.config/claude-kitty/config.json）。リポジトリの外に置き、手元の役割名はそこに書く。

  {"view": {"left": "main", "restore": [["dev-a"], ["dev-b"]], "guard": ["^\\\\./SomeGame"]}}

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
    # 空のまま export されただけで設定ファイルの guard（ゲームの保護など）が黙って外れないよう、止めるのは "none" のときだけ
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
