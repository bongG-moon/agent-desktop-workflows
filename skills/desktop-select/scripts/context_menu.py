"""Teach an interior point of a named surface and follow its newly opened menu."""
from collections import deque
from dataclasses import dataclass
from hashlib import sha256
from types import SimpleNamespace
import ctypes as C
from ctypes import wintypes as W
import math
import time

import win_bridge as win
import control_lookup as lookup
import coordination
import provider_visibility as visibility
from row_action import foreground


REGIONS = {'table', 'group', 'list', 'tree', 'custom', 'document'}
MENU = 50009
MENUITEM = 50011


class _GUI_THREAD_INFO(C.Structure):
    _fields_ = [('cbSize', W.DWORD), ('flags', W.DWORD),
                ('hwndActive', W.HWND), ('hwndFocus', W.HWND), ('hwndCapture', W.HWND),
                ('hwndMenuOwner', W.HWND), ('hwndMoveSize', W.HWND), ('hwndCaret', W.HWND),
                ('rcCaret', win.RECT)]


def _thread_identity(hwnd):
    pid = W.DWORD()
    thread = win._sig(win.user32, 'GetWindowThreadProcessId', W.DWORD, W.HWND, C.POINTER(W.DWORD))(
        hwnd, C.byref(pid))
    return int(thread or 0), pid.value


def _native_menu_owner(binding):
    """Prove a native menu loop belongs to this exact bound business window."""
    binding.check()
    if type(binding.hwnd) is not int or type(binding.pid) is not int:
        return None
    thread, pid = _thread_identity(binding.hwnd)
    if not thread or pid != binding.pid:
        return None
    state = _GUI_THREAD_INFO()
    state.cbSize = C.sizeof(state)
    read = win._sig(win.user32, 'GetGUIThreadInfo', W.BOOL, W.DWORD, C.POINTER(_GUI_THREAD_INFO))
    if not read(thread, C.byref(state)):
        return None
    owner = win.normalize_hwnd(state.hwndMenuOwner)
    # GUI_INMENUMODE / GUI_POPUPMENUMODE. A stale nonzero HWND is insufficient.
    if (not state.flags & (0x4 | 0x10) or not owner or not binding.contains_window(owner)
            or _thread_identity(owner) != (thread, binding.pid)):
        return None
    return thread, owner


def _scoped_windows(binding):
    """Include ownerless #32768 only during the proven target thread's menu loop."""
    windows = list(binding.scoped_windows())
    state = _native_menu_owner(binding)
    if state is None:
        return windows
    found = []
    callback_type = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    def visit(hwnd, _):
        hwnd = win.normalize_hwnd(hwnd)
        if (hwnd and win.user32.IsWindowVisible(hwnd) and win.window_class(hwnd) == '#32768'
                and win.root_window(hwnd) == hwnd
                and _thread_identity(hwnd) == (state[0], binding.pid)):
            found.append(hwnd)
        return len(found) < 64
    complete = win._sig(win.user32, 'EnumThreadWindows', W.BOOL, W.DWORD, callback_type, W.LPARAM)(
        state[0], callback_type(visit), 0)
    _need(complete and len(found) < 64, 'context_menu_scope_incomplete')
    _need(_native_menu_owner(binding) == state, 'context_menu_owner_changed')
    return list(dict.fromkeys([*windows, *found]))


def _contains_window(binding, hwnd):
    if binding.contains_window(hwnd):
        return True
    return bool(hwnd and win.root_window(hwnd) in _scoped_windows(binding))


def _need(condition, code):
    if not condition:
        raise win.BridgeError(code)


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= 512 and value == value.strip() and all(
        ord(c) >= 32 and not 0xD800 <= ord(c) <= 0xDFFF for c in value)


def validate_spec(spec):
    _need(isinstance(spec, dict) and {'target', 'point', 'path'} <= set(spec)
          and not set(spec) - {'target', 'point', 'path', 'point_policy', 'visual_patch'}, 'invalid_context_spec')
    if 'point_policy' in spec:
        _need(spec['point_policy'] == 'any_data_cell', 'invalid_context_point_policy')
    if 'visual_patch' in spec:
        digest = spec['visual_patch']
        _need(isinstance(digest, str) and len(digest) == 64 and all(c in '0123456789abcdef' for c in digest),
              'invalid_context_visual_patch')
    _validate_surface(spec['target'], spec['point'])
    path = spec['path']
    _need(isinstance(path, list) and 1 <= len(path) <= 8 and all(_text(v) for v in path),
          'invalid_context_menu_path')


