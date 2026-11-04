"""Windows UI Automation and guarded input using Python's standard library.

No generated assemblies, downloaded modules, driver, service or executable is
used. COM slots/property IDs follow Windows SDK UIAutomationClient.h/.idl.
All COM interfaces belong to the thread/apartment which created their UIA object.
"""
from __future__ import annotations
from collections import deque
import ctypes as C
from ctypes import wintypes as W
import math
import ntpath
import os
import re
import struct
import threading
import time
import uuid
import weakref


class BridgeError(RuntimeError):
    def __init__(self, code, dispatched=False):
        super().__init__(code if isinstance(code, str) and re.fullmatch(r'[a-z_]{1,80}', code) else 'bridge_operation_failed')
        self.code = str(self)
        self.dispatched = dispatched
        self.input_dispatched = dispatched


class POINT(C.Structure):
    _fields_ = [('x', C.c_int32), ('y', C.c_int32)]


class RECT(C.Structure):
    _fields_ = [('left', C.c_int32), ('top', C.c_int32), ('right', C.c_int32), ('bottom', C.c_int32)]


class GUID(C.Structure):
    _fields_ = [('Data1', C.c_uint32), ('Data2', C.c_uint16), ('Data3', C.c_uint16), ('Data4', C.c_ubyte * 8)]

    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


class _VariantData(C.Union):
    # VARIANT's BRECORD has two pointers; preserving this union size is essential.
    _fields_ = [('i4', C.c_int32), ('ui4', C.c_uint32), ('i8', C.c_int64),
                ('r8', C.c_double), ('boolean', C.c_int16), ('pointer', C.c_void_p),
                ('record', C.c_void_p * 2)]


class VARIANT(C.Structure):
    _anonymous_ = ('data',)
    _fields_ = [('vt', C.c_uint16), ('r1', C.c_uint16), ('r2', C.c_uint16), ('r3', C.c_uint16), ('data', _VariantData)]


class MOUSEINPUT(C.Structure):
    _fields_ = [('dx', C.c_int32), ('dy', C.c_int32), ('mouseData', C.c_uint32),
                ('dwFlags', C.c_uint32), ('time', C.c_uint32), ('dwExtraInfo', C.c_size_t)]


class KEYBDINPUT(C.Structure):
    _fields_ = [('wVk', C.c_uint16), ('wScan', C.c_uint16), ('dwFlags', C.c_uint32),
                ('time', C.c_uint32), ('dwExtraInfo', C.c_size_t)]


class _InputUnion(C.Union):
    _fields_ = [('mi', MOUSEINPUT), ('ki', KEYBDINPUT)]


class INPUT(C.Structure):
    _anonymous_ = ('data',)
    _fields_ = [('type', C.c_uint32), ('data', _InputUnion)]


