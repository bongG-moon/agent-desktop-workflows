"""Describe required input paths without inspecting or changing the desktop."""
from collections.abc import Mapping
from copy import deepcopy
import ctypes as C
from ctypes import wintypes as W
import os
import key_chords


_POINTER_ACTIONS = frozenset({'context-menu', 'result-menu', 'row-action', 'row-launch', 'row-open', 'visual-click', 'double-click-result', 'conditional-open'})
_ACCESSIBILITY_ACTIONS = frozenset({'set-value', 'invoke', 'select', 'fill', 'open', 'choose-item'})
VALUE_OBSERVATIONS = frozenset({'read-value', 'verify-value'})
_OBSERVATION_ACTIONS = frozenset({'wait', 'verify-file'}) | VALUE_OBSERVATIONS

# Windows SDK 10.0.26100.0 UIAutomationClient.idl and UIAutomationClient.h:
# IUIAutomation2 extends IUIAutomation; the getter/setter occupy slots 58/59.
# https://learn.microsoft.com/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomation2-put_autosetfocus
_UIA2_IID = '34723aff-0c9d-49d0-9896-7ab52df8cd8a'
_UIA8_CLSID = 'e22ad333-b25f-460c-83d0-0581107395c9'
_E_NOINTERFACE = 0x80004002


def profile(steps, numbers=None):
    """Classify planned mechanisms; this is not an input-delivery receipt.

    Accessibility-only steps may still cause the application itself to open a
    window. No classification claims an independent desktop or focus isolation.
    """
    steps = list(steps)
    numbers = list(range(1, len(steps) + 1)) if numbers is None else list(numbers)
    if len(numbers) != len(steps):
        raise ValueError('step_number_count_mismatch')
    if any(type(number) is not int or number < 1 for number in numbers):
        raise ValueError('invalid_step_number')

    entries = []
    for number, step in zip(numbers, steps):
        action = step.get('action') if isinstance(step, Mapping) else None
        action = action if isinstance(action, str) else 'unknown'
        mode, foreground, eligible = 'unknown', False, False
        if action in _POINTER_ACTIONS:
            mode, foreground = 'physical_pointer', True
        elif action in ('submit', 'press-key'):
            mode, foreground = 'physical_keyboard', True
        elif action in _ACCESSIBILITY_ACTIONS:
            mode, eligible = 'accessibility', True
        elif action in _OBSERVATION_ACTIONS:
            mode, eligible = 'observation', True
        entries.append(dict(number=number, action=action, input_mode=mode,
                            requires_foreground=foreground, background_eligible=eligible))
        if action == 'press-key' and key_chords.virtual_keys(step.get('keys')):
            entries[-1]['keys'] = step['keys']

    return dict(independent_desktop=False,
                physical_input_required=any(row['input_mode'] in ('physical_pointer', 'physical_keyboard')
                                            for row in entries),
                foreground_required=any(row['requires_foreground'] for row in entries),
                background_eligible=all(row['background_eligible'] for row in entries),
                minimized_window_requires_restore=True, steps=entries)


def blocked(requirements):
    """Return a no-input policy rejection, or None for an eligible whole plan."""
    blocked_steps = [row['number'] for row in requirements['steps']
                     if row.get('background_eligible') is not True]
    if requirements.get('background_eligible') is True and not blocked_steps:
        return None
    return dict(status='blocked', diagnostic='background_requires_desktop_interaction',
                input_dispatched=False, physical_input=False, steps=[],
                blocked_steps=blocked_steps, input_policy='background_only',
                input_requirements=deepcopy(requirements))


def _interactive_desktop(win=None):
    """Read desktop identity only. Never switch, unlock or activate a desktop."""
    if win is None:
        if os.name != 'nt':
            # The real window binding rejects non-Windows execution. Keep
            # platform-independent recipe validation usable in tests/tools.
            return True
        import win_bridge as win
    desktop = None
    try:
        user = win.user32
        open_input = win._sig(user, 'OpenInputDesktop', W.HANDLE, W.DWORD, W.BOOL, W.DWORD)
        close = win._sig(user, 'CloseDesktop', W.BOOL, W.HANDLE)
        thread_desktop = win._sig(user, 'GetThreadDesktop', W.HANDLE, W.DWORD)
        current_thread = win._sig(win.kernel32, 'GetCurrentThreadId', W.DWORD)
        info = win._sig(user, 'GetUserObjectInformationW', W.BOOL, W.HANDLE, C.c_int,
                        C.c_void_p, W.DWORD, C.POINTER(W.DWORD))
        desktop = open_input(0, False, 1)  # DESKTOP_READOBJECTS, no switching access.
        if not desktop:
            return False
        thread = thread_desktop(current_thread())
        if not thread:
            return False
        names = []
        for handle in (desktop, thread):
            name, size = C.create_unicode_buffer(256), W.DWORD()
            if not info(handle, 2, name, C.sizeof(name), C.byref(size)) or not name.value:
                return False
            names.append(name.value.casefold())  # UOI_NAME; Windows names ignore case.
        return names[0] == names[1]
    except (OSError, AttributeError, ValueError):
        return False
    finally:
        if desktop:
            close(desktop)  # The thread desktop is borrowed and must stay open.


