"""Dispatch one scoped UI Automation command, then read current provider state."""
import ctypes as C
from collections import deque
import re
import time

import win_bridge as win
import control_lookup
import provider_values
import provider_visibility


# UIAutomationClient.idl: each of these methods is slot 3 after IUnknown.
PATTERNS = {
    'Invoke': (10000, 'fb377fbe-8ea6-46d5-9c73-6499642d3059'),
    'SetValue': (10002, 'a94cd8b1-0844-4cd6-9d2d-640537ab39e9'),
    'Expand': (10005, '619be086-1f4e-4ee4-bafa-210128738730'),
    'Select': (10010, 'a8efa66a-0fda-421a-9194-38021f3578ea'),
}
READBACK_SECONDS = .6
OPEN_SECONDS = 1.2
VALUE_SETTLE_SECONDS = .35


def _call(element, method, value, authorize, dispatching):
    """Pattern acquisition is read-only. An error from its method has unknown delivery."""
    num, guid = PATTERNS[method]
    pointer = C.c_void_p()
    iid = win.GUID.parse(guid)
    element.call(14, (C.c_int32, C.POINTER(win.GUID), C.POINTER(C.c_void_p)),
                 num, C.byref(iid), C.byref(pointer))
    if not pointer.value:
        raise win.BridgeError('pattern_unavailable')
    pattern = win._Com(pointer, element.uia)
    bstr = None
    try:
        if method == 'SetValue':
            # IUIAutomationValuePattern::SetValue takes a length-prefixed BSTR.
            allocate = win._sig(win.oleaut32, 'SysAllocString', C.c_void_p, C.c_wchar_p)
            bstr = allocate(value)
            if not bstr:
                raise win.BridgeError('value_allocation_failed')
        authorize()
        dispatching(method)
        if method == 'SetValue':
            pattern.call(3, (C.c_void_p,), bstr)
        else:
            pattern.call(3, ())
    finally:
        if bstr:
            win._sig(win.oleaut32, 'SysFreeString', None, C.c_void_p)(bstr)
        pattern.close()


def _require(ok, reason):
    if not ok:
        raise win.BridgeError(reason)


def _visible(uia, item, binding=None, root=None):
    if binding is not None:
        return provider_visibility.visible(binding, uia, item, root)
    bounds = uia.bounds(item)
    return item.property(30022) is False and bounds['width'] > 0 and bounds['height'] > 0


def _combo_visible(binding, uia, combo, root):
    if combo.property(30022) in (False, None):
        return _visible(uia, combo, binding, root)
    box, window = uia.bounds(combo), uia.bounds(root)
    for axis, size in (('x', 'width'), ('y', 'height')):
        if (box[size] <= 0 or window[size] <= 0 or
                max(box[axis], window[axis]) >= min(box[axis] + box[size], window[axis] + window[size])):
            return False
    # FX providers sometimes mark the combo offscreen while exposing its actual
    # on-screen edit/button. A leftover combo rectangle alone proves nothing.
    pending = deque((child, 1) for child in uia.children(combo))
    visited = 0
    while pending:
        child, depth = pending.popleft()
        visited += 1
        _require(visited <= 96 and depth <= 6 and uia.pid(child) == binding.pid, 'combo_visibility_unavailable')
        if uia.secure(child):
            continue
        role = child.property(30003)
        if role in (50025, 50026, 50033):
            pending.extend((c, depth + 1) for c in uia.children(child))
        elif (role in (50004, 50000) and child.property(30010) is True and _visible(uia, child, binding, root)
              and uia.within(child, combo)):
            child_box = uia.bounds(child)
            if all(box[axis] <= child_box[axis] and child_box[axis] + child_box[size] <= box[axis] + box[size]
                   and window[axis] <= child_box[axis] and child_box[axis] + child_box[size] <= window[axis] + window[size]
                   for axis, size in (('x', 'width'), ('y', 'height'))):
                return True
    return False