user32 = kernel32 = ole32 = oleaut32 = None
if os.name == 'nt':
    user32 = C.WinDLL('user32', use_last_error=True)
    kernel32 = C.WinDLL('kernel32', use_last_error=True)
    ole32 = C.WinDLL('ole32', use_last_error=True)
    oleaut32 = C.WinDLL('oleaut32', use_last_error=True)

    def _sig(dll, name, result, *args):
        fn = getattr(dll, name)
        fn.restype, fn.argtypes = result, list(args)
        return fn

    _sig(user32, 'IsWindow', W.BOOL, W.HWND)
    _sig(user32, 'IsWindowVisible', W.BOOL, W.HWND)
    _sig(user32, 'IsIconic', W.BOOL, W.HWND)
    _sig(user32, 'IsChild', W.BOOL, W.HWND, W.HWND)
    _sig(user32, 'GetWindow', W.HWND, W.HWND, W.UINT)
    _sig(user32, 'GetAncestor', W.HWND, W.HWND, W.UINT)
    _sig(user32, 'GetWindowThreadProcessId', W.DWORD, W.HWND, C.POINTER(W.DWORD))
    _sig(user32, 'GetWindowTextW', C.c_int, W.HWND, W.LPWSTR, C.c_int)
    _sig(user32, 'GetClassNameW', C.c_int, W.HWND, W.LPWSTR, C.c_int)
    _sig(user32, 'GetForegroundWindow', W.HWND)
    _sig(user32, 'WindowFromPoint', W.HWND, POINT)
    _sig(user32, 'GetCursorPos', W.BOOL, C.POINTER(POINT))
    _sig(user32, 'SetForegroundWindow', W.BOOL, W.HWND)
    _sig(user32, 'ShowWindow', W.BOOL, W.HWND, C.c_int)
    _sig(user32, 'GetSystemMetrics', C.c_int, C.c_int)
    _sig(user32, 'GetAsyncKeyState', C.c_short, C.c_int)
    _sig(user32, 'SendInput', W.UINT, W.UINT, C.POINTER(INPUT), C.c_int)
    _sig(kernel32, 'OpenProcess', W.HANDLE, W.DWORD, W.BOOL, W.DWORD)
    _sig(kernel32, 'CloseHandle', W.BOOL, W.HANDLE)
    _sig(kernel32, 'ProcessIdToSessionId', W.BOOL, W.DWORD, C.POINTER(W.DWORD))
    _sig(kernel32, 'QueryFullProcessImageNameW', W.BOOL, W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD))
    _sig(kernel32, 'GetProcessTimes', W.BOOL, W.HANDLE, C.POINTER(C.c_uint64), C.POINTER(C.c_uint64), C.POINTER(C.c_uint64), C.POINTER(C.c_uint64))
    _sig(ole32, 'CoInitializeEx', C.c_int32, C.c_void_p, W.DWORD)
    _sig(ole32, 'CoUninitialize', None)
    _sig(ole32, 'CoCreateInstance', C.c_int32, C.POINTER(GUID), C.c_void_p, W.DWORD, C.POINTER(GUID), C.POINTER(C.c_void_p))
    _sig(oleaut32, 'VariantClear', C.c_int32, C.POINTER(VARIANT))
    _sig(oleaut32, 'SysStringLen', W.UINT, C.c_void_p)


def _windows():
    if os.name != 'nt':
        raise BridgeError('windows_required')
    if struct.calcsize('P') != 8:
        raise BridgeError('python_x64_required')


def set_dpi_awareness():
    _windows()
    try:
        fn = _sig(user32, 'SetProcessDpiAwarenessContext', W.BOOL, C.c_void_p)
        fn(C.c_void_p(-4))
    except AttributeError:
        _sig(user32, 'SetProcessDPIAware', W.BOOL)()


configure_dpi = set_dpi_awareness


def hwnd_pid(hwnd):
    _windows()
    pid = W.DWORD()
    user32.GetWindowThreadProcessId(hwnd, C.byref(pid))
    return pid.value


def root_window(hwnd):
    return int(user32.GetAncestor(hwnd, 2) or 0)


def window_text(hwnd):
    out = C.create_unicode_buffer(1024)
    user32.GetWindowTextW(hwnd, out, len(out))
    return out.value


def window_class(hwnd):
    out = C.create_unicode_buffer(512)
    user32.GetClassNameW(hwnd, out, len(out))
    return out.value


def foreground():
    return int(user32.GetForegroundWindow() or 0)


def window_from_point(x, y):
    return int(user32.WindowFromPoint(POINT(int(x), int(y))) or 0)


def cursor():
    p = POINT()
    if not user32.GetCursorPos(C.byref(p)):
        raise BridgeError('cursor_unavailable')
    return p.x, p.y


def owned(child, owner):
    seen = set()
    for _ in range(12):
        if not child or child in seen:
            break
        seen.add(child)
        child = int(user32.GetWindow(child, 4) or 0)
        if child == owner:
            return True
    return False


def _process_identity(pid):
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        raise BridgeError('target_unavailable')
    try:
        length, path = W.DWORD(32768), C.create_unicode_buffer(32768)
        times = [C.c_uint64() for _ in range(4)]
        if not kernel32.QueryFullProcessImageNameW(handle, 0, path, C.byref(length)) or not kernel32.GetProcessTimes(handle, *[C.byref(v) for v in times]):
            raise BridgeError('target_unavailable')
        return ntpath.basename(path.value), str(times[0].value + 504911232000000000)
    finally:
        kernel32.CloseHandle(handle)


