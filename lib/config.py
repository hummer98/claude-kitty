"""利用者ごとの設定（役割名・プリセット・前面のときに並べ替えを控える窓）。副作用は読むことだけ。

優先順: コマンドの引数 > 環境変数 > 設定ファイル > 既定値。
設定ファイルは $CLAUDE_KITTY_CONFIG、無ければ $XDG_CONFIG_HOME/claude-kitty/config.json
（既定 ~/.config/claude-kitty/config.json）。リポジトリの外に置き、手元の役割名はそこに書く。

  {"view": {"left": "main", "pinned": ["ops"], "restore": [["dev-a"], ["dev-b"]], "guard": ["^\\\\./fullscreen-app"]}}

pinned は left と同じく常時表示にする役割（右側の 1 段目の先頭に固定）。restore は段のリストか "a,b/c" の形の文字列。guard は前面にあったら並べ替えない窓のプロセスの
正規表現（pgrep -f に渡す）。既定は空で、そのときは前面の窓を調べない（KWin を呼ばない）。
playtest_guard は claude-show が「人がテストプレイ中か」を見る claude-guard のガードの名前（既定 null = 見ない）。
"""
import json
import os
import shutil
from pathlib import Path

from view import parse_rows

# 既定値は kitty/example.session に合わせる
DEFAULT_VIEW = {"left": "main", "pinned": [], "restore": [["dev-a"], ["dev-b"]], "guard": []}


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
    """claude-view の設定 {"left", "pinned", "restore", "guard"} を返す。設定ファイルが壊れていれば ValueError。"""
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
        if "pinned" in view:
            pins = view["pinned"]
            if isinstance(pins, str):
                pins = [pins]
            # 区切り（, /）を含む名前は claude-view rows の段組みに入れられない
            if not isinstance(pins, list) or not all(isinstance(x, str) and x and not set(x) & set(",/ ") for x in pins):
                raise ValueError(f"{path} の view.pinned が正しくない: {view['pinned']!r}")
            out["pinned"] = list(dict.fromkeys(pins))
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


def load_playtest_guard(env, path=None):
    """view.playtest_guard（claude-show が「人がテストプレイ中か」を見る claude-guard のガードの名前）か None。
    既定は None（見ない = 今までどおり）。値が文字列でなければ ValueError。
    設定ファイルそのものが読めない・壊れているときは None（load_view_config がその誤りを返すので、ここでは重ねない。
    テストプレイ中でもないのに「テストプレイ中」と断らないため）。"""
    path = Path(path) if path else config_path(env)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    view = data.get("view", {}) if isinstance(data, dict) else None
    if not isinstance(view, dict):
        return None
    name = view.get("playtest_guard")
    if name is None:
        return None
    if not isinstance(name, str) or not name:
        raise ValueError(f"{path} の view.playtest_guard はガードの名前（文字列）か null: {name!r}")
    return name


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

    notice の既定は空（上は例）。

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


DEFAULT_ROSTER = {"models": {}, "ignore": [], "generations": 5, "keep_absent_hours": 24, "claude": {}}


def _plain(v):
    """空でない文字列で、制御文字を含まない。"""
    return isinstance(v, str) and bool(v) and not any(ord(c) < 32 or ord(c) == 127 for c in v)


