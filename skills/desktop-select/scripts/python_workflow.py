"""Register a small, literal Python workflow beside the original 0.4 recorder.

One UIA session runs known controls. No model plan, replay cache, shell text or
implicit retry is involved. UIA discovery remains available for first setup.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import desktop_select as app
import control_lookup as lookup
import coordination
import context_menu
import export_evidence
import input_policy
import key_chords
import live_action as live
import result_menu
import row_action
import row_intent
import row_vision
import window_context
import conditional_action
import workflow_repair


CHILD_VISUAL_TIMEOUT = 30
RESULT_VISUAL_TIMEOUT = 5
KEY_INTERVAL_SECONDS = 0.8
_KEY_ACTIONS = ('submit', 'press-key')
RESULT_SURFACES = {'table', 'list', 'group', 'custom'}
OBSERVABLE_FIELDS = {'textfield', 'combobox', 'statictext', 'row', 'listitem'}
RESULT_IMAGE_ERRORS = {
    'target_missing', 'result_table_missing', 'result_column_unavailable', 'result_cell_missing', 'result_cell_unreadable',
}
RESULT_INSPECTION_ERRORS = RESULT_IMAGE_ERRORS | {
    'result_table_ambiguous', 'result_cell_ambiguous', 'result_column_ambiguous', 'target_ambiguous',
    'result_value_not_found', 'result_column_not_found', 'result_grid_scan_limit', 'result_grid_incomplete',
}


class WorkflowError(ValueError):
    def __init__(self, code, *, required_parameters=None):
        super().__init__(code)
        self.code = code
        self.required_parameters = required_parameters


def need(ok, reason):
    if not ok:
        raise WorkflowError(reason)


def text(value, limit=1000):
    return (isinstance(value, str) and bool(value.strip()) and len(value.encode('utf8')) <= limit
            and not any(ord(c) < 32 for c in value))


def validate_selector(selector):
    if isinstance(selector, dict) and 'query' in selector:
        need(not set(selector) - {'query', 'role', 'within'} and text(selector['query'])
             and selector.get('role') in app.bridge.ROLES.values()
             and ('within' not in selector or text(selector['within'])), 'invalid_semantic_selector')
    else:
        lookup._selector(selector)


def semantic_match(binding, uia, root, selector):
    """Use the same current-window label/name resolver as a direct command."""
    nodes, elements = app.observe(binding, uia)
    need(bool(elements) and uia.same(root, elements[0]), 'target_window_changed')
    index, basis = app.resolve(nodes, selector['query'], role=selector['role'], within=selector.get('within'))
    element, node = elements[index], nodes[index]
    lookup.ancestry(uia, element, root, pid=binding.pid)
    # Promote only an identity witnessed in this live read, never old coordinates.
    stable = app._stable_selector(nodes, index)
    if stable is not None:
        current = lookup.resolve(binding, uia, stable, root=root)
        need(uia.same(element, current), 'target_changed')
    observed_label = app.field_labels(nodes).get(index, {})
    proof = dict(matched_by=basis, query=selector['query'], role=node['role'])
    if node.get('automation_id'):
        proof['automation_id'] = node['automation_id']
    if node.get('name'):
        proof['name'] = node['name']
    proof.update(observed_label)
    return element, stable, proof


def resolve_step(binding, uia, root, selector):
    if 'query' in selector:
        return semantic_match(binding, uia, root, selector)
    return lookup.resolve(binding, uia, selector, root=root), selector, None


def revalidate_step(binding, uia, root, selector, stable, proof, element):
    if stable is not None:
        current = lookup.resolve(binding, uia, stable, root=root)
    else:
        current, _, _ = semantic_match(binding, uia, root, selector)
    need(uia.same(element, current), 'target_changed')
    if proof:
        if selector['role'] not in ('combobox', 'textfield') and proof.get('name'):
            need(current.property(30005) == proof['name'], 'command_name_changed')
        if proof.get('semantic_label'):
            app.tasks.check_display({'label': proof['semantic_label']}, app.field_label(binding, uia, current, root))
    return current


def validate(recipe):
    need(isinstance(recipe, dict) and recipe.get('schema') == 1, 'invalid_workflow')
    need(not set(recipe) - {'schema', 'name', 'exe', 'window', 'steps'}, 'unknown_workflow_field')
    app.tasks._window(recipe.get('exe'), recipe.get('window'))
    need(text(recipe.get('name'), 200), 'workflow_name_required')
    steps = recipe.get('steps')
    need(isinstance(steps, list) and 0 < len(steps) <= 100, 'invalid_workflow_steps')
    window_context.validate_sequence(steps)
    for index, raw in enumerate(steps):
        step = raw
        if isinstance(raw, dict) and 'window_context' in raw:
            step = {key: value for key, value in raw.items() if key != 'window_context'}
        need(isinstance(step, dict), 'invalid_workflow_step')
        if step.get('action') == 'double-click-result':
            if 'result_row' in step:
                need(set(step) <= {'action', 'label', 'result_row', 'selector'}
                     and step['result_row'] in ('single', 'first') and text(step.get('label', 'step'), 200),
                     'invalid_result_row')
                selector = step.get('selector')
                need(isinstance(selector, dict) and selector.get('role') in RESULT_SURFACES,
                     'result_container_required')
                if selector != {'role': 'table'}:
                    validate_selector(selector)
                continue
            previous = steps[index - 1] if index else {}
            need(set(step) <= {'action', 'label'} and text(step.get('label', 'step'), 200)
                 and previous.get('action') == 'verify-value' and 'result_column' in previous
                 and raw.get('window_context') == previous.get('window_context') and 'popup' not in previous,
                 'verified_result_required')
            continue
        if step.get('action') == 'conditional-open':
            conditional_action.validate_conditional(step)
            continue
        if step.get('action') == 'row-open':
            need(set(step) - {'alias'} == {'action','label','row_target'} and text(step['label'],1000)
                 and ('alias' not in step or text(step['alias'],200)), 'invalid_row_step')
            import row_target
            row_target.validate_spec(step['row_target'])
            continue
        if step.get('action') == 'visual-click':
            from visual_target import validate_spec
            need(set(step) - {'alias'} == {'action', 'label', 'visual'}
                 and text(step['label']) and ('alias' not in step or text(step['alias'], 200)), 'invalid_visual_step')
            validate_spec(step['visual'])
            continue
        if step.get('action') in ('row-action', 'row-launch'):
            ordinary = step['action'] == 'row-action'
            key = 'row' if ordinary else 'ordinal'
            optional = {'alias'} if ordinary else {'alias', 'row_hint'}
            need(set(step) - optional == {'action', 'label', key}, 'invalid_row_step')
            need(text(step['label'], 1000) and ('alias' not in step or text(step['alias'], 200)), 'invalid_row_step')
            if ordinary:
                row_vision.validate_spec(step['row'])
            else:
                need(type(step['ordinal']) is int and row_intent.parse(step['label']) == step['ordinal'],
                     'invalid_row_step')
                if 'row_hint' in step:
                    row_vision.validate_spec(step['row_hint'])
                    need(step['row_hint']['version'] == 1, 'invalid_row_step')
            continue
        if step.get('action') == 'wait':
            need(set(step) == {'action', 'seconds'} and type(step.get('seconds')) in (int, float)
                 and 0 <= step['seconds'] <= 120, 'invalid_wait')
            continue
        if step.get('action') == 'context-menu':
            need(not set(step) - {'action', 'spec', 'label'}, 'unknown_step_field')
            need(text(step.get('label', 'step'), 200), 'invalid_step_label')
            context_menu.validate_spec(step.get('spec'))
            continue
        if step.get('action') == 'result-menu':
            app.tasks._annotations(step)
            app.tasks._validate_context(step)
            continue
        if step.get('action') == 'verify-file':
            need(not set(step) - {'action', 'output', 'timeout', 'label'}, 'unknown_step_field')
            need(text(step.get('label', 'step'), 200), 'invalid_step_label')
            export_evidence.validate(step.get('output'))
            timeout = step.get('timeout', 26)
            need(type(timeout) in (int, float) and 0 <= timeout <= 120, 'invalid_file_timeout')
            continue
        need(not set(step) - {'action', 'selector', 'value', 'parameter', 'label', 'command_name', 'field_label', 'popup',
                              'grounding_text', 'allow_option_select', 'verify_closed', 'result_column',
                              'result_adapter', 'keys'}, 'unknown_step_field')
        observation = step.get('action') in input_policy.VALUE_OBSERVATIONS
        need(observation or step.get('action') in ('set-value', 'invoke', 'submit', 'press-key', 'choose-item'), 'unsupported_workflow_action')
        need('keys' not in step or step['action'] == 'press-key', 'unexpected_key_chord')
        if 'verify_closed' in step:
            need(step['verify_closed'] is True and step['action'] == 'invoke'
                 and ('popup' in step or 'window_context' in raw), 'invalid_close_verification')
        if 'popup' in step:
            popup = step['popup']
            need(isinstance(popup, dict) and not set(popup) - {'title', 'timeout'}
                 and text(popup.get('title')) and popup['title'] == popup['title'].strip(),
                 'invalid_popup_scope')
            timeout = popup.get('timeout', 3)
            need(type(timeout) in (int, float) and 0 <= timeout <= 10, 'invalid_popup_timeout')
        selector = step.get('selector')
        if 'result_column' in step:
            need(step['action'] == 'verify-value' and text(step['result_column'], 200)
                 and isinstance(selector, dict) and selector.get('role') in RESULT_SURFACES, 'invalid_result_column')
        if 'result_adapter' in step:
            adapter = step['result_adapter']
            need('result_column' in step and selector.get('role') == 'group'
                 and ('automation_id' in selector or 'name' in selector)
                 and isinstance(adapter, dict) and set(adapter) == {'kind', 'row_count'}
                 and adapter['kind'] == 'windows-ocr-grid', 'invalid_result_adapter')
            counter = adapter['row_count']
            need(isinstance(counter, dict) and counter.get('role') in ('statictext', 'textfield')
                 and ('automation_id' in counter or 'name' in counter), 'invalid_result_count_selector')
            lookup._selector(counter)
        if not ('result_column' in step and selector == {'role': 'table'}):
            validate_selector(selector)
        if observation:
            need('anchor' not in selector, 'named_observation_target_required')
            need('result_column' in step or selector['role'] in OBSERVABLE_FIELDS, 'invalid_observation_role')
        if 'allow_option_select' in step:
            need(step['action'] == 'set-value' and selector['role'] == 'combobox' and
                 step['allow_option_select'] is True, 'invalid_combo_selection')
        if 'grounding_text' in step:
            need(step['action'] == 'invoke' and 'query' in selector and text(step['grounding_text']),
                 'invalid_grounding_instruction')
        need(text(step.get('label', 'step'), 200), 'invalid_step_label')
        need('command_name' not in step or (step['action'] == 'invoke' and text(step['command_name'])),
             'invalid_command_name')
        need('field_label' not in step or ((observation or step['action'] == 'set-value') and text(step['field_label'], 200)),
             'invalid_field_label')
        if step['action'] == 'read-value':
            need('value' not in step and 'parameter' not in step, 'invalid_read_value')
        elif step['action'] in ('set-value', 'verify-value'):
            role = selector['role'] if not observation else 'textfield'
            need(role in ('combobox', 'textfield'), 'value_field_required')
            need(('value' in step) != ('parameter' in step), 'fixed_value_or_parameter_required')
            if 'value' in step:
                need(app.tasks.valid_value(step['value'], role),
                     'invalid_text_value' if selector['role'] == 'textfield' else 'invalid_combo_value')
                need('result_column' not in step or bool(step['value'].strip()), 'result_value_required')
            else:
                need(text(step['parameter'], 100) and step['parameter'] == step['parameter'].strip()
                     and '=' not in step['parameter'], 'invalid_parameter')
        elif step['action'] == 'submit':
            need(selector['role'] in ('combobox', 'textfield')
                 and 'value' not in step and 'parameter' not in step, 'invalid_submit')
        elif step['action'] == 'press-key':
            need(key_chords.virtual_keys(step.get('keys')) and selector['role'] in key_chords.TARGET_ROLES
                 and 'value' not in step and 'parameter' not in step, 'invalid_key_chord')
        elif step['action'] == 'choose-item':
            need(selector['role'] in ('listitem', 'treeitem', 'tab')
                 and 'value' not in step and 'parameter' not in step, 'invalid_item_selection')
        else:
            need(selector['role'] in ('button', 'menuitem', 'splitbutton', 'hyperlink')
                 and 'value' not in step and 'parameter' not in step, 'invalid_invoke')
    return recipe


def load(path):
    path = Path(path)
    # A recorded row can contain four bounded masks. A complete valid recording
    # and its exported workflow share the same maximum size.
    need(path.is_file() and path.stat().st_size <= app.tasks.MAX_FILE_BYTES, 'workflow_file_unavailable')
    return validate(json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=app.tasks._unique_keys))


def selected_steps(recipe, only):
    entries = list(enumerate(recipe['steps'], 1))
    if only is None:
        return entries
    key = str(only).strip()
    if key.isdecimal():
        selected = [(n, s) for n, s in entries if n == int(key)]
    else:
        selected = [(n, s) for n, s in entries if key and key in (s.get('alias'), s.get('label'))]
    need(len(selected) == 1, 'workflow_step_ambiguous' if selected else 'workflow_step_not_found')
    if selected[0][1]['action'] == 'double-click-result' and 'result_row' not in selected[0][1]:
        number = selected[0][0]
        return entries[number - 2:number]
    return selected


def _key_intervals(steps, index):
    """Charge each default interval to its key, once per executed boundary."""
    if steps[index]['action'] not in _KEY_ACTIONS:
        return 0, 0
    previous = steps[index - 1]['action'] if index else None
    following = steps[index + 1]['action'] if index + 1 < len(steps) else None
    before = 0 if previous in ('wait', *_KEY_ACTIONS) else KEY_INTERVAL_SECONDS
    after = 0 if following == 'wait' else KEY_INTERVAL_SECONDS
    return before, after


def _pause_until(deadline):
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)


def worker_timeout(recipe):
    return 90 + sum(step.get('seconds', 0) + step.get('popup', {}).get('timeout', 3 if 'popup' in step else 0)
                    + (RESULT_VISUAL_TIMEOUT if step['action'] == 'verify-value' or 'result_row' in step else 0)
                    + (3 if step.get('verify_closed') else 0)
                    + (step.get('timeout', 26) if step['action'] == 'verify-file' else 0)
                    + (3 * len(step['spec']['path']) + 3 if step['action'] == 'context-menu' else 0)
                    + (60 + 3 * len(step['context']['menu']['path']) if step['action'] == 'result-menu' else 0)
                    + (120 if step['action'] in ('row-action', 'row-launch') else 0)
                    + (CHILD_VISUAL_TIMEOUT if step['action'] == 'visual-click'
                       and 'after_step' in step.get('window_context', {}) else
                       RESULT_VISUAL_TIMEOUT if app.tasks.is_double_click_step(step) else 0)
                    + sum(_key_intervals(recipe['steps'], index))
                    for index, step in enumerate(recipe['steps'])) + window_context.OPEN_TIMEOUT * len({
                        (spec['exe'].casefold(), spec['title'], spec['after_step'])
                        for step in recipe['steps'] if 'after_step' in (spec := step.get('window_context', {}))})


def values_for(recipe, overrides):
    parameters = {s['parameter'].casefold(): s['parameter'] for s in recipe['steps'] if 'parameter' in s}
    values = {k.casefold(): v for k, v in overrides.items()}
    need(len(values) == len(overrides) and all(app.tasks.valid_value(v, 'textfield') for v in values.values()),
         'invalid_override')
    if values.keys() - parameters.keys():
        raise WorkflowError('unknown_parameter', required_parameters=list(parameters.values()))
    for step in recipe['steps']:
        key = step.get('parameter', '').casefold()
        if key in values:
            role = 'textfield' if step['action'] == 'verify-value' else step['selector']['role']
            need(app.tasks.valid_value(values[key], role), 'invalid_override')
            need('result_column' not in step or bool(values[key].strip()), 'result_value_required')
    return values, [name for key, name in parameters.items() if key not in values]


def bind(exe, title, hwnd=None):
    app.bridge.set_dpi_awareness()
    records = app.windows(exe) if hwnd is None else app.windows(exe, only_hwnd=hwnd)
    row = app.pick_window(records, title, hwnd)
    binding = app.bridge.Binding(row)
    app.guard(binding, binding, title)
    return binding


def check_window_readability(binding, uia):
    """Read one live UIA property on the bound HWND, without enumerating its controls."""
    started = time.monotonic()
    result = dict(scope='window_root', count=1, input_dispatched=False, status='unavailable')
    try:
        root = uia.from_handle(binding.hwnd)
        pid = uia.pid(root) if root is not None else None
        if type(pid) is not int or pid <= 0:
            result['diagnostic'] = 'window_root_unreadable'
        elif pid != binding.pid:
            result['diagnostic'] = 'window_root_process_mismatch'
        else:
            result['status'] = 'observed'
    except app.bridge.BridgeError as error:
        result.update(diagnostic='window_root_unreadable', provider_diagnostic=error.code)
    result['elapsed_seconds'] = round(time.monotonic() - started, 4)
    return result


def wait(binding, title, seconds):
    deadline = time.monotonic() + seconds
    while True:
        app.guard(binding, binding, title)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(.2, remaining))


def combine(previous, current):
    return None if previous is None or current is None else bool(previous or current)


def result_surface(binding, uia, selector, *, on_ambiguous=None):
    import result_cell
    root = uia.from_handle(binding.hwnd)
    if selector != {'role': 'table'}:
        return resolve_step(binding, uia, root, selector)[0]
    candidates = lookup.find_all(uia, root, roles=('table', 'list'))
    kept = None
    try:
        visible = [item for item in candidates if live._visible(uia, item, binding, root)
                   or result_cell.readable_surface(binding, uia, item, root)]
        if len(visible) > 1 and on_ambiguous is not None:
            kept = on_ambiguous(visible)
        else:
            need(len(visible) == 1, 'result_table_ambiguous' if visible else 'result_table_missing')
            kept = visible[0]
        return kept
    finally:
        for item in candidates:
            if item is not kept:
                item.close()


def _stable_result_selector(binding, uia, table):
    role = app.bridge.ROLES.get(table.property(30003))
    if role not in RESULT_SURFACES:
        return None
    identifier = table.property(30011)
    if lookup._text(identifier) and not identifier.isdecimal():
        selector = dict(role=role, automation_id=identifier)
    else:
        name = table.property(30005)
        if not lookup._text(name):
            return None
        selector = dict(role=role, name=name)
    try:
        same = lookup.resolve(binding, uia, selector)
    except app.bridge.BridgeError as error:
        if error.code in ('target_missing', 'target_ambiguous'):
            return None
        raise
    return selector if uia.same(same, table) else None


def _group_for_column(binding, uia, column, *, native=()):
    """Find a provider-backed result group even if it has no native table role."""
    import result_cell
    window = uia.from_handle(binding.hwnd)
    groups = [g for g in lookup.find_all(uia, window, roles=('group','custom'), grid_group=True)
              if live._visible(uia, g, binding, window) or result_cell.readable_surface(binding, uia, g, window)]
    def matching(surfaces):
        import result_cell
        need(len(surfaces) <= 24, 'result_grid_scan_limit')
        matches = []
        for surface in surfaces:
            header = result_cell._column(binding, uia, surface, window, column, None)
            if header.get('element') is not None and uia.same(header['grid'], surface):
                matches.append(surface)
        return matches
    if groups and not native:
        if len(groups) == 1:
            return groups[0]
        matches = matching(groups)
        if matches:
            need(len(matches) == 1, 'result_table_ambiguous')
            return matches[0]
    if not groups or native:
        # Without a grid pattern, a column header and actual data rows must
        # belong to the same immediate group. A form's matching caption is insufficient.
        def owner(node):
            chain = lookup.ancestry(uia, node, window, pid=binding.pid)
            return next((e for e in chain[1:-1] if app.bridge.ROLES.get(e.property(30003)) == 'group'), None)
        headers = lookup.find_all(uia, window, name=column, roles=('headeritem',))
        if not headers:
            headers = [item for item in lookup.find_all(uia, window, roles=('headeritem',))
                       if (item.property(30005) or '').strip().casefold() == column.strip().casefold()]
        for header in headers:
            if not live._visible(uia, header, binding, window):
                continue
            group = owner(header)
            if group is None or any(uia.same(g, group) for g in groups):
                continue
            rows = lookup.find_all(uia, group, roles=('row','listitem'), limit=4096)
            if any((parent := owner(row)) is not None and uia.same(parent, group) for row in rows):
                groups.append(group)
    if native:
        groups = matching([*native, *groups])
    need(len(groups) == 1, 'result_table_ambiguous' if groups else 'result_table_missing')
    return groups[0]


def _result_target(binding, uia, selector, reviewed_group, *, column=None, element_hint=False):
    if selector == {'role': 'table'} and reviewed_group and (element_hint or reviewed_group['role'] in ('table','list')):
        try:
            return result_surface(binding, uia, reviewed_group), reviewed_group
        except app.bridge.BridgeError as error:
            if error.code != 'target_missing':
                raise
        reviewed_group = None
    # A visually witnessed opaque group must not hide a newly appeared native table.
    try:
        if selector == {'role': 'table'} and column:
            selected = selector

            def choose_by_column(candidates):
                nonlocal selected
                try:
                    target = _group_for_column(binding, uia, column, native=candidates)
                except WorkflowError as scoped_error:
                    if scoped_error.code == 'result_table_missing':
                        raise WorkflowError('result_table_ambiguous') from scoped_error
                    raise
                selected = _stable_result_selector(binding, uia, target) or selector
                return target

            table = result_surface(binding, uia, selector, on_ambiguous=choose_by_column)
            return table, selected
        return result_surface(binding, uia, selector), selector
    except WorkflowError as error:
        if error.code != 'result_table_missing' or selector != {'role': 'table'}:
            raise
        if not reviewed_group:
            if column:
                group = _group_for_column(binding, uia, column)
                return group, _stable_result_selector(binding, uia, group) or selector
            raise
    try:
        return result_surface(binding, uia, reviewed_group), reviewed_group
    except app.bridge.BridgeError as error:
        if error.code not in ('target_missing', 'target_ambiguous'):
            raise
        if column and error.code == 'target_missing':
            group = _group_for_column(binding, uia, column)
            return group, _stable_result_selector(binding, uia, group) or selector
        # A stale hint cannot change the original recording's scope or
        # prevent the registered runner from offering a fresh local review.
        raise WorkflowError('result_table_missing') from error


def result_count(binding, uia, adapter):
    selector = adapter['row_count']
    element = lookup.resolve(binding, uia, selector)
    try:
        # Editable search/filter values cannot attest to the number of results.
        need(selector['role'] != 'textfield' or element.property(30046) is True,
             'result_count_not_readonly')
        value = observe_field(binding, uia, element, selector['role'], 'read-value')['value'].strip()
        need(re.fullmatch(r'[0-9]{1,8}', value), 'result_count_unavailable')
        return int(value)
    finally:
        element.close()


def observe_result(binding, uia, step, expected, guard, *, wait_seconds, reviewed_group=None, on_match=None,
                   column_hint=None, element_hint=False, remember=False, on_adapter_match=None, after_query=False):
    """Wait only for the explicitly requested result. Never submit the query again."""
    import result_cell
    started = time.monotonic()
    deadline = started + wait_seconds
    attempts, provider_seconds, waited = 0, 0., 0.
    retry_selector = None
    while True:
        guard()
        attempts += 1
        began = time.monotonic()
        table, selector = None, step['selector']
        evidence = {}
        try:
            table, selector = _result_target(binding, uia, selector, retry_selector or reviewed_group,
                                              column=step['result_column'], element_hint=bool(retry_selector) or element_hint)
            try:
                result_cell.find(binding, uia, table, expected, column=step['result_column'],
                                 column_hint=column_hint, evidence=evidence)
            except app.bridge.BridgeError as original:
                # A form can expose an unrelated list beside its custom result grid.
                # Disambiguate by the recorded column only if the first table lacks it.
                if (original.code not in ('result_column_unavailable','result_column_not_found')
                        or selector != {'role':'table'} or step['selector'] != selector):
                    raise
                try:
                    other = _group_for_column(binding, uia, step['result_column'], native=(table,))
                except WorkflowError as scoped_error:
                    if scoped_error.code == 'result_table_missing':
                        raise original
                    raise
                if uia.same(table, other):
                    raise original
                table = other
                selector = _stable_result_selector(binding, uia, table) or selector
                evidence.clear()
                result_cell.find(binding, uia, table, expected, column=step['result_column'], evidence=evidence)
        except (ValueError, app.bridge.BridgeError) as error:
            diagnostic = getattr(error, 'code', None)
            if diagnostic not in RESULT_INSPECTION_ERRORS:
                raise
            if (diagnostic == 'result_column_unavailable' and 'result_adapter' in step
                    and table is not None and result_cell.bare_group(uia, table)):
                import result_row_adapter
                try:
                    count = result_count(binding, uia, step['result_adapter'])
                    if count == 0 and time.monotonic() < deadline:
                        # A delayed VIEW may clear the table before populating it.
                        # Poll its declared count, without capturing pixels or resubmitting.
                        while count == 0 and (remaining := deadline - time.monotonic()) > 0:
                            before = time.monotonic()
                            time.sleep(min(.2, remaining))
                            waited += time.monotonic() - before
                            guard()
                            count = result_count(binding, uia, step['result_adapter'])
                        if count:
                            # Rows may have replaced the UIA surface, or added real cells.
                            continue
                except (WorkflowError, app.bridge.BridgeError) as count_error:
                    return dict(status='blocked', input_dispatched=False,
                                diagnostic='result_count_unavailable',
                                result_observation=dict(strategy='row_count', reason=count_error.code,
                                    column=step['result_column'], requested=expected, table_lookup='recorded_element',
                                    elapsed_seconds=round(time.monotonic()-started, 4)))
                result = result_row_adapter.observe(binding, uia, table, step['result_column'], expected,
                                                     guard, row_count=count)
                proof = result.pop('proof', None)
                snapshot = result.pop('_miss_snapshot', None)
                if after_query and snapshot is not None and result.get('diagnostic') == 'result_value_mismatch':
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        # The previous query can still be visible with the same row count.
                        # Spend the remaining result budget once; only changed pixels warrant new OCR.
                        before = time.monotonic()
                        time.sleep(remaining)
                        waited += time.monotonic() - before
                        guard()
                        if result_row_adapter.changed(binding, uia, table, snapshot, guard):
                            after_query = False
                            continue
                if result['status'] == 'verified':
                    need(proof is not None, 'result_row_proof_unavailable')
                    if on_match:
                        on_match(table)
                    if on_adapter_match:
                        on_adapter_match((count, proof))
                result['result_observation'].update(table_lookup='recorded_element', attempts=attempts,
                    wait_seconds=round(waited, 4),
                    elapsed_seconds=round(time.monotonic() - started, 4), query_freshness='not_asserted')
                return result
            retry = diagnostic in RESULT_IMAGE_ERRORS or diagnostic in ('result_value_not_found','result_column_not_found')
            # Empty once is not evidence that delayed headers/rows will never
            # appear. Only short-circuit for a previously visually verified
            # group, still unambiguously live and still exposing no table data.
            painted = (diagnostic == 'result_column_unavailable' and reviewed_group == selector
                       and table is not None and result_cell.bare_group(uia, table))
            provider_seconds += time.monotonic() - began
            remaining = deadline - time.monotonic()
            if retry and not painted and remaining > 0:
                # Retain only a uniquely resolved live element identity while
                # waiting. Each read resolves it again; no value/row is cached.
                # This avoids repeating a full window discovery every 0.2s.
                if retry_selector is None and table is not None and step['selector'] == {'role': 'table'}:
                    retry_selector = (_stable_result_selector(binding, uia, table)
                                      if selector == {'role': 'table'} else selector)
                before = time.monotonic()
                time.sleep(min(.2, remaining))
                waited += time.monotonic() - before
                continue
            strategy = 'reviewed_group_pixels' if painted else 'uia_timeout' if retry else 'uia_inconclusive'
            receipt = dict(status='blocked', input_dispatched=False, diagnostic=diagnostic)
            if painted:
                receipt['result_surface_selector'] = selector
        else:
            strategy = 'uia_matched'
            if on_match is not None:
                on_match(table)
            receipt = dict(status='verified', input_dispatched=False, diagnostic='result_value_matched',
                           value=expected, value_evidence=dict(status='observed', source=evidence.get('source','result_column')))
            if remember or selector != step['selector']:
                stable = _stable_result_selector(binding, uia, table) if selector == {'role':'table'} else selector
                if stable and ('automation_id' in stable or 'name' in stable) and (
                        'name' not in stable or expected.casefold() not in stable['name'].casefold()):
                    receipt['result_element'] = dict(selector=stable, source='element')
                    if evidence.get('column_hint'):
                        receipt['result_element']['column_hint'] = evidence['column_hint']
            provider_seconds += time.monotonic() - began
        route = ('remembered_element' if reviewed_group and selector == reviewed_group
                 else 'discovery' if step['selector'] == {'role':'table'} else 'recorded_element')
        receipt['result_observation'] = dict(strategy=strategy, attempts=attempts, wait_seconds=round(waited, 4),
            provider_seconds=round(provider_seconds, 4), elapsed_seconds=round(time.monotonic() - started, 4),
            table_lookup=route, column=step['result_column'], requested=expected, query_freshness='not_asserted')
        receipt['result_observation'].update({k: evidence[k] for k in (
            'observed', 'rows_examined', 'match_count', 'match_count_at_least', 'scope') if k in evidence})
        return receipt


def observe_result_row(binding, uia, step, guard, *, wait_seconds, on_match=None):
    """Wait for current provider rows, without text equality or image matching."""
    import result_cell
    started = time.monotonic()
    attempts, waited, provider_seconds = 0, 0., 0.
    deadline = started + wait_seconds
    retry_selector, hint_attempted = None, False
    while True:
        guard()
        attempts += 1
        began = time.monotonic()
        table = None
        try:
            table, selector = _result_target(binding, uia, step['selector'], retry_selector,
                                            element_hint=bool(retry_selector))
            result_cell.find_row(binding, uia, table, mode=step['result_row'])
        except (ValueError, app.bridge.BridgeError) as error:
            diagnostic = getattr(error, 'code', None)
            if diagnostic not in ('result_row_missing', 'result_row_unavailable', 'result_row_not_visible',
                                  'result_row_ambiguous', 'result_table_missing', 'target_missing'):
                raise
            provider_seconds += time.monotonic() - began
            remaining = deadline - time.monotonic()
            # Empty known rows may be populated by the query. An opaque
            # drawing has no row API to poll: hand off once, with this scope.
            if diagnostic in ('result_row_missing', 'result_table_missing', 'target_missing') and remaining > 0:
                if not hint_attempted and table is not None and step['selector'] == {'role': 'table'}:
                    retry_selector = _stable_result_selector(binding, uia, table)
                    hint_attempted = True
                before = time.monotonic()
                time.sleep(min(.2, remaining))
                waited += time.monotonic() - before
                continue
            receipt = dict(status='blocked', input_dispatched=False, diagnostic=diagnostic,
                           result_surface_selector=step['selector'])
            if table is not None:
                receipt['result_table_bounds'] = dict(uia.bounds(table))
            strategy = 'uia_rows_unavailable' if diagnostic == 'result_row_unavailable' else 'uia_rows_inconclusive'
        else:
            provider_seconds += time.monotonic() - began
            if on_match is not None:
                on_match(table)
            strategy = 'uia_row_found'
            receipt = dict(status='observed', input_dispatched=False, diagnostic='current_result_row_found',
                           result_row=step['result_row'], query_freshness='not_asserted', value_comparison=False)
        receipt['result_observation'] = dict(strategy=strategy, attempts=attempts, wait_seconds=round(waited, 4),
            provider_seconds=round(provider_seconds, 4), elapsed_seconds=round(time.monotonic()-started, 4),
            table_lookup='discovery' if step['selector'] == {'role': 'table'} else 'recorded_element')
        return receipt


def observe_field(binding, uia, element, role, action, expected=None):
    if role == 'textfield':
        observed = live.text_readback(binding, uia, element)
    elif role == 'combobox':
        observed = live.combo_readback(binding, uia, element)
    else:
        root = uia.from_handle(binding.hwnd)
        lookup.ancestry(uia, element, root, pid=binding.pid)
        need(app.bridge.ROLES.get(element.property(30003)) == role, 'target_changed')
        pattern = element.property(30043) is True
        value = element.property(30045) if pattern else element.property(30005)
        need(pattern or (isinstance(value, str) and bool(value)), 'value_observation_unavailable')
        binding.check()
        observed = dict(value=value, value_evidence=dict(status='observed', source='value_pattern' if pattern else 'name'))
    evidence = observed['value_evidence']
    need(isinstance(observed['value'], str) and evidence.get('status') == 'observed', 'value_observation_unavailable')
    need(len(observed['value']) <= 10000 and len(observed['value'].encode('utf8')) <= 10000, 'observed_value_too_large')
    matched = action == 'read-value' or expected == observed['value']
    if action == 'read-value':
        return dict(**observed, status='observed', diagnostic='value_observed', input_dispatched=False)
    return dict(**observed, status='verified' if matched else 'mismatch',
                diagnostic='current_state_matched' if matched else 'current_state_mismatch', input_dispatched=False)


def lookup_observation(binding, uia, step, expected, guard, *, read_only, wait_seconds):
    deadline = time.monotonic() + wait_seconds
    selector = step['selector']
    while True:
        guard()
        try:
            root = uia.from_handle(binding.hwnd)
            element, stable, resolution = resolve_step(binding, uia, root, selector)
            if step.get('field_label'):
                app.tasks.check_display({'label': step['field_label']}, app.field_label(binding, uia, element, root))
            receipt = observe_field(binding, uia, element, selector['role'],
                                    'read-value' if read_only else step['action'], expected)
            if resolution is not None and stable is not None:
                receipt['resolved_selector'] = stable
            if receipt['status'] != 'mismatch' or time.monotonic() >= deadline:
                return receipt
        except (ValueError, app.bridge.BridgeError) as error:
            if getattr(error, 'code', None) not in {'target_missing', 'value_observation_unavailable'} or time.monotonic() >= deadline:
                raise
        time.sleep(min(.2, max(0, deadline - time.monotonic())))


def confirm_closed(owner, title, surface, list_matching, uia, receipt):
    """Observe the exact bound child after a delivered close, retaining dispatch evidence on failure."""
    deadline = time.monotonic() + 3
    try:
        while True:
            app.guard(owner, owner, title)
            if not app.bridge.user32.IsWindowVisible(surface.hwnd) and not list_matching():
                if check_window_readability(owner, uia)['status'] == 'observed':
                    return dict(receipt, status='verified', diagnostic='recorded_window_closed',
                                effect_verification='matched', verification_scope='window_closed')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(.1, remaining))
    except (ValueError, OSError, app.bridge.BridgeError):
        pass
    return dict(receipt, diagnostic='window_close_unverified')


def inspect_menu(binding, uia, step, preceding):
    try:
        if step['action'] == 'context-menu':
            return context_menu.observe(binding, uia, step['spec'])
        return result_menu.observe(binding, uia, step['context'])
    except app.bridge.BridgeError as error:
        # A recorded query can create its results table. Registration does not issue it.
        if error.code != 'context_region_missing' or not any(
                s['action'] == 'invoke' and not s.get('popup') and
                s.get('window_context') == step.get('window_context') for _, s in preceding):
            raise
        return dict(status='observed', input_dispatched=False, region_observed=False,
                    point_observed=False, point_diagnostic=error.code, menu_verification='pending_execution')


def popup_surface(owner, title, spec, *, read_only=False):
    """Wait by observing only; never restore, focus or act on the owner as a fallback."""
    deadline = time.monotonic() + (0 if read_only else spec.get('timeout', 3))
    while True:
        app.guard(owner, owner, title)
        matches = [row for row in app.owned_popups(owner) if row['title'] == spec['title']
                   and app.bridge.user32.IsWindowVisible(row['hwnd'])]
        need(len(matches) <= 1, 'ambiguous_popup')
        if matches:
            surface = app.bind_surface(owner, matches[0]['hwnd'])
            need(surface.pid == owner.pid, 'popup_owner_mismatch')
            check_surface(owner, surface, title, spec)
            return surface
        if read_only:
            return None
        remaining = deadline - time.monotonic()
        need(remaining > 0, 'popup_not_found')
        time.sleep(min(.1, remaining))


def open_secondary_handles(owner, routes, steps):
    """Existing routes are already bound. Inspect only explicitly requested popup titles."""
    yield from (bound.hwnd for bound in routes.bindings.values() if bound is not None)
    popup_owners = {}
    for step in steps:
        if 'popup' not in step:
            continue
        context = step.get('window_context')
        parent = routes.bindings.get(window_context._key(context)) if context else owner
        if parent is not None:
            _, titles = popup_owners.setdefault(parent.hwnd, (parent, set()))
            titles.add(step['popup']['title'])
    for parent, titles in popup_owners.values():
        rows = app.owned_popups(parent)
        for title in titles:
            matches = [r['hwnd'] for r in rows if r['title'] == title and app.bridge.user32.IsWindowVisible(r['hwnd'])]
            if len(matches) == 1:
                yield matches[0]


def check_surface(owner, surface, title, popup=None, *, mutate=False):
    app.guard(owner, surface, title, mutate=mutate)
    if popup is not None:
        need(surface.pid == owner.pid and surface.hwnd != owner.hwnd, 'popup_owner_mismatch')
        need(app.bridge.window_text(surface.hwnd) == popup['title'], 'popup_title_changed')
        matches = [row for row in app.owned_popups(owner) if row['title'] == popup['title']
                   and app.bridge.user32.IsWindowVisible(row['hwnd'])]
        need(len(matches) == 1 and matches[0]['hwnd'] == surface.hwnd, 'popup_scope_changed')


def row_receipt(binding, title, step, *, read_only, context_guard=None):
    def guard():
        if context_guard is not None:
            context_guard()
        check_surface(binding, binding, title, mutate=not read_only)

    guard()
    if step['action'] == 'row-launch':
        return row_intent.execute(binding, title, step['ordinal'], guard, directory=app.task_directory(),
                                  preview=read_only, hint=step.get('row_hint'))
    if read_only:
        state = row_vision.observe_row(binding, step['row'])['kind']
        need(state in ('before', 'after'), 'row_' + state)
        return dict(status='observed', observed_state=state, input_dispatched=False)
    return row_action.execute(binding, step['row'], guard)


def confirm_opened_window(report, routes, number):
    if number is None or not routes.origins.get(number) or not routes.origins[number].keys() <= routes.bindings.keys():
        return
    receipt = routes.outcomes.get(number)
    if (receipt and (routes.steps[number-1]['action'],receipt.get('diagnostic')) in (
            ('invoke','business_result_not_verified'),('press-key','key_chord_sent'),('row-open','row_open_dispatched'),
            ('row-action','row_click_dispatched'),
            ('double-click-result','visual_double_click_dispatched'),
            ('visual-click','visual_double_click_dispatched'),
            ('result-menu','menu_delivered_no_output_condition'))
            and receipt['status'] == 'delivered_unverified' and receipt['input_dispatched'] is True):
        receipt.update(status='verified', effect_verification='matched',
                       verification_scope='recorded_window_opened', business_commit='not_verified')
        receipt.pop('diagnostic')
        if report['status'] == 'completed_unverified' and all(
                item['receipt']['status'] in ('verified','observed','waited') for item in report['steps']):
            report['status'] = 'verified'


def verified_detail_read(steps, rows):
    """Scope completion to the current matched row and the observed, closed detail window."""
    if len(steps) != len(rows) or not steps[-1].get('verify_closed'):
        return False
    scope = steps[-1].get('window_context', {})
    opener = scope.get('after_step', 0)
    if not 3 <= opener <= len(steps) - 2 or not (scope.get('related') or scope.get('owned')):
        return False
    match, open_row = steps[opener-2:opener]
    receipts = [row['receipt'] for row in rows]
    checked, opened = receipts[opener-2:opener]
    unverified = [i for i, receipt in enumerate(receipts) if receipt['status'] == 'delivered_unverified']
    if len(unverified) != 1 or unverified[0] >= opener-2:
        return False
    i = unverified[0]
    sent = receipts[i]
    if (not app.tasks.is_query_button(steps[i]) or sent.get('diagnostic') != 'business_result_not_verified'
            or sent.get('input_dispatched') is not True or sent['status'] != 'delivered_unverified'
            or steps[i].get('window_context') != match.get('window_context')
            or any(s['action'] not in ('wait', 'verify-value') for s in steps[i+1:opener-2])
            or 'result_column' not in match or checked['status'] != 'verified'
            or checked.get('value_evidence', {}).get('status') != 'observed'
            or open_row['action'] != 'double-click-result' or 'result_row' in open_row
            or opened.get('verification_scope') != 'recorded_window_opened' or opened['status'] != 'verified'
            or receipts[-1].get('diagnostic') != 'recorded_window_closed' or receipts[-1]['status'] != 'verified'):
        return False
    if any(s.get('window_context') != scope or 'popup' in s for s in steps[opener:]):
        return False
    detail = list(zip(steps[opener:-1], receipts[opener:-1]))
    return (any(s['action'] == 'read-value' for s, _ in detail)
            and all(s['action'] in input_policy.VALUE_OBSERVATIONS
                    and r['status'] == ('observed' if s['action'] == 'read-value' else 'verified')
                    and r.get('value_evidence', {}).get('status') == 'observed' for s, r in detail)
            and all(r['status'] in ('verified', 'observed', 'waited') or j == i
                    for j, r in enumerate(receipts[:opener-2])))


_GROUNDING_DIAGNOSTICS = {'target_missing', 'target_ambiguous', 'scope_missing', 'scope_ambiguous',
                          'workflow_pattern_unavailable'}


def _grounding_step(recipe, number, step, reason):
    request = dict(app.tasks.grounding_request(number, step), reason=reason)
    if not request['instruction']:
        request['instruction'] = step.get('label','')
    children = [s['window_context'] for s in recipe['steps'] if s.get('window_context',{}).get('after_step')==number]
    if children:
        request['expected_windows'] = children
        from row_intent import conflicts_with_launch
        if (len({(c['exe'].casefold(), c['title']) for c in children}) == 1
                and not conflicts_with_launch(request['instruction'])):
            request['row_binding_command'] = 'bind-row'
    return request


def _grounding_report(report, requests):
    return dict(report, status='needs_grounding', diagnostic='target_grounding_required',
                host_action='ground_named_rows' if all(i.get('row_binding_command') for i in requests)
                else 'ground_recorded_targets', grounding_steps=requests, stopped_at=requests[0]['number'],
                prior_input_requires_observation=report.get('input_dispatched') is not False)


def _missing_groundings(recipe, entries, owner, uia, prepare=None):
    """Check already-open scopes of recovered click instructions before dispatch."""
    openers = {s.get('window_context',{}).get('after_step') for s in recipe['steps']}
    candidates = [(n, step) for n, step in entries if step.get('grounding_text') and (
                  n == entries[0][0] or n in openers or step.get('window_context'))]
    if not candidates:
        return []
    lookup_routes = window_context.WindowRoutes(recipe['steps'], owner, recipe['window'], read_only=True,
                                                active=[n for n, _ in candidates])
    missing = []
    for number, step in candidates:
        scope = step.get('window_context')
        if number != entries[0][0] and not scope and not missing:
            # A later control may be created by an earlier action. Batch the
            # still-visible manager rows only after there is already a handoff.
            continue
        binding = lookup_routes.resolve(scope) if scope else owner
        if binding is None:
            continue
        try:
            if prepare:
                prepare(binding, scope['title'] if scope else recipe['window'])
            element, _, _ = resolve_step(binding, uia, uia.from_handle(binding.hwnd), step['selector'])
            need('Click' in uia.actions(element), 'workflow_pattern_unavailable')
        except (app.SelectionError, app.bridge.BridgeError, WorkflowError) as error:
            if error.code not in _GROUNDING_DIAGNOSTICS:
                error.step_number = number
                raise
            missing.append(_grounding_step(recipe, number, step, error.code))
    return missing


def check_result_point(binding, uia, selector, point, *, reviewed_frame=None):
    table = result_surface(binding, uia, selector)
    root = uia.from_handle(binding.hwnd)
    box = uia.bounds(table)
    x, y = point
    visible = live._visible(uia, table, binding, root)
    if reviewed_frame is not None:
        import result_trial
        region = result_trial.visible_table_box(reviewed_frame, box)
        need(region['x'] <= x < region['x'] + region['width']
             and region['y'] <= y < region['y'] + region['height'], 'result_point_outside_frame')
    if not visible and reviewed_frame is not None and table.property(30022) is None:
        # The exact captured visible portion has just been reviewed and compared.
        # A provider may expose its scrollable table larger than this HWND. Prove
        # ownership and the actual pointed data surface instead of accepting the
        # full off-window rectangle as a general visibility override.
        app.visibility.require_observable(binding)
        need(bool(app.bridge.user32.IsWindowVisible(binding.hwnd)), 'result_point_outside_table')
        lookup.ancestry(uia, table, root, pid=binding.pid)
        hit = app.bridge.window_from_point(x, y)
        need(hit and app.bridge.root_window(hit) == binding.hwnd
             and app.bridge.hwnd_pid(hit) == binding.pid, 'result_cell_occluded')
        visible = True
    need(visible, 'result_point_outside_table')
    need(box['x'] <= x < box['x'] + box['width'] and box['y'] <= y < box['y'] + box['height'],
         'result_point_outside_table')
    # Owner-drawn cells may expose only their group, image or pane. A sibling grid,
    # header, button overlay or editable field must never become a result-row click.
    chain = lookup.ancestry(uia, uia.from_point(x, y), table, pid=binding.pid)
    data_roles = {50006, 50007, 50008, 50020, 50025, 50026, 50028, 50029, 50033, 50036}
    need(all(item.property(30003) in data_roles and item.property(30149) is not True
             and (item.property(30043) is not True or item.property(30046) is True) for item in chain),
         'result_point_not_data')
    return box


def check_result_frame(binding, confirmation, point, guard):
    """Authenticate the reviewed, masked window image directly before the pointer gesture."""
    import grounding
    scope = confirmation['receipt'].encode('ascii')
    _, witnessed = grounding.read_frame(grounding._directory(), scope, confirmation['frame'])
    rect, current = grounding._capture(app, binding, guard)
    relative_point = [point[0] - rect[0], point[1] - rect[1]]
    need(relative_point == confirmation['point'], 'result_frame_changed')
    if 'table_box' in confirmation:
        import result_trial
        need(list(rect) == list(confirmation['window_rect']) and
             result_trial.same_table_pixels(witnessed, current, rect, confirmation['table_box']),
             'result_frame_changed')
    else:
        need(current == witnessed, 'result_frame_changed')
    with app.bridge.UIA() as uia:
        if confirmation.get('original_selector'):
            _, selector = _result_target(binding, uia, confirmation['original_selector'], confirmation['selector'])
            need(selector == confirmation['selector'], 'result_table_changed')
        box = check_result_point(binding, uia, confirmation['selector'], point,
                                 reviewed_frame=rect if 'table_box' in confirmation else None)
        if 'table_box' in confirmation:
            need(box == confirmation['table_box'], 'result_table_changed')


def checked_result_hints(recipe, saved):
    result_hints = {}
    if saved is None:
        return result_hints
    if not isinstance(saved, list):
        saved = [saved]
    for hint in saved:
        import result_cell
        need(isinstance(hint, dict), 'invalid_result_surface_hint')
        number = hint.get('number')
        selector = hint.get('selector')
        need(type(number) is int and 1 <= number <= len(recipe['steps'])
             and 'result_column' in recipe['steps'][number-1] and isinstance(selector, dict)
             and number not in result_hints and isinstance(selector.get('role'), str) and selector['role'] in RESULT_SURFACES
             and ('name' in selector or 'automation_id' in selector)
             and recipe['steps'][number-1]['selector'] in ({'role': 'table'}, selector)
             and ('source' not in hint or hint['source'] == 'element')
             and ('column_hint' not in hint or result_cell.valid_column_hint(hint['column_hint'])),
             'invalid_result_surface_hint')
        lookup._selector(selector)
        result_hints[number] = hint
    return result_hints


def run(recipe, overrides=None, *, read_only=False, hwnd=None, allow_recorded_selection=False,
        background_only=False, only=None, scene_session=None, resume=None, track_menu=False,
        registered_session=None, result_confirmation=None, result_surface_hint=None,
        remember_result_elements=False, result_owner=None):
    recipe = validate(recipe)
    result_hints = checked_result_hints(recipe, result_surface_hint)
    if hwnd is not None:
        hwnd = app.bridge.normalize_hwnd(hwnd)
        need(hwnd, 'invalid_target')
    if result_confirmation is not None:
        first = recipe['steps'][0]
        need(not read_only and only is None and resume is None and registered_session is None and
             first['action'] == 'visual-click' and app.tasks.is_double_click_step(first) and
             not first.get('window_context') and all(s['action'] != 'conditional-open' for s in recipe['steps']) and
             hwnd is not None and hwnd == app.bridge.normalize_hwnd(result_confirmation['owner']['hwnd']),
             'invalid_result_resume')
    if registered_session is not None:
        # A conditional screen prompt returns control to the host while its worker
        # remains alive. Keep correction receipts and the single-use lock in that worker.
        need(scene_session is not None, 'menu_repair_session_unavailable')
        args = SimpleNamespace(**registered_session)
        args.recipe = Path(args.recipe)
        need(workflow_repair.registered(args.recipe), 'registered_skill_required')
        return workflow_repair.run_registered(sys.modules[__name__], recipe, args, overrides or {}, scene_session)
    entries = selected_steps(recipe, only)
    requested = [step for _, step in entries]
    if resume is not None:
        entries = [(n, s) for n, s in entries if n >= resume['number']]
    active = [step for _, step in entries]
    mutates_window = not read_only and any(s['action'] not in input_policy._OBSERVATION_ACTIONS for s in active)
    requirements = input_policy.profile(active, [n for n, _ in entries])
    if background_only and not read_only:
        refusal = input_policy.blocked(requirements)
        if refusal:
            return refusal
    values, missing = values_for(dict(recipe, steps=requested), overrides or {})
    if missing and not read_only:
        return dict(status='needs_input', missing_parameters=missing, steps=[], input_dispatched=False)
    if not read_only:
        refusal = input_policy.interactive_desktop_blocked(requirements)
        if refusal:
            return refusal
    # A relative row by itself means "start the current Nth row". When a
    # conditional instruction explicitly targets the window it opened, starting
    # the new occupant at that ordinal could dispatch into a different device.
    if not read_only:
        requested = dict(entries)
        unresolved = []
        for number, step in enumerate(recipe['steps'],1):
            scope = step.get('window_context', {})
            opener = scope.get('after_step')
            source = requested.get(opener, {})
            if step['action'] == 'conditional-open' and source.get('action') == 'row-launch':
                unresolved.append(dict(number=opener, child_step=number, expected_window=scope['title'],
                                       expected_exe=scope['exe'], instruction=source['label'],
                                       row_binding_command='bind-row'))
        if unresolved:
            return dict(status='needs_grounding', diagnostic='named_window_row_identity_required', host_action='ground_named_rows',
                        grounding_steps=unresolved, stopped_at=unresolved[0]['number'], steps=[],
                        input_dispatched=False)
    if not read_only and scene_session is None and any(s['action']=='conditional-open' for s in active):
        from scene_session import start
        return start(__file__, recipe, dict(overrides=overrides, hwnd=hwnd, only=only, resume=resume,
                                            track_menu=track_menu, allow_recorded_selection=allow_recorded_selection),
                     worker_timeout(recipe))
    report = dict(status='observed' if read_only else 'verified', steps=[], input_dispatched=False,
                  business_commit='not_verified', input_requirements=requirements,
                  input_policy='background_only' if background_only else 'auto')
    started = time.monotonic()
    result_baselines = {}
    verified_tables = []
    adapter_matches = []
    number, dispatching = 0, False
    anchor_proof = None
    reservations = ExitStack()
    stage_started = started
    try:
        owner = bind(recipe['exe'], recipe['window'], hwnd)
        if result_confirmation or result_owner:
            identity = result_confirmation['owner'] if result_confirmation else result_owner
            need(workflow_repair.matches_identity(owner, [identity['hwnd'], identity['pid'], identity['process_started_utc_ticks']])
                 and recipe['window'] == identity['title'] and recipe['exe'].casefold() == identity['exe_name'].casefold(),
                 'result_window_changed')
        reserved_handles = {owner.hwnd}
        if not read_only:
            # A selected action can still produce a file checked by a later,
            # unselected verify-file step. Reserve it without running that check.
            checks = recipe['steps'] if only is not None and mutates_window else ()
            reservations.enter_context(coordination.reserve_outputs(active, verify_steps=checks))
        if mutates_window:
            scopes = coordination.scopes_for(active, recipe['exe'])
            reservations.enter_context(coordination.window(dict(hwnd=owner.hwnd, exe_name=recipe['exe']),
                                                            recipe['window'], scopes))

        def reserve_surface(binding):
            if mutates_window and binding.hwnd not in reserved_handles:
                reservations.enter_context(coordination.handle(binding))
                reserved_handles.add(binding.hwnd)

        if resume:
            need(workflow_repair.matches_identity(owner, resume['scope'][0]), 'menu_repair_window_changed')
        if background_only and not read_only:
            input_policy.window_ready(owner)
        active_scopes = window_context.effect_scopes(recipe['steps'], (n for n, _ in entries))
        routes = window_context.WindowRoutes(recipe['steps'], owner, recipe['window'], read_only=read_only,
                                              active=active_scopes)
        if resume:
            workflow_repair.restore_routes(sys.modules[__name__], recipe, resume, routes)
        if mutates_window and scopes:
            coordination.reserve_handles(reservations, reserved_handles, open_secondary_handles(owner, routes, active))
        # Capture every requested output before window restoration or any input.
        baselines = {i: export_evidence.baseline(step['output']) for i, step in entries
                     if step['action'] == 'verify-file'} if not read_only else {}
        report['window_restored'] = False
        prepared = set()

        def prepare(binding, title):
            if read_only:
                app.visibility.require_observable(binding)
                return
            if binding.hwnd in prepared:
                return
            reserve_surface(binding)
            if background_only:
                input_policy.window_ready(binding)
            else:
                restored = app.visibility.restore_for_action(binding, title)
                report['window_restored'] = bool(restored) or report['window_restored']
            prepared.add(binding.hwnd)

        pending_opener = None
        with app.bridge.UIA() as uia:
            if background_only and not read_only:
                input_policy.configure_uia(uia)
            elif mutates_window and any(row['input_mode'] == 'accessibility' for row in requirements['steps']):
                input_policy.prefer_background(uia)
            if mutates_window:
                first = active[0]
                if ('window_context' not in first and 'popup' not in first
                        and first['action'] not in input_policy._OBSERVATION_ACTIONS):
                    prepare(owner, recipe['window'])
                check = check_window_readability(owner, uia)
                report['readability_check'] = check
                if check['status'] != 'observed':
                    raise WorkflowError(check['diagnostic'])
            missing = _missing_groundings(recipe, entries, owner, uia, prepare)
            if missing:
                return _grounding_report(report, missing)
            if resume:
                for field in resume['checked_fields']:
                    prior = recipe['steps'][field - 1]
                    selector = prior['selector']
                    root = uia.from_handle(owner.hwnd)
                    element, _, _ = resolve_step(owner, uia, root, selector)
                    reader = live.text_readback if selector['role'] == 'textfield' else live.combo_readback
                    expected = prior.get('value') if 'value' in prior else values[prior['parameter'].casefold()]
                    need(reader(owner, uia, element).get('value') == expected, 'menu_repair_conditions_changed')
            report['preparation_seconds'] = round(time.monotonic() - started, 4)
            for index, (number, step) in enumerate(entries):
                stage_started = time.monotonic()
                before_key, after_key = (0, 0) if read_only else _key_intervals(active, index)
                app.guard(owner, owner, recipe['window'])
                context = step.get('window_context')
                if pending_opener is not None and (step['action'] not in ('wait', 'verify-file')
                        or number == entries[-1][0] or (context or {}).get('after_step') == pending_opener):
                    # Two launch buttons may have been recorded before work in
                    # either child. Prove the first result before sending the
                    # second command; its eventual target lookup is too late.
                    for expected in routes.origins[pending_opener].values():
                        routes.resolve(expected)
                    confirm_opened_window(report, routes, pending_opener)
                    pending_opener = None
                binding = routes.resolve(context) if context else owner
                if resume and number == resume['number']:
                    need(binding is not None and workflow_repair.matches_identity(binding, resume['scope'][1]),
                         'menu_repair_window_changed')
                title = context['title'] if context else recipe['window']
                if context and binding is not None and not read_only:
                    confirm_opened_window(report, routes, context.get('after_step'))
                if binding is None:
                    pending = dict(window_context=deepcopy(context), diagnostic='recorded_window_not_open')
                    if step.get('grounding_text'):
                        pending.update(instruction=step['grounding_text'], query=step['selector']['query'])
                    report.setdefault('pending_scopes', []).append(dict(number=number, **pending))
                    receipt = dict(status='deferred', input_dispatched=False, scope_validation='pending', **pending)
                    report['steps'].append(dict(number=number, label=step.get('label', ''), receipt=receipt,
                                                elapsed_seconds=round(time.monotonic() - stage_started, 4)))
                    continue
                reserve_surface(binding)
                if before_key:
                    _pause_until(stage_started + before_key)
                routes.before(number, binding)

                def check_context():
                    if context is not None:
                        routes.check(context, binding)

                def check_target(surface, popup=None, *, mutate=False):
                    check_context()
                    check_surface(binding, surface, title, popup, mutate=mutate)
                after_query = (step['action'] == 'verify-value' and number > 1 and
                               app.tasks.is_query_button(recipe['steps'][number-2]) and
                               recipe['steps'][number-2].get('window_context') == context)
                # A popup may disable its owner. Only actions on that owner
                # require it to be enabled/restored; missing popups never do.
                if 'popup' not in step and step['action'] in input_policy.VALUE_OBSERVATIONS:
                    app.visibility.require_observable(binding)
                elif 'popup' not in step and step['action'] not in ('wait', 'verify-file'):
                    prepare(binding, title)
                if (not read_only and step['action'] == 'invoke' and not routes.origins.get(number)
                        and app.tasks.query_before_double_click(step, requested.get(number + 1))):
                    import visual_target
                    # A matching row may still be left over from the previous query.
                    try:
                        visual_target.observe(binding, requested[number + 1]['visual'])
                    except app.bridge.BridgeError as error:
                        result_baselines[number + 1] = error.code
                    else:
                        result_baselines[number + 1] = 'already_visible'
                if step['action'] == 'wait':
                    if read_only:
                        continue
                    wait(binding, title, step['seconds'])
                    receipt = dict(status='waited', seconds=step['seconds'], input_dispatched=False,
                                   business_result='not_verified')
                elif step['action'] == 'verify-file':
                    if read_only:
                        export_evidence.inventory(step['output'])
                        receipt = dict(status='observed', input_dispatched=False,
                                       output_validation='path_observed', file_verification='pending_execution')
                    else:
                        ok, diagnostic = export_evidence.wait(step['output'], baselines[number],
                                                              timeout=step.get('timeout', 26))
                        app.guard(binding, binding, title)
                        receipt = dict(status='verified' if ok else 'blocked', input_dispatched=False,
                                       diagnostic=diagnostic, file_verified=ok,
                                       baseline_scope='before_resumed_menu' if resume else 'before_workflow',
                                       business_commit='not_verified')
                        if ok and getattr(baselines[number], 'verified_path', None):
                            receipt['verified_path'] = baselines[number].verified_path
                elif 'result_column' in step or (read_only and after_query):
                    popup = step.get('popup')
                    if read_only:
                        # Registration cannot inspect the result of a query it never sent.
                        receipt = dict(status='deferred', input_dispatched=False, verification='pending_execution',
                                       diagnostic='validate_result_on_execution')
                    else:
                        surface = popup_surface(binding, title, popup) if popup else binding
                        reserve_surface(surface)
                        expected = values[step['parameter'].casefold()] if 'parameter' in step else step['value']
                        hint = result_hints.get(number, {})
                        verified_tables.clear()
                        adapter_matches.clear()
                        receipt = observe_result(surface, uia, step, expected, lambda: check_target(surface, popup),
                                                 wait_seconds=RESULT_VISUAL_TIMEOUT, reviewed_group=hint.get('selector'),
                                                 column_hint=hint.get('column_hint'), element_hint=hint.get('source')=='element',
                                                 on_match=verified_tables.append, remember=remember_result_elements,
                                                 on_adapter_match=adapter_matches.append,
                                                 after_query=after_query and number-1 in requested)
                        if receipt.get('result_element'):
                            result_hints[number] = receipt['result_element']
                        if receipt['status'] == 'blocked':
                            check_target(surface, popup)
                            receipt['result_window'] = dict(hwnd=surface.hwnd, pid=surface.pid, exe_name=surface.exe,
                                title=popup['title'] if popup else title, process_started_utc_ticks=surface.created)
                elif step['action'] == 'double-click-result':
                    if read_only:
                        receipt = dict(status='deferred', input_dispatched=False, verification='pending_execution',
                                       diagnostic='validate_result_on_execution')
                    elif 'result_row' in step:
                        import result_cell
                        tables = []
                        receipt = observe_result_row(binding, uia, step, lambda: check_target(binding),
                                                     wait_seconds=RESULT_VISUAL_TIMEOUT, on_match=tables.append)
                        if receipt['status'] == 'observed':
                            table = tables[0]
                            def row_guard():
                                check_target(binding, mutate=True)
                                need(uia.same(result_surface(binding, uia, step['selector']), table),
                                     'result_table_changed')
                            observed = receipt
                            receipt = result_cell.execute_row(binding, uia, table, row_guard, mode=step['result_row'])
                            receipt.update(result_observation=observed['result_observation'],
                                           result_row=step['result_row'], query_freshness='not_asserted',
                                           value_comparison=False)
                        else:
                            receipt['result_window'] = dict(hwnd=binding.hwnd, pid=binding.pid, exe_name=binding.exe,
                                title=title, process_started_utc_ticks=binding.created)
                    else:
                        import result_cell
                        prior = recipe['steps'][number - 2]
                        expected = (values[prior['parameter'].casefold()] if 'parameter' in prior else prior['value'])
                        check_target(binding, mutate=True)
                        hint = result_hints.get(number - 1, {})
                        def current_table():
                            return _result_target(binding, uia, prior['selector'], hint.get('selector'),
                                                  column=prior['result_column'], element_hint=hint.get('source')=='element')[0]
                        table = current_table()
                        need(verified_tables and uia.same(verified_tables.pop(), table),
                             'result_table_changed')
                        def result_guard():
                            check_target(binding, mutate=True)
                            need(uia.same(current_table(), table), 'result_table_changed')
                        # execute() reports all attempted input as a receipt.
                        # Its lookup/foreground guards can raise before input.
                        if adapter_matches:
                            import result_row_adapter
                            count, proof = adapter_matches.pop()
                            def check_count():
                                need(result_count(binding, uia, prior['result_adapter']) == count,
                                     'result_count_changed')
                            receipt = result_row_adapter.execute(binding, uia, table, proof, result_guard,
                                                                 check_count=check_count)
                        else:
                            receipt = result_cell.execute(binding, uia, table, expected, result_guard,
                                                          column=prior['result_column'], column_hint=hint.get('column_hint'))
                elif step['action'] == 'conditional-open':
                    if read_only:
                        receipt = dict(status='observed',input_dispatched=False,condition='pending_execution')
                    else:
                        receipt = conditional_action.execute(binding,step,lambda:check_target(binding,mutate=True),
                                                             scene_session,report,number,title)
                elif step['action'] == 'visual-click':
                    import visual_target
                    def click_guard():
                        check_target(binding, mutate=not read_only)
                    click_guard()
                    baseline = result_baselines.get(number)
                    if baseline is not None and baseline != 'visual_target_not_found':
                        diagnostic = 'query_result_freshness_unproven'
                        report.update(status='needs_review', stopped_at=number, diagnostic=diagnostic,
                                      host_action='verify_current_query_result_then_test_only_this_step')
                        report['steps'].append(dict(number=number, label=step['label'],
                            receipt=dict(status='needs_review', input_dispatched=False, diagnostic=diagnostic,
                                         before_query=baseline), elapsed_seconds=round(time.monotonic()-stage_started, 4)))
                        break
                    dispatching = not read_only
                    try:
                        opener = routes.outcomes.get((context or {}).get('after_step'), {})
                        first_match_timeout = CHILD_VISUAL_TIMEOUT if not read_only and opener.get('input_dispatched') is True else 0
                        if baseline == 'visual_target_not_found' and not first_match_timeout:
                            first_match_timeout = RESULT_VISUAL_TIMEOUT
                        options = {}
                        if result_confirmation and number == 1:
                            options['before_input'] = lambda point: check_result_frame(
                                binding, result_confirmation, point, click_guard)
                            if 'table_box' in result_confirmation and 'window_rect' in result_confirmation:
                                options['reviewed_point'] = [result_confirmation['window_rect'][i]
                                                             + result_confirmation['point'][i] for i in range(2)]
                        receipt = visual_target.execute(binding, step['visual'], click_guard,
                                                        read_only=read_only, background_only=background_only,
                                                        first_match_timeout=first_match_timeout, **options)
                    except app.bridge.BridgeError as error:
                        dispatching = getattr(error, 'input_dispatched', False) is not False
                        raise
                    dispatching = False
                elif step['action'] == 'row-open':
                    import row_target
                    receipt = row_target.execute(binding,step['row_target'],lambda:check_target(binding,mutate=not read_only),
                                                 read_only=read_only,child_open=lambda:routes.existing_for_row(number))
                elif step['action'] in ('row-action', 'row-launch'):
                    dispatching = not read_only
                    try:
                        receipt = row_receipt(binding, title, step, read_only=read_only, context_guard=check_context)
                    except (app.bridge.BridgeError, coordination.CoordinationError) as error:
                        # Both shared row engines report any input they already
                        # sent in their receipt. A failed input reservation precedes dispatch.
                        dispatching = getattr(error, 'input_dispatched', False) is not False
                        raise
                    dispatching = False
                elif step['action'] == 'context-menu':
                    check_target(binding, mutate=not read_only)
                    if read_only:
                        receipt = inspect_menu(binding, uia, step, ((n,s) for n,s in entries if n < number))
                    else:
                        def context_guard():
                            check_target(binding, mutate=True)
                        dispatching = True
                        receipt = context_menu.execute(binding, step['spec'], context_guard)
                        dispatching = False
                elif step['action'] == 'result-menu':
                    spec = step['context']
                    if resume and number == resume['number'] and resume['query_sent']:
                        spec = dict(spec, trigger=None)
                    check_target(binding, mutate=not read_only)
                    if read_only:
                        receipt = inspect_menu(binding, uia, step, ((n,s) for n,s in entries if n < number))
                    else:
                        dispatching = True
                        receipt = result_menu.execute(binding, spec, lambda: check_target(binding, mutate=True),
                                                      title, observe_guard=lambda: check_target(binding))
                        dispatching = False
                else:
                    lookup_started = time.monotonic()
                    popup = step.get('popup')
                    surface = popup_surface(binding, title, popup, read_only=read_only) if popup else binding
                    if surface is None:
                        pending = dict(number=number, popup=deepcopy(popup), diagnostic='popup_not_open')
                        report.setdefault('pending_scopes', []).append(pending)
                        receipt = dict(status='deferred', input_dispatched=False, scope_validation='pending',
                                       diagnostic='popup_not_open', popup=deepcopy(popup))
                        report['steps'].append(dict(number=number, label=step.get('label', ''), receipt=receipt,
                                                    elapsed_seconds=round(time.monotonic() - stage_started, 4)))
                        continue
                    reserve_surface(surface)
                    observation = step['action'] in input_policy.VALUE_OBSERVATIONS
                    check_target(surface, popup, mutate=not read_only and not observation)
                    if read_only and popup:
                        app.visibility.require_observable(surface)
                    if background_only and not read_only:
                        input_policy.window_ready(surface)
                    selector = step['selector']
                    if observation:
                        expected = values.get(step.get('parameter','').casefold(), step.get('value'))
                        receipt = lookup_observation(surface, uia, step, expected, lambda: check_target(surface, popup),
                                                     read_only=read_only, wait_seconds=RESULT_VISUAL_TIMEOUT if after_query else 0)
                    else:
                        root = uia.from_handle(surface.hwnd)
                    if not observation and not read_only and 'anchor' in selector:
                        # Resolve the relative position in this window's UIA
                        # subtree. A covering application need not lose focus.
                        anchor_proof = lookup.bind_anchored(surface, uia, selector, root=root)
                        element, stable, resolution = anchor_proof.element, None, None
                    elif not observation:
                        element, stable, resolution = resolve_step(surface, uia, root, selector)
                    if step.get('grounding_text'):
                        need('Click' in uia.actions(element), 'workflow_pattern_unavailable')
                    need('command_name' not in step or element.property(30005) == step['command_name'],
                         'command_name_changed')
                    if not observation and step.get('field_label'):
                        app.tasks.check_display({'label': step['field_label']},
                                                app.field_label(surface, uia, element, root))
                    lookup_elapsed = round(time.monotonic() - lookup_started, 4)
                    if not observation and read_only:
                        receipt = dict(status='observed', input_dispatched=False, actions=uia.actions(element))
                        if resolution is not None:
                            receipt['target_resolution'] = resolution
                            if stable is not None:
                                receipt['resolved_selector'] = stable
                        if step['action'] == 'set-value':
                            reader = live.text_readback if selector['role'] == 'textfield' else live.combo_readback
                            receipt.update(reader(surface, uia, element))
                        elif step['action'] == 'submit':
                            live.submit_ready(surface, uia, element, {'role': selector['role']})
                            receipt['submit_validation'] = 'editable_field_observed'
                        elif step['action'] == 'press-key':
                            live.key_target_ready(surface, uia, element, {'role': selector['role']})
                            receipt['keys'] = step['keys']
                        else:
                            receipt['command_name'] = element.property(30005) or ''
                    elif not observation:
                        def check():
                            check_target(surface, popup, mutate=True)
                            if background_only:
                                input_policy.window_ready(surface)
                            current = (anchor_proof.revalidate() if anchor_proof is not None else
                                       revalidate_step(surface, uia, root, selector, stable, resolution, element))
                            need('command_name' not in step or current.property(30005) == step['command_name'],
                                 'command_name_changed')
                            if step.get('field_label'):
                                app.tasks.check_display({'label': step['field_label']},
                                                        app.field_label(surface, uia, current, root))
                            if (step['action'] != 'set-value' or selector['role'] == 'textfield' or
                                    not (allow_recorded_selection or step.get('allow_option_select'))):
                                capability = {'set-value': 'SetValue', 'invoke': 'Click', 'submit': 'SetFocus', 'press-key': 'SetFocus',
                                              'choose-item': 'Select'}[step['action']]
                                need(capability in uia.actions(current),
                                     'workflow_pattern_unavailable')
                        value = values[step['parameter'].casefold()] if 'parameter' in step else step.get('value')
                        if step['action'] == 'press-key':
                            value = step['keys']
                        dispatching = True
                        operation = (('fill' if selector['role'] == 'textfield' else 'select')
                                     if step['action'] == 'set-value' else
                                     'choose' if step['action'] == 'choose-item' else step['action'])
                        try:
                            receipt = live.execute(surface, uia, element, {'role': selector['role']}, operation,
                                                   value, still_valid=check, just_resolved='automation_id' in selector)
                            if after_key:
                                key_settled_at = time.monotonic() + after_key
                        finally:
                            if anchor_proof is not None:
                                anchor_proof.close()
                                anchor_proof = None
                        dispatching = False
                        if (step.get('verify_closed') and receipt['status'] == 'delivered_unverified'
                                and receipt.get('input_dispatched') is True):
                            if popup:
                                def matches():
                                    return [r for r in app.owned_popups(binding) if r['title'] == popup['title']
                                            and app.bridge.user32.IsWindowVisible(r['hwnd'])]
                                receipt = confirm_closed(binding, title, surface, matches, uia, receipt)
                            else:
                                def matches():
                                    if context.get('owned'):
                                        return routes.matching_windows(context)
                                    return [r for r in app.windows(context['exe']) if r['title'] == title]
                                receipt = confirm_closed(owner, recipe['window'], surface, matches, uia, receipt)
                        if resolution is not None:
                            receipt['target_resolution'] = resolution
                    receipt['lookup_seconds'] = lookup_elapsed
                    if popup:
                        receipt['popup'] = dict(title=popup['title'])
                routes.after(number, receipt)
                if track_menu and workflow_repair.menu(step) and (workflow_repair.point_failed(receipt)
                                                                  or workflow_repair.input_paused(receipt)):
                    report['menu_repair_scope'] = [workflow_repair.identity(owner), workflow_repair.identity(binding)]
                    report['menu_repair_routes'] = workflow_repair.saved_routes(routes, number)
                if context:
                    receipt['window_context'] = deepcopy(context)
                report['steps'].append(dict(number=number, label=step.get('label', ''), receipt=receipt,
                                            elapsed_seconds=round(time.monotonic() - stage_started, 4)))
                report['input_dispatched'] = combine(report['input_dispatched'], receipt['input_dispatched'])
                if receipt.get('physical_input'):
                    report['physical_input'] = True
                if (step['action']=='row-open' and receipt['status']=='blocked' and receipt['input_dispatched'] is False
                        and receipt.get('diagnostic') in ('row_identity_missing','row_identity_ambiguous',
                                                         'row_launch_button_not_found','row_launch_button_ambiguous',
                                                         'row_launch_button_outside_recorded_column')):
                    return _grounding_report(report, [_grounding_step(recipe, number, step, receipt['diagnostic'])])
                row_opened = (step['action']=='row-open' and (receipt['status']=='verified' or
                              (receipt.get('diagnostic')=='row_open_dispatched' and receipt['input_dispatched'] is True)))
                delivered_row_click = (step['action'] == 'row-action' and step['row']['version'] == 2
                                       and receipt['status'] == 'delivered_unverified'
                                       and receipt.get('diagnostic') == 'row_click_dispatched'
                                       and receipt['input_dispatched'] is True)
                double_sent = ((app.tasks.is_double_click_step(step) or step['action'] == 'double-click-result') and receipt['status']=='delivered_unverified'
                               and receipt.get('diagnostic')=='visual_double_click_dispatched'
                               and receipt['input_dispatched'] is True)
                if not read_only and (row_opened or delivered_row_click or double_sent) and routes.origins.get(number):
                    for expected in routes.origins[number].values():
                        routes.resolve(expected)
                    confirm_opened_window(report,routes,number)
                if read_only and step['action'] in ('visual-click','row-open') and receipt['status'] != 'observed':
                    report.update(status=receipt['status'], stopped_at=number,
                                  diagnostic=receipt.get('diagnostic', 'visual_target_unavailable'))
                    break
                if not read_only and step['action'] == 'result-menu':
                    # Match the recording player's stop on unverified delivery. A new
                    # output may confirm completion; a delivered leaf alone cannot.
                    if (receipt['status'] == 'verified' and receipt.get('workbook_verified') is True
                            and receipt.get('menu_path_completed') is True and receipt.get('leaf_dispatched') is True
                            and receipt.get('effect_verification') == 'matched'):
                        continue
                    if (routes.origins.get(number) and receipt.get('diagnostic') == 'menu_delivered_no_output_condition'
                            and receipt['status'] == 'delivered_unverified' and receipt['input_dispatched'] is True
                            and receipt.get('menu_path_completed') is True and receipt.get('leaf_dispatched') is True):
                        # A demonstrated dialog is a separate, observable outcome.
                        # Wait for its exact window before any dependent action.
                        pending_opener = number
                        report['status'] = 'completed_unverified'
                        continue
                    report.update(status='blocked' if receipt['status'] == 'verified' else receipt['status'],
                                  stopped_at=number, diagnostic=receipt.get('diagnostic', 'step_not_verified'))
                    break
                if read_only or (step['action'] == 'read-value' and receipt['status'] == 'observed'
                                 or step['action'] not in ('context-menu', 'submit', 'press-key') and receipt['status'] in ('verified', 'waited')):
                    continue
                delivered = (step['action'] == 'invoke' and receipt['status'] == 'delivered_unverified'
                             and receipt['input_dispatched'] is True
                             and receipt.get('diagnostic') == 'business_result_not_verified')
                delivered_menu = (step['action'] == 'context-menu' and receipt['status'] == 'delivered_unverified'
                                  and receipt['input_dispatched'] is True and receipt.get('leaf_dispatched') is True
                                  and receipt.get('menu_path_completed') is True
                                  and receipt.get('diagnostic') == 'menu_leaf_sent')
                delivered_submit = (step['action'] == 'submit' and receipt['status'] == 'delivered_unverified'
                                    and receipt['input_dispatched'] is True and receipt.get('keyboard_events_sent') == 2
                                    and receipt.get('diagnostic') == 'submit_enter_sent')
                delivered_keys = (step['action'] == 'press-key' and receipt['status'] == 'delivered_unverified'
                                  and receipt['input_dispatched'] is True and receipt.get('keys') == step['keys']
                                  and receipt.get('diagnostic') == 'key_chord_sent'
                                  and receipt.get('keyboard_events_sent') == 2 * len(key_chords.virtual_keys(step['keys'])))
                delivered_visual = (step['action'] == 'visual-click' and receipt['status'] == 'delivered_unverified'
                                    and receipt['input_dispatched'] is True
                                    and receipt.get('diagnostic') == ('visual_double_click_dispatched'
                                        if app.tasks.is_double_click_step(step) else 'visual_click_dispatched'))
                delivered_conditional = (step['action'] == 'conditional-open' and receipt['status'] == 'delivered_unverified'
                                         and receipt.get('diagnostic') == 'conditional_open_dispatched'
                                         and receipt['input_dispatched'] is True)
                if (delivered or delivered_menu or delivered_submit or delivered_keys or delivered_visual or delivered_conditional
                        or delivered_row_click or double_sent):
                    report['status'] = 'completed_unverified'
                    if delivered_keys and routes.origins.get(number):
                        for expected in routes.origins[number].values():
                            routes.resolve(expected)
                        confirm_opened_window(report, routes, number)
                    elif delivered and routes.origins.get(number):
                        pending_opener = number
                    if after_key and (delivered_keys or delivered_submit):
                        # Observing a window opened by the key can use the same interval.
                        # The physical input lock has already been released.
                        _pause_until(key_settled_at)
                        report['steps'][-1]['elapsed_seconds'] = round(time.monotonic() - stage_started, 4)
                else:
                    report.update(status=receipt['status'], stopped_at=number,
                                  diagnostic=receipt.get('diagnostic', 'step_not_verified'))
                    if step['action'] in ('context-menu', 'submit', 'press-key') and receipt['status'] in ('verified', 'waited'):
                        report.update(status='blocked', diagnostic='submission_completion_unproven')
                    break
    except Exception as error:
        diagnostic = getattr(error, 'code', 'provider_error')
        if not number and getattr(error, 'step_number', None) in dict(entries):
            number = error.step_number
            step = recipe['steps'][number - 1]
        failure = dict(status='uncertain' if dispatching else 'blocked',
                       input_dispatched=None if dispatching else False, diagnostic=diagnostic)
        if number:
            report['stopped_at'] = number
        if number and (not report['steps'] or report['steps'][-1]['number'] != number):
            report['steps'].append(dict(number=number, label=step.get('label', ''), receipt=failure,
                                        elapsed_seconds=round(time.monotonic() - stage_started, 4)))
        report['input_dispatched'] = combine(report['input_dispatched'], failure['input_dispatched'])
        report.update(status=failure['status'], diagnostic=diagnostic)
        if not dispatching and number and step.get('grounding_text') and diagnostic in _GROUNDING_DIAGNOSTICS:
            report = _grounding_report(report, [_grounding_step(recipe, number, step, diagnostic)])
    finally:
        try:
            if anchor_proof is not None:
                anchor_proof.close()
        finally:
            reservations.close()
    if (report['status'] == 'completed_unverified' and 'stopped_at' not in report
            and only is None and not resume and verified_detail_read(recipe['steps'], report['steps'])):
        report.update(status='verified', outcome='current_result_detail_read_and_closed',
                      query_freshness='not_asserted')
    report['elapsed_seconds'] = round(time.monotonic() - started, 4)
    return report


def discover(exe, title, hwnd=None):
    """Only form controls, with nearby labels; never enumerate option inventories."""
    binding = bind(exe, title, hwnd)
    found = []
    with app.bridge.UIA() as uia:
        root = uia.from_handle(binding.hwnd)
        for element in lookup.find_all(uia, root, roles=('combobox', 'textfield', 'button', 'splitbutton'), limit=256):
            if element.property(30003) == 50004:
                try:
                    live.textfield_scope(binding, uia, element)
                except app.bridge.BridgeError as error:
                    if error.code == 'textfield_requires_combo':
                        continue
                    raise
            identifier = element.property(30011)
            name = element.property(30005) or ''
            role = app.bridge.ROLES[element.property(30003)]
            if text(identifier) and not identifier.isdecimal():
                selector = dict(automation_id=identifier, role=role)
            elif text(name):
                selector = dict(name=name, role=role)
            else:
                continue
            item = dict(selector, name=name, actions=uia.actions(element), selector=selector)
            item.update(app.field_label(binding, uia, element, root))
            found.append(item)
        app.guard(binding, binding, title)
    return dict(status='observed', targets=found, input_dispatched=False)


RUNNER = '''"""Run this project workflow with its shared Desktop Select runtime."""
from pathlib import Path
import sys

HERE = Path(__file__).resolve()
RUNTIME = HERE.parents[RUNTIME_PARENT] / "desktop-select" / "scripts"
if not (RUNTIME / "python_workflow.py").is_file():
    raise SystemExit(f"Desktop Select runtime missing: {RUNTIME}")
sys.path.insert(0, str(RUNTIME))
import python_workflow
if sys.argv[1:2] == ["scene"]:
    raise SystemExit(python_workflow.main(sys.argv[1:]))
raise SystemExit(python_workflow.main(["run", "--recipe", str(HERE.parents[1] / "workflow.json"), *sys.argv[1:]]))
'''


def _registered_runner(destination):
    skill_root = destination.parent
    # Installed Claude skills are siblings. In the Windows ZIP layout the
    # engine remains in <project>/desktop-select, outside .claude/skills.
    relative_parent = 2
    if skill_root.name == 'skills' and skill_root.parent.name == '.claude':
        portable = skill_root.parent.parent / 'desktop-select/scripts'
        sibling = skill_root / 'desktop-select/scripts/python_workflow.py'
        if portable == Path(__file__).resolve().parent or (not sibling.is_file() and (portable / 'python_workflow.py').is_file()):
            relative_parent = 4
    return RUNNER.replace('RUNTIME_PARENT', str(relative_parent))


REGISTERED_HEADING = '# 등록된 업무 실행\n\n'
RUN_COMMAND = 'python -X utf8 -B "${CLAUDE_SKILL_DIR}/scripts/run.py"'
GUIDE_REFERENCE = 'workflow-guide.md'
GUIDE_BEGIN = '<!-- desktop-select:run -->\n'
GUIDE_END = '<!-- /desktop-select:run -->\n\n'


def run_instructions(recipe, skill_name):
    parameters = list(dict.fromkeys(s['parameter'] for s in recipe['steps'] if 'parameter' in s))
    # Only put plain names in executable examples. The literal names are listed
    # separately for recipes with quotes, shell expansion characters, etc.
    arguments = ''.join(' --set "' + (p if re.fullmatch(r'[\w .-]+', p) else '<변수명>') + '=<값>"'
                        for p in parameters)
    profile = input_policy.profile(recipe['steps'])
    physical = profile['physical_input_required']
    scopes = []
    scene_windows = []
    if physical:
        for step, mode in zip(recipe['steps'], profile['steps']):
            if mode['input_mode'] not in ('physical_pointer', 'physical_keyboard'):
                continue
            scope = step.get('window_context') or step.get('popup') or {}
            menu = workflow_repair.menu(step)
            action = '우클릭 → ' + ' → '.join(menu['path']) if menu else step.get('label') or step['action']
            if step['action'] == 'press-key':
                action = step['keys']
            title = scope.get('title', recipe['window'])
            line = json.dumps([title, action], ensure_ascii=False)
            if line not in scopes:
                scopes.append(line)
            if step['action'] == 'conditional-open' and title not in scene_windows:
                scene_windows.append(title)
    note = ''
    if physical:
        note = '실제 입력 범위는 ' + ', '.join(scopes) + '이다. '
        if scene_windows:
            note += ('조건을 판단할 때 ' + json.dumps(scene_windows, ensure_ascii=False) + ' 창의 현재 이미지를 읽는다. '
                     '이번 실행의 실제 입력과 해당 창 이미지 확인이 모두 승인됐으면 바로 실행한다. '
                     '빠진 승인은 실행 전에 한 번에 묻는다. `needs_scene`이면 상세 안내에 따라 같은 실행을 이어간다.\n\n')
            arguments += ' --allow-physical'
        else:
            note += ('이번 요청에서 이 범위의 마우스 또는 키보드 사용을 이미 허용했다면 첫 호출은 다음 명령이다.\n\n'
                     '`' + RUN_COMMAND + ' --summary --allow-physical' + arguments + '`\n\n'
                     '같은 업무의 이후 실행까지 명시적으로 허용했을 때만 `--allow-physical --remember-physical`을 사용한다. '
                     '반복 승인은 `--forget-physical`로 취소한다.\n\n'
                     '이번 요청에 실제 입력 허용이 없으면 아래 명령을 쓴다. 저장된 승인이 있으면 바로 실행되며, '
                     '`needs_consent`가 오면 표시된 범위를 한 번 묻는다.\n\n')
    repair = ''
    if any(workflow_repair.menu(step) for step in recipe['steps']):
        repair = '이번 실행에서 표 위치 보정용 화면 확인까지 승인받았으면 `--auto-repair-frame`과 `--allow-physical`을 함께 준다. '
        repair += ('표 보정 승인이 없으면 이 옵션을 생략한다.\n\n' if scene_windows else
                   '저장된 반복 승인은 보정 이미지를 볼 권한을 포함하지 않는다.\n\n')
    if any(step['action'] == 'double-click-result' for step in recipe['steps']):
        import result_trial
        if any(result_trial.supported(recipe, n) for n in range(1, len(recipe['steps']) + 1)):
            if any(s['action'] == 'verify-value' and s.get('result_column') for s in recipe['steps']):
                repair += '값 비교 단계는 기록된 열의 현재 값과 이번 요청값을 비교한다. '
            if any(s['action'] == 'double-click-result' and s.get('result_row') for s in recipe['steps']):
                repair += '현재 행 열기는 저장된 single/first 정책을 쓰며 값 비교를 추가하지 않는다. '
            repair += ('표 확인을 허용받으면 `--local-result-review --allow-physical`을 붙인다. '
                       '`result_review.resume_args`에는 이번 입력값이 들어 있다. 같은 실행기에 인자 그대로 전달한다. '
                       '앞선 조회는 반복하지 않는다.\n\n')
    return (GUIDE_BEGIN
            + f'Claude Code에서 사용자가 `/{skill_name}`으로 직접 호출했고 Company Agent가 이번 턴의 기본 Skill 도구 호출을 '
            f'요구하며 이 업무의 Skill 호출이 아직 성공하지 않은 경우에만 `Skill(skill="{skill_name}")`을 한 번 먼저 호출한다.\n\n'
            '실행 요청이면 다음 명령을 첫 셸 명령으로 한 번 실행한다. '
            + ('필수 변수 ' + json.dumps(parameters, ensure_ascii=False) + '에 요청값을 넣고 빠진 값만 묻는다. '
               if parameters else '')
            + ('빠진 변수와 실제 입력 승인이 함께 필요하면 한 번에 묻는다. ' if parameters and physical else '')
            + '실행기가 대상 창을 확인한다.\n\n' + note
            + '`' + RUN_COMMAND + ' --summary' + arguments + '`\n\n' + repair +
            '현재 상태만 요청하면 실행 인자 대신 `--read-only`, 입력 방식만 요청하면 `--input-plan`을 쓴다. '
            '백그라운드 전용 요청은 실행 변수와 `--background-only`로 호출한다.\n\n'
            'JSON의 상태를 보고하고 workbook_verified이면 verified_outputs의 파일 경로를 전달한다. '
            '`host_action=report_execution_receipt`이면 영수증에서 확인한 값과 결과를 보고한다. '
            '이미 확인된 값, 창 닫힘과 파일을 화면으로 다시 검사하지 않는다. '
            '이미지 확인이 필요한 중단 응답에서만 반환된 이미지를 읽고, 같은 창의 같은 판단에서 동일한 frame 또는 image_sha256은 다시 읽지 않는다. '
            '로컬 확인창과 Python 내부 픽셀 검사는 모델에 이미지를 보내라는 지시가 아니다. '
            'input_pause이면 사용자에게 입력을 잠시 멈춰 달라고 안내한다. 사용자가 멈췄다고 하면 제공된 resume_args로 이어간다. '
            'retry_full_run_after_idle은 전체를 다시 시작할 수 있다. 그 밖에는 이미 보낸 입력의 결과부터 확인한다. '
            '녹화 순서 설명이나 중단 후 복구가 필요할 때만 '
            f'[업무 상세 안내]({GUIDE_REFERENCE})를 읽는다.\n' + GUIDE_END)


def refresh_guide(destination):
    """Add the quick-run instructions to an existing generated skill without re-registering it."""
    destination = Path(destination).resolve()
    path = destination / 'SKILL.md'
    runner = destination / 'scripts/run.py'
    need(not path.is_symlink() and path.is_file() and path.stat().st_size <= app.tasks.MAX_FILE_BYTES
         and runner.is_file() and runner.stat().st_size <= 16384, 'registered_skill_required')
    need(runner.read_text(encoding='utf8') in (RUNNER.replace('RUNTIME_PARENT', '2'),
                                                RUNNER.replace('RUNTIME_PARENT', '4')), 'registered_skill_required')
    recipe = load(destination / 'workflow.json')
    original = path.read_bytes()
    eol = b'\r\n' if b'\r\n' in original else b'\n'
    heading = REGISTERED_HEADING.encode().replace(b'\n', eol)
    prefix = ('---\nname: ' + destination.name + '\n').encode().replace(b'\n', eol)
    need(original.startswith(prefix) and original.count(heading) == 1, 'generated_guide_required')
    split = original.index(heading) + len(heading)
    following = original[split:]
    begin, end = (tag.encode().replace(b'\n', eol) for tag in (GUIDE_BEGIN, GUIDE_END))
    reference = destination / GUIDE_REFERENCE
    has_reference = False
    if begin in following or end in following:
        need(following.startswith(begin) and following.count(begin) == following.count(end) == 1,
             'generated_guide_section_changed')
        instructions, following = following.split(end, 1)
        has_reference = f']({GUIDE_REFERENCE})'.encode() in instructions
    need(not reference.is_symlink() and (not reference.exists() or reference.is_file()), 'guide_reference_conflict')
    if not (has_reference and reference.is_file()):
        source_task = None
        source_file = destination / 'source-task.json'
        if source_file.exists():
            need(source_file.is_file() and not source_file.is_symlink()
                 and source_file.stat().st_size <= app.tasks.MAX_FILE_BYTES, 'invalid_task')
            source_task = json.loads(source_file.read_text(encoding='utf8'))
            app.tasks._validate(source_task)
        # Registration-time popup availability is not stored in workflow.json.
        # Move only an exactly recognized old notes block, preserving custom text.
        for pending_popup in (True, False):
            notes = workflow_notes(recipe, source_task, pending_popup)
            legacy = notes.encode().replace(b'\n', eol)
            if following.startswith(legacy):
                following = following[len(legacy):]
                break
        contents = guide_reference(recipe, notes).encode().replace(b'\n', eol)
        try:
            with reference.open('xb') as stream:
                stream.write(contents)
        except FileExistsError:
            need(reference.stat().st_size == len(contents) and reference.read_bytes() == contents,
                 'guide_reference_conflict')
    updated = original[:split] + run_instructions(recipe, destination.name).encode().replace(b'\n', eol) + following
    if updated == original:
        return dict(status='guide_current', input_dispatched=False)
    with tempfile.NamedTemporaryFile(dir=destination, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        temporary.write_bytes(updated)
        need(path.read_bytes() == original, 'guide_changed_during_refresh')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return dict(status='guide_updated', path=str(path), input_dispatched=False)


def _literal_recorded_targets(recipe):
    """A compiled recording needs no discovery for identities it already carries."""
    def concrete(selector):
        return (isinstance(selector, dict) and 'query' not in selector
                and any(key in selector for key in ('automation_id', 'name', 'anchor'))
                and all(concrete(parent) for parent in selector.get('ancestors', ())))
    for step in recipe['steps']:
        if 'grounding_text' in step:
            return False
        selector = step.get('selector')
        if selector is not None:
            if not concrete(selector):
                return False
            # Preflight supplies a caption guard when an ID-only recording has none.
            if step['action'] == 'invoke' and not (step.get('command_name') or selector.get('name')):
                return False
        elif step['action'] not in ('wait', 'verify-file', 'visual-click', 'row-action', 'conditional-open',
                                    'result-menu', 'context-menu', 'double-click-result'):
            # Keep the existing separate evidence checks for named row launches.
            if not (step['action'] == 'row-open' and 'row_target' in step
                    or step['action'] == 'row-launch' and 'row_hint' in step):
                return False
    return True


def _recorded_targets(recipe, source_task, *, first_trial=False):
    has_conditional = any(s['action']=='conditional-open' for s in recipe['steps'])
    if source_task is None:
        return False
    deferred = first_trial and any(s['action'] in input_policy.VALUE_OBSERVATIONS for s in recipe['steps'])
    literal = _literal_recorded_targets(recipe)
    if not deferred and not literal and not all(
            step['action'] in ('visual-click', 'row-action', 'conditional-open') or
            (step['action']=='row-open' and 'row_target' in step)
            or (step['action'] == 'row-launch' and ('row_hint' in step or has_conditional))
            for step in recipe['steps']):
        return False
    import recorded_plan
    compiled = (recorded_plan.compile_saved(source_task, app.task_directory()) if
                deferred or literal or has_conditional or any(s['action']=='row-open' for s in recipe['steps'])
                else recorded_plan.compile_plan(source_task))
    need(compiled.get('envelope', {}).get('recipe') == recipe, 'recorded_workflow_mismatch')
    return True


def register(recipe, destination, hwnd=None, *, source_task=None, first_trial=False):
    """Register demonstrated targets offline, or resolve other selectors read-only."""
    recipe = deepcopy(validate(recipe))
    if source_task is not None:
        app.tasks._validate(source_task)
    destination = Path(destination).resolve()
    need(not destination.exists(), 'workflow_destination_exists')
    need(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', destination.name), 'workflow_folder_name_required')
    recorded = _recorded_targets(recipe, source_task, first_trial=first_trial)
    observed = dict(status='observed', steps=[]) if recorded else run(recipe, read_only=True, hwnd=hwnd)
    if observed['status'] != 'observed':
        return observed
    active = [s for s in recipe['steps'] if s['action'] != 'wait']
    rows = {row['number']: row for row in observed['steps']}
    for number, step in enumerate(recipe['steps'], 1):
        if recorded:
            # The exact compiled source already supplies the selector. One
            # fresh lookup in the real run will validate its current location.
            if step['action'] == 'set-value' and step['selector']['role'] == 'combobox':
                step['allow_option_select'] = True
            if step['action'] == 'invoke' and step['selector'].get('name'):
                step.setdefault('command_name', step['selector']['name'])
            continue
        if step['action'] in ('wait', 'context-menu', 'result-menu', 'verify-file', 'row-action', 'row-launch','row-open',
                              'visual-click', 'conditional-open', 'double-click-result') or 'result_column' in step:
            continue
        row = rows[number]
        if row['receipt']['status'] == 'deferred':
            need('popup' in step or 'window_context' in step or step['action'] == 'verify-value'
                 and row['receipt'].get('diagnostic') == 'validate_result_on_execution', 'invalid_deferred_scope')
            continue
        capabilities = {'set-value': {'SetValue'}, 'invoke': {'Click'}, 'submit': {'SetFocus'}, 'press-key': {'SetFocus'},
                        'choose-item': {'Select'}}.get(step['action'])
        if (step['action'] == 'set-value' and step['selector']['role'] == 'combobox'
                and (source_task is not None or step.get('allow_option_select'))):
            # A list-only combo exposes Expand on the field and Select on a
            # live option once opened. The player validates the requested item.
            capabilities.add('Expand')
            if 'SetValue' not in row['receipt']['actions'] and 'Expand' in row['receipt']['actions']:
                step['allow_option_select'] = True
        if capabilities is not None:
            need(not capabilities.isdisjoint(row['receipt']['actions']), 'workflow_pattern_unavailable')
        if row['receipt'].get('resolved_selector'):
            step['selector'] = row['receipt']['resolved_selector']
            step.pop('grounding_text', None)
        resolution = row['receipt'].get('target_resolution', {})
        if step['action'] == 'set-value' and resolution.get('semantic_label'):
            step['field_label'] = resolution['semantic_label']
        if step['action'] == 'invoke' and text(row['receipt'].get('command_name')):
            step['command_name'] = row['receipt']['command_name']
    required = list(dict.fromkeys(s['parameter'] for s in active if 'parameter' in s))
    destination.mkdir(parents=True, exist_ok=False)
    (destination / 'scripts').mkdir()
    (destination / 'workflow.json').write_text(json.dumps(recipe, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    (destination / 'scripts/run.py').write_text(_registered_runner(destination), encoding='utf8')
    if source_task is not None:
        (destination / 'source-task.json').write_text(json.dumps(source_task, ensure_ascii=False, indent=2) + '\n',
                                                     encoding='utf8')
    description = json.dumps(recipe['name'] + ' 녹화 순서를 Windows에서 실행한다.', ensure_ascii=False)
    guide = ('---\nname: ' + destination.name + '\ndescription: ' + description + '\n---\n\n'
             + REGISTERED_HEADING + run_instructions(recipe, destination.name))
    pending_popup = any('popup' in item for item in observed.get('pending_scopes', []))
    notes = workflow_notes(recipe, source_task, pending_popup)
    (destination / GUIDE_REFERENCE).write_text(guide_reference(recipe, notes), encoding='utf8')
    (destination / 'SKILL.md').write_text(guide, encoding='utf8')
    result = dict(status='registered', path=str(destination), required_parameters=required, input_dispatched=False,
                  input_requirements=input_policy.profile(active),
                  run_args=[str(destination / 'scripts/run.py'), '--summary'])
    menu_checks = [dict(number=r['number'], **{k: v for k, v in r['receipt'].items()
                       if k in ('region_observed', 'point_observed', 'proposed_point', 'point_diagnostic',
                                'menu_verification')}) for r in observed['steps']
                   if workflow_repair.menu(recipe['steps'][r['number'] - 1])]
    if menu_checks:
        result['menu_checks'] = menu_checks
    if recorded:
        result['target_validation'] = 'recorded_evidence'
        result['pending_scopes'] = [dict(number=i, window_context=deepcopy(s['window_context']),
                                         diagnostic='validate_on_execution')
                                    for i, s in enumerate(recipe['steps'], 1) if 'window_context' in s]
    elif observed.get('pending_scopes'):
        result.update(target_validation='pending_scopes', pending_scopes=observed['pending_scopes'])
    return result


def guide_reference(recipe, notes):
    recovery = ('중단 시 `stopped_at`, `diagnostic`, `input_dispatched`를 확인한다. '
                '확인된 출력은 결과 JSON의 경로를 쓴다.\n\n')
    if any(workflow_repair.menu(step) for step in recipe['steps']):
        recovery += ('`menu_repair.image`가 있으면 그 이미지에서 녹화한 셀을 확인한다. 이미지 없이 티켓만 오면 '
                     '해당 표의 보정용 화면 확인 승인을 확인하고, 없으면 한 번 물은 뒤 `frame_args`로 확인한다. '
                     '`resume_script` 또는 이 스킬의 `scripts/run.py`에 `resume_args`와 '
                     '원본 이미지의 픽셀 좌표를 주어 같은 실행을 이어간다.\n\n')
    return '# 등록 업무 상세 안내\n\n' + recovery + notes


def workflow_notes(recipe, source_task, pending_popup):
    # Keep these generated paragraphs recognizable when upgrading older
    # guides. User-authored text outside them stays in the active SKILL.md.
    command = RUN_COMMAND
    active = [s for s in recipe['steps'] if s['action'] != 'wait']
    required = list(dict.fromkeys(s['parameter'] for s in active if 'parameter' in s))
    guide = ('업무명, 대상 창과 순서는 `workflow.json`에 있다. '
             + ('필수 입력은 ' + json.dumps(required, ensure_ascii=False) + '이다. 각 값을 받아 `--set "이름=값"`으로 전달한다.'
                if required else '이 작업은 실행 시 입력할 변수가 없다.') + '\n\n'
             '사용자가 현재 상태만 확인해 달라고 하면 `--read-only`를 붙인다. 이 명령은 대상 창을 복원하지 않는다.\n\n'
             '`--input-plan`은 앱을 조회하지 않고 전체 단계의 입력 방식을 표시한다. '
             '`--background-only`는 도구의 실제 마우스/키보드 입력과 전면화 요청을 금지한다. '
             '그런 단계가 있으면 첫 입력 전에 전체 실행을 거절한다. 최소화된 창도 복원하지 않는다. '
             '허용된 UIA 실행에서는 자동 포커스를 끄고 설정값을 확인한다. '
             '앱 자체가 대화상자를 열거나 포커스를 바꾸는 동작은 별개이며 독립 데스크톱은 아니다.\n\n'
             '콤보 선택값과 입력란 문자열을 필수 변수로 받을 수 있다. '
             '예를 들어 `--set "Family=Hybrid" --set "검색어=00123"`처럼 이번 값을 전달한다. '
             '입력란을 비우라는 요청은 해당 변수에 `--set "검색어="`를 준다. 콤보에는 빈 값을 쓰지 않는다. '
             '값 확인 실패 또는 불확실한 전달이면 다음 단계로 진행하지 않는다. '
             '조회/내보내기 버튼 전달과 대기는 업무 완료나 파일 생성의 증거가 아니다. 재클릭하지 않는다. '
             '프로젝트의 Desktop Select 엔진을 사용한다.\n')
    if source_task is not None:
        guide += '\n녹화한 위치와 자연어 지시 원문은 `source-task.json`에 보관되어 있다. 실행에는 `workflow.json`을 사용한다.\n'
        guide += '\n## 녹화한 순서\n\n'
        interpreted = {i: step for i,step in enumerate(recipe['steps'],1) if step['action']=='conditional-open'}
        for index, step in enumerate(source_task['steps'], 1):
            caption = (step.get('description') or step.get('instruction', {}).get('text') or step.get('instruction_draft')
                       or step.get('alias') or step.get('label') or step.get('target') or step['action'])
            if index in interpreted and len(recipe['steps'])==len(source_task['steps']):
                conditional = interpreted[index]
                caption = (f"{conditional['seconds']}초 기다린 뒤 {conditional['when_present']['name']}이 보이면 "
                           f"{conditional['dismiss']['name']}을 눌러 닫고, 표시 여부와 관계없이 "
                           f"{conditional['always']['name']}을 연다.")
            guide += f'{index}. {caption}\n'
    if any(step['action'] in ('row-action', 'row-launch') for step in active):
        guide += ('\n녹화한 행 버튼은 저장된 행 표시와 버튼을 다시 찾아 실행한다. '
                  '“두 번째 행 실행”처럼 순번으로 등록한 동작은 현재 목록의 순서를 따른다. '
                  '이미 확인한 실행 상태라면 클릭을 생략한다. 행 버튼을 누르는 동안 실제 마우스를 사용한다. '
                  f'한 단계의 현재 상태는 `{command} --only 1 --read-only`로 확인한다.\n')
    if any('window_context' in step for step in active):
        guide += ('\n다른 창에서 수행하는 단계는 등록한 실행 파일과 창 제목으로 대상을 찾는다. '
                  '기존 창으로 연결한 경우 작업 시작 때 그 창을 열어 둔다. 새 창으로 연결한 경우 '
                  '앞 단계가 실행된 뒤 해당 창이 열리면 이어서 처리한다. 녹화한 행이 이미 실행 중이면 '
                  '시작 버튼을 생략하고 같은 행에 연결된 기존 창을 확인한다. 같은 제목의 창이 둘이면 '
                  '단계 번호와 이유를 확인한다. --read-only에서 아직 열리지 않은 창은 deferred로 표시된다.\n')
    if any(step['action'] == 'conditional-open' for step in active):
        guide += ('\n조건 단계가 `needs_scene`이면 Claude Code가 영수증의 `image`에서 지정된 창을 읽는다. '
                  '광고처럼 조건의 대상이 보이면 그 안의 닫기 버튼을, 없고 최종 아이콘이 보이면 그 아이콘을 찾는다. '
                  f'같은 실행을 `{command} scene --ticket <ticket> --decision present|absent --point X Y`로 이어간다. '
                  '원본 PNG 기준 좌표를 사용한다. 판단할 수 없으면 `--decision unknown`만 보낸다. '
                  '창 제목 표시줄의 X를 누르지 않는다. 닫기 뒤의 새 화면에서 최종 아이콘을 확인한다. '
                  '새 영수증이 나오면 같은 절차를 계속하며, 실행 명령을 처음부터 다시 시작하지 않는다.\n')
    if any(step['action'] == 'visual-click' for step in active):
        guide += ('\n화면으로 녹화한 클릭은 지정한 창에서 녹화된 모양을 다시 찾고 실제 마우스로 한 번 누른다. '
                  'delivered_unverified는 클릭 전달을 뜻한다. 후속 단계의 값 읽기나 창 확인 영수증이 있으면 그 근거를 사용한다. '
                  '요청한 결과의 근거가 없을 때만 해당 요소를 읽고, 요소로 확인할 수 없을 때 해당 창 이미지를 사용한다.\n')
    if any('grounding_text' in step or step['action']=='row-open' for step in active):
        guide += ('\n설명 속 이름이 현재 창에서 보이지 않으면 grounding_steps의 단계와 원문으로 '
                  'desktop-select의 ground에서 같은 창을 확인한다. 여러 행에 같은 실행 버튼이 있으면 '
                  '원본 녹화의 해당 단계에 bind-row --row-point X Y --point X Y로 행 이름과 그 행의 실행 버튼을 연결한다. '
                  '고유한 단일 대상은 bind-visual로 연결한다. 연결한 뒤 등록한 스킬을 갱신한다.\n')
    if pending_popup:
        guide += ('\n등록 시 아직 열리지 않은 팝업의 대상은 미확인 상태로 보존했다. '
                  '실행할 때 지정된 제목의 소속 팝업이 하나로 확인된 뒤에만 해당 단계에 입력한다. '
                  '팝업이 없거나 모호하면 멈추며 원래 창에 대신 입력하지 않는다.\n')
    if any(step['action'] == 'verify-file' for step in active):
        guide += ('\n파일 확인은 모든 입력 전 기준 상태와 비교하여 새로 생기거나 변경된 로컬 통합 문서를 검사한다. '
                  '파일 확인 실패 후 내보내기를 다시 클릭하지 않는다.\n')
    if any(step['action'] == 'submit' for step in active):
        guide += ('\nsubmit 단계는 지정한 입력란과 실제 키보드 포커스를 확인한 뒤 Enter를 한 번 보낸다. '
                  '실제 창을 앞으로 가져오며 실행 중 마우스와 키보드를 사용하지 않는다. '
                  '키 전달은 조회 완료의 증거가 아니며 불확실하면 반복하지 않는다.\n')
    if any(step['action'] == 'press-key' for step in active):
        guide += ('\n키 입력 단계는 대상 창과 컨트롤의 포커스를 확인한 뒤 녹화된 키 조합을 한 번 보낸다. '
                  '실제 키보드를 사용하는 동안 다른 입력은 잠시 멈춘다.\n')
    if any(step['action'] in ('context-menu', 'result-menu') for step in active):
        guide += ('\n실제 마우스로 표를 우클릭하는 단계는 대상 창을 앞으로 가져온 뒤 녹화한 영역을 확인하고 지정된 메뉴 경로를 순서대로 선택한다. '
                  '해당 단계가 끝날 때까지 마우스와 키보드를 사용하지 않는다.\n')
    if any('anchor' in step.get('selector', {}) for step in active):
        guide += ('\n상위 영역과 상대 위치로 찾는 입력란은 대상 창의 접근성 구조 안에서 확인한다. '
                  '다른 창에 가려져 있어도 유일한 입력란을 찾으면 전면화 없이 실행한다. '
                  '같은 종류 입력란의 배치가 바뀌면 역할과 클래스 검사만으로 구별할 수 없으므로 지정 위치를 다시 확인한다. '
                  '영역 안에서 대상이 겹치거나 구조를 읽을 수 없으면 입력 전에 멈춘다.\n')
    return guide


def summarize_execution(report, entries):
    """Summarize a finished run; preserve the full recovery protocol on interruption."""
    rows = report.get('steps', [])
    if (report['status'] not in ('completed_unverified', 'verified') or 'stopped_at' in report
            or not entries or [row['number'] for row in rows] != [n for n, _ in entries]):
        return report
    summary = {k: v for k, v in report.items() if k not in ('steps', 'input_requirements')}
    summary['steps'] = []
    outputs = []
    for row, (_, step) in zip(rows, entries):
        receipt = row['receipt']
        if receipt.get('status') not in ('verified', 'waited', 'delivered_unverified', 'observed'):
            return report
        item = {k: row[k] for k in ('number', 'label', 'elapsed_seconds') if k in row}
        item.update(action=step['action'])
        item.update({k: receipt[k] for k in ('status', 'diagnostic', 'input_dispatched', 'menu_items', 'menu_recovery',
                                             'verified_path', 'verification_scope', 'keys') if k in receipt})
        if step['action'] in input_policy.VALUE_OBSERVATIONS:
            item.update({k: receipt[k] for k in ('value', 'value_evidence', 'result_observation') if k in receipt})
        if step['action'] == 'double-click-result' and 'result_row' in step:
            item.update({k: receipt[k] for k in ('result_row', 'result_observation', 'query_freshness',
                                               'value_comparison') if k in receipt})
        summary['steps'].append(item)
        if (receipt['status'] == 'verified' and receipt.get('diagnostic') == 'export_workbook_verified'
                and (receipt.get('file_verified') is True or receipt.get('workbook_verified') is True)
                and isinstance(receipt.get('verified_path'), str) and receipt['verified_path']):
            outputs.append(dict(number=row['number'], path=receipt['verified_path']))
    if outputs:
        summary['verified_outputs'] = outputs
        if outputs[-1]['number'] == entries[-1][0] and entries[-1][1]['action'] in ('verify-file', 'result-menu'):
            summary['outcome'] = 'workbook_verified'
    summary.setdefault('host_action', 'report_execution_receipt')
    return summary


def _cli_receipt(argv):
    """Compose the public command paths without emitting intermediate JSON."""
    with redirect_stdout(StringIO()) as output:
        code = main(argv)
    receipt = json.loads(output.getvalue())
    need(code in (0, 2, 3) and isinstance(receipt, dict) and 'status' in receipt,
         'invalid_worker_receipt')
    return receipt


def register_first_run(recipe, args, source_task=None):
    """Register once, then enter the normal supervised runner at most once."""
    destination = args.out.resolve()
    if destination.exists():
        return dict(status='blocked', diagnostic='workflow_destination_exists', input_dispatched=False,
                    path=str(destination), host_action='check_existing_registration_without_rerunning')
    requirements = input_policy.profile(recipe['steps'])
    if args.remember_physical:
        need(args.allow_physical and requirements['physical_input_required'] and not args.background_only
             and args.hwnd is None and not any(s['action'] == 'conditional-open' for s in recipe['steps']),
             'repeat_approval_requires_full_physical_test')
    if args.background_only:
        refusal = input_policy.blocked(requirements)
        if refusal:
            return refusal
    _, missing = values_for(recipe, app.tasks.parse_overrides(args.set))
    if requirements['physical_input_required'] and not args.allow_physical:
        return dict(status='needs_consent', diagnostic='registered_physical_consent_required',
                    window=recipe['window'], input_requirements=requirements, steps=[], missing_parameters=missing,
                    approval_flag='--allow-physical', input_dispatched=False)
    if missing:
        return dict(status='needs_input', missing_parameters=missing, steps=[], input_dispatched=False)
    if source_task and (_literal_recorded_targets(recipe)
                        or any(s['action'] in input_policy.VALUE_OBSERVATIONS for s in recipe['steps'])):
        registration = register(recipe, destination, args.hwnd, source_task=source_task, first_trial=True)
    else:
        register_args = ['register', '--out', str(destination)]
        register_args += ['--task', args.task] if args.task else ['--recipe', str(args.recipe.resolve())]
        if args.hwnd is not None:
            register_args += ['--hwnd', str(args.hwnd)]
        registration = _cli_receipt(register_args)
    if registration['status'] != 'registered':
        return registration
    run_args = ['run', '--recipe', str(destination / 'workflow.json')]
    if args.hwnd is not None:
        run_args += ['--hwnd', str(args.hwnd)]
    for value in args.set:
        run_args += ['--set', value]
    for option in ('summary', 'allow_physical', 'remember_physical', 'background_only', 'auto_repair_frame'):
        if getattr(args, option):
            run_args.append('--' + option.replace('_', '-'))
    try:
        result = _cli_receipt(run_args)
    except (Exception, KeyboardInterrupt) as error:
        # A receipt can be lost after a successful input. Never retry the run.
        result = dict(status='uncertain', input_dispatched=None,
                      diagnostic=getattr(error, 'code', 'workflow_unavailable'))
    return dict(result, registration=registration)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == '_scene_worker':
        from scene_session import worker
        worker(sys.modules[__name__],argv[1])
        return 0
    parser = argparse.ArgumentParser(description='Discover once, register literal actions, run with Python.')
    sub = parser.add_subparsers(dest='command', required=True)
    scene = sub.add_parser('scene',help='continue the exact live run after inspecting the returned window image')
    scene.add_argument('--ticket',required=True)
    scene.add_argument('--decision',choices=['present','absent','unknown'],required=True)
    scene.add_argument('--point',type=int,nargs=2)
    discovery = sub.add_parser('discover')
    discovery.add_argument('--exe', required=True)
    discovery.add_argument('--window', required=True)
    discovery.add_argument('--hwnd', type=int)
    outputs = sub.add_parser('discover-output', help='check local output directory candidates for one running app')
    outputs.add_argument('--exe', required=True)
    outputs.add_argument('--window', required=True)
    outputs.add_argument('--hwnd', type=int)
    refresh = sub.add_parser('refresh-guide', help='update repeat-run instructions in an existing generated skill')
    refresh.add_argument('--skill', type=Path, required=True)
    for command in ('register', 'run'):
        p = sub.add_parser(command)
        if command == 'register':
            source = p.add_mutually_exclusive_group(required=True)
            source.add_argument('--recipe', type=Path)
            source.add_argument('--task', help='saved natural-language recording to register')
        else:
            p.add_argument('--recipe', type=Path, required=True)
        p.add_argument('--hwnd', type=int)
        if command == 'register':
            p.add_argument('--out', type=Path, required=True)
            p.add_argument('--run', action='store_true', help='register once and perform the requested first run')
        else:
            p.add_argument('--only', help='run or inspect one workflow step by number or label')
            p.add_argument('--read-only', action='store_true')
            p.add_argument('--input-plan', action='store_true', help='preview input modes without opening or inspecting an app')
            repair = p.add_mutually_exclusive_group()
            repair.add_argument('--repair-frame', metavar='TICKET', help='inspect the failed result area without input')
            repair.add_argument('--resume-menu', metavar='TICKET', help='resume a before-click failure from the approved frame')
            repair.add_argument('--resume-input', metavar='TICKET', help='after the user pauses, continue at the unsent menu')
            repair.add_argument('--confirm-result-locally', metavar='TICKET', help='review the current result cell on this PC and continue from it')
            p.add_argument('--local-result-review', action='store_true',
                           help='if needed, open a local confirmation window for an unreadable result cell during this run')
            p.add_argument('--forget-physical', action='store_true', help='remove the repeat permission for this registration')
            p.add_argument('--frame', metavar='TOKEN')
            p.add_argument('--point', nargs=2, type=int, metavar=('X', 'Y'))
        p.add_argument('--set', action='append', default=[])
        p.add_argument('--summary', action='store_true', help='return a compact receipt; exit 0 for a verified final workbook')
        p.add_argument('--allow-physical', action='store_true', help='the user approved the registered screen/input scope for this run')
        p.add_argument('--remember-physical', action='store_true', help='after an approved full test, allow future requested runs of the identical workflow')
        p.add_argument('--background-only', action='store_true', help='refuse physical input, foregrounding and window restoration')
        p.add_argument('--auto-repair-frame', action='store_true',
                       help='include the approved result-area image after a before-input menu point failure')
    parser.add_argument('--_worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.command == 'scene':
        from scene_session import answer
        try:
            result = answer(args.ticket,args.decision,args.point)
        except (ValueError,OSError) as error:
            result = dict(status='blocked',diagnostic=getattr(error,'code','scene_answer_unavailable'),input_dispatched=False)
        print(json.dumps(result,ensure_ascii=False))
        return 0 if result['status']=='verified' else (3 if result['status'] in ('completed_unverified','uncertain') else 2)
    mutating = args.command == 'run' and not args.read_only and not args.input_plan and not args.repair_frame
    launched, executing = False, False
    try:
        source_task = None
        recipe = None
        if args.command == 'register':
            need(args.run or not (args.set or args.summary or args.allow_physical or args.remember_physical or
                                 args.background_only or args.auto_repair_frame), 'register_run_required')
        if args.command == 'refresh-guide':
            result = refresh_guide(args.skill)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command in ('run', 'register'):
            if getattr(args, 'task', None):
                import first_trial
                directory = app.task_directory()
                document = app.tasks.load_task(args.task, directory, allow_review=True)
                planned = first_trial._compiled(document, directory)
                if not planned.get('envelope'):
                    result = dict(status='needs_review', diagnostic=planned.get('diagnostic', 'recorded_task_not_compatible'),
                                  input_dispatched=False, recover_script=str(Path(app.__file__).resolve()),
                                  recover_args=['recover', '--task', args.task, '--prepare'])
                    if planned.get('step') is not None:
                        result['step'] = planned['step']
                    if planned.get('grounding_steps'):
                        result.update(status='needs_grounding', diagnostic='target_grounding_required',
                                      grounding_steps=planned['grounding_steps'])
                    print(json.dumps(result, ensure_ascii=False))
                    return 2
                source_task = planned['envelope']['source']
                recipe = planned['envelope']['recipe']
            else:
                recipe = load(args.recipe)
        if args.command == 'register' and args.run:
            result = register_first_run(recipe, args, source_task)
            print(json.dumps(result, ensure_ascii=False))
            return (0 if result['status'] in ('registered', 'observed', 'verified')
                    or (args.summary and result.get('outcome') == 'workbook_verified') else
                    3 if result['status'] in ('completed_unverified', 'uncertain', 'delivered_unverified') else 2)
        overrides = {}
        if args.command == 'run':
            registered = workflow_repair.registered(args.recipe)
            if args.forget_physical:
                need(registered and not any((args.only, args.hwnd, args.resume_input, args.resume_menu, args.repair_frame,
                     args.confirm_result_locally, args.local_result_review,
                     args.set, args.remember_physical, args.allow_physical, args.background_only, args.frame, args.point,
                     args.input_plan, args.read_only, args.auto_repair_frame)), 'invalid_forget_physical_arguments')
                workflow_repair.forget_approval(args.recipe)
                print(json.dumps(dict(status='forgotten', input_dispatched=False)))
                return 0
            repairing = bool(args.repair_frame or args.resume_menu or args.resume_input or args.confirm_result_locally)
            need(not repairing or (registered and not args.read_only and not args.input_plan and
                                   not args.background_only and args.only is None and args.hwnd is None),
                 'invalid_registered_repair_arguments')
            need((args.frame is not None) == bool(args.resume_menu) and
                 (args.point is not None) == bool(args.resume_menu), 'menu_repair_frame_and_point_required')
            entries = selected_steps(recipe, args.only)
            active = dict(recipe, steps=[step for _, step in entries])
            if args.local_result_review or args.confirm_result_locally:
                import result_trial
                need(registered and not args.background_only and not args.input_plan and not args.read_only
                     and args.only is None and not (args.local_result_review and repairing)
                     and any(result_trial.supported(recipe,n) for n, _ in entries), 'invalid_registered_result_review')
            requirements = input_policy.profile(active['steps'], [n for n, _ in entries])
            eligible = (registered and args.only is None and args.hwnd is None and not args.background_only
                        and not args.read_only and not args.input_plan and requirements['physical_input_required']
                        and not any(s['action'] == 'conditional-open' for s in recipe['steps']))
            need(not args.remember_physical or (eligible and args.allow_physical and not repairing),
                 'repeat_approval_requires_full_physical_test')
            if args.input_plan:
                print(json.dumps(dict(status='observed', input_dispatched=False,
                                      input_requirements=requirements), ensure_ascii=False))
                return 0
            if args.background_only and not args.read_only:
                refusal = input_policy.blocked(requirements)
                if refusal:
                    print(json.dumps(refusal, ensure_ascii=False))
                    return 2
            args.repeat_approval_used = (eligible and not args.allow_physical and not repairing and not args.local_result_review
                                         and not args.auto_repair_frame and workflow_repair.approved(args.recipe, recipe))
            if args.confirm_result_locally and args.set:
                # Older generated guides said to append the original --set to
                # resume_args, which already contains those values. Leave
                # conflicting duplicates for the normal override validator.
                supplied = {}
                for item in args.set:
                    name, separator, value = item.partition('=')
                    supplied.setdefault((name.casefold(), separator, value), item)
                args.set = list(supplied.values())
            overrides = app.tasks.parse_overrides(args.set)
            _, missing = values_for(active, overrides)
            if (registered and not args.allow_physical and not args.repeat_approval_used and not args.resume_input and
                    (repairing or (mutating and requirements['physical_input_required']))):
                print(json.dumps(dict(status='needs_consent', diagnostic='registered_physical_consent_required',
                                      window=recipe['window'], input_requirements=requirements, steps=[],
                                      missing_parameters=[] if repairing else missing,
                                      approval_flag='--allow-physical', input_dispatched=False), ensure_ascii=False))
                return 2
            if missing and not args.read_only and not repairing:
                print(json.dumps(dict(status='needs_input', missing_parameters=missing,
                                      steps=[], input_dispatched=False), ensure_ascii=False))
                return 2
        if args.command == 'register' and source_task is not None and _literal_recorded_targets(recipe):
            # A literal recording can be registered entirely from its validated
            # source; a UIA worker would only reload and compile the same task.
            result = register(recipe, args.out, args.hwnd, source_task=source_task)
        elif args._worker:
            if args.command == 'discover':
                result = discover(args.exe, args.window, args.hwnd)
            elif args.command == 'discover-output':
                owner = bind(args.exe, args.window, args.hwnd)
                result = export_evidence.discover_directories(owner)
            elif args.command == 'register':
                result = register(recipe, args.out, args.hwnd, source_task=source_task)
            elif registered and not args.read_only and (repairing or any(workflow_repair.menu(s) for _, s in entries)
                                                       or (args.only is None and any('result_column' in s or 'result_row' in s for _, s in entries))):
                executing = mutating
                if not args.repair_frame and any(s['action'] == 'conditional-open' for _, s in entries):
                    from scene_session import start
                    session_args = {k: getattr(args, k) for k in ('repair_frame', 'resume_menu', 'resume_input', 'frame',
                                                                  'confirm_result_locally', 'local_result_review',
                                                                  'point', 'only', 'hwnd', 'auto_repair_frame',
                                                                  'allow_physical', 'background_only', 'set')}
                    session_args['recipe'] = str(args.recipe.absolute())
                    result = start(__file__, recipe, dict(overrides=overrides, registered_session=session_args),
                                   worker_timeout(recipe))
                else:
                    result = workflow_repair.run_registered(sys.modules[__name__], recipe, args, overrides)
            else:
                executing = mutating
                with ExitStack() as permission:
                    if args.remember_physical or args.repeat_approval_used:
                        permission.enter_context(coordination.recipe(args.recipe))
                        need(load(args.recipe) == recipe, 'registered_workflow_changed')
                        if args.repeat_approval_used:
                            need(workflow_repair.approved(args.recipe, recipe), 'registered_physical_consent_required')
                    approval_before = workflow_repair.approval_state(args.recipe) if args.remember_physical else None
                    result = run(recipe, overrides, read_only=args.read_only, hwnd=args.hwnd,
                                 background_only=args.background_only, only=args.only)
                    if args.remember_physical:
                        workflow_repair.remember_test(args.recipe, recipe, result, approval_before)
        else:
            timeout = worker_timeout(active if args.command == 'run' else recipe) if recipe else 90
            if getattr(args, 'local_result_review', False) or getattr(args, 'confirm_result_locally', None):
                timeout += 600
            launched = True
            supervise = coordination.run_worker if mutating or getattr(args, 'repair_frame', None) else subprocess.run
            child = supervise([sys.executable, '-X', 'utf8', '-B', str(Path(__file__).resolve()),
                               '--_worker', *argv], capture_output=True, encoding='utf8', timeout=timeout)
            result = json.loads(child.stdout)
            need(child.returncode in (0, 2, 3) and isinstance(result, dict) and 'status' in result,
                 'invalid_worker_receipt')
            if getattr(child, 'queue_seconds', 0):
                result['queue_seconds'] = child.queue_seconds
            if args.command == 'run' and args.summary and mutating:
                if result.get('resumed_at'):
                    executed = {r['number'] for r in result.get('steps', [])}
                    entries = [(n, s) for n, s in entries if n in executed]
                result = summarize_execution(result, entries)
        if (mutating and result.get('diagnostic') in workflow_repair.INPUT_CHANGED
                and not result.get('input_pause')):
            untouched = result.get('input_dispatched') is False and not (args.resume_input or args.resume_menu)
            result['input_pause'] = dict(host_action='retry_full_run_after_idle' if untouched
                                         else 'check_current_effect_before_continuing')
    except (Exception, KeyboardInterrupt) as error:
        uncertain = (launched or executing) and mutating
        result = dict(status='uncertain' if uncertain else 'blocked', input_dispatched=None if uncertain else False,
                      diagnostic=getattr(error, 'code', 'workflow_unavailable'))
        if isinstance(error, WorkflowError) and error.required_parameters is not None:
            result['required_parameters'] = error.required_parameters
    print(json.dumps(result, ensure_ascii=False))
    return (0 if result['status'] in ('registered', 'observed', 'verified')
              or (getattr(args, 'summary', False) and result.get('outcome') == 'workbook_verified') else
            3 if result['status'] in ('completed_unverified', 'uncertain', 'delivered_unverified') else 2)


if __name__ == '__main__':
    raise SystemExit(main())
