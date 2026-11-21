#!/usr/bin/env python3
"""~/.claude/settings.json の hooks に、claude-kitty の hook を足す・外す・調べる（tools/install.sh と claude-kitty doctor が使う）。

  python3 tools/merge_hooks.py --settings PATH --repo DIR [--parts state,lang,guard,relay] [--uninstall] [--dry-run] [--check]

- 既存のキーと既存の hook は消さない。同じ道具の同じ hook（下の「同じ」）が既にあれば足さない。
- 「同じ」は command の最初の語のファイル名と、残りの引数で比べる（`claude-state hook Stop` と
  `~/.local/bin/claude-state hook Stop` と `/x/claude-kitty/bin/claude-state hook Stop` は同じ）。
  手で入れた形（PATH の名前・リンク・リポジトリの絶対パス）をどれも「入っている」と数えるため。
- 書く前に settings.json.bak-<日時> を作り、同じフォルダの一時ファイルに書いてから置き換える（途中で止まっても壊れない）。
  settings.json がシンボリックリンクなら、リンク先を書き換える（リンクは残す）。
- JSON が読めない・形が違う（hooks がオブジェクトでないなど）ときは何も書かずに exit 2。
- --uninstall は、同じ道具の hook を外す（手で入れたものも）。空になった組とイベントは消す。

出力は 1 行 1 件で、頭に印: "+ " 足す、"- " 外す、"= " 入っている（または入っていない）、"! " 問題。
終了コード: 0 = できた（--dry-run と --check では変えるものの有無を問わない）、2 = settings.json が壊れている、1 = 引数の誤り。
"""
import argparse
import json
import os
import re
import shlex
import sys
import tempfile
import time
from pathlib import Path

# docs（SETUP.md の 5.、docs/lang-check.md、docs/guard-hook.md）と同じ設定。(イベント, matcher, 引数)
STATE_EVENTS = [
    ("UserPromptSubmit", None), ("Stop", None), ("StopFailure", None),
    ("Notification", "permission_prompt|elicitation_dialog|elicitation_url_dialog|agent_needs_input|idle_prompt"),
    ("PreToolUse", "*"), ("PermissionRequest", "*"), ("SubagentStart", None), ("SubagentStop", None),
    ("PostToolUse", "*"), ("PostToolUseFailure", "*"), ("SessionStart", None), ("SessionEnd", None),
]
PARTS = {
    "state": [(ev, m, "claude-state", ["hook", ev]) for ev, m in STATE_EVENTS],
    "lang": [("Stop", None, "claude-lang-check", [])],
    "guard": [("PreToolUse", "Bash|Edit|Write|NotebookEdit", "claude-guard", ["hook"])],
    # 別の端末からのメッセージを受け取りの役割のセッションに渡す（docs/relay.md。--hooks=relay と明示したときだけ）
    "relay": [("UserPromptSubmit", None, "claude-relay", ["hook", "UserPromptSubmit"]),
              ("Stop", None, "claude-relay", ["hook", "Stop"]),
              # 起こすための socket を、始まったばかり（まだ 1 ターンもしていない）のセッションでも記録する
              ("SessionStart", None, "claude-relay", ["hook", "SessionStart"])],
}
TIMEOUT = 10


class Broken(Exception):
    """settings.json が読めない・形が違う。書かずに止める。"""


def identity(command):
    """command の (道具のファイル名, 引数のタプル)。読めない command は None。"""
    if not isinstance(command, str):
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
        words = words[1:]                 # 頭の VAR=値 は同じ道具の呼び方の違い
    if not words:
        return None
    return os.path.basename(words[0]), tuple(words[1:])


def wanted(parts):
    out = []
    for p in parts:
        out.extend(PARTS[p])
    return out


