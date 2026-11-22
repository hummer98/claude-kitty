"""claude-roster の名簿（副作用なし、テスト対象）。

名簿 = kitty の中の Claude Code セッションの一覧と配置。kitty を起動し直したり落ちたりしても、
同じ配置・フォルダ・モデル・会話（セッション ID）に戻すために使う。

  {"version": 1, "saved_at": 1795000000.0, "left": "main",
   "tabs": [{"title": "main", "roles": ["main", "dev-a", "dev-b"], "rows": [["dev-a"], ["dev-b"]]},
            {"title": "standby", "roles": ["dev-c"]}],
   "sessions": {"dev-a": {"session_id": "…", "cwd": "/…", "name": "dev-a", "kind": "interactive",
                          "model": "opus", "model_source": "config", "state": "running", …}}}

rows は統括（left）のいるタブだけ。左の列を除いた右側を、段（上から）× 横（左から）で表す（claude-view rows の形）。
state は running（claude が動いている）/ waiting（claude-wait が待っている）/ shell（claude がいない）。
"""
import json
import os
import re
import shlex

from view import current_rows, left_percent, overlay_ids, pin_rows

MARK = "● "  # claude-state が待受中のペインのいるタブの題に付ける印（lib/state.py の MARK と同じ）


def strip_mark(title):
    """タブの題から claude-state の印を外す（印の有無で別のタブとして記録しないように）。"""
    title = title or ""
    while title.startswith(MARK):
        title = title[len(MARK):]
    return title

VERSION = 1
SID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# claude-view rows の "a,b/c" に入れるので、区切りや空白を含む名前は扱わない
ROLE_RE = re.compile(r"^[^\s,/'\"]+$")
STANDBY = "standby"
# 署名（変わったかどうか）に入れる項目。status や pid は 5 分ごとに変わるので入れない（世代が無駄に回らないように）
SIG_KEYS = ("session_id", "cwd", "model", "name", "kind", "absent_since")
NEXT_MAX_AGE = 300  # 続きの指示のファイルの有効期限（秒）。止められなかったあと、後で /exit したときに勝手に再開しないように


def valid_sid(sid):
    return isinstance(sid, str) and bool(SID_RE.match(sid))


def valid_role(role):
    return isinstance(role, str) and bool(ROLE_RE.match(role))


# ---- コマンド行の読み取り -------------------------------------------------

def _opt(argv, i, names):
    """argv[i] が names のどれか（--x v / --x=v）なら (値, 次の位置)。違えば None。"""
    a = argv[i]
    for n in names:
        if a == n and i + 1 < len(argv):
            return argv[i + 1], i + 2
        if n.startswith("--") and a.startswith(n + "="):
            return a[len(n) + 1:], i + 1
    return None


def parse_argv(argv):
    """claude か claude-wait のコマンド行から {"tool", "session_id", "model", "name", "role"} を読む。

    どちらでもなければ None。claude-wait は bash や python3 から呼ばれるので、どの位置にあってもよい。
    """
    argv = list(argv or [])
    base = [os.path.basename(a) for a in argv]
    if "claude-wait" in base:
        i = base.index("claude-wait")
        rest = argv[i + 1:]
        pos, model, j = [], None, 0
        while j < len(rest):
            got = _opt(rest, j, ("--model",))
            if got:
                model, j = got
                continue
            if not rest[j].startswith("-"):
                pos.append(rest[j])
            j += 1
        if len(pos) >= 2:
            return {"tool": "claude-wait", "role": pos[0], "session_id": pos[1] if valid_sid(pos[1]) else None,
                    "model": model, "name": None}
        return None
    for i, b in enumerate(base):
        if b in ("claude", "claude.exe"):
            break
    else:
        return None
    out = {"tool": "claude", "role": None, "session_id": None, "model": None, "name": None}
    j = i + 1
    while j < len(argv):
        for key, names in (("session_id", ("--resume", "-r", "--session-id")), ("model", ("--model",)),
                           ("name", ("-n", "--name"))):
            got = _opt(argv, j, names)
            if got:
                out[key], j = got
                break
        else:
            j += 1
    if out["session_id"] is not None and not valid_sid(out["session_id"]):
        out["session_id"] = None
    return out


