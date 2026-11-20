"""kitty のペイン配置から、ブラウザを重ねる矩形を計算する（副作用なし、テスト対象）。

kitty の `kitten @ ls` にはペインのピクセル座標が無いので、隣接関係（neighbors）と
各ペインの columns/lines から OS ウィンドウ内での割合を求め、KWin で得た kitty の
クライアント領域（論理座標）に当てはめる。罫線の数ピクセル分は誤差として無視する。
"""


def _chain_sum(windows, start, direction, size_key):
    """start から direction 方向へ隣接ペインをたどり、size_key の合計を返す。"""
    total = 0
    seen = {start}
    cur = start
    while True:
        nbrs = windows[cur].get("neighbors", {}).get(direction) or []
        nxt = next((n for n in nbrs if n in windows and n not in seen), None)
        if nxt is None:
            return total
        seen.add(nxt)
        total += windows[nxt][size_key]
        cur = nxt


def pane_fraction(windows_list, window_id):
    """ペインの OS ウィンドウ内での位置を (x, y, w, h) の割合 0..1 で返す。"""
    windows = {w["id"]: w for w in windows_list}
    if window_id not in windows:
        raise KeyError(f"kitty window {window_id} is not in the active tab")
    me = windows[window_id]
    left = _chain_sum(windows, window_id, "left", "columns")
    right = _chain_sum(windows, window_id, "right", "columns")
    top = _chain_sum(windows, window_id, "top", "lines")
    bottom = _chain_sum(windows, window_id, "bottom", "lines")
    cols = left + me["columns"] + right
    rows = top + me["lines"] + bottom
    if cols <= 0 or rows <= 0:
        raise ValueError("pane has no size")
    return (left / cols, top / rows, me["columns"] / cols, me["lines"] / rows)


def target_rect(client, frac, mode="self", ratio=0.65):
    """kitty のクライアント領域 client=(x,y,w,h) とペインの割合から重ねる矩形を返す。

    mode="self": ペイン全体。mode="show": ペインの上 ratio だけ
    （claude-code は出力が下へ伸び入力欄も最下段なので、下側を見せたまま会話できる）。
    """
    if mode not in ("self", "show"):
        raise ValueError(f"unknown mode: {mode}")
    if not 0.1 <= ratio <= 1.0:
        raise ValueError("ratio must be between 0.1 and 1.0")
    cx, cy, cw, ch = client
    fx, fy, fw, fh = frac
    x, y = cx + fx * cw, cy + fy * ch
    w, h = fw * cw, fh * ch
    if mode == "show":
        h *= ratio
    return (round(x), round(y), round(w), round(h))




def split_rects(pairs, rect=(0.0, 0.0, 1.0, 1.0)):
    """kitty の splits レイアウトの木（layout_state.pairs）から {group_id: (x, y, w, h)} を返す。

    葉は group id（整数）。節は {"one", "two", "horizontal"(既定 True=左右), "bias"(既定 0.5=one の割合)}。
    neighbors の番号は window id ではなく group id で、ペインを移すとずれるので、こちらを正とする。
    """
    out = {}
    if pairs is None:
        return out
    if isinstance(pairs, int):
        out[pairs] = rect
        return out
    x, y, w, h = rect
    bias = pairs.get("bias", 0.5)
    if pairs.get("horizontal", True):
        a, b = (x, y, w * bias, h), (x + w * bias, y, w * (1 - bias), h)
    else:
        a, b = (x, y, w, h * bias), (x, y + h * bias, w, h * (1 - bias))
    out.update(split_rects(pairs.get("one"), a))
    out.update(split_rects(pairs.get("two"), b))
    return out


