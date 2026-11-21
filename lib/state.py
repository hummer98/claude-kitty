"""claude-state の副作用のない部分（テスト対象）: 状態の遷移・色・一覧の整形・タブの印。

状態は 4 つ:
  working  作業中（ユーザーの入力を受けてから終わるまで）
  waiting  待受中（許可の確認・AskUserQuestion・手で付けた「問いを残した」）
  done     終わって、まだユーザーがそのペインを見ていない（完了・未読）
  idle     終わって何も待っていない（done のペインを見たあと）
目立たせるのは waiting だけにする（working・done・idle は控えめ）。
"""
import hashlib
import json
import os
import re

STATES = ("working", "waiting", "done", "idle")

# background が None なら kitty.conf の background（元の色）に戻す。name は 256 色の番号（None ならそのまま）
DEFAULT_COLORS = {
    "working": {"background": "#22282e", "name": None},
    "waiting": {"background": "#2e2a22", "name": 208},
    "done": {"background": "#222b25", "name": 108},
    "idle": {"background": None, "name": 245},
}
DEFAULT_BASE = "#000000"  # kitty の既定の背景（kitty.conf に background が無いとき）

# 許可・入力を待っている通知。idle_prompt（入力欄で 60 秒放置）は待ちではなく「本体が止まっている」印として別に扱う
WAIT_NOTIFICATIONS = ("permission_prompt", "elicitation_dialog", "elicitation_url_dialog", "agent_needs_input")
ASK_TOOL = "AskUserQuestion"
STOP_TOOL = "TaskStop"
MARK = "● "
# Stop の background_tasks のうち、「まだ作業している」と数える種類（shell・monitor は数えない: tail -f などが
# 何日も残り、ペインがずっと作業中に見えてしまうため）
BG_AGENT_TYPES = ("subagent", "workflow", "teammate", "cloud session")
BG_FINISHED = ("completed", "failed", "killed", "stopped", "cancelled")
# 許可を待っている同じエージェントが、これより後に別のツールを呼び始めたら、その許可は片付いた（断られた）と見なす。
# 並行するツール呼び出しの PreToolUse は 1 秒以内に続けて来るので、それより長くとる
STALE_WAIT = 2.0
# 他のセッションからの「通知」（返事も作業も要らない知らせ）の印。そのターンは色を変えない（設定 state.notice で決める）。
# 印の言い回しはチームの約束ごとなので、既定は空（何も通知とみなさない）。例は docs/config.md
DEFAULT_NOTICE = {"contains": [], "prefix": []}
_PEER = re.compile(r"<cross-session-message\b[^>]*>(.*?)</cross-session-message>", re.S)
# 待ちの理由の強さ（list と waiting の reason に出すのは、いちばん強いもの）
KIND_ORDER = ("manual", "ask", "permission", "notify", "relay")


def parse_hook_input(text):
    """hook の stdin（JSON）を dict にする。壊れていれば {}（hook は決して失敗させない）。"""
    try:
        data = json.loads(text) if text and text.strip() else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def valid_session_id(sid):
    """ファイル名に使うので、英数字と - _ だけの id を受け付ける（../ で外へ出さない）。"""
    return isinstance(sid, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid))


def _agent(payload):
    a = payload.get("agent_id")
    return a if isinstance(a, str) and a else None


def _perm_key(agent, payload):
    """許可の待ちの鍵。PermissionRequest には tool_use_id が無いので、エージェント・ツール名・入力で照合する
    （同じ入力の PostToolUse / PostToolUseFailure で外す）。"""
    try:
        body = json.dumps([payload.get("tool_name"), payload.get("tool_input")], sort_keys=True, ensure_ascii=False,
                          default=str)
    except (TypeError, ValueError):
        body = repr(payload.get("tool_name"))
    return f"perm:{agent or 'main'}:{hashlib.sha1(body.encode()).hexdigest()[:12]}"


