"""Describe required input paths without inspecting or changing the desktop."""
from collections.abc import Mapping
from copy import deepcopy
import ctypes as C
from ctypes import wintypes as W


_POINTER_ACTIONS = frozenset({'context-menu', 'result-menu', 'row-action'})
_ACCESSIBILITY_ACTIONS = frozenset({'set-value', 'invoke', 'select', 'fill', 'open'})
_OBSERVATION_ACTIONS = frozenset({'wait', 'verify-file'})

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
        elif action == 'submit':
            mode, foreground = 'physical_keyboard', True
        elif action in _ACCESSIBILITY_ACTIONS:
            selector = step.get('selector')
            foreground = isinstance(selector, Mapping) and 'anchor' in selector
            mode, eligible = 'accessibility', not foreground
        elif action in _OBSERVATION_ACTIONS:
            mode, eligible = 'observation', True
        entries.append(dict(number=number, action=action, input_mode=mode,
                            requires_foreground=foreground, background_eligible=eligible))

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
