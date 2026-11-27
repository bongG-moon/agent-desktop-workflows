"""Direct Windows actions and named demonstrations using the same live targets."""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import ExitStack
from copy import deepcopy
import ctypes as C
from ctypes import wintypes as W
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time

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
import review_drafts
import input_policy
import coordination


def windows(exe, *, only_hwnd=None):
    """Read captions only in the requested app; a specific top-level HWND needs no inventory."""
    bridge._windows()
    if not exe or Path(exe).name != exe or not exe.casefold().endswith('.exe'):
        raise SelectionError('exact_exe_name_required')
    records, truncated = [], False

    def visit(hwnd, _):
        nonlocal truncated
        hwnd = bridge.normalize_hwnd(hwnd)
        if not hwnd or not bridge.user32.IsWindowVisible(hwnd):
            return True
        pid = bridge.hwnd_pid(hwnd)
        try:
            actual, started = bridge._process_identity(pid)
        except (bridge.BridgeError, OSError):
            return True
        if actual.casefold() == exe.casefold():
            records.append({'exe_name': actual, 'pid': pid, 'hwnd': hwnd,
                            'title': bridge.window_text(hwnd), 'process_started_utc_ticks': started})
            if len(records) >= 256:
                truncated = True
                return False
        return True

    if only_hwnd is not None:
        only_hwnd = bridge.normalize_hwnd(only_hwnd)
        if only_hwnd and bridge.root_window(only_hwnd) == only_hwnd:
            visit(only_hwnd, 0)
        return records
    callback_type = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    enum = bridge._sig(bridge.user32, 'EnumWindows', W.BOOL, callback_type, W.LPARAM)
    if not enum(callback_type(visit), 0) or truncated:
        raise SelectionError('window_list_incomplete')
    return records


def pick_window(records, title, hwnd):
    if not title.strip():
        raise SelectionError('exact_business_window_title_required')
    if hwnd is not None:
        hwnd = bridge.normalize_hwnd(hwnd)
        if not hwnd:
            raise SelectionError('invalid_target')
    matches = [r for r in records if r['title'] == title and (hwnd is None or r['hwnd'] == hwnd)]
    if len(matches) != 1:
        raise SelectionError('ambiguous_window' if matches else 'window_not_found')
    return matches[0]


def guard(owner, surface, title, mutate=False):
    owner.check()
    if surface is not owner:
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
    selected = bridge.normalize_hwnd(selected)
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


