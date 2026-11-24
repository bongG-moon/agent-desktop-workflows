"""Locate the modern Windows Notepad Find Next button in its Find overlay."""

import ctypes as C
from ctypes import wintypes as W

import row_vision as vision
import win_bridge as win


_PACKAGE = 'Microsoft.WindowsNotepad_8wekyb3d8bbwe'
_OVERLAY = 'Microsoft.UI.Content.DesktopChildSiteBridge'
_EDIT = 'RichEditD2DPT'
_EDIT_PARENT = 'NotepadTextBox'


def is_notepad(binding):
    return (getattr(binding, 'exe', '').casefold() == 'notepad.exe'
            and win.window_class(binding.hwnd) == 'Notepad')


def _require(condition, diagnostic):
    if not condition:
        raise win.BridgeError(diagnostic)


def _package_family(pid):
    open_process = win._sig(win.kernel32, 'OpenProcess', W.HANDLE, W.DWORD, W.BOOL, W.DWORD)
    process = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        return ''
    try:
        family = win._sig(win.kernel32, 'GetPackageFamilyName', W.LONG, W.HANDLE,
                          C.POINTER(W.UINT), W.LPWSTR)
        name = C.create_unicode_buffer(128)
        size = W.UINT(len(name))
        return name.value if family(process, C.byref(size), name) == 0 else ''
    finally:
        win.kernel32.CloseHandle(process)


def _rect(hwnd):
    rect = win.RECT()
    read = win._sig(win.user32, 'GetWindowRect', W.BOOL, W.HWND, C.POINTER(win.RECT))
    if read(hwnd, C.byref(rect)):
        return [rect.left, rect.top, rect.right-rect.left, rect.bottom-rect.top]
    return [0, 0, 0, 0]


def _overlay(binding, window, scale):
    candidates = []
    enabled = win._sig(win.user32, 'IsWindowEnabled', W.BOOL, W.HWND)
    hwnd = win.normalize_hwnd(win.user32.GetWindow(binding.hwnd, 5))  # GW_CHILD
    for _ in range(128):
        if not hwnd:
            break
        if (win.window_class(hwnd) == _OVERLAY and win.user32.IsWindowVisible(hwnd)
                and enabled(hwnd) and win.hwnd_pid(hwnd) == binding.pid
                and win.root_window(hwnd) == binding.hwnd):
            rect = _rect(hwnd)
            if (vision._inside(rect, window) and rect[2] >= 180*scale and 60*scale <= rect[3] <= 92*scale
                    and 55*scale <= rect[1]-window[1] <= 110*scale):
                candidates.append((hwnd, rect))
        hwnd = win.normalize_hwnd(win.user32.GetWindow(hwnd, 2))  # GW_HWNDNEXT
    _require(not hwnd and len(candidates) == 1, 'notepad_find_overlay_unavailable')
    return candidates[0]


