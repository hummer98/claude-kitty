#!/usr/bin/env python3
"""~/.claude/settings.json の statusLine に claude-statusline を入れる・外す（tools/install.sh --statusline が使う）。

  python3 tools/merge_statusline.py --settings PATH --repo DIR [--uninstall] [--dry-run]

- statusLine が無いときだけ入れる。別の statusLine（自分のスクリプトなど）があれば置き換えず、"! " の行で知らせる
  （ステータス行は 1 つしか持てず、黙って消すと元に戻せないため。置き換えるなら docs/statusline.md の手順で手で）。
- claude-statusline かは command の最初の語のファイル名で見る（PATH の名前・~/.local/bin・リポジトリの絶対パスは同じ）。
- --uninstall は claude-statusline の statusLine だけを外す（別のものは触らない）。
- 書き方（バックアップ・一時ファイルからの置き換え・壊れた JSON では書かない）は tools/merge_hooks.py と同じ。

出力の印と終了コードは tools/merge_hooks.py と同じ（"+ " 足す、"- " 外す、"= " そのまま、"! " 注意。2 = settings.json が壊れている）。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from merge_hooks import Broken, command_for, load, program, write  # noqa: E402

TOOL = "claude-statusline"
REFRESH = 30  # 会話が止まっている間も、サブエージェントの数と週の残り時間を進めるため（秒）


def ours(sl):
    if not isinstance(sl, dict):
        return False
    p = program(sl.get("command")) if isinstance(sl.get("command"), str) else None
    return bool(p) and Path(p).name == TOOL


def install(data, repo):
    sl = data.get("statusLine")
    if ours(sl):
        return [f"= statusLine は {TOOL}（入っている）"], data
    if sl is not None:
        shown = sl.get("command") if isinstance(sl, dict) else sl
        return [f"! 別の statusLine がある（{shown!r}）。置き換えない（置き換えるなら docs/statusline.md の手順で手で）"], data
    new = dict(data)
    new["statusLine"] = {"type": "command", "command": command_for(repo, TOOL, []), "refreshInterval": REFRESH}
    return [f"+ statusLine: {new['statusLine']['command']}（refreshInterval {REFRESH}）"], new


def uninstall(data):
    sl = data.get("statusLine")
    if not ours(sl):
        return [f"= statusLine に {TOOL} は入っていない" + ("（別のものは触らない）" if sl is not None else "")], data
    new = {k: v for k, v in data.items() if k != "statusLine"}
    return [f"- statusLine を外す（{sl.get('command')}）"], new


def main(argv=None):
    ap = argparse.ArgumentParser(description="claude-statusline を settings.json の statusLine に入れる・外す")
    ap.add_argument("--settings", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        data = load(a.settings)
    except Broken as e:
        print(f"! settings.json を書き換えない: {e}")
        return 2
    lines, new = uninstall(data) if a.uninstall else install(data, a.repo)
    for line in lines:
        print(line)
    if new != data and not a.dry_run:
        backup = write(a.settings, new)
        if backup:
            print(f"  （前の settings.json は {backup}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