class Binding:
    def __init__(self, request):
        _windows()
        if not isinstance(request, dict):
            raise BridgeError('invalid_target')
        self.pid, self.hwnd, self.exe = request.get('pid'), request.get('hwnd'), request.get('exe_name')
        if type(self.pid) is not int or not 1 <= self.pid <= 0x7fffffff or type(self.hwnd) is not int or not 1 <= self.hwnd <= 0x7fffffffffffffff:
            raise BridgeError('invalid_target')
        if not isinstance(self.exe, str) or not self.exe.lower().endswith('.exe') or not 1 <= len(self.exe) <= 200 or re.search(r'[<>:"/\\|?*\x00-\x1f]', self.exe) or self.exe.strip() != self.exe:
            raise BridgeError('invalid_exe_name')
        self.created = self._identity()
        if request.get('process_started_utc_ticks') is not None and request['process_started_utc_ticks'] != self.created:
            raise BridgeError('target_lifetime_changed')

    def _identity(self):
        if not user32.IsWindow(self.hwnd) or hwnd_pid(self.hwnd) != self.pid:
            raise BridgeError('target_window_mismatch')
        target_session, our_session = W.DWORD(), W.DWORD()
        if not kernel32.ProcessIdToSessionId(self.pid, C.byref(target_session)) or not kernel32.ProcessIdToSessionId(os.getpid(), C.byref(our_session)):
            raise BridgeError('target_unavailable')
        if target_session.value != our_session.value:
            raise BridgeError('different_session')
        exe, created = _process_identity(self.pid)
        if exe.casefold() != self.exe.casefold():
            raise BridgeError('target_executable_mismatch')
        return created

    def check(self):
        if self._identity() != self.created:
            raise BridgeError('target_lifetime_changed')

    def contains_window(self, hwnd):
        if not hwnd or hwnd_pid(hwnd) != self.pid:
            return False
        root = root_window(hwnd)
        return root == self.hwnd or bool(user32.IsChild(self.hwnd, hwnd)) or owned(root, self.hwnd)

    def info(self):
        dpi = 96
        try:
            dpi = _sig(user32, 'GetDpiForWindow', W.UINT, W.HWND)(self.hwnd) or 96
        except AttributeError:
            pass
        return {'pid': self.pid, 'hwnd': self.hwnd, 'exe_name': self.exe, 'title': window_text(self.hwnd),
                'class_name': window_class(self.hwnd), 'owner_hwnd': int(user32.GetWindow(self.hwnd, 4) or 0),
                'root_owner_hwnd': int(user32.GetAncestor(self.hwnd, 3) or 0), 'process_started_utc_ticks': self.created,
                'visible': bool(user32.IsWindowVisible(self.hwnd)), 'minimized': bool(user32.IsIconic(self.hwnd)), 'dpi': dpi}


def _check_hresult(result):
    if result < 0:
        raise BridgeError('accessibility_unavailable')


class _Com:
    def __init__(self, pointer, uia):
        self.ptr = C.c_void_p(pointer.value if isinstance(pointer, C.c_void_p) else pointer)
        self.uia = uia
        uia._objects.add(self)

    def call(self, slot, argtypes, *args):
        self.uia._thread()
        if not self.ptr.value:
            raise BridgeError('element_disappeared')
        table = C.cast(self.ptr, C.POINTER(C.POINTER(C.c_void_p))).contents
        method = C.WINFUNCTYPE(C.c_int32, C.c_void_p, *argtypes)(table[slot])
        _check_hresult(method(self.ptr, *args))

    def close(self):
        if self.ptr.value:
            self.uia._thread(allow_closed=True)
            table = C.cast(self.ptr, C.POINTER(C.POINTER(C.c_void_p))).contents
            C.WINFUNCTYPE(C.c_uint32, C.c_void_p)(table[2])(self.ptr)
            self.ptr = C.c_void_p()

    def __del__(self):
        try:
            if threading.get_ident() == self.uia.owner:
                self.close()
        except Exception:
            pass


class Element(_Com):
    def property(self, property_id):
        variant = VARIANT()
        try:
            # GetCurrentPropertyValueEx(ignoreDefaultValue=True) retains unknowns.
            self.call(11, (C.c_int32, W.BOOL, C.POINTER(VARIANT)), property_id, True, C.byref(variant))
            if variant.vt == 8:
                n = oleaut32.SysStringLen(variant.pointer) if variant.pointer else 0
                if n > 1024 * 1024:
                    raise BridgeError('accessibility_value_too_large')
                return C.wstring_at(variant.pointer, n) if n else ''
            if variant.vt == 11:
                return bool(variant.boolean)
            if variant.vt == 3:
                return int(variant.i4)
            if variant.vt == 19:
                return int(variant.ui4)
            if variant.vt == 20:
                return int(variant.i8)
            if variant.vt == 5:
                return float(variant.r8) if math.isfinite(variant.r8) else None
            return None  # VT_UNKNOWN includes the reserved NotSupported sentinel.
        finally:
            oleaut32.VariantClear(C.byref(variant))