def load_roster_config(env, path=None):
    """claude-roster の設定 {"models", "ignore", "generations", "keep_absent_hours", "claude"} を返す（無ければ既定）。

    claude はフォルダ → claude の代わりに claude-wait が開くコマンド（CLAUDE_WAIT_CLAUDE。プロジェクトごとの包み）。

    壊れていれば ValueError（別のモデルで開き直さないよう、呼ぶ側は既定に戻さずに止める）。"""
    path = Path(path) if path else config_path(env)
    out = {k: (type(v)(v) if isinstance(v, (dict, list)) else v) for k, v in DEFAULT_ROSTER.items()}
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return out
    except (OSError, ValueError) as e:
        raise ValueError(f"設定ファイルを読めない: {path}: {e}")
    r = data.get("roster", {}) if isinstance(data, dict) else {}
    if not isinstance(r, dict):
        raise ValueError(f"{path} の roster がオブジェクトでない")
    if not isinstance(r.get("models", {}), dict) or not all(isinstance(v, str) for v in r.get("models", {}).values()):
        raise ValueError(f"{path} の roster.models は 役割→モデル名")
    if not isinstance(r.get("ignore", []), list):
        raise ValueError(f"{path} の roster.ignore は役割のリスト")
    c = r.get("claude", {})
    if not isinstance(c, dict) or not all(_plain(k) and _plain(v) and not any(ch.isspace() for ch in v)
                                          for k, v in c.items()):
        raise ValueError(f"{path} の roster.claude は フォルダ→引数なしの実行ファイル（空白・制御文字は使えない）")
    g = r.get("generations", 5)
    h = r.get("keep_absent_hours", 24)
    if isinstance(g, bool) or not isinstance(g, int) or not 1 <= g <= 50:
        raise ValueError(f"{path} の roster.generations は 1〜50")
    if isinstance(h, bool) or not isinstance(h, (int, float)) or h < 0:
        raise ValueError(f"{path} の roster.keep_absent_hours は 0 以上の数")
    out.update(models=r.get("models", {}), ignore=r.get("ignore", []), generations=g, keep_absent_hours=h, claude=c)
    return out


DEFAULT_BROWSER = {"enabled": True, "command": None, "profile_root": None}


def load_browser_config(env, path=None):
    """claude-browser の設定 {"enabled", "command", "profile_root"} を返す（無ければ既定: 使う・どちらも null で自動で探す）。

      {"browser": {"command": ["flatpak", "run", "com.google.Chrome"], "profile_root": "~/chrome-profiles"}}

    enabled が false なら claude-browser は Chrome を起動・操作せず、claude-show は OS の既定のアプリで開く
    （ブラウザの使い方を自分で決めている人の邪魔をしないため）。
    command は起動のコマンド（文字列のリスト。引数を足してもよい）。探す順と置き場の既定は lib/browser.py。
    壊れていれば ValueError（別のブラウザ・別のプロファイルで黙って開かないよう、呼ぶ側は止める）。"""
    path = Path(path) if path else config_path(env)
    out = dict(DEFAULT_BROWSER)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return out
    except (OSError, ValueError) as e:
        raise ValueError(f"設定ファイルを読めない: {path}: {e}")
    b = data.get("browser", {}) if isinstance(data, dict) else {}
    if not isinstance(b, dict):
        raise ValueError(f"{path} の browser がオブジェクトでない")
    e = b.get("enabled", True)
    if not isinstance(e, bool):  # "false" や 0 を黙って「使う」「使わない」のどちらかに読み替えない
        raise ValueError(f"{path} の browser.enabled は true / false: {e!r}")
    c = b.get("command")
    if c is not None and not (isinstance(c, list) and c and all(isinstance(x, str) and x for x in c)):
        raise ValueError(f"{path} の browser.command は空でない文字列のリスト（[\"google-chrome\"] など）か null: {c!r}")
    r = b.get("profile_root")
    if r is not None and not (isinstance(r, str) and r):
        raise ValueError(f"{path} の browser.profile_root はフォルダのパスの文字列か null: {r!r}")
    out.update(enabled=e, command=list(c) if c else None, profile_root=r)
    return out


DEFAULT_KITTY = {"desktop": None, "maximize": False}


def load_kitty_config(env, path=None):
    """claude-kitty の起動の設定 {"desktop", "maximize"} を返す（無ければ既定: 移さない・最大化しない）。

      {"kitty": {"desktop": 2, "maximize": true}}

    desktop は KDE Plasma の仮想デスクトップの番号（1 から）。壊れていれば ValueError。"""
    path = Path(path) if path else config_path(env)
    out = dict(DEFAULT_KITTY)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return out
    except (OSError, ValueError) as e:
        raise ValueError(f"設定ファイルを読めない: {path}: {e}")
    k = data.get("kitty", {}) if isinstance(data, dict) else {}
    if not isinstance(k, dict):
        raise ValueError(f"{path} の kitty がオブジェクトでない")
    d = k.get("desktop")
    if d is not None and (isinstance(d, bool) or not isinstance(d, int) or not 1 <= d <= 64):
        raise ValueError(f"{path} の kitty.desktop は 1〜64 の番号か null: {d!r}")
    m = k.get("maximize", False)
    if not isinstance(m, bool):
        raise ValueError(f"{path} の kitty.maximize は true / false: {m!r}")
    out.update(desktop=d, maximize=m)
    return out


