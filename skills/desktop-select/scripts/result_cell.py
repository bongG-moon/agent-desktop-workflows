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
_MAX_ROW_COLUMNS = 16


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


def readable_surface(binding, uia, element, window):
    """A clipped container may be read; this never establishes a clickable cell."""
    role = win.ROLES.get(element.property(30003))
    if role not in SURFACES and not (role == 'custom' and (
            element.property(30030) is True or element.property(30038) is True)):
        return False
    if visibility.visible(binding, uia, element, window):
        return True
    if element.property(30022) is not None:
        return False
    box = uia.bounds(element)
    if not visibility.valid(box):
        return False
    binding.check()
    if not win.user32.IsWindowVisible(binding.hwnd) or win.user32.IsIconic(binding.hwnd):
        return False
    lookup.ancestry(uia, element, window, pid=binding.pid)
    # UIA can expose the full scrollable surface beyond this HWND. Use actual
    # window geometry only for this read permission; cell/header guards remain
    # unchanged and local confirmation still compares the captured intersection.
    from row_vision import _window_rect
    x, y, width, height = _window_rect(binding)
    return (width > 0 and height > 0
            and max(x, box['x']) < min(x + width, box['x'] + box['width'])
            and max(y, box['y']) < min(y + height, box['y'] + box['height']))


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
    if header['indexed']:
        owner = next((item for item in chain[:-1] if item.property(30029) is True), None)
        if owner is not None:
            logical_column = owner.property(30065)
            require(logical_column is None or logical_column == header['index'], 'result_column_ambiguous')
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


def _grid_match(binding, uia, table, window, header, expected, evidence):
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
        require(uia.same(header['grid'], _surface(chain)) and cell.property(30064) in (None, number)
                and cell.property(30065) in (None, index), 'result_grid_incomplete')
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
    require(table.property(30062) == rows and table.property(30063) == columns, 'result_grid_incomplete')
    if evidence is not None:
        evidence.update(source='grid_pattern', rows_examined=rows)
        if not unreadable:
            evidence['match_count'] = len(found)
        elif found:
            evidence['match_count_at_least'] = len(found)
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


def _readable_rows(binding, uia, table, window, header, *, count=None, matched=None):
    # A value search can find one visible ID even when a second row has no
    # readable cell. Use the cheap row-only query on the common successful
    # single-row path; inspect cells only when other rows need accounting.
    scope = header.get('grid', table)

    # A scrolled/virtualized provider can omit whole rows from its children.
    # Without the logical row count, the mounted viewport cannot prove uniqueness.
    if count is None and scope.property(30058) is True:
        return 0

    def own_row(node):
        chain = lookup.ancestry(uia, node, scope, pid=binding.pid)
        return uia.same(_surface(chain), scope) and not any(_non_data_control(e) for e in chain)

    if matched is not None and count is None:
        rows = [row for row in _cells_in(uia, scope, roles=('listitem', 'row')) if own_row(row)]
        if len(rows) == 1 and uia.within(matched, rows[0]):
            return 1
    roles = ('listitem', 'row', 'statictext', 'custom', 'textfield')
    nodes = _cells_in(uia, scope, roles=roles)
    rows, readable, indices = [], [], set()
    for node in nodes:
        if node.property(30003) in _ROW and own_row(node):
            rows.append(node)
        chain = _data_chain(binding, uia, node, scope, window, header)
        if chain and _cell_text(node) is not None:
            readable.extend(e for e in chain if e.property(30003) in _ROW)
            if count is not None:
                for item in chain:
                    index = item.property(30064)
                    if item.property(30029) is True and type(index) is int and 0 <= index < count:
                        indices.add(index)
                        break
    if count is not None:
        return count if len(indices) == count else 0
    if all(any(uia.same(row, known) for known in readable) for row in rows):
        return len(rows)
    return 0