def _validate_surface(target, point):
    _need(isinstance(target, dict) and set(target) in ({'role','name'}, {'role','automation_id'})
          and isinstance(target['role'], str) and target['role'] in REGIONS
          and _text(target.get('name', target.get('automation_id')))
          and not str(target.get('automation_id', '')).isdecimal(), 'invalid_context_target')
    _need(isinstance(point, list) and len(point) == 2 and all(
        type(v) in (float, int) and .02 <= v <= .98 and math.isfinite(v) for v in point),
        'context_point_near_boundary')


def _rect(uia, element):
    r = uia.bounds(element)
    return r['x'], r['y'], r['width'], r['height']


def _inside(point, rect):
    x, y, w, h = rect
    return x <= point[0] < x + w and y <= point[1] < y + h


def _region(binding, uia, target):
    # Resolve only the demonstrated identity. The native lookup retains all
    # matches and proves the entire owned, unprotected ancestor chain.
    try:
        element = lookup.resolve(binding, uia, target)
    except win.BridgeError as error:
        code = {'target_missing': 'context_region_missing',
                'target_ambiguous': 'context_region_ambiguous'}.get(error.code, error.code)
        raise win.BridgeError(code) from error
    root = uia.from_handle(binding.hwnd)
    _need(not uia.same(element, root), 'context_region_missing')
    _need(element.property(30010) is True and visibility.visible(binding, uia, element, root),
          'context_region_unavailable')
    return element


def _menu_visible(binding, uia, item):
    # Keep the normal provider path unchanged. Unknown visibility additionally
    # requires current geometry and containment in a visible owned surface.
    state = item.property(30022)
    if state is not None:
        return state is False
    if visibility.visible(binding, uia, item):
        return True
    # Native #32768 popups do not necessarily have a Win32 owner chain, so
    # the bridge's normal visibility helper cannot find their surface. Use a
    # temporary read-only surface after the active menu-owner proof above.
    for hwnd in _scoped_windows(binding):
        if binding.contains_window(hwnd):
            continue
        root = uia.from_handle(hwnd)
        surface = SimpleNamespace(hwnd=hwnd, pid=binding.pid, check=binding.check)
        if visibility.visible(surface, uia, item, root) and hwnd in _scoped_windows(binding):
            return True
    return False


_NON_DATA_CONTROLS = frozenset({50000, 50002, 50003, 50004, 50005, 50009, 50010, 50011, 50014, 50015,
                                50016, 50017, 50018, 50019, 50021, 50022, 50027, 50031, 50034, 50035,
                                50037, 50038})


def _transparent_data_ancestor(uia, element, hit):
    # A Tab control wraps its active content as well as its selectable tabs.
    # Only the already-proven hit's ancestor is transparent. A tab header,
    # direct Tab hit or overlapping sibling keeps the non-data classification.
    return (hit is not None and element.property(30003) == 50018 and not uia.same(element, hit)
            and uia.within(hit, element))


def _check_region_children(binding, uia, region, point, *, data_only=False, hit_element=None):
    # Blank/custom peers can answer ElementFromPoint with the parent even
    # when a separate protected editor is drawn over part of the surface.
    pending = deque([(region, 0)])
    visits = 0
    while pending:
        element, depth = pending.popleft()
        for child in uia.children(element):
            visits += 1
            _need(visits <= 1500 and depth < 20, 'context_region_tree_incomplete')
            rect = _rect(uia, child)
            visible = child.property(30022) is not True
            unsafe = uia.pid(child) != binding.pid or uia.secure(child)
            if unsafe:
                _need(not _inside(point, rect) and (not visible or (rect[2] > 0 and rect[3] > 0)),
                      'context_point_protected')
                continue
            if _inside(point, rect) and visible:
                _need(child.property(30010) is True, 'context_point_disabled')
                if data_only:
                    _need(child.property(30003) not in _NON_DATA_CONTROLS
                          or _transparent_data_ancestor(uia, child, hit_element), 'context_point_not_data')
            # Providers sometimes give wrappers no bounds, so do not prune a
            # branch merely because the wrapper did not contain the point.
            pending.append((child, depth + 1))


