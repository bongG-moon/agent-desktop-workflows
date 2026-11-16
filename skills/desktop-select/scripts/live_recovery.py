"""Observe the recorded application and bind a demonstrated context click without input."""
import json

import context_menu
import grounding
import learned_tasks as tasks
import result_menu
import row_vision


def _scope_key(main, surface, frame_key):
    return json.dumps(['context-menu', frame_key, main.hwnd, main.pid, main.created,
                       surface.hwnd, surface.pid, surface.created]).encode('ascii')


def _targets(app, binding, uia):
    nodes, _ = app.observe(binding, uia)
    regions, labels = app.scope_map(nodes), app.field_labels(nodes)
    targets = []
    for i, node in enumerate(nodes):
        if (node['secure'] or node.get('role') not in app.LIST_ROLES | context_menu.REGIONS or
                not (node.get('automation_id') or node.get('name') or i in labels)):
            continue
        description = app.describe(node, regions[i], labels.get(i, {}))
        if node['role'] in context_menu.REGIONS and node.get('bounds_valid') and not node.get('offscreen'):
            description['screen_bounds'] = node['bounds']
        targets.append(description)
    return targets


def _guard(app, main, surface, title):
    surface_title = title if main.hwnd == surface.hwnd else app.bridge.window_text(surface.hwnd)

    def check():
        app.guard(main, main, title)
        if main.hwnd != surface.hwnd:
            app.guard(surface, surface, surface_title)
    return check


def observe_scope(app, main, surface, title, *, frame_key=None):
    check = _guard(app, main, surface, title)
    check()
    result = dict(exe=surface.exe, window=app.bridge.window_text(surface.hwnd),
                  targets=[], owned_popups=[], input_dispatched=False)
    with app.bridge.UIA() as uia:
        try:
            result['targets'] = _targets(app, surface, uia)
        except (app.SelectionError, app.bridge.BridgeError) as error:
            # A nonstandard provider may still expose its rendered window.
            result['tree_diagnostic'] = error.code
        for popup in app.owned_popups(surface):
            entry = dict(popup)
            try:
                owned = app.bind_surface(surface, popup['hwnd'])
                app.guard(surface, owned, app.bridge.window_text(surface.hwnd))
                targets = _targets(app, owned, uia)
                app.guard(surface, owned, app.bridge.window_text(surface.hwnd))
                entry['targets'] = targets
            except (app.SelectionError, app.bridge.BridgeError) as error:
                entry['diagnostic'] = error.code
            result['owned_popups'].append(entry)
        if context_menu._native_menu_owner(surface):
            try:
                result['open_menu_items'] = [item.name for item in context_menu._menus(surface, uia)]
            except app.bridge.BridgeError as error:
                result['menu_diagnostic'] = error.code
    if frame_key is not None:
        window, png = grounding._capture(app, surface, check)
        token, path = grounding.store_frame(grounding._directory(), _scope_key(main, surface, frame_key), png)
        result.update(frame=token, image=str(path), screen_origin=window[:2], width=window[2], height=window[3],
                      expires_seconds=grounding.TTL)
    check()
    return result


def _same_patch(previous, current, point):
    old_box, old = grounding._pixels(previous)
    new_box, new = grounding._pixels(current)
    if old_box != new_box:
        return False
    width, height = old_box[2:]
    x, y = point
    if not (0 <= x < width and 0 <= y < height):
        return False
    for row in range(max(y - 12, 0), min(y + 13, height)):
        start = 4 * (row * width + max(x - 12, 0))
        end = 4 * (row * width + min(x + 13, width))
        if old[start:end] != new[start:end]:
            return False
    return True


def menu_from_frame(app, main, surface, title, frame_key, binding):
    check = _guard(app, main, surface, title)
    check()
    anchor = binding.get('menu_anchor')
    if anchor is not None and 'frame' not in binding:
        candidate = dict(target=anchor['target'], point=anchor['point'], path=binding['path'])
        context_menu.validate_spec(candidate)
        target = candidate['target']
        name, role = target.get('automation_id', target.get('name')), target['role']
        if binding.get('target', name) != name or binding.get('role', role) != role:
            raise tasks.TaskError('context_region_changed')
        with app.bridge.UIA() as uia:
            region = context_menu._region(surface, uia, target)
            x, y, w, h = context_menu._rect(uia, region)
        screen = (x + round(anchor['point'][0] * w), y + round(anchor['point'][1] * h))
    else:
        point = binding.get('point')
        name, role = binding.get('target'), binding.get('role')
        if (not isinstance(point, list) or len(point) != 2 or any(type(n) != int for n in point)
                or not tasks._text(name) or role not in context_menu.REGIONS):
            raise tasks.TaskError('invalid_context_point')
        scope = _scope_key(main, surface, frame_key)
        _, previous = grounding.read_frame(grounding._directory(), scope, binding.get('frame'))
        window, current = grounding._capture(app, surface, check)
        if not _same_patch(previous, current, point) or window != row_vision._window_rect(surface):
            raise tasks.TaskError('grounding_frame_changed')
        screen = (window[0] + point[0], window[1] + point[1])
    menu = context_menu.teach_at(surface, screen, binding['path'], target=name, role=role)
    check()
    context = dict(trigger=None, ready=None, menu=menu, output=binding.get('output'))
    result_menu.validate_spec(context)
    return context
