"""Observe the recorded application and bind a demonstrated context click without input."""
import json

import context_menu
import control_lookup
import grounding
import learned_tasks as tasks
import result_menu
import row_vision
import targeting


def _scope_key(main, surface, frame_key):
    return json.dumps(['context-menu', frame_key, main.hwnd, main.pid, main.created,
                       surface.hwnd, surface.pid, surface.created]).encode('ascii')


def _scoped_selectors(app, nodes, fields):
    """Prove each requested region with the same locator that replay will use."""
    scopes = {}
    by_path = {node['path']: node for node in nodes}
    for query, role, within in fields:
        try:
            index, _ = app.resolve(nodes, query, role=role, within=within)
        except app.SelectionError:
            continue
        field = tasks._selector(nodes[index])
        if not field:
            continue
        parent = by_path.get(nodes[index].get('parent_path'))
        while parent:
            parent_selector = tasks._selector(parent)
            if parent_selector and within.casefold() in (
                    (targeting._alias(parent) or '').casefold(), (parent.get('name') or '').casefold(),
                    (parent.get('automation_id') or '').casefold()):
                match = 'automation_id' if 'automation_id' in field else 'name'
                selector = dict(role=role, target=field[match], match=match, ancestors=[parent_selector])
                try:
                    resolved, _ = tasks.locate_target(nodes, selector)
                except tasks.TaskError:
                    break
                if resolved == index:
                    scopes.setdefault(index, {})[within] = selector
                break
            parent = by_path.get(parent.get('parent_path'))
    return scopes


def _targets(app, binding, uia, scoped_fields=()):
    nodes, _ = app.observe(binding, uia)
    regions, labels = app.scope_map(nodes), app.field_labels(nodes)
    scopes = _scoped_selectors(app, nodes, scoped_fields) if scoped_fields else {}
    targets = []
    for i, node in enumerate(nodes):
        if (node['secure'] or node.get('role') not in app.LIST_ROLES | context_menu.REGIONS or
                not (node.get('automation_id') or node.get('name') or i in labels)):
            continue
        description = app.describe(node, regions[i], labels.get(i, {}))
        if i in scopes:
            description['binding_scopes'] = scopes[i]
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


def _recorded_targets(app, surface, uia, lookups):
    """Observe stable recorded controls and detect caption collisions without a Python tree walk."""
    root = uia.from_handle(surface.hwnd)
    targets = []
    for selector in lookups['selectors']:
        element = control_lookup.resolve(surface, uia, selector, root)
        node = dict(uia.basic(element), enabled=element.property(30010) is True)
        node['recorded_selector'] = selector
        if selector.get('ancestors'):
            key = 'automation_id' if 'automation_id' in selector else 'name'
            ancestor = selector['ancestors'][0]
            within = ancestor.get('automation_id') or ancestor['name']
            node['binding_scopes'] = {within: dict(role=selector['role'], match=key, target=selector[key],
                                                  ancestors=selector['ancestors'])}
        app.add_display_properties(node, element, surface, uia, root)
        if node['offscreen']:
            raise app.bridge.BridgeError('target_offscreen')
        if node['role'] in tasks.VALUE_ACTIONS.values() and not node.get('label_source'):
            node.update(app.field_label(surface, uia, element, root))
        targets.append(node)
    # A legacy frame binding names a region without saying whether the string
    # was its ID or caption. Keep collisions across both identities visible.
    for name, role in lookups.get('regions', ()):
        seen = []
        for key in ('automation_id', 'name'):
            for element in control_lookup.find_all(uia, root, roles=(role,), **{key: name}):
                if not any(uia.same(element, previous) for previous in seen):
                    seen.append(element)
                    targets.append(uia.basic(element))
    if lookups['captions']:
        captions = {' '.join(label.split()).casefold() for label in lookups['captions']}
        for button in control_lookup.find_all(uia, root, roles=tasks.INVOKE - {'menuitem'}, limit=256):
            name = button.property(30005)
            if isinstance(name, str) and ' '.join(name.split()).casefold() in captions:
                node = dict(uia.basic(button), enabled=button.property(30010) is True)
                app.add_display_properties(node, button, surface, uia, root)
                if node not in targets:
                    targets.append(node)
    return targets


