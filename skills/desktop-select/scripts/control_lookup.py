"""Find known controls with native UIA conditions, without Python tree snapshots.

Only identities are queried. Values and labels are read after the candidate's
entire ancestor chain is proven to belong to the selected, unprotected window.
The Windows bridge stays pinned; these adapters use its COM lifetime handling.
"""
import ctypes as C

import win_bridge as win


_DESCENDANTS = 4
_MAX_ANCESTORS = 32
_MAX_CANDIDATES = 4096


def _need(condition, code):
    if not condition:
        raise win.BridgeError(code)


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and '\0' not in value and len(value) <= 1000


def _roles(roles):
    _need(isinstance(roles, (list, tuple, set, frozenset)) and
          all(isinstance(role, str) and role in win.ROLES.values() for role in roles),
          'invalid_target_role')
    return [number for number, role in win.ROLES.items() if role in roles]


def _property_condition(uia, property_id, value):
    """IUIAutomation::CreatePropertyCondition takes a VARIANT by value."""
    variant, pointer, string = win.VARIANT(), C.c_void_p(), None
    try:
        if isinstance(value, str):
            string = win._sig(win.oleaut32, 'SysAllocString', C.c_void_p, C.c_wchar_p)(value)
            _need(bool(string), 'target_condition_allocation_failed')
            variant.vt, variant.pointer = 8, string
        else:
            variant.vt, variant.i4 = 3, value
        # UIAutomationClient.idl, IUIAutomation vtable slot 23.
        uia.client.call(23, (C.c_int32, win.VARIANT, C.POINTER(C.c_void_p)),
                        property_id, variant, C.byref(pointer))
        _need(bool(pointer.value), 'target_condition_unavailable')
        return win._Com(pointer, uia)
    finally:
        if string:
            win._sig(win.oleaut32, 'SysFreeString', None, C.c_void_p)(string)


def _join(uia, left, right, *, either=False):
    pointer = C.c_void_p()
    # CreateAndCondition / CreateOrCondition, following IUnknown's three slots.
    uia.client.call(28 if either else 25,
                    (C.c_void_p, C.c_void_p, C.POINTER(C.c_void_p)),
                    left.ptr, right.ptr, C.byref(pointer))
    _need(bool(pointer.value), 'target_condition_unavailable')
    return win._Com(pointer, uia)


def _raw_parent(uia, element):
    # The bridge's ControlViewWalker omits non-control wrappers. Security must
    # include those wrappers too. Keep this walker on the same UIA apartment;
    # bridge.UIA.close() releases every registered _Com object at session end.
    walker = getattr(uia, '_desktop_select_raw_walker', None)
    if walker is None:
        pointer = C.c_void_p()
        uia.client.call(16, (C.POINTER(C.c_void_p),), C.byref(pointer))
        _need(bool(pointer.value), 'target_ancestry_unavailable')
        walker = win._Com(pointer, uia)
        uia._desktop_select_raw_walker = walker
    pointer = C.c_void_p()
    walker.call(3, (C.c_void_p, C.POINTER(C.c_void_p)), element.ptr, C.byref(pointer))
    return win.Element(pointer, uia) if pointer.value else None


def ancestry(uia, element, root, *, pid=None):
    """Return [element, ..., root] only for an entirely owned, unprotected chain.

    Use this before reading any candidate names, values or identifiers. A PID
    alone is insufficient: another business window in that PID is out of scope.
    """
    _need(root is not None and element is not None, 'target_outside_window')
    expected = uia.pid(root) if pid is None else pid
    _need(type(expected) is int and expected > 0 and uia.pid(root) == expected,
          'target_owner_mismatch')
    _need(not uia.secure(root), 'target_protected')
    chain, current = [], element
    for _ in range(_MAX_ANCESTORS):
        _need(current is not None, 'target_outside_window')
        _need(uia.pid(current) == expected, 'target_owner_mismatch')
        _need(not uia.secure(current), 'target_protected')
        chain.append(current)
        if uia.same(current, root):
            return chain
        current = _raw_parent(uia, current)
    raise win.BridgeError('target_ancestry_incomplete')