def _hit_region(binding, uia, region, point, *, require_body=False, recorded_opaque=False):
    hit = win.window_from_point(*point)
    _need(hit and win.root_window(hit) == binding.hwnd and win.hwnd_pid(hit) == binding.pid,
          'context_point_occluded')
    element = uia.from_point(*point)
    _need(element is not None and uia.pid(element) == binding.pid, 'context_point_unproven')
    if not uia.within(element, region):
        _need(uia.within(region, element), 'context_point_outside_region')
        element = uia.point_descendant(element, *point, binding.pid)
    hit_element = element
    body, opaque = False, True
    for _ in range(32):
        if element is None:
            break
        _need(uia.pid(element) == binding.pid and not uia.secure(element)
              and element.property(30010) is True, 'context_point_protected')
        if uia.same(element, region):
            _need(_inside(point, _rect(uia, region)), 'context_point_outside_region')
            _need(not require_body or body or (recorded_opaque and opaque), 'context_data_cell_unconfirmed')
            _check_region_children(binding, uia, region, point, data_only=require_body, hit_element=hit_element)
            _need(win.window_from_point(*point) == hit, 'context_point_occluded')
            return
        if require_body:
            role = element.property(30003)
            # A row may contain a command button, editor or trailing scroll bar.
            # Its row ancestor alone does not turn an interactive control into data.
            transparent = _transparent_data_ancestor(uia, element, hit_element)
            _need(role in (50006, 50007, 50020, 50025, 50026, 50028, 50029, 50033, 50036) or transparent,
                  'context_point_not_data')
            body |= role in (50007, 50029) or element.property(30029) is True
            opaque &= role in _PROXY_ROLES or transparent
        element = uia.parent(element)
    raise win.BridgeError('context_point_outside_region')


_UNPROVEN_POINT = frozenset({'context_point_outside_region', 'context_point_unproven',
                              'context_point_not_data', 'context_data_cell_unconfirmed'})
_PROXY_ROLES = frozenset({50008, 50025, 50026, 50028, 50030, 50032, 50033, 50036})


def _visual_point(binding, uia, region, point):
    """Prove that an opaque peer at this pixel belongs to the visible target surface."""
    hit = win.window_from_point(*point)
    _need(hit and win.root_window(hit) == binding.hwnd and win.hwnd_pid(hit) == binding.pid,
          'context_point_occluded')
    element = uia.from_point(*point)
    _need(element is not None and _inside(point, _rect(uia, region)), 'context_point_outside_region')
    root = uia.from_handle(binding.hwnd)
    chain = lookup.ancestry(uia, element, root, pid=binding.pid)
    _need(all(node.property(30010) is True for node in chain), 'context_point_disabled')
    _need(element.property(30003) in _PROXY_ROLES, 'context_point_outside_region')
    # If UIA hit the region itself, its owned ancestry was just proven. Only
    # that subtree can hide an embedded command/editor under this surface.
    # A broader proxy hit is rarer and requires checking competing branches.
    direct_hit = uia.within(element, region)
    alias = False
    if not direct_hit and not uia.within(region, element):
        # Some providers expose two peers for a single native child control.
        # Require the actual hit HWND, equal geometry and the same parent;
        # two unrelated panels in a process do not establish an alias.
        a, b = uia.parent(element), uia.parent(region)
        alias = (hit != binding.hwnd and win.normalize_hwnd(region.property(30020)) == hit
                 and win.normalize_hwnd(element.property(30020)) == hit
                 and _rect(uia, element) == _rect(uia, region) and a is not None and b is not None
                 and uia.same(a, b))
        _need(alias, 'context_point_outside_region')
    pending, visited = deque([region if direct_hit else root]), 0
    while pending:
        node = pending.popleft()
        visited += 1
        _need(visited <= 2500, 'context_region_tree_incomplete')
        box = _rect(uia, node)
        if uia.pid(node) != binding.pid or uia.secure(node):
            _need(node.property(30022) is True or (box[2] > 0 and box[3] > 0 and not _inside(point, box)),
                  'context_point_protected')
            continue
        if node.property(30022) is not True and _inside(point, box):
            _need(node.property(30010) is True, 'context_point_disabled')
            ancestor = uia.within(region, node)
            _need(ancestor or uia.within(node, region) or (alias and uia.within(node, element)),
                  'context_point_occluded')
            if not ancestor:
                _need(node.property(30003) in _PROXY_ROLES | {50006, 50007, 50020, 50029}
                      or _transparent_data_ancestor(uia, node, element),
                      'context_point_not_data')
        pending.extend(uia.children(node))
    _need(win.window_from_point(*point) == hit, 'context_point_occluded')


