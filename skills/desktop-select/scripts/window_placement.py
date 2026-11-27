"""Remember a window's Windows placement for coordinate-dependent gestures."""
import ctypes as C
from ctypes import wintypes as W
from functools import lru_cache
import time

import coordination


class _Placement(C.Structure):
    _fields_ = [('length', W.UINT), ('flags', W.UINT), ('showCmd', W.UINT),
                ('ptMinPosition', W.POINT), ('ptMaxPosition', W.POINT),
                ('rcNormalPosition', W.RECT)]


class _MonitorInfo(C.Structure):
    _fields_ = [('cbSize', W.DWORD), ('rcMonitor', W.RECT), ('rcWork', W.RECT),
                ('dwFlags', W.DWORD), ('szDevice', W.WCHAR * 32)]


@lru_cache(maxsize=1)
def _user():
    user = C.WinDLL('user32', use_last_error=True)
    for name, result, args in (
            ('GetWindowPlacement', W.BOOL, [W.HWND, C.POINTER(_Placement)]),
            ('SetWindowPlacement', W.BOOL, [W.HWND, C.POINTER(_Placement)]),
            ('GetWindowRect', W.BOOL, [W.HWND, C.POINTER(W.RECT)]),
            ('GetDpiForWindow', W.UINT, [W.HWND]),
            ('MonitorFromRect', W.HANDLE, [C.POINTER(W.RECT), W.DWORD]),
            ('MonitorFromWindow', W.HANDLE, [W.HWND, W.DWORD]),
            ('GetMonitorInfoW', W.BOOL, [W.HANDLE, C.POINTER(_MonitorInfo)]),
            ('IsIconic', W.BOOL, [W.HWND])):
        fn = getattr(user, name)
        fn.restype, fn.argtypes = result, args
    return user


def valid(spec):
    if type(spec) is not dict or set(spec) != {'rect', 'normal_rect', 'maximized', 'dpi', 'monitor'}:
        return False
    for key in ('rect', 'normal_rect'):
        r = spec[key]
        if not (type(r) is list and len(r) == 4
                and all(type(v) is int and -(2**30) < v < 2**30 for v in r)):
            return False
    w, h = spec['rect'][2:]
    l, t, r, b = spec['normal_rect']
    return (0 < w <= 65535 and 0 < h <= 65535 and 0 < r - l <= 65535 and 0 < b - t <= 65535
            and type(spec['maximized']) is bool and type(spec['dpi']) is int and 48 <= spec['dpi'] <= 960
            and isinstance(spec['monitor'], str) and 0 < len(spec['monitor']) < 32 and '\x00' not in spec['monitor'])


def _device(monitor):
    info = _MonitorInfo(cbSize=C.sizeof(_MonitorInfo))
    if monitor and _user().GetMonitorInfoW(monitor, C.byref(info)):
        return info.szDevice
    return ''


def capture(hwnd, *, expected_rect=None):
    """Record only visible geometry; callers may tie it to a particular frame."""
    if type(hwnd) is not int or hwnd <= 0:
        return None
    try:
        user = _user()
        p = _Placement(length=C.sizeof(_Placement))
        r = W.RECT()
        if (user.IsIconic(hwnd) or not user.GetWindowPlacement(hwnd, C.byref(p))
                or not user.GetWindowRect(hwnd, C.byref(r))):
            return None
        rect = [r.left, r.top, r.right - r.left, r.bottom - r.top]
        if expected_rect is not None and list(expected_rect) != rect:
            return None
        normal = p.rcNormalPosition
        spec = dict(rect=rect, normal_rect=[normal.left, normal.top, normal.right, normal.bottom],
                    maximized=p.showCmd == 3, dpi=user.GetDpiForWindow(hwnd),
                    monitor=_device(user.MonitorFromWindow(hwnd, 0)))
        return spec if valid(spec) else None
    except (OSError, AttributeError):
        return None


def _matches(current, spec):
    return bool(current and all(current[k] == spec[k] for k in ('rect', 'maximized', 'dpi', 'monitor')))


def _set(hwnd, spec, *, maximize):
    # Preserve native workspace coordinates. SetWindowPlacement (unlike
    # SetWindowPos) understands taskbar offsets and tool-window exceptions.
    user = _user()
    p = _Placement(length=C.sizeof(_Placement), flags=4, showCmd=3 if maximize else 4,
                   ptMinPosition=W.POINT(-1, -1), ptMaxPosition=W.POINT(-1, -1),
                   rcNormalPosition=W.RECT(*spec['normal_rect']))
    return user.SetWindowPlacement(hwnd, C.byref(p))


def restore(binding, spec, guard):
    """Return True after a verified change, False if already in place. Never send a gesture."""
    import win_bridge as bridge

    def unavailable():
        raise bridge.BridgeError('recorded_window_placement_unavailable')

    if not valid(spec):
        unavailable()
    hwnd = binding.hwnd
    if _matches(capture(hwnd), spec):
        return False
    with coordination.desktop_input():
        guard()
        binding.check()
        current = capture(hwnd)
        if _matches(current, spec):
            return False
        x, y, w, h = spec['rect']
        target = W.RECT(x, y, x + w, y + h)
        try:
            user = _user()
        except (OSError, AttributeError) as error:
            raise bridge.BridgeError('recorded_window_placement_unavailable') from error
        if (_device(user.MonitorFromRect(C.byref(target), 0)) != spec['monitor']
                or (current and current['monitor'] == spec['monitor'] and current['dpi'] != spec['dpi'])
                or (current is None and not user.IsIconic(hwnd))):
            unavailable()
        original, tick = bridge.foreground(), bridge.input_tick()
        bridge.wait_for_quiet_foreground(original, tick)

        def check():
            binding.check()
            bridge.check_foreground_input(original, tick, hwnd)

        def place(maximize, ready):
            check()
            if not _set(hwnd, spec, maximize=maximize):
                unavailable()
            deadline = time.monotonic() + .65
            while True:
                check()
                if ready(capture(hwnd)):
                    return
                if time.monotonic() >= deadline:
                    raise bridge.BridgeError('recorded_window_placement_unconfirmed')
                time.sleep(.02)

        # Windows can keep an already maximized window on its current monitor.
        # Establish the restored monitor before maximizing a cross-monitor window.
        if spec['maximized'] and _device(user.MonitorFromWindow(hwnd, 0)) != spec['monitor']:
            place(False, lambda state: state and not state['maximized'] and state['monitor'] == spec['monitor']
                  and state['dpi'] == spec['dpi'])
        place(spec['maximized'], lambda state: _matches(state, spec))
        guard()
        check()
        return True
