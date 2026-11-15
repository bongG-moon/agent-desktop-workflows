"""Direct Windows actions and named demonstrations using the same live targets."""
from __future__ import annotations

import argparse
from collections import deque
from copy import deepcopy
import ctypes as C
from ctypes import wintypes as W
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

from runtime_integrity import checked_path
from provider_security import install as install_security_compatibility


def load_bridge():
    path = checked_path('win_bridge.py')
    existing = sys.modules.get('win_bridge')
    if existing is not None and Path(existing.__file__).resolve() == path.resolve():
        return install_security_compatibility(existing)
    spec = importlib.util.spec_from_file_location('win_bridge', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules['win_bridge'] = module
    return install_security_compatibility(module)


bridge = load_bridge()
from targeting import SelectionError, field_labels, resolve, scope_map
from live_action import execute
import provider_visibility as visibility
import learned_tasks as tasks
import input_policy


def windows(exe):
    """Enumerate top-level windows. Only obtain captions from the requested app."""
    bridge._windows()
    if not exe or Path(exe).name != exe or not exe.casefold().endswith('.exe'):
        raise SelectionError('exact_exe_name_required')
    records, truncated = [], False
    callback_type = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)

    def visit(hwnd, _):
        nonlocal truncated
        if not bridge.user32.IsWindowVisible(hwnd):
            return True
        pid = bridge.hwnd_pid(hwnd)
        try:
            actual, started = bridge._process_identity(pid)
        except (bridge.BridgeError, OSError):
            return True
        if actual.casefold() == exe.casefold():
            records.append({'exe_name': actual, 'pid': pid, 'hwnd': int(hwnd),
                            'title': bridge.window_text(hwnd), 'process_started_utc_ticks': started})
            if len(records) >= 256:
                truncated = True
                return False
        return True

    enum = bridge._sig(bridge.user32, 'EnumWindows', W.BOOL, callback_type, W.LPARAM)
    if not enum(callback_type(visit), 0) or truncated:
        raise SelectionError('window_list_incomplete')
    return records


def pick_window(records, title, hwnd):
    if not title.strip():
        raise SelectionError('exact_business_window_title_required')
    matches = [r for r in records if r['title'] == title and (hwnd is None or r['hwnd'] == hwnd)]
    if len(matches) != 1:
        raise SelectionError('ambiguous_window' if matches else 'window_not_found')
    return matches[0]


def guard(owner, surface, title, mutate=False):
    owner.check()
    surface.check()
    if bridge.window_text(owner.hwnd) != title or not bridge.user32.IsWindowVisible(owner.hwnd):
        raise SelectionError('business_window_changed')
    if owner.hwnd != surface.hwnd and (not bridge.owned(surface.hwnd, owner.hwnd)
            or not bridge.user32.IsWindowVisible(surface.hwnd)):
        raise SelectionError('popup_not_owned_or_closed')
    enabled = bridge._sig(bridge.user32, 'IsWindowEnabled', W.BOOL, W.HWND)
    if mutate and not enabled(surface.hwnd):
        raise SelectionError('window_disabled')


def owned_popups(owner):
    return [{'hwnd': h, 'title': bridge.window_text(h), 'class_name': bridge.window_class(h)}
            for h in owner.scoped_windows() if h != owner.hwnd and bridge.owned(h, owner.hwnd)]


def bind_surface(owner, selected):
    if selected is None:
        return owner
    if not any(p['hwnd'] == selected for p in owned_popups(owner)):
        raise SelectionError('popup_not_owned_or_closed')
    return bridge.Binding({'pid': owner.pid, 'exe_name': owner.exe, 'hwnd': selected,
                           'process_started_utc_ticks': owner.created})


def add_display_properties(node, element, binding, uia, root):
    bounds = uia.bounds(element)
    valid = visibility.valid(bounds)
    node.update(class_name=element.property(30012) or '', bounds=bounds, bounds_valid=valid,
                offscreen=not visibility.visible(binding, uia, element, root))
    if node['role'] in {'combobox', 'textfield', 'checkbox', 'radiobutton', 'spinner'}:
        label = uia.related_element(element, 30018)
        if label is not None:
            if (uia.pid(label) != binding.pid or not uia.within(label, root) or uia.secure(label)
                    or not visibility.visible(binding, uia, label, root)):
                node['label_source'] = 'invalid_labeled_by'
            else:
                node.update(semantic_label=label.property(30005) or '', label_source='labeled_by')


def field_label(binding, uia, element, root):
    """Read one field's label from its local form, including a single label wrapper.

    This uses the original 0.4.0 label resolver without collecting an entire
    business window or reading neighboring field values.
    """
    binding.check()
    if (uia.pid(element) != binding.pid or uia.secure(element)
            or not uia.within(element, root)):
        return {}
    node = dict(uia.basic(element), secure=False)
    if node['role'] not in {'combobox', 'textfield', 'checkbox', 'radiobutton', 'spinner'}:
        return {}
    add_display_properties(node, element, binding, uia, root)
    if node.get('label_source') == 'labeled_by':
        return {key: node[key] for key in ('semantic_label', 'label_source')}
    if node.get('label_source') == 'invalid_labeled_by':
        return {}
    parent = uia.parent(element)
    if (parent is None or uia.pid(parent) != binding.pid or uia.secure(parent)
            or not uia.within(parent, root)):
        return {}
    nodes, target = [dict(path='0', parent_path='', secure=False, **uia.basic(parent))], None
    for index, sibling in enumerate(uia.children(parent)):
        if index >= 160 or uia.pid(sibling) != binding.pid:
            return {}
        path = '0/' + str(index)
        current = dict(path=path, parent_path='0', secure=uia.secure(sibling))
        if not current['secure']:
            current.update(uia.basic(sibling))
            add_display_properties(current, sibling, binding, uia, root)
        if uia.same(sibling, element):
            target = len(nodes)
        nodes.append(current)
        if current.get('role') != 'group' or current['secure']:
            continue
        children = iter(uia.children(sibling))
        child = next(children, None)
        if child is None or next(children, None) is not None:
            continue
        if uia.pid(child) != binding.pid:
            return {}
        wrapped = dict(path=path+'/0', parent_path=path, secure=uia.secure(child))
        if not wrapped['secure']:
            wrapped.update(uia.basic(child))
            add_display_properties(wrapped, child, binding, uia, root)
        nodes.append(wrapped)
    binding.check()
    return field_labels(nodes).get(target, {})