def normalize(prev):
    """記録を新しい形（waits・agents・main・seen を持つ）にそろえた写しを返す。

    前の版の記録（state と reason だけ）も読めるようにする: 待受中なら、その理由の待ちを 1 つ持たせる。
    """
    rec = dict(prev or {})
    st = rec.get("state")
    waits = rec.get("waits")
    if not isinstance(waits, dict):
        waits = {}
        if st == "waiting":
            r = rec.get("reason")
            if r == "manual":
                waits["manual"] = {"kind": "manual", "agent": None, "at": rec.get("since") or 0}
            elif r == "ask":
                waits[f"ask:{rec.get('tool_use_id') or '?'}"] = {"kind": "ask", "agent": None, "at": rec.get("since") or 0}
            else:
                waits["permnote:main"] = {"kind": "permission", "agent": None, "at": rec.get("since") or 0}
    rec["waits"] = {k: dict(v) for k, v in waits.items() if isinstance(v, dict)}
    agents = rec.get("agents")
    rec["agents"] = {k: dict(v) for k, v in agents.items() if isinstance(v, dict)} if isinstance(agents, dict) else {}
    if not isinstance(rec.get("main"), bool):
        rec["main"] = st in ("working", "waiting")
    if not isinstance(rec.get("seen"), bool):
        rec["seen"] = st not in ("done", "working", "waiting")
    rec["quiet"] = rec.get("quiet") is True
    rec.pop("tool_use_id", None)
    return rec


def running_agents(rec):
    return [k for k, v in (rec.get("agents") or {}).items() if isinstance(v, dict) and v.get("end") is None]


def derive(rec):
    """ペインの状態: 待ちが 1 つでもあれば waiting > 本体かサブエージェントが動いていれば working
    > まだ見ていなければ done > idle。"""
    if rec.get("waits") or rec.get("relay"):
        return "waiting"
    if rec.get("main") or running_agents(rec):
        return "working"
    return "idle" if rec.get("seen") else "done"


_COMPARED = ("state", "reason", "waits", "agents", "main", "seen", "quiet", "relay")


def _finish(prev, rec, now):
    st = derive(rec)
    if st in ("working", "waiting") and not rec.get("quiet"):
        rec["seen"] = False  # 次に終わったら未読にする（通知のターンは、終わっても前の状態に戻す）
    kinds = [w.get("kind") for w in rec["waits"].values()] + (["relay"] if rec.get("relay") else [])
    rec["reason"] = next((k for k in KIND_ORDER if k in kinds), None) if st == "waiting" else None
    if prev.get("state") != st:
        rec["since"] = now
    rec["state"] = st
    old = normalize(prev)
    if prev and all(old.get(k) == rec.get(k) for k in _COMPARED):
        return None  # 何も変わらない（ほとんどの PreToolUse / PostToolUse はここで終わる）
    rec["updated"] = now
    return rec


MAX_ENDED = 50  # 終わった背景のサブエージェントを残す数（記録が限りなく育たないよう、古いものから捨てる）


def _system_prompt(prompt):
    """人が打ったものでない入力（背景の作業の完了の通知・他のセッションからのメッセージ）。"""
    t = prompt.lstrip() if isinstance(prompt, str) else ""
    return t.startswith("<task-notification") or "<cross-session-message" in t


def _trim_ended(agents):
    ended = sorted((v.get("end"), k) for k, v in agents.items()
                   if isinstance(v.get("end"), (int, float)))
    for _end, k in ended[:max(0, len(ended) - MAX_ENDED)]:
        del agents[k]


def _reconcile(agents, tasks, now):
    """Stop の background_tasks（本体の止まった時点で動いている背景の作業）に、数えているサブエージェントを合わせる。

    本体が止まった時点で、前面のサブエージェントはもう終わっている。SubagentStop を取りこぼしても残らないように。
    """
    live = {t["id"]: t for t in tasks if isinstance(t, dict) and isinstance(t.get("id"), str)
            and t.get("type") in BG_AGENT_TYPES and t.get("status") not in BG_FINISHED}
    for k in list(agents):
        ag = agents[k]
        if ag.get("end") is None and k not in live:
            if ag.get("bg"):
                ag["end"] = now
            else:
                del agents[k]
    for k, t in live.items():
        ag = agents.get(k)
        if ag is None or ag.get("end") is not None:
            agents[k] = {"type": t.get("agent_type") or t.get("type"), "start": (ag or {}).get("start", now),
                         "end": None, "bg": True}
        else:
            ag["bg"] = True
    _trim_ended(agents)