LIST_ROLES = {'button', 'splitbutton', 'hyperlink', 'combobox', 'menuitem', 'treeitem', 'tab', 'table', 'toolbar', 'textfield', 'checkbox'}


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
                capture_storage=tasks.storage_paths.capture_info(directory),
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
    mutate = args.command in {'select', 'fill', 'open'}
    if background and mutate:
        input_policy.window_ready(owner)
    surface = bind_surface(owner, args.popup)
    def check():
        guard(owner, surface, args.window, mutate)
        if background and mutate:
            input_policy.window_ready(surface)
        elif mutate and bridge.user32.IsIconic(surface.hwnd):
            raise bridge.BridgeError('target_window_minimized')
    check()
    if not mutate:
        visibility.require_observable(surface)
    with bridge.UIA() as uia:
        if mutate:
            if background:
                input_policy.configure_uia(uia)
            else:
                input_policy.prefer_background(uia)
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
                    physical_input_required=input_policy.profile(recipe['steps'])['physical_input_required'],
                    required_parameters=list(dict.fromkeys(s['parameter'] for s in recipe['steps'] if 'parameter' in s)))
        if envelope.get('notes'):
            info['notes'] = envelope['notes']
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
    source = tasks.load_task(args.task, task_directory(), allow_review=True)
    if source.get('kind') == review_drafts.KIND:
        # Ground the user's original double-click directly from the editor draft.
        # Dispatching a generic button action or surveying every other row cannot bind it.
        rows = source['rows']
        number = tasks._step_number({'steps': [row['form'] for row in rows]}, args.only)
        row = rows[number-1]
        stored_text = row['form']['text'].strip()
        if (tasks.is_double_click_step(row['step'].get('saved',row['step'])) and tasks.double_click_instruction(stored_text)
                and args.text.strip() == stored_text):
            from recovery import double_click_handoff
            return dict(status='needs_grounding', diagnostic='double_click_target_requires_grounding',
                        source_task=source['name'], step=number, input_dispatched=False,
                        **double_click_handoff(source['name'],number))
        result = review_drafts.receipt(source, 'needs_review')
        result['save_as'] = args.save_as
        return attach_recovery(result, args.task)
    plan = deepcopy(source)
    tasks.check_new_task(args.save_as, task_directory())
    number = tasks._step_number(plan, args.only)
    step = plan['steps'][number - 1]
    if (tasks.is_double_click_step(step) and step.get('action') == 'manual'
            and tasks.double_click_instruction(args.text) and args.text.strip() == step.get('instruction_draft')
            and any(value is not None for value in (args.target,args.role,getattr(args,'row',None)))):
        from recovery import double_click_handoff
        return dict(status='needs_grounding',diagnostic='double_click_target_requires_grounding',
                    source_task=source['name'],step=number,input_dispatched=False,
                    **double_click_handoff(source['name'],number))
    if args.alias is not None:
        step['alias'] = args.alias
    ordinal = getattr(args, 'row', None)
    if ordinal is not None:
        from row_intent import confirmed_launch_hint, conflicts_with_launch, parse
        interpreted = parse(args.text)
        legacy_ordinal = tasks.legacy_row_button_ordinal(source['steps'][number - 1])
        if conflicts_with_launch(args.text) or (interpreted is not None and interpreted != ordinal):
            raise tasks.TaskError('instruction_binding_conflict')
        if legacy_ordinal is not None and (legacy_ordinal != ordinal or interpreted != ordinal):
            raise tasks.TaskError('instruction_binding_conflict')
        if (not 1 <= ordinal <= 64 or not tasks._text(args.text, 1000)
                or any(v is not None for v in (args.mode, args.argument, args.target, args.role, args.within, args.wait_after))
                or not (legacy_ordinal or tasks.is_recoverable(step) or step.get('action') in ('row-action','row-launch'))):
            raise tasks.TaskError('invalid_row_instruction')
        evidence = tasks._recorded_row(step)
        hint = evidence['row'] if evidence and evidence['row']['version'] == 1 else None
        if legacy_ordinal:
            context = step.get('window_context')
            exe, title = (context['exe'], context['title']) if context else (source['exe'], source['window'])
            hint = confirmed_launch_hint(exe, title, ordinal, task_directory())
        original = step
        step = dict(action='row-launch', ordinal=ordinal, label=f'{ordinal}번째 행 실행',
                    **tasks._annotations(original))
        step['description'] = args.text.strip()
        if 'row_cue' in original:
            step['row_cue'] = deepcopy(original['row_cue'])
        if evidence and evidence['row']['version'] == 2:
            step['recorded_row'] = deepcopy(evidence)
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
        selected = tasks._step_number(plan, args.only) if args.only is not None else None
        rows = tasks.resolve_saved_intents(plan)['steps']
        if any(s['action'] in (*tasks.OBSERVE_ACTIONS, 'close-popup', 'double-click-result', 'submit', 'press-key')
               for n, s in enumerate(rows, 1) if selected is None or selected == n):
            return dict(status='needs_review', diagnostic=saved.get('diagnostic', 'execution_plan_required'),
                        stopped_at=saved.get('step'), steps=[], input_dispatched=False)
        return None
    source_steps = saved['envelope']['source_steps']
    has_host_binding = any(step['action'] in ('conditional-open', 'row-open') for step in recipe['steps'])
    requires_plan = any(step['action'] in (*tasks.OBSERVE_ACTIONS, 'double-click-result', 'submit', 'press-key')
                        or step.get('verify_closed') for step in recipe['steps'])
    if requires_plan and overrides:
        # Keep field aliases when a later step requires the literal plan executor.
        declared = {s['parameter'].casefold() for s in recipe['steps'] if 'parameter' in s}
        aliases = {key: value for key, value in overrides.items() if key.casefold() not in declared}
        if aliases:
            numbered = list(enumerate(plan['steps'], 1))
            assignments = tasks.recorded_overrides(numbered, aliases)
            overrides = {k:v for k,v in overrides.items() if k not in aliases}
            names = {k.casefold() for k in overrides}
            for position, value in assignments.items():
                key = tasks._parameter(plan['steps'][position - 1])
                if not key or key.casefold() in names:
                    raise tasks.TaskError('duplicate_override')
                names.add(key.casefold())
                overrides[key] = value
    if legacy_values and not (has_host_binding or requires_plan):
        return None
    if requires_plan and legacy_values:
        # Keep legacy recorded defaults, including when a result assertion
        # references the same parameter. An explicit required input still wins.
        recipe = deepcopy(recipe)
        defaults = {}
        required = {s['instruction']['parameter'].casefold() for s in plan['steps']
                    if s.get('instruction', {}).get('mode') == 'required'}
        for raw in plan['steps']:
            if raw.get('action') in tasks.VALUE_ACTIONS and 'instruction' not in raw and 'value' in raw and raw.get('parameter'):
                defaults.setdefault(raw['parameter'].casefold(), set()).add(raw['value'])
        supplied = {key.casefold() for key in overrides}
        for step in recipe['steps']:
            key = step.get('parameter', '').casefold()
            if key in defaults and key not in supplied | required and len(defaults[key]) == 1:
                step['value'] = next(iter(defaults[key]))
                del step['parameter']
    elif has_host_binding and not requires_plan:
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
        if len(matches) != 1:
            return None
        chosen = recipe['steps'][matches[0]]
        if chosen['action'] == 'double-click-result':
            return dict(status='blocked', diagnostic='verified_result_row_required', input_dispatched=False, steps=[])
        if (chosen['action'] not in ('conditional-open','row-open','submit','press-key', *tasks.OBSERVE_ACTIONS)
                and not chosen.get('verify_closed') and not tasks.is_double_click_step(chosen)):
            return None
        only = str(matches[0] + 1)
        active = dict(recipe, steps=[recipe['steps'][matches[0]]])
    parameters = {s['parameter'].casefold() for s in active['steps'] if 'parameter' in s}
    if any(key.casefold() not in parameters for key in overrides):
        if requires_plan:
            raise tasks.TaskError('unknown_parameter')
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
    from window_context import WindowRoutes, ContextError, effect_scopes
    steps = plan['steps']
    active = [tasks._step_number(plan,args.only)] if args.only is not None else range(1,len(steps)+1)
    numbers = iter(active)
    scope_numbers = effect_scopes(steps, active)
    linked = any('window_context' in steps[n-1] for n in scope_numbers)
    main = routes = None
    held_windows = set()

    def invoke(step):
        nonlocal main, routes
        number = next(numbers)
        try:
            if main is None:
                main = pick_window(windows(args.exe), args.window, args.hwnd)
                contexts = [steps[n-1]['window_context'] for n in scope_numbers if 'window_context' in steps[n-1]]
                # Per-step workers run their result-menu under these parent-held
                # reservations, including its leaf baseline and workbook check.
                leases.enter_context(coordination.reserve_outputs(steps[n-1] for n in active))
                leases.enter_context(coordination.window(main, args.window, contexts))
                if linked:
                    routes = WindowRoutes(steps,bridge.Binding(main),args.window,active=scope_numbers)
                    coordination.reserve_handles(leases, held_windows,
                                                 (bound.hwnd for bound in routes.bindings.values() if bound is not None))
            target = main
            context = step.get('window_context')
            if routes:
                source_window = None
                if context:
                    source_window = routes.resolve(context)
                    if source_window.hwnd not in held_windows:
                        leases.enter_context(coordination.handle(source_window))
                        held_windows.add(source_window.hwnd)
                        routes.check(context, source_window)
                    target = _linked_record(source_window,context['title'])
                routes.before(number,source_window)
        except (SelectionError, bridge.BridgeError, ContextError, coordination.CoordinationError) as error:
            return dict(status='blocked',diagnostic=error.code,input_dispatched=False)
        payload = dict(binding=target, step=step, background_only=background)
        if context:
            payload['main_window'] = main
            payload['opener_window'] = _opener_record(routes, context)
        receipt = worker_receipt(['_step'], mutating=True, payload=payload)
        if routes:
            routes.after(number,receipt)
            invoked = (step['action']=='open' and not step.get('expand')
                       and receipt.get('diagnostic')=='business_result_not_verified')
            double_clicked = tasks.is_double_click_step(step) and receipt.get('diagnostic')=='visual_double_click_dispatched'
            if (routes.origins.get(number) and (invoked or double_clicked)
                    and receipt.get('status')=='delivered_unverified' and receipt.get('input_dispatched') is True):
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
    # Keep the workflow reservation in the parent across legacy per-step workers.
    # Validation, including missing variables, finishes before invoke acquires it.
    with ExitStack() as leases:
        return tasks.replay(plan,args.exe,args.window,overrides,invoke,only=args.only)


