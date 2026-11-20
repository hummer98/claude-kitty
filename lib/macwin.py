"""macOS: kitty の OS 窓の画面上の矩形を CoreGraphics から読む（claude-browser の重ね置き。試験的）。

CGWindowListCopyWindowInfo を ctypes で呼ぶ。窓の持ち主の pid（kCGWindowOwnerPID）・窓の番号（kCGWindowNumber）・
矩形（kCGWindowBounds）は、画面収録の許可なしで読める（窓の題 kCGWindowName は許可が要るので使わない）。
アクセシビリティの許可が要る方法（System Events・AXUIElement）は使わない。

座標は、主ディスプレイの左上を原点にしたポイント（Retina の倍率は OS が吸収する）。Chrome の CDP の
Browser.setWindowBounds も同じ座標なので、そのまま渡せる。kCGWindowBounds はタイトルバーを含む窓の外枠なので、
kitty のクライアント領域は、上からタイトルバーの高さを除いたものとする（kitty.conf の hide_window_decorations が
yes / titlebar-only / titlebar-and-corners なら 0、それ以外は TITLEBAR。$CLAUDE_BROWSER_MAC_TITLEBAR で変えられる）。

kitty の `kitten @ ls` の OS 窓の platform_window_id は、macOS では NSWindow の番号（= kCGWindowNumber）なので、
それで窓を 1 つに決める（無ければ、その pid のふつうの層の窓が 1 つだけのとき）。
"""
import os

TITLEBAR = 28  # macOS の標準のタイトルバーの高さ（ポイント）
NO_TITLEBAR = {"yes", "y", "true", "titlebar-only", "titlebar-and-corners"}


class MacWinError(Exception):
    pass


def window_list():
    """[{"number", "pid", "layer", "x", "y", "w", "h"}]。CoreGraphics が使えなければ MacWinError。"""
    try:
        import ctypes
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    except Exception as e:  # noqa: BLE001
        raise MacWinError(f"CoreGraphics を読めない: {e}") from None
    c = ctypes
    vp = c.c_void_p

    class CGRect(c.Structure):
        _fields_ = [("x", c.c_double), ("y", c.c_double), ("w", c.c_double), ("h", c.c_double)]

    cg.CGWindowListCopyWindowInfo.restype = vp
    cg.CGWindowListCopyWindowInfo.argtypes = [c.c_uint32, c.c_uint32]
    cg.CGRectMakeWithDictionaryRepresentation.restype = c.c_bool
    cg.CGRectMakeWithDictionaryRepresentation.argtypes = [vp, c.POINTER(CGRect)]
    cf.CFArrayGetCount.restype = c.c_long
    cf.CFArrayGetCount.argtypes = [vp]
    cf.CFArrayGetValueAtIndex.restype = vp
    cf.CFArrayGetValueAtIndex.argtypes = [vp, c.c_long]
    cf.CFDictionaryGetValue.restype = vp
    cf.CFDictionaryGetValue.argtypes = [vp, vp]
    cf.CFStringCreateWithCString.restype = vp
    cf.CFStringCreateWithCString.argtypes = [vp, c.c_char_p, c.c_uint32]
    cf.CFNumberGetValue.restype = c.c_bool
    cf.CFNumberGetValue.argtypes = [vp, c.c_long, vp]  # CFNumberType は CFIndex
    cf.CFRelease.argtypes = [vp]
    utf8, sint64 = 0x08000100, 4
    keys = {k: cf.CFStringCreateWithCString(None, k.encode(), utf8)
            for k in ("kCGWindowNumber", "kCGWindowOwnerPID", "kCGWindowLayer", "kCGWindowBounds")}
    # kCGWindowListOptionAll (0) | kCGWindowListExcludeDesktopElements (16)。別の Space の窓も数える
    arr = cg.CGWindowListCopyWindowInfo(16, 0)
    if not arr:
        for k in keys.values():
            if k:
                cf.CFRelease(k)
        raise MacWinError("CGWindowListCopyWindowInfo が何も返さない")

    def num(d, key):
        v = cf.CFDictionaryGetValue(d, keys[key])
        out = c.c_int64(0)
        if not v or not cf.CFNumberGetValue(v, sint64, c.byref(out)):
            return None
        return int(out.value)

    wins = []
    try:
        for i in range(cf.CFArrayGetCount(arr)):
            d = cf.CFArrayGetValueAtIndex(arr, i)
            b = cf.CFDictionaryGetValue(d, keys["kCGWindowBounds"])
            r = CGRect()
            if not b or not cg.CGRectMakeWithDictionaryRepresentation(b, c.byref(r)):
                continue
            wins.append({"number": num(d, "kCGWindowNumber"), "pid": num(d, "kCGWindowOwnerPID"),
                         "layer": num(d, "kCGWindowLayer"), "x": r.x, "y": r.y, "w": r.w, "h": r.h})
    finally:
        cf.CFRelease(arr)
        for k in keys.values():
            if k:
                cf.CFRelease(k)
    return wins


def pick_window(wins, pid, number=None):
    """kitty の窓を 1 つ選ぶ。number（platform_window_id）が合う窓、無ければ pid のふつうの層（0）の窓が 1 つだけのとき。"""
    if number is not None:
        hit = [w for w in wins if w.get("number") == number and w.get("pid") == pid]
        if len(hit) == 1:
            return hit[0]
    mine = [w for w in wins if w.get("pid") == pid and w.get("layer") == 0 and w.get("w", 0) > 1 and w.get("h", 0) > 1]
    return mine[0] if len(mine) == 1 else None


def titlebar_height(env, conf_values=()):
    """kitty のタイトルバーの高さ（ポイント）。$CLAUDE_BROWSER_MAC_TITLEBAR > hide_window_decorations > TITLEBAR。"""
    v = (env or {}).get("CLAUDE_BROWSER_MAC_TITLEBAR")
    if v not in (None, ""):
        try:
            return max(0, int(float(v)))
        except ValueError:
            pass
    hide = [val for key, val in conf_values if key == "hide_window_decorations"]
    if hide and hide[-1].strip().lower() in NO_TITLEBAR:
        return 0
    return TITLEBAR


def client_rect(win, titlebar):
    """窓の外枠から、kitty のクライアント領域 (x, y, w, h) を返す。"""
    t = min(max(0, titlebar), max(0, int(win["h"]) - 1))
    return (round(win["x"]), round(win["y"]) + t, round(win["w"]), round(win["h"]) - t)


def kitty_conf_values(env, home=None):
    """kitty.conf の (キー, 値)（include を辿る）。読めなければ空。"""
    home = home or os.path.expanduser("~")
    d = env.get("KITTY_CONFIG_DIRECTORY") or os.path.join(env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"),
                                                          "kitty")
    try:
        from doctor import read_kitty_conf
    except Exception:  # noqa: BLE001
        return []
    return read_kitty_conf(os.path.join(d, "kitty.conf"), d, env)


def kitty_client(pid, number, env, wins=None, conf_values=None):
    """kitty（pid、platform_window_id = number）のクライアント領域。決められなければ MacWinError。"""
    wins = window_list() if wins is None else wins
    w = pick_window(wins, pid, number)
    if w is None:
        n = len([x for x in wins if x.get("pid") == pid])
        raise MacWinError(f"kitty (pid {pid}) の窓を 1 つに決められない（候補 {n} 個）")
    conf_values = kitty_conf_values(env) if conf_values is None else conf_values
    return client_rect(w, titlebar_height(env, conf_values))