# ---- モデルの決め方 --------------------------------------------------------

def settings_model(cwd, read_text):
    """そのフォルダの .claude/settings.local.json、無ければ .claude/settings.json の model。"""
    if not cwd:
        return None
    for name in ("settings.local.json", "settings.json"):
        try:
            data = json.loads(read_text(os.path.join(cwd, ".claude", name)))
        except (OSError, ValueError):
            continue
        m = data.get("model") if isinstance(data, dict) else None
        if isinstance(m, str) and m:
            return m
    return None


def transcript_model(lines):
    """会話記録（jsonl の行、古い順）のうち、最後の本体の assistant の model。合成の応答（<synthetic>）は飛ばす。"""
    for line in reversed(list(lines)):
        if '"assistant"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("type") != "assistant" or d.get("isSidechain"):
            continue
        m = (d.get("message") or {}).get("model")
        if isinstance(m, str) and m and not m.startswith("<"):
            return m
    return None


def resolve_model(role, cwd, sid, argv_model, conf_models, settings_fn, transcript_fn):
    """(model, source)。順: 設定の roster.models > 起動時の --model > フォルダの settings > 会話記録。

    起動時の --model は、そのセッションを開いた人が選んだもので、フォルダの既定より強い（claude も同じ順で効かせる）。
    会話記録は /model で途中で変えたものも映るが、一時的な切り替えかもしれないので最後にする。
    """
    if isinstance(conf_models, dict) and isinstance(conf_models.get(role), str) and conf_models[role]:
        return conf_models[role], "config"
    if argv_model:
        return argv_model, "argv"
    m = settings_fn(cwd)
    if m:
        return m, "settings"
    m = transcript_fn(sid) if sid else None
    if m:
        return m, "transcript"
    return None, None


# ---- 名簿の組み立て --------------------------------------------------------

def pane_session(win, sessions_by_pid, sessions_by_sid):
    """ペインの前面のプロセスから、そのペインのセッションを読む。

    返り値 {"state", "session_id", "pid", "session"(JSON), "argv_model", "argv_name", "front_pid", "waiter_pid",
    "wait_id"(claude-wait の引数の ID。/clear のあとは session_id と違う)}。
    - claude の pid に ~/.claude/sessions/<pid>.json があれば、それが正（/clear で ID が変わっても追える）。
    - 無ければ（背景セッションの前面・起動の直後）コマンド行の --resume の ID で JSON を探す。
    - claude-wait だけが動いていれば waiting（まだ claude を開いていない）。
    """
    procs = win.get("foreground_processes") or []
    out = {"state": "shell", "session_id": None, "pid": None, "session": None, "argv_model": None,
           "argv_name": None, "front_pid": None, "waiter_pid": None, "cwd": None, "wait_id": None}
    parsed = [(p, parse_argv(p.get("cmdline"))) for p in procs]
    # 並び順によらず、先に claude-wait を拾う（claude が先に並んでも waiter を取りこぼさない）
    wait_info = None
    for p, info in parsed:
        if info and info["tool"] == "claude-wait":
            out["waiter_pid"], out["wait_id"] = p.get("pid"), info["session_id"]
            wait_info = info
            break
    for p, info in parsed:
        if not info or info["tool"] == "claude-wait":
            continue
        out["front_pid"] = p.get("pid")
        out["argv_model"], out["argv_name"] = info["model"], info["name"]
        out["cwd"] = p.get("cwd")
        js = sessions_by_pid.get(p.get("pid"))
        if js is None and info["session_id"]:
            js = sessions_by_sid.get(info["session_id"])
        sid = (js or {}).get("sessionId") or info["session_id"]
        out.update(state="running", session_id=sid if valid_sid(sid) else None, session=js,
                   pid=(js or {}).get("pid") or p.get("pid"))
        return out
    if wait_info:
        out.update(state="waiting", session_id=wait_info["session_id"], argv_model=wait_info["model"])
    return out