DEFAULT_RELAY = {"enabled": True, "name": None, "receivers": ["left"], "max_bytes": 16384,
                 "rate": {"count": 30, "per_seconds": 600}, "peers": {}}
# 終わりは \Z（$ は末尾の改行の前でも当たり、authorized_keys に行を足されうるため）
RELAY_NAME_CORE = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
RELAY_NAME = rf"^{RELAY_NAME_CORE}\Z"                # 端末名・役割名（ファイル名・authorized_keys・ssh の引数に入る）
RELAY_HOST = r"^[A-Za-z0-9_][A-Za-z0-9._:%-]{0,252}\Z"  # 頭に - を許さない（ssh のオプションと取り違えない）
RELAY_USER = r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}\Z"


def load_relay_config(env, path=None):
    """claude-relay の設定 {"enabled", "name", "receivers", "max_bytes", "rate", "peers", "present"} を返す。

      {"relay": {"name": "linux-desk", "receivers": ["left"],
                 "peers": {"mac-a": {"host": "mac-a.local", "user": "taro", "port": 22}}}}

    receivers の "left" は設定ファイルの view.left の役割（既定 main。$CLAUDE_VIEW_LEFT は見ない）を指す
    （返す値では役割名に置き換え、重なりを除く）。
    present は設定ファイルに relay の節があるか（doctor が relay を調べるか決める）。
    壊れていれば ValueError（受け取る側は拒否し、送る側は止める。黙って別の相手に送らないため）。"""
    import re
    path = Path(path) if path else config_path(env)
    out = json.loads(json.dumps(DEFAULT_RELAY))
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as e:
        raise ValueError(f"設定ファイルを読めない: {path}: {e}")
    r = data.get("relay", {}) if isinstance(data, dict) else {}
    if not isinstance(r, dict):
        raise ValueError(f"{path} の relay がオブジェクトでない")
    out["present"] = isinstance(data, dict) and "relay" in data
    e = r.get("enabled", True)
    if not isinstance(e, bool):
        raise ValueError(f"{path} の relay.enabled は true / false: {e!r}")
    n = r.get("name")
    if n is not None and not (isinstance(n, str) and re.match(RELAY_NAME, n)):
        raise ValueError(f"{path} の relay.name は英数字と . _ - の名前（64 字まで）か null: {n!r}")
    rc = r.get("receivers", ["left"])
    if isinstance(rc, str):
        rc = [rc]
    if not (isinstance(rc, list) and all(isinstance(x, str) and re.match(RELAY_NAME, x) for x in rc)):
        raise ValueError(f"{path} の relay.receivers は役割名のリスト: {rc!r}")
    mb = r.get("max_bytes", DEFAULT_RELAY["max_bytes"])
    if isinstance(mb, bool) or not isinstance(mb, int) or not 1 <= mb <= 1024 * 1024:
        raise ValueError(f"{path} の relay.max_bytes は 1〜1048576 の整数: {mb!r}")
    rate = r.get("rate", DEFAULT_RELAY["rate"])
    if not isinstance(rate, dict):
        raise ValueError(f"{path} の relay.rate は {{\"count\": N, \"per_seconds\": S}}: {rate!r}")
    cnt = rate.get("count", DEFAULT_RELAY["rate"]["count"])
    per = rate.get("per_seconds", DEFAULT_RELAY["rate"]["per_seconds"])
    if isinstance(cnt, bool) or not isinstance(cnt, int) or cnt < 0:
        raise ValueError(f"{path} の relay.rate.count は 0 以上の整数: {cnt!r}")
    if isinstance(per, bool) or not isinstance(per, (int, float)) or not 0 < per <= 7 * 86400:
        raise ValueError(f"{path} の relay.rate.per_seconds は 0 より大きく 604800 までの数: {per!r}")
    peers = r.get("peers", {})
    if not isinstance(peers, dict):
        raise ValueError(f"{path} の relay.peers は 名前 → {{host, user, port}}")
    clean = {}
    for name, p in peers.items():
        where = f"{path} の relay.peers.{name}"
        if not re.match(RELAY_NAME, name):
            raise ValueError(f"{where}: 名前は英数字と . _ - （64 字まで）")
        if not isinstance(p, dict):
            raise ValueError(f"{where} がオブジェクトでない")
        host, user, port = p.get("host"), p.get("user"), p.get("port", 22)
        if not (isinstance(host, str) and re.match(RELAY_HOST, host)):
            raise ValueError(f"{where}.host が正しくない: {host!r}")
        if not (isinstance(user, str) and re.match(RELAY_USER, user)):
            raise ValueError(f"{where}.user が正しくない: {user!r}")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(f"{where}.port は 1〜65535: {port!r}")
        clean[name] = {"host": host, "user": user, "port": port}
    # "left" は設定ファイルの view.left だけで決める（$CLAUDE_VIEW_LEFT は見ない）。recv は sshd から動き、
    # kitty の中のシェルの環境変数を持たないので、hook と recv で受け取りの役割が食い違わないように
    view = data.get("view") if isinstance(data, dict) else None
    left = (view.get("left") if isinstance(view, dict) and isinstance(view.get("left"), str) and view.get("left")
            else DEFAULT_VIEW["left"])
    roles = list(dict.fromkeys(left if x == "left" else x for x in rc))
    out.update(enabled=e, name=n, receivers=roles, max_bytes=mb, rate={"count": cnt, "per_seconds": per}, peers=clean)
    return out