def edit_recording(owner, storage, *, report_ready=False):
    try:
        import tkinter
    except ImportError as error:
        raise SelectionError('recorder_tk_unavailable') from error
    from teach_capture import capture
    focus = (storage.previous or storage.source or {}).get('step_number')

    def ready():
        # Escaped JSON also round-trips through Windows PowerShell's legacy output code page.
        print(json.dumps(dict(status='recorder_ready', task=storage.name, window=storage.title,
                              input_dispatched=False)), flush=True)

    def locate_window(scope):
        exe, title = (scope['exe'], scope['title']) if scope else (storage.exe, storage.title)
        current = bridge.Binding(pick_window(windows(exe), title, None))
        guard(current, current, title)
        return current

    try:
        result = capture(owner, title=storage.title, initial_draft=storage.initial_rows(), initial_step=focus,
                         save_recording=storage.save_recording, save_draft=storage.save_draft,
                         on_ready=ready if report_ready else None, locate_window=locate_window)
    except tkinter.TclError as error:
        raise SelectionError('recording_ui_failed') from error
    if storage.saved_plan is not None:
        try:
            result['execution_plan'] = execution_plan_info(saved_execution_plan(storage.saved_plan))
        except Exception as error:
            # The task is already on disk. Keep its receipt if deriving the plan fails.
            result['execution_plan'] = dict(status='unavailable',
                                            diagnostic=getattr(error, 'code', 'execution_plan_unavailable'))
    if (result.get('status') == 'recorded' and
            (result.get('draft') or result.get('execution_plan', {}).get('status') != 'saved')):
        # Saving ends the demonstration. Only a requested trial needs to inspect
        # anything the captured evidence could not resolve.
        result.update(host_action='test_saved_recording', first_trial_args=[
            'try-recording', '--task', storage.name, '--summary'])
        result.pop('recover_args', None)
        result.pop('resume_args', None)
    return result


