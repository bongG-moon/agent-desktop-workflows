"""Observe window identities for a recording. Never activate a candidate window."""
import ctypes as C
from collections import Counter
from ctypes import wintypes as W
import os

import win_bridge as bridge


_SHELL_CLASSES = {'Progman', 'WorkerW', 'Shell_TrayWnd', 'Shell_SecondaryTrayWnd', 'tooltips_class32', '#32768'}


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
        hwnd = bridge.normalize_hwnd(hwnd)
        if hwnd and bridge.user32.IsWindowVisible(hwnd):
            keys.add((hwnd, bridge.hwnd_pid(hwnd)))
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


def _lineage(pid, parents):
    visited = set()
    while pid in parents and pid not in visited:
        visited.add(pid)
        pid = parents[pid]
        if not pid:
            break
        yield pid


def _descendant(pid, created, parent, parents=None):
    chain = tuple(_lineage(pid, _parents() if parents is None else parents))
    if parent.pid not in chain:
        return False
    ticks = int(created)
    for ancestor in chain:
        if ancestor == parent.pid:
            return int(parent.created) <= ticks
        # A recycled PID cannot prove a relation to a process created earlier.
        try:
            _, started = bridge._process_identity(ancestor)
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


def _eligible_key(key, *, allow_minimized):
    hwnd, pid = key
    return (pid != os.getpid() and _key(hwnd) == key and bridge.user32.IsWindowVisible(hwnd)
            and (allow_minimized or not bridge.user32.IsIconic(hwnd))
            and bridge.window_class(hwnd) not in _SHELL_CLASSES)


def _valid_title(title):
    return bool(title and title == title.strip() and len(title) < 1023 and
                all(ord(ch) >= 32 and not 0xD800 <= ord(ch) <= 0xDFFF for ch in title))


def window_choices(connected, active, *, all_windows=False):
    """List possible recording surfaces only when the user opens the local picker."""
    anchors = {}
    for binding in connected:
        try:
            binding.check()
        except (bridge.BridgeError, OSError):
            continue
        anchors[binding.hwnd, binding.pid] = binding
    try:
        parents = {} if all_windows else _parents()
    except (bridge.BridgeError, OSError):
        parents = {}  # Same-process and owned windows remain discoverable.
    anchor_pids = {a.pid for a in anchors.values()}
    records = []
    for key in visible_window_keys():
        hwnd, pid = key
        try:
            if not _eligible_key(key, allow_minimized=True):
                continue
            direct = all_windows or pid in anchor_pids or any(bridge.owned(hwnd, a.hwnd) for a in anchors.values())
            if not direct and anchor_pids.isdisjoint(_lineage(pid, parents)):
                continue
            exe, created = bridge._process_identity(pid)
            if not direct and not any(_descendant(pid, created, a, parents) for a in anchors.values()):
                continue
            title = bridge.window_text(hwnd)
            if not _valid_title(title) or _key(hwnd) != key:
                continue
            linked = anchors.get(key)
            records.append(dict(hwnd=hwnd, pid=pid, created=created, exe=exe, title=title,
                                connected=bool(linked and linked.created == created),
                                current=bool(linked and linked is active and linked.created == created),
                                minimized=bool(bridge.user32.IsIconic(hwnd))))
        except (bridge.BridgeError, OSError):
            continue  # A closing or inaccessible window cannot be chosen.
    counts = Counter((r['exe'].casefold(), r['title']) for r in records)
    for row in records:
        row['ambiguous'] = counts[row['exe'].casefold(), row['title']] > 1
    return sorted(records, key=lambda r: (not r['current'], not r['connected'], r['exe'].casefold(),
                                          r['title'].casefold(), r['hwnd']))


def confirm_choice(choice):
    """Bind the listed lifetime and title only if still uniquely replayable."""
    from desktop_select import windows
    hwnd, pid, exe, title, created = (choice[k] for k in ('hwnd','pid','exe','title','created'))
    key = hwnd, pid
    if not _eligible_key(key, allow_minimized=True):
        raise bridge.BridgeError('recorded_window_lifetime_changed')
    binding = bridge.Binding(dict(hwnd=hwnd,pid=pid,exe_name=exe,process_started_utc_ticks=created))
    if bridge.window_text(hwnd) != title:
        raise bridge.BridgeError('recorded_window_lifetime_changed')
    matches = [r for r in windows(exe) if r['title'] == title]
    if len(matches) > 1:
        raise bridge.BridgeError('recorded_window_ambiguous')
    if (not matches or (matches[0]['hwnd'], matches[0]['pid'], matches[0]['process_started_utc_ticks'])
            != (hwnd, pid, created) or not _eligible_key(key, allow_minimized=True) or bridge.window_text(hwnd) != title):
        raise bridge.BridgeError('recorded_window_lifetime_changed')
    binding.check()
    return binding, title


def candidate(key, parent, title, *, automatic, baseline):
    """Resolve one pointed or related foreground window, scoped by its exact live title."""
    if key is None or key[1] == os.getpid() or key[0] == parent.hwnd:
        return None
    if automatic and (baseline is None or key in baseline):
        return None
    hwnd, pid = key
    parent.check()
    if bridge.window_text(parent.hwnd) != title or not _eligible_key(key, allow_minimized=False):
        return None
    exe, created = bridge._process_identity(pid)
    child = bridge.Binding(dict(hwnd=hwnd,pid=pid,exe_name=exe,process_started_utc_ticks=created))
    if automatic and not related(child,parent):
        return None
    # Read the title only after an explicit selection or confirmed relationship.
    child_title = bridge.window_text(hwnd)
    if not _valid_title(child_title):
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
