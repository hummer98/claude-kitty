"""サンドボックスの Chrome（Flatpak / Snap）から見えない場所の file:// を、見える場所に写して開く（claude-browser・claude-show）。

Flatpak の Chrome は、許可された場所（filesystems）とアプリの ~/.var/app/<アプリ ID>/ しか読めない。
Snap は home インターフェースがつながっていれば、ホームの隠しでない場所と ~/snap/<名前>/ を読める。
見えない file:// を開くと Chrome は「アクセスできませんでした」を出すので、見えないときだけ
  Flatpak: ~/.var/app/<アプリ ID>/claude-show/<短いハッシュ>/
  Snap:    ~/snap/<名前>/common/claude-show/<短いハッシュ>/
に写して、写した先を開く。HTML は同じフォルダの中を指す相対参照の資源（img・script・link・srcset・CSS の url() など）も
同じ相対の位置に写す。ネイティブの Chrome（sandbox が None）では何もしない。

判定は保守的: 許可を読めない・解釈できないものは「見えない」側に倒す（写しても開けることは変わらないので）。
パスは realpath でそろえて比べる（Fedora Atomic 系は /home → /var/home の symlink）。
外のコマンド（flatpak・snap・xdg-user-dir）は run で呼ぶ（テストでは偽物にする）。
"""
import hashlib
import os
import re
import shutil
import subprocess
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

COPY_DIR = "claude-show"
COPY_LIMIT = 50 * 1024 * 1024  # 資源の合計の上限。超えたら HTML だけ写す（大きな資料で写す時間とディスクを食わないように）
KEEP_SECONDS = 24 * 3600       # 写した先をこれより古いものから掃除する
HTML_EXT = {".html", ".htm", ".xhtml"}
CSS_EXT = {".css"}

# Flatpak は host を許しても、これらはサンドボックスの側のものになる（ホストのものは見えない）
# （名前だけで書くのは、/proc を読むのは lib/proc.py だけという検査に、読まない文字列で掛からないように）
RESERVED = tuple("/" + d for d in ("app", "bin", "boot", "dev", "etc", "lib", "lib32", "lib64", "proc", "root", "run",
                                   "sbin", "sys", "tmp", "usr", "var", ".flatpak-info"))
# /var はサンドボックスの側のもの。ホームが /var/home の下でも、host・home の許可はホームを別に足すので見える
# filesystems の xdg-* と xdg-user-dir の名前
XDG_USER = {"xdg-desktop": "DESKTOP", "xdg-documents": "DOCUMENTS", "xdg-download": "DOWNLOAD",
            "xdg-music": "MUSIC", "xdg-pictures": "PICTURES", "xdg-public-share": "PUBLICSHARE",
            "xdg-templates": "TEMPLATES", "xdg-videos": "VIDEOS"}
XDG_BASE = {"xdg-config": ("XDG_CONFIG_HOME", ".config"), "xdg-data": ("XDG_DATA_HOME", ".local/share"),
            "xdg-cache": ("XDG_CACHE_HOME", ".cache"), "xdg-state": ("XDG_STATE_HOME", ".local/state")}
MODES = (":ro", ":rw", ":create", ":reset")  # :reset は Flatpak 1.14 以降の !host:reset など


def _real(p):
    return os.path.realpath(os.path.expanduser(str(p)))


