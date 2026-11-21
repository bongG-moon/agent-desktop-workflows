"""Locate a unique current result value in its recorded table before using the pointer."""
import control_lookup as lookup
import coordination
import grid_provider
import provider_visibility as visibility
import row_action
import win_bridge as win


SURFACES = {'list', 'table', 'group'}
_GRID = {50008, 50028, 50036}
_ROW = {50007, 50029}
_FORM_CONTROLS = {50000, 50002, 50003, 50004, 50005, 50013}
_NON_DATA = _FORM_CONTROLS | {50034, 50035}
_MAX_GRID_ROWS = 64


def require(condition, code):
    if not condition:
        raise win.BridgeError(code)


def _surface(chain):
    return next((e for e in chain if e.property(30003) in _GRID), chain[-1])


def _cells_in(uia, table, **query):
    try:
        return lookup.find_all(uia, table, **query)
    except win.BridgeError as error:
        if error.code == 'target_candidates_incomplete':
            raise win.BridgeError('result_grid_scan_limit') from error
        raise


def bare_group(uia, element):
    """Whether this group currently exposes neither table patterns nor child controls."""
    if (element.property(30003) != 50026 or
            any(element.property(prop) is True for prop in (30030, 30038, 30108))):
        return False
    child = next(iter(uia.children(element)), None)
    if child is not None:
        child.close()
        return False
    return True


def bound_group(binding, uia, header_point, cell_point):
    """Identify the owner-drawn group directly under both reviewed screen points."""
    binding.check()
    window = uia.from_handle(binding.hwnd)
    native_tables = lookup.find_all(uia, window, roles=('table', 'list'))
    try:
        require(not any(visibility.visible(binding, uia, t, window) for t in native_tables),
                'result_table_ambiguous')
    finally:
        for item in native_tables:
            item.close()
    require(header_point[1] < cell_point[1], 'result_group_not_proven')
    for point in (header_point, cell_point):
        native = win.window_from_point(*point)
        require(native and win.root_window(native) == binding.hwnd and win.hwnd_pid(native) == binding.pid,
                'result_cell_occluded')
    group = uia.from_point(*header_point)
    lookup.ancestry(uia, group, window, pid=binding.pid)
    require(uia.same(group, uia.from_point(*cell_point)) and bare_group(uia, group)
            and not uia.same(group, window) and group.property(30010) is True
            and group.property(30149) is not True
            and (group.property(30043) is not True or group.property(30046) is True)
            and visibility.visible(binding, uia, group, window), 'result_group_not_proven')
    box = uia.bounds(group)
    require(all(box['x'] <= x < box['x'] + box['width'] and box['y'] <= y < box['y'] + box['height']
                for x, y in (header_point, cell_point)), 'result_group_not_proven')
    selector = {'role': 'group'}
    identifier = group.property(30011)
    if lookup._text(identifier) and not identifier.isdecimal():
        selector['automation_id'] = identifier
    else:
        name = group.property(30005)
        require(lookup._text(name), 'result_group_not_proven')
        selector['name'] = name
    current = lookup.resolve(binding, uia, selector, window)
    require(uia.same(group, current) and uia.bounds(current) == box, 'result_group_changed')
    return selector, box


def valid_column_hint(hint):
    return (isinstance(hint, dict) and set(hint) in ({'automation_id', 'role'}, {'automation_id', 'role', 'index'})
            and hint['role'] in win.ROLES.values() and lookup._text(hint['automation_id'])
            and not hint['automation_id'].isdecimal()
            and ('index' not in hint or type(hint['index']) is int and 0 <= hint['index'] < 256))


def _remembered_header(binding, uia, table, window, column, hint, headers):
    if not valid_column_hint(hint):
        return None
    index = hint.get('index')
    if index is not None and index < len(headers):
        candidate = headers[index]
        lookup.ancestry(uia, candidate, table, pid=binding.pid)
        if (candidate.property(30011) == hint['automation_id']
                and win.ROLES.get(candidate.property(30003)) == hint['role']
                and (candidate.property(30005) or '').strip().casefold() == column.strip().casefold()
                and visibility.visible(binding, uia, candidate, window)):
            return candidate
    try:
        candidate = lookup.resolve(binding, uia, {k: hint[k] for k in ('role', 'automation_id')}, table)
    except win.BridgeError as error:
        if error.code in ('target_missing', 'target_ambiguous'):
            return None
        raise
    if ((candidate.property(30005) or '').strip().casefold() == column.strip().casefold()
            and visibility.visible(binding, uia, candidate, window)):
        return candidate
    return None