def find(binding, uia, root_element, expected, *, column=None, column_hint=None, evidence=None):
    """Read a unique current cell without moving the pointer or capturing pixels."""
    binding.check()
    window = uia.from_handle(binding.hwnd)
    lookup.ancestry(uia, root_element, window, pid=binding.pid)
    role = win.ROLES.get(root_element.property(30003))
    require(role in SURFACES or role == 'custom' and (
        root_element.property(30030) is True or root_element.property(30038) is True), 'result_container_required')
    require(isinstance(expected, str) and expected.strip(), 'result_value_required')
    require(readable_surface(binding, uia, root_element, window), 'result_container_not_visible')
    # Owner-drawn groups with no patterns or descendants have no provider text
    # to inspect. One child lookup replaces header, value and row subtree scans.
    # The caller still decides whether a delayed provider should be polled.
    if column is not None and bare_group(uia, root_element):
        raise win.BridgeError('result_column_unavailable')
    header = None
    grid_count = None
    if column is not None:
        require(isinstance(column, str) and column.strip(), 'result_column_required')
        header = _column(binding, uia, root_element, window, column, column_hint)
        if root_element.property(30030) is True:
            count = root_element.property(30062)
            if type(count) is int and count == 0:
                if evidence is not None:
                    evidence.update(source='grid_pattern', rows_examined=0, match_count=0)
                raise win.BridgeError('result_value_not_found')
            if type(count) is int and count > 0:
                grid_count = count
        if evidence is not None and header['hint']:
            evidence['column_hint'] = header['hint']
        if header['index'] is not None and grid_count is not None:
            try:
                cell = _grid_match(binding, uia, root_element, window, header, expected, evidence)
            except win.BridgeError as error:
                if error.code not in ('accessibility_unavailable', 'result_provider_unavailable'):
                    raise
            else:
                if cell is not None:
                    if evidence is not None:
                        evidence['observed'] = expected
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
    if evidence is not None:
        evidence['source'] = 'cell_element'
        if len(matches) > 1:
            evidence['match_count_at_least'] = len(matches)
    if len(matches) < 2 and header is not None:
        rows = _readable_rows(binding, uia, root_element, window, header, count=grid_count,
                              matched=matches[0] if matches else None)
        if evidence is not None and rows:
            evidence.update(rows_examined=rows, match_count=len(matches))
        if not rows and grid_count is not None and grid_count > _MAX_GRID_ROWS:
            raise win.BridgeError('result_grid_scan_limit')
        if not rows and matches:
            raise win.BridgeError('result_grid_incomplete')
        if not matches and rows:
            raise win.BridgeError('result_value_not_found')
    if not matches and header and 'relation' in header:
        raise win.BridgeError('result_column_not_found' if header['known_columns'] else 'result_column_unavailable')
    require(len(matches) == 1, 'result_cell_ambiguous' if matches else 'result_cell_missing')
    if evidence is not None:
        evidence.update(observed=expected, match_count=1)
        if header and header['hint']:
            evidence['column_hint'] = header['hint']
    return matches[0]


def find_row(binding, uia, table, *, mode='single', column=None):
    """Read an actual current row without comparing text to the recording.

    A Grid provider establishes logical row zero. Otherwise select the single
    uppermost visible row of this exact table; a bare drawing is not a row.
    """
    binding.check()
    require(mode in ('single', 'first', 'recorded'), 'invalid_result_row_mode')
    window = uia.from_handle(binding.hwnd)
    lookup.ancestry(uia, table, window, pid=binding.pid)
    require(readable_surface(binding, uia, table, window), 'result_container_not_visible')
    if bare_group(uia, table):
        raise win.BridgeError('result_column_unavailable' if column else 'result_row_unavailable')
    header = _column(binding, uia, table, window, column, None) if column else None
    if table.property(30030) is True:
        rows, columns = table.property(30062), table.property(30063)
        require(type(rows) is int and rows >= 0 and type(columns) is int and columns > 0,
                'result_grid_incomplete')
        require(rows > 0, 'result_row_missing')
        require(mode != 'single' or rows == 1, 'result_row_ambiguous')
        # The first column may be hidden or contain a checkbox. Keep logical
        # row zero fixed and try a bounded number of data cells, without text
        # reads, scrolling or virtual-item realization. Ordinary grids use one.
        # A named column can be after the scan bound. Use a provider index
        # if available, but corroborate it against the actual cell/header.
        indices = range(min(columns, _MAX_ROW_COLUMNS))
        if header and header.get('indexed'):
            index = header['index']
            require(type(index) is int and 0 <= index < columns, 'result_column_unavailable')
            indices = (index,)
        for index in indices:
            cell = next(grid_provider.column_cells(table, index, 1))
            chain = lookup.ancestry(uia, cell, table, pid=binding.pid)
            require(uia.same(_surface(chain), table)
                    and cell.property(30064) in (None, 0) and cell.property(30065) in (None, index),
                    'result_grid_incomplete')
            if any(_non_data_control(e) for e in chain):
                continue
            require(not any(e.property(30003) in _ROW and e.property(30022) is True for e in chain),
                    'result_grid_incomplete')
            if visibility.visible(binding, uia, cell, window) and _in_column(uia, cell, chain, header):
                return cell
        raise win.BridgeError('result_column_unavailable' if column else 'result_row_not_visible')
    rows = []
    for row in _cells_in(uia, table, roles=('row', 'listitem')):
        chain = lookup.ancestry(uia, row, table, pid=binding.pid)
        if (not uia.same(_surface(chain), table) or any(_non_data_control(e) for e in chain)
                or any(e.property(30003) in _ROW and e.property(30022) is True for e in chain[1:])):
            continue
        # Nested row peers represent one row, not two independent candidates.
        if any(uia.within(row, old) for old in rows):
            continue
        rows = [old for old in rows if not uia.within(old, row)]
        rows.append(row)
    require(bool(rows), 'result_row_unavailable')
    require(mode != 'single' or len(rows) == 1, 'result_row_ambiguous')
    # A sole visible row is not a sole result when other row peers are offscreen.
    # Without Grid row indices their relative order is also unknown.
    if mode == 'recorded':
        rows = [row for row in rows if visibility.visible(binding, uia, row, window)]
        require(rows, 'result_row_not_visible')
    else:
        require(all(visibility.visible(binding, uia, row, window) for row in rows), 'result_row_not_visible')
    rows.sort(key=lambda item: (uia.bounds(item)['y'], uia.bounds(item)['x']))
    first = rows[0]
    top = uia.bounds(first)
    require(not any(uia.bounds(other)['y'] < top['y'] + top['height'] for other in rows[1:]),
            'result_row_ambiguous')
    if not header:
        return first
    # A tree provider may expose both a cell wrapper and its text child.
    # Nested matches have one target; two sibling cells remain ambiguous.
    candidates = []
    for peer in [first, *_cells_in(uia, first, roles=('statictext', 'custom', 'textfield', 'listitem', 'row'))]:
        chain = lookup.ancestry(uia, peer, table, pid=binding.pid)
        if (any(_non_data_control(e) for e in chain) or not uia.same(_surface(chain), table)
                or not visibility.visible(binding, uia, peer, window) or not _in_column(uia, peer, chain, header)):
            continue
        if any(uia.within(prior, peer) for prior in candidates):
            continue
        candidates = [prior for prior in candidates if not uia.within(peer, prior)] + [peer]
    require(len(candidates) == 1, 'result_column_ambiguous' if candidates else 'result_column_unavailable')
    return candidates[0]