def interactive_desktop_blocked(requirements):
    """Fail before a mixed workflow starts if its physical steps cannot run."""
    physical = [row['number'] for row in requirements['steps']
                if row['input_mode'] in ('physical_pointer', 'physical_keyboard')]
    if not physical or _interactive_desktop():
        return None
    return dict(status='blocked', diagnostic='no_interactive_desktop',
                host_action='unlock_desktop_before_retry', input_dispatched=False,
                physical_input=False, steps=[], blocked_steps=physical,
                input_requirements=deepcopy(requirements))


def window_ready(binding):
    """Check the current binding and refuse restoration; never activate a window."""
    import win_bridge
    binding.check()
    if win_bridge.user32.IsIconic(binding.hwnd):
        raise win_bridge.BridgeError('background_window_minimized')


def _query_interface(client, iid, pointer):
    """Preserve QueryInterface's HRESULT so only E_NOINTERFACE permits replacement."""
    client.uia._thread()
    if not client.ptr.value:
        raise ValueError('closed_uia_client')
    table = C.cast(client.ptr, C.POINTER(C.POINTER(C.c_void_p))).contents
    query = C.WINFUNCTYPE(C.c_int32, C.c_void_p, C.POINTER(type(iid)), C.POINTER(C.c_void_p))(table[0])
    return query(client.ptr, C.byref(iid), C.byref(pointer))


def configure_uia(uia):
    """Disable UIA's implicit focus before obtaining any action targets.

    Legacy CUIAutomation can lack IUIAutomation2. In that case a fresh
    CUIAutomation8 client and its walker replace the unopened legacy client
    only after AutoSetFocus=False has been read back. No desktop input is sent.
    An application can still change focus through its own action handling.
    """
    import win_bridge as win
    uia._desktop_select_no_autofocus = False
    interface = walker = None
    try:
        try:
            uia._thread()
            iid, pointer = win.GUID.parse(_UIA2_IID), C.c_void_p()
            result = _query_interface(uia.client, iid, pointer)
            replace = result & 0xffffffff == _E_NOINTERFACE
            if replace:
                if pointer.value or any(obj is not uia.client and obj is not uia.walker and obj.ptr.value
                                        for obj in uia._objects):
                    raise ValueError('uia_targets_already_created')
                clsid = win.GUID.parse(_UIA8_CLSID)
                result = win.ole32.CoCreateInstance(C.byref(clsid), None, 1, C.byref(iid), C.byref(pointer))
            if result != 0 or not pointer.value:
                raise ValueError('uia2_unavailable')
            interface = win._Com(pointer, uia)
            interface.call(59, (W.BOOL,), False)
            actual = W.BOOL(-1)
            interface.call(58, (C.POINTER(W.BOOL),), C.byref(actual))
            if actual.value != 0:
                raise ValueError('uia_autofocus_not_disabled')
            if replace:
                walker_pointer = C.c_void_p()
                interface.call(14, (C.POINTER(C.c_void_p),), C.byref(walker_pointer))
                if not walker_pointer.value:
                    raise ValueError('uia_walker_unavailable')
                walker = win._Com(walker_pointer, uia)
                uia.walker.close()
                uia.client.close()
                uia.client, uia.walker = interface, walker
                interface = walker = None  # Ownership now belongs to this UIA context.
        finally:
            if walker is not None:
                walker.close()
            if interface is not None:
                interface.close()
    except Exception as error:
        raise win.BridgeError('background_autofocus_unavailable') from error
    uia._desktop_select_no_autofocus = True


def prefer_background(uia):
    """Allow default execution to take the foreground lease when UIA cannot suppress focus."""
    import win_bridge as win
    try:
        configure_uia(uia)
    except win.BridgeError as error:
        if error.code != 'background_autofocus_unavailable':
            raise
        return False
    return True