def transition(prev, event, payload, now):
    """前の状態 prev（state ファイルの中身か None）と hook のイベントから、次の記録を返す。

    記録は待ち（waits: 鍵 → {kind, agent, at}。手の待ちは、付けた後のターンに入ると older も持つ）・
    サブエージェント（agents: id → {type, start, end, bg}）・本体が動いているか（main）・終わったあと見たか（seen）を持ち、state はそこから derive() で決める。
    変えないときは None。SessionEnd は {"state": "end"}（ファイルを消して色を戻す）。
    """
    prev = prev or {}
    payload = payload if isinstance(payload, dict) else {}
    if event == "SessionEnd":
        return {"state": "end"}
    if event == "SessionStart":
        if payload.get("source") == "compact" and prev:
            return None  # 自動の compact は同じ作業の続き。今の状態を保つ
        rec = {k: v for k, v in prev.items() if k not in _COMPARED}
        rec.update(waits={}, agents={}, main=False, seen=True, quiet=False)
        return _finish(prev, rec, now)
    rec = normalize(prev)
    a = _agent(payload)
    waits, agents = rec["waits"], rec["agents"]
    tuid = payload.get("tool_use_id") if isinstance(payload.get("tool_use_id"), str) else None
    tool = payload.get("tool_name")

    def busy():
        # ツールの hook が来た = そのエージェントは動いている。Stop の後に本体が自分で再開したとき
        # （背景の作業の完了の通知など）や、SubagentStart を取りこぼしたときのため
        if a:
            ag = agents.get(a)
            if ag is None or ag.get("end") is not None:
                agents[a] = {"type": payload.get("agent_type"), "start": now, "end": None, "bg": bool((ag or {}).get("bg"))}
        elif not rec.get("quiet"):  # 通知で始まったターンは、作業中にも見せない
            new_turn()  # UserPromptSubmit なしで本体が自分で再開したターン（背景の作業の完了など）も、新しいターン
            if not rec["main"]:
                rec["seen_before"] = rec["seen"]
            rec["main"] = True

    def drop(pred):
        for k in [k for k, w in waits.items() if pred(k, w)]:
            del waits[k]

    def new_turn():
        # 手の「問いを残した」は付けたターンのもの。本体の新しいターンが始まったら印を付け、そのターンの Stop までに
        # 付け直されなければ外す（最後の返答が問いでなくなったのに待受の色が残らないよう）。ターンの途中に来た
        # UserPromptSubmit では付けない（付けたそのターンで消えてしまう）
        if rec["main"] or rec.get("quiet"):
            return
        for w in waits.values():
            if w.get("agent") is None and w.get("kind") == "manual":
                w["older"] = True

    if event == "UserPromptSubmit":
        new_turn()
        if payload.get("_notice") and not rec["main"]:
            # 他のセッションからの通知で始まったターン: 前の状態（idle / done）のまま見せ、終わっても done にしない
            # （前のターンの手の待ちは、ほかのターンと同じくこのターンの Stop で外す）
            rec["quiet"] = True
        else:
            # 本体が入力を受けた = 本体の問い・許可の画面は閉じている。サブエージェントの待ちは、そのエージェントの
            # hook で外す（背景の作業の完了の通知や、他のセッションからの依頼でも UserPromptSubmit は来るため）。
            # 手の「問いを残した」は、人が打ったときはすぐ外す（そうでなければ、このターンの Stop で外す）
            human = not _system_prompt(payload.get("prompt"))
            drop(lambda k, w: w.get("agent") is None and (human or w.get("kind") != "manual"))
            if not rec["main"]:
                rec["seen_before"] = rec["seen"]  # 通知のターンだったと後で分かったら、ここへ戻す
            rec["main"] = True
            rec["quiet"] = False
    elif event in ("Stop", "StopFailure"):
        rec["main"] = False
        if rec.get("quiet"):
            rec["quiet"] = False  # 通知のターンの終わり: 前の状態（seen）のまま
        elif payload.get("_notice_turn") and isinstance(rec.get("seen_before"), bool):
            # UserPromptSubmit では分からず、Stop で会話記録から通知のターンだったと分かった: 始まる前に戻す
            rec["seen"] = rec["seen_before"]
        else:
            rec["seen"] = False  # 応答が 1 回終わった = 新しく読むものがある（見ていれば呼ぶ側が seen にする）
        # 担当が手で「問いを残した」と付けたターンの Stop では待受のまま。次のターン（他のセッションからのメッセージ・
        # 背景の作業の完了の通知で始まったものも）で付け直さなければ、そのターンの Stop で外す
        # StopFailure（API のエラーなど）では返答を書けていないので、前のターンの手の待ちは次のターンまで残す
        drop(lambda k, w: w.get("agent") is None
             and (w.get("kind") != "manual" or (w.get("older") and event == "Stop")))
        if event == "StopFailure":
            # 残した印は「付けたターンのもの」に戻す（後の Esc の通知で外れないように。次のターンでまた older が付く）
            for w in waits.values():
                if w.get("agent") is None and w.get("kind") == "manual":
                    w.pop("older", None)
        if isinstance(payload.get("background_tasks"), list):
            _reconcile(agents, payload["background_tasks"], now)
            running = set(running_agents(rec))
            drop(lambda k, w: w.get("agent") is not None and w.get("agent") not in running)
    elif event == "SubagentStart":
        if not a:
            return None
        old = agents.get(a) or {}
        agents[a] = {"type": payload.get("agent_type"), "start": now, "end": None, "bg": bool(old.get("bg"))}
    elif event == "SubagentStop":
        if not a:
            return None
        drop(lambda k, w: w.get("agent") == a)
        tasks = payload.get("background_tasks")
        bg = isinstance(tasks, list) and any(isinstance(t, dict) and t.get("id") == a for t in tasks)
        if bg or (agents.get(a) or {}).get("bg"):
            # 背景のサブエージェント: Claude Code の一覧に残るので、終わった時刻を残す（ステータス行の要約用）
            agents[a] = dict(agents.get(a) or {"type": payload.get("agent_type"), "start": now}, end=now, bg=True)
            _trim_ended(agents)
        else:
            agents.pop(a, None)  # 前面のもの（と、Claude Code の内部のエージェント）は終われば消す
    elif event == "PreToolUse":
        busy()
        # armed は PR #1 の版が書いた印（前の版の記録を読んだときのため。今の版は書かない）
        if a is None and ((waits.get("manual") or {}).get("older") or (waits.get("manual") or {}).get("armed")):
            del waits["manual"]  # 手の待ちを付けたターンの後で、本体が動き始めた
        key = _perm_key(a, payload)
        # 許可を待っていたエージェントが別のツールを呼び始めた = その許可は片付いた（断られたなど）
        drop(lambda k, w: w.get("agent") == a and w.get("kind") in ("permission", "notify") and k != key
             and isinstance(w.get("at"), (int, float)) and w["at"] <= now - STALE_WAIT)
        if tool == ASK_TOOL:
            waits[f"ask:{tuid or '?'}"] = {"kind": "ask", "agent": a, "at": now}
    elif event == "PermissionRequest":
        busy()
        waits[_perm_key(a, payload)] = {"kind": "permission", "agent": a, "at": now}
    elif event in ("PostToolUse", "PostToolUseFailure"):
        busy()
        if tuid:
            waits.pop(f"ask:{tuid}", None)
        waits.pop(_perm_key(a, payload), None)
        # 前の版の記録・PermissionRequest の hook が無いときの許可（どのツールのものか分からない）は、どれかが終われば外す
        drop(lambda k, w: k.startswith("permnote:") or (w.get("agent") == a and (w.get("kind") == "notify" or k == "ask:?")))
        if tool == STOP_TOOL and isinstance(payload.get("tool_input"), dict):
            tid = payload["tool_input"].get("task_id") or payload["tool_input"].get("shell_id")
            if isinstance(tid, str):
                agents.pop(tid, None)  # 本体が背景の作業を止めた（一覧からも消える）
    elif event == "Notification":
        nt = payload.get("notification_type")
        if nt == "idle_prompt":
            # 入力欄のまま 60 秒。Esc で止めたときは Stop が来ないので、ここで本体を止まったことにする
            interrupted = rec["main"]
            rec["main"] = False
            rec["quiet"] = False
            # Esc で止めたターンも Stop と同じ: 前のターンの手の待ちは外す
            drop(lambda k, w: w.get("agent") is None and (w.get("kind") != "manual" or w.get("older")))
            # 前面のサブエージェントは本体のターンの中でしか動かない。入力欄に戻っているなら終わっている
            for k in [k for k, v in agents.items() if v.get("end") is None and not v.get("bg")]:
                del agents[k]
                drop(lambda kk, w: w.get("agent") == k)
            if interrupted:
                rec["seen"] = True  # 止めたのはユーザー自身（見ている）
        elif nt is None or nt == "permission_prompt":
            # matcher で絞って登録するので、種類が書かれていない通知は許可の待ちと見なす。
            # PermissionRequest で既に数えている（約 6 秒前に来る）か、より強い理由があれば足さない
            if any(w.get("kind") in ("permission", "ask") or k == "manual" for k, w in waits.items()):
                return None
            waits[f"permnote:{a or 'main'}"] = {"kind": "permission", "agent": a, "at": now}
        elif nt in WAIT_NOTIFICATIONS:
            waits[f"note:{nt}:{a or 'main'}"] = {"kind": "notify", "agent": a, "at": now}
        else:
            return None
    elif event == "manual-waiting":
        waits["manual"] = {"kind": "manual", "agent": None, "at": now}
    elif event == "seen":
        # ユーザーがペインを見た（フォーカスを移した）。done だけが idle になる
        if derive(rec) != "done":
            return None
        rec["seen"] = True
    elif event == "relay":
        # 別の端末から届いた（claude-relay recv）。waits に入れないのは、Stop・入力で外れず、渡したとき（relay-clear）だけ外すため
        rec["relay"] = rec.get("relay") or {"at": now}
    elif event == "relay-clear":
        if not rec.pop("relay", None):
            return None
    else:
        return None
    return _finish(prev, rec, now)


