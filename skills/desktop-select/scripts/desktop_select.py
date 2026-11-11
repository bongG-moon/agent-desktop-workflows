"""Direct Windows actions and named demonstrations using the same live targets."""
from __future__ import annotations

import argparse
from collections import deque
import ctypes as C
from ctypes import wintypes as W
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys


def load_bridge():
    from runtime_integrity import checked_path
    path = checked_path('win_bridge.py')
    existing = sys.modules.get('win_bridge')
    if existing is not None and Path(existing.__file__).resolve() == path.resolve():
        return existing
    spec = importlib.util.spec_from_file_location('win_bridge', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules['win_bridge'] = module
    return module


try:
    bridge = load_bridge()
except RuntimeError as error:
    if __name__ != '__main__':
        raise
    print(json.dumps({'status': 'blocked', 'diagnostic': getattr(error, 'code', 'runtime_load_failed'),
                      'file': getattr(error, 'filename', 'win_bridge.py'), 'input_dispatched': False,
                      'help': 'Reinstall the complete Desktop Select ZIP; do not mix versions.'}))
    raise SystemExit(2)
from targeting import SelectionError, field_labels, resolve, scope_map
from live_action import execute
import learned_tasks as tasks
from provider_visibility import visible, restore_for_action
from provider_labels import semantic_label
from runtime_integrity import RuntimeIntegrityError
from intent_tasks import IntentError


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
    valid = bounds['width'] > 0 and bounds['height'] > 0
    node.update(class_name=element.property(30012) or '', bounds=bounds, bounds_valid=valid,
                offscreen=not visible(binding, uia, element, root),
                visibility_evidence='provider' if element.property(30022) is not None else 'window_geometry')
    if node['role'] in {'combobox', 'textfield', 'checkbox', 'radiobutton', 'spinner'}:
        label = uia.related_element(element, 30018)
        if label is not None:
            if (uia.pid(label) != binding.pid or not uia.within(label, root) or uia.secure(label)
                    or not visible(binding, uia, label, root)):
                node['label_source'] = 'invalid_labeled_by'
            else:
                node.update(semantic_label=label.property(30005) or '', label_source='labeled_by')


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
        if len(nodes) >= 2500 or depth > 18 or uia.pid(element) != binding.pid:
            raise SelectionError('incomplete_tree')
        # A protected subtree has no usable targets and leaks no labels.
        secure = uia.secure(element)
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
    return os.environ.get('DESKTOP_SELECT_TASK_DIR')


def near_region(nodes, anchor):
    """Return the nearest grouping of a freshly resolved demonstrated control."""
    if anchor.get('action') == 'intent':
        from intent_replay import scope
        return scope(nodes, anchor)
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
    restored = restore_for_action(owner, args.window) if args.command in {'select', 'open'} else False
    surface = bind_surface(owner, args.popup)
    mutate = args.command in {'select', 'open'}
    check = lambda: guard(owner, surface, args.window, mutate)
    check()
    with bridge.UIA() as uia:
        def read_view():
            nodes, elements = observe(surface, uia, identities_only=learned is not None or anchor is not None)
            if anchor is None:
                return nodes, elements, None
            scoped, indexes, a = near_region(nodes, anchor)
            if anchor.get('label') and anchor.get('action') != 'intent':
                tasks.check_display(anchor, semantic_label(surface, uia, elements[a], elements[0]))
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
        role = 'combobox' if args.command == 'select' else args.role
        def locate(tree, available):
            if learned is not None:
                index, evidence = tasks.locate_target(tree, learned)
                if learned.get('label'):
                    tasks.check_display(learned, semantic_label(surface, uia, available[index], available[0]))
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
            return result

        def still_valid():
            check()
            current, live, current_anchor = read_view()
            if anchor is not None:
                if not uia.same(anchor_element, current_anchor) or not uia.same(elements[0], live[0]):
                    raise SelectionError('demonstrated_anchor_changed')
            j, basis = locate(current, live)
            if basis != evidence or not uia.same(element, live[j]):
                raise SelectionError('target_changed')
            check()

        command = 'select' if args.command == 'select' else ('expand' if args.expand else
                  'choose' if node['role'] in {'treeitem', 'tab', 'listitem'} else 'invoke')
        # A direct command has already resolved its exact label/ID, scope and
        # layout once. Pin that choice to native identities for the one action;
        # neither a saved registration nor repeated full-window reads are needed.
        direct = (learned is None and anchor is None and args.popup is None
                  and node.get('automation_id') and len(node['automation_id']) <= 1000)
        capability = 'SetValue' if command == 'select' else 'Click'
        if (direct and (command == 'select' or command == 'invoke' and node['role'] in
                        {'button', 'splitbutton', 'hyperlink', 'menuitem'})
                and capability in uia.actions(element)):
            by_path = {n['path']: (n, e) for n, e in zip(nodes, elements)}
            chain, path = [], node['parent_path']
            while path:
                parent, peer = by_path[path]
                if parent['parent_path']:
                    chain.append((parent, peer))
                path = parent['parent_path']
            ancestors = []
            for parent, _ in chain:
                key = 'automation_id' if parent.get('automation_id') else 'name'
                if parent.get(key):
                    ancestors.append({'role': parent['role'], key: parent[key]})
            # Unusually deep/oversized provider identities keep the legacy path.
            native_shape = (len(ancestors) <= 16 and all(a['role'] in bridge.ROLES.values() for a in ancestors) and all(
                len(value) <= 1000 and '\0' not in value and value.strip()
                for item in ancestors for key, value in item.items() if key != 'role'))
            if native_shape:
                from registered_runtime import _dispatch
                import native_targets
                root = elements[0]
                selector = {'role': node['role'], 'automation_id': node['automation_id']}
                if ancestors:
                    selector['ancestors'] = ancestors
                def authorize_direct():
                    check()
                    current_root = uia.from_handle(surface.hwnd)
                    if not uia.same(root, current_root):
                        raise SelectionError('target_changed')
                    if args.within:
                        # A region alias can refer to an unnamed/class-only
                        # container, which an ID selector cannot encode.
                        current_chain = native_targets.ancestry(uia, element, root, pid=surface.pid)
                        if any(not any(uia.same(peer, live) for live in current_chain[1:])
                               for _, peer in chain):
                            raise SelectionError('target_changed')
                    if ((command == 'invoke' or evidence == 'name')
                            and (element.property(30005) or '') != node.get('name', '')):
                        raise SelectionError('target_caption_changed')
                step = {'action': 'set-value' if command == 'select' else 'invoke', 'selector': selector}
                result = _dispatch(surface, uia, root, step, args.window, getattr(args, 'value', None),
                                   expected_element=element, authorize=authorize_direct)
                return dict(result, target=detail, matched_by=evidence, business_commit='not_verified',
                            window_restored=restored, execution_path='direct_native',
                            metrics={'full_tree_snapshots': 1, 'option_inventories': 0})
        result = execute(surface, uia, element, node, command, getattr(args, 'value', None), still_valid=still_valid)
        return dict(result, target=detail, matched_by=evidence, business_commit='not_verified',
                    window_restored=restored)


def run(args):
    bridge.set_dpi_awareness()
    if args.command == 'register':
        from registered_setup import register
        return register(args)
    if args.command == 'doctor':
        from runtime_integrity import checked_path
        checked_path('win_bridge.py')
        checked_path('recorder.py')
        here = Path(__file__).resolve().parent.parent
        version = json.loads((here / 'runtime-manifest.json').read_text(encoding='utf8')).get('version', 'unknown')
        return dict(status='observed', version=version, skill_path=str(here),
                    runtime_integrity='matched', input_dispatched=False,
                    capabilities=['position_recording', 'free_text_instructions', 'edit_delete_steps',
                                  'model_resolution', 'provider_compatibility', 'auto_instruction_drafts',
                                  'native_target_discovery', 'registered_python_workflows'])
    if args.command in ('tasks', 'show'):
        if args.command == 'tasks':
            return {'status': 'observed', 'tasks': tasks.list_tasks(task_directory()), 'input_dispatched': False}
        from intent_replay import describe_plan
        return dict(describe_plan(tasks.load_task(args.task, task_directory())), status='observed')
    if args.command == 'replay':
        plan = tasks.load_task(args.task, task_directory())
        overrides = tasks.parse_overrides(args.set)
        binding = None
        def invoke(step):
            nonlocal binding
            if binding is None:
                try:
                    binding = pick_window(windows(args.exe), args.window, args.hwnd)
                except (SelectionError, bridge.BridgeError) as error:
                    return {'status': 'blocked', 'diagnostic': error.code, 'input_dispatched': False}
            return worker_receipt(['_step'], mutating=True, payload={'binding': binding, 'step': step})
        resolution = None
        if getattr(args, 'resolution', None):
            try:
                path = Path(args.resolution).absolute()
                if str(path).startswith(('\\\\', '//')):
                    raise tasks.TaskError('local_resolution_required')
                tasks._deny_redirect(path)
                with path.open('rb') as f:
                    data = f.read(tasks.MAX_FILE_BYTES + 1)
            except OSError as error:
                raise tasks.TaskError('resolution_file_unavailable') from error
            if len(data) > tasks.MAX_FILE_BYTES:
                raise tasks.TaskError('resolution_too_large')
            try:
                resolution = json.loads(data, object_pairs_hook=tasks._unique_keys)
            except (ValueError, UnicodeError) as error:
                raise tasks.TaskError('invalid_resolution_file') from error
        if any(step['action'] == 'intent' for step in plan['steps']):
            from confirmed_plan import replay as replay_confirmed
            return replay_confirmed(plan, args.exe, args.window, overrides, invoke,
                                    directory=task_directory(), only=args.only, resolution=resolution)
        return tasks.replay(plan, args.exe, args.window, overrides, invoke, only=args.only, resolution=resolution)
    rows = windows(args.exe)
    if args.command == 'windows':
        return {'status': 'observed', 'windows': rows, 'input_dispatched': False}
    binding = pick_window(rows, args.window, args.hwnd)
    owner = bridge.Binding(binding)
    if args.command == 'discover':
        from registered_setup import discover
        return discover(owner, args.window, args.id)
    if args.command == 'record-rows':
        from row_action import teach_rows
        restore_for_action(owner, args.window)
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
        result = []
        for number in numbers:
            step = plan['steps'][number - 1]
            if step['action'] != 'row-action':
                raise SelectionError('not_a_row_step')
            guard(owner, owner, args.window)
            state = observe_row(owner, step['row'])['kind']
            guard(owner, owner, args.window)
            result.append({'number': number, 'label': step['label'], 'observed_state': state})
        return {'status': 'observed', 'steps': result, 'input_dispatched': False}
    if args.command in ('record', 'edit'):
        from teach_capture import capture
        old = None
        if args.command == 'edit':
            old = tasks.load_task(args.task, task_directory())
            tasks.check_binding(old, args.exe, args.window)
        else:
            tasks.check_new_task(args.task, task_directory())
        restore_for_action(owner, args.window)
        guard(owner, owner, args.window)
        capture_result = capture(owner, title=args.window, steps=old['steps'] if old else None)
        if capture_result['status'] != 'recorded':
            return capture_result
        plan = tasks.from_recording(args.task, args.exe, args.window, capture_result['steps'])
        saved = {}
        if old:
            from intent_tasks import plan_digest
            from task_edit import replace_task
            saved = replace_task(plan, plan_digest(old), task_directory())
        else:
            tasks.save_task(plan, task_directory())
        return {'status': 'recorded', 'task': tasks.summary(plan), 'input_dispatched': False, **saved}
    anchor = None
    if getattr(args, 'near', None) is not None:
        plan = tasks.load_task(args.task, task_directory())
        tasks.check_binding(plan, args.exe, args.window)
        anchor = tasks.get_step(plan, args.near)
    return action(args, owner, anchor=anchor)


def replay_step():
    request = json.load(sys.stdin)
    binding, step = request['binding'], request['step']
    bridge.set_dpi_awareness()
    owner = bridge.Binding(binding)
    if step['action'] == 'intent-execute':
        from intent_tasks import validate_step
        from intent_replay import validate_action, scope
        intent = step['intent']
        validate_step(intent)
        operation = validate_action(step['operation'])
        if 'parameter' in operation:
            raise tasks.TaskError('required_parameter_missing')
        if operation['action'] == 'result-menu':
            from result_menu import execute as execute_result
            restored = restore_for_action(owner, binding['title'])
            def check_context():
                guard(owner, owner, binding['title'], mutate=True)
                with bridge.UIA() as uia:
                    nodes, _ = observe(owner, uia, identities_only=True)
                    region, _, _ = scope(nodes, intent)
                    target = operation['context']['menu']['target']
                    matches = [n for n in region if not n.get('secure') and
                               all(n.get(k) == v for k, v in target.items())]
                    if len(matches) != 1:
                        raise tasks.TaskError('resolved_target_outside_taught_area')
            check_context()
            receipt = execute_result(owner, operation['context'], check_context, binding['title'],
                                     observe_guard=lambda: guard(owner, owner, binding['title']))
            return dict(receipt, target=intent['label'], window_restored=restored)
        args = argparse.Namespace(command=operation['action'], role=operation['role'], window=binding['title'],
                                  target=operation['target'], value=operation.get('value'),
                                  expand=operation.get('expand', False), within=None, popup=None)
        return action(args, owner, learned=operation, anchor=intent)
    if step['action'] == 'result-menu':
        from result_menu import execute as execute_result
        tasks._validate_context(step)
        restored = restore_for_action(owner, binding['title'])
        receipt = execute_result(owner, step['context'], lambda: guard(owner, owner, binding['title'], mutate=True),
                                 binding['title'], observe_guard=lambda: guard(owner, owner, binding['title']))
        return dict(receipt, target=step['label'], window_restored=restored)
    if step['action'] == 'row-action':
        from row_action import execute as execute_row
        tasks._validate_row(step)
        restored = restore_for_action(owner, binding['title'])
        receipt = execute_row(owner, step['row'], lambda: guard(owner, owner, binding['title'], mutate=True))
        return dict(receipt, target=step['label'], window_restored=restored)
    args = argparse.Namespace(command=step['action'], role=step['role'], window=binding['title'],
                              target=step['target'], value=step.get('value'), expand=step.get('expand', False),
                              within=step.get('within'), popup=None)
    if args.command not in ('select', 'open'):
        raise SelectionError('step_requires_review')
    return action(args, owner, learned=step)


def parser():
    p = argparse.ArgumentParser(description='Select an exact field or command in one Windows business window.')
    commands = p.add_subparsers(dest='command', required=True)
    commands.add_parser('tasks', help='list local named demonstrations')
    commands.add_parser('doctor', help='show installed version, path and compatibility capabilities')
    commands.add_parser('show', help='inspect saved steps and notes').add_argument('--task', required=True)
    register = commands.add_parser('register', help='verify IDs once and create a local Python workflow skill')
    source = register.add_mutually_exclusive_group(required=True)
    source.add_argument('--recipe', help='local registered workflow JSON')
    source.add_argument('--task', help='existing recorded task to export without modifying it')
    register.add_argument('--resolution', help='one-time interpreted actions for the recorded task')
    register.add_argument('--out', required=True, help='new local skill folder beside desktop-select')
    register.add_argument('--hwnd', type=int)
    for verb in ('windows', 'discover', 'inspect', 'select', 'open', 'record', 'edit', 'replay', 'record-rows', 'row-status'):
        s = commands.add_parser(verb)
        s.add_argument('--exe', required=True, help='exact executable basename')
        if verb != 'windows':
            s.add_argument('--window', required=True, help='exact business window title from windows')
            s.add_argument('--hwnd', type=int, help='disambiguate windows with identical titles')
        if verb == 'discover':
            s.add_argument('--id', action='append', default=[], help='query only these automation IDs')
        if verb in ('record', 'edit', 'replay', 'record-rows', 'row-status'):
            s.add_argument('--task', required=True, help='local task name')
        if verb == 'record-rows':
            s.add_argument('--row', nargs=3, action='append', required=True, metavar=('LABEL', 'X', 'Y'),
                           help='one demonstrated row label and physical screen point per occurrence')
            for field in ('key', 'button', 'state'):
                s.add_argument('--' + field + '-box', nargs=4, type=int, required=True, metavar=('X', 'Y', 'W', 'H'),
                               help='physical pixel rectangle relative to the row')
        if verb == 'replay':
            s.add_argument('--set', action='append', default=[], metavar='FIELD=VALUE')
            s.add_argument('--resolution', help='local JSON actions interpreted from this exact saved task')
        if verb in ('replay', 'row-status'):
            s.add_argument('--only', help='run just one named step or step number')
        if verb in ('inspect', 'open', 'select'):
            s.add_argument('--popup', type=int, help='owned popup from current inspect output')
            s.add_argument('--target', required=verb != 'inspect', help='field label, name or automation ID')
            s.add_argument('--within', help='observed region, ancestor ID or name')
            s.add_argument('--task', help='demonstration that contains the anchor')
            s.add_argument('--near', help='limit search to this demonstrated step’s current container')
        if verb in ('inspect', 'open'):
            s.add_argument('--role', help='e.g. combobox, button, menuitem, treeitem')
        if verb == 'select':
            s.add_argument('--value', required=True, help='exact option text/code')
        if verb == 'open':
            s.add_argument('--expand', action='store_true', help='expand a submenu/tree item instead of invoking it')
    return p


def worker(args):
    try:
        result = replay_step() if args is None else run(args)
    except (SelectionError, bridge.BridgeError, tasks.TaskError, RuntimeIntegrityError, IntentError) as error:
        dispatched = getattr(error, 'input_dispatched', False)
        result = {'status': 'blocked' if dispatched is False else 'uncertain', 'diagnostic': error.code,
                  'input_dispatched': dispatched}
        if isinstance(error, RuntimeIntegrityError):
            result.update(file=error.filename, help='Reinstall the complete Desktop Select ZIP; do not mix versions.')
    print(json.dumps(result, ensure_ascii=False))
    return result_code(result)


def result_code(result):
    if result['status'] in ('verified', 'resolved', 'observed', 'recorded', 'registered', 'completed', 'completed_unverified'):
        return 0
    return 2 if result['status'] in ('blocked', 'cancelled', 'needs_input', 'needs_review', 'needs_target', 'needs_instruction',
                                   'needs_interpretation') else 3


def worker_receipt(argv, mutating, payload=None):
    # A third-party accessibility provider can hang inside a COM call. Terminate
    # our helper only; never assume a timed-out mutation failed or repeat it.
    try:
        kind = (payload or {}).get('step', {}).get('action')
        timeout = {'row-action': 120, 'result-menu': 160, 'intent-execute': 160}.get(kind, 90)
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
    receipt = worker_receipt(['_worker', *argv], mutating=args.command in ('open', 'select'))
    print(json.dumps(receipt, ensure_ascii=False))
    return result_code(receipt)


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    argv = sys.argv[1:]
    if argv == ['_step']:
        return worker(None)
    is_worker = argv[:1] == ['_worker']
    if is_worker:
        argv = argv[1:]
    args = parser().parse_args(argv)
    if args.command in ('inspect', 'select', 'open') and (
            bool(args.task) != bool(args.near) or (args.popup is not None and args.near)):
        print(json.dumps({'status': 'blocked', 'diagnostic': 'near_requires_task_and_main_window', 'input_dispatched': False}))
        return 2
    if args.command == 'inspect' and not args.target and (args.within or args.role):
        print(json.dumps({'status': 'blocked', 'diagnostic': 'specify_target_with_filter', 'input_dispatched': False}))
        return 2
    # Recording lasts as long as the user demonstrates. Replay has one timeout
    # per direct step, with the original window lifetime pinned throughout.
    return worker(args) if is_worker or args.command in ('record', 'edit', 'record-rows', 'replay', 'tasks', 'show') else supervise(args, argv)


if __name__ == '__main__':
    raise SystemExit(main())