def build_roster(osw_list, sessions, left, fraction, conf_models=None, settings_fn=None, transcript_fn=None,
                 now=0.0, ignore=()):
    """kitten @ ls と ~/.claude/sessions/*.json（dict のリスト）から名簿を作る。

    fraction(tab, window_id) → (x, y, w, h)（geometry.tab_pane_fraction）。role の無いペイン・overlay・
    ignore の role・名前に区切り文字を含む role は入れない（復元できないか、claude-view で扱えないため）。
    """
    settings_fn = settings_fn or (lambda cwd: None)
    transcript_fn = transcript_fn or (lambda sid: None)
    by_pid = {s.get("pid"): s for s in sessions if isinstance(s, dict) and s.get("pid")}
    by_sid = {s.get("sessionId"): s for s in sessions if isinstance(s, dict) and s.get("sessionId")}
    tabs, entries, skipped = [], {}, []
    for osw in osw_list:
        for tab in osw.get("tabs", []):
            skip = overlay_ids(tab)
            roles = []
            for w in tab.get("windows", []):
                if w.get("id") in skip:
                    continue
                role = (w.get("user_vars") or {}).get("role")
                if not role or role in ignore:
                    continue
                if not valid_role(role) or role in entries:
                    skipped.append(role)
                    continue
                ps = pane_session(w, by_pid, by_sid)
                js = ps["session"] or {}
                cwd = js.get("cwd") or ps["cwd"] or w.get("cwd")
                model, source = resolve_model(role, cwd, ps["session_id"], ps["argv_model"], conf_models,
                                              settings_fn, transcript_fn)
                entries[role] = {
                    "session_id": ps["session_id"], "cwd": cwd, "name": js.get("name") or ps["argv_name"],
                    "kind": js.get("kind"), "status": js.get("status"), "state": ps["state"],
                    "pid": ps["pid"], "model": model, "model_source": source,
                    "window_id": w.get("id"), "tab": strip_mark(tab.get("title")),
                    "slot": (w.get("user_vars") or {}).get("slot"),
                }
                roles.append(role)
            if not roles:
                continue
            t = {"title": strip_mark(tab.get("title")), "roles": roles}
            if left in roles:
                # 入れなかったペイン（ignore など）は段組みからも外す（位置は元のタブの分割の木で計算する）
                kept = dict(tab, windows=[w for w in tab.get("windows", [])
                                          if (w.get("user_vars") or {}).get("role") in roles])
                t["rows"] = current_rows(kept, left, fraction)
            tabs.append(t)
    # 統括のいるタブを先頭に（復元のとき main タブとして最初に作る）
    tabs.sort(key=lambda t: 0 if "rows" in t else 1)
    dedupe_ids(entries)
    return {"version": VERSION, "saved_at": now, "left": left, "tabs": tabs, "sessions": entries,
            "skipped": skipped}


def dedupe_ids(entries):
    """同じセッション ID を 2 人が持たないようにする（claude-wait で別のペインへ移している途中など）。

    claude が動いている役割を正とし、待っているだけ・引き継いだだけの役割の ID は消す（restore で二重に開かず、
    名簿が check_roster で読めなくなって保存も復元も止まる、ということが無いように）。
    """
    rank = {"running": 0, "waiting": 1}
    holders = {}
    for role, e in entries.items():
        if e.get("session_id"):
            holders.setdefault(e["session_id"], []).append(role)
    for sid, roles in holders.items():
        if len(roles) < 2:
            continue
        roles.sort(key=lambda r: (rank.get(entries[r].get("state"), 2), 1 if entries[r].get("carried") else 0))
        for r in roles[1:]:
            entries[r]["session_id"] = None
            entries[r]["dup_of"] = roles[0]
    return entries


