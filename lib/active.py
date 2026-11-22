"""人が起きているか（どれかの役割のセッションで、人が打った最後の発話からの分数）を会話の記録から調べる（claude-active）。

claude-presence の last.json は hook が書くもので、hook が入っていない・落ちたときに古いままになる。こちらは Claude Code の
会話の記録（<設定の置き場>/projects/<フォルダ>/<session>.jsonl）そのものを読むので、hook に頼らない。

人の発話の見分け方（実測した形）。type が "user" の行のうち、
- 人: origin.kind == "human" / turnOrigin == "human"（promptSource は typed・suggestion_accepted・queued）
- 別のセッションからの知らせ（claude-relay の起こす知らせも）: origin.kind == "peer"、turnOrigin == "peer"
- 定期の見回り（CronCreate / ScheduleWakeup）: turnOrigin == "scheduled"、origin 無し、isMeta
- 背景の仕事の知らせ: origin.kind == "task-notification"
- 画像などの付き物: isMeta、turnCompanion。道具の結果: content がリスト
人とみなすのは、content が文字列で、origin.kind か turnOrigin が "human" の行だけ。origin も turnOrigin も無い行は
古い形式かもしれないので数えず、ほかに人の発話が無いときの「分からない」の材料にする。
自動の要約（isCompactSummary・isVisibleInTranscriptOnly。turnOrigin は要約を起こしたターンのものを引き継ぎ human になりうる）と、
会話の最初の発話（parentUuid が無く turnPosition.promptIndex が 1。統括が `claude "指示"` の引数で担当を起こすと human typed と
記録される【計測】）も数えない。人が新しい会話の最初に打ったものも落ちるが、away 寄りに倒れるだけ。
誤って「起きている」と答えないことを一番にする（寝ているのに人を呼ぶより、起きているのに呼ばない方がまし）。
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import proc
from presence import MACHINE_MARKS, MACHINE_PREFIXES

JST = timezone(timedelta(hours=9), "JST")
DEFAULT_MINUTES = 20
# 末尾から読む量。人の発話が見つからなければ前へ広げる（最後は全体）。数十 MB の記録があるため
SPANS = (2 * 1024 * 1024, 16 * 1024 * 1024)
# いまよりこれ以上先の時刻の行は使わない（時計のずれ・壊れた timestamp で、その時刻まで active が続かないように）
FUTURE_SLACK = 120
# origin も turnOrigin も無いが、古い形式ではなく Claude Code が今も書く機械の行（! コマンドの入力・スラッシュコマンドの
# 名前と出力）。「古い形式」に数えると unknown の理由が誤解を招くので、黙って飛ばす。<bash-stdout> は turnOrigin が human で
# 人の ! コマンドの結果なので数える
SILENT_PREFIXES = ("<bash-input", "<bash-stderr", "<command-name", "<command-message", "<command-args",
                   "<local-command-stdout", "<local-command-stderr", "<local-command-caveat")


def is_human_line(d):
    """記録の 1 行（dict）が人の発話なら True、人でなければ False、古い形式で分からなければ None。"""
    if not isinstance(d, dict) or d.get("type") != "user":
        return False
    msg = d.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, str):
        return False  # 道具の結果（リスト）など
    if d.get("isMeta") or d.get("turnCompanion") or d.get("isCompactSummary") or d.get("isVisibleInTranscriptOnly"):
        return False
    pos = d.get("turnPosition")
    if d.get("parentUuid") is None and isinstance(pos, dict) and pos.get("promptIndex") == 1:
        return False  # 起動の引数で渡した最初の指示（統括が担当を起こしたとき）と見分けられない
    origin = d.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    turn = d.get("turnOrigin")
    if kind is None and turn is None:
        return False if content.lstrip().startswith(SILENT_PREFIXES) else None
    if kind not in (None, "human") or turn not in (None, "human"):
        return False  # 片方でも人でないと書いてあれば人でない
    body = content.lstrip()
    if body.startswith(MACHINE_PREFIXES) or any(m in content for m in MACHINE_MARKS):
        return False
    return True


def parse_ts(s):
    """記録の timestamp（"2026-11-22T22:13:04.854Z"）を epoch 秒に。読めなければ None。"""
    if not isinstance(s, str) or not s:
        return None
    try:
        t = datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
    except ValueError:
        return None
    if t.tzinfo is None:
        return None  # 時差の分からない時刻は使わない
    return t.timestamp()


def scan_lines(lines, now=None):
    """行（bytes か str）の並びから (最後の人の発話の時刻, 古い形式の行があったか)。now より FUTURE_SLACK 秒以上先の行は使わない。

    promptSource が queued の行（作業中に打って待たせた発話）の timestamp は会話に入った時刻で、人が打った時刻ではない
    （実例で 15.7 分遅れ）。その前の同じ本文の queue-operation の enqueue の時刻を使い、見つからなければ数えない（away 側）。"""
    best, legacy = None, False
    limit = None if now is None else now + FUTURE_SLACK
    queue = []  # [(本文, enqueue の時刻)]。待ち行列は先に入れたものから出る
    for line in lines:
        if isinstance(line, bytes):
            # 速さのため: 人の行には必ず "user"、待ち行列の行には "queue-operation" が入る。デコードと JSON の読みは候補だけ
            if b'"user"' not in line and b'"queue-operation"' not in line:
                continue
            line = line.decode("utf-8", errors="replace")
        elif '"user"' not in line and '"queue-operation"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("type") == "queue-operation":
            text = d.get("content")
            if not isinstance(text, str):
                continue
            if d.get("operation") == "enqueue":
                queue.append((text.strip(), parse_ts(d.get("timestamp"))))
            elif d.get("operation") == "remove":  # 出さずに取り除いた（Esc など）
                _take(queue, text)
            continue
        h = is_human_line(d)
        if h is None:
            legacy = True
            continue
        if h:
            if d.get("promptSource") == "queued":
                t = _take(queue, d["message"]["content"])
            else:
                t = parse_ts(d.get("timestamp"))
            if t is not None and (limit is None or t <= limit) and (best is None or t > best):
                best = t
    return best, legacy


def _take(queue, text):
    """待ち行列から同じ本文の一番古いものを取り出し、その enqueue の時刻を返す（無ければ None）。"""
    text = text.strip()
    for i, (q, t) in enumerate(queue):
        if q == text:
            del queue[i]
            return t
    return None


def last_human(path, spans=SPANS, not_before=None, now=None):
    """記録のファイルの、人が打った最後の発話の時刻を末尾から探す。

    返り値 (時刻か None, 古い形式の行があったか)。読めなければ OSError を投げる。
    not_before: この時刻より前しか無いと分かった時点で探すのをやめる（ほかのセッションでもっと新しいのが見つかっている）。"""
    size = os.path.getsize(path)
    legacy = False
    spans = [s for s in spans if s < size] + [size]
    with open(path, "rb") as fh:
        for span in spans:
            start = max(0, size - span)
            fh.seek(start)
            lines = fh.read(size - start).split(b"\n")
            if start > 0:
                lines = lines[1:]  # 途中から読んだ最初の行は切れている
            best, leg = scan_lines(lines, now)
            legacy = legacy or leg
            if best is not None:
                return best, legacy
            if start == 0:
                break
            first = _first_ts(lines) if not_before is not None else None
            if first is not None and first < not_before:
                break  # ここより前は、見つかっている発話より古い
    return None, legacy


def _first_ts(lines):
    for line in lines:
        try:
            t = parse_ts(json.loads(line).get("timestamp"))
        except (ValueError, AttributeError):
            continue
        if t is not None:
            return t
    return None


def transcript_files(sid, projects_dirs):
    """session ID の会話の記録。フォルダをまたいで再開すると元のフォルダの側に書き足されるので、全フォルダから探す。"""
    out = []
    for p in projects_dirs:
        try:
            out += [f for f in Path(p).glob(f"*/{sid}.jsonl") if f.is_file()]
        except OSError:
            continue
    return out


def live_sessions(sessions_dirs, start_time=None, matches=None):
    """<置き場>/sessions/*.json のうち、プロセスが生きているもの（claude-roster の live_sessions と同じ決まり:
    procStart で pid の再利用を除く。Linux 以外で起動時刻が読めなくても生きていれば残す）。"""
    start_time = start_time or _start_time
    matches = matches or proc.proc_start_matches
    out, seen = [], set()
    for d in sessions_dirs:
        try:
            files = sorted(Path(d).glob("*.json"))
        except OSError:
            continue
        for f in files:
            try:
                data = json.loads(f.read_text())
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            if not isinstance(data, dict) or not isinstance(data.get("sessionId"), str):
                continue
            pid = data.get("pid") or (int(f.stem) if f.stem.isdigit() else None)
            st = start_time(pid) if isinstance(pid, int) else None
            if st is None or not matches(data.get("procStart"), st):
                continue
            if data["sessionId"] in seen:
                continue
            seen.add(data["sessionId"])
            data["pid"] = pid
            out.append(data)
    return out


def _start_time(pid):
    s = proc.start_time(pid)
    if s is None and not proc.is_linux() and proc.alive(pid):
        return "alive"
    return s


def roster_roles(roster):
    """名簿（roster.json の中身）から {session_id: 役割} と、記録された設定の置き場の一覧。"""
    roles, dirs = {}, []
    sessions = roster.get("sessions") if isinstance(roster, dict) else None
    for role, e in (sessions or {}).items():
        if not isinstance(e, dict):
            continue
        if isinstance(e.get("session_id"), str):
            roles[e["session_id"]] = role
        cd = e.get("config_dir")
        if isinstance(cd, str) and cd.startswith("/") and cd not in dirs:
            dirs.append(cd)
    return roles, dirs


def find_last(sessions, role_of, projects_dirs, scan=last_human, now=None):
    """役割のある生きたセッションのうち、人が最後に打ったもの。

    sessions: live_sessions の返り値。role_of(sid) → 役割（無ければ None。役割の無いセッションは見ない）。
    返り値 {"at", "role", "session_id", "seen", "unreadable", "legacy", "missing"}（at が None なら見つからない）。"""
    cands = []
    for s in sessions:
        sid = s.get("sessionId")
        role = role_of(sid)
        if not role:
            continue
        files = transcript_files(sid, projects_dirs)
        cands.append((sid, role, files))
    res = {"at": None, "role": None, "session_id": None, "seen": len(cands), "unreadable": 0, "legacy": 0, "missing": 0}

    def mtime(f):
        try:
            return f.stat().st_mtime
        except OSError:
            return 0.0
    # 新しく書かれたファイルから。ファイルの更新時刻より新しい行は無いので、見つかった時刻より古いファイルは読まない
    items = sorted(((mtime(f), sid, role, f) for sid, role, files in cands for f in files), key=lambda x: -x[0])
    for sid, role, files in cands:
        if not files:
            res["missing"] += 1
    for mt, sid, role, f in items:
        if res["at"] is not None and mt and mt < res["at"]:
            continue
        try:
            t, legacy = scan(f, not_before=res["at"], now=now)
        except OSError:
            res["unreadable"] += 1
            continue
        if legacy:
            res["legacy"] += 1
        if t is not None and (res["at"] is None or t > res["at"]):
            res.update(at=t, role=role, session_id=sid)
    return res


def judge(res, now, minutes=DEFAULT_MINUTES):
    """find_last の結果から {"status": active/away/unknown, "code": 0/1/2, ...}。"""
    out = dict(res, threshold_minutes=minutes)
    if res["at"] is None:
        why = "人の発話が見つからない"
        if res["seen"] == 0:
            why = "役割のある動いているセッションが無い"
        notes = []
        if res["legacy"]:
            notes.append(f"出どころの無い古い形式の行 {res['legacy']}")
        if res["unreadable"]:
            notes.append(f"読めない記録 {res['unreadable']}")
        if res["missing"]:
            notes.append(f"記録が無い {res['missing']}")
        out.update(status="unknown", code=2, minutes=None, reason=why, notes=notes)
        return out
    m = max(0.0, (now - res["at"]) / 60.0)  # 少し先（FUTURE_SLACK 以内）の記録は 0
    active = m <= minutes
    out.update(status="active" if active else "away", code=0 if active else 1, minutes=round(m, 1),
               at_jst=datetime.fromtimestamp(res["at"], JST).isoformat(timespec="seconds"))
    return out


def format_line(j):
    if j["status"] == "unknown":
        extra = "、".join([f"見たセッション {j['seen']}"] + j.get("notes", []))
        return f"unknown {j['reason']}（{extra}）"
    hm = datetime.fromtimestamp(j["at"], JST).strftime("%H:%M")
    return f"{j['status']} {int(j['minutes'])}分前 {j['role']}（{hm} JST）"