def find_all(uia, root, *, automation_id=None, roles=(), limit=128):
    """Native descendant search, retaining all usable matches to detect duplicates.

    The provider still searches its subtree internally. This removes Python's
    per-node walk and property reads; it does not promise an indexed provider.
    Protected and foreign candidates never supply labels or values to discovery.
    """
    _need(root is not None, 'root_unavailable')
    _need(type(limit) is int and 1 <= limit <= _MAX_CANDIDATES, 'invalid_target_limit')
    _need(automation_id is None or _text(automation_id), 'invalid_target_identifier')
    role_numbers = _roles(roles)
    _need(automation_id is not None or bool(role_numbers), 'target_query_required')
    pid = uia.pid(root)
    _need(type(pid) is int and pid > 0, 'target_owner_mismatch')
    _need(not uia.secure(root), 'target_protected')
    resources, array, result = [], None, []
    try:
        condition = None
        for number in role_numbers:
            part = _property_condition(uia, 30003, number)
            resources.append(part)
            if condition is not None:
                part = _join(uia, condition, part, either=True)
                resources.append(part)
            condition = part
        if automation_id is not None:
            part = _property_condition(uia, 30011, automation_id)
            resources.append(part)
            if condition is not None:
                part = _join(uia, condition, part)
                resources.append(part)
            condition = part
        pointer = C.c_void_p()
        # IUIAutomationElement::FindAll: slot 6, TreeScope_Descendants.
        root.call(6, (C.c_int32, C.c_void_p, C.POINTER(C.c_void_p)),
                  _DESCENDANTS, condition.ptr, C.byref(pointer))
        _need(bool(pointer.value), 'target_search_unavailable')
        array = win._Com(pointer, uia)
        count = C.c_int32()
        array.call(3, (C.POINTER(C.c_int32),), C.byref(count))
        _need(0 <= count.value <= limit, 'target_candidates_incomplete')
        for index in range(count.value):
            pointer = C.c_void_p()
            array.call(4, (C.c_int32, C.POINTER(C.c_void_p)), index, C.byref(pointer))
            _need(bool(pointer.value), 'target_search_incomplete')
            element = win.Element(pointer, uia)
            try:
                ancestry(uia, element, root, pid=pid)
            except win.BridgeError as error:
                element.close()
                if error.code not in {'target_protected', 'target_owner_mismatch', 'target_outside_window'}:
                    raise
                continue
            result.append(element)
        return result
    except BaseException:
        for element in result:
            element.close()
        raise
    finally:
        if array is not None:
            array.close()
        for resource in reversed(resources):
            resource.close()


def _matches(element, identity):
    # The caller proves the complete ancestor chain before entering this helper.
    properties = {'automation_id': 30011, 'name': 30005}
    return (win.ROLES.get(element.property(30003)) == identity['role'] and
            all(element.property(properties[key]) == value for key, value in identity.items() if key != 'role'))


def _selector(selector):
    _need(isinstance(selector, dict) and not set(selector) - {'automation_id', 'role', 'ancestors'}
          and _text(selector.get('automation_id')) and selector.get('role') in win.ROLES.values(),
          'invalid_target_selector')
    parents = selector.get('ancestors', [])
    _need(isinstance(parents, list) and len(parents) <= 16, 'invalid_target_ancestors')
    for parent in parents:
        _need(isinstance(parent, dict) and not set(parent) - {'automation_id', 'role', 'name'}
              and parent.get('role') in win.ROLES.values()
              and bool(set(parent) & {'automation_id', 'name'})
              and all(_text(value) for key, value in parent.items() if key != 'role'),
              'invalid_target_ancestors')
    return parents


def resolve(binding, uia, selector, root=None):
    """Resolve an ID+role and optional nearest-first ancestor identities uniquely."""
    parents = _selector(selector)
    binding.check()
    window = uia.from_handle(binding.hwnd)
    _need(window is not None and uia.pid(window) == binding.pid, 'root_unavailable')
    root = window if root is None else root
    ancestry(uia, root, window, pid=binding.pid)
    candidates = find_all(uia, root, automation_id=selector['automation_id'], roles=(selector['role'],))
    chosen, returned = [], None
    try:
        # FindAll excludes its starting element; a demonstrated group may be it.
        identity = {key: selector[key] for key in ('automation_id', 'role')}
        if _matches(root, identity):
            candidates.insert(0, root)
        for element in candidates:
            chain = ancestry(uia, element, window, pid=binding.pid)
            if not _matches(element, identity):
                raise win.BridgeError('target_changed')
            remaining = iter(chain[1:])
            if all(any(_matches(ancestor, expected) for ancestor in remaining) for expected in parents):
                chosen.append(element)
        _need(len(chosen) == 1, 'target_ambiguous' if chosen else 'target_missing')
        binding.check()
        returned = chosen[0]
        return returned
    finally:
        for element in candidates:
            if element is not root and element is not returned:
                element.close()