def _column(binding, uia, table, window, column, hint):
    from_pattern = False
    headers = []
    if table.property(30038) is True:
        try:
            headers = grid_provider.headers(table)
        except win.BridgeError as error:
            if error.code not in ('accessibility_unavailable', 'result_provider_unavailable'):
                raise
        from_pattern = bool(headers)
    remembered = _remembered_header(binding, uia, table, window, column, hint, headers)
    if not headers:
        headers = _cells_in(uia, table, roles=('headeritem',))
    named, visible, known_names = [], [], 0
    strict_surface = from_pattern or table.property(30003) in _GRID
    for index, item in enumerate(headers):
        # Pattern arrays are supplied by the application. Check ancestry before reading names.
        chain = lookup.ancestry(uia, item, table, pid=binding.pid)
        scope = _surface(chain)
        if strict_surface and not uia.same(scope, table):
            continue
        shown = visibility.visible(binding, uia, item, window)
        if not shown and not from_pattern:
            continue
        if shown:
            visible.append((item, scope))
        if remembered is not None and uia.same(item, remembered):
            label = column
        else:
            label = item.property(30005)
        if isinstance(label, str) and label.strip():
            known_names += 1
        if isinstance(label, str) and label.strip().casefold() == column.strip().casefold():
            named.append((index, item))
    if not named and not from_pattern and remembered is not None:
        named.append((None, remembered))
    if not named:
        missing = (bool(known_names) and known_names == len(headers)
                   and (from_pattern or table.property(30063) == known_names))
        return dict(relation=column, index=None, hint=None, known_columns=missing)
    require(len(named) == 1, 'result_column_ambiguous')
    index, header = named[0]
    header_box = uia.bounds(header)
    grid = _surface(lookup.ancestry(uia, header, table, pid=binding.pid))
    overlaps = [uia.bounds(item) for item, scope in visible if not uia.same(item, header) and uia.same(scope, grid)]
    identifier = header.property(30011)
    current = {'role': win.ROLES.get(header.property(30003)), 'automation_id': identifier}
    if from_pattern and index is not None:
        current['index'] = index
    known_index = header.property(30065)
    if type(known_index) is int and known_index >= 0:
        index = known_index
    return dict(box=header_box, grid=grid, others=overlaps, index=index, name=column, element=header,
                geometry=visibility.visible(binding, uia, header, window),
                indexed=type(known_index) is int and known_index >= 0,
                hint=current if valid_column_hint(current) else None)


def _related_column(uia, chain, name, expected_header=None):
    # Logical TableItem relationships do not depend on screen rectangles.
    item = next((peer for peer in chain[:-1] if peer.property(30039) is True), None)
    if item is None:
        return None
    matches = []
    for peer in grid_provider.headers(item, cell=True):
        owner = lookup.ancestry(uia, peer, chain[-1], pid=uia.pid(chain[-1]))
        if (uia.same(_surface(owner), _surface(chain))
                and (peer.property(30005) or '').strip().casefold() == name.strip().casefold()
                and (expected_header is None or uia.same(peer, expected_header))):
            matches.append(peer)
    require(len(matches) <= 1, 'result_column_ambiguous')
    return matches[0] if matches else None


def _in_column(uia, cell, chain, header):
    if header is None:
        return True
    if 'relation' in header:
        peer = _related_column(uia, chain, header['relation'])
        if peer is not None:
            identity = {'role': win.ROLES.get(peer.property(30003)), 'automation_id': peer.property(30011)}
            if valid_column_hint(identity):
                header['hint'] = identity
        return peer is not None
    if not header['geometry']:
        return (uia.same(header['grid'], _surface(chain))
                and (header['indexed'] and cell.property(30065) == header['index']
                     or _related_column(uia, chain, header['name'], header['element']) is not None))
    box = uia.bounds(cell)
    head = header['box']
    center = box['x'] + box['width'] / 2
    if not (uia.same(header['grid'], _surface(chain)) and head['x'] - 1 <= box['x']
            and box['x'] + box['width'] <= head['x'] + head['width'] + 1):
        return False
    require(head['x'] <= center < head['x'] + head['width']
            and not any(other['x'] <= center < other['x'] + other['width'] for other in header['others']),
            'result_column_ambiguous')
    return True


def _provider_value(cell):
    value, legacy = cell.property(30045), cell.property(30093)
    if isinstance(value, str):
        return True, None if isinstance(legacy, str) and legacy and legacy != value else value
    if isinstance(legacy, str) and legacy:
        return True, legacy
    return False, None


