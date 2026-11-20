"""KWin スクリプトを一回だけ実行して結果を受け取る（claude-browser / claude-view 共通）。

窓の操作は KDE Plasma の KWin だけ（qdbus でスクリプトを読ませ、結果を journalctl --user から読む）。
KWin が無い環境では、何が足りないかを書いて止める（die）。"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from core import parse_journal
from rundir import runtime_dir

STATE_DIR = runtime_dir() / "claude-browser"


def die(msg):
    print(f"{Path(sys.argv[0]).name}: {msg}", file=sys.stderr)
    sys.exit(1)


# Plasma 6 の qdbus は、ディストリビューションによって qdbus6・qdbus-qt6 の名前で入る
QDBUS_NAMES = ("qdbus", "qdbus6", "qdbus-qt6")
NO_KWIN = "窓の配置は KDE Plasma の KWin だけで動く（ほかのデスクトップは SETUP.md の移植の手引き）"


def qdbus_path(env, which=shutil.which):
    """qdbus の場所: $CLAUDE_KITTY_QDBUS > PATH の qdbus・qdbus6・qdbus-qt6。無ければ None。"""
    if env.get("CLAUDE_KITTY_QDBUS"):
        return env["CLAUDE_KITTY_QDBUS"]
    for name in QDBUS_NAMES:
        found = which(name, path=env.get("PATH"))
        if found:
            return found
    return None


def kwin(js_body):
    """KWin スクリプトを実行し、console.info の出力（トークン付き行）を返す。"""
    qdbus = qdbus_path(os.environ)
    if not qdbus:
        die(f"qdbus（{'・'.join(QDBUS_NAMES)}）が無いので KWin を呼べない。{NO_KWIN}")
    if not shutil.which("journalctl", path=os.environ.get("PATH")):
        die(f"journalctl が無いので KWin スクリプトの結果を受け取れない。{NO_KWIN}")
    token = f"cb{os.getpid()}x{time.time_ns()}"
    js = js_body.replace("@@OUT@@", f'console.info("{token} " + ')
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{token}.js"
    path.write_text(js)
    since = int(time.time()) - 1
    try:
        try:
            r = subprocess.run([qdbus, "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.loadScript",
                                str(path), token], capture_output=True, text=True)
        except OSError as e:
            die(f"qdbus を動かせない（{qdbus}: {e}）。{NO_KWIN}")
        sid = r.stdout.strip()
        if not sid.lstrip("-").isdigit() or int(sid) < 0:
            # KWin が動いていない（KDE 以外のデスクトップ）と、qdbus は何も返さずにエラーを出す
            why = (r.stderr or "").strip().splitlines()
            die(f"KWin スクリプトを読み込めない（KWin が動いていない？）: {sid or (why[-1] if why else '応答なし')!r}。{NO_KWIN}")
        subprocess.run([qdbus, "org.kde.KWin", f"/Scripting/Script{sid}", "org.kde.kwin.Script.run"],
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
        try:
            subprocess.run([qdbus, "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.unloadScript", token],
                           capture_output=True)
        except OSError:
            pass
        path.unlink(missing_ok=True)




def active_window_pid():
    out = kwin("""
const a = workspace.activeWindow;
@@OUT@@String(a ? a.pid : -1));
@@OUT@@"END");""")
    return int(out[0]) if out else -1