def _stable_selector(nodes, index):
    """Carry the initially resolved field and its witnessed ancestor scope forward."""
    node = nodes[index]
    if node.get('role') not in bridge.ROLES.values():
        return None
    # SWT/native providers can expose decimal HWNDs as AutomationId. Those
    # identify this instance only, and must not become a registered selector.
    identifier = node.get('automation_id')
    if isinstance(identifier, str) and identifier and not identifier.isdecimal():
        target = dict(automation_id=identifier)
    else:
        name = node.get('name')
        if not isinstance(name, str) or not name.strip() or sum(
                n.get('secure') is False and n.get('role') == node['role'] and n.get('name') == name
                for n in nodes) != 1:
            return None
        # The caller also resolves this selector against the live tree and
        # compares element identity before storing or using it.
        target = dict(name=name)
    parents, path, by_path = [], node.get('parent_path'), {n['path']: n for n in nodes}
    while path:
        ancestor = by_path[path]
        if ancestor.get('secure') is not False or ancestor.get('role') not in bridge.ROLES.values():
            return None
        identity = {key: ancestor[key] for key in ('automation_id', 'name') if ancestor.get(key)
                    and (key != 'automation_id' or not str(ancestor[key]).isdecimal())}
        if identity:
            parents.append(dict(role=ancestor['role'], **identity))
        path = ancestor.get('parent_path')
    if len(parents) > 16:
        return None
    return dict(**target, role=node['role'], ancestors=parents)


def observe(binding, uia, *, identities_only=False):
    """Read structural properties only. Never read values of unrelated fields/rows."""
    binding.check()
    root = uia.from_handle(binding.hwnd)
    if root is None or uia.pid(root) != binding.pid:
        raise SelectionError('root_unavailable')
    nodes, elements = [], []
    queue = deque([(root, '0', '', 0)])
    while queue:
        element, path, parent, depth = queue.popleft()
        if len(nodes) >= 2500 or depth > 32:
            raise SelectionError('incomplete_tree')
        # An embedded browser can have a different process from its native
        # host. Keep an opaque boundary instead of aborting every unrelated
        # host control; never read its identity, value or descendants.
        foreign = uia.pid(element) != binding.pid
        secure = foreign or uia.secure(element)
        node = {'path': path, 'parent_path': parent, 'secure': secure}
        if not secure:
            basic = uia.basic(element)
            node.update(basic, enabled=element.property(30010) is True)
            if not identities_only:
                add_display_properties(node, element, binding, uia, root)
            for i, child in enumerate(uia.children(element)):
                queue.append((child, path + '/' + str(i), path, depth + 1))
        elements.append(element)
        nodes.append(node)
    binding.check()
    return nodes, elements


LIST_ROLES = {'button', 'combobox', 'menuitem', 'treeitem', 'tab', 'table', 'toolbar', 'textfield', 'checkbox'}


def describe(node, regions, label):
    result = {k: node[k] for k in ('role', 'automation_id', 'name', 'enabled', 'offscreen')
              if node.get(k) not in (None, '')}
    result.update(label)
    result['regions'] = regions
    return result


def task_directory():
    return tasks._directory(None)


def storage_info():
    """Describe the chosen local store without creating it or observing a window."""
    directory = task_directory()
    info = tasks.storage_paths.storage_info()
    return dict(status='observed', **info, execution_plan_directory=str(directory / 'execution-plans'),
                input_dispatched=False)


def near_region(nodes, anchor):
    """Return the nearest grouping of a freshly resolved demonstrated control."""
    if anchor.get('action') in ('manual', 'row-action', 'result-menu'):
        raise SelectionError('anchor_requires_review')
    anchor_idx, _ = tasks.locate_target(nodes, anchor)
    ancestors = anchor.get('ancestors', [])
    by_path = {n['path']: n for n in nodes}
    path = nodes[anchor_idx]['parent_path']
    while path:
        group = by_path[path]
        # A business window alone doesn't establish which portion was demonstrated.
        if not group['parent_path'] or group['secure']:
            break
        witnessed = any(all(group.get(k) == v for k, v in item.items())
                        for item in ancestors if item.get('automation_id') or item.get('name'))
        if witnessed and group.get('role') in {'group', 'toolbar', 'tab', 'menu', 'table', 'list', 'tree'}:
            indexes = [i for i, n in enumerate(nodes) if n['path'] == path or n['path'].startswith(path + '/')]
            scoped = [dict(nodes[i], parent_path='') if nodes[i]['path'] == path else nodes[i] for i in indexes]
            return scoped, indexes, anchor_idx
        path = group['parent_path']
    raise SelectionError('demonstrated_region_unavailable')


