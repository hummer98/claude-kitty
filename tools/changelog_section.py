#!/usr/bin/env python3
"""CHANGELOG.md から 1 つの版の節を取り出して標準出力に出す（GitHub の release の本文と、tools/release.sh の確認用）。

  tools/changelog_section.py 0.2.0 [--file CHANGELOG.md]    v0.2.0 でもよい

出すのは見出し「## [0.2.0] - YYYY-MM-DD」の次の行から、次の「## 」の見出しの前まで（見出しそのものは出さない。
release の題に版が入るため）。末尾の比較リンクの定義（[0.2.0]: https://…）は節の中身ではないので除く。
終了コード: 0 = 出せた、1 = その版の節が無いか中身が空、2 = 引数かファイルが正しくない。
"""
import re
import sys
from pathlib import Path

HEADING = re.compile(r"^## \[(?P<ver>[^\]]+)\](?:\s+-\s+(?P<date>\S+))?\s*$")
LINK_DEF = re.compile(r"^\[[^\]]+\]:\s+\S+\s*$")


def normalize(ver):
    """"v0.2.0" も "0.2.0" として扱う（タグの名前をそのまま渡せるように）。"""
    ver = ver.strip()
    return ver[1:] if ver[:1] in ("v", "V") else ver


def headings(text):
    """[(版, 日付か None)]（上から順）。Unreleased も含む。"""
    out = []
    for line in text.splitlines():
        m = HEADING.match(line)
        if m:
            out.append((m["ver"], m["date"]))
    return out


def section(text, ver):
    """版の節の本文（前後の空行を除く）。見出しが無ければ None。"""
    ver = normalize(ver)
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        m = HEADING.match(line)
        if start is None:
            if m and m["ver"] == ver:
                start = i + 1
        elif m or line.startswith("## "):
            lines = lines[start:i]
            break
    else:
        if start is None:
            return None
        lines = lines[start:]
    # 末尾の比較リンクの定義と空行を落とす（最後の節の後ろに置くため）
    while lines and (not lines[-1].strip() or LINK_DEF.match(lines[-1])):
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines)


def main(argv):
    args = list(argv)
    path = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
    if "--file" in args:
        i = args.index("--file")
        if i + 1 >= len(args):
            print("usage: changelog_section.py <version> [--file CHANGELOG.md]", file=sys.stderr)
            return 2
        path = Path(args[i + 1])
        del args[i:i + 2]
    if len(args) != 1 or not args[0].strip():
        print("usage: changelog_section.py <version> [--file CHANGELOG.md]", file=sys.stderr)
        return 2
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        print(f"changelog_section.py: {path} を読めない: {e}", file=sys.stderr)
        return 2
    body = section(text, args[0])
    if body is None:
        print(f"changelog_section.py: {path.name} に「## [{normalize(args[0])}]」の節が無い", file=sys.stderr)
        return 1
    if not body.strip():
        print(f"changelog_section.py: 「## [{normalize(args[0])}]」の節が空", file=sys.stderr)
        return 1
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