def carry_over(new, old, now, keep_seconds, drop_absent=False):
    """前の名簿から引き継ぐ。

    - ペインはあるが claude がいない（/exit してシェルに戻った）role は、前の ID・フォルダ・モデルを残す
      （次に開くとき、同じ会話に戻れるように）。
    - ペインごと無くなった role は absent_since を付けて残す（誤ってペインを閉じたときに戻せるように）。
      keep_seconds を過ぎたもの、drop_absent（手で save したとき = いまが正）のときは残さない。
    """
    if not old or not isinstance(old.get("sessions"), dict):
        return new
    out = dict(new)
    sess = {k: dict(v) for k, v in new["sessions"].items()}
    held = {e.get("session_id") for e in sess.values() if e.get("session_id")}
    for role, e in sess.items():
        prev = old["sessions"].get(role)
        # ほかの役割がいま持っている ID は引き継がない（同じ ID を 2 人に持たせない）
        if prev and not e.get("session_id") and prev.get("session_id") and prev["session_id"] not in held:
            for k in ("session_id", "model", "model_source", "name", "kind"):
                e[k] = prev.get(k)
            e["carried"] = True
    tabs = [dict(t, roles=list(t["roles"])) for t in new["tabs"]]
    if not drop_absent:
        for t_old in old.get("tabs", []):
            for role in t_old.get("roles", []):
                prev = old["sessions"].get(role)
                if role in sess or not prev or role == new.get("left") or prev.get("session_id") in held:
                    continue
                since = prev.get("absent_since") or now
                if now - since > keep_seconds:
                    continue
                sess[role] = dict(prev, absent_since=since, state="absent", window_id=None, pid=None)
                if prev.get("session_id"):
                    held.add(prev["session_id"])
                # 前と同じ題のタブに置く（無ければ standby）。段組みには入れない（いまの配置を崩さない）
                dest = next((t for t in tabs if t["title"] == t_old.get("title") and "rows" not in t), None)
                if dest is None:
                    dest = next((t for t in tabs if t["title"] == STANDBY), None)
                if dest is None:
                    dest = {"title": STANDBY, "roles": []}
                    tabs.append(dest)
                dest["roles"].append(role)
    out["sessions"], out["tabs"] = sess, tabs
    dedupe_ids(sess)
    return out


def signature(roster):
    """名簿が意味のある変化をしたか比べるための値（保存の時刻・状態・pid・window id は入れない）。"""
    if not roster:
        return None
    sess = {r: {k: e.get(k) for k in SIG_KEYS} for r, e in (roster.get("sessions") or {}).items()}
    tabs = [(t.get("title"), t.get("roles"), t.get("rows")) for t in roster.get("tabs", [])]
    return json.dumps({"left": roster.get("left"), "tabs": tabs, "sessions": sess}, sort_keys=True)


def check_roster(roster):
    """読み込んだ名簿の形を確認する。おかしければ ValueError（壊れた名簿で別のセッションを開かないように）。"""
    if not isinstance(roster, dict) or roster.get("version") != VERSION:
        raise ValueError("名簿の形か版が違う")
    sess = roster.get("sessions")
    if not isinstance(sess, dict) or not isinstance(roster.get("tabs"), list):
        raise ValueError("名簿に sessions か tabs が無い")
    seen = {}
    for role, e in sess.items():
        if not valid_role(role):
            raise ValueError(f"役割の名前が使えない: {role!r}")
        sid = e.get("session_id")
        if sid is not None and not valid_sid(sid):
            raise ValueError(f"{role} のセッション ID の形が違う: {sid!r}")
        if sid and sid in seen:
            raise ValueError(f"{seen[sid]} と {role} が同じセッション ID {sid[:8]} を持っている")
        if sid:
            seen[sid] = role
    for t in roster["tabs"]:
        for role in t.get("roles", []):
            if role not in sess:
                raise ValueError(f"タブ {t.get('title')} の {role} が sessions に無い")
    return roster


# ---- 復元 ------------------------------------------------------------------

def rows_spec(rows):
    """[["a","b"],["c"]] → "a,b/c"（claude-view rows に渡す形）。"""
    return "/".join(",".join(r) for r in rows if r)


def filter_rows(rows, keep):
    return [r2 for r2 in ([x for x in r if x in keep] for r in rows or []) if r2]