def action(args, owner, *, learned=None, anchor=None):
    background = getattr(args, 'background_only', False)
    if args.command in {'select', 'fill', 'open'}:
        if background:
            input_policy.window_ready(owner)
        else:
            visibility.restore_for_action(owner, args.window)
    surface = bind_surface(owner, args.popup)
    mutate = args.command in {'select', 'fill', 'open'}
    def check():
        guard(owner, surface, args.window, mutate)
        if background and mutate:
            input_policy.window_ready(surface)
    check()
    with bridge.UIA() as uia:
        if background and mutate:
            input_policy.configure_uia(uia)
        def read_view():
            nodes, elements = observe(surface, uia, identities_only=learned is not None or anchor is not None)
            if anchor is None:
                return nodes, elements, None
            scoped, indexes, a = near_region(nodes, anchor)
            if anchor.get('label'):
                tasks.check_display(anchor, uia.semantic_label(elements[a], elements[0]))
            region = [elements[i] for i in indexes]
            for n, e in zip(scoped, region):
                if not n['secure']:
                    add_display_properties(n, e, surface, uia, region[0])
            return scoped, region, elements[a]
        nodes, elements, anchor_element = read_view()
        grouped, labels = (None, {}) if learned is not None else (scope_map(nodes), field_labels(nodes))
        if args.command == 'inspect' and not args.target:
            items = [describe(n, grouped[i], labels.get(i, {})) for i, n in enumerate(nodes) if not n['secure']
                     and n.get('role') in LIST_ROLES and (n.get('automation_id') or n.get('name') or i in labels)]
            return {'status': 'observed', 'targets': items, 'owned_popups': owned_popups(owner), 'input_dispatched': False}
        role = {'select': 'combobox', 'fill': 'textfield'}.get(args.command, getattr(args, 'role', None))
        def locate(tree, available):
            if learned is not None:
                index, evidence = tasks.locate_target(tree, learned)
                if learned.get('label'):
                    tasks.check_display(learned, uia.semantic_label(available[index], available[0]))
                return index, evidence
            return resolve(tree, args.target, role=role, within=args.within)
        i, evidence = locate(nodes, elements)
        element, node = elements[i], nodes[i]
        detail = describe(node, grouped[i] if grouped is not None else [], labels.get(i, {}))
        if args.command == 'inspect':
            result = {'status': 'resolved', 'target': detail, 'matched_by': evidence,
                      'actions': uia.actions(element), 'input_dispatched': False}
            if node['role'] == 'combobox':
                from live_action import combo_readback, option_inventory
                try:
                    result.update(combo_readback(surface, uia, element))
                except bridge.BridgeError:
                    result.update(value=None, value_evidence={'status': 'unavailable', 'source': 'none'})
                try:
                    result.update(option_inventory(surface, uia, element))
                except bridge.BridgeError:
                    result.update(options=[], options_status='unavailable')
            elif node['role'] == 'textfield':
                from live_action import text_readback
                result.update(text_readback(surface, uia, element))
            return result

        # After the initial 0.4.0 label/region resolution, revalidate a stable
        # identity with native UIA queries. Learned regions and owned popups
        # retain their original full-resolution path.
        stable = (_stable_selector(nodes, i) if learned is None and anchor is None
                  and args.popup is None else None)
        initial_label = labels.get(i, {})

        def still_valid():
            check()
            if stable is not None:
                import control_lookup
                root = uia.from_handle(surface.hwnd)
                if not uia.same(elements[0], root):
                    raise SelectionError('target_changed')
                current = control_lookup.resolve(surface, uia, stable, root)
                if not uia.same(element, current):
                    raise SelectionError('target_changed')
                if (node['role'] in {'button', 'menuitem', 'splitbutton', 'hyperlink', 'treeitem', 'tab', 'listitem'}
                        and node.get('name') and current.property(30005) != node['name']):
                    raise SelectionError('target_name_changed')
                if initial_label and field_label(surface, uia, current, root) != initial_label:
                    raise SelectionError('target_label_changed')
                check()
                return
            current, live, current_anchor = read_view()
            if anchor is not None:
                if not uia.same(anchor_element, current_anchor) or not uia.same(elements[0], live[0]):
                    raise SelectionError('demonstrated_anchor_changed')
            j, basis = locate(current, live)
            if basis != evidence or not uia.same(element, live[j]):
                raise SelectionError('target_changed')
            check()

        command = args.command if args.command in ('select', 'fill') else ('expand' if args.expand else
                  'choose' if node['role'] in {'treeitem', 'tab', 'listitem'} else 'invoke')
        result = execute(surface, uia, element, node, command, getattr(args, 'value', None), still_valid=still_valid)
        return dict(result, target=detail, matched_by=evidence, business_commit='not_verified',
                    input_policy='background_only' if background else 'auto')


def saved_execution_plan(plan):
    """Derived plans are optional; failure cannot undo an already saved recording."""
    import recorded_plan
    try:
        return recorded_plan.get_plan(plan, task_directory())
    except (ValueError, OSError) as error:
        return {'status': 'unavailable', 'diagnostic': getattr(error, 'code', 'execution_plan_unavailable')}