def _cell_text(cell):
    present, value = _provider_value(cell)
    if present:
        return value
    name = cell.property(30005)
    return name if isinstance(name, str) and name else None


def _non_data_control(item):
    role = item.property(30003)
    if role == 50004:
        return item.property(30046) is not True
    return role in _NON_DATA


def _grid_value(binding, uia, cell):
    present, value = _provider_value(cell)
    if present:
        return cell, value
    name = cell.property(30005)
    name = name if isinstance(name, str) and name else None
    if cell.property(30003) not in {50007, 50025, 50026, 50029}:
        return cell, name
    # Grid.GetItem can return a named wrapper (e.g. "Cell"), with the
    # actual displayed data accessible on a descendant TextBlock.
    observed = {}
    for peer in _cells_in(uia, cell, roles=('statictext', 'textfield')):
        chain = lookup.ancestry(uia, peer, cell, pid=binding.pid)
        if any(_non_data_control(e) or e.property(30003) in _GRID for e in chain[:-1]):
            continue
        text = _cell_text(peer)
        if text is not None:
            observed[text] = peer
    if len(observed) == 1:
        text, peer = next(iter(observed.items()))
        return peer, text
    return cell, None if observed else name


def _grid_match(binding, uia, table, window, header, expected):
    """Try the provider's two-dimensional API on a bounded result set."""
    rows, columns = table.property(30062), table.property(30063)
    index = header['index']
    if not (type(rows) is int and 0 < rows <= _MAX_GRID_ROWS and type(columns) is int and 0 <= index < columns):
        return None
    found, proved, unreadable, readable = [], False, False, False
    for number, cell in enumerate(grid_provider.column_cells(table, index, rows)):
        chain = lookup.ancestry(uia, cell, table, pid=binding.pid)
        if any(_non_data_control(e) for e in chain):
            unreadable = True
            continue
        if (not uia.same(header['grid'], _surface(chain)) or cell.property(30064) not in (None, number)
                or cell.property(30065) not in (None, index)):
            return None
        # The header array order alone does not establish a logical column.
        # GetItem accesses one column across all rows. Verify its identity by an
        # explicit GridItem index on the header, a linked header, or live geometry.
        if header['indexed'] and cell.property(30065) == index:
            proved = True
        elif header['geometry'] and visibility.visible(binding, uia, cell, window):
            if not _in_column(uia, cell, chain, header):
                return None
            proved = True
        elif _related_column(uia, chain, header['name'], header['element']) is not None:
            proved = True
        peer, value = _grid_value(binding, uia, cell)
        if value is None:
            unreadable = True
        else:
            readable = True
            if value == expected:
                found.append(peer)
    if not proved:
        return None
    require(len(found) < 2, 'result_cell_ambiguous')
    require(not unreadable, 'result_grid_incomplete' if readable else 'result_cell_unreadable')
    require(bool(found), 'result_value_not_found')
    return found[0]


def _data_chain(binding, uia, cell, root_element, window, header):
    if not visibility.visible(binding, uia, cell, window):
        return None
    chain = lookup.ancestry(uia, cell, root_element, pid=binding.pid)
    if any(_non_data_control(e) for e in chain):
        return None
    if not any(e.property(30003) in _ROW or e.property(30029) is True or e.property(30039) is True
               for e in chain[:-1]):
        return None
    if any(e.property(30003) in _ROW and e.property(30022) is True for e in chain):
        return None
    return chain if _in_column(uia, cell, chain, header) else None


def _readable_rows(binding, uia, table, window, header, *, count=None):
    # Only run this diagnostic after a miss. A named row spanning multiple
    # columns is not evidence that its cells are readable.
    roles = ('listitem', 'row', 'statictext', 'custom', 'textfield')
    nodes = _cells_in(uia, table, roles=roles)
    rows, readable, indices = [], [], set()
    for node in nodes:
        if node.property(30003) in _ROW and visibility.visible(binding, uia, node, window):
            rows.append(node)
        chain = _data_chain(binding, uia, node, table, window, header)
        if chain and _cell_text(node) is not None:
            readable.extend(e for e in chain if e.property(30003) in _ROW)
            if count is not None:
                for item in chain:
                    index = item.property(30064)
                    if item.property(30029) is True and type(index) is int and 0 <= index < count:
                        indices.add(index)
                        break
    if count is not None:
        return len(indices) == count
    return bool(rows) and all(any(uia.same(row, known) for known in readable) for row in rows)


