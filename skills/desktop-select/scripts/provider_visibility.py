"""Missing IsOffscreen is unknown: use current owned-window geometry to resolve it."""
import ctypes as C
from ctypes import wintypes as W
import math
import time

import win_bridge as win


def valid(box):
    return isinstance(box, dict) and all(type(box.get(k)) in (int, float) and math.isfinite(box[k])
           for k in ('x', 'y', 'width', 'height')) and box['width'] > 0 and box['height'] > 0


def inside(box, window):
    return valid(box) and valid(window) and all(
        window[axis] <= box[axis] and box[axis]+box[size] <= window[axis]+window[size]
        for axis,size in (('x','width'), ('y','height')))


def _shown(hwnd):
    return bool(win.user32.IsWindowVisible(hwnd)) and not win.user32.IsIconic(hwnd)


def _window_box(hwnd, uia, root):
    box = uia.bounds(root)
    if valid(box):
        return box
    rect = win.RECT()
    fn = win._sig(win.user32, 'GetWindowRect', W.BOOL, W.HWND, C.POINTER(win.RECT))
    if not fn(hwnd, C.byref(rect)):
        return None
    return dict(x=rect.left, y=rect.top, width=rect.right-rect.left, height=rect.bottom-rect.top)


def visible(binding, uia, item, root=None):
    """False+valid is normal; None also requires shown HWND, ancestry and full containment."""
    state, box = item.property(30022), uia.bounds(item)
    if not valid(box):
        return False
    if state is False:
        return True
    if state is not None:  # Explicit True and unexpected provider data are never geometry overrides.
        return False
    binding.check()
    if uia.pid(item) != binding.pid or uia.secure(item):
        return False
    handles = [binding.hwnd] if root is not None else binding.scoped_windows()
    for hwnd in handles:
        if not _shown(hwnd):
            continue
        candidate = root if root is not None else uia.from_handle(hwnd)
        if (candidate is None or uia.pid(candidate) != binding.pid or uia.secure(candidate)
                or not uia.within(item, candidate)):
            continue
        if inside(box, _window_box(hwnd, uia, candidate)):
            binding.check()
            return True
    return False


def restore_for_action(binding, title):
    """Restore only an authorized mutation's exact target; read-only commands never call this."""
    binding.check()
    if win.window_text(binding.hwnd) != title:
        raise win.BridgeError('business_window_changed')
    if not win.user32.IsIconic(binding.hwnd):
        return False
    # Reuse scoped, input-idle activation; no synthetic click or keypress.
    from row_action import foreground
    foreground(binding)
    deadline = time.monotonic()+1.5
    while True:
        binding.check()
        if win.window_text(binding.hwnd) != title:
            raise win.BridgeError('business_window_changed')
        if _shown(binding.hwnd):
            return True
        if time.monotonic() >= deadline:
            raise win.BridgeError('target_window_restore_failed')
        time.sleep(.05)