ROLES = {50000: 'button', 50001: 'calendar', 50002: 'checkbox', 50003: 'combobox', 50004: 'textfield',
         50005: 'hyperlink', 50006: 'image', 50007: 'listitem', 50008: 'list', 50009: 'menu', 50010: 'menu',
         50011: 'menuitem', 50012: 'progressbar', 50013: 'radiobutton', 50014: 'scrollbar', 50015: 'slider',
         50016: 'spinner', 50017: 'statusbar', 50018: 'tabgroup', 50019: 'tab', 50020: 'statictext',
         50021: 'toolbar', 50022: 'tooltip', 50023: 'tree', 50024: 'treeitem', 50025: 'custom',
         50026: 'group', 50027: 'thumb', 50028: 'table', 50029: 'row', 50030: 'document', 50031: 'splitbutton',
         50032: 'window', 50033: 'group', 50034: 'header', 50035: 'headeritem', 50036: 'table',
         50037: 'titlebar', 50038: 'separator'}


class UIA:
    def __init__(self):
        _windows()
        self.owner = threading.get_ident()
        self.closed = False
        self._objects = weakref.WeakSet()
        self._initialized = False
        self.client = self.walker = None
        # Dedicated helpers/workers use MTA. UIA is never initialized on Tk's UI thread.
        _check_hresult(ole32.CoInitializeEx(None, 0))
        self._initialized = True
        try:
            clsid = GUID.parse('ff48dba4-60ef-4201-aa87-54103eef594e')
            iid = GUID.parse('30cbe57d-d9d0-452a-ab13-7ac5ac4825ee')
            pointer = C.c_void_p()
            _check_hresult(ole32.CoCreateInstance(C.byref(clsid), None, 1, C.byref(iid), C.byref(pointer)))
            self.client = _Com(pointer, self)
            walker = C.c_void_p()
            self.client.call(14, (C.POINTER(C.c_void_p),), C.byref(walker))
            self.walker = _Com(walker, self)
        except BaseException:
            self.close()
            raise

    def _thread(self, allow_closed=False):
        if threading.get_ident() != self.owner:
            raise BridgeError('accessibility_wrong_thread')
        if self.closed and not allow_closed:
            raise BridgeError('accessibility_context_closed')

    def close(self):
        self._thread(allow_closed=True)
        if self.closed:
            return
        for obj in list(self._objects):
            obj.close()
        self.closed = True
        if self._initialized:
            ole32.CoUninitialize()
            self._initialized = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _element(self, method, argtypes, *args):
        pointer = C.c_void_p()
        self.client.call(method, (*argtypes, C.POINTER(C.c_void_p)), *args, C.byref(pointer))
        return Element(pointer, self) if pointer.value else None

    def from_handle(self, hwnd):
        return self._element(6, (W.HWND,), hwnd)

    def from_point(self, x, y):
        return self._element(7, (POINT,), POINT(int(x), int(y)))

    def focused(self):
        return self._element(8, ())

    def _walk(self, slot, element):
        pointer = C.c_void_p()
        self.walker.call(slot, (C.c_void_p, C.POINTER(C.c_void_p)), element.ptr, C.byref(pointer))
        return Element(pointer, self) if pointer.value else None

    def parent(self, element):
        return self._walk(3, element)

    def children(self, element):
        child = self._walk(4, element)
        count = 0
        while child is not None:
            if count >= 400:
                raise BridgeError('too_many_children')
            yield child
            child = self._walk(6, child)
            count += 1

    def same(self, a, b):
        if a is None or b is None:
            return False
        result = W.BOOL()
        self.client.call(3, (C.c_void_p, C.c_void_p, C.POINTER(W.BOOL)), a.ptr, b.ptr, C.byref(result))
        return bool(result.value)

    def within(self, element, parent):
        for _ in range(32):
            if element is None:
                return False
            if self.same(element, parent):
                return True
            element = self.parent(element)
        return False

    def pid(self, element):
        return element.property(30002)

    def secure(self, element):
        # Unknown security metadata is not permission to observe text or send input.
        return element.property(30019) is not False

    def basic(self, element):
        name, identifier = element.property(30005), element.property(30011)
        result = {'role': ROLES.get(element.property(30003), 'unknown')}
        if isinstance(identifier, str) and identifier and not identifier.isdecimal():
            result['automation_id'] = identifier
        if isinstance(name, str) and name:
            result['name'] = name
        return result

    def locator(self, element, root):
        result = self.basic(element)
        ancestors = []
        for _ in range(10):
            element = self.parent(element)
            if element is None or self.same(element, root):
                break
            item = self.basic(element)
            if item.get('name') or item.get('automation_id'):
                ancestors.append(item)
                if len(ancestors) >= 4:
                    break
        if ancestors:
            result['ancestors'] = ancestors
        return result

    def bounds(self, element):
        rect = RECT()
        element.call(43, (C.POINTER(RECT),), C.byref(rect))
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width <= 0 or height <= 0:
            return {'x': 0, 'y': 0, 'width': 0, 'height': 0}
        return {'x': rect.left, 'y': rect.top, 'width': width, 'height': height}

    def value(self, element):
        if self.secure(element) or element.property(30043) is not True:
            return None
        value = element.property(30045)
        return value if isinstance(value, str) else None

    def checked(self, element):
        if element.property(30041) is not True:
            return None
        value = element.property(30086)
        return value == 1 if type(value) is int and value in (0, 1) else None

    def actions(self, element):
        if self.secure(element):
            return []
        actions = []
        for property_id, names in ((30031, ['Click']), (30041, ['Check', 'Uncheck']),
                                   (30028, ['Expand', 'Collapse']), (30036, ['Select']), (30034, ['Scroll'])):
            if element.property(property_id) is True:
                actions.extend(names)
        if element.property(30043) is True and element.property(30046) is False:
            actions.append('SetValue')
        if element.property(30009) is True:
            actions.append('SetFocus')
        return actions

    def states(self, element):
        result = []
        if self.secure(element):
            result.append('protected')
        if element.property(30010) is not True:
            result.append('disabled')
        if element.property(30022) is not False:
            result.append('offscreen')
        if element.property(30008) is True:
            result.append('focused')
        if element.property(30041) is True:
            toggle = element.property(30086)
            if type(toggle) is int:
                result.append({0: 'unchecked', 1: 'checked', 2: 'indeterminate'}.get(toggle, 'unknown_check_state'))
        if element.property(30028) is True:
            expansion = element.property(30070)
            if expansion in (0, 1):
                result.append('expanded' if expansion == 1 else 'collapsed')
        if element.property(30036) is True and element.property(30079) is True:
            result.append('selected')
        return result

    def node(self, element, root, path='0', parent=''):
        bounds = self.bounds(element)
        valid = bounds['width'] > 0 and bounds['height'] > 0
        states = self.states(element)
        if not valid and 'offscreen' not in states:
            states.append('offscreen')
        native_handle = element.property(30020)
        # NativeWindowHandle is a 32-bit UIA property even on x64; HWNDs are sign-extended.
        hwnd = int(native_handle or 0) & 0xffffffff
        return {'name': element.property(30005) or '', 'role': ROLES.get(element.property(30003), 'unknown'),
                'automation_id': element.property(30011) or '', 'value': self.value(element),
                'checked': self.checked(element), 'enabled': element.property(30010) is True,
                'offscreen': element.property(30022) is not False or not valid, 'secure': self.secure(element),
                'states': states, 'available_actions': self.actions(element), 'locator': self.locator(element, root),
                'bounds': bounds, 'bounds_valid': valid, 'hwnd': hwnd, 'path': path, 'parent_path': parent}

    def observe_point(self, binding, x, y, window=None):
        binding.check()
        hit = window_from_point(x, y)
        if not binding.contains_window(hit):
            raise BridgeError('point_outside_target')
        window = int(window if window is not None else root_window(hit))
        if not binding.contains_window(window):
            raise BridgeError('outside_target')
        root, element = self.from_handle(window), self.from_point(x, y)
        if root is None or element is None or self.pid(element) != binding.pid or not self.within(element, root):
            raise BridgeError('point_identity_unproven')
        binding.check()
        return {'element': element, 'root': root, 'window': window, 'node': self.node(element, root)}