def pane_command(role, entry, wait_cmd):
    """ペインで動かすコマンド（argv）。セッション ID があれば claude-wait で resume、無ければシェル。"""
    sid = entry.get("session_id")
    if not sid:
        return ["bash", "-l"]
    inner = [wait_cmd, role, sid]
    if entry.get("model"):
        inner += ["--model", entry["model"]]
    if entry.get("name") and entry["name"] != role:
        inner.append("--rename")  # resume では -n が効かないので、claude-wait が /rename の案内を出す
    # ログインシェルの PATH（~/.local/bin など）で claude を見つける。claude-wait が最後にシェルへ戻る
    return ["bash", "-lic", shlex.join(inner)]


def _launch_line(role, entry, wait_cmd, extra=()):
    args = ["launch", "--title", role, "--var", f"role={role}"]
    if entry.get("slot"):
        args += ["--var", f"slot={entry['slot']}"]
    if entry.get("cwd"):
        args += ["--cwd", entry["cwd"]]
    args += list(extra)
    return shlex.join(args + pane_command(role, entry, wait_cmd))


def main_tab_lines(left, rows, sessions, wait_cmd, pinned=()):
    """統括のタブ: 左に統括、右に段組みどおり（等分割に近い比で。最後は claude-view rows で正確にそろえる）。

    kitty のセッションの launch は、いま前にあるペインを割る（--bias は新しいペインの割合）。
    claude-view の plan と同じく、先に各段の先頭を縦に積み、そのあと段の中を左から埋める。
    pinned（設定の view.pinned。常時表示）は claude-view と同じく 1 段目の先頭に置く。left と同じもの・
    sessions に無いものは置かない。
    """
    rows = pin_rows(rows, [p for p in pinned if p != left and p in sessions])
    out = [_launch_line(left, sessions[left], wait_cmd)]
    if not rows:
        return out
    out.append(_launch_line(rows[0][0], sessions[rows[0][0]], wait_cmd,
                            ["--location=vsplit", f"--bias={100 - left_percent(rows)}"]))
    n = len(rows)
    for i in range(1, n):
        out.append(f"focus_matching_window var:role={rows[i - 1][0]}")
        out.append(_launch_line(rows[i][0], sessions[rows[i][0]], wait_cmd,
                                ["--location=hsplit", f"--bias={round(100 * (n - i) / (n - i + 1))}"]))
    for row in rows:
        k = len(row)
        for j in range(1, k):
            out.append(f"focus_matching_window var:role={row[j - 1]}")
            out.append(_launch_line(row[j], sessions[row[j]], wait_cmd,
                                    ["--location=vsplit", f"--bias={round(100 * (k - j) / (k - j + 1))}"]))
    return out


def plan_restore(roster, present_roles, busy, only=None, include_absent=False):
    """復元で開く role と、開かない理由を決める。

    present_roles: いまの kitty にいる role。busy: {session_id: 理由}（どこかで動いている・待っている）。
    only: 指定があれば、その role だけ（absent でも開く）。
    返り値 (launch: [role], skipped: [(role, 理由)])。
    """
    sess = roster["sessions"]
    order = [r for t in roster["tabs"] for r in t["roles"]]
    if only:
        unknown = [r for r in only if r not in sess]
        if unknown:
            raise ValueError("名簿に無い役割: " + ", ".join(unknown))
        order = [r for r in order if r in only]
    launch, skipped = [], []
    for role in order:
        e = sess[role]
        if role in present_roles:
            continue
        if e.get("absent_since") and not include_absent and not only:
            skipped.append((role, "保存の前からペインが無かった（--include-absent か、役割を名指しで開く）"))
            continue
        sid = e.get("session_id")
        if sid and sid in busy:
            skipped.append((role, f"{sid[:8]} は{busy[sid]}（二重に開かない）"))
            continue
        launch.append(role)
    return launch, skipped