def _point_target(binding, uia, cell, point, *, body=False, header=False):
    native = win.window_from_point(*point)
    require(native and win.root_window(native) == binding.hwnd and win.hwnd_pid(native) == binding.pid,
            'result_cell_occluded')
    hit = uia.from_point(*point)
    chain = lookup.ancestry(uia, hit, cell, pid=binding.pid)
    if header:
        # Plain text within the named header is fine. A filter button, splitter,
        # embedded editor or another header is a different gesture.
        require(cell.property(30003) == 50035 and all(e.property(30010) is True
                and e.property(30022) is not True for e in chain)
                and all(e.property(30003) == 50020 and not any(e.property(p) is True
                    for p in (30028, 30031, 30036, 30041)) for e in chain[:-1]), 'result_header_hit_changed')
        return native
    require(all(not _non_data_control(e) and e.property(30022) is not True for e in chain),
            'result_cell_occluded')
    if body:
        data_roles = {50006, 50007, 50008, 50020, 50025, 50026, 50028, 50029, 50033, 50036}
        require(all(e.property(30003) in data_roles and e.property(30010) is True
                    and e.property(30149) is not True and (e.property(30043) is not True or e.property(30046) is True)
                    for e in chain) and uia.same(_surface(chain), cell), 'result_point_not_data')
        if not any(e.property(30003) in _ROW or e.property(30029) is True for e in chain):
            # Only a demonstrated point may stand in for a row on a painted
            # surface. A provider that exposes rows must actually hit one.
            require(uia.same(hit, cell) and bare_group(uia, cell), 'result_point_not_data')
    return native


def execute(binding, uia, root_element, expected, guard, *, column=None, column_hint=None):
    """Re-resolve the current value under the pointer reservation, then send one double-click."""
    return _execute(binding, uia, guard,
                    lambda: find(binding, uia, root_element, expected, column=column, column_hint=column_hint),
                    'current_result_value')


def body_point(box, screen_point):
    """Keep a gesture relative to the surface origin, not to a transient screen/window."""
    from learned_tasks import valid_result_point
    require(visibility.valid(box), 'result_point_outside_table')
    point = dict(x=screen_point[0] - box['x'], y=screen_point[1] - box['y'],
                 width=box['width'], height=box['height'])
    require(valid_result_point(point), 'result_point_outside_table')
    return point