def find(binding, uia, root_element, expected, *, column=None, column_hint=None, evidence=None):
    """Read a unique current cell without moving the pointer or capturing pixels."""
    binding.check()
    window = uia.from_handle(binding.hwnd)
    lookup.ancestry(uia, root_element, window, pid=binding.pid)
    role = win.ROLES.get(root_element.property(30003))
    require(role in SURFACES or role == 'custom' and (
        root_element.property(30030) is True or root_element.property(30038) is True), 'result_container_required')
    require(isinstance(expected, str) and expected.strip(), 'result_value_required')
    require(visibility.visible(binding, uia, root_element, window), 'result_container_not_visible')
    header = None
    oversized = None
    if column is not None:
        require(isinstance(column, str) and column.strip(), 'result_column_required')
        header = _column(binding, uia, root_element, window, column, column_hint)
        if root_element.property(30030) is True and root_element.property(30062) == 0:
            raise win.BridgeError('result_value_not_found')
        if evidence is not None and header['hint']:
            evidence['column_hint'] = header['hint']
        if header['index'] is not None and root_element.property(30030) is True:
            count = root_element.property(30062)
            if type(count) is int and count > _MAX_GRID_ROWS:
                oversized = count
            try:
                cell = _grid_match(binding, uia, root_element, window, header, expected)
            except win.BridgeError as error:
                if error.code not in ('accessibility_unavailable', 'result_provider_unavailable'):
                    raise
            else:
                if cell is not None:
                    if evidence is not None:
                        evidence['source'] = 'grid_pattern'
                    return cell

    matches = []
    roles = ('listitem', 'row', 'statictext', 'custom', 'textfield')
    for cell in _cells_in(uia, root_element, cell_text=expected, roles=roles):
        if not _data_chain(binding, uia, cell, root_element, window, header):
            continue
        cell, value = _grid_value(binding, uia, cell)
        if value != expected:
            continue
        # Providers may name both a row and its contained text peer. Prefer
        # the more precise peer while preserving ambiguity across siblings.
        if any(uia.within(existing, cell) for existing in matches):
            continue
        matches = [existing for existing in matches if not uia.within(cell, existing)]
        matches.append(cell)
    if len(matches) < 2 and header is not None and (not matches or oversized is not None):
        complete = _readable_rows(binding, uia, root_element, window, header, count=oversized)
        if oversized is not None and not complete:
            raise win.BridgeError('result_grid_scan_limit')
        if not matches and complete:
            raise win.BridgeError('result_value_not_found')
    if not matches and header and 'relation' in header:
        raise win.BridgeError('result_column_not_found' if header['known_columns'] else 'result_column_unavailable')
    require(len(matches) == 1, 'result_cell_ambiguous' if matches else 'result_cell_missing')
    if evidence is not None:
        evidence['source'] = 'cell_element'
        if header and header['hint']:
            evidence['column_hint'] = header['hint']
    return matches[0]


def _point_target(binding, uia, cell, point):
    native = win.window_from_point(*point)
    require(native and win.root_window(native) == binding.hwnd and win.hwnd_pid(native) == binding.pid,
            'result_cell_occluded')
    hit = uia.from_point(*point)
    chain = lookup.ancestry(uia, hit, cell, pid=binding.pid)
    require(all(not _non_data_control(e) and e.property(30022) is not True for e in chain),
            'result_cell_occluded')
    return native


def execute(binding, uia, root_element, expected, guard, *, column=None, column_hint=None):
    """Re-resolve the current value under the pointer reservation, then send one double-click."""
    with coordination.desktop_input():
        guard()
        row_action.foreground(binding)
        win._combo_surface(binding)
        tick = row_action._input_tick()
        cell = find(binding, uia, root_element, expected, column=column, column_hint=column_hint)
        root = uia.from_handle(binding.hwnd)
        require(visibility.visible(binding, uia, cell, root), 'result_cell_not_clickable')
        box = uia.bounds(cell)
        point = (int(box['x'] + box['width'] / 2), int(box['y'] + box['height'] / 2))
        native_hit = _point_target(binding, uia, cell, point)
        guard()
        win._combo_surface(binding)
        # Refresh the column, uniqueness and UIA hit after the final guard. Virtualized
        # rows and in-grid overlays can change without changing the native window.
        current = find(binding, uia, root_element, expected, column=column, column_hint=column_hint)
        require(uia.same(current, cell) and uia.bounds(current) == box and visibility.visible(binding, uia, current, root)
                and current.property(30010) is True,
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