def observe_point(binding, x, y, window=None, *, uia=None):
    if uia is None:
        raise BridgeError('accessibility_context_required')
    return uia.observe_point(binding, x, y, window)


def read_tree(binding, uia):
    binding.check()
    root = uia.from_handle(binding.hwnd)
    if root is None or uia.pid(root) != binding.pid:
        raise BridgeError('root_unavailable')
    nodes, elements, complete = [], [], True
    queue = deque([(root, '0', '', 0)])
    while queue:
        if len(nodes) >= 2500:
            complete = False
            break
        element, path, parent, depth = queue.popleft()
        try:
            if uia.pid(element) != binding.pid:
                complete = False
                continue
            nodes.append(uia.node(element, root, path, parent))
            elements.append(element)
            for index, child in enumerate(uia.children(element)):
                if depth >= 18:
                    complete = False
                    break
                queue.append((child, path + '/' + str(index), path, depth + 1))
        except BridgeError:
            complete = False
    binding.check()
    return nodes, elements, complete


def _basic_match(actual, expected):
    if not isinstance(actual, dict) or not isinstance(expected, dict) or not (expected.get('name') or expected.get('automation_id')):
        return False
    return all(actual.get(key) == expected[key] for key in ('automation_id', 'name', 'role') if key in expected)


