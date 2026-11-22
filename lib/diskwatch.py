"""ディスクの使用率の見張り（claude-state watch の中で interval_sec ごとに動く。設定は config.json の disk_watch）。

2026-11-23 に /tmp（tmpfs）の inode が 100% になり、全部のセッションでコマンドが起動できなくなった（テストの一時
フォルダの残り）。そうなる前に、人（デスクトップの通知）と統括のセッション（claude-relay 第 2 段の起こす部品）に知らせる。

- 見るもの: 設定の各 path の os.statvfs の容量と inode。inode の総数が 0 以下（btrfs など、数を持たないもの）なら
  inode は見ない。存在しない path（$XDG_RUNTIME_DIR が無くて展開できないものも）は飛ばす。
- 知らせ方: 閾値（既定 70・85 %）ごとに 1 回だけ。閾値より HYSTERESIS ポイント下に下がったら、また知らせられる
  ようにする（閾値のあたりで上下して何度も知らせないため）。知らせた印は runtime_dir の state.json に置き、watch を
  起動し直しても同じ知らせを繰り返さない（書けなくても、動いている間は覚えている）。
- 送り先: notify_role があれば、その役割のセッションに lib/relay.py の WAKERS（cross-session の socket）で 1 通。
  送れない・notify_role が無いときはデスクトップの通知（Linux は notify-send、macOS は osascript）。
- 手がかり: path の直下の項目を名前の頭（数字・乱数の部分を除いた形。tmp*・lua_*・pytest-* など）でまとめた数の
  上位 3 つと、直下の項目の数。事故の形（直下に小さな一時フォルダがたくさん並ぶ）では、中を数えた上位は
  tmp0（5）のような小さな数になって原因を指せないので、中には潜らない。TOP_BUDGET 秒で数え終わらなければ省く。

statvfs・時計・送り先・通知は差し替えられる（テストでは偽物）。
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from rundir import runtime_dir

HYSTERESIS = 10      # 閾値よりこれだけ（ポイント）下がったら、次に超えたときにまた知らせる
TOP_BUDGET = 1.0     # 直下を数える時間の上限（秒）
TOP_N = 3
MARK = "[claude-kitty disk]"  # lib/presence.py の MACHINE_MARKS に入れてある（人の入力と数えない）
KIND_LABEL = {"bytes": "容量", "inodes": "inode"}


def state_path(env):
    return runtime_dir(env) / "claude-disk-watch" / "state.json"


_VAR = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def expand(path, env):
    """~ と $VAR / ${VAR} を env で展開する。無い変数があれば None（空にして / などを見ないため）。"""
    missing = []

    def sub(m):
        name = m.group(1) or m.group(2)
        v = env.get(name)
        if not v:
            missing.append(name)
            return ""
        return v
    out = _VAR.sub(sub, path)
    if missing:
        return None
    if out == "~" or out.startswith("~/"):
        home = env.get("HOME") or os.path.expanduser("~")
        out = home + out[1:]
    return out


def measure(path, statvfs=os.statvfs):
    """{"bytes": (使用, 総量, %), "inodes": (使用, 総数, %) か None}。

    容量の % は df と同じ 使用 ÷（使用 + 一般ユーザーの空き）。数を持たない（f_blocks / f_files が 0 以下。btrfs の
    inode、composefs など）ものは None。"""
    st = statvfs(path)
    out = {"bytes": None, "inodes": None}
    fr = st.f_frsize or st.f_bsize
    if st.f_blocks > 0 and fr > 0:
        used = (st.f_blocks - st.f_bfree) * fr
        denom = used + max(st.f_bavail, 0) * fr
        out["bytes"] = (used, st.f_blocks * fr, used * 100.0 / denom if denom > 0 else 0.0)
    if st.f_files > 0:
        used = st.f_files - max(st.f_ffree, 0)
        out["inodes"] = (used, st.f_files, used * 100.0 / st.f_files)
    return out


_SEP = re.compile(r"[-_.]")
_MIXED = re.compile(r"[a-z][A-Z]|[A-Z]{2}[a-z]")  # 大文字・小文字の混ざり（kXaPFP など）


def _random_part(part):
    """区切りの間の部分が、数字・乱数か（v8・X11 のような短い名前の一部は除く）。"""
    return (part.isdigit() or (len(part) >= 4 and any(c.isdigit() for c in part))
            or (len(part) >= 3 and bool(_MIXED.search(part))))


def name_group(name):
    """名前の頭（まとめる鍵）。数字・乱数の部分から先を * にする。

      tmp0・tmpp1rtt87k → tmp*、lua_ab12 → lua_*、pytest-of-x-12 → pytest-of-x-*、claude-1000 → claude-*、
      pressure-vessel-libs-kXaPFP → pressure-vessel-libs-*、abc123 → abc*、_MEI12345 → _MEI*、
      node-compile-cache・.X11-unix・tmpfiles.d → そのまま
    区切りの無い「頭 + 乱数」（foo + 英数字）は、乱数の英字の分まで頭に入る（fooab*）。それは top_groups が
    merge_loose でまとめ直す。"""
    if re.fullmatch(r"tmp[a-z0-9_]{8}", name):
        return "tmp*"  # Python の tempfile（tmp + 8 字の乱数。_ も入るので区切りでは分けられない）
    pos = 0
    for part in _SEP.split(name):
        if part and _random_part(part):
            if pos and any(c.isalnum() for c in name[:pos]):
                return name[:pos] + "*"
            head = re.match(r"[A-Za-z]*", part).group(0)
            lead = name[:pos]  # 頭の区切りだけ（_MEI12345 の _）は残す
            return lead + head + "*" if len(head) >= 2 else lead + "*"
        pos += len(part) + 1
    return name


_LOOSE = re.compile(r"[A-Za-z]{4,}\*?")  # 区切りの無い英字だけの鍵（乱数が英字だけだった名前もそのまま入る）


def merge_loose(counts, clock=time.monotonic, end=None):
    """区切りの無い「頭 + 乱数」の鍵（fooab*・fooXyz* …）を、頭の 3 字ごとに、ほかより多い続きの字がある間だけ
    伸ばした共通の頭（foo*）にまとめ直す（mktemp -d /tmp/fooXXXXXX の形が何万ものグループに割れないように）。
    1 つしかない鍵（steamhdBNOH*）は、伸ばし切って元のまま。end を過ぎたら、そこまでのまま返す。"""
    buckets = {}
    for k, c in counts.items():
        if _LOOSE.fullmatch(k):
            buckets.setdefault(k[:3], []).append((k.rstrip("*"), c, k))
    out = dict(counts)
    for i, (p3, members) in enumerate(buckets.items()):
        if end is not None and i % 64 == 0 and clock() > end:
            break
        if len(members) < 2:
            continue
        prefix, cur = p3, members
        weight = sum(c for _s, c, _k in members)
        while len(prefix) < 16:
            nxt = {}
            for stem, c, _k in cur:
                if len(stem) > len(prefix):
                    nxt[stem[len(prefix)]] = nxt.get(stem[len(prefix)], 0) + c
            if not nxt:
                break
            ch, c = max(nxt.items(), key=lambda x: x[1])
            if c * 2 < weight:  # 続きの字がばらばら（乱数）: ここまでが頭
                break
            prefix += ch
            cur = [m for m in cur if m[0].startswith(prefix)]
        merged = [m for m in members if m[0].startswith(prefix)]
        if len(merged) < 2:
            continue
        for _st, _cc, k in merged:
            del out[k]
        out[prefix + "*"] = out.get(prefix + "*", 0) + sum(cc for _s, cc, _k in merged)
    return out


DEEP_MAX_TOP = 2000  # 直下がこれより少ないときだけ、中を数える（何十万も並ぶ形では名前の頭でまとめた数で足りる）
DEEP_N = 2


def _count_inside(path, dev, tick):
    """path の下の全部の数（リンクはたどらず、別のファイルシステムには入らない）。(数, 時間切れか)。"""
    c, stack = 0, [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for x in it:
                    if tick():
                        return c, True
                    c += 1
                    try:
                        if x.is_dir(follow_symlinks=False) and x.stat(follow_symlinks=False).st_dev == dev:
                            stack.append(x.path)
                    except OSError:
                        pass
        except OSError:
            pass  # 読めないフォルダは数えない
    return c, False


def top_groups(path, budget=TOP_BUDGET, n=TOP_N, clock=time.monotonic, every=256):
    """手がかり {"total", "groups", "deep", "partial"}。

    - groups: 直下の項目を名前の頭でまとめた数の上位 n 個 [(名前の頭, 数)]（平たく何十万も並ぶ形の手がかり）。
    - deep: 直下が DEEP_MAX_TOP より少ないとき、直下のフォルダごとの中の数の上位 DEEP_N 個 [(名前, 数)]
      （1 つのフォルダの下に積もる形の手がかり。claude-<uid>/ の scratchpad・pytest-of-<user>/ など）。残りの時間で数え、
      時間切れなら partial が真（数は「以上」、まだ数えていないフォルダもある）。
    path がリンクなら、たどった先を数える（中を数えるときの「別のファイルシステム」の基準を、リンクの置き場でなく
    たどった先の装置にするため）。直下を数え終わらなければ None（省く）。時計は every 個ごとに見る。"""
    end = clock() + budget
    counts, total, dirs = {}, 0, []
    real = os.path.realpath(path)
    try:
        dev = os.stat(real).st_dev
        with os.scandir(real) as it:
            for e in it:
                total += 1
                if total % every == 0 and clock() > end:
                    return None
                k = name_group(e.name)
                counts[k] = counts.get(k, 0) + 1
                if total <= DEEP_MAX_TOP:
                    try:
                        if e.is_dir(follow_symlinks=False) and e.stat(follow_symlinks=False).st_dev == dev:
                            dirs.append(e)
                    except OSError:
                        pass
    except OSError:
        return None
    counts = merge_loose(counts, clock, end)
    groups = sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:n]
    deep, partial = [], False
    if total <= DEEP_MAX_TOP and dirs:
        seen = [0]

        def tick():
            seen[0] += 1
            return seen[0] % every == 0 and clock() > end
        sizes = []
        for e in dirs:
            if clock() > end:
                partial = True
                break
            c, late = _count_inside(e.path, dev, tick)
            sizes.append((e.name, c))
            if late:
                partial = True
                break
        deep = [x for x in sorted(sizes, key=lambda x: (-x[1], x[0]))[:DEEP_N] if x[1] > 0]
    return {"total": total, "groups": groups, "deep": deep, "partial": partial}


def _num(n):
    return f"{n:,}"


def _size(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


def message(path, kind, used, total, pct, threshold, top):
    amount = f"{_num(used)} / {_num(total)}" if kind == "inodes" else f"{_size(used)} / {_size(total)}"
    what = "の inode が" if kind == "inodes" else "の容量が"
    text = f"{MARK} {path} {what} {pct:.0f}%（{amount}）。{threshold:g}% を超えました。"
    if top and top.get("groups"):
        text += (f"直下は {_num(top['total'])} 項目。多いもの（名前の頭でまとめた数）: "
                 + "、".join(f"{name}（{_num(c)}）" for name, c in top["groups"]) + "。")
    if top and top.get("deep"):
        more = " 以上" if top.get("partial") else ""
        text += ("中が多いもの（下の全部の数" + ("。時間切れで途中まで" if top.get("partial") else "") + "）: "
                 + "、".join(f"{name}（{_num(c)}{more}）" for name, c in top["deep"]) + "。")
    text += "（claude-kitty の見張りの知らせで、ユーザーが打ったものではない。片付けは中身を確認してから）"
    return text


def desktop_notify(text, platform=None, run=subprocess.run):
    """デスクトップの通知。出せたら True。"""
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        script = f"display notification {json.dumps(text)} with title \"claude-kitty disk\""
        argv = ["osascript", "-e", script]
    else:
        argv = ["notify-send", "-a", "claude-kitty", "-u", "critical", "claude-kitty disk", text]
    try:
        return run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def wake_send(env, role, text, wakers=None):
    """その役割のセッションに起こす部品（lib/relay.py の WAKERS）で 1 通送る。(送れたか, 部品の名前か理由)。"""
    import relay
    wakers = relay.WAKERS if wakers is None else wakers
    env = relay.runtime_env(env)
    errors = []
    for w in wakers:
        name = getattr(w, "name", type(w).__name__)
        try:
            if not w.available(env, role):
                continue
            if w.wake(env, role, text):
                return True, name
            errors.append(f"{name}: 送れなかった")
        except Exception as e:  # noqa: BLE001 — 部品の失敗で見張りを止めない。次の部品へ
            errors.append(f"{name}: {type(e).__name__}: {e}")
    return False, "; ".join(errors) or "使える部品が無い（そのセッションの記録が無い）"


class DiskWatch:
    """1 回の check で、全部の path を測って、新しく超えた閾値を知らせる。"""

    def __init__(self, env, conf, statvfs=os.statvfs, send=None, notify=None, top=top_groups, log=None,
                 state_file=None):
        self.env, self.conf = env, conf
        self.statvfs, self.top = statvfs, top
        self.send = send or (lambda role, text: wake_send(env, role, text))
        self.notify = notify or desktop_notify
        self.log = log or (lambda msg: None)
        self.state_file = Path(state_file) if state_file else state_path(env)
        self.fired = self._load()
        self.next_at = None

    def _load(self):
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        f = data.get("fired") if isinstance(data, dict) else None
        return {k: True for k, v in f.items() if v is True} if isinstance(f, dict) else {}

    def _save(self):
        try:
            self.state_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            tmp = self.state_file.with_name(f".{self.state_file.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"fired": self.fired}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.state_file)
        except OSError as e:  # inode が尽きていると書けない。動いている間は覚えている
            self.log(f"disk: 知らせた印を書けない（動いている間は覚えている）: {e}")

    def due(self, mono):
        """interval_sec ごとに True（初めは すぐ）。"""
        if not self.conf["enabled"]:
            return False
        if self.next_at is None or mono >= self.next_at:
            self.next_at = mono + self.conf["interval_sec"]
            return True
        return False

    def check(self):
        """測って知らせる。[(path, 種類, 閾値, 届け先)] を返す（届け先は部品の名前・"desktop"・"none"）。"""
        sent, changed = [], False
        for raw in self.conf["paths"]:
            path = expand(raw, self.env)
            if path is None or not os.path.isdir(path):
                continue
            try:
                m = measure(path, self.statvfs)
            except OSError:
                continue  # 消えた・読めない path は飛ばす
            for kind in ("bytes", "inodes"):
                if m[kind] is None:
                    continue
                used, total, pct = m[kind]
                new = []
                for t in self.conf["thresholds"]:
                    key = f"{path}|{kind}|{t:g}"
                    if key in self.fired:
                        if pct < t - HYSTERESIS:
                            del self.fired[key]
                            changed = True
                    elif pct >= t:
                        self.fired[key] = True
                        new.append(t)
                        changed = True
                if new:
                    sent.append((path, kind, max(new), m[kind]))
        if changed:
            self._save()  # 送る前に書く（送っている途中で落ちても、起動し直して同じ知らせを繰り返さない）
        out, tops = [], {}
        for path, kind, t, (used, total, pct) in sent:
            if path not in tops:  # 容量と inode が同時に超えても、数えるのは path ごとに 1 回
                try:
                    tops[path] = self.top(path)
                except Exception:  # noqa: BLE001
                    tops[path] = None
            top = tops[path]
            text = message(path, kind, used, total, pct, t, top)
            out.append((path, kind, t, self._deliver(text)))
        return out

    def preview(self):
        """測った値だけ（知らせない・印を付けない。--once の確認用）。status_rows と同じ形。"""
        return status_rows(self.env, self.conf, self.statvfs)

    def _deliver(self, text):
        role = self.conf["notify_role"]
        if role:
            try:
                ok, how = self.send(role, text)
            except Exception as e:  # noqa: BLE001
                ok, how = False, f"{type(e).__name__}: {e}"
            if ok:
                return how
            self.log(f"disk: {role} に送れない（デスクトップの通知に落とす）: {how}")
        if self.notify(text):
            return "desktop"
        self.log(f"disk: デスクトップの通知も出せない: {text}")
        return "none"


def status_rows(env, conf, statvfs=os.statvfs):
    """[(path, 種類, 使用, 総量, %, 超えている閾値の最大か None)]。無い path は (path, None, …)。doctor 用（数えない・書かない）。"""
    rows = []
    for raw in conf["paths"]:
        path = expand(raw, env)
        if path is None or not os.path.isdir(path):
            rows.append((path or raw, None, None, None, None, None))
            continue
        try:
            m = measure(path, statvfs)
        except OSError:
            rows.append((path, None, None, None, None, None))
            continue
        for kind in ("bytes", "inodes"):
            if m[kind] is None:
                continue
            used, total, pct = m[kind]
            over = [t for t in conf["thresholds"] if pct >= t]
            rows.append((path, kind, used, total, pct, max(over) if over else None))
    return rows