def session_file(roster, launch, wait_cmd, header="", pinned=()):
    """kitty のセッションファイルの本文。launch の role だけを開く（統括は必ず先頭のタブの左）。

    統括のタブ以外は、名簿のタブの題のまま作り、縦に積む（claude-view は standby を題で探す）。
    pinned（設定の view.pinned）は、名簿で別のタブや段にいても統括のタブの 1 段目の先頭に開く
    （保存したあとで設定に足した役割も、開き直したときから常時表示にするため）。
    """
    sess, left = roster["sessions"], roster["left"]
    keep = set(launch)
    lines = [f"# {header}" if header else "# claude-roster が名簿から作った", ""]
    main = next((t for t in roster["tabs"] if "rows" in t), None)
    first = True
    placed = set()
    if main and left in keep:
        lines += ["new_tab " + shlex.quote(main["title"] or "main"), "layout splits"]
        rows = pin_rows(filter_rows(main.get("rows"), keep), [p for p in pinned if p in keep and p != left])
        lines += main_tab_lines(left, rows, sess, wait_cmd)
        placed = {left} | {r for row in rows for r in row}
        # 段組みに無かったが統括のタブにいた role（段の計算から漏れたもの）は standby に回す
        rest_main = [r for r in main["roles"] if r in keep and r not in placed]
        lines.append(f"focus_matching_window var:role={left}")
        first = False
    else:
        rest_main = [r for r in (main["roles"] if main else []) if r in keep and r != left]
    others = []
    for t in roster["tabs"]:
        if t is main:
            continue
        roles = [r for r in t["roles"] if r in keep and r not in placed]
        if t["title"] == STANDBY:
            roles = rest_main + roles
            rest_main = []
        if roles:
            others.append((t["title"] or STANDBY, roles))
    if rest_main:
        others.append((STANDBY, rest_main))
    for title, roles in others:
        lines += ["", "new_tab " + shlex.quote(title), "layout splits"]
        for i, role in enumerate(roles):
            extra = [] if i == 0 else ["--location=hsplit"]
            lines.append(_launch_line(role, sess[role], wait_cmd, extra))
        first = False
    if first:
        return None  # 開くものが無い
    return "\n".join(lines) + "\n"


def launch_args(role, entry, wait_cmd, tab_title, tab_exists):
    """動いている kitty に 1 人足す kitten @ launch の引数（フォーカスは動かさない）。"""
    args = ["launch", "--keep-focus", "--title", role, "--var", f"role={role}"]
    if entry.get("slot"):
        args += ["--var", f"slot={entry['slot']}"]
    if entry.get("cwd"):
        args += ["--cwd", entry["cwd"]]
    if tab_exists:
        args += ["--type=window", "--match", f"title:^{re.escape(tab_title)}$", "--location=hsplit"]
    else:
        args += ["--type=tab", "--tab-title", tab_title]
    return args + pane_command(role, entry, wait_cmd)


def target_tab(roster, role, pinned=()):
    """足すときに入れるタブの題。統括のタブの役と常時表示（pinned）は standby に入れて、claude-view rows で前に出す。"""
    if role in pinned:
        return STANDBY
    for t in roster["tabs"]:
        if role in t["roles"]:
            return STANDBY if "rows" in t else (t["title"] or STANDBY)
    return STANDBY


# ---- restart ---------------------------------------------------------------

def restart_refusal(role, pane, session_json, state_word, guard_running, waiter, left=None):
    """起動し直してよいか。だめなら理由（文字列）、よければ None。

    pane: pane_session() の結果。session_json: そのセッションの ~/.claude/sessions の JSON。
    state_word: claude-state get の 1 語（無ければ ""）。guard_running: 保護するプロセスが動いているか。
    waiter: そのセッションを開いた claude-wait の記録 {"pid"}（無ければ None）。
    """
    if guard_running:
        return "保護するプロセス（view.guard。全画面のアプリなど）が動いているので止めない"
    if pane is None:
        return f"{role} のペインが kitty に無い（claude-roster restore {role} で開く）"
    if pane["state"] != "running" or not pane.get("session_id"):
        return f"{role} のペインで claude が動いていない（{pane['state']}）"
    if not session_json:
        return f"{role} のセッションの記録（~/.claude/sessions）が見つからない"
    status = session_json.get("status")
    if status != "idle":
        return f"{role} は idle でない（status={status}）"
    if state_word and state_word not in ("idle", "done"):
        return f"{role} は idle でない（claude-state={state_word}）"
    if not waiter or not pane.get("waiter_pid") or waiter.get("pid") != pane["waiter_pid"]:
        return (f"{role} の claude は、続きの指示に対応した claude-wait から開かれていない"
                "（止めるとシェルに戻るだけなので止めない。一度 /exit して claude-wait で開き直す）")
    return None