def record_application(hint, name=None):
    """Resolve a human app name once, then use the existing editor and save path."""
    from datetime import datetime
    from uuid import uuid4
    from recorder_targets import inventory, choose_window

    if hint is not None and not tasks._text(hint, 200):
        raise SelectionError('invalid_recorder_app')
    if name is not None:
        tasks._name(name)
    bridge.set_dpi_awareness()

    def selecting():
        print(json.dumps(dict(status='recorder_selection_required', app=hint,
                              input_dispatched=False)), flush=True)

    row = choose_window(hint or '', inventory(), on_ready=selecting)
    if row is None:
        return dict(status='cancelled', input_dispatched=False)
    # Pin the inventory's process lifetime and preserve the actual caption,
    # including whitespace. A stale HWND must not bind to a replacement app.
    owner = bridge.Binding(row)
    name = name or f"녹화 {datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"
    return record_editor(owner, name, row['exe_name'], row['title'], report_ready=True)


def record_editor(owner, name, exe, title, *, report_ready=False):
    try:
        storage = review_drafts.ReviewStorage(name, exe, title, task_directory())
    except tasks.TaskError as error:
        if error.code != 'task_already_exists':
            raise
        source = tasks.load_task(name, task_directory())
        tasks.check_binding(source, exe, title)
        return dict(status='needs_review', diagnostic=error.code, task=tasks.summary(source),
                    host_action='continue_recording_from_saved_task',
                    continue_args=['continue-recording', '--task', name], input_dispatched=False)
    guard(owner, owner, title)
    return edit_recording(owner, storage, report_ready=report_ready)


def attach_recovery(result, name, *, prepare=False):
    """After an incomplete recording, give the host current app evidence in the same response."""
    if prepare and result.get('status') == 'recorded' and result.get('draft'):
        import first_trial
        directory = task_directory()
        doc = tasks.load_task(name, directory, allow_review=True)
        if doc.get('kind') == review_drafts.KIND:
            local = first_trial._compiled(doc, directory)
            if local.get('envelope'):
                info = execution_plan_info(local)
                ready = dict(result, preparation='ready', effective_task=name,
                             next_action=dict(kind='run_prepared_recording', script=str(Path(__file__).absolute()),
                                 args=['try-recording', '--task', name, '--summary'], required_parameters=info['required_parameters'],
                                 physical_input_required=info['physical_input_required']),
                             host_action='test_saved_recording')
                ready.pop('recover_args', None)
                return ready
    result['recover_args'] = ['recover', '--task', name]
    if prepare:
        result['recover_args'].append('--prepare')
    result['live_recovery'] = worker_receipt(['_worker', *result['recover_args']], mutating=False)
    if result.get('input_dispatched') is not False:
        result['host_action'] = 'check_current_effect_before_continuing'
    elif prepare:
        if result['live_recovery'].get('host_action') == 'use_prepared_task':
            result['host_action'] = 'use_prepared_task'
        elif result['live_recovery'].get('host_action') == 'ground_recorded_double_click':
            result.update(host_action='ground_recorded_double_click', ground_args=result['live_recovery']['ground_args'],
                          capture_requires_consent=True)
        else:
            result.setdefault('host_action', 'bind_recorded_instructions_from_live_scopes')
    return result


def recorder_request(path):
    """Read literal host data, leaving quotes and whitespace out of the shell command."""
    try:
        tasks._directory(path.parent)
        tasks._deny_redirect(path)
        if not path.is_file():
            raise SelectionError('recorder_request_unreadable')
        with path.open('rb') as stream:
            data = stream.read(65537)
    except OSError as error:
        raise SelectionError('recorder_request_unreadable') from error
    if len(data) > 65536:
        raise SelectionError('recorder_request_too_large')
    try:
        request = json.loads(data.decode('utf-8-sig'), object_pairs_hook=tasks._unique_keys)
    except (ValueError, RecursionError) as error:
        raise SelectionError('invalid_recorder_request') from error

    fields = {
        'record': ({'command', 'task', 'exe'}, {'window': None, 'hwnd': None}),
        'review': ({'command', 'task'}, {'edit': False, 'restart': False}),
        'continue-recording': ({'command', 'task'}, {'from_step': None, 'supplement': None, 'save_as': None}),
    }
    if not isinstance(request, dict) or not isinstance(request.get('command'), str) or request['command'] not in fields:
        raise SelectionError('invalid_recorder_request')
    required, defaults = fields[request['command']]
    if not required <= request.keys() <= required.union(defaults):
        raise SelectionError('invalid_recorder_request')
    for key, value in request.items():
        if key in ('edit', 'restart'):
            if type(value) is not bool:
                raise SelectionError('invalid_recorder_request')
        elif key == 'hwnd':
            request[key] = bridge.normalize_hwnd(value)
            if not request[key]:
                raise SelectionError('invalid_recorder_request')
        elif key == 'from_step':
            if type(value) is not int or value < 1:
                raise SelectionError('invalid_recorder_request')
        elif not tasks._text(value):
            raise SelectionError('invalid_recorder_request')
    if request.get('edit') and request.get('restart'):
        raise SelectionError('invalid_recorder_request')
    return argparse.Namespace(**{**defaults, **request}, report_ready=True)


