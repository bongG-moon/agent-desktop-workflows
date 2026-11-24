"""Identify an ID-less Win32 toolbar button by its demonstrated glyph inside a real button peer."""
import base64
from collections import Counter
import ctypes as C
from ctypes import wintypes as W
import time

import row_vision as vision
import win_bridge as bridge


SURFACE = 'win32-toolbar-glyph'
SIDE = 25


def _need(valid, reason):
    if not valid:
        raise bridge.BridgeError(reason)


def native_toolbar(binding, hwnd):
    return (bool(hwnd) and bridge.hwnd_pid(hwnd) == binding.pid
            and bridge.window_class(hwnd).casefold() == 'toolbarwindow32'
            and bridge.root_window(hwnd) == binding.hwnd and bridge.user32.IsWindowVisible(hwnd))


def _glyph(bounds):
    if min(bounds[2:]) < SIDE:
        return None
    return [bounds[0]+(bounds[2]-SIDE)//2, bounds[1]+(bounds[3]-SIDE)//2, SIDE, SIDE]


def _has_detail(rgb):
    # Reject a plain rectangle, separator, or disabled monochrome placeholder.
    for stride in (1, SIDE):
        n = 0
        for i in range(SIDE*SIDE-stride):
            if stride == 1 and i % SIDE == SIDE-1:
                continue
            if any(abs(rgb[3*i+c]-rgb[3*(i+stride)+c]) >= 25 for c in range(3)):
                n += 1
                if n >= 10:
                    break
        if n < 10:
            return False
    return True


def decoded(spec):
    _need(set(spec) == {'surface','rgb'} and spec['surface'] == SURFACE and isinstance(spec['rgb'], str)
          and len(spec['rgb']) == SIDE*SIDE*4, 'invalid_visual_target')
    try:
        rgb = base64.b64decode(spec['rgb'], validate=True)
    except ValueError as error:
        raise bridge.BridgeError('invalid_visual_target') from error
    _need(len(rgb) == SIDE*SIDE*3 and _has_detail(rgb), 'visual_target_lacks_detail')
    return rgb


def matches(reference, actual):
    if reference == actual:
        return True
    # The standard control tints the surrounding fill and border on hover.
    # Ignore the outer button border by construction. Within the glyph permit
    # only one uniformly recoloured flat fill. Every other pixel must agree.
    a = list(zip(reference[::3],reference[1::3],reference[2::3]))
    b = list(zip(actual[::3],actual[1::3],actual[2::3]))
    if len(a) != len(b) or len(a) != SIDE*SIDE:
        return False
    bg,count = Counter(a).most_common(1)[0]
    other = b[a.index(bg)]
    if not 20 <= count <= len(a)-25 or max(abs(v-w) for v,w in zip(bg,other)) > 48:
        return False
    return all(all(abs(v-w) <= 5 for v,w in zip(other if p==bg else p, q)) for p,q in zip(a,b))


def _peer(binding, uia, element):
    return (element is not None and uia.pid(element)==binding.pid and not uia.secure(element)
            and element.property(30003)==50000 and element.property(30010) is True
            and element.property(30022) is not True and element.property(30149) is not True
            and element.property(30040) is not True and element.property(30043) is not True)


def sample(binding, uia, window, point, hit):
    from visual_target import _safe_patch
    el = uia.from_point(*point)
    if not _peer(binding,uia,el) or el.property(30011):
        return None
    bounds = vision._bounds(uia,el)
    box = _glyph(bounds)
    if not box or not vision._inside([*point,1,1],bounds):
        return None
    _need(_safe_patch(binding,uia,box,point,exposed=True)==hit,'visual_point_not_exposed')
    start = time.monotonic()
    raw = vision._frame(binding,window,crop=box)
    end = time.monotonic()
    rgb = vision._rgb((box,(),raw),box)
    _need(_has_detail(rgb),'visual_target_lacks_detail')
    live = uia.from_point(*point)
    _need(tuple(bridge.cursor()) == point and live is not None and uia.same(el,live)
          and vision._bounds(uia,live)==bounds
          and _safe_patch(binding,uia,box,point,exposed=True)==hit,'visual_target_changed')
    spec = dict(surface=SURFACE,rgb=base64.b64encode(rgb).decode('ascii'))
    return start,end,hit,bounds,point,spec


def _toolbars(binding):
    handles = []
    callback = C.WINFUNCTYPE(W.BOOL,W.HWND,W.LPARAM)
    @callback
    def visit(hwnd, _):
        hwnd = bridge.normalize_hwnd(hwnd)
        if hwnd and native_toolbar(binding,hwnd):
            handles.append(hwnd)
        return len(handles) <= 16
    bridge._sig(bridge.user32,'EnumChildWindows',W.BOOL,W.HWND,callback,W.LPARAM)(binding.hwnd,visit,0)
    _need(len(handles)<=16,'visual_scope_incomplete')
    return handles


def observe(binding, spec):
    from visual_target import _safe_patch
    ref = decoded(spec)
    window = vision._window_rect(binding)
    found = None
    with bridge.UIA() as uia:
        root = uia.from_handle(binding.hwnd)
        count = 0
        pixels = None
        for hwnd in _toolbars(binding):
            bar = uia.from_handle(hwnd)
            if (bar is None or uia.pid(bar)!=binding.pid or uia.secure(bar)
                    or bar.property(30003)!=50021 or not uia.within(bar,root)):
                continue
            area = vision._bounds(uia,bar)
            for el in uia.children(bar):
                count += 1
                _need(count<=512,'visual_scope_incomplete')
                if not _peer(binding,uia,el):
                    continue
                box = _glyph(vision._bounds(uia,el))
                if not box or not vision._inside(box,area) or not vision._inside(box,window):
                    continue
                if pixels is None:
                    pixels = vision._frame(binding,window)
                if not matches(ref,vision._rgb((window,(),pixels),box)):
                    continue
                _need(found is None,'visual_target_ambiguous')
                point = [box[0]+SIDE//2,box[1]+SIDE//2]
                _safe_patch(binding,uia,box,point,exposed=False)
                found = dict(window=window,box=box,point=point,toolbar=hwnd)
    _need(found is not None,'visual_target_not_found')
    _need(window==vision._window_rect(binding),'visual_window_moved')
    return found


def exposed(binding,uia,current):
    point = current['point']
    hit = bridge.window_from_point(*point)
    el = uia.from_point(*point)
    return (hit==current['toolbar'] and native_toolbar(binding,hit) and _peer(binding,uia,el)
            and _glyph(vision._bounds(uia,el))==current['box'])