def _patch_digest(window, png, point, rect):
    import grounding
    box, pixels = grounding._pixels(png)
    x, y = point
    radius = 12
    corners = [(x-radius, y-radius), (x+radius, y+radius)]
    _need(list(window[2:]) == box[2:] and all(_inside(c, rect) and _inside(c, window) for c in corners),
          'context_patch_outside_region')
    x, y = x-window[0], y-window[1]
    width = window[2]
    patch = b''.join(pixels[4*((y+dy)*width + x-radius):4*((y+dy)*width + x+radius+1)]
                     for dy in range(-radius, radius+1))
    _need(any(max(patch[channel::4])-min(patch[channel::4]) >= 20 for channel in range(3)),
          'context_visual_patch_blank')
    return sha256(patch).hexdigest()


def adjust_from_frame(binding, uia, spec, screen_point, *, window, png):
    """Use a point chosen in an approved, freshly matched frame of this exact window."""
    validate_spec(spec)
    _need(len(screen_point) == 2 and all(type(v) is int for v in screen_point), 'invalid_context_point')
    region = _region(binding, uia, spec['target'])
    x, y, w, h = rect = _rect(uia, region)
    _need(w > 0 and h > 0, 'context_region_unavailable')
    adjusted = dict(spec, point=[(screen_point[0]-x)/w, (screen_point[1]-y)/h])
    adjusted.pop('visual_patch', None)
    _validate_surface(spec['target'], adjusted['point'])
    try:
        # The host just selected this point in a fresh image, so a directly
        # hit, opaque grid can use this point just as the original recording did.
        _hit_region(binding, uia, region, screen_point,
                    require_body='point_policy' in spec, recorded_opaque=True)
    except win.BridgeError as error:
        if error.code not in _UNPROVEN_POINT:
            raise
        _visual_point(binding, uia, region, screen_point)
        adjusted['visual_patch'] = _patch_digest(window, png, screen_point, rect)
    return adjusted


def _verify_menu_point(binding, uia, region, point, spec, *, allow_capture, recorded_opaque=True):
    if 'visual_patch' not in spec:
        return _hit_region(binding, uia, region, point, require_body='point_policy' in spec,
                           recorded_opaque=recorded_opaque)
    _need(allow_capture, 'context_visual_check_deferred')
    import desktop_select as app
    import grounding
    hit = win.window_from_point(*point)
    _need(hit and win.root_window(hit) == binding.hwnd and win.hwnd_pid(hit) == binding.pid,
          'context_point_occluded')
    window, png = grounding._capture(app, binding, binding.check)
    _need(_patch_digest(window, png, point, _rect(uia, region)) == spec['visual_patch'],
          'context_visual_patch_changed')
    _visual_point(binding, uia, region, point)


def _resolve_menu_point(binding, uia, region, spec, *, allow_capture, defer_visual=False):
    """Try the stored point first. Only a table-wide intent permits another data cell."""
    x, y, w, h = rect = _rect(uia, region)
    _need(type(w) in (int, float) and type(h) in (int, float) and w > 0 and h > 0,
          'context_region_unavailable')
    px, py = spec['point']
    screen = (x + round(px*w), y + round(py*h))
    if defer_visual and 'visual_patch' in spec:
        # The final guarded check immediately before sending already validates
        # the current image. Do not capture the same window twice per click.
        return rect, screen, None
    try:
        _verify_menu_point(binding, uia, region, screen, spec, allow_capture=allow_capture)
        return rect, screen, None
    except win.BridgeError as error:
        if (error.code not in _UNPROVEN_POINT or 'visual_patch' in spec
                or spec.get('point_policy') != 'any_data_cell'):
            raise
    # These are input-free hit tests, restricted to the same named surface.
    # Never turn a coordinate or an empty group into evidence of a data row.
    candidates = {(cx, cy) for cx in (px, .25, .5, .75) for cy in (py, .25, .5, .75)}
    candidates.discard((px, py))
    deadline = time.monotonic() + .45
    for cx, cy in sorted(candidates, key=lambda c: (((c[0]-px)*w)**2 + ((c[1]-py)*h)**2, c)):
        candidate = x+round(cx*w), y+round(cy*h)
        try:
            _hit_region(binding, uia, region, candidate, require_body=True)
        except win.BridgeError as error:
            if error.code not in _UNPROVEN_POINT:
                raise
            if time.monotonic() >= deadline:
                break
            continue
        return rect, candidate, {'from': list(spec['point']), 'to': [cx, cy]}
    raise win.BridgeError('context_data_cell_unconfirmed')