def execution_plan_info(result):
    info = {key: result[key] for key in ('status', 'path', 'diagnostic', 'step', 'grounding_steps') if key in result}
    envelope = result.get('envelope', {})
    recipe = envelope.get('recipe')
    if recipe:
        info.update(source_fingerprint=envelope['source_fingerprint'], steps=len(recipe['steps']),
                    required_parameters=list(dict.fromkeys(s['parameter'] for s in recipe['steps'] if 'parameter' in s)))
        conditional = [dict(number=n, seconds=s['seconds'], when_present=s['when_present']['name'],
                            dismiss=s['dismiss']['name'], always=s['always']['name'])
                       for n, s in zip(envelope['source_steps'], recipe['steps']) if s['action'] == 'conditional-open']
        if conditional:
            info['interpreted_steps'] = conditional
    elif result.get('diagnostic') == 'instruction_requires_structure':
        info['host_action'] = 'interpret_recorded_instructions'
    return info


def save_instruction(args):
    """Keep freeform source prose and its once-interpreted action in a new task."""
    source = tasks.load_task(args.task, task_directory())
    plan = deepcopy(source)
    tasks.check_new_task(args.save_as, task_directory())
    number = tasks._step_number(plan, args.only)
    step = plan['steps'][number - 1]
    if args.alias is not None:
        step['alias'] = args.alias
    ordinal = getattr(args, 'row', None)
    if ordinal is not None:
        from row_intent import conflicts_with_launch, parse
        interpreted = parse(args.text)
        if conflicts_with_launch(args.text) or (interpreted is not None and interpreted != ordinal):
            raise tasks.TaskError('instruction_binding_conflict')
        if (not 1 <= ordinal <= 64 or not tasks._text(args.text, 1000)
                or any(v is not None for v in (args.mode, args.argument, args.target, args.role, args.within, args.wait_after))
                or not (tasks.is_recoverable(step) or step.get('action') in ('row-action','row-launch'))):
            raise tasks.TaskError('invalid_row_instruction')
        hint = step.get('row', step.get('row_hint'))
        step = dict(action='row-launch', ordinal=ordinal, label=f'{ordinal}번째 행 실행',
                    **tasks._annotations(step))
        step['description'] = args.text.strip()
        if hint is not None:
            step['row_hint'] = hint
    else:
        spec = tasks.compile_instruction(step, args.text, mode=args.mode, argument=args.argument,
                                         target=args.target, role=args.role, within=args.within)
        if spec is None:
            raise tasks.TaskError('instruction_required')
        step = tasks.instructed_step(step, spec)
    plan['steps'][number - 1] = step
    if args.wait_after is not None:
        step['wait_after'] = args.wait_after
    plan['name'] = args.save_as
    tasks.save_task(plan, task_directory())
    return dict(status='recorded', task=tasks.summary(plan), source_task=source['name'],
                execution_plan=execution_plan_info(saved_execution_plan(plan)), input_dispatched=False)


def fast_replay(plan, args, overrides):
    """Use a saved literal plan only when the entire original task is compatible."""
    legacy_values = any(s['action'] in ('select', 'fill') and 'instruction' not in s and 'instruction_draft' not in s
                        for s in plan['steps'])
    tasks.check_binding(plan, args.exe, args.window)
    saved = saved_execution_plan(plan)
    recipe = saved.get('envelope', {}).get('recipe')
    if recipe is None:
        if saved.get('status')=='plan_unavailable':
            return dict(status='blocked',diagnostic=saved.get('diagnostic'),steps=[],input_dispatched=False)
        return None
    source_steps = saved['envelope']['source_steps']
    has_host_binding = any(step['action'] in ('conditional-open', 'row-open') for step in recipe['steps'])
    if legacy_values and not has_host_binding:
        return None
    if has_host_binding:
        # A host interpretation can coexist with 0.4 recorded defaults. A raw
        # field whose prose now describes a conditional is no longer an input.
        recipe = deepcopy(recipe)
        selected = tasks._step_number(plan, args.only) if args.only is not None else None
        source_numbers = {n for n, step in zip(source_steps, recipe['steps'])
                          if step['action'] not in ('row-open', 'conditional-open') and (selected is None or n == selected)}
        sequence = [(n, plan['steps'][n - 1]) for n in sorted(source_numbers)]
        supplied = tasks.recorded_overrides(sequence, overrides)
        for n, step in zip(source_steps, recipe['steps']):
            raw = plan['steps'][n - 1]
            if n in source_numbers and 'parameter' in step:
                if n in supplied:
                    step['value'] = supplied[n]
                elif 'instruction' not in raw and 'value' in raw:
                    step['value'] = raw['value']
                if 'value' in step:
                    del step['parameter']
        overrides = {}
    active = recipe
    only = None
    if args.only is not None:
        number = tasks._step_number(plan, args.only)
        matches = [i for i, n in enumerate(source_steps) if n == number]
        if len(matches) != 1 or recipe['steps'][matches[0]]['action'] not in ('conditional-open','row-open'):
            return None
        only = str(matches[0] + 1)
        active = dict(recipe, steps=[recipe['steps'][matches[0]]])
    parameters = {s['parameter'].casefold() for s in active['steps'] if 'parameter' in s}
    if any(key.casefold() not in parameters for key in overrides):
        return None  # Keep the original alias and fixed-value-override rules.
    import python_workflow
    _, missing = python_workflow.values_for(active, overrides)
    if missing:
        return dict(status='needs_input', diagnostic='required_parameter_missing', missing_parameters=missing,
                    steps=[], input_dispatched=False, execution_plan=execution_plan_info(saved))
    result = worker_receipt(['_plan'], mutating=True,
                            payload={'recipe': recipe, 'overrides': overrides, 'hwnd': args.hwnd,
                                     'background_only': getattr(args, 'background_only', False),
                                     **({'only': only} if only is not None else {})})
    for row in result.get('steps', []):
        number = row.get('number')
        if type(number) is int and 1 <= number <= len(source_steps):
            row['source_step'] = source_steps[number - 1]
            row['phase'] = 'wait' if recipe['steps'][number - 1]['action'] == 'wait' else 'action'
    stopped = result.get('stopped_at')
    if type(stopped) is int and 1 <= stopped <= len(source_steps):
        result['source_stopped_at'] = source_steps[stopped - 1]
    for item in result.get('grounding_steps', []):
        number = item.get('number')
        if type(number) is int and 1 <= number <= len(source_steps):
            item['source_step'] = source_steps[number - 1]
            item['ground_args'] = ['ground','--task',plan['name'],'--only',str(item['source_step'])]
    return dict(result, execution_engine='saved_python_plan', execution_plan=execution_plan_info(saved))


