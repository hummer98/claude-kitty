"""claude-view の配置計画（副作用なし、テスト対象）。

kitty の splits レイアウトでは:
- 別タブのペインを detach-window で持ってくると、いま前にあるペインを左右に割って入る。
- `layout_action rotate` は前にあるペインを含む分割の向きを左右↔上下に変える。
- `layout_action bias N` は前にあるペインが、そのすぐ外側の分割で N% を占めるようにする。
差分だけ動かすと形が前の状態に左右されるので、毎回いったん左以外を standby タブへ下げてから組み直す。
"""

def left_percent(rows):
    """等分割: 統括の列も 1 列分に数える（ユーザーの指示「等分割に。以後何もいわないときは守って」）。"""
    widest = max((len(r) for r in rows), default=0)
    return round(100 / (widest + 1))


def current_rows(tab, left, fraction):
    """いまの main タブの右側を段（上から）× 横（左から）の role のリストにする。

    fraction(tab, window_id) → (x, y, w, h)。上端の y が同じペインを同じ段とみなす。
    """
    cells = []
    for w in tab.get("windows", []):
        role = (w.get("user_vars") or {}).get("role")
        if not role or role == left:
            continue
        x, y, _, _ = fraction(tab, w["id"])
        cells.append((round(y, 2), x, role))
    rows, last_y = [], None
    for y, x, role in sorted(cells):
        if y != last_y:
            rows.append([])
            last_y = y
        rows[-1].append(role)
    return rows


def split_two(roles):
    """全員を 2 段に分ける（上が多め）。"""
    if not roles:
        return []
    k = (len(roles) + 1) // 2
    return [r for r in (roles[:k], roles[k:]) if r]


def add_role(rows, role, row=None):
    """role を指定の段（0 始まり）か、一番少ない段の右端に足す。段が無ければ新しい段にする。"""
    rows = remove_role(rows, role)  # すでに右側にいれば、いったん抜いてから足す
    if not rows:
        return [[role]]
    if row is None:
        row = min(range(len(rows)), key=lambda i: len(rows[i]))
    if row >= len(rows):
        return rows + [[role]]
    rows[row].append(role)
    return rows


def remove_role(rows, role):
    return [[r for r in row if r != role] for row in rows if [r for r in row if r != role]]


def swap_roles(rows, a, b):
    """a と b の位置を入れ替える。片方が右側にいなければ（standby など）、いる方の位置に入れる。"""
    flat = [r for row in rows for r in row]
    if a not in flat and b not in flat:
        raise ValueError(f"{a} も {b} も右側にいない")
    return [[b if r == a else a if r == b else r for r in row] for row in rows]


def parse_rows(spec):
    """"a,b,c/d,e" → [["a","b","c"],["d","e"]]。空の段は捨てる。"""
    return [[r.strip() for r in row.split(",") if r.strip()] for row in spec.split("/") if row.strip()]


def validate(left, rows, known):
    """配置できない指定なら ValueError。known は kitty にいる role の集合。"""
    seen = set()
    for row in rows:
        if not row:
            raise ValueError("空の段がある")
        for role in row:
            if role == left:
                raise ValueError(f"{role} は左の枠に固定なので右には置けない")
            if role in seen:
                raise ValueError(f"{role} が 2 回出てくる")
            seen.add(role)
    missing = (seen | {left}) - set(known)
    if missing:
        raise ValueError("kitty に見つからない: " + ", ".join(sorted(missing)))


def plan(left, rows, main_roles):
    """操作の列を返す。main_roles はいま main タブにいる role（left を含む）。

    操作: ("park", role) 標準タブへ下げる / ("bring", role) main へ持ってくる（前のペインの右に入る）
          ("focus", role) / ("rotate",) / ("bias", percent)
    """
    ops = [("park", r) for r in main_roles if r != left]
    if not rows:
        return ops + [("focus", left)]
    # 1. 左と右の境目
    ops += [("focus", left), ("bring", rows[0][0]), ("focus", left), ("bias", left_percent(rows))]
    # 2. 段を縦に積む（各段の先頭だけ）
    for i in range(1, len(rows)):
        ops += [("focus", rows[i - 1][0]), ("bring", rows[i][0]), ("focus", rows[i][0]), ("rotate",)]
    # 3. 段の高さをそろえる（段の中を埋める前に。埋めると先頭のすぐ外側の分割が変わる）
    for i in range(len(rows) - 1):
        ops += [("focus", rows[i][0]), ("bias", round(100 / (len(rows) - i)))]
    # 4. 段の中を左から埋め、幅をそろえる
    for row in rows:
        for j in range(1, len(row)):
            ops += [("focus", row[j - 1]), ("bring", row[j])]
        for j in range(len(row) - 1):
            ops += [("focus", row[j]), ("bias", round(100 / (len(row) - j)))]
    return ops + [("focus", left)]


def overlay_ids(tab):
    """タブの中の overlay（claude-show の画像など）の window id。

    overlay は重ねた先のペインと同じ window group に入り、kitten @ ls ではふつうのペインと同じように
    並ぶ（role も無い）。group の 2 つめ以降が overlay なので、並べ替えの対象から外すのに使う。
    """
    groups = ((tab.get("layout_state") or {}).get("all_windows") or {}).get("window_groups") or []
    return {i for g in groups for i in (g.get("window_ids") or [])[1:]}