def observe(binding, uia, spec):
    """Preview the same point resolver used immediately before dispatch, without taking input or a picture."""
    validate_spec(spec)
    region = _region(binding, uia, spec['target'])
    receipt = dict(status='observed', input_dispatched=False, region_observed=True,
                   point_observed=False, menu_verification='pending_execution')
    try:
        _, _, correction = _resolve_menu_point(binding, uia, region, spec, allow_capture=False)
    except win.BridgeError as error:
        receipt['point_diagnostic'] = error.code
    else:
        receipt['point_observed'] = True
        if correction:
            receipt['proposed_point'] = correction['to']
    return receipt


def _surface_target(binding, uia, element, *, target=None, role=None):
    info = uia.basic(element)
    if info['role'] not in REGIONS or role not in (None, info['role']):
        return None
    for key in ('automation_id', 'name'):
        value = info.get(key)
        if not _text(value) or target not in (None, value) or (key == 'automation_id' and value.isdecimal()):
            continue
        locator = {'role': info['role'], key: value}
        try:
            region = _region(binding, uia, locator)
        except win.BridgeError as error:
            # The recorded point may be in a repeated child group of a named
            # table. An explicitly requested region must still be unique.
            if target is None and error.code == 'context_region_ambiguous':
                continue
            raise
        _need(uia.same(element, region), 'context_region_changed')
        return locator
    return None


def _surface_candidates(binding, uia, element, root, *, target=None, role=None):
    """Rank the durable containing regions of an already scoped native/UIA hit."""
    _need(element is not None and root is not None and uia.within(element, root), 'context_point_occluded')
    candidates = []
    window_box = uia.bounds(root)
    for depth in range(32):
        if element is None or uia.same(element, root):
            break
        _need(uia.pid(element) == binding.pid and not uia.secure(element), 'context_point_protected')
        info = uia.basic(element)
        if info['role'] in REGIONS:
            # Some grids label the current row as a group. The containing
            # table, stable control ID or outer surface survives value changes.
            identifier = info.get('automation_id')
            rank = (0 if info['role'] in ('table', 'list', 'tree') else
                    1 if _text(identifier) and not identifier.isdecimal() else 2)
            box = uia.bounds(element)
            shell = (target is None and role is None and rank > 0 and visibility.valid(box)
                     and visibility.valid(window_box) and
                     box['width'] >= .9 * window_box['width'] and
                     box['height'] >= .75 * window_box['height'])
            if not shell:
                candidates.append((rank, -depth if rank == 2 else depth, element))
        element = uia.parent(element)
    return [item for _, _, item in sorted(candidates, key=lambda row: row[:2])]


def teach_surface(binding, screen_point, *, target=None, role=None):
    """Observe the named region and local point before a context menu opens."""
    binding.check()
    _need(isinstance(screen_point, (tuple,list)) and len(screen_point) == 2 and
          all(type(v) is int for v in screen_point), 'invalid_context_point')
    with win.UIA() as uia:
        element = uia.from_point(*screen_point)
        root = uia.from_handle(binding.hwnd)
        _need(element is not None and root is not None and uia.within(element, root), 'context_point_occluded')
        element = uia.point_descendant(element, *screen_point, binding.pid)
        for element in _surface_candidates(binding, uia, element, root, target=target, role=role):
            locator = _surface_target(binding, uia, element, target=target, role=role)
            if locator:
                try:
                    _hit_region(binding, uia, element, screen_point)
                except win.BridgeError as error:
                    if target is None and error.code == 'context_region_tree_incomplete':
                        continue
                    raise
                x, y, w, h = _rect(uia, element)
                ratios = [(screen_point[0]-x)/w, (screen_point[1]-y)/h]
                _validate_surface(locator, ratios)
                return {'target':locator, 'point':ratios}
    raise win.BridgeError('named_context_region_required')


def teach_at(binding, screen_point, menu_path, *, target=None, role=None):
    """Bind a supplied menu path to a region observed in the live window."""
    _need(isinstance(menu_path, (tuple, list)), 'invalid_context_menu_path')
    spec = dict(teach_surface(binding, screen_point, target=target, role=role), path=list(menu_path))
    validate_spec(spec)
    return spec


@dataclass
class _Item:
    surface: int
    element: object
    menu: object
    menu_box: tuple
    box: tuple
    name: str

    @property
    def center(self):
        x, y, w, h = self.box
        return x + w//2, y + h//2