def replay_input_requirements(plan, only=None):
    """Use the same literal compiler as replay without saving or observing anything."""
    import recorded_plan
    compiled = recorded_plan.compile_saved(plan, task_directory())
    recipe = compiled.get('envelope', {}).get('recipe')
    if only is not None:
        number = tasks._step_number(plan, only)
        if recipe:
            linked = [step for step, n in zip(recipe['steps'], compiled['envelope']['source_steps']) if n == number]
            return input_policy.profile(linked, [number] * len(linked))
        return input_policy.profile([plan['steps'][number - 1]], [number])
    if recipe:
        return input_policy.profile(recipe['steps'], compiled['envelope']['source_steps'])
    return input_policy.profile(plan['steps'])


class _LinkedBinding(bridge.Binding):
    """Keep the primary window and the chosen companion pinned inside the action worker."""
    def __init__(self, record, main, opener=None):
        super().__init__(record)
        self.title = record['title']
        self.main = bridge.Binding(main)
        self.main_title = main['title']
        self.opener = bridge.Binding(opener) if opener else None
        self.opener_title = opener['title'] if opener else None
        self.companion = bridge.Binding(record) if opener else None

    def check(self):
        super().check()
        guard(self.main, self.main, self.main_title)
        selected = pick_window(windows(self.exe), self.title, None)
        if (selected['hwnd'], selected['pid'], selected['process_started_utc_ticks']) != (
                self.hwnd, self.pid, self.created):
            raise SelectionError('recorded_window_lifetime_changed')
        if self.opener is not None:
            from capture_windows import related
            guard(self.opener, self.opener, self.opener_title)
            if not related(self.companion, self.opener):
                raise SelectionError('recorded_window_relationship_changed')


def _linked_record(binding, title):
    return dict(hwnd=binding.hwnd, pid=binding.pid, exe_name=binding.exe, title=title,
                process_started_utc_ticks=binding.created)


def _opener_record(routes, context):
    if context.get('related'):
        number = context['after_step']
        source = routes.openers[number]
        title = routes.steps[number-1].get('window_context', {}).get('title', routes.title)
        return _linked_record(source, title)
    return None


def _replay_recorded(plan, args, overrides, background):
    # Specialized steps may use individual action workers. Their window routing
    # must follow the same lifetime checks as a compiled Python workflow.
    from window_context import WindowRoutes, ContextError
    steps = plan['steps']
    active = [tasks._step_number(plan,args.only)] if args.only is not None else range(1,len(steps)+1)
    numbers = iter(active)
    linked = any('window_context' in steps[n-1] for n in active)
    main = routes = None

    def invoke(step):
        nonlocal main, routes
        number = next(numbers)
        try:
            if main is None:
                main = pick_window(windows(args.exe), args.window, args.hwnd)
                if linked:
                    routes = WindowRoutes(steps,bridge.Binding(main),args.window,active=active)
            target = main
            context = step.get('window_context')
            if routes:
                source_window = None
                if context:
                    source_window = routes.resolve(context)
                    target = _linked_record(source_window,context['title'])
                routes.before(number,source_window)
        except (SelectionError, bridge.BridgeError, ContextError) as error:
            return dict(status='blocked',diagnostic=error.code,input_dispatched=False)
        payload = dict(binding=target, step=step, background_only=background)
        if context:
            payload['main_window'] = main
            payload['opener_window'] = _opener_record(routes, context)
        receipt = worker_receipt(['_step'], mutating=True, payload=payload)
        if routes:
            routes.after(number,receipt)
            if (routes.origins.get(number) and step['action']=='open' and not step.get('expand')
                    and receipt.get('status')=='delivered_unverified' and receipt.get('input_dispatched') is True
                    and receipt.get('diagnostic')=='business_result_not_verified'):
                # Only the appearance of the specifically recorded window is
                # verified here. The application's business outcome is separate.
                try:
                    for spec in routes.origins[number].values():
                        routes.resolve(spec)
                except (SelectionError,bridge.BridgeError,ContextError) as error:
                    return dict(receipt,diagnostic=error.code)
                receipt = dict(receipt,status='verified',effect_verification='matched',
                               verification_scope='recorded_window_opened',business_commit='not_verified')
                receipt.pop('diagnostic',None)
        return receipt
    return tasks.replay(plan,args.exe,args.window,overrides,invoke,only=args.only)