DEFAULT_DISK_WATCH = {"enabled": True, "paths": ["/tmp"], "thresholds": [70, 85], "notify_role": None, "interval_sec": 60}


def load_disk_watch_config(env, path=None):
    """claude-state watch のディスクの見張りの設定 {"enabled", "paths", "thresholds", "notify_role", "interval_sec"} を返す
    （無ければ既定: /tmp を 60 秒ごと、70 % と 85 %、送り先の役割は無し = デスクトップの通知だけ）。

      {"disk_watch": {"paths": ["/tmp", "$XDG_RUNTIME_DIR", "~/.cache/cc-tmp"], "notify_role": "main"}}

    paths の ~ と $VAR は使うときに展開する（無い path は飛ばす）。thresholds は 20〜100 の数で、小さい順・重なり無しに
    そろえる。壊れていれば ValueError（watch は見張りだけを止めて errors.log に残す）。"""
    import re
    path = Path(path) if path else config_path(env)
    out = json.loads(json.dumps(DEFAULT_DISK_WATCH))
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return out
    except (OSError, ValueError) as e:
        raise ValueError(f"設定ファイルを読めない: {path}: {e}")
    d = data.get("disk_watch", {}) if isinstance(data, dict) else {}
    if d is None:
        d = {}  # "disk_watch": null は書いていないのと同じ（既定で見張る）
    if not isinstance(d, dict):
        raise ValueError(f"{path} の disk_watch がオブジェクトでない")
    unknown = sorted(set(d) - set(DEFAULT_DISK_WATCH))
    if unknown:  # "threshold" などの書き間違いで、黙って既定のまま動かない
        raise ValueError(f"{path} の disk_watch に知らない項目: {', '.join(unknown)}")
    e = d.get("enabled", True)
    if not isinstance(e, bool):
        raise ValueError(f"{path} の disk_watch.enabled は true / false: {e!r}")
    ps = d.get("paths", DEFAULT_DISK_WATCH["paths"])
    if not (isinstance(ps, list) and all(isinstance(x, str) and x for x in ps)):
        raise ValueError(f"{path} の disk_watch.paths は空でない文字列のリスト（[\"/tmp\"] など）: {ps!r}")
    ts = d.get("thresholds", DEFAULT_DISK_WATCH["thresholds"])
    if not (isinstance(ts, list) and ts
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) and 20 <= x <= 100 for x in ts)):
        # 20 より下は、10 ポイント下がるのを待つ戻りの幅に対して小さすぎる（10 以下は二度と知らせなくなる）
        raise ValueError(f"{path} の disk_watch.thresholds は 20〜100 の数のリスト（[70, 85] など）: {ts!r}")
    role = d.get("notify_role")
    if role is not None and not (isinstance(role, str) and re.match(RELAY_NAME, role)):
        raise ValueError(f"{path} の disk_watch.notify_role は役割名（英数字と . _ -、64 字まで）か null: {role!r}")
    iv = d.get("interval_sec", DEFAULT_DISK_WATCH["interval_sec"])
    if isinstance(iv, bool) or not isinstance(iv, (int, float)) or not 5 <= iv <= 86400:
        raise ValueError(f"{path} の disk_watch.interval_sec は 5〜86400 の数（秒）: {iv!r}")
    out.update(enabled=e, paths=list(dict.fromkeys(ps)), thresholds=sorted(set(ts)), notify_role=role,
               interval_sec=float(iv))
    return out