def _menu_caption(binding, uia, item, box):
    name = item.property(30005)
    if _text(name):
        return name
    legacy = item.property(30092)  # LegacyIAccessible.Name
    captions = {legacy} if _text(legacy) else set()
    for number, child in enumerate(uia.children(item), 1):
        if number > 64:
            return None
        if (uia.pid(child) != binding.pid or uia.secure(child) or child.property(30003) != 50020
                or not _menu_visible(binding, uia, child)):
            continue
        x, y, w, h = _rect(uia, child)
        if w > 0 and h > 0 and _inside((x, y), box) and _inside((x+w-1, y+h-1), box):
            label = child.property(30005)
            if _text(label):
                captions.add(label)
    return next(iter(captions)) if len(captions) == 1 else None


def _menus(binding, uia):
    """Only visible menu items in the selected window or its owned popups."""
    items = []
    count = 0
    def append(hwnd, item, frame):
        if frame is None or not _menu_visible(binding, uia, item):
            return
        rect = _rect(uia, item)
        if rect[2] > 1 and rect[3] > 1:
            name = _menu_caption(binding, uia, item, rect)
            if _text(name) and not any(uia.same(item, prior.element) for prior in items):
                items.append(_Item(hwnd, item, frame, _rect(uia, frame), rect, name))
    # Some trees expose a popup both on its own HWND and below the host.
    # Prefer the popup root so the final hit test has its actual window.
    scopes = _scoped_windows(binding)
    native_scopes = {hwnd for hwnd in scopes if hwnd != binding.hwnd and win.window_class(hwnd) == '#32768'}
    if native_scopes and _native_menu_owner(binding) is None:
        native_scopes = set()
    for hwnd in sorted(scopes, key=lambda h: h == binding.hwnd):
        if hwnd == binding.hwnd and any(item.surface in native_scopes for item in items):
            # A proven native menu already contains the actual visible
            # commands. A whole-window FindAll adds unrelated application
            # menus and can outlive a short mouse gesture on large SWT trees.
            continue
        root = uia.from_handle(hwnd)
        _need(root is not None and uia.pid(root) == binding.pid, 'context_menu_scope_unavailable')
        if hwnd == binding.hwnd:
            if uia.secure(root):
                continue
            # The main window may contain thousands of unrelated grid cells.
            # Ask its provider only for menu items, retaining all candidates
            # for ambiguity checks. find_all proves raw owned/unprotected
            # ancestry before any candidate name is read.
            candidates = lookup.find_all(uia, root, roles=('menuitem',), limit=4096)
            count += len(candidates)
            _need(count <= 4200, 'context_menu_tree_incomplete')
            for item in candidates:
                current, frame, classified, complete = item, None, False, False
                # Recover the original ControlView MENU / MENUBAR frame.
                # Continue to the host after finding it so a disappearing or
                # reparented peer cannot silently broaden the menu scope.
                for depth in range(29):
                    _need(current is not None and uia.pid(current) == binding.pid
                          and not uia.secure(current), 'context_menu_scope_unavailable')
                    kind = current.property(30003)
                    if depth == 0:
                        _need(kind == MENUITEM, 'context_menu_item_changed')
                    elif not classified and kind in (MENU, 50010):
                        frame, classified = (current if kind == MENU else None), True
                    if uia.same(current, root):
                        complete = True
                        break
                    current = uia.parent(current)
                _need(complete, 'context_menu_tree_incomplete')
                append(hwnd, item, frame)
            continue
        # Some providers expose visible items directly below an owned popup.
        default_frame = root if hwnd != binding.hwnd else None
        pending = deque([(root, default_frame, 0)])
        while pending:
            item, frame, depth = pending.popleft()
            count += 1
            _need(count <= 4200 and depth <= 28, 'context_menu_tree_incomplete')
            if uia.pid(item) != binding.pid or uia.secure(item):
                continue
            kind = item.property(30003)
            if kind == MENU:
                frame = item
            elif kind == 50010:  # MenuBar routes normal host commands.
                frame = None
            if kind == MENUITEM:
                append(hwnd, item, frame)
            pending.extend((c, frame, depth+1) for c in uia.children(item))
    return items


def _near(anchor, frame, gap):
    # A context menu may flip to the other side at a monitor boundary.
    x, y, w, h = frame
    if w <= 0 or h <= 0:
        return False
    ax, ay, aw, ah = anchor
    left_or_right_edge = min(abs(x-ax), abs(x-(ax+aw)), abs(x+w-ax), abs(x+w-(ax+aw))) <= gap
    return left_or_right_edge and y <= ay+ah+gap and ay <= y+h+gap


