"""KWin スクリプトを一回だけ実行して結果を受け取る（claude-browser / claude-view 共通）。"""
import os
import subprocess
import sys
import time
from pathlib import Path

from core import parse_journal

STATE_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "claude-browser"


def die(msg):
    print(f"{Path(sys.argv[0]).name}: {msg}", file=sys.stderr)
    sys.exit(1)


def kwin(js_body):
    """KWin スクリプトを実行し、console.info の出力（トークン付き行）を返す。"""
    token = f"cb{os.getpid()}x{time.time_ns()}"
    js = js_body.replace("@@OUT@@", f'console.info("{token} " + ')
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{token}.js"
    path.write_text(js)
    since = int(time.time()) - 1
    try:
        sid = subprocess.run(["qdbus", "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.loadScript",
                              str(path), token], capture_output=True, text=True).stdout.strip()
        if not sid.lstrip("-").isdigit() or int(sid) < 0:
            die(f"KWin スクリプトを読み込めない: {sid!r}")
        subprocess.run(["qdbus", "org.kde.KWin", f"/Scripting/Script{sid}", "org.kde.kwin.Script.run"],
                       capture_output=True)
        for _ in range(40):  # journal への反映を待つ（最大 6 秒）
            time.sleep(0.15)
            j = subprocess.run(["journalctl", "--user", "-o", "cat", f"--since=@{since}"],
                               capture_output=True, text=True).stdout
            lines, ended = parse_journal(j, token)
            if ended:
                return lines
        die("KWin スクリプトの応答が journal に出ない")
    finally:
        subprocess.run(["qdbus", "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.unloadScript", token],
                       capture_output=True)
        path.unlink(missing_ok=True)




def active_window_pid():
    out = kwin("""
const a = workspace.activeWindow;
@@OUT@@String(a ? a.pid : -1));
@@OUT@@"END");""")
    return int(out[0]) if out else -1