def combo_readback(binding, uia, element):
    """Read the same provider patterns, validating each selected row before reading its value."""
    unknown = {'value': None, 'value_evidence': {'status': 'unavailable', 'source': 'none'}}
    binding.check()
    root = uia.from_handle(binding.hwnd)
    if (root is None or not uia.within(element, root) or uia.pid(element) != binding.pid
            or uia.secure(element) or element.property(30003) != 50003):
        return unknown

    class ScopedReader:
        def __getattr__(self, key):
            return getattr(uia, key)

        def _selected_option_observation(self, item):
            # Some providers attach another combo's selected peer under this
            # combo. Explicit SelectionContainer and ancestry must agree.
            owner = uia._combo_owner({'element': item, 'root': root, 'window': binding.hwnd})
            if owner is None or not uia.same(owner['element'], element):
                return unknown
            return uia._selected_option_observation(item)

    try:
        result = provider_values.value_observation(ScopedReader(), element)
    except win.BridgeError as error:
        if error.code not in {'accessibility_unavailable', 'element_disappeared'}:
            raise
        result = unknown
    binding.check()
    return result


def textfield_scope(binding, uia, element):
    """Prove an ordinary input belongs to this window before reading its text.

    A combo's editor must still be handled through its owning combo. Raw-tree
    ancestry includes wrappers that the accessibility control view may omit.
    """
    binding.check()
    root = uia.from_handle(binding.hwnd)
    chain = control_lookup.ancestry(uia, element, root, pid=binding.pid)
    _require(element.property(30003) == 50004, 'textfield_required')
    _require(not any(parent.property(30003) == 50003 for parent in chain[1:]),
             'textfield_requires_combo')
    return root


def text_readback(binding, uia, element):
    """An available ValuePattern can prove an intentional empty value as well."""
    unknown = {'value': None, 'value_evidence': {'status': 'unavailable', 'source': 'none'}}
    textfield_scope(binding, uia, element)
    if element.property(30043) is not True:
        return unknown
    value = element.property(30045)
    binding.check()
    if not isinstance(value, str):
        return unknown
    return {'value': value, 'value_evidence': {'status': 'observed', 'source': 'value_pattern'}}


def _option_rows(binding, uia, combo):
    """Rows can be in the combo's tree or in a proven SelectionContainer popup."""
    root = uia.from_handle(binding.hwnd)
    _require(root is not None and uia.within(combo, root) and uia.pid(combo) == binding.pid
             and not uia.secure(combo) and combo.property(30003) == 50003, 'combo_required')
    queue = deque([(combo, root, binding.hwnd, 0)])
    for hwnd in binding.scoped_windows():
        if hwnd != binding.hwnd:
            popup = uia.from_handle(hwnd)
            _require(popup is not None, 'combo_options_incomplete')
            queue.append((popup, popup, hwnd, 0))
    visited = 0
    while queue:
        item, scope, hwnd, depth = queue.popleft()
        visited += 1
        _require(visited <= 1024 and depth <= 16 and uia.pid(item) == binding.pid,
                 'combo_options_incomplete')
        if uia.secure(item):
            continue
        role = item.property(30003)
        if role == 50007:
            owner = uia._combo_owner({'element': item, 'root': scope, 'window': hwnd,
                                      'scope_root': root, 'scope_window': binding.hwnd})
            if owner and uia.same(combo, owner['element']):
                observation = uia._selected_option_observation(item)
                value = observation.get('value')
                if isinstance(value, str) and value:
                    name = item.property(30005)
                    yield item, value, name if isinstance(name, str) else ''
            continue
        if role != 50003 or uia.same(item, combo):
            queue.extend((child, scope, hwnd, depth + 1) for child in uia.children(item))


def option_inventory(binding, uia, element):
    """Read options exposed now. A collapsed provider may return an empty inventory."""
    binding.check()
    rows = dict.fromkeys((value, name) for _, value, name in _option_rows(binding, uia, element))
    binding.check()
    return {'options': [{'value': value, 'name': name} for value, name in list(rows)[:128]],
            'truncated': len(rows) > 128}


def _options(binding, uia, combo, wanted):
    code, matches = None, []
    for item, value, name in _option_rows(binding, uia, combo):
        if wanted not in (value, name):
            continue
        _require(code is None or value == code, 'combo_option_ambiguous')
        code = value
        if not any(uia.same(item, seen) for seen in matches):
            matches.append(item)
    return code, matches


def _visible_choice(uia, matches, binding=None):
    visible = [item for item in matches if _visible(uia, item, binding)]
    _require(len(visible) < 2, 'combo_option_ambiguous')
    if visible:
        _require(visible[0].property(30010) is True, 'combo_option_disabled')
        if 'Select' in uia.actions(visible[0]):
            return visible[0]
    return None