# 会話の長さは淡い黄・くすんだ橙にして、週の使用量（鮮やかな橙 208・赤 196）や待受中の名前（208）と見分ける
DEFAULT_STATUSLINE = {"context": True, "context_thresholds": [200000, 400000], "context_colors": [186, 173],
                      "weekly_roles": ["left"], "weekly_thresholds": [85, 95], "weekly_colors": [208, 196],
                      "waits": True, "usage_file": True, "usage_warn_percent": 96}
USAGE_WINDOWS = ("seven_day", "five_hour")


def _color_steps(sl, key, lo, hi, path):
    """しきい値（小さい順・重なり無し）と色（0〜255、同じ数）の組。形が違えば ValueError。"""
    ts, cs = sl.get(f"{key}_thresholds", DEFAULT_STATUSLINE[f"{key}_thresholds"]), sl.get(f"{key}_colors", DEFAULT_STATUSLINE[f"{key}_colors"])
    if not (isinstance(ts, list) and all(isinstance(x, (int, float)) and not isinstance(x, bool) and lo <= x <= hi for x in ts)
            and all(a < b for a, b in zip(ts, ts[1:]))):
        raise ValueError(f"{path} の statusline.{key}_thresholds は {lo}〜{hi} の数の、小さい順のリスト: {ts!r}")
    if not (isinstance(cs, list) and all(isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 255 for x in cs)):
        raise ValueError(f"{path} の statusline.{key}_colors は 0〜255 の数のリスト: {cs!r}")
    if len(cs) != len(ts):  # 数が違うと、どのしきい値の色か決まらない
        raise ValueError(f"{path} の statusline.{key}_colors は {key}_thresholds と同じ数: {len(cs)} と {len(ts)}")
    return list(ts), list(cs)


