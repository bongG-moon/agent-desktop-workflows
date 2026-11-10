"""Teach an interior point of a named surface and follow its newly opened menu."""
from collections import deque
from dataclasses import dataclass
import math
import time

import win_bridge as win
from row_action import foreground


REGIONS = {'table', 'group', 'list', 'tree', 'custom', 'document'}
MENU = 50009
MENUITEM = 50011


def _need(condition, code):
    if not condition:
        raise win.BridgeError(code)


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= 512 and value == value.strip() and all(
        ord(c) >= 32 and not 0xD800 <= ord(c) <= 0xDFFF for c in value)


def validate_spec(spec):
    _need(isinstance(spec, dict) and set(spec) == {'target', 'point', 'path'}, 'invalid_context_spec')
    target = spec['target']
    _need(isinstance(target, dict) and set(target) in ({'role','name'}, {'role','automation_id'})
          and isinstance(target['role'], str) and target['role'] in REGIONS
          and _text(target.get('name', target.get('automation_id')))
          and not str(target.get('automation_id', '')).isdecimal(), 'invalid_context_target')
    point = spec['point']
    _need(isinstance(point, list) and len(point) == 2 and all(
        type(v) in (float, int) and .02 <= v <= .98 and math.isfinite(v) for v in point),
        'context_point_near_boundary')
    path = spec['path']
    _need(isinstance(path, list) and 1 <= len(path) <= 8 and all(_text(v) for v in path),
          'invalid_context_menu_path')


def _rect(uia, element):
    r = uia.bounds(element)
    return r['x'], r['y'], r['width'], r['height']


def _inside(point, rect):
    x, y, w, h = rect
    return x <= point[0] < x + w and y <= point[1] < y + h


def _region(binding, uia, target):
    # The standard observer skips protected descendants and validates the PID.
    from desktop_select import observe
    nodes, elements = observe(binding, uia, identities_only=True)
    hits = [e for n, e in zip(nodes[1:], elements[1:])
            if all(n.get(k) == v for k, v in target.items())]
    _need(len(hits) == 1, 'context_region_ambiguous' if hits else 'context_region_missing')
    element = hits[0]
    _need(element.property(30010) is True and element.property(30022) is False, 'context_region_unavailable')
    return element


def _check_region_children(binding, uia, region, point):
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
            # Providers sometimes give wrappers no bounds, so do not prune a
            # branch merely because the wrapper did not contain the point.
            pending.append((child, depth + 1))


def _hit_region(binding, uia, region, point):
    hit = win.window_from_point(*point)
    _need(hit and win.root_window(hit) == binding.hwnd and win.hwnd_pid(hit) == binding.pid,
          'context_point_occluded')
    element = uia.from_point(*point)
    _need(element is not None and uia.pid(element) == binding.pid, 'context_point_unproven')
    if not uia.within(element, region):
        _need(uia.within(region, element), 'context_point_outside_region')
        element = uia.point_descendant(element, *point, binding.pid)
    for _ in range(32):
        if element is None:
            break
        _need(uia.pid(element) == binding.pid and not uia.secure(element)
              and element.property(30010) is True, 'context_point_protected')
        if uia.same(element, region):
            _need(_inside(point, _rect(uia, region)), 'context_point_outside_region')
            _check_region_children(binding, uia, region, point)
            _need(win.window_from_point(*point) == hit, 'context_point_occluded')
            return
        element = uia.parent(element)
    raise win.BridgeError('context_point_outside_region')


def teach_at(binding, screen_point, menu_path, *, target=None, role=None):
    """Return the reviewed region's identity and relative point. Sends no input."""
    binding.check()
    _need(isinstance(screen_point, (tuple,list)) and len(screen_point) == 2 and
          all(type(v) is int for v in screen_point), 'invalid_context_point')
    _need(isinstance(menu_path, (tuple, list)), 'invalid_context_menu_path')
    with win.UIA() as uia:
        element = uia.from_point(*screen_point)
        root = uia.from_handle(binding.hwnd)
        _need(element is not None and root is not None and uia.within(element, root), 'context_point_occluded')
        element = uia.point_descendant(element, *screen_point, binding.pid)
        for _ in range(32):
            if element is None or uia.same(element, root):
                break
            _need(uia.pid(element) == binding.pid and not uia.secure(element), 'context_point_protected')
            info = uia.basic(element)
            if info['role'] in REGIONS and role in (None, info['role']):
                key = next((k for k in ('automation_id','name') if _text(info.get(k))
                            and target in (None, info[k])), None)
                if key:
                    locator = {'role':info['role'], key:info[key]}
                    region = _region(binding, uia, locator)
                    _need(uia.same(element, region), 'context_region_changed')
                    _hit_region(binding, uia, region, screen_point)
                    x, y, w, h = _rect(uia, region)
                    ratios = [(screen_point[0]-x)/w, (screen_point[1]-y)/h]
                    spec = {'target':locator, 'point':ratios, 'path':list(menu_path)}
                    validate_spec(spec)
                    return spec
            element = uia.parent(element)
    raise win.BridgeError('named_context_region_required')


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


