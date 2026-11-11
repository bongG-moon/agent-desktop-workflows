"""Conservative combo readback for providers exposing duplicate editor peers.

This compatibility layer is shared by live execution and demonstration capture;
the independently pinned Windows bridge is deliberately left unchanged.
"""
from collections import deque

import win_bridge as win


def value_observation(uia, element):
    """Combine agreeing values, never derive a selection from available options.

    Several editor peers may describe the same current value. Their identical
    nonempty values corroborate each other; conflicting values or multiple
    distinct selected rows remain ambiguous. Blank peers supply no evidence.
    """
    if element.property(30003) != 50003:
        return win.UIA.value_observation(uia, element)
    unavailable = {'value': None, 'value_evidence': {'status': 'unavailable', 'source': 'none'}}
    conflict = {'value': None, 'value_evidence': {'status': 'conflict', 'source': 'none'}}
    if uia.secure(element):
        return unavailable
    owner_pid = uia.pid(element)
    if type(owner_pid) is not int or owner_pid <= 0:
        return unavailable
    own = uia._accessible_value(element)
    if own.get('value_evidence', {}).get('status') == 'conflict':
        return conflict
    values = [('parent', own['value'])] if own.get('value') else []
    selections = []

    def selection(item):
        # Validate both the explicit owner and nearest ancestor combo before
        # reading text. A detached selected peer needs an explicit owner.
        if uia.pid(item) != owner_pid or uia.secure(item):
            return unavailable
        container = uia.related_element(item, 30080)
        if container is not None and not uia.same(container, element):
            return unavailable
        current, in_combo = item, False
        for _ in range(32):
            if current is None:
                break
            if uia.pid(current) != owner_pid or uia.secure(current):
                return unavailable
            if current.property(30003) == 50003:
                if not uia.same(current, element):
                    return unavailable
                in_combo = True
                break
            if current.property(30003) == 50032:
                break  # A detached popup ends here; its desktop parent is not its owner.
            current = uia.parent(current)
        if not in_combo and container is None:
            return unavailable
        if any(uia.same(item, prior) for prior in selections):
            return None
        selections.append(item)
        if len(selections) > 1:
            return conflict
        observed = uia._selected_option_observation(item)
        if observed.get('value') is None:
            return observed
        values.append(('selection', observed['value']))
        return None

    if element.property(30037) is True:
        getter = getattr(element, 'selected_items', None)
        if getter is not None:
            try:
                selected = getter()
            except win.BridgeError:
                return unavailable
            if selected is None or len(selected) > 256:
                return unavailable
            for item in selected:
                result = selection(item)
                if result is not None:
                    return result

    pending = deque((child, 1) for child in uia.children(element))
    visited = 0
    while pending:
        child, depth = pending.popleft()
        visited += 1
        if visited > 256 or depth > 8 or uia.pid(child) != owner_pid or uia.secure(child):
            return unavailable
        role = child.property(30003)
        if role == 50003:
            continue
        if role == 50007:
            if child.property(30036) is True and child.property(30079) is True:
                result = selection(child)
                if result is not None:
                    return result
            continue  # An option's content is not a combo editor.
        if role == 50004:
            observed = uia._accessible_value(child)
            if observed.get('value_evidence', {}).get('status') == 'conflict':
                return conflict
            if observed.get('value'):
                values.append(('editor', observed['value']))
            continue
        pending.extend((nested, depth + 1) for nested in uia.children(child))
    if len({value for _, value in values}) > 1:
        return conflict
    if not values:
        return unavailable
    return {'value': values[0][1], 'value_evidence': {
        'status': 'observed', 'source': values[0][0] if len(values) == 1 else 'corroborated'}}