def _receipt(status, effect, diagnostic, method, dispatched, expanded):
    return {'status': status, 'effect_verification': effect, 'diagnostic': diagnostic,
            'method': method, 'input_dispatched': dispatched, 'expanded': expanded}


def submit_ready(binding, uia, element, node):
    """Read-only capability check for submitting one ordinary editable field."""
    binding.check()
    root = uia.from_handle(binding.hwnd)
    control_lookup.ancestry(uia, element, root, pid=binding.pid)
    role = win.ROLES.get(element.property(30003))
    _require(role in ('textfield', 'combobox') and role == node.get('role'), 'submit_field_required')
    if role == 'textfield':
        textfield_scope(binding, uia, element)
    _require(not uia.secure(element) and element.property(30010) is True, 'target_not_actionable')
    _require(element.property(30009) is True, 'submit_target_not_focusable')
    _require(element.property(30043) is True and element.property(30046) is False,
             'submit_writable_value_required')
    visible = _combo_visible(binding, uia, element, root) if role == 'combobox' else _visible(uia, element, binding, root)
    _require(visible, 'target_offscreen')
    return root


def submit(binding, uia, element, node, *, still_valid):
    """Focus the revalidated field and send exactly one Enter pair, without retries."""
    from row_action import foreground, _input_tick
    pending, focused, focus_pending = False, False, False
    sent_count = None

    def result(status, diagnostic, dispatched):
        return dict(status=status, diagnostic=diagnostic, input_dispatched=dispatched,
                    input_mode='physical_keyboard', physical_input=True, method='SendInput.Enter',
                    keyboard_events_sent=sent_count, focus_changed=None if focus_pending else focused,
                    effect_verification='unverified', business_commit='not_verified', retry='unsafe')

    def current():
        still_valid()
        return submit_ready(binding, uia, element, node)

    def idle():
        win._combo_surface(binding)
        _require(win.root_window(win.foreground()) == binding.hwnd, 'target_not_foreground')
        _require(not any(win.user32.GetAsyncKeyState(key) & 0x8000 for key in range(1, 255)),
                 'physical_input_busy')

    try:
        current()
        foreground(binding)
        current()
        idle()
        tick = _input_tick()
        focus_pending = True
        element.call(3, ())  # IUIAutomationElement::SetFocus.
        focus_pending, focused = False, True
        root = current()
        idle()
        _require(_input_tick() == tick, 'physical_input_changed')
        focus = uia.focused()
        _require(focus is not None, 'submit_focus_unavailable')
        chain = control_lookup.ancestry(uia, focus, root, pid=binding.pid)
        _require(not uia.secure(focus) and focus.property(30010) is True, 'submit_focus_protected')
        if not uia.same(focus, element):
            _require(node['role'] == 'combobox' and focus.property(30003) == 50004,
                     'submit_focus_mismatch')
            owner = next((ancestor for ancestor in chain[1:] if ancestor.property(30003) == 50003), None)
            _require(owner is not None and uia.same(owner, element) and uia.within(focus, element),
                     'submit_focus_mismatch')
            _require(focus.property(30043) is True and focus.property(30046) is False
                     and _visible(uia, focus, binding, root), 'submit_focus_not_editable')
        # Recheck the selector, foreground and OS focus immediately before input.
        current()
        idle()
        _require(_input_tick() == tick, 'physical_input_changed')
        latest = uia.focused()
        _require(latest is not None and uia.same(focus, latest), 'submit_focus_changed')
        control_lookup.ancestry(uia, latest, root, pid=binding.pid)
        _require(not uia.secure(latest) and latest.property(30010) is True
                 and latest.property(30043) is True and latest.property(30046) is False,
                 'submit_focus_not_editable')
        idle()
        _require(_input_tick() == tick, 'physical_input_changed')
        events = (win.INPUT * 2)()
        events[0].type, events[0].ki = 1, win.KEYBDINPUT(0x0D, 0, 0, 0, 0)
        events[1].type, events[1].ki = 1, win.KEYBDINPUT(0x0D, 0, 0x0002, 0, 0)
        pending = True
        sent_count = win.user32.SendInput(2, events, C.sizeof(win.INPUT))
        if sent_count != 2:
            return result('uncertain', 'submit_partial_input', None)
        pending = False
        return result('delivered_unverified', 'submit_enter_sent', True)
    except (Exception, KeyboardInterrupt) as error:
        return result('uncertain' if pending else 'blocked',
                      'submit_delivery_unknown' if pending else getattr(error, 'code', 'submit_unavailable'),
                      None if pending else False)


