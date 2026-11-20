"""claude-browser の副作用のない部分（テスト対象）。"""
import re


def sanitize(key):
    """キーはファイル名とプロファイル名に使うので、英数字と - _ だけにする（../ で外へ出さない）。"""
    # 小文字にそろえる（XWayland では KWin が窓のクラス名を小文字にするので、大文字だと窓を探せない）
    s = re.sub(r"[^a-z0-9_-]", "", (key or "").lower())[:64]
    if not s:
        raise ValueError(f"プロファイルの名前に使える文字（英数字と - _）が無い: {key!r}")
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
    raise ValueError("プロファイルの名前を決められない（kitty のペインか Claude Code の中で呼ぶか、--key NAME で名前を付ける）")


def kitty_pid_from_socket(sock):
    """unix:/run/user/1000/kitty-1234 → 1234。形が違えば None。"""
    m = re.search(r"kitty-(\d+)$", sock or "")
    return int(m.group(1)) if m else None


def profile_processes(procs, profile, loose=False):
    """procs=[(pid, argv)] から、このプロファイルで動いている Chrome の (pid, port) を返す。

    state ファイルが失われても（起動直後の失敗・手動の削除）実際のプロセスから
    ポートを取り戻せるようにする。ポートの持ち主の確認にも使う。
    loose（macOS。引数を ps の空白区切りでしか読めなかったとき。lib/proc.py）なら、空白を含むプロファイルの
    パス（~/Library/Application Support/…）は切れているので、つないだ文字列でも探す。Linux では使わない
    （`bash -c "… --user-data-dir=… "` のような別のプロセスを Chrome と数えて止めないように）。
    """
    want = f"--user-data-dir={profile}"
    out = []
    for pid, argv in procs:
        if want not in argv and not (loose and (want + " ") in (" ".join(argv) + " ")):
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


AREA_FOR_MODE = {"self": "pane", "show": "others"}  # area を省いたときの既定（自分の確認は自分のペイン、見せるときは他のペイン）
RATIO_FOR_AREA = {"others": 1.0}  # ratio を省いたときの既定。others は縦 100%、pane / right は前からの 0.65
DEFAULT_RATIO = 0.65


def resolve_place_opts(given, state, move=False):
    """claude-browser の配置オプション {mode, ratio, over, focus, area} を決める。

    - mode・over: 未指定なら前回の値（open を呼び直しても show が self に戻って会話を隠さないように）。
      ただし open では、前回が他のペインの場所（others・right）なら mode を引き継がず self に戻す
      （claude-show で見せたあとの、自分の作業用の open が他のペインを覆わないように）。
      over を引き継ぐのは、決まった area が pane のときだけ。others・right は「呼び出した自分のペインを避ける」
      「左の列を除く」規則で over は関係ないので、前回の over の先（見えないタブに移ったかもしれない）を探しに
      行かないよう None にする。引き継いだ先が見つからないときに自分のペインに戻すのは prepare。
    - focus: 一回ごとの指定（保護する窓が前面のときの none が以後ずっと残らないように）。
    - area: 明示が最優先。move で mode も area も省いたときは前回の area（同じものを置き直すだけ）。
      --over を明示したときは pane（その先に重ねる指定なので）。
      それ以外（open、または mode を指定した move）は mode の既定（self → pane、show → others）。
    - ratio: 明示が最優先。前回と同じ area になったときだけ前回の値（area が違えば「上から何割」の基準が違う）。
      それ以外は area の既定（others は 1.0、pane / right は 0.65）。
    """
    state = state or {}
    prev_mode = state.get("mode")
    if not move and state.get("area") in ("others", "right"):
        prev_mode = None
    mode = given.get("mode") or prev_mode or "self"
    over = given.get("over") if given.get("over") is not None else state.get("over")
    area = given.get("area")
    if area is None and move and given.get("mode") is None and state.get("area"):
        area = state["area"]
    if area is None and given.get("over") not in (None, "self"):
        area = "pane"
    if area is None:
        area = AREA_FOR_MODE.get(mode, "pane")
    if given.get("over") is None and area != "pane":
        over = None
    ratio = given.get("ratio")
    if ratio is None and state.get("ratio") is not None and (state.get("area") or "pane") == area:
        ratio = state["ratio"]
    if ratio is None:
        ratio = RATIO_FOR_AREA.get(area, DEFAULT_RATIO)
    return {"mode": mode, "ratio": ratio, "over": over, "focus": given.get("focus") or "kitty", "area": area}


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