def run(args):
    if args.command == 'storage':
        return storage_info()
    bridge.set_dpi_awareness()
    if args.command in ('ground','bind-visual','bind-row'):
        import grounding
        return grounding.run(sys.modules[__name__],args)
    if args.command == 'instruct':
        return save_instruction(args)
    if args.command == 'interpret':
        import compound_plan
        task = tasks.load_task(args.task, task_directory())
        instructions = compound_plan.read_json(args.plan_file)
        result = compound_plan.save(task, instructions, task_directory())
        return dict(status='planned' if result.get('envelope') else 'needs_review', task=task['name'],
                    execution_plan=execution_plan_info(result), input_dispatched=False)
    if args.command == 'plan':
        plan = tasks.load_task(args.task, task_directory())
        result = saved_execution_plan(plan)
        return dict(status='planned' if result.get('envelope') else 'needs_review',
                    task=tasks.summary(plan), execution_plan=execution_plan_info(result), input_dispatched=False)
    if args.command in ('tasks', 'show'):
        if args.command == 'tasks':
            return {'status': 'observed', 'tasks': tasks.list_tasks(task_directory()), 'input_dispatched': False}
        return {'status': 'observed', 'task': tasks.summary(tasks.load_task(args.task, task_directory())),
                'input_dispatched': False}
    if args.command == 'replay':
        original = tasks.load_task(args.task, task_directory())
        plan = tasks.resolve_saved_intents(original)
        requirements = replay_input_requirements(original, args.only)
        if getattr(args, 'input_plan', False):
            return dict(status='observed', input_dispatched=False, input_requirements=requirements)
        background = getattr(args, 'background_only', False)
        if background:
            refusal = input_policy.blocked(requirements)
            if refusal:
                return refusal
        overrides = tasks.parse_overrides(args.set)
        quick = fast_replay(original, args, overrides)
        if quick is not None:
            return quick
        report = _replay_recorded(plan, args, overrides, background)
        if report.get('diagnostic') == 'instruction_requires_structure':
            report['host_action'] = 'interpret_recorded_instructions'
        return dict(report, input_policy='background_only' if background else 'auto', input_requirements=requirements)
    rows = windows(args.exe)
    if args.command == 'windows':
        return {'status': 'observed', 'windows': rows, 'input_dispatched': False}
    binding = pick_window(rows, args.window, args.hwnd)
    owner = bridge.Binding(binding)
    if args.command == 'row-launch':
        from row_intent import execute
        if args.background_only and not args.preview:
            return input_policy.blocked(input_policy.profile([{'action':'row-launch'}]))
        return execute(owner, args.window, args.ordinal,
                       lambda: guard(owner, owner, args.window, mutate=not args.preview),
                       task_directory(), preview=args.preview)
    if args.command == 'record-rows':
        from row_action import teach_rows
        guard(owner, owner, args.window)
        return teach_rows(owner, args.window, args, task_directory())
    if args.command == 'row-status':
        from row_vision import observe_row
        plan = tasks.load_task(args.task, task_directory())
        tasks.check_binding(plan, args.exe, args.window)
        numbers = [tasks._step_number(plan, args.only)] if args.only is not None else [
            n for n, step in enumerate(plan['steps'], 1) if step['action'] == 'row-action']
        if not numbers:
            raise SelectionError('task_has_no_row_steps')
        from window_context import WindowRoutes
        routes = WindowRoutes(plan['steps'], owner, args.window, read_only=True, active=numbers)
        result = []
        for number in numbers:
            step = plan['steps'][number - 1]
            if step['action'] != 'row-action':
                raise SelectionError('not_a_row_step')
            context = step.get('window_context')
            target = routes.resolve(context) if context else owner
            if target is None:
                result.append(dict(number=number, label=step['label'], observed_state='unavailable',
                                   diagnostic='recorded_window_not_open'))
                continue
            guard(owner, owner, args.window)
            state = observe_row(target, step['row'])['kind']
            if context:
                routes.check(context, target)
            guard(owner, owner, args.window)
            result.append({'number': number, 'label': step['label'], 'observed_state': state})
        return {'status': 'observed', 'steps': result, 'input_dispatched': False}
    if args.command == 'record':
        from teach_capture import capture
        tasks.check_new_task(args.task, task_directory())
        guard(owner, owner, args.window)
        capture_result = capture(owner, title=args.window)
        if capture_result['status'] != 'recorded':
            return capture_result
        plan = tasks.from_recording(args.task, args.exe, args.window, capture_result['steps'])
        tasks.save_task(plan, task_directory())
        return {'status': 'recorded', 'task': tasks.summary(plan), 'input_dispatched': False,
                'execution_plan': execution_plan_info(saved_execution_plan(plan))}
    anchor = None
    if getattr(args, 'near', None) is not None:
        plan = tasks.load_task(args.task, task_directory())
        tasks.check_binding(plan, args.exe, args.window)
        anchor = tasks.get_step(plan, args.near)
        context = anchor.get('window_context')
        if context:
            from window_context import WindowRoutes
            routes = WindowRoutes(plan['steps'],owner,args.window,read_only=args.command=='inspect',
                                  active=[tasks._step_number(plan,args.near)])
            found = routes.resolve(context)
            if found is None:
                raise SelectionError('recorded_window_not_open')
            owner = _LinkedBinding(_linked_record(found,context['title']),binding,_opener_record(routes,context))
            args = argparse.Namespace(**{**vars(args), 'window':context['title']})
    return action(args, owner, anchor=anchor)


