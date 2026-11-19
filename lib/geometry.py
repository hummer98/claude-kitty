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
