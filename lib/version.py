"""版の番号。リポジトリの直下の VERSION を、すべてのコマンドの --version がここから読む（番号を 1 か所で上げるため）。

  python3 lib/version.py PROG   "PROG 0.2.0" を出す（bash のコマンド用）
"""
import sys
from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def version(path=VERSION_FILE):
    """VERSION の 1 行目。読めなければ "unknown"（--version のためにコマンドを落とさない）。"""
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return "unknown"
    return text.splitlines()[0].strip() if text else "unknown"


def handle_version(argv, prog, out=None):
    """argv（プログラム名を除く）が --version だけなら "PROG 版" を出して True。それ以外は何もしない。"""
    if list(argv) != ["--version"]:
        return False
    print(f"{prog} {version()}", file=out or sys.stdout)
    return True


if __name__ == "__main__":
    print(f"{sys.argv[1] if len(sys.argv) > 1 else 'claude-kitty'} {version()}")