def peer_body(prompt):
    """他のセッションからのメッセージ（<cross-session-message> の中身）。そうでなければ None。"""
    m = _PEER.search(prompt) if isinstance(prompt, str) else None
    return m.group(1).strip() if m else None


def is_notice(prompt, markers=None, body=None):
    """他のセッションからのメッセージで、「通知」の印（本文に contains のどれか、または prefix のどれかで始まる）があるか。

    body を渡せばそれを本文とする（会話記録の origin.body から判定するとき）。
    """
    body = peer_body(prompt) if body is None else body.strip()
    if body is None:
        return False
    m = markers or DEFAULT_NOTICE
    return any(c and c in body for c in m.get("contains", ())) or any(x and body.startswith(x) for x in m.get("prefix", ()))


def merge_notice(user):
    """設定の state.notice を既定に重ねる。形が違えば ValueError。"""
    out = {k: list(v) for k, v in DEFAULT_NOTICE.items()}
    if user is None:
        return out
    if not isinstance(user, dict):
        raise ValueError(f"state.notice がオブジェクトでない: {user!r}")
    for k, v in user.items():
        if k not in out or not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
            raise ValueError(f"state.notice.{k} は空でない文字列のリスト: {v!r}")
        out[k] = list(v)
    return out


def merge_colors(user):
    """設定の state.colors を既定に重ねる。形が違えば ValueError。"""
    out = {s: dict(v) for s, v in DEFAULT_COLORS.items()}
    if user is None:
        return out
    if not isinstance(user, dict):
        raise ValueError(f"state.colors がオブジェクトでない: {user!r}")
    for s, v in user.items():
        if s not in STATES or not isinstance(v, dict):
            raise ValueError(f"state.colors.{s} が正しくない: {v!r}")
        for k, x in v.items():
            if k == "background":
                if x is not None and not (isinstance(x, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", x)):
                    raise ValueError(f"state.colors.{s}.background は #rrggbb か null: {x!r}")
            elif k == "name":
                if x is not None and not (isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 255):
                    raise ValueError(f"state.colors.{s}.name は 0〜255 か null: {x!r}")
            else:
                raise ValueError(f"state.colors.{s} に知らないキー: {k}")
            out[s][k] = x
    return out


def background_for(state, colors, base):
    """その状態のペインの背景色。指定が無い（None）か知らない状態なら元の色。"""
    c = (colors.get(state) or {}).get("background") if state in STATES else None
    return c or base


def paint(text, state, colors):
    """ステータス行の名前に色を付ける（256 色の ANSI）。色の指定が無ければそのまま。"""
    n = (colors.get(state) or {}).get("name") if state in STATES else None
    return text if n is None else f"\033[38;5;{n}m{text}\033[0m"


DEFAULT_SUBAGENT_COLORS = {"running": 110, "ended": 245}  # 動いているもの: 控えめな青、終わって残っているもの: 灰色


def age_ja(seconds):
    """経った時間を短く（分・時間・日）。"""
    s = max(0, int(seconds))
    if s < 3600:
        return f"{s // 60}分"
    if s < 86400:
        return f"{s // 3600}時間"
    return f"{s // 86400}日"


def subagent_summary(rec, now, colors=None):
    """ステータス行の 1 区切り「サブ: 動作 N ／ 終了済み M（最古 X日）」。どちらも 0 なら空文字。

    終了済みは、背景のサブエージェントで、終わったあとも Claude Code の一覧に残っているもの
    （本体が TaskStop で止めたものは記録から外す）。
    """
    c = dict(DEFAULT_SUBAGENT_COLORS, **(colors or {}))
    raw = (rec or {}).get("agents")
    agents = [v for v in raw.values() if isinstance(v, dict)] if isinstance(raw, dict) else []
    running = [v for v in agents if v.get("end") is None]
    ended = [v["end"] for v in agents if isinstance(v.get("end"), (int, float))]
    if not running and not ended:
        return ""

    def col(n, text):
        return text if n is None else f"\033[38;5;{n}m{text}\033[0m"
    parts = []
    if running:
        parts.append(col(c["running"], f"動作 {len(running)}"))
    if ended:
        parts.append(col(c["ended"], f"終了済み {len(ended)}（最古 {age_ja(now - min(ended))}）"))
    return "サブ: " + " ／ ".join(parts)


def merge_subagent_colors(user):
    out = dict(DEFAULT_SUBAGENT_COLORS)
    if user is None:
        return out
    if not isinstance(user, dict):
        raise ValueError(f"state.subagent_colors がオブジェクトでない: {user!r}")
    for k, x in user.items():
        if k not in out or not (x is None or (isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 255)):
            raise ValueError(f"state.subagent_colors.{k} は 0〜255 か null: {x!r}")
        out[k] = x
    return out


def kitty_conf_background(text):
    """kitty.conf の中身から background の値（最後の指定）を返す。無ければ None。"""
    found = None
    for line in (text or "").splitlines():
        m = re.match(r"\s*background\s+(#[0-9a-fA-F]{6})\s*$", line)
        if m:
            found = m.group(1)
    return found


def duration(seconds):
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    return f"{s // 86400}d{s % 86400 // 3600}h"


def list_rows(entries, now):
    """state ファイルの中身のリストを、表示用の dict に（待受を先頭に、長く続いている順）。"""
    order = {"waiting": 0, "done": 1, "working": 2, "idle": 3}
    rows = []
    for e in entries:
        st = e.get("state")
        since = e.get("since") if isinstance(e.get("since"), (int, float)) else now
        rows.append({"role": e.get("role") or (f"w{e['window']}" if e.get("window") is not None else "?"),
                     "state": st, "minutes": int(max(0, now - since) // 60), "for": duration(now - since),
                     "reason": e.get("reason"), "session": e.get("session_id"), "window": e.get("window")})
    rows.sort(key=lambda r: (order.get(r["state"], 4), -r["minutes"], str(r["role"])))
    return rows


def format_list(rows):
    if not rows:
        return "（記録されたセッションは無い）"
    w = max(len(str(r["role"])) for r in rows)
    out = []
    for r in rows:
        extra = f"  ({r['reason']})" if r["state"] == "waiting" and r.get("reason") else ""
        out.append(f"{str(r['role']).ljust(w)}  {str(r['state']).ljust(7)}  {r['for']:>6}{extra}")
    return "\n".join(out)


def repaint_ops(entries, sock, colors, base, alive=lambda e: True):
    """記録から塗り直す [(window id, 背景色)]。同じペインに記録が 2 つあれば新しい方。"""
    best = {}
    for e in entries:
        w = e.get("window")
        if e.get("socket") != sock or not isinstance(w, int) or not alive(e):
            continue
        if w not in best or (e.get("updated") or 0) > (best[w].get("updated") or 0):
            best[w] = e
    return [(w, background_for(e.get("state"), colors, base)) for w, e in sorted(best.items())]


def strip_mark(title):
    title = title or ""
    while title.startswith(MARK):
        title = title[len(MARK):]
    return title


def main_tab_ids(osw_list, left):
    """各 OS 窓の「main」タブ: 統括（role=left）のいるタブ。いなければその OS 窓のアクティブなタブ。"""
    out = set()
    for osw in osw_list:
        tabs = osw.get("tabs", [])
        hit = [t["id"] for t in tabs if any((w.get("user_vars") or {}).get("role") == left
                                            for w in t.get("windows", []))]
        if not hit:
            hit = [t["id"] for t in tabs if t.get("is_active")]
        out.update(hit)
    return out


def tab_mark_ops(osw_list, waiting_windows, left, clear=False):
    """タブの題の「●」を付け外しする操作 [(tab id, 新しい題)] を返す。

    main 以外のタブに waiting のペインがあれば付け、無ければ外す。clear なら全部外す（claude-view が
    standby タブを題で探す前など）。変える必要の無いタブは返さない。
    """
    mains = main_tab_ids(osw_list, left)
    waiting = set(waiting_windows or ())
    ops = []
    for osw in osw_list:
        for t in osw.get("tabs", []):
            title = t.get("title") or ""
            base = strip_mark(title)
            want = (not clear and t["id"] not in mains
                    and any(w.get("id") in waiting for w in t.get("windows", [])))
            new = MARK + base if want else base
            if new != title and base:
                ops.append((t["id"], new))
    return ops


def viewing(osw_list, wid):
    """ユーザーがそのペインを見ているか: OS 窓が前面で、そのタブが表示中で、ペインが入力先。

    ペインの is_focused はタブごとの「最後の入力先」なので、それだけでは決めない。
    kitty の窓が前面でなければ（別のアプリや別のデスクトップを見ていれば）見ていないとする。
    """
    hit = find_window(osw_list or [], wid)
    if not hit:
        return False
    osw, tab, w = hit
    return bool(osw.get("is_focused") and tab.get("is_active") and w.get("is_focused"))


def find_window(osw_list, wid):
    """(OS 窓, タブ, ペイン) か None。"""
    for osw in osw_list:
        for t in osw.get("tabs", []):
            for w in t.get("windows", []):
                if w.get("id") == wid:
                    return osw, t, w
    return None


def is_claude_argv(argv):
    if not argv:
        return False
    base = os.path.basename(argv[0])
    if base in ("claude", "claude.exe"):
        return True
    return base in ("node", "bun") and any("claude-code" in a or os.path.basename(a) == "claude" for a in argv[1:3])


def nested_claude(ancestors):
    """ancestors=[(argv, environ dict or None)]（近い順）。いちばん近い claude が別の claude の
    中から起動されたもの（環境に CLAUDECODE がある）なら True。

    ペインの中の claude が Bash ツールで起動した claude（-p など）が、親のペインの色を変えないように。
    """
    for argv, env in ancestors:
        if is_claude_argv(argv):
            # 値が空（CLAUDECODE= で起動された、など）なら入れ子ではない
            return bool(env) and bool(env.get("CLAUDECODE") or env.get("CLAUDE_CODE_CHILD_SESSION"))
    return False


def claude_pid(ancestors_with_pid):
    """[(pid, argv)] から、いちばん近い claude の pid（無ければ None）。"""
    for pid, argv in ancestors_with_pid:
        if is_claude_argv(argv):
            return pid
    return None