def execute(binding, uia, element, node, action, value=None, *, still_valid):
    """The caller re-resolves this exact business field/window in still_valid()."""
    if action == 'submit':
        if value is not None:
            return _receipt('blocked', 'unavailable', 'submit_value_not_allowed', None, False, False)
        return submit(binding, uia, element, node, still_valid=still_valid)
    method, pending, sent, expanded = None, False, False, False
    selected_value = value
    verifying = False
    def dispatching(next_method):
        nonlocal method, pending
        method, pending = next_method, True

    def check_target(full=True):
        if full:
            still_valid()
        binding.check()
        root = uia.from_handle(binding.hwnd)
        _require(root is not None and uia.within(element, root) and uia.pid(element) == binding.pid,
                 'target_changed')
        _require(not uia.secure(element) and element.property(30010) is True, 'target_not_actionable')
        if full and element.property(30003) == 50003:
            _require(_combo_visible(binding, uia, element, root), 'target_offscreen')

    def send(target, operation, guard, argument=None):
        nonlocal pending, sent, expanded
        _call(target, operation, argument, guard, dispatching)
        sent, pending = True, False
        expanded |= operation == 'Expand'

    def receipt(status, diagnostic, effect='unavailable'):
        return _receipt(status, effect, diagnostic, method, None if pending else sent,
                        None if pending and method == 'Expand' else expanded)

    def read_value():
        result = combo_readback(binding, uia, element)
        evidence = result.get('value_evidence', {})
        if (evidence.get('status') == 'observed' and
                evidence.get('source') in {'parent', 'editor', 'selection', 'corroborated'} and result.get('value')):
            return result['value'] == selected_value
        return None

    try:
        check_target()
        role = win.ROLES.get(element.property(30003))
        _require(role and role == node.get('role'), 'target_role_changed')

        if action == 'fill':
            _require(role == 'textfield', 'textfield_required')
            _require(isinstance(value, str) and (value == '' or value.strip())
                     and not any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
                     and len(value.encode('utf-8')) <= 10000, 'invalid_text_value')
            def text_write_guard():
                check_target()
                root = textfield_scope(binding, uia, element)
                _require(_visible(uia, element, binding, root), 'target_offscreen')
                _require(element.property(30043) is True and element.property(30046) is False
                         and 'SetValue' in uia.actions(element), 'text_value_not_writable')
            send(element, 'SetValue', text_write_guard, value)
            verifying = True
            time.sleep(VALUE_SETTLE_SECONDS)
            check_target()
            result = text_readback(binding, uia, element)
            if result.get('value_evidence', {}).get('status') != 'observed':
                return receipt('delivered_unverified', 'current_state_unavailable')
            if result['value'] != value:
                return receipt('mismatch', 'current_state_mismatch', 'mismatch')
            return receipt('verified', 'current_state_matched', 'matched')
        elif action in ('select', 'set-value'):
            _require(role == 'combobox', 'combo_required')
            _require(isinstance(value, str) and value.strip() and '\0' not in value
                     and len(value.encode('utf-8')) <= 10000, 'invalid_combo_value')
            if 'SetValue' in uia.actions(element):
                # Writable custom combos need neither visible option peers nor
                # a truthful ExpandCollapse state. The caller supplies a literal
                # value; a single write is followed by independent readback.
                def direct_write_guard():
                    check_target()
                    _require('SetValue' in uia.actions(element), 'combo_value_not_writable')
                send(element, 'SetValue', direct_write_guard, value)
                verifying = True
                time.sleep(VALUE_SETTLE_SECONDS)
                check_target()
                matched = read_value()
                if matched is True:
                    return receipt('verified', 'current_state_matched', 'matched')
                if matched is False:
                    return receipt('mismatch', 'current_state_mismatch', 'mismatch')
                return receipt('delivered_unverified', 'current_state_unavailable')
            try:
                code, matches = _options(binding, uia, element, value)
            except win.BridgeError as error:
                if (error.code not in {'accessibility_unavailable', 'element_disappeared'}
                        or 'Expand' not in uia.actions(element) or element.property(30070) != 0):
                    raise
                code, matches = None, []
            known_code = code
            selected_value = code or value
            if read_value() is True:
                check_target()
                if read_value() is True:
                    return receipt('verified', 'value_already_selected', 'matched')
            choice = _visible_choice(uia, matches, binding)
            if choice is None and 'Expand' in uia.actions(element) and element.property(30070) == 0:
                def expansion_guard():
                    check_target()
                    _require('Expand' in uia.actions(element) and element.property(30070) == 0,
                             'expansion_state_changed')
                send(element, 'Expand', expansion_guard)
                deadline = time.monotonic() + OPEN_SECONDS
                while True:
                    binding.check()
                    code, matches = _options(binding, uia, element, value)
                    _require(code is None or known_code in (None, code), 'combo_option_changed')
                    if code is not None:
                        selected_value = known_code = code
                    choice = _visible_choice(uia, matches, binding)
                    if choice is not None or time.monotonic() >= deadline:
                        break
                    time.sleep(.05)
            if choice is not None:
                def choice_guard():
                    check_target()
                    actual, candidates = _options(binding, uia, element, value)
                    current = _visible_choice(uia, candidates, binding)
                    _require(actual == selected_value and current is not None and uia.same(choice, current),
                             'combo_option_changed')
                send(choice, 'Select', choice_guard)
            else:
                def write_guard():
                    check_target()
                    actual, candidates = _options(binding, uia, element, value)
                    _require(bool(candidates), 'combo_option_not_observed')
                    _require(actual == selected_value, 'combo_option_changed')
                    _require(len(candidates) == 1, 'combo_option_ambiguous')
                    _require(candidates[0].property(30010) is True and 'SetValue' in uia.actions(element),
                             'combo_value_not_writable')
                    # Name can identify a visible item for Select, but custom
                    # editor labels/indices are not a value to write to a combo.
                    _require(uia._accessible_value(candidates[0]).get('value') == selected_value,
                             'combo_option_provider_value_required')
                _require(bool(matches), 'combo_option_not_observed')
                _require('SetValue' in uia.actions(element), 'visible_selectable_option_required')
                send(element, 'SetValue', write_guard, selected_value)
            readback = read_value
        else:
            commands = {
                'click': ('Invoke', 'Click', {'menuitem', 'button', 'splitbutton', 'hyperlink'}),
                'invoke': ('Invoke', 'Click', {'menuitem', 'button', 'splitbutton', 'hyperlink'}),
                'expand': ('Expand', 'Expand', {'menuitem', 'combobox', 'treeitem', 'splitbutton'}),
                'choose': ('Select', 'Select', {'listitem', 'treeitem', 'tab'}),
            }
            _require(action in commands and value is None, 'unsupported_action')
            operation, capability, roles = commands[action]
            _require(role in roles, 'unsupported_role')
            def guard():
                check_target()
                _require(capability in uia.actions(element), 'pattern_unavailable')
                if operation == 'Select' or role != 'combobox':
                    _require(_visible(uia, element, binding), 'target_offscreen')
                if operation == 'Expand':
                    _require(element.property(30070) == 0, 'expansion_state_changed')
            if operation == 'Expand' and element.property(30070) == 1:
                check_target()
                if element.property(30070) == 1:
                    return receipt('verified', 'already_expanded', 'matched')
            send(element, operation, guard)
            if operation == 'Invoke':
                return receipt('delivered_unverified', 'business_result_not_verified')
            def readback():
                if operation == 'Select':
                    return element.property(30079)
                state = element.property(30070)
                return state == 1 if state in (0, 1) else None

        # One mutation, optional read-only settling. A disappearing element is never success.
        verifying = True
        deadline = time.monotonic() + READBACK_SECONDS
        while True:
            check_target(full=False)
            matched = readback()
            if matched is True:
                check_target()
                matched = readback()
                if matched is True:
                    return receipt('verified', 'current_state_matched', 'matched')
            if time.monotonic() >= deadline:
                if matched is False:
                    return receipt('mismatch', 'current_state_mismatch', 'mismatch')
                return receipt('delivered_unverified', 'current_state_unavailable')
            time.sleep(.05)
    except (Exception, KeyboardInterrupt) as error:
        detail = str(error)
        diagnostic = detail if re.fullmatch('[a-z_]{1,80}', detail) else 'provider_error'
        if pending:
            return receipt('uncertain', 'pattern_delivery_unknown')
        if sent and verifying:
            return receipt('delivered_unverified', 'current_state_unavailable')
        return receipt('blocked', diagnostic)
