"""claude-browser の副作用のない部分（テスト対象）。"""
import re


def sanitize(key):
    """キーはファイル名とプロファイル名に使うので、英数字と - _ だけにする（../ で外へ出さない）。"""
    # 小文字にそろえる（XWayland では KWin が窓のクラス名を小文字にするので、大文字だと窓を探せない）
    s = re.sub(r"[^a-z0-9_-]", "", (key or "").lower())[:64]
    if not s:
        raise ValueError(f"invalid key: {key!r}")
    return s


def choose_key(explicit, pane_vars, env):
    """ブラウザ（プロファイル）を分ける単位を決める。

    ログイン状態を残したいので、起動し直しても変わらない名前を優先する:
    --key > ペインの役割 (user var role) > ペインの席 (user var slot) > セッション ID > kitty ペイン。
    セッション ID と kitty のペイン ID は起動し直すたびに変わるので最後の手段。
    """
    if explicit:
        return sanitize(explicit)
    pane_vars = pane_vars or {}
    for var in ("role", "slot"):
        if pane_vars.get(var):
            return sanitize(f"{var}-{pane_vars[var]}")
    sid = env.get("CLAUDE_CODE_SESSION_ID")
    if sid:
        return sanitize("s-" + sid)
    wid = env.get("KITTY_WINDOW_ID")
    if wid:
        pid = kitty_pid_from_socket(env.get("KITTY_LISTEN_ON", "")) or "x"
        return sanitize(f"k{pid}-w{wid}")
    raise ValueError("no --key, kitty pane or CLAUDE_CODE_SESSION_ID")


def kitty_pid_from_socket(sock):
    """unix:/run/user/1000/kitty-1234 → 1234。形が違えば None。"""
    m = re.search(r"kitty-(\d+)$", sock or "")
    return int(m.group(1)) if m else None


def profile_processes(procs, profile):
    """procs=[(pid, argv)] から、このプロファイルで動いている Chrome の (pid, port) を返す。

    state ファイルが失われても（起動直後の失敗・手動の削除）実際のプロセスから
    ポートを取り戻せるようにする。ポートの持ち主の確認にも使う。
    """
    want = f"--user-data-dir={profile}"
    out = []
    for pid, argv in procs:
        if want not in argv:
            continue
        port = None
        for a in argv:
            if a.startswith("--remote-debugging-port="):
                try:
                    port = int(a.split("=", 1)[1])
                except ValueError:
                    pass
        out.append((pid, port))
    return out


def merge_place_opts(given, state, defaults):
    """未指定 (None) の配置オプションは前回の state、無ければ既定値を使う。

    open を呼び直しても show モードが self に戻ってペインを覆い隠さないようにするため。
    """
    out = {}
    for k, d in defaults.items():
        v = given.get(k)
        if v is None:
            v = (state or {}).get(k)
        out[k] = d if v is None else v
    return out


def parse_journal(text, token):
    """KWin スクリプトの console.info 出力から (行のリスト, END まで来たか) を返す。"""
    lines, ended = [], False
    for line in text.splitlines():
        idx = line.find(token + " ")
        if idx < 0:
            continue
        payload = line[idx + len(token) + 1:]
        if payload == "END":
            ended = True
        else:
            lines.append(payload)
    return lines, ended


def resolve_pane(osw_list, over, own_id):
    """(OS ウィンドウ, アクティブなタブ, 対象ペイン) を返す。見つからなければ LookupError。"""
    if over is None and own_id is None:
        raise LookupError("kitty の外から呼ぶときは --over SLOT|ID を指定する")
    for osw in osw_list:
        for tab in osw.get("tabs", []):
            for w in tab.get("windows", []):
                if over is None:
                    hit = w["id"] == own_id
                else:
                    uv = w.get("user_vars", {})
                    hit = str(w["id"]) == str(over) or over in (uv.get("slot"), uv.get("role"))
                if hit:
                    if not tab.get("is_active"):
                        raise LookupError(f"ペイン {w['id']} はいま表示されていないタブにある")
                    if (tab.get("layout_state") or {}).get("class") != "Splits":
                        check_layout(tab["windows"])
                    return osw, tab, w
    raise LookupError(f"ペインが見つからない: {over if over is not None else own_id}")


def check_layout(windows):
    """複数ペインなのに隣接情報が無いと位置を計算できない（黙って全面を覆わないため）。"""
    if len(windows) > 1 and not any(any((w.get("neighbors") or {}).values()) for w in windows):
        raise LookupError("kitty の隣接情報 (neighbors) が無く、ペインの位置を計算できない")


def ledger_touch(ledger, key, now, url=None, session=None, pane_vars=None, note=None):
    """プロファイル台帳を更新した新しい dict を返す（どのプロファイルが何に使われ、どこにログイン済みか）。"""
    out = dict(ledger or {})
    e = dict(out.get(key) or {"created": now, "notes": []})
    e["last_used"] = now
    if url:
        e["last_url"] = url
    if session:
        e["last_session"] = session
    if pane_vars:
        e["pane"] = {k: v for k, v in pane_vars.items() if k in ("role", "slot")}
    if note:
        e["notes"] = list(e.get("notes", [])) + [f"{now} {note}"]
    out[key] = e
    return out