def match_locator(actual, expected):
    if not _basic_match(actual, expected):
        return False
    wanted, available = expected.get('ancestors', []), actual.get('ancestors', [])
    if not isinstance(wanted, list) or not isinstance(available, list):
        return False
    at = 0
    for item in wanted:
        while at < len(available) and not _basic_match(available[at], item):
            at += 1
        if at == len(available):
            return False
        at += 1
    return True


def unique(nodes, locator):
    matches = [i for i, node in enumerate(nodes) if match_locator(node.get('locator'), locator)]
    if not matches:
        raise BridgeError('target_not_found')
    if len(matches) != 1:
        raise BridgeError('ambiguous_target')
    return matches[0]


def inventory():
    _windows()
    rows = []
    callback_type = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    def visit(hwnd, _):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            pid = hwnd_pid(hwnd)
            if pid and pid != os.getpid():
                exe, _ = _process_identity(pid)
                rows.append(Binding({'pid': pid, 'hwnd': int(hwnd), 'exe_name': exe}).info())
        except (BridgeError, OSError):
            pass
        return len(rows) < 250
    callback = callback_type(visit)
    _sig(user32, 'EnumWindows', W.BOOL, callback_type, W.LPARAM)(callback, 0)
    return {'ok': True, 'status': 'observed', 'data': rows}


def inspect(request):
    binding = Binding(request)
    with UIA() as uia:
        nodes, _, complete = read_tree(binding, uia)
        data = binding.info()
        data.update(nodes=nodes, complete=complete)
        return {'ok': True, 'status': 'observed', 'data': data}


def probe(request):
    binding = Binding(request)
    if request.get('cursor') is True:
        x, y = cursor()
    else:
        x, y = request.get('x'), request.get('y')
        if any(type(v) is not int or not -0x80000000 <= v <= 0x7fffffff for v in (x, y)):
            raise BridgeError('invalid_point')
    with UIA() as uia:
        observed = uia.observe_point(binding, x, y)
        window = observed['window']
        return {'ok': True, 'status': 'observed', 'data': observed['node'],
                'window': {'hwnd': window, 'title': window_text(window), 'class_name': window_class(window)}}


def activate(request):
    binding = Binding(request)
    if not request.get('process_started_utc_ticks'):
        raise BridgeError('lifetime_required')
    binding.check()
    try:
        if user32.IsIconic(binding.hwnd):
            user32.ShowWindow(binding.hwnd, 9)
        returned = bool(user32.SetForegroundWindow(binding.hwnd))
        time.sleep(.1)
        binding.check()
        matched = root_window(foreground()) == binding.hwnd
        return {'ok': matched, 'status': 'completed' if matched else 'unavailable', 'input_dispatched': True,
                'delivery': 'delivered_unverified', 'foreground_verified': matched, 'activation_returned': returned}
    except BaseException as error:
        raise BridgeError('activation_outcome_unknown', dispatched=None) from error