def replay_step():
    request = json.load(sys.stdin)
    binding, step = request['binding'], request['step']
    background = request.get('background_only', False)
    if background:
        refusal = input_policy.blocked(input_policy.profile([step]))
        if refusal:
            return refusal
    bridge.set_dpi_awareness()
    if step.get('window_context'):
        if 'main_window' not in request or binding['title'] != step['window_context']['title'] or (
                binding['exe_name'].casefold() != step['window_context']['exe'].casefold()):
            raise SelectionError('recorded_window_context_required')
        opener = request.get('opener_window')
        if step['window_context'].get('related') and not opener:
            raise SelectionError('recorded_window_origin_required')
        owner = _LinkedBinding(binding,request['main_window'],opener)
        owner.check()
    else:
        owner = bridge.Binding(binding)
    if step['action'] == 'visual-click':
        import visual_target
        return visual_target.execute(owner, step['visual'], lambda:guard(owner,owner,binding['title'],mutate=True),
                                     background_only=background)
    if step['action'] == 'row-launch':
        from row_intent import execute
        return execute(owner, binding['title'], step['ordinal'],
                       lambda: guard(owner, owner, binding['title'], mutate=True), task_directory(),
                       hint=step.get('row_hint'))
    if step['action'] == 'result-menu':
        from result_menu import execute as execute_result
        tasks._validate_context(step)
        receipt = execute_result(owner, step['context'], lambda: guard(owner, owner, binding['title'], mutate=True),
                                 binding['title'], observe_guard=lambda: guard(owner, owner, binding['title']))
        return dict(receipt, target=step['label'])
    if step['action'] == 'row-action':
        from row_action import execute as execute_row
        tasks._validate_row(step)
        receipt = execute_row(owner, step['row'], lambda: guard(owner, owner, binding['title'], mutate=True))
        return dict(receipt, target=step['label'])
    args = argparse.Namespace(command=step['action'], role=step['role'], window=binding['title'],
                              target=step['target'], value=step.get('value'), expand=step.get('expand', False),
                              within=step.get('within'), popup=None, background_only=background)
    if args.command not in ('select', 'fill', 'open'):
        raise SelectionError('step_requires_review')
    return action(args, owner, learned=None if step.get('match') == 'query' else step)


def parser():
    p = argparse.ArgumentParser(description='Select an exact field or command in one Windows business window.')
    commands = p.add_subparsers(dest='command', required=True)
    commands.add_parser('storage', help='show the project recording and execution-plan directories without creating them')
    commands.add_parser('tasks', help='list local named demonstrations')
    commands.add_parser('show', help='inspect saved steps and notes').add_argument('--task', required=True)
    commands.add_parser('plan', help='save a literal execution plan from recorded instructions').add_argument('--task', required=True)
    interpret = commands.add_parser('interpret', help='compile host-interpreted conditional steps for an existing recording')
    interpret.add_argument('--task', required=True)
    interpret.add_argument('--plan-file', type=Path, required=True, help='local declarative instruction JSON')
    for name in ('ground','bind-visual','bind-row'):
        tool = commands.add_parser(name, help='inspect or bind a visible target for one recorded instruction, without input')
        tool.add_argument('--task', required=True)
        tool.add_argument('--only', required=True)
        if name != 'ground':
            tool.add_argument('--frame', required=True, help='frame receipt from ground for this task and step')
            tool.add_argument('--point', nargs=2, type=int, required=True, metavar=('IMAGE_X','IMAGE_Y'))
        if name == 'bind-row':
            tool.add_argument('--row-point', nargs=2, type=int, required=True, metavar=('LABEL_X','LABEL_Y'),
                              help='point inside the identifying text on the same row as --point')
        if name == 'bind-visual':
            tool.add_argument('--save-as', required=True, help='new task name; preserves original')
    edit = commands.add_parser('instruct', help='save an interpreted instruction as a new task, preserving its source')
    edit.add_argument('--task', required=True)
    edit.add_argument('--only', required=True, help='recorded step number or alias')
    edit.add_argument('--text', required=True, help='original natural-language instruction')
    edit.add_argument('--mode', choices=('fixed', 'required', 'recorded'))
    edit.add_argument('--argument', help='literal value or required parameter name')
    edit.add_argument('--alias', help='explicitly associate this name with the recorded target')
    edit.add_argument('--target', help='interpreted current-screen field or command name when recording missed it')
    edit.add_argument('--role', help='interpreted target role, such as combobox or button')
    edit.add_argument('--row', type=int, help='visible list ordinal for a launch instruction interpreted by the host')
    edit.add_argument('--within', help='observed region to distinguish identical names')
    edit.add_argument('--wait-after', type=float, help='explicit seconds to wait after this button call')
    edit.add_argument('--save-as', required=True, help='new task name; never overwrite the original')
    for verb in ('windows', 'inspect', 'select', 'fill', 'open', 'record', 'replay', 'record-rows', 'row-status', 'row-launch'):
        s = commands.add_parser(verb)
        s.add_argument('--exe', required=True, help='exact executable basename')
        if verb != 'windows':
            s.add_argument('--window', required=True, help='exact business window title from windows')
            s.add_argument('--hwnd', type=int, help='disambiguate windows with identical titles')
        if verb in ('record', 'replay', 'record-rows', 'row-status'):
            s.add_argument('--task', required=True, help='local task name')
        if verb == 'record-rows':
            s.add_argument('--row', nargs=3, action='append', required=True, metavar=('LABEL', 'X', 'Y'),
                           help='one demonstrated row label and physical screen point per occurrence')
            for field in ('key', 'button', 'state'):
                s.add_argument('--' + field + '-box', nargs=4, type=int, required=True, metavar=('X', 'Y', 'W', 'H'),
                               help='physical pixel rectangle relative to the row')
        if verb == 'replay':
            s.add_argument('--set', action='append', default=[], metavar='FIELD=VALUE')
            s.add_argument('--input-plan', action='store_true', help='preview input modes without inspecting an app')
        if verb == 'row-launch':
            s.add_argument('--ordinal', required=True, type=int)
            s.add_argument('--preview', action='store_true', help='find the current row and play control without input')
        if verb in ('replay', 'select', 'fill', 'open', 'row-launch'):
            s.add_argument('--background-only', action='store_true', help='refuse physical input, foregrounding and restoration')
        if verb in ('replay', 'row-status'):
            s.add_argument('--only', help='run just one named step or step number')
        if verb in ('inspect', 'open', 'select', 'fill'):
            s.add_argument('--popup', type=int, help='owned popup from current inspect output')
            s.add_argument('--target', required=verb != 'inspect', help='field label, name or automation ID')
            s.add_argument('--within', help='observed region, ancestor ID or name')
            s.add_argument('--task', help='demonstration that contains the anchor')
            s.add_argument('--near', help='limit search to this demonstrated step’s current container')
        if verb in ('inspect', 'open'):
            s.add_argument('--role', help='e.g. combobox, button, menuitem, treeitem')
        if verb in ('select', 'fill'):
            s.add_argument('--value', required=True, help='literal text to enter' if verb == 'fill' else 'exact option text/code')
        if verb == 'open':
            s.add_argument('--expand', action='store_true', help='expand a submenu/tree item instead of invoking it')
    return p