def _unresolved_menu(binding, uia, remaining, previous, anchor, gap):
    """Provide a read-only handoff for an actual menu left open after this dispatch."""
    detail = dict(host_action='inspect_current_menu_state', remaining_items=list(remaining),
                  repeat_right_click=False)
    try:
        matches = []
        for hwnd in _scoped_windows(binding):
            if hwnd in previous or hwnd == binding.hwnd:
                continue
            root = uia.from_handle(hwnd)
            if root is None or uia.pid(root) != binding.pid or uia.secure(root):
                continue
            if (root.property(30003) != MENU and win.window_class(hwnd) != '#32768'):
                continue
            box = _rect(uia, root)
            if _near(anchor, box, gap) and win.user32.IsWindowVisible(hwnd):
                matches.append(dict(hwnd=hwnd, bounds=list(box)))
        if len(matches) == 1:
            detail.update(host_action='inspect_open_menu', observed_popup=matches[0])
    except (OSError, win.BridgeError):
        pass  # A transient popup can disappear during this final read.
    return detail


def _choose(uia, items, old, name, anchor, parent, gap):
    candidates = []
    for item in items:
        if item.name.casefold() != name.casefold():
            continue
        nested = parent is not None and uia.within(item.element, parent.element)
        # Embedded menus must be newly shown; an existing toolbar command or
        # a previously visible context menu cannot authorize this transition.
        if any(uia.same(item.element, previous.element) or
               (item.surface == previous.surface and item.box == previous.box
                and item.name.casefold() == previous.name.casefold()) for previous in old):
            continue
        if not nested and any(uia.same(item.menu, previous.menu) for previous in old):
            continue
        if nested or _near(anchor, item.menu_box, gap):
            candidates.append(item)
    _need(len(candidates) <= 1, 'context_menu_item_ambiguous')
    return candidates[0] if candidates else None


def _hit_item(binding, uia, item, parent, prior_surfaces, gap):
    center = item.center
    hit = win.window_from_point(*center)
    _need(_contains_window(binding, hit) and _contains_window(binding, win.foreground()),
          'context_menu_occluded')
    _need(item.element.property(30010) is True and _menu_visible(binding, uia, item.element)
          and _rect(uia, item.element) == item.box, 'context_menu_item_changed')
    actual = uia.from_point(*center)
    _need(actual is not None and uia.pid(actual) == binding.pid and not uia.secure(actual),
          'context_menu_hit_unproven')
    surface = win.root_window(hit)
    if surface != item.surface:
        top = uia.from_handle(surface)
        # An owned tooltip/dialog may cover this very pixel. Ownership
        # alone does not identify it as the demonstrated menu.
        _need(top is not None and surface != binding.hwnd, 'context_menu_occluded')
        if not uia.within(item.element, top):
            # A ToolStrip submenu can expose an empty menu HWND, with its
            # actual items parented to the opener's accessibility peer.
            # Prove both halves: fresh adjacent popup AND logical parent.
            _need(parent is not None and surface not in prior_surfaces
                  and top.property(30003) == MENU and actual.property(30003) == MENU
                  and uia.within(actual, top) and uia.within(item.element, parent.element),
                  'context_menu_occluded')
            popup_box = _rect(uia, top)
            x, y, w, h = item.box
            _need(_inside((x, y), popup_box) and _inside((x+w-1, y+h-1), popup_box)
                  and _near(parent.box, popup_box, gap), 'context_menu_occluded')
    if uia.within(actual, item.element):
        _need(actual.property(30010) is True, 'context_menu_hit_disabled')
    else:
        # A ToolStrip can expose just the menu container to ElementFromPoint.
        _need(actual.property(30003) in (MENU, 50010, 50026, 50032), 'context_menu_hit_unproven')
        actual = uia.point_descendant(actual, *center, binding.pid)
        if not uia.within(actual, item.element):
            # Managed submenu peers sometimes live only under the parent popup.
            observed = uia.menu_observation(binding, *center, hit)
            _need(observed is not None and uia.same(observed['element'], item.element)
                  and win.root_window(hit) != binding.hwnd, 'context_menu_hit_unproven')
    _need(win.window_from_point(*center) == hit and _contains_window(binding, hit), 'context_menu_occluded')
    return surface