def run(args):
    if args.command == 'try-recording':
        import first_trial
        app = sys.modules[__name__]
        result = first_trial.run(app, args)
        if (getattr(args, 'local_result_review', False) and args.allow_physical
                and result.get('result_inspection', {}).get('local_resume_args')
                and result.get('host_action') == 'confirm_result_locally_without_replaying'):
            import result_trial
            return result_trial.review_locally(app, result['trial_receipt'], True)
        return result
    if args.command == 'inspect-trial':
        import first_trial
        return first_trial.inspect_result(sys.modules[__name__], args.receipt)
    if args.command == 'resume-trial':
        import result_trial
        if getattr(args, 'local_result_review', False):
            if any(v is not None for v in (args.point, args.column, args.value, args.row_policy)):
                raise SelectionError('invalid_result_review_options')
            return result_trial.review_locally(sys.modules[__name__], args.receipt, args.allow_physical)
        if args.point is None:
            raise SelectionError('result_point_required')
        return result_trial.resume(sys.modules[__name__], args)
    if args.command in ('inspect-menu-trial', 'resume-menu-trial'):
        import trial_menu
        return trial_menu.continue_menu(sys.modules[__name__], args, inspect=args.command == 'inspect-menu-trial')
    if args.command == '_record_app':
        result = record_application(args.app, args.task)
        if result.get('status') == 'recorded' and getattr(args, 'session', None):
            from recorder_launch import after_save
            return after_save(sys.modules[__name__], args.session, result)
        return result
    if args.command == 'recorder':
        from recorder_launch import launch, status
        if args.session is not None:
            if args.task is not None:
                raise SelectionError('invalid_recorder_arguments')
            return status(args.session, wait_seconds=args.wait) if args.wait is not None else status(args.session)
        if args.wait is not None or (args.request is not None and args.task is not None):
            raise SelectionError('invalid_recorder_arguments')
        if args.request is None:
            return launch(args.app, task=args.task)
        args = recorder_request(args.request)
    if args.command == 'storage':
        return storage_info()
    if args.command == 'review':
        directory = task_directory()
        doc = tasks.read_document(args.task, directory)
        draft = doc.get('kind') == review_drafts.KIND
        edit, restart = getattr(args, 'edit', False), getattr(args, 'restart', False)
        if draft:
            review_drafts.validate(doc)
        else:
            tasks._validate(doc)
            if not edit and not restart:
                return dict(status='observed', task=tasks.summary(doc), diagnostic='task_already_saved',
                            edit_args=['review', '--task', args.task, '--edit'],
                            restart_args=['review', '--task', args.task, '--restart'],
                            continue_args=['continue-recording', '--task', args.task],
                            recover_args=['recover', '--task', args.task], input_dispatched=False)
        bridge.set_dpi_awareness()
        owner = None
        if edit or restart:
            from continue_recording import _new_name
            name = _new_name(doc['name'], '다시 녹화' if restart else '수정본', directory)
            storage = review_drafts.ReviewStorage(name, doc['exe'], doc['window'], directory,
                                                  source=None if restart else doc, new_only=True)
            if restart:
                owner = bridge.Binding(pick_window(windows(doc['exe']), doc['window'], None))
                guard(owner, owner, doc['window'])
        else:
            storage = review_drafts.ReviewStorage(args.task, doc['exe'], doc['window'], directory, existing=doc)
        return edit_recording(owner, storage, report_ready=getattr(args, 'report_ready', False))
    bridge.set_dpi_awareness()
    if args.command == 'continue-recording':
        import continue_recording
        return continue_recording.run(sys.modules[__name__], args)
    if args.command == 'recover':
        import recovery
        return recovery.run(sys.modules[__name__], args)
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
    if args.command in ('plan', 'show', 'replay'):
        plan = tasks.load_task(args.task, task_directory(), allow_review=True)
        if plan.get('kind') == review_drafts.KIND:
            if args.command == 'replay' and args.only is not None:
                import recovery
                selected = recovery.selected_draft_double(plan, args.only)
                if selected is not None:
                    single, number = selected
                    tasks.check_binding(single,args.exe,args.window)
                    requirements = input_policy.profile([single['steps'][number-1]], [number])
                    if args.input_plan:
                        return dict(status='observed', input_dispatched=False, input_requirements=requirements)
                    if args.background_only:
                        return input_policy.blocked(requirements)
                    report = _replay_recorded(single,args,tasks.parse_overrides(args.set),False)
                    recovery.missing_double_target(report,single)
                    return dict(report, draft=True, tested_step=number, input_requirements=requirements)
            result = review_drafts.receipt(plan, 'observed' if args.command == 'show' else 'needs_review')
            if args.command == 'replay' and not getattr(args, 'input_plan', False):
                if args.exe.casefold() != plan['exe'].casefold() or args.window != plan['window']:
                    raise tasks.TaskError('recorded_window_mismatch')
                return attach_recovery(result, args.task, prepare=not (args.only or args.background_only))
            return result
    if args.command == 'plan':
        result = saved_execution_plan(plan)
        return dict(status='planned' if result.get('envelope') else 'needs_review',
                    task=tasks.summary(plan), execution_plan=execution_plan_info(result), input_dispatched=False)
    if args.command == 'tasks':
        return {'status': 'observed', 'tasks': tasks.list_tasks(task_directory()), 'input_dispatched': False}
    if args.command == 'show':
        return {'status': 'observed', 'task': tasks.summary(plan), 'input_dispatched': False}
    if args.command == 'replay':
        original = plan
        if getattr(args, 'input_plan', False):
            return dict(status='observed', input_dispatched=False,
                        input_requirements=replay_input_requirements(original, args.only))
        background = getattr(args, 'background_only', False)
        requirements = None
        if background:
            requirements = replay_input_requirements(original, args.only)
            refusal = input_policy.blocked(requirements)
            if refusal:
                return refusal
        overrides = tasks.parse_overrides(args.set)
        quick = fast_replay(original, args, overrides)
        if quick is None:
            plan = tasks.resolve_saved_intents(original)
            if requirements is None:
                requirements = replay_input_requirements(original, args.only)
            report = _replay_recorded(plan, args, overrides, background)
            if report.get('diagnostic') == 'instruction_requires_structure':
                report['host_action'] = 'interpret_recorded_instructions'
            quick = dict(report, input_policy='background_only' if background else 'auto', input_requirements=requirements)
        if quick.get('diagnostic') == 'query_result_freshness_unproven' and quick.get('source_stopped_at'):
            quick['test_step_args'] = ['replay', '--task', original['name'], '--only', str(quick['source_stopped_at']),
                                       '--exe', args.exe, '--window', args.window]
            return quick
        if quick.get('diagnostic','') and quick['diagnostic'].startswith('visual_target_'):
            from recovery import missing_double_target
            if missing_double_target(quick,original):
                return quick
        missing_target = quick.get('diagnostic') in (
            'target_missing', 'target_ambiguous', 'target_grounding_required', 'new_context_menu_item_missing',
            'context_menu_item_changed', 'recorded_window_not_open')
        if quick['status'] in ('needs_grounding', 'needs_review') or missing_target:
            return attach_recovery(quick, args.task, prepare=quick.get('input_dispatched') is False
                                   and not (quick.get('grounding_steps') or args.only or background))
        return quick
    if args.command == 'record' and args.hwnd is not None:
        rows = windows(args.exe, only_hwnd=args.hwnd)
    else:
        rows = windows(args.exe)
    if args.command == 'windows':
        return {'status': 'observed', 'windows': rows, 'input_dispatched': False}
    if args.command == 'record' and getattr(args, 'report_ready', False):
        choices = [dict(title=r['title'], hwnd=r['hwnd']) for r in rows
                   if tasks._text(r['title']) and (args.window is None or r['title'] == args.window)]
        if not choices:
            raise SelectionError('window_not_found')
        if len(choices) > 1:
            return dict(status='needs_input', diagnostic='ambiguous_window', windows=choices,
                        input_dispatched=False)
        args.window = choices[0]['title']
    binding = pick_window(rows, args.window, args.hwnd)
    owner = bridge.Binding(binding)
    if args.command == 'row-launch':
        from row_intent import execute
        if args.background_only and not args.preview:
            return input_policy.blocked(input_policy.profile([{'action':'row-launch'}]))
        with ExitStack() as leases:
            if not args.preview:
                leases.enter_context(coordination.window(owner, args.window))
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
        return record_editor(owner, args.task, args.exe, args.window,
                             report_ready=getattr(args, 'report_ready', False))
    anchor = context = None
    if getattr(args, 'near', None) is not None:
        plan = tasks.load_task(args.task, task_directory())
        tasks.check_binding(plan, args.exe, args.window)
        anchor = tasks.get_step(plan, args.near)
        context = anchor.get('window_context')
    with ExitStack() as leases:
        mutate = args.command in ('select', 'fill', 'open')
        if mutate:
            scopes = [context] if context else []
            if args.popup is not None:
                if not owner.contains_window(args.popup) or not bridge.owned(args.popup, owner.hwnd):
                    raise SelectionError('popup_not_owned_or_closed')
                scopes.append(dict(exe=owner.exe, title=bridge.window_text(args.popup)))
            leases.enter_context(coordination.window(owner, args.window, scopes))
            if args.popup is not None:
                leases.enter_context(coordination.handle({'hwnd': args.popup}))
                if (not owner.contains_window(args.popup) or not bridge.owned(args.popup, owner.hwnd)
                        or bridge.window_text(args.popup) != scopes[-1]['title']):
                    raise SelectionError('popup_not_owned_or_closed')
        if context:
            from window_context import WindowRoutes
            routes = WindowRoutes(plan['steps'],owner,args.window,read_only=args.command=='inspect',
                                  active=[tasks._step_number(plan,args.near)])
            found = routes.resolve(context)
            if found is None:
                raise SelectionError('recorded_window_not_open')
            if mutate:
                leases.enter_context(coordination.handle(found))
                routes.check(context, found)
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
        if step['context']['trigger'] and bridge.user32.IsIconic(owner.hwnd):
            # This compound gesture observes a visible query-completion indicator before opening the menu.
            visibility.restore_for_action(owner, binding['title'])
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
    host = commands.add_parser('recorder', help='open a recorder by app name and return when visible; or use a literal JSON request')
    start = host.add_mutually_exclusive_group()
    start.add_argument('--app', help='human app name or title fragment; opens a window picker if ambiguous')
    start.add_argument('--session', help='read the saved or cancelled result of an earlier recorder launch')
    start.add_argument('--request', type=Path, help='UTF-8 JSON file for record, review or continue-recording (streaming)')
    host.add_argument('--task', help='optional name for a new recording; defaults to a new timestamped name')
    host.add_argument('--wait', type=float, help='with --session, wait up to 30 seconds only for startup or saved-draft processing')
    host.add_argument('--summary', action='store_true', help='return the recording status and its next action')
    trial = commands.add_parser('try-recording', help='test the saved recording once, without registering a skill')
    source = trial.add_mutually_exclusive_group(required=True)
    source.add_argument('--session', help='recorder session from this project')
    source.add_argument('--task', help='saved recording name')
    trial.add_argument('--set', action='append', default=[], metavar='FIELD=VALUE')
    policy = trial.add_mutually_exclusive_group()
    policy.add_argument('--allow-physical', action='store_true')
    policy.add_argument('--background-only', action='store_true')
    trial.add_argument('--hwnd', type=int, help='disambiguate identical business windows')
    trial.add_argument('--trial-id', help='reuse the same ID on interruption; use a new ID for a deliberate repeat')
    trial.add_argument('--auto-repair-frame', action='store_true',
                       help='include an approved local menu-area image only after a before-click position failure')
    trial.add_argument('--local-result-review', action='store_true', help='pick a missing recorded row position on this PC and continue the same trial')
    inspection = commands.add_parser('inspect-trial', help='capture the still-open result window from a paused trial without sending input')
    inspection.add_argument('--receipt', required=True)
    resume = commands.add_parser('resume-trial', help='continue from a unique result cell confirmed in a scoped trial image')
    resume.add_argument('--receipt', required=True)
    selection = resume.add_mutually_exclusive_group(required=True)
    selection.add_argument('--frame')
    selection.add_argument('--local-result-review', action='store_true')
    resume.add_argument('--column')
    resume.add_argument('--value')
    resume.add_argument('--row-policy', choices=('single', 'first', 'recorded'), help='confirm visible rows without a value comparison')
    resume.add_argument('--point', type=int, nargs=2)
    resume.add_argument('--allow-physical', action='store_true')
    trial_commands = [trial, inspection, resume]
    for verb in ('inspect-menu-trial', 'resume-menu-trial'):
        menu_trial = commands.add_parser(verb, help='continue a first-trial menu without repeating prior actions')
        trial_commands.append(menu_trial)
        menu_trial.add_argument('--receipt', required=True)
        menu_trial.add_argument('--allow-physical', action='store_true')
        menu_trial.add_argument('--set', action='append', default=[], metavar='FIELD=VALUE')
        if verb == 'resume-menu-trial':
            menu_trial.add_argument('--frame')
            menu_trial.add_argument('--point', type=int, nargs=2)
    for command in trial_commands:
        command.add_argument('--summary', action='store_true', help='return the trial result or a compact preparation diagnostic')
    commands.add_parser('storage', help='show the project recording and execution-plan directories without creating them')
    commands.add_parser('tasks', help='list local named demonstrations')
    commands.add_parser('show', help='inspect saved steps and notes').add_argument('--task', required=True)
    review = commands.add_parser('review', help='reopen a recording for review')
    review.add_argument('--task', required=True)
    revision = review.add_mutually_exclusive_group()
    revision.add_argument('--edit', action='store_true', help='edit a copy of the saved recording or draft')
    revision.add_argument('--restart', action='store_true', help='start a fresh copy in the same application window')
    repair = commands.add_parser('recover', help='inspect the live task windows or bind interpreted steps into a new task')
    repair.add_argument('--task', required=True)
    repair.add_argument('--only', type=int, help='observe one original step')
    repair.add_argument('--image', action='store_true', help='also capture the selected step window locally')
    repair.add_argument('--bindings', type=Path, help='local JSON with source_fingerprint and interpreted steps')
    repair.add_argument('--prepare', action='store_true', help='resolve the recording once and save a ready copy without input')
    repair.add_argument('--restore-window', action='store_true', help='permit restoring the identified minimized application window for observation')
    repair.add_argument('--save-as', help='derived task name when applying bindings')
    continuation = commands.add_parser('continue-recording', help='record or merge a supplement while preserving the source')
    continuation.add_argument('--task', required=True)
    continuation.add_argument('--from-step', type=int, help='replace the tail starting at this step; defaults to append')
    continuation.add_argument('--supplement', help='merge a previously saved supplement instead of opening a recorder')
    continuation.add_argument('--save-as', help='optional name for the combined task')
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
        if result['diagnostic'] == 'unknown_parameter' and hasattr(error, 'required_parameters'):
            result['required_parameters'] = error.required_parameters
    if result.get('status') in ('recorded', 'needs_grounding'):
        from recording_followup import followup
        result = followup(result)
    if getattr(args, 'summary', False):
        from first_trial import host_summary
        result = host_summary(result)
    print(json.dumps(result, ensure_ascii=args is not None and args.command == 'recorder'))
    return result_code(result, summary=getattr(args, 'summary', False))