def stop_target(pane, session_json, sessions, allow_bg=False):
    """SIGTERM を送る pid。

    ふつうは前面の claude。背景セッション（kind=bg）は daemon の中の、そのセッションの host が本体で、
    前面を止めても終わらない。JSON の pid がそれ。daemon 本体を止めると他の背景セッションも落ちるので、
    同じ pid を持つセッションが他にあれば None（止めない）。
    """
    if (session_json or {}).get("kind") != "bg":
        return pane.get("front_pid")
    if not allow_bg:
        return None  # 実物の背景セッションで確認していないので、明示したときだけ
    pid = session_json.get("pid")
    if not pid:
        return None
    shared = [s for s in sessions if s.get("pid") == pid and s.get("sessionId") != session_json.get("sessionId")]
    return None if shared else pid


def next_payload(waiter_pid, wait_id, sid, cwd, model, now):
    """wait_id: claude-wait の引数の ID（ファイルの名前）。sid: 開き直す ID（/clear のあとは wait_id と違う）。"""
    return {"waiter_pid": waiter_pid, "wait_id": wait_id, "session_id": sid, "cwd": cwd, "model": model, "at": now,
            "by": "claude-roster restart"}


def read_next(text, waiter_pid, wait_id, now, max_age=NEXT_MAX_AGE):
    """続きの指示のファイルの中身を確認して {"session_id", "cwd", "model"} を返す。使えなければ None。

    別の claude-wait 宛て・別の claude-wait の ID 宛て・古いもの（止められずに残ったもの）・ID の形が違うものは使わない。
    """
    try:
        d = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("waiter_pid") != waiter_pid or d.get("wait_id") != wait_id:
        return None
    if not valid_sid(d.get("session_id")):
        return None
    at = d.get("at")
    if not isinstance(at, (int, float)) or not (now - max_age <= at <= now + 5):
        return None
    cwd, model = d.get("cwd"), d.get("model")
    if cwd is not None and not (isinstance(cwd, str) and cwd):
        return None
    if model is not None and not (isinstance(model, str) and model):
        return None
    return {"session_id": d["session_id"], "cwd": cwd, "model": model}


# ---- status ----------------------------------------------------------------

def diff_lines(saved, now_roster):
    """名簿といまの差を 1 行ずつ。差が無ければ空のリスト。"""
    out = []
    cur = now_roster["sessions"] if now_roster else {}
    for role, e in saved["sessions"].items():
        c = cur.get(role)
        sid = e.get("session_id")
        if e.get("absent_since"):
            out.append(f"{role}: 保存の前からペインが無い（名簿に残しているだけ）")
            continue
        if c is None:
            out.append(f"{role}: いない（名簿では {(sid or '-')[:8]}、{e.get('cwd')}）")
            continue
        if c.get("state") != "running" and e.get("state", "running") == "running":
            out.append(f"{role}: claude が動いていない（{c.get('state')}）")
        if (c.get("session_id") or None) != (sid or None) and c.get("session_id"):
            out.append(f"{role}: ID が変わった {(sid or '-')[:8]} → {c['session_id'][:8]}")
        if c.get("cwd") and e.get("cwd") and os.path.normpath(c["cwd"]) != os.path.normpath(e["cwd"]):
            out.append(f"{role}: 別のフォルダにいる {c['cwd']}（名簿では {e['cwd']}）")
    for role in cur:
        if role not in saved["sessions"]:
            out.append(f"{role}: 名簿に無い（save で足す）")
    return out