def observe_scope(app, main, surface, title, *, frame_key=None, lookups=None, bindings_only=False):
    check = _guard(app, main, surface, title)
    check()
    app.visibility.require_observable(surface)
    result = dict(exe=surface.exe, window=app.bridge.window_text(surface.hwnd),
                  targets=[], owned_popups=[], input_dispatched=False)
    with app.bridge.UIA() as uia:
        try:
            scoped_fields = (lookups or {}).get('scoped_fields', ())
            if lookups and not scoped_fields:
                try:
                    result['targets'] = _recorded_targets(app, surface, uia, lookups)
                    result['lookup_mode'] = 'recorded_selectors'
                except app.bridge.BridgeError:
                    scoped_fields = lookups.get('fallback_scopes', ())
                    lookups = None
            if not lookups or scoped_fields:
                result['targets'] = (_targets(app, surface, uia, scoped_fields) if scoped_fields
                                     else _targets(app, surface, uia))
                result['lookup_mode'] = 'window_inventory'
        except (app.SelectionError, app.bridge.BridgeError) as error:
            # A nonstandard provider may still expose its rendered window.
            result['tree_diagnostic'] = error.code
        # Explicit bindings are checked only against their own recorded window.
        # Popup inventories aid discovery but cannot satisfy these bindings.
        for popup in (() if bindings_only else app.owned_popups(surface)):
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
        if not bindings_only and context_menu._native_menu_owner(surface):
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


def menu_from_frame(app, main, surface, title, frame_key, binding, *, observation=None, inventory=None):
    check = _guard(app, main, surface, title)
    check()
    app.visibility.require_observable(surface)
    anchor = binding.get('menu_anchor')
    menu = None
    if anchor is not None and 'frame' not in binding:
        candidate = dict(target=anchor['target'], point=anchor['point'], path=binding['path'])
        if binding.get('point_policy'):
            candidate['point_policy'] = binding['point_policy']
        context_menu.validate_spec(candidate)
        target = candidate['target']
        name, role = target.get('automation_id', target.get('name')), target['role']
        if binding.get('target', name) != name or binding.get('role', role) != role:
            raise tasks.TaskError('context_region_changed')
        with app.bridge.UIA() as uia:
            if binding.get('preparing'):
                proof = context_menu.observe(surface, uia, candidate)
                if observation is not None:
                    observation['menu_preview'] = proof
                menu = candidate
            else:
                region = context_menu._region(surface, uia, target)
                x, y, w, h = context_menu._rect(uia, region)
                screen = (x + round(anchor['point'][0] * w), y + round(anchor['point'][1] * h))
    elif binding.get('menu_window_point') is not None and 'frame' not in binding:
        name, role = binding.get('target'), binding.get('role')
        if not tasks._text(name) or role not in context_menu.REGIONS:
            raise tasks.TaskError('named_context_region_required')
        window = row_vision._window_rect(surface)
        screen = tuple(start + min(extent-1, round(fraction*extent))
                       for start, extent, fraction in zip(window, window[2:], binding['menu_window_point']))
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
        with app.bridge.UIA() as uia:
            targets = [node for node in inventory['targets'] if node.get('role') == role
                       and name in (node.get('automation_id'), node.get('name'))]
            if len(targets) != 1:
                raise tasks.TaskError('context_region_ambiguous' if targets else 'context_region_missing')
            target = targets[0]
            key = 'automation_id' if target.get('automation_id') == name and not name.isdecimal() else 'name'
            candidate = dict(target={'role': role, key: name}, point=[.5, .5], path=binding['path'])
            if binding.get('point_policy'):
                candidate['point_policy'] = binding['point_policy']
            menu = context_menu.adjust_from_frame(surface, uia, candidate, screen, window=window, png=current)
    if menu is None:
        menu = context_menu.teach_at(surface, screen, binding['path'], target=name, role=role)
        if binding.get('point_policy'):
            menu['point_policy'] = binding['point_policy']
    check()
    context = dict(trigger=None, ready=None, menu=menu, output=binding.get('output'))
    result_menu.validate_spec(context)
    return context