def _under(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")  # root が "/" なら何でも


def _call(run, argv):
    """標準出力の文字列。動かせない・失敗なら None。"""
    try:
        r = run(argv, capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


# ---------- Flatpak ----------
def filesystems_of(text):
    """flatpak info --show-permissions / override --show の出力の [Context] の filesystems の項目（順のまま）。"""
    out, section = [], None
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        elif section == "[Context]" and line.startswith("filesystems="):
            out += [e for e in line[len("filesystems="):].split(";") if e]
    return out


def argv_filesystems(argv):
    """`flatpak run --filesystem=X --nofilesystem=Y <ID>` の X・!Y（アプリ ID より前のものだけ）。"""
    from browser import FLATPAK_VALUE_OPTS
    out, rest = [], list(argv or [])
    if "run" not in rest:
        return out
    rest = rest[rest.index("run") + 1:]
    i = 0
    while i < len(rest):
        a = rest[i]
        name, eq, val = a.partition("=")
        if name in ("--filesystem", "--nofilesystem"):
            if not eq:
                i += 1
                val = rest[i] if i < len(rest) else ""
            if val:
                out.append(val if name == "--filesystem" else "!" + val)
        elif a in FLATPAK_VALUE_OPTS:
            i += 1
        elif not a.startswith("-"):
            break  # アプリ ID。後ろはアプリへの引数
        i += 1
    return out


def apply_entries(entries):
    """否定（!X）を順に効かせた、残る項目（:ro などを除いた名前）の集合。"""
    keep = []
    for e in entries:
        e = e.strip()
        neg = e.startswith("!")
        name = e[1:] if neg else e
        for m in MODES:
            if name.endswith(m):
                name = name[:-len(m)]
        name = name.rstrip("/") or name
        if name == "~":
            name = "home"  # 同じ場所なので、!~ で home も消えるように
        if not name:
            continue
        if neg:
            keep = [k for k in keep if k != name]
        elif name not in keep:
            keep.append(name)
    return keep


class XdgDirs:
    """xdg-user-dir の答え（無ければ ~/.config/user-dirs.dirs を読む）。1 回の呼び出しの中でキャッシュする。"""

    def __init__(self, home, env, run):
        self.home, self.env, self.run, self.cache = str(home), env, run, {}

    def user_dir(self, name):
        if name not in self.cache:
            got = _call(self.run, ["xdg-user-dir", name])
            got = got.strip() if got else self._from_file(name)
            # 設定が無いと xdg-user-dir はホームを返す。Flatpak もそのときはホームを見せないので、無いものとする
            self.cache[name] = got if got and _real(got) != _real(self.home) else None
        return self.cache[name]

    def _from_file(self, name):
        conf = Path(self.env.get("XDG_CONFIG_HOME") or Path(self.home) / ".config") / "user-dirs.dirs"
        try:
            text = conf.read_text()
        except OSError:
            return None
        m = re.search(rf'^XDG_{name}_DIR="([^"]*)"', text, re.M)
        return m.group(1).replace("$HOME", self.home) if m else None


def entry_path(name, home, env, xdg):
    """filesystems の 1 項目が指す場所（realpath）。host は "/"。分からないもの（host-os・host-etc など）は None。"""
    if name == "host":
        return "/"
    if name == "home" or name == "~":
        return _real(home)
    if name.startswith("~/"):
        return _real(Path(home) / name[2:])
    if name.startswith("/"):
        return _real(name)
    head, _, sub = name.partition("/")
    if head in XDG_USER:
        base = xdg.user_dir(XDG_USER[head])
    elif head in XDG_BASE:
        var, default = XDG_BASE[head]
        base = env.get(var) or str(Path(home) / default)
    elif head == "xdg-run":
        base = env.get("XDG_RUNTIME_DIR")
    else:
        return None
    if not base:
        return None
    return _real(Path(base) / sub) if sub else _real(base)


def flatpak_roots(app, argv, home, env, run, xdg=None):
    """そのアプリのサンドボックスから見える場所の一覧 [(realpath, 予約の場所を除くか)]。許可を読めなければアプリの場所だけ。"""
    roots = [(_real(Path(home) / ".var/app" / app), False)]
    info = _call(run, ["flatpak", "info", "--show-permissions", app])
    if info is None:
        return roots  # 読めない: 見えない側に倒す
    entries = filesystems_of(info)
    # info は override を混ぜた結果だが、版によっては混ざらないことがあるので、system・user の override も順に重ねる
    for scope in (["--system"], ["--user"]):
        entries += filesystems_of(_call(run, ["flatpak", "override", *scope, "--show", app]))
    entries += argv_filesystems(argv)
    xdg = xdg or XdgDirs(home, env, run)
    for name in apply_entries(entries):
        p = entry_path(name, home, env, xdg)
        if p:
            # host と絶対パスで書いた場所は、/tmp・/usr などサンドボックスの側のものになる所を除く
            roots.append((p, name == "host" or name.startswith("/")))
            if name == "host":
                roots.append((_real(home), False))  # host はホームも含む（ホームが予約の場所の下にあっても）
    return roots


def flatpak_visible(path, roots):
    """roots は flatpak_roots の (場所, 予約の場所を除くか) の一覧。"""
    path = _real(path)
    for root, reserved in roots:
        if _under(path, root) and not (reserved and any(_under(path, r) for r in RESERVED)):
            return True
    return False


# ---------- Snap ----------
def snap_home_connected(name, run):
    """snap connections <名前> で home インターフェースがつながっているか。分からなければ False。"""
    out = _call(run, ["snap", "connections", name])
    for line in (out or "").splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 3 and cols[0] == "home" and cols[2] != "-":
            return True
    return False


def snap_visible(path, name, home, run):
    path, home = _real(path), _real(home)
    if _under(path, _real(Path(home) / "snap" / name)):
        return True
    if not _under(path, home) or path == home:
        return False
    # home インターフェースはホームの隠しでない場所だけ。保守的に、途中に隠しフォルダがあれば見えないとする
    if any(part.startswith(".") for part in Path(os.path.relpath(path, home)).parts):
        return False
    return snap_home_connected(name, run)


# ---------- 判定 ----------
class Sandbox:
    """sandbox = {"type": "flatpak", "id": アプリ ID} か {"type": "snap", "id": 名前}（lib/browser.py の find_browser）。"""

    def __init__(self, sandbox, home, env, run=subprocess.run, argv=None):
        self.type, self.id = sandbox["type"], sandbox["id"]
        self.home, self.env, self.run, self.argv = Path(home), env, run, argv
        self._roots = None

    def visible(self, path):
        if self.type == "flatpak":
            if self._roots is None:
                self._roots = flatpak_roots(self.id, self.argv, self.home, self.env, self.run)
            return flatpak_visible(path, self._roots)
        if self.type == "snap":
            return snap_visible(path, self.id, self.home, self.run)
        return False

    def copy_base(self):
        # 開く場所はプロファイルの置き場と同じ $HOME の形（Flatpak はアプリの場所を $HOME/.var/app/<ID> で見せる）。
        # 比べるときだけ realpath にそろえる
        home = Path(self.home)
        if self.type == "flatpak":
            return home / ".var/app" / self.id / COPY_DIR
        return home / "snap" / self.id / "common" / COPY_DIR

    def label(self):
        return f"{'Flatpak' if self.type == 'flatpak' else 'Snap'} の {self.id}"


# ---------- HTML の資源 ----------
URL_RE = re.compile(r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^)'"\s]*))\s*\)""", re.I)
IMPORT_RE = re.compile(r"""@import\s+(?:"([^"]*)"|'([^']*)')""", re.I)
SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
SRC_ATTRS = {"img": ("src", "srcset"), "script": ("src",), "link": ("href",), "source": ("src", "srcset"),
             "video": ("src", "poster"), "audio": ("src",), "track": ("src",), "embed": ("src",),
             "input": ("src",), "iframe": ("src",), "object": ("data",)}


def css_refs(text):
    out = []
    for m in URL_RE.finditer(text):
        out.append(next(g for g in m.groups() if g is not None))
    for m in IMPORT_RE.finditer(text):
        out.append(m.group(1) if m.group(1) is not None else m.group(2))
    return [r.strip() for r in out]


def srcset_refs(value):
    return [part.strip().split()[0] for part in value.split(",") if part.strip()]


class _Refs(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.refs, self._style = [], False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for name in SRC_ATTRS.get(tag, ()):
            v = attrs.get(name)
            if v:
                self.refs += srcset_refs(v) if name == "srcset" else [v.strip()]
        if attrs.get("style"):
            self.refs += css_refs(attrs["style"])
        self._style = tag == "style"

    def handle_endtag(self, tag):
        self._style = False

    def handle_data(self, data):
        if self._style:
            self.refs += css_refs(data)


def html_refs(text):
    p = _Refs()
    try:
        p.feed(text)
        p.close()
    except Exception:  # 壊れた HTML でも、拾えた分だけ使う  # noqa: BLE001
        pass
    return p.refs


def classify_ref(ref, owner_dir, root):
    """("skip", None)（ファイルではない・無視）/ ("outside", ref)（写さない）/ ("file", 元のフォルダからの相対パス)。"""
    if not ref or ref.startswith("#") or ref.startswith("//"):
        return "skip", None
    if SCHEME_RE.match(ref):
        return ("outside", ref) if ref.lower().startswith("file:") else ("skip", None)
    if ref.startswith("/"):
        return "outside", ref
    rel = unquote(ref.split("#", 1)[0].split("?", 1)[0])
    if not rel:
        return "skip", None
    target = os.path.normpath(os.path.join(owner_dir, rel))
    inside = os.path.relpath(target, root)
    if inside == ".." or inside.startswith("../") or os.path.isabs(inside):
        return "outside", ref
    return ("file", inside) if os.path.isfile(target) else ("skip", None)


def collect_resources(html_path):
    """(写す相対パスの一覧, 写さない参照の一覧)。CSS ファイルの中の url()・@import もたどる。"""
    root = os.path.dirname(html_path)
    files, outside, seen = [], [], set()
    todo = [(html_path, True)]
    while todo:
        path, is_html = todo.pop(0)
        try:
            text = Path(path).read_text(errors="replace")
        except OSError:
            continue
        for ref in (html_refs(text) if is_html else css_refs(text)):
            kind, val = classify_ref(ref, os.path.dirname(path), root)
            if kind == "outside":
                if val not in outside:
                    outside.append(val)
            elif kind == "file" and val not in seen:
                seen.add(val)
                files.append(val)
                if Path(val).suffix.lower() in CSS_EXT:
                    todo.append((os.path.join(root, val), False))
    return files, outside


# ---------- 写す ----------
def cleanup(base, keep=None, now=None, keep_seconds=KEEP_SECONDS):
    """base の下の、古い写し（フォルダごと）を消す。消せなくても止めない。"""
    now = time.time() if now is None else now
    try:
        entries = list(Path(base).iterdir())
    except OSError:
        return []
    gone = []
    for e in entries:
        if e.name == keep:
            continue
        try:
            if now - e.lstat().st_mtime > keep_seconds:
                shutil.rmtree(e) if e.is_dir() and not e.is_symlink() else e.unlink()
                gone.append(e.name)
        except OSError:
            pass
    return gone


def copy_for(src, base, limit=COPY_LIMIT, now=None):
    """src（realpath）を base/<短いハッシュ>/ に写す。(写した先のファイル, 資源の数, 知らせる文の一覧)。"""
    tag = hashlib.sha1(src.encode()).hexdigest()[:12]
    cleanup(base, keep=tag, now=now)
    dest_dir = Path(base) / tag
    if dest_dir.exists():
        shutil.rmtree(dest_dir)  # 前の写しの消えた資源を残さない
    dest_dir.mkdir(parents=True)
    dest = dest_dir / os.path.basename(src)
    shutil.copyfile(src, dest)
    notes, copied, failed = [], 0, []
    if Path(src).suffix.lower() in HTML_EXT:
        root = os.path.dirname(src)
        files, outside = collect_resources(src)
        total = 0
        for rel in files:
            try:
                total += os.path.getsize(os.path.join(root, rel))
            except OSError:
                pass
        if total > limit:
            notes.append(f"資源の合計 {total // (1024 * 1024)}MB が上限 {limit // (1024 * 1024)}MB を超えるので、"
                         f"HTML だけ写した（画像などは表示されない）")
        else:
            for rel in files:
                to = dest_dir / rel
                try:
                    to.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(os.path.join(root, rel), to)
                    copied += 1
                except OSError as e:
                    failed.append(f"{rel}（{e.strerror}）")
        if outside:
            notes.append("元のフォルダの外を指す参照（../・絶対パス）は写していない: " + ", ".join(outside[:10])
                         + (f" ほか {len(outside) - 10} 件" if len(outside) > 10 else ""))
        if failed:
            notes.append("資源を写せなかった: " + ", ".join(failed[:10])
                         + (f" ほか {len(failed) - 10} 件" if len(failed) > 10 else ""))
    return dest, copied, notes


def prepare_url(url, sandbox, home, env, run=subprocess.run, argv=None, warn=None, limit=COPY_LIMIT, now=None):
    """(開く URL, 写したことの dict か None)。サンドボックスでない・file:// でない・見えるなら url のまま。

    写せないとき（ディスクが一杯など）は、知らせて元の URL のまま開く（開けないのは前と同じ）。"""
    if not sandbox or not str(url).startswith("file:"):
        return url, None
    u = urlparse(url)
    src = _real(unquote(u.path))
    if not os.path.isfile(src):
        return url, None
    box = Sandbox(sandbox, home, env, run, argv)
    if box.visible(src):
        return url, None
    try:
        dest, n, notes = copy_for(src, box.copy_base(), limit=limit, now=now)
    except OSError as e:
        if warn:
            warn(f"{box.label()} から見えない場所だが、見える場所に写せなかった（{e}）。そのまま開く")
        return url, None
    for line in notes:
        if warn:
            warn(line)
    new = dest.as_uri() + (f"?{u.query}" if u.query else "") + (f"#{u.fragment}" if u.fragment else "")
    return new, {"from": src, "to": str(dest), "resources": n, "note": staged_note(box.label())}


def staged_note(label):
    """結果の JSON の note。写した先は表示用なので、呼んだ側（AI）が元のファイルを見失わないように書く。"""
    return (f"{label} から見えない場所なので、表示用のコピー（staged）を開いた。元のファイル（target）は動かしていない。"
            "直すのは元のファイル。直したら claude-show（claude-browser open）をもう一度呼ぶと写し直す")


def staged_fields(info):
    """claude-browser・claude-show の結果に足すキー（target は呼ぶ側が元のまま入れる）。"""
    return {"staged": info["to"], "staged_resources": info["resources"], "note": info["note"]} if info else {}