def result_code(result, *, summary=False):
    if (summary and result['status'] == 'completed_unverified' and 'stopped_at' not in result
            and result.get('outcome') == 'workbook_verified'):
        return 0
    if result['status'] in ('verified', 'resolved', 'observed', 'recorded', 'bound', 'completed', 'planned',
                           'recorder_ready', 'recorder_selection_required', 'recorder_starting',
                           'recorder_processing', 'recording'):
        return 0
    return 2 if result['status'] in ('blocked', 'cancelled', 'needs_input', 'needs_review', 'needs_consent',
                                     'needs_grounding', 'needs_scene') else 3


def worker_receipt(argv, mutating, payload=None, *, may_queue=False):
    # A third-party accessibility provider can hang inside a COM call. Terminate
    # our helper only; never assume a timed-out mutation failed or repeat it.
    started = time.monotonic()
    try:
        kind = (payload or {}).get('step', {}).get('action')
        timeout = {'row-action': 120, 'row-launch': 120, 'result-menu': 160}.get(kind, 90)
        if argv == ['_plan']:
            from python_workflow import worker_timeout
            timeout = worker_timeout(payload['recipe'])
        run_child = coordination.run_worker if mutating or may_queue else subprocess.run
        child = run_child([sys.executable, '-X', 'utf8', '-B', str(Path(__file__).resolve()), *argv],
                          input=json.dumps(payload) if payload is not None else None,
                          capture_output=True, encoding='utf-8', timeout=timeout)
        receipt = json.loads(child.stdout)
        if not isinstance(receipt, dict) or 'status' not in receipt or child.returncode not in (0, 2, 3):
            raise ValueError()
        coordination.worker_timings(receipt, started, getattr(child, 'queue_seconds', 0))
        return receipt
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        diagnostic = 'provider_timeout_or_interrupted'
    except (OSError, ValueError):
        diagnostic = 'provider_unavailable'
    failure = {'status': 'uncertain' if mutating else 'blocked', 'diagnostic': diagnostic,
               'input_dispatched': None if mutating else False}
    coordination.worker_timings(failure, started)
    return failure