def load_statusline_config(env, path=None):
    """claude-statusline の設定 {"context", "context_thresholds", "context_colors", "weekly_roles",
    "weekly_thresholds", "weekly_colors", "waits", "usage_file", "usage_warn_percent"} を返す（weekly_roles の "left" は view.left に置き換える）。

      {"statusline": {"weekly_roles": ["left", "ops"], "context_thresholds": [150000, 300000]}}

    weekly_roles は週の使用量を出す役割（セッション名か claude-state の記録の role）。"*" は全員、[] は誰にも出さない。
    壊れていれば ValueError（claude-statusline は既定で出し、行の末尾に「設定の誤り」と添える）。"""
    path = Path(path) if path else config_path(env)
    out = json.loads(json.dumps(DEFAULT_STATUSLINE))
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as e:
        raise ValueError(f"設定ファイルを読めない: {path}: {e}")
    sl = data.get("statusline", {}) if isinstance(data, dict) else {}
    if sl is None:
        sl = {}
    if not isinstance(sl, dict):
        raise ValueError(f"{path} の statusline がオブジェクトでない")
    unknown = sorted(set(sl) - set(DEFAULT_STATUSLINE))
    if unknown:  # "weekly_role" などの書き間違いで、黙って既定のまま動かない
        raise ValueError(f"{path} の statusline に知らない項目: {', '.join(unknown)}")
    c = sl.get("context", True)
    if not isinstance(c, bool):
        raise ValueError(f"{path} の statusline.context は true / false: {c!r}")
    roles = sl.get("weekly_roles", DEFAULT_STATUSLINE["weekly_roles"])
    if not (isinstance(roles, list) and all(isinstance(x, str) and x for x in roles)):
        raise ValueError(f"{path} の statusline.weekly_roles は役割名のリスト（\"*\" は全員、[] は出さない）: {roles!r}")
    ct, cc = _color_steps(sl, "context", 1, 10 ** 9, path)
    wt, wc = _color_steps(sl, "weekly", 0, 100, path)
    view = data.get("view") if isinstance(data, dict) else None
    left = env.get("CLAUDE_VIEW_LEFT") or (view.get("left") if isinstance(view, dict) and isinstance(view.get("left"), str)
                                           and view.get("left") else DEFAULT_VIEW["left"])
    wa = sl.get("waits", True)
    if not isinstance(wa, bool):
        raise ValueError(f"{path} の statusline.waits は true / false: {wa!r}")
    uf = sl.get("usage_file", True)
    # 相対パスは Claude Code を起動した場所で変わり、どこに書いたか分からなくなるので、絶対パスか ~ だけ
    if not (isinstance(uf, bool) or uf is None
            or (isinstance(uf, str) and (uf.startswith("~/") or os.path.isabs(uf)) and uf.rstrip("/") not in ("~", ""))):
        raise ValueError(f"{path} の statusline.usage_file は絶対パスか ~/…、true（既定の置き場）、false / null（書かない）: {uf!r}")
    wp = sl.get("usage_warn_percent", DEFAULT_STATUSLINE["usage_warn_percent"])

    def pct_ok(x):
        return isinstance(x, (int, float)) and not isinstance(x, bool) and 0 <= x <= 100
    # 窓ごとの {"seven_day": 96, "five_hour": 90} は書いた窓だけ変える（書かない窓は既定のまま）
    if not (pct_ok(wp) or (isinstance(wp, dict) and set(wp) <= set(USAGE_WINDOWS) and all(map(pct_ok, wp.values())))):
        raise ValueError(f"{path} の statusline.usage_warn_percent は 0〜100 の数か、"
                         f"{{\"seven_day\": 96, \"five_hour\": 90}} の形: {wp!r}")
    out.update(usage_warn_percent=dict(wp) if isinstance(wp, dict) else wp, usage_file=uf, context=c, context_thresholds=ct, context_colors=cc, weekly_thresholds=wt, weekly_colors=wc, waits=wa,
               weekly_roles=list(dict.fromkeys(left if x == "left" else x for x in roles)))
    return out


DEFAULT_WAITS = {"enabled": True, "poll_seconds": 60, "master_role": None, "catchup_hours": 12,
                 "claude": ["claude", "-p"], "prompt_timeout_seconds": 1800, "cmd_timeout_seconds": 60,
                 "body_max": 1000, "keep_days": 7}


def _num_in(v, lo, hi):
    return not isinstance(v, bool) and isinstance(v, (int, float)) and lo <= v <= hi


def load_waits_config(env, path=None):
    """claude-waits（待ちと定例の台帳）の設定 {"enabled", "poll_seconds", "master_role", "catchup_hours", "claude",
    "prompt_timeout_seconds", "cmd_timeout_seconds", "body_max", "keep_days"} を返す。

      {"waits": {"poll_seconds": 30, "master_role": "main", "catchup_hours": 6}}

    master_role が null なら設定ファイルの view.left（無ければ main。$CLAUDE_VIEW_LEFT は見ない: 常駐は kitty の外の
    systemd / launchd から動き、hook と常駐で統括が食い違わないように。relay.receivers の "left" と同じ決め方）。
    壊れていれば ValueError（常駐はその回を飛ばして errors.log に残し、add は止める）。"""
    import re
    path = Path(path) if path else config_path(env)
    out = json.loads(json.dumps(DEFAULT_WAITS))
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as e:
        raise ValueError(f"設定ファイルを読めない: {path}: {e}")
    w = data.get("waits", {}) if isinstance(data, dict) else {}
    if w is None:
        w = {}
    if not isinstance(w, dict):
        raise ValueError(f"{path} の waits がオブジェクトでない")
    unknown = sorted(set(w) - set(DEFAULT_WAITS))
    if unknown:  # 書き間違いで、黙って既定のまま動かない
        raise ValueError(f"{path} の waits に知らない項目: {', '.join(unknown)}")
    e = w.get("enabled", True)
    if not isinstance(e, bool):
        raise ValueError(f"{path} の waits.enabled は true / false: {e!r}")
    checks = {"poll_seconds": (5, 3600), "catchup_hours": (0, 168), "prompt_timeout_seconds": (10, 6 * 3600),
              "cmd_timeout_seconds": (1, 600), "body_max": (100, 8000), "keep_days": (0, 365)}
    for k, (lo, hi) in checks.items():
        v = w.get(k, DEFAULT_WAITS[k])
        if not _num_in(v, lo, hi):
            raise ValueError(f"{path} の waits.{k} は {lo}〜{hi} の数: {v!r}")
        out[k] = v
    m = w.get("master_role")
    if m is not None and not (isinstance(m, str) and re.match(RELAY_NAME, m)):
        raise ValueError(f"{path} の waits.master_role は役割名（英数字と . _ -、64 字まで）か null: {m!r}")
    c = w.get("claude", DEFAULT_WAITS["claude"])
    if isinstance(c, str):
        c = [c]
    if not (isinstance(c, list) and c and all(isinstance(x, str) and x for x in c)):
        raise ValueError(f"{path} の waits.claude はコマンドと引数のリスト（[\"claude\", \"-p\"] など）: {c!r}")
    view = data.get("view") if isinstance(data, dict) else None
    left = (view.get("left") if isinstance(view, dict) and isinstance(view.get("left"), str) and view.get("left")
            else DEFAULT_VIEW["left"])
    out.update(enabled=e, master_role=m or left, claude=list(c), body_max=int(out["body_max"]))
    return out