def _surface_point(binding, uia, table, anchor):
    """Keep the demonstrated offset inside the current surface and window."""
    root = uia.from_handle(binding.hwnd)
    lookup.ancestry(uia, table, root, pid=binding.pid)
    require(not uia.same(table, root) and table.property(30010) is True
            and readable_surface(binding, uia, table, root), 'result_container_not_visible')
    box = uia.bounds(table)
    require(0 <= anchor['x'] < box['width'] and 0 <= anchor['y'] < box['height'], 'result_row_unavailable')
    point = (box['x'] + anchor['x'], box['y'] + anchor['y'])
    from row_vision import _window_rect
    x, y, width, height = _window_rect(binding)
    require(x <= point[0] < x + width and y <= point[1] < y + height, 'result_row_unavailable')
    return point


def position_in_body(binding, uia, table, anchor):
    point = _surface_point(binding, uia, table, anchor)
    if table.property(30030) is True:
        count = table.property(30062)
        require(type(count) is int and count >= 0, 'result_grid_incomplete')
        require(count > 0, 'result_row_missing')
    return point


def _named_header(binding, uia, table, column):
    try:
        header = lookup.resolve(binding, uia, {'role': 'headeritem', 'name': column}, table)
    except win.BridgeError as error:
        if error.code in ('target_missing', 'target_ambiguous'):
            raise win.BridgeError('result_header_ambiguous' if error.code == 'target_ambiguous'
                                  else 'result_header_unavailable') from error
        raise
    chain = lookup.ancestry(uia, header, table, pid=binding.pid)
    require(uia.same(_surface(chain), table)
            and not any(e.property(30003) in _FORM_CONTROLS | _ROW | {50035}
                        or e.property(30030) is True for e in chain[1:-1])
            and header.property(30010) is True
            and visibility.visible(binding, uia, header, uia.from_handle(binding.hwnd)),
            'result_header_unavailable')
    return header


def _header_point(binding, uia, table, header, anchor):
    point = _surface_point(binding, uia, table, anchor)
    box = uia.bounds(header)
    require(visibility.valid(box) and box['x'] <= point[0] < box['x'] + box['width']
            and box['y'] <= point[1] < box['y'] + box['height'], 'result_header_point_mismatch')
    return point


def position_in_header(binding, uia, table, anchor, column):
    """Observe a recorded point in one named header without foregrounding the window."""
    return _header_point(binding, uia, table, _named_header(binding, uia, table, column), anchor)


def execute_row(binding, uia, table, guard, *, mode='single', point=None, column=None, before_header_dispatch=None):
    if mode == 'header':
        require(point is not None and bool(column), 'invalid_result_row')
        return _execute(binding, uia, guard, lambda: _named_header(binding, uia, table, column),
                        'recorded_column_header',
                        header_point=lambda header: _header_point(binding, uia, table, header, point),
                        before_dispatch=before_header_dispatch)
    if point is not None:
        require(mode == 'recorded' and column is None, 'invalid_result_row')
        return _execute(binding, uia, guard, lambda: table, 'recorded_result_position', anchor=point)
    resolution = 'current_single_result' if mode == 'single' else 'current_first_result'
    return _execute(binding, uia, guard, lambda: find_row(binding, uia, table, mode=mode, column=column), resolution)


def _execute(binding, uia, guard, find_current, resolution, *, anchor=None, header_point=None, before_dispatch=None):
    def point_for(element, box):
        if header_point is not None:
            return header_point(element)
        if anchor is not None:
            return position_in_body(binding, uia, element, anchor)
        require(visibility.visible(binding, uia, element, root), 'result_cell_not_clickable')
        return (int(box['x'] + box['width'] / 2), int(box['y'] + box['height'] / 2))

    def hit_at(element, point):
        return _point_target(binding, uia, element, point, body=anchor is not None, header=header_point is not None)

    with coordination.desktop_input():
        guard()
        row_action.foreground(binding)
        win._combo_surface(binding)
        tick = row_action._input_tick()
        cell = find_current()
        root = uia.from_handle(binding.hwnd)
        box = uia.bounds(cell)
        point = point_for(cell, box)
        native_hit = hit_at(cell, point)
        guard()
        if before_dispatch is not None:
            before_dispatch()
        win._combo_surface(binding)
        # Refresh the column, uniqueness and UIA hit after the final guard. Virtualized
        # rows and in-grid overlays can change without changing the native window.
        current = find_current()
        require(uia.same(current, cell) and uia.bounds(current) == box and current.property(30010) is True,
                'result_cell_changed')
        require(point_for(current, box) == point, 'result_cell_changed')
        chain = lookup.ancestry(uia, current, root, pid=binding.pid)
        require(not any(peer.property(30003) in _ROW and peer.property(30022) is True for peer in chain),
                'result_cell_not_clickable')
        require(hit_at(current, point) == native_hit and win.root_window(win.foreground()) == binding.hwnd,
                'result_cell_changed')
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
                gesture='double-click', target_resolution=resolution, effect_verification='unverified')