def supervise(args, argv):
    mutating = args.command in ('open','select','fill') or (args.command == 'row-launch' and not args.preview)
    receipt = worker_receipt(['_worker', *argv], mutating=mutating,
                             may_queue=args.command == 'recover' and args.restore_window)
    print(json.dumps(receipt, ensure_ascii=False))
    return result_code(receipt, summary=getattr(args, 'summary', False))


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    argv = sys.argv[1:]
    if argv == ['_plan']:
        entered = time.monotonic()
        import python_workflow
        try:
            payload = json.load(sys.stdin)
            if 'menu_trial' in payload:
                import trial_menu
                result = trial_menu.execute(sys.modules[__name__], payload)
            else:
                result = python_workflow.run(payload['recipe'], payload['overrides'], hwnd=payload.get('hwnd'),
                                         allow_recorded_selection=True,
                                         background_only=payload.get('background_only', False), only=payload.get('only'),
                                         **({'result_confirmation': payload['result_confirmation']}
                                            if 'result_confirmation' in payload else {}))
        except Exception as error:
            result = dict(status='uncertain', diagnostic=getattr(error, 'code', 'execution_plan_unavailable'),
                          input_dispatched=None)
        result['_worker_entered'] = entered
        print(json.dumps(result, ensure_ascii=False))
        return result_code(result)
    if argv == ['_step']:
        return worker(None)
    is_worker = argv[:1] == ['_worker']
    if is_worker:
        argv = argv[1:]
    try:
        args = parser().parse_args(argv)
    except SystemExit as error:
        if error.code != 2 or argv[:1] not in (['recorder'], ['record'], ['review'], ['continue-recording']):
            raise
        hint = 'recorder --request <UTF-8 JSON file>'
        if argv[:1] == ['recorder'] and '--request' not in argv:
            hint = 'recorder --session "<session>"' if '--session' in argv else 'recorder [--app "<app or title>"]'
        print(json.dumps(dict(status='blocked', diagnostic='invalid_recorder_arguments',
                              hint=hint, input_dispatched=False)))
        return 2
    if args.command in ('inspect', 'select', 'fill', 'open') and (
            bool(args.task) != bool(args.near) or (args.popup is not None and args.near)):
        print(json.dumps({'status': 'blocked', 'diagnostic': 'near_requires_task_and_main_window', 'input_dispatched': False}))
        return 2
    if args.command == 'inspect' and not args.target and (args.within or args.role):
        print(json.dumps({'status': 'blocked', 'diagnostic': 'specify_target_with_filter', 'input_dispatched': False}))
        return 2
    # Recording lasts as long as the user demonstrates. Replay has one timeout
    # per direct step, with the original window lifetime pinned throughout.
    return worker(args) if is_worker or args.command in ('recorder', 'record', 'review', 'continue-recording', 'record-rows', 'replay', 'try-recording', 'resume-trial', 'inspect-menu-trial', 'resume-menu-trial', 'tasks', 'show', 'plan', 'instruct', 'storage') else supervise(args, argv)


if __name__ == '__main__':
    raise SystemExit(main())