MODEL_NAME = r"^[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,63}$"


def load_model_policy(env, path=None):
    """離席中のサブエージェントのモデルの切り替え（claude-presence）の設定 {"enabled", "away_minutes", "away_model", "keep"}。

      {"model_policy": {"enabled": true, "away_minutes": 20, "away_model": "haiku", "keep": ["review", "レビュー"]}}

    既定は無効（enabled: false）。壊れていれば ValueError（hook は書き換えずに通す）。"""
    import re
    from presence import DEFAULT_MODEL_POLICY
    path = Path(path) if path else config_path(env)
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULT_MODEL_POLICY.items()}
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return out
    except (OSError, ValueError) as e:
        raise ValueError(f"設定ファイルを読めない: {path}: {e}")
    mp = data.get("model_policy", {}) if isinstance(data, dict) else {}
    if mp is None:
        mp = {}
    if not isinstance(mp, dict):
        raise ValueError(f"{path} の model_policy がオブジェクトでない")
    unknown = sorted(set(mp) - set(DEFAULT_MODEL_POLICY))
    if unknown:
        raise ValueError(f"{path} の model_policy に知らない項目: {', '.join(unknown)}")
    e = mp.get("enabled", out["enabled"])
    if not isinstance(e, bool):
        raise ValueError(f"{path} の model_policy.enabled は true か false: {e!r}")
    m = mp.get("away_minutes", out["away_minutes"])
    if isinstance(m, bool) or not isinstance(m, (int, float)) or not 1 <= m <= 7 * 24 * 60:
        raise ValueError(f"{path} の model_policy.away_minutes は 1〜10080 の数（分）: {m!r}")
    am = mp.get("away_model", out["away_model"])
    if not (isinstance(am, str) and re.match(MODEL_NAME, am)):
        raise ValueError(f"{path} の model_policy.away_model はモデル名（\"haiku\" など）: {am!r}")
    k = mp.get("keep", out["keep"])
    if not isinstance(k, list) or not all(isinstance(x, str) and x for x in k):
        raise ValueError(f"{path} の model_policy.keep は空でない文字列のリスト: {k!r}")
    out.update(enabled=e, away_minutes=m, away_model=am, keep=list(k))
    return out


if __name__ == "__main__":
    # bin/claude-kitty（bash）用: "kitty-launch" で「デスクトップの番号（無ければ -） 最大化（1/0）」を 1 行出す。
    # 設定が壊れていれば stderr に理由を出し、既定（移さない・最大化しない）を出す（起動は止めない）
    import sys
    if sys.argv[1:] == ["kitty-launch"]:
        try:
            c = load_kitty_config(os.environ)
        except ValueError as e:
            print(f"claude-kitty: {e}（既定で起動する）", file=sys.stderr)
            c = dict(DEFAULT_KITTY)
        print(f"{c['desktop'] or '-'} {1 if c['maximize'] else 0}")