def _arrow(pixels, strip, center, scale, direction):
    """Match the shaft and both diagonal strokes against their local background."""
    origin_x, origin_y, width, height = strip
    center_x, center_y = (center[0]-origin_x, center[1]-origin_y)
    radius = round(11*scale)
    if not (len(pixels) == width*height*4 and radius <= center_x < width-radius
            and radius <= center_y < height-radius):
        return None
    samples = []
    for y in range(center_y-radius, center_y+radius+1):
        for x in range(center_x-radius, center_x+radius+1):
            offset = 4*(y*width+x)
            blue, green, red = pixels[offset:offset+3]
            samples.append((x, y, (blue+2*green+red)//4))
    luminance = sorted(level for _, _, level in samples)
    background = luminance[len(luminance)//2]
    stroke = [(x, y) for x, y, level in samples if abs(level-background) >= 70]
    if not stroke:
        return None
    left, right = min(x for x, _ in stroke), max(x for x, _ in stroke)
    top, bottom = min(y for _, y in stroke), max(y for _, y in stroke)
    glyph_width, glyph_height = right-left+1, bottom-top+1
    if not (13*scale <= glyph_width <= 18*scale and 13*scale <= glyph_height <= 18*scale):
        return None
    x_mid, y_mid = (left+right)/2, (top+bottom)/2
    if abs(x_mid-center_x) > 3*scale or abs(y_mid-center_y) > 3*scale:
        return None
    head_edge = bottom if direction == 1 else top
    tolerance = max(1.15, .9*scale)
    vertices = sorted(y+direction*abs(x-x_mid) for x, y in stroke
                      if abs(x-x_mid) > tolerance and direction*(y-y_mid) >= -tolerance)
    if not vertices:
        return None
    vertex = vertices[len(vertices)//2]
    if abs(vertex-head_edge) > 1.4*scale:
        return None
    off_stroke = sum(
        abs(x-x_mid) > tolerance
        and (direction*(y-y_mid) < -tolerance or abs(y-vertex+direction*abs(x-x_mid)) > tolerance)
        for x, y in stroke
    )
    if off_stroke > .12*len(stroke):
        return None
    points = [(x_mid, y_mid+part*glyph_height) for part in (-.42, -.24, 0, .24, .42)]
    for side in (-1, 1):
        points.extend((x_mid+side*part*glyph_width, vertex-direction*part*glyph_width)
                      for part in (.18, .34, .45))
    if not all(any(max(abs(x-target_x), abs(y-target_y)) <= tolerance for x, y in stroke)
               for target_x, target_y in points):
        return None
    return [origin_x+round(x_mid), origin_y+round(y_mid)]


def _strip(rect, scale):
    left, top, width, height = rect
    # The two adjacent arrows occupy the row between the query and Settings.
    right = left+width
    center_y = round(top+height/2)
    start = right-round(169*scale)
    end = right-round(100*scale)
    radius = round(13*scale)
    return [start, center_y-radius, end-start, 2*radius+1]


def _find_arrows(pixels, strip, overlay, scale):
    right = overlay[0]+overlay[2]
    center_y = round(overlay[1]+overlay[3]/2)
    down = _arrow(pixels, strip, (right-round(155*scale), center_y), scale, 1)
    up = _arrow(pixels, strip, (right-round(115*scale), center_y), scale, -1)
    if (down and up and abs(up[0]-down[0]-40*scale) <= 2*scale
            and abs(up[1]-down[1]) <= scale):
        return down
    return None


def _exposed_hit(binding, overlay, point):
    _require(win.root_window(win.foreground()) == binding.hwnd, 'notepad_find_not_foreground')
    hit = win.window_from_point(*point)
    _require(hit and win.root_window(hit) == binding.hwnd and win.hwnd_pid(hit) == binding.pid,
             'notepad_find_point_not_exposed')
    if hit == overlay or win.user32.IsChild(overlay, hit):
        return hit
    # WinUI composes the Find panel above the editor, but WindowFromPoint can
    # report the underlying RichEdit. Accept only this known native hierarchy.
    parent = win.normalize_hwnd(win.user32.GetAncestor(hit, 1))  # GA_PARENT
    _require(parent and win.window_class(hit) == _EDIT and win.window_class(parent) == _EDIT_PARENT
             and win.normalize_hwnd(win.user32.GetAncestor(parent, 1)) == binding.hwnd,
             'notepad_find_point_not_exposed')
    return hit


def locate(binding, *, exposed=False):
    """Find the downward arrow using only the installed Notepad's bounded overlay."""
    binding.check()
    _require(is_notepad(binding) and _package_family(binding.pid).casefold() == _PACKAGE.casefold(),
             'notepad_find_package_unavailable')
    _require(win.user32.IsWindowVisible(binding.hwnd) and not win.user32.IsIconic(binding.hwnd),
             'notepad_find_overlay_unavailable')
    affinity = W.DWORD()
    read_affinity = win._sig(win.user32, 'GetWindowDisplayAffinity', W.BOOL, W.HWND, C.POINTER(W.DWORD))
    _require(read_affinity(binding.hwnd, C.byref(affinity)) and not affinity.value,
             'visual_window_capture_protected')
    dpi = binding.info()['dpi']
    _require(96 <= dpi <= 384, 'notepad_find_dpi_unsupported')
    scale = dpi/96
    window = vision._window_rect(binding)
    overlay, rect = _overlay(binding, window, scale)
    strip = _strip(rect, scale)
    _require(vision._inside(strip, rect), 'notepad_find_overlay_unavailable')
    if exposed:
        # Check the visible receiver before calling PrintWindow as well as after.
        _exposed_hit(binding, overlay, (rect[0]+rect[2]-round(155*scale), rect[1]+rect[3]//2))
    pixels = vision._frame(binding, window, crop=strip)
    point = _find_arrows(pixels, strip, rect, scale)
    _require(point is not None, 'notepad_find_arrow_unavailable')
    _require(_overlay(binding, window, scale) == (overlay, rect), 'notepad_find_overlay_changed')
    radius = round(9*scale)
    box = [point[0]-radius, point[1]-radius, 2*radius+1, 2*radius+1]
    _require(vision._inside(box, strip), 'notepad_find_overlay_changed')
    hit = _exposed_hit(binding, overlay, point) if exposed else None
    binding.check()
    return dict(point=point, box=box, window=window, overlay=overlay, hit=hit)


def contains_click(found, x, y):
    return vision._inside([x, y, 1, 1], found['box'])
