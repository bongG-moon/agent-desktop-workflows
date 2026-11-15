"""Observe window identities for a recording. Never activate a candidate window."""
import ctypes as C
from ctypes import wintypes as W
import os

import win_bridge as bridge


class _ProcessEntry(C.Structure):
    _fields_ = [
        ('size', W.DWORD), ('usage', W.DWORD), ('pid', W.DWORD),
        ('heap', C.c_size_t), ('module', W.DWORD), ('threads', W.DWORD),
        ('parent', W.DWORD), ('priority', W.LONG), ('flags', W.DWORD),
        ('name', W.WCHAR * 260),
    ]


def visible_window_keys():
    """Snapshot only handles and PIDs. Do not read unrelated captions or UI trees."""
    bridge._windows()
    keys = set()
    callback = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    def visit(hwnd, _):
        if bridge.user32.IsWindowVisible(hwnd):
            keys.add((int(hwnd), bridge.hwnd_pid(hwnd)))
        return True
    enum = bridge._sig(bridge.user32, 'EnumWindows', W.BOOL, callback, W.LPARAM)
    if not enum(callback(visit), 0):
        raise bridge.BridgeError('window_list_incomplete')
    return keys


def _key(hwnd):
    root = bridge.root_window(hwnd) if hwnd else 0
    pid = bridge.hwnd_pid(root) if root else 0
    return (root, pid) if root and pid else None


def foreground_key():
    return _key(bridge.foreground())


def pointed_key():
    return _key(bridge.window_from_point(*bridge.cursor()))


def _parents():
    # Toolhelp's parent PID is useful only together with process creation times.
    kernel = bridge.kernel32
    snapshot = bridge._sig(kernel, 'CreateToolhelp32Snapshot', W.HANDLE, W.DWORD, W.DWORD)(2, 0)
    if not snapshot or snapshot == C.c_void_p(-1).value:
        raise bridge.BridgeError('process_tree_unavailable')
    try:
        first = bridge._sig(kernel, 'Process32FirstW', W.BOOL, W.HANDLE, C.POINTER(_ProcessEntry))
        following = bridge._sig(kernel, 'Process32NextW', W.BOOL, W.HANDLE, C.POINTER(_ProcessEntry))
        entry = _ProcessEntry()
        entry.size = C.sizeof(entry)
        parents = {}
        ok = first(snapshot, C.byref(entry))
        while ok:
            parents[entry.pid] = entry.parent
            ok = following(snapshot, C.byref(entry))
        return parents
    finally:
        kernel.CloseHandle(snapshot)


def _descendant(pid, created, parent):
    parents = _parents()
    visited = set()
    ticks = int(created)
    while pid in parents and pid not in visited:
        visited.add(pid)
        pid = parents[pid]
        if not pid:
            break
        if pid == parent.pid:
            return int(parent.created) <= ticks
        # A recycled PID cannot prove a relation to a process created earlier.
        try:
            _, started = bridge._process_identity(pid)
        except (bridge.BridgeError, OSError):
            break
        if int(started) > ticks:
            break
        ticks = int(started)
    return False


def related(child, parent):
    child.check()
    parent.check()
    return (child.pid == parent.pid or bridge.owned(child.hwnd,parent.hwnd)
            or _descendant(child.pid,child.created,parent))


def candidate(key, parent, title, *, automatic, baseline):
    """Resolve one pointed or related foreground window, scoped by its exact live title."""
    if key is None or key[1] == os.getpid() or key[0] == parent.hwnd:
        return None
    if automatic and (baseline is None or key in baseline):
        return None
    hwnd, pid = key
    parent.check()
    if (bridge.window_text(parent.hwnd) != title or _key(hwnd) != key
            or not bridge.user32.IsWindowVisible(hwnd) or bridge.user32.IsIconic(hwnd)):
        return None
    if bridge.window_class(hwnd) in ('Progman', 'WorkerW', 'Shell_TrayWnd', 'Shell_SecondaryTrayWnd',
                                     'tooltips_class32', '#32768'):
        return None
    exe, created = bridge._process_identity(pid)
    child = bridge.Binding(dict(hwnd=hwnd,pid=pid,exe_name=exe,process_started_utc_ticks=created))
    if automatic and not related(child,parent):
        return None
    # Read the title only after an explicit selection or confirmed relationship.
    child_title = bridge.window_text(hwnd)
    if not child_title.strip() or child_title != child_title.strip() or len(child_title) >= 1023:
        return None
    from desktop_select import windows
    matches = [r for r in windows(exe) if r['title'] == child_title]
    if (len(matches) != 1 or matches[0]['hwnd'] != hwnd or matches[0]['pid'] != pid
            or matches[0]['process_started_utc_ticks'] != created):
        return None
    child.check()
    parent.check()
    if bridge.window_text(hwnd) != child_title:
        return None
    return child, child_title