def _menus(binding, uia):
    """Only visible menu items in the selected window or its owned popups."""
    items = []
    count = 0
    # Some trees expose a popup both on its own HWND and below the host.
    # Prefer the popup root so the final hit test has its actual window.
    for hwnd in sorted(binding.scoped_windows(), key=lambda h: h == binding.hwnd):
        root = uia.from_handle(hwnd)
        _need(root is not None and uia.pid(root) == binding.pid, 'context_menu_scope_unavailable')
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
            if kind == MENUITEM and frame is not None and item.property(30022) is False:
                rect = _rect(uia, item)
                if rect[2] > 1 and rect[3] > 1:
                    name = item.property(30005)
                    if _text(name) and not any(uia.same(item, prior.element) for prior in items):
                        items.append(_Item(hwnd, item, frame, _rect(uia, frame), rect, name))
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
    _need(binding.contains_window(hit) and binding.contains_window(win.foreground()),
          'context_menu_occluded')
    _need(item.element.property(30010) is True and item.element.property(30022) is False
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
    _need(win.window_from_point(*center) == hit, 'context_menu_occluded')
    return surface


def execute(binding, spec, guard, *, before_leaf=None):
    """Deliver each event once and distinguish leaf delivery from output verification."""
    sent, pending, leaf_sent, completed = False, False, False, []
    try:
        validate_spec(spec)
        guard()
        foreground(binding)
        with win.UIA() as uia:
            region = _region(binding, uia, spec['target'])
            rect = _rect(uia, region)
            x, y, w, h = rect
            px, py = x+round(w*spec['point'][0]), y+round(h*spec['point'][1])
            _hit_region(binding, uia, region, (px, py))
            before = _menus(binding, uia)
            prior_surfaces = set(binding.scoped_windows())
            guard()
            current = _region(binding, uia, spec['target'])
            _need(uia.same(current, region) and _rect(uia, current) == rect, 'context_region_changed')
            _hit_region(binding, uia, current, (px, py))
            win._combo_surface(binding)
            _need(_rect(uia, current) == rect and
                  win.root_window(win.window_from_point(px, py)) == binding.hwnd, 'context_region_changed')
            pending = True
            output = win._send_point(px, py, 'right-click')
            _need(output.get('ok') and output.get('input_dispatched') is True, 'context_click_uncertain')
            sent, pending = True, False
            parent, anchor = None, (px, py, 1, 1)
            gap = max(6, round(12*binding.info()['dpi']/96))
            for number, name in enumerate(spec['path']):
                deadline = time.monotonic() + 3
                while True:
                    guard()
                    snapshot = _menus(binding, uia)
                    choice = _choose(uia, snapshot, before, name, anchor, parent, gap)
                    if choice is not None:
                        break
                    _need(time.monotonic() < deadline, 'new_context_menu_item_missing')
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
                prior_surfaces = set(binding.scoped_windows())
                _need(exposed in prior_surfaces and win.root_window(win.window_from_point(*fresh.center)) == exposed,
                      'context_menu_occluded')
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
        return dict(status='delivered_unverified', diagnostic='menu_leaf_sent',
                    input_dispatched=True, leaf_dispatched=True, menu_path_completed=True,
                    menu_items=completed, effect_verification='unverified')
    except Exception as error:
        return dict(status='uncertain' if pending else 'blocked', diagnostic=getattr(error,'code','context_provider_error'),
                    input_dispatched=None if pending and not sent else sent, leaf_dispatched=leaf_sent,
                    menu_path_completed=False, menu_items=completed, effect_verification='unverified')