def tab_pane_fraction(tab, window_id):
    """タブの情報から、ペインの割合 (x, y, w, h) を返す。splits なら木から、それ以外は neighbors から。"""
    state = tab.get("layout_state") or {}
    if state.get("class") == "Splits" and "pairs" in state:
        group = next((g["id"] for g in tab.get("groups", []) if window_id in g.get("windows", [])), None)
        rects = split_rects(state["pairs"])
        if group in rects:
            return rects[group]
        if len(tab.get("windows", [])) == 1:
            return (0.0, 0.0, 1.0, 1.0)
        raise KeyError(f"kitty window {window_id} is not in the split tree")
    return pane_fraction(tab.get("windows", []), window_id)


def right_area_fraction(tab, left_window_id=None):
    """タブの左の列（統括のペイン left_window_id）を除いた右側全体の割合 (x, 0, 1 - x, 1)。

    left_window_id が無い・木に無いときは、splits の根が左右の分割ならその左側を左の列とする。
    どちらでもなければ（左右に分かれていない）タブ全体。
    """
    if left_window_id is not None:
        try:
            x, _y, w, _h = tab_pane_fraction(tab, left_window_id)
        except (KeyError, ValueError):
            pass
        else:
            if x + w < 1.0:
                return (x + w, 0.0, 1.0 - (x + w), 1.0)
    state = tab.get("layout_state") or {}
    root = state.get("pairs") if state.get("class") == "Splits" else None
    if isinstance(root, dict) and "one" in root and "two" in root and root.get("horizontal", True):
        b = root.get("bias", 0.5)
        return (b, 0.0, 1.0 - b, 1.0)
    return (0.0, 0.0, 1.0, 1.0)



MIN_GAP = 0.3  # others の隙間が右側の幅のこれ未満なら、右側を左右半分に分けて使う


def others_area_fraction(right, own, min_gap=MIN_GAP):
    """右側 right=(x, y, w, h) のうち、自分のペイン own=(x, y, w, h) を横に避けた矩形の割合を返す。

    ユーザーに見せるときは、自分以外のペインの場所に縦 100% で開く（自分の会話は見せたまま、
    小さなペインの上の一部だけでは小さすぎるので）。自分の横の範囲を除いた左右の隙間のうち広い方を、
    right の全高で使う（同じ幅なら右）。広い方でも right の幅の min_gap 未満なら（自分が右側をほぼ横断している）、
    right を左右半分に分け、自分の中心が無い方を使う。own が None か、right より左（左の列＝統括）なら right 全体。
    """
    rx, ry, rw, rh = right
    if own is None or rw <= 0:
        return right
    px, _py, pw, _ph = own
    eps = 1e-9
    if px + pw <= rx + eps or px >= rx + rw - eps:
        return right  # 自分は右側の外（左の列）にいる
    left_w = max(0.0, px - rx)
    right_x = min(rx + rw, px + pw)
    right_w = max(0.0, rx + rw - right_x)
    if max(left_w, right_w) >= min_gap * rw - eps:
        if right_w >= left_w - eps:  # 同じ幅（浮動小数の誤差を含む）なら右
            return (right_x, ry, right_w, rh)
        return (rx, ry, left_w, rh)
    mid = rx + rw / 2
    if px + pw / 2 <= mid:
        return (mid, ry, rx + rw - mid, rh)
    return (rx, ry, mid - rx, rh)


MAX_OFFSET = 64  # 一定のずれとして打ち消す上限（窓の装飾・影の大きさ）


def fit_correction(want, got):
    """配置した窓の実際の矩形 got が want とずれていれば、ずれを打ち消す矩形を返す（ずれが 2px 以下なら None）。

    窓の装飾（タイトルバー）や、最大化の解除で前の大きさに戻されるなどで、指定と実際がずれることがある。
    """
    if got is None or all(abs(a - b) <= 2 for a, b in zip(want, got)):
        return None
    if any(abs(a - b) > MAX_OFFSET for a, b in zip(want, got)):
        return None  # 装飾の分より大きなずれ（大きさを戻されたなど）は打ち消さない（極端に小さな窓にしない）
    wx, wy, ww, wh = want
    gx, gy, gw, gh = got
    return (wx - (gx - wx), wy - (gy - wy), max(1, ww - (gw - ww)), max(1, wh - (gh - wh)))