def load(path):
    """settings.json を dict で返す。無ければ {}。壊れていれば Broken。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise Broken(f"読めない: {path}: {e}")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as e:
        raise Broken(f"JSON として読めない: {path}: {e}")
    if not isinstance(data, dict):
        raise Broken(f"中身がオブジェクトでない: {path}")
    check_shape(data, path)
    return data


def check_shape(data, path="settings.json"):
    hooks = data.get("hooks")
    if hooks is None:
        return
    if not isinstance(hooks, dict):
        raise Broken(f"{path} の hooks がオブジェクトでない")
    for ev, groups in hooks.items():
        if not isinstance(groups, list):
            raise Broken(f"{path} の hooks.{ev} がリストでない")
        for g in groups:
            if not isinstance(g, dict) or not isinstance(g.get("hooks", []), list):
                raise Broken(f"{path} の hooks.{ev} の組の形が違う: {g!r}")


def present(data, event, tool, args):
    """そのイベントに同じ道具・同じ引数の hook があるか（matcher は問わない）。"""
    for g in (data.get("hooks") or {}).get(event, []):
        for h in g.get("hooks", []):
            if isinstance(h, dict) and identity(h.get("command")) == (tool, tuple(args)):
                return True
    return False


def commands(data, part):
    """その部品の道具を呼んでいる hook の command の文字列（重なりなし、出てきた順）。doctor が道具の有無を調べる用。"""
    tools = {tool for _ev, _m, tool, _a in PARTS[part]}
    out = []
    for groups in (data.get("hooks") or {}).values():
        for g in groups if isinstance(groups, list) else []:
            for h in g.get("hooks", []) if isinstance(g, dict) else []:
                c = h.get("command") if isinstance(h, dict) else None
                ident = identity(c)
                if ident and ident[0] in tools and c not in out:
                    out.append(c)
    return out


def program(command):
    """command の最初の語（頭の VAR=値 を除く）。読めなければ None。"""
    try:
        words = shlex.split(command)
    except (ValueError, AttributeError):
        return None
    while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
        words = words[1:]
    return words[0] if words else None


def status(data):
    """部品ごとに {"part": ("all" | "some" | "none", [足りないイベント])}。doctor 用。"""
    out = {}
    for part, items in PARTS.items():
        missing = [ev for ev, _m, tool, args in items if not present(data, ev, tool, args)]
        out[part] = ("none" if len(missing) == len(items) else "some" if missing else "all", missing)
    return out


def command_for(repo, tool, args):
    """書く command。PATH に頼らないよう、リポジトリの bin の絶対パスで書く（Claude Code を GUI や別のシェルから
    起動して ~/.local/bin が PATH に無くても、hook が黙って失敗しないように）。"""
    return " ".join([shlex.quote(str(Path(repo) / "bin" / tool))] + [shlex.quote(a) for a in args])


def install(data, repo, parts):
    """足した hook の説明のリストと、新しい dict を返す（data は変えない）。"""
    data = json.loads(json.dumps(data))
    lines = []
    for ev, matcher, tool, args in wanted(parts):
        label = f"{ev}: {tool} {' '.join(args)}".rstrip()
        if present(data, ev, tool, args):
            lines.append(f"= {label}（入っている）")
            continue
        group = {"hooks": [{"type": "command", "command": command_for(repo, tool, args), "timeout": TIMEOUT}]}
        if matcher is not None:
            group = {"matcher": matcher, **group}
        data.setdefault("hooks", {}).setdefault(ev, []).append(group)
        lines.append(f"+ {label}")
    return lines, data


def uninstall(data, parts):
    data = json.loads(json.dumps(data))
    lines = []
    hooks = data.get("hooks")
    for ev, _matcher, tool, args in wanted(parts):
        label = f"{ev}: {tool} {' '.join(args)}".rstrip()
        groups = hooks.get(ev) if isinstance(hooks, dict) else None
        if not groups:
            lines.append(f"= {label}（入っていない）")
            continue
        removed = 0
        kept_groups = []
        for g in groups:
            hs = g.get("hooks", [])
            kept = [h for h in hs if not (isinstance(h, dict) and identity(h.get("command")) == (tool, tuple(args)))]
            removed += len(hs) - len(kept)
            if kept or not hs:
                kept_groups.append(dict(g, hooks=kept) if hs else g)
        if removed:
            lines.append(f"- {label}")
        else:
            lines.append(f"= {label}（入っていない）")
        if kept_groups:
            hooks[ev] = kept_groups
        else:
            del hooks[ev]
    if isinstance(hooks, dict) and not hooks:
        del data["hooks"]
    return lines, data


def write(path, data, now=None):
    """バックアップを取ってから、一時ファイル経由で置き換える。バックアップのパス（新しく作ったときは None）を返す。"""
    path = Path(path)
    target = Path(os.path.realpath(path)) if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    mode = 0o600
    if target.exists():
        st = target.stat()
        mode = st.st_mode & 0o777
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        backup = target.with_name(f"{target.name}.bak-{stamp}")
        n = 1
        while backup.exists():
            backup = target.with_name(f"{target.name}.bak-{stamp}-{n}")
            n += 1
        backup.write_bytes(target.read_bytes())
        os.chmod(backup, mode)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return backup


def parse_parts(text):
    # 省いたときは relay（使う人だけのもの）を除いた全部
    parts = [p.strip() for p in (text or "").split(",") if p.strip()] or [p for p in PARTS if p != "relay"]
    bad = [p for p in parts if p not in PARTS]
    if bad:
        raise ValueError(f"知らない hook: {', '.join(bad)}（使えるのは {', '.join(PARTS)}）")
    return list(dict.fromkeys(parts))


def main(argv=None):
    ap = argparse.ArgumentParser(description="claude-kitty の hook を settings.json に足す・外す")
    ap.add_argument("--settings", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--parts", default="")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true", help="部品ごとの入り具合を JSON で出す（書かない）")
    a = ap.parse_args(argv)
    try:
        parts = parse_parts(a.parts)
    except ValueError as e:
        print(f"! {e}", file=sys.stderr)
        return 1
    try:
        data = load(a.settings)
    except Broken as e:
        print(f"! settings.json を書き換えない: {e}")
        return 2
    if a.check:
        print(json.dumps({k: {"state": v[0], "missing": v[1]} for k, v in status(data).items()}, ensure_ascii=False))
        return 0
    lines, new = uninstall(data, parts) if a.uninstall else install(data, a.repo, parts)
    for line in lines:
        print(line)
    if new != data and not a.dry_run:
        backup = write(a.settings, new)
        if backup:
            print(f"  （前の settings.json は {backup}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