def worker(args):
    try:
        result = replay_step() if args is None else run(args)
    except (SelectionError, bridge.BridgeError, tasks.TaskError, ValueError) as error:
        dispatched = getattr(error, 'input_dispatched', False)
        result = {'status': 'blocked' if dispatched is False else 'uncertain', 'diagnostic': getattr(error, 'code', 'invalid_request'),
                  'input_dispatched': dispatched}
    print(json.dumps(result, ensure_ascii=False))
    return result_code(result)


def result_code(result):
    if result['status'] in ('verified', 'resolved', 'observed', 'recorded', 'bound', 'completed', 'planned'):
        return 0
    return 2 if result['status'] in ('blocked', 'cancelled', 'needs_input', 'needs_review',
                                     'needs_grounding', 'needs_scene') else 3


def worker_receipt(argv, mutating, payload=None):
    # A third-party accessibility provider can hang inside a COM call. Terminate
    # our helper only; never assume a timed-out mutation failed or repeat it.
    try:
        kind = (payload or {}).get('step', {}).get('action')
        timeout = {'row-action': 120, 'row-launch': 120, 'result-menu': 160}.get(kind, 90)
        if argv == ['_plan']:
            from python_workflow import worker_timeout
            timeout = worker_timeout(payload['recipe'])
        child = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(Path(__file__).resolve()), *argv],
                               input=json.dumps(payload) if payload is not None else None,
                               capture_output=True, encoding='utf-8', timeout=timeout)
        receipt = json.loads(child.stdout)
        if not isinstance(receipt, dict) or 'status' not in receipt or child.returncode not in (0, 2, 3):
            raise ValueError()
        return receipt
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        diagnostic = 'provider_timeout_or_interrupted'
    except (OSError, ValueError):
        diagnostic = 'provider_unavailable'
    return {'status': 'uncertain' if mutating else 'blocked', 'diagnostic': diagnostic,
            'input_dispatched': None if mutating else False}


def supervise(args, argv):
    mutating = args.command in ('open','select','fill') or (args.command == 'row-launch' and not args.preview)
    receipt = worker_receipt(['_worker', *argv], mutating=mutating)
    print(json.dumps(receipt, ensure_ascii=False))
    return result_code(receipt)


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    argv = sys.argv[1:]
    if argv == ['_plan']:
        import python_workflow
        try:
            payload = json.load(sys.stdin)
            result = python_workflow.run(payload['recipe'], payload['overrides'], hwnd=payload.get('hwnd'),
                                         allow_recorded_selection=True,
                                         background_only=payload.get('background_only', False), only=payload.get('only'))
        except Exception as error:
            result = dict(status='uncertain', diagnostic=getattr(error, 'code', 'execution_plan_unavailable'),
                          input_dispatched=None)
        print(json.dumps(result, ensure_ascii=False))
        return result_code(result)
    if argv == ['_step']:
        return worker(None)
    is_worker = argv[:1] == ['_worker']
    if is_worker:
        argv = argv[1:]
    args = parser().parse_args(argv)
    if args.command in ('inspect', 'select', 'fill', 'open') and (
            bool(args.task) != bool(args.near) or (args.popup is not None and args.near)):
        print(json.dumps({'status': 'blocked', 'diagnostic': 'near_requires_task_and_main_window', 'input_dispatched': False}))
        return 2
    if args.command == 'inspect' and not args.target and (args.within or args.role):
        print(json.dumps({'status': 'blocked', 'diagnostic': 'specify_target_with_filter', 'input_dispatched': False}))
        return 2
    # Recording lasts as long as the user demonstrates. Replay has one timeout
    # per direct step, with the original window lifetime pinned throughout.
    return worker(args) if is_worker or args.command in ('record', 'record-rows', 'replay', 'tasks', 'show', 'plan', 'instruct', 'storage') else supervise(args, argv)


if __name__ == '__main__':
    raise SystemExit(main())