def execute(binding, spec, guard, *, before_leaf=None):
    """Deliver each event once and distinguish leaf delivery from output verification."""
    sent, pending, leaf_sent, completed = False, False, False, []
    unresolved = None
    try:
        validate_spec(spec)
        # Keep the foreground, right-click and all submenu selections together.
        with coordination.desktop_input(), win.UIA() as uia:
            guard()
            foreground(binding)
            tick = win.input_tick()
            # A delayed query can populate the result surface shortly after the
            # button returns. This short wait only re-observes; no query is sent.
            ready_until = time.monotonic() + .9
            while True:
                try:
                    region = _region(binding, uia, spec['target'])
                    rect, (px, py), correction = _resolve_menu_point(binding, uia, region, spec, allow_capture=True,
                                                                    defer_visual=True)
                    break
                except win.BridgeError as error:
                    if (spec.get('point_policy') != 'any_data_cell' or time.monotonic() >= ready_until
                            or error.code not in ('context_data_cell_unconfirmed', 'context_region_missing',
                                                  'context_region_unavailable')):
                        raise
                    guard()
                    _need(win.input_tick() == tick, 'foreground_or_input_changed')
                    time.sleep(.12)
            before = _menus(binding, uia)
            prior_surfaces = set(_scoped_windows(binding))
            guard()
            current = _region(binding, uia, spec['target'])
            _need(uia.same(current, region) and _rect(uia, current) == rect, 'context_region_changed')
            _verify_menu_point(binding, uia, current, (px, py), spec, allow_capture=True,
                               recorded_opaque=correction is None)
            win._combo_surface(binding)
            _need(_rect(uia, current) == rect and
                  win.root_window(win.window_from_point(px, py)) == binding.hwnd, 'context_region_changed')
            _need(win.input_tick() == tick, 'foreground_or_input_changed')
            pending = True
            output = win._send_point(px, py, 'right-click')
            _need(output.get('ok') and output.get('input_dispatched') is True, 'context_click_uncertain')
            sent, pending = True, False
            parent, anchor = None, (px, py, 1, 1)
            gap = max(6, round(12*binding.info()['dpi']/96))
            for number, name in enumerate(spec['path']):
                tick = win.input_tick()
                deadline = time.monotonic() + 3
                while True:
                    guard()
                    snapshot = _menus(binding, uia)
                    choice = _choose(uia, snapshot, before, name, anchor, parent, gap)
                    if choice is not None:
                        break
                    if time.monotonic() >= deadline:
                        unresolved = _unresolved_menu(binding, uia, spec['path'][number:], prior_surfaces, anchor, gap)
                        raise win.BridgeError('new_context_menu_item_missing')
                    time.sleep(.06)
                leaf = number == len(spec['path'])-1
                guard()
                snapshot = _menus(binding, uia)
                fresh = _choose(uia, snapshot, before, name, anchor, parent, gap)
                _need(fresh is not None and uia.same(fresh.element, choice.element)
                      and fresh.box == choice.box, 'context_menu_item_changed')
                if leaf and before_leaf is not None:
                    before_leaf()
                    guard()
                exposed = _hit_item(binding, uia, fresh, parent, prior_surfaces, gap)
                win._combo_surface(binding)
                prior_surfaces = set(_scoped_windows(binding))
                _need(exposed in prior_surfaces and win.root_window(win.window_from_point(*fresh.center)) == exposed,
                      'context_menu_occluded')
                _need(win.input_tick() == tick, 'foreground_or_input_changed')
                pending = True
                if leaf:
                    leaf_sent = None
                output = win._send_point(*fresh.center, 'click')
                _need(output.get('ok') and output.get('input_dispatched') is True, 'context_menu_delivery_uncertain')
                pending = False
                if leaf:
                    leaf_sent = True
                completed.append(name)
                parent, anchor, before = fresh, fresh.box, snapshot
        receipt = dict(status='delivered_unverified', diagnostic='menu_leaf_sent',
                    input_dispatched=True, leaf_dispatched=True, menu_path_completed=True,
                    menu_items=completed, effect_verification='unverified',
                    input_mode='physical_pointer', physical_input=True)
        if correction:
            receipt['point_correction'] = correction
        return receipt
    except Exception as error:
        receipt = dict(status='uncertain' if pending else 'blocked', diagnostic=getattr(error,'code','context_provider_error'),
                    input_dispatched=None if pending and not sent else sent, leaf_dispatched=leaf_sent,
                    menu_path_completed=False, menu_items=completed, effect_verification='unverified',
                    input_mode='physical_pointer', physical_input=True)
        if unresolved:
            receipt['menu_recovery'] = unresolved
        return receipt
