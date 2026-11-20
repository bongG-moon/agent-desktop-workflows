"""Locate a unique current result value in its recorded table before using the pointer."""
import control_lookup as lookup
import coordination
import provider_visibility as visibility
import row_action
import win_bridge as win


SURFACES = {'list', 'table', 'group'}
_GRID = {50008, 50028, 50036}
_ROW = {50007, 50029}
_FORM_CONTROLS = {50000, 50002, 50003, 50004, 50005, 50013}
_NON_DATA = _FORM_CONTROLS | {50034, 50035}


def require(condition, code):
    if not condition:
        raise win.BridgeError(code)


def _surface(chain):
    return next((e for e in chain if e.property(30003) in _GRID), chain[-1])


def find(binding, uia, root_element, expected, *, column=None):
    """Return one visible cell, skipping form inputs, column captions and hidden rows."""
    binding.check()
    window = uia.from_handle(binding.hwnd)
    lookup.ancestry(uia, root_element, window, pid=binding.pid)
    require(win.ROLES.get(root_element.property(30003)) in SURFACES, 'result_container_required')
    require(isinstance(expected, str) and expected.strip(), 'result_value_required')
    require(visibility.visible(binding, uia, root_element, window), 'result_container_not_visible')
    header = None
    if column is not None:
        require(isinstance(column, str) and column.strip(), 'result_column_required')
        headers = [e for e in lookup.find_all(uia, root_element, roles=('headeritem',))
                   if visibility.visible(binding, uia, e, window)]
        named = [e for e in headers if (e.property(30005) or '').strip().casefold() == column.strip().casefold()]
        require(len(named) == 1, 'result_column_ambiguous' if named else 'result_column_unavailable')
        header = named[0]
        header_box = uia.bounds(header)
        header_grid = _surface(lookup.ancestry(uia, header, root_element, pid=binding.pid))
        overlaps = []
        for other in headers:
            box = uia.bounds(other)
            if (not uia.same(other, header) and box['x'] < header_box['x'] + header_box['width']
                    and header_box['x'] < box['x'] + box['width']
                    and uia.same(_surface(lookup.ancestry(uia, other, root_element, pid=binding.pid)), header_grid)):
                overlaps.append(box)

    matches = []
    roles = ('listitem', 'row', 'statictext', 'custom')
    for cell in lookup.find_all(uia, root_element, name=expected, roles=roles):
        if not visibility.visible(binding, uia, cell, window) or cell.property(30005) != expected:
            continue
        chain = lookup.ancestry(uia, cell, root_element, pid=binding.pid)
        if not any(e.property(30003) in _ROW for e in chain) and cell.property(30029) is not True:
            continue  # No row or GridItemPattern. A label over a form is not a result.
        if any(e.property(30003) in _NON_DATA for e in chain):
            continue
        if any(e.property(30003) in _ROW and e.property(30022) is True for e in chain):
            continue
        if header is not None:
            box = uia.bounds(cell)
            if not (uia.same(header_grid, _surface(chain))
                    and header_box['x'] - 1 <= box['x']
                    and box['x'] + box['width'] <= header_box['x'] + header_box['width'] + 1):
                continue
            center = box['x'] + box['width'] / 2
            require(header_box['x'] <= center < header_box['x'] + header_box['width']
                    and not any(other['x'] <= center < other['x'] + other['width'] for other in overlaps),
                    'result_column_ambiguous')
        # Providers may name both a row and its contained text peer. Prefer
        # the more precise peer while preserving ambiguity across siblings.
        if any(uia.within(existing, cell) for existing in matches):
            continue
        matches = [existing for existing in matches if not uia.within(cell, existing)]
        matches.append(cell)
    require(len(matches) == 1, 'result_cell_ambiguous' if matches else 'result_cell_missing')
    return matches[0]


def _point_target(binding, uia, cell, point):
    native = win.window_from_point(*point)
    require(native and win.root_window(native) == binding.hwnd and win.hwnd_pid(native) == binding.pid,
            'result_cell_occluded')
    hit = uia.from_point(*point)
    chain = lookup.ancestry(uia, hit, cell, pid=binding.pid)
    require(all(e.property(30003) not in _FORM_CONTROLS and e.property(30022) is not True for e in chain),
            'result_cell_occluded')
    return native


def execute(binding, uia, root_element, expected, guard, *, column=None):
    """Re-resolve the current value under the pointer reservation, then send one double-click."""
    with coordination.desktop_input():
        guard()
        row_action.foreground(binding)
        win._combo_surface(binding)
        tick = row_action._input_tick()
        cell = find(binding, uia, root_element, expected, column=column)
        box = uia.bounds(cell)
        point = (int(box['x'] + box['width'] / 2), int(box['y'] + box['height'] / 2))
        native_hit = _point_target(binding, uia, cell, point)
        guard()
        win._combo_surface(binding)
        # Refresh the column, uniqueness and UIA hit after the final guard. Virtualized
        # rows and in-grid overlays can change without changing the native window.
        current = find(binding, uia, root_element, expected, column=column)
        require(uia.same(current, cell) and uia.bounds(current) == box and current.property(30010) is True,
                'result_cell_changed')
        require(_point_target(binding, uia, current, point) == native_hit
                and win.root_window(win.foreground()) == binding.hwnd, 'result_cell_changed')
        require(row_action._input_tick() == tick, 'foreground_or_input_changed')
        try:
            sent = win._send_point(*point, 'double-click')
        except Exception as error:
            sent = dict(ok=False, diagnostic=getattr(error, 'code', 'point_delivery_unknown'),
                        input_dispatched=getattr(error, 'input_dispatched', None))
    delivered = sent['ok'] and sent.get('input_dispatched') is True
    return dict(status='delivered_unverified' if delivered else 'uncertain',
                diagnostic='visual_double_click_dispatched' if delivered else sent.get('diagnostic', 'partial_input'),
                input_dispatched=sent.get('input_dispatched'), physical_input=True, input_mode='physical_pointer',
                gesture='double-click', target_resolution='current_result_value', effect_verification='unverified')