def _mouse(x=0, y=0, flags=0):
    result = INPUT()
    result.type, result.mi.dx, result.mi.dy, result.mi.dwFlags = 0, x, y, flags
    return result


def point(request):
    binding = Binding(request)
    if not request.get('process_started_utc_ticks'):
        raise BridgeError('lifetime_required')
    action, ratio = request.get('action'), request.get('point')
    if action not in {'click', 'right-click', 'double-click'}:
        raise BridgeError('invalid_action')
    if not isinstance(ratio, dict):
        raise BridgeError('recorded_point_required')
    xr, yr = ratio.get('x_ratio'), ratio.get('y_ratio')
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in (xr, yr)):
        raise BridgeError('invalid_number')
    if not .02 <= xr <= .98 or not .02 <= yr <= .98:
        raise BridgeError('point_near_boundary')
    with UIA() as uia:
        nodes, elements, complete = read_tree(binding, uia)
        if not complete:
            raise BridgeError('incomplete_tree')
        index = unique(nodes, request.get('locator'))
        node, element = nodes[index], elements[index]
        if node.get('path') == '0' or node['role'] == 'window':
            raise BridgeError('window_root_point_rejected')
        rect = uia.bounds(element)
        if rect['width'] < 4 or rect['height'] < 4:
            raise BridgeError('target_bounds_unavailable')
        if uia.secure(element) or element.property(30010) is not True or element.property(30022) is not False:
            raise BridgeError('target_not_actionable')
        x, y = round(rect['x'] + xr * rect['width']), round(rect['y'] + yr * rect['height'])
        if not user32.IsWindowVisible(binding.hwnd) or user32.IsIconic(binding.hwnd):
            raise BridgeError('window_not_visible')
        fg = root_window(foreground())
        if fg != binding.hwnd and not owned(binding.hwnd, fg):
            raise BridgeError('target_not_foreground')
        hit = window_from_point(x, y)
        if root_window(hit) != binding.hwnd and not user32.IsChild(binding.hwnd, hit):
            raise BridgeError('point_occluded')
        if hwnd_pid(hit) != binding.pid:
            raise BridgeError('point_process_mismatch')
        live = uia.from_point(x, y)
        if live is None or uia.pid(live) != binding.pid or not uia.within(live, element):
            raise BridgeError('point_element_mismatch')
        if uia.secure(live):
            raise BridgeError('protected_point_target')
        if any(user32.GetAsyncKeyState(key) & 0x8000 for key in (0x10, 0x11, 0x12, 0x5b, 0x5c, 1, 2, 4, 5, 6)):
            raise BridgeError('physical_input_busy')
        binding.check()
        if uia.bounds(element) != rect or window_from_point(x, y) != hit:
            raise BridgeError('target_moved')
        sx, sy, sw, sh = [user32.GetSystemMetrics(index) for index in (76, 77, 78, 79)]
        if sw < 2 or sh < 2:
            raise BridgeError('desktop_bounds_unavailable')
        if not sx <= x < sx + sw or not sy <= y < sy + sh:
            raise BridgeError('point_outside_desktop')
        down, up = (0x0008, 0x0010) if action == 'right-click' else (0x0002, 0x0004)
        values = [_mouse(round((x - sx) * 65535 / (sw - 1)), round((y - sy) * 65535 / (sh - 1)), 0x8000 | 0x4000 | 0x0001),
                  _mouse(flags=down), _mouse(flags=up)]
        if action == 'double-click':
            values.extend([_mouse(flags=down), _mouse(flags=up)])
        inputs = (INPUT * len(values))(*values)
        # One dispatch boundary. No retry after SendInput, even if its return is partial.
        try:
            sent = user32.SendInput(len(inputs), inputs, C.sizeof(INPUT))
        except BaseException as error:
            raise BridgeError('point_delivery_unknown', dispatched=None) from error
        if sent != len(inputs):
            return {'ok': False, 'status': 'uncertain', 'input_dispatched': None, 'delivery': 'possibly_dispatched',
                    'diagnostic': 'partial_input', 'retry': 'unsafe'}
        return {'ok': True, 'status': 'executed_unverified', 'input_dispatched': True,
                'delivery': 'delivered_unverified', 'retry': 'unsafe', 'business_commit': 'not_verified'}
