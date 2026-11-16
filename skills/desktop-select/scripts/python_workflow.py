"""Register a small, literal Python workflow beside the original 0.4 recorder.

One UIA session runs known controls. No model plan, replay cache, shell text or
implicit retry is involved. UIA discovery remains available for first setup.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import subprocess
import sys
import time

import desktop_select as app
import control_lookup as lookup
import context_menu
import export_evidence
import input_policy
import live_action as live
import result_menu
import row_action
import row_intent
import row_vision
import window_context
import conditional_action


CHILD_VISUAL_TIMEOUT = 30


class WorkflowError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


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
    for raw in steps:
        step = raw
        if isinstance(raw, dict) and 'window_context' in raw:
            step = {key: value for key, value in raw.items() if key != 'window_context'}
        need(isinstance(step, dict), 'invalid_workflow_step')
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
                              'grounding_text', 'allow_option_select'}, 'unknown_step_field')
        need(step.get('action') in ('set-value', 'invoke', 'submit'), 'unsupported_workflow_action')
        if 'popup' in step:
            popup = step['popup']
            need(isinstance(popup, dict) and not set(popup) - {'title', 'timeout'}
                 and text(popup.get('title')) and popup['title'] == popup['title'].strip(),
                 'invalid_popup_scope')
            timeout = popup.get('timeout', 3)
            need(type(timeout) in (int, float) and 0 <= timeout <= 10, 'invalid_popup_timeout')
        selector = step.get('selector')
        validate_selector(selector)
        if 'allow_option_select' in step:
            need(step['action'] == 'set-value' and selector['role'] == 'combobox' and
                 step['allow_option_select'] is True, 'invalid_combo_selection')
        if 'grounding_text' in step:
            need(step['action'] == 'invoke' and 'query' in selector and text(step['grounding_text']),
                 'invalid_grounding_instruction')
        need(text(step.get('label', 'step'), 200), 'invalid_step_label')
        need('command_name' not in step or (step['action'] == 'invoke' and text(step['command_name'])),
             'invalid_command_name')
        need('field_label' not in step or (step['action'] == 'set-value' and text(step['field_label'], 200)),
             'invalid_field_label')
        if step['action'] == 'set-value':
            need(selector['role'] in ('combobox', 'textfield'), 'value_field_required')
            need(('value' in step) != ('parameter' in step), 'fixed_value_or_parameter_required')
            if 'value' in step:
                need(app.tasks.valid_value(step['value'], selector['role']),
                     'invalid_text_value' if selector['role'] == 'textfield' else 'invalid_combo_value')
            else:
                need(text(step['parameter'], 100) and step['parameter'] == step['parameter'].strip()
                     and '=' not in step['parameter'], 'invalid_parameter')
        elif step['action'] == 'submit':
            need(selector['role'] in ('combobox', 'textfield')
                 and 'value' not in step and 'parameter' not in step, 'invalid_submit')
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
    return selected


def worker_timeout(recipe):
    return 90 + sum(step.get('seconds', 0) + step.get('popup', {}).get('timeout', 3 if 'popup' in step else 0)
                    + (step.get('timeout', 26) if step['action'] == 'verify-file' else 0)
                    + (3 * len(step['spec']['path']) + 3 if step['action'] == 'context-menu' else 0)
                    + (60 + 3 * len(step['context']['menu']['path']) if step['action'] == 'result-menu' else 0)
                    + (120 if step['action'] in ('row-action', 'row-launch') else 0)
                    + (CHILD_VISUAL_TIMEOUT if step['action'] == 'visual-click'
                       and 'after_step' in step.get('window_context', {}) else 0)
                    for step in recipe['steps']) + window_context.OPEN_TIMEOUT * len({
                        (spec['exe'].casefold(), spec['title'], spec['after_step'])
                        for step in recipe['steps'] if 'after_step' in (spec := step.get('window_context', {}))})


def values_for(recipe, overrides):
    parameters = {s['parameter'].casefold(): s['parameter'] for s in recipe['steps'] if 'parameter' in s}
    values = {k.casefold(): v for k, v in overrides.items()}
    need(len(values) == len(overrides) and all(app.tasks.valid_value(v, 'textfield') for v in values.values()),
         'invalid_override')
    need(not set(values) - parameters.keys(), 'unknown_parameter')
    for step in recipe['steps']:
        key = step.get('parameter', '').casefold()
        if key in values:
            need(app.tasks.valid_value(values[key], step['selector']['role']), 'invalid_override')
    return values, [name for key, name in parameters.items() if key not in values]


def bind(exe, title, hwnd=None):
    app.bridge.set_dpi_awareness()
    row = app.pick_window(app.windows(exe), title, hwnd)
    binding = app.bridge.Binding(row)
    app.guard(binding, binding, title)
    return binding


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
    if number is None or not routes.origins[number].keys() <= routes.bindings.keys():
        return
    receipt = routes.outcomes.get(number)
    if (receipt and (routes.steps[number-1]['action'],receipt.get('diagnostic')) in (
            ('invoke','business_result_not_verified'),('row-open','row_open_dispatched'),
            ('result-menu','menu_delivered_no_output_condition'))
            and receipt['status'] == 'delivered_unverified' and receipt['input_dispatched'] is True):
        receipt.update(status='verified', effect_verification='matched',
                       verification_scope='recorded_window_opened', business_commit='not_verified')
        receipt.pop('diagnostic')
        if report['status'] == 'completed_unverified' and all(
                item['receipt']['status'] in ('verified','waited') for item in report['steps']):
            report['status'] = 'verified'


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


def _missing_groundings(recipe, entries, owner, uia):
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
            element, _, _ = resolve_step(binding, uia, uia.from_handle(binding.hwnd), step['selector'])
            need('Click' in uia.actions(element), 'workflow_pattern_unavailable')
        except (app.SelectionError, app.bridge.BridgeError, WorkflowError) as error:
            if error.code not in _GROUNDING_DIAGNOSTICS:
                raise
            missing.append(_grounding_step(recipe, number, step, error.code))
    return missing


def run(recipe, overrides=None, *, read_only=False, hwnd=None, allow_recorded_selection=False,
        background_only=False, only=None, scene_session=None):
    recipe = validate(recipe)
    entries = selected_steps(recipe, only)
    active = [step for _, step in entries]
    requirements = input_policy.profile(active, [n for n, _ in entries])
    if background_only and not read_only:
        refusal = input_policy.blocked(requirements)
        if refusal:
            return refusal
    values, missing = values_for(dict(recipe, steps=active), overrides or {})
    if missing and not read_only:
        return dict(status='needs_input', missing_parameters=missing, steps=[], input_dispatched=False)
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
        return start(__file__, recipe, dict(overrides=overrides, hwnd=hwnd, only=only,
                                            allow_recorded_selection=allow_recorded_selection),
                     worker_timeout(recipe))
    report = dict(status='observed' if read_only else 'verified', steps=[], input_dispatched=False,
                  business_commit='not_verified', input_requirements=requirements,
                  input_policy='background_only' if background_only else 'auto')
    started = time.monotonic()
    number, dispatching = 0, False
    anchor_proof = None
    stage_started = started
    try:
        owner = bind(recipe['exe'], recipe['window'], hwnd)
        if background_only and not read_only:
            input_policy.window_ready(owner)
        active_scopes = {n for n,_ in entries}
        row_openers = {n for n,step in entries if step['action']=='row-open'}
        active_scopes.update(n for n,s in enumerate(recipe['steps'],1) if s.get('window_context',{}).get('after_step') in row_openers)
        routes = window_context.WindowRoutes(recipe['steps'], owner, recipe['window'], read_only=read_only,
                                              active=sorted(active_scopes))
        # Capture every requested output before window restoration or any input.
        baselines = {i: export_evidence.baseline(step['output']) for i, step in entries
                     if step['action'] == 'verify-file'} if not read_only else {}
        report['window_restored'] = False
        prepared = set()
        pending_opener = None
        with app.bridge.UIA() as uia:
            if background_only and not read_only:
                input_policy.configure_uia(uia)
            missing = _missing_groundings(recipe, entries, owner, uia)
            if missing:
                return _grounding_report(report, missing)
            for number, step in entries:
                stage_started = time.monotonic()
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
                routes.before(number, binding)

                def check_context():
                    if context is not None:
                        routes.check(context, binding)

                def check_target(surface, popup=None, *, mutate=False):
                    check_context()
                    check_surface(binding, surface, title, popup, mutate=mutate)
                # A popup may disable its owner. Only actions on that owner
                # require it to be enabled/restored; missing popups never do.
                if (not read_only and binding.hwnd not in prepared and 'popup' not in step
                        and step['action'] in ('set-value', 'invoke', 'submit', 'context-menu', 'result-menu', 'row-action',
                                               'row-launch', 'row-open', 'visual-click', 'conditional-open')):
                    if background_only:
                        input_policy.window_ready(binding)
                    else:
                        restored = app.visibility.restore_for_action(binding, title)
                        report['window_restored'] = bool(restored) or report['window_restored']
                    prepared.add(binding.hwnd)
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
                                       baseline_scope='before_workflow', business_commit='not_verified')
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
                    dispatching = not read_only
                    try:
                        opener = routes.outcomes.get((context or {}).get('after_step'), {})
                        first_match_timeout = CHILD_VISUAL_TIMEOUT if not read_only and opener.get('input_dispatched') is True else 0
                        receipt = visual_target.execute(binding, step['visual'], click_guard,
                                                        read_only=read_only, background_only=background_only,
                                                        first_match_timeout=first_match_timeout)
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
                    except app.bridge.BridgeError as error:
                        # Both shared row engines report any input they already
                        # sent in their receipt. Guard/lookup failures precede it.
                        dispatching = getattr(error, 'input_dispatched', False) is not False
                        raise
                    dispatching = False
                elif step['action'] == 'context-menu':
                    check_target(binding, mutate=not read_only)
                    if read_only:
                        context_menu._region(binding, uia, step['spec']['target'])
                        receipt = dict(status='observed', input_dispatched=False,
                                       region_observed=True, menu_verification='pending_execution')
                    else:
                        def context_guard():
                            check_target(binding, mutate=True)
                        dispatching = True
                        receipt = context_menu.execute(binding, step['spec'], context_guard)
                        dispatching = False
                elif step['action'] == 'result-menu':
                    spec = step['context']
                    check_target(binding, mutate=not read_only)
                    if read_only:
                        receipt = result_menu.observe(binding, uia, spec)
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
                    check_target(surface, popup, mutate=not read_only)
                    if background_only and not read_only:
                        input_policy.window_ready(surface)
                    root = uia.from_handle(surface.hwnd)
                    selector = step['selector']
                    anchor_proof = None
                    if not read_only and 'anchor' in selector:
                        # Point-based identity needs the actual target surface
                        # exposed. Ordinary UIA actions remain background-capable.
                        from row_action import foreground
                        foreground(surface)
                        report['foreground_requested'] = True
                        check_target(surface, popup, mutate=True)
                        anchor_proof = lookup.bind_anchored(surface, uia, selector, root=root)
                        element, stable, resolution = anchor_proof.element, None, None
                    else:
                        element, stable, resolution = resolve_step(surface, uia, root, selector)
                    if step.get('grounding_text'):
                        need('Click' in uia.actions(element), 'workflow_pattern_unavailable')
                    need('command_name' not in step or element.property(30005) == step['command_name'],
                         'command_name_changed')
                    if step.get('field_label'):
                        app.tasks.check_display({'label': step['field_label']},
                                                app.field_label(surface, uia, element, root))
                    lookup_elapsed = round(time.monotonic() - lookup_started, 4)
                    if read_only:
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
                        else:
                            receipt['command_name'] = element.property(30005) or ''
                    else:
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
                                capability = {'set-value': 'SetValue', 'invoke': 'Click', 'submit': 'SetFocus'}[step['action']]
                                need(capability in uia.actions(current),
                                     'workflow_pattern_unavailable')
                        value = values[step['parameter'].casefold()] if 'parameter' in step else step.get('value')
                        dispatching = True
                        operation = ('fill' if selector['role'] == 'textfield' else 'select') if step['action'] == 'set-value' else step['action']
                        try:
                            receipt = live.execute(surface, uia, element, {'role': selector['role']}, operation,
                                                   value, still_valid=check)
                        finally:
                            if anchor_proof is not None:
                                anchor_proof.close()
                                anchor_proof = None
                        dispatching = False
                        if resolution is not None:
                            receipt['target_resolution'] = resolution
                    receipt['lookup_seconds'] = lookup_elapsed
                    if popup:
                        receipt['popup'] = dict(title=popup['title'])
                routes.after(number, receipt)
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
                if (not read_only and step['action']=='row-open' and (receipt['status']=='verified' or
                        (receipt.get('diagnostic')=='row_open_dispatched' and receipt['input_dispatched'] is True))):
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
                if read_only or (step['action'] not in ('context-menu', 'submit') and receipt['status'] in ('verified', 'waited')):
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
                delivered_visual = (step['action'] == 'visual-click' and receipt['status'] == 'delivered_unverified'
                                    and receipt['input_dispatched'] is True
                                    and receipt.get('diagnostic') == 'visual_click_dispatched')
                delivered_conditional = (step['action'] == 'conditional-open' and receipt['status'] == 'delivered_unverified'
                                         and receipt.get('diagnostic') == 'conditional_open_dispatched'
                                         and receipt['input_dispatched'] is True)
                if delivered or delivered_menu or delivered_submit or delivered_visual or delivered_conditional:
                    report['status'] = 'completed_unverified'
                    if delivered and routes.origins.get(number):
                        pending_opener = number
                else:
                    report.update(status=receipt['status'], stopped_at=number,
                                  diagnostic=receipt.get('diagnostic', 'step_not_verified'))
                    if step['action'] in ('context-menu', 'submit') and receipt['status'] in ('verified', 'waited'):
                        report.update(status='blocked', diagnostic='submission_completion_unproven')
                    break
    except Exception as error:
        diagnostic = getattr(error, 'code', 'provider_error')
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
        if anchor_proof is not None:
            anchor_proof.close()
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


def _recorded_targets(recipe, source_task):
    has_conditional = any(s['action']=='conditional-open' for s in recipe['steps'])
    if source_task is None or not all(
            step['action'] in ('visual-click', 'row-action', 'conditional-open') or
            (step['action']=='row-open' and 'row_target' in step)
            or (step['action'] == 'row-launch' and ('row_hint' in step or has_conditional))
            for step in recipe['steps']):
        return False
    import recorded_plan
    compiled = (recorded_plan.compile_saved(source_task, app.task_directory())
                if has_conditional or any(s['action']=='row-open' for s in recipe['steps']) else recorded_plan.compile_plan(source_task))
    need(compiled.get('envelope', {}).get('recipe') == recipe, 'recorded_workflow_mismatch')
    return True


def register(recipe, destination, hwnd=None, *, source_task=None):
    """Register demonstrated targets offline, or resolve other selectors read-only."""
    recipe = deepcopy(validate(recipe))
    if source_task is not None:
        app.tasks._validate(source_task)
    destination = Path(destination).resolve()
    need(not destination.exists(), 'workflow_destination_exists')
    need(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', destination.name), 'workflow_folder_name_required')
    recorded = _recorded_targets(recipe, source_task)
    observed = dict(status='observed', steps=[]) if recorded else run(recipe, read_only=True, hwnd=hwnd)
    if observed['status'] != 'observed':
        return observed
    active = [s for s in recipe['steps'] if s['action'] != 'wait']
    rows = {row['number']: row for row in observed['steps']}
    for number, step in enumerate(recipe['steps'], 1):
        if step['action'] in ('wait', 'context-menu', 'result-menu', 'verify-file', 'row-action', 'row-launch','row-open', 'visual-click','conditional-open'):
            continue
        row = rows[number]
        if row['receipt']['status'] == 'deferred':
            need('popup' in step or 'window_context' in step, 'invalid_deferred_scope')
            continue
        capabilities = {'set-value': {'SetValue'}, 'invoke': {'Click'}, 'submit': {'SetFocus'}}[step['action']]
        if (step['action'] == 'set-value' and step['selector']['role'] == 'combobox'
                and (source_task is not None or step.get('allow_option_select'))):
            # A list-only combo exposes Expand on the field and Select on a
            # live option once opened. The player validates the requested item.
            capabilities.add('Expand')
            if 'SetValue' not in row['receipt']['actions'] and 'Expand' in row['receipt']['actions']:
                step['allow_option_select'] = True
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
    command = 'python -X utf8 -B "${CLAUDE_SKILL_DIR}/scripts/run.py"'
    guide = ('---\nname: ' + destination.name + '\ndescription: ' + description + '\n---\n\n'
             '# 등록된 업무 실행\n\n'
             '업무명, 대상 창과 순서는 `workflow.json`에 있다. 사용자의 실행 요청 범위와 대조한다. '
             + ('필수 입력은 ' + json.dumps(required, ensure_ascii=False) + '이다. 각 값을 받아 `--set "이름=값"`으로 전달한다.'
                if required else '이 작업은 실행 시 입력할 변수가 없다.') + '\n\n'
             '`' + command + '`으로 실행한다. '
             '현재 상태만 확인할 때는 `--read-only`를 붙인다. 이 명령은 대상 창을 복원하지 않는다.\n\n'
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
                  'delivered_unverified는 클릭 전달을 뜻한다. 앱 실행 등 결과는 해당 창에서 별도로 확인한다.\n')
    if any('grounding_text' in step or step['action']=='row-open' for step in active):
        guide += ('\n설명 속 이름이 현재 창에서 보이지 않으면 grounding_steps의 단계와 원문으로 '
                  'desktop-select의 ground에서 같은 창을 확인한다. 여러 행에 같은 실행 버튼이 있으면 '
                  '원본 녹화의 해당 단계에 bind-row --row-point X Y --point X Y로 행 이름과 그 행의 실행 버튼을 연결한다. '
                  '고유한 단일 대상은 bind-visual로 연결한다. 연결한 뒤 등록한 스킬을 갱신한다.\n')
    if any('popup' in pending for pending in observed.get('pending_scopes', [])):
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
    if any(step['action'] in ('context-menu', 'result-menu') for step in active):
        guide += ('\n실제 마우스로 표를 우클릭하는 단계는 대상 창을 앞으로 가져온 뒤 녹화한 영역을 확인하고 지정된 메뉴 경로를 순서대로 선택한다. '
                  '해당 단계가 끝날 때까지 마우스와 키보드를 사용하지 않는다.\n')
    if any('anchor' in step.get('selector', {}) for step in active):
        guide += ('\n상위 영역과 상대 위치로 찾는 입력란은 실행할 때 대상 창을 앞으로 가져와 확인한다. '
                  '대상을 찾는 동안 마우스와 키보드를 사용하지 않는다. '
                  '같은 종류 입력란의 배치가 바뀌면 역할과 클래스 검사만으로 구별할 수 없으므로 지정 위치를 다시 확인한다. '
                  '등록과 --read-only는 창을 앞으로 가져오지 않으므로 사용자가 해당 영역을 보이게 해 두어야 한다.\n')
    (destination / 'SKILL.md').write_text(guide, encoding='utf8')
    result = dict(status='registered', path=str(destination), required_parameters=required, input_dispatched=False,
                  input_requirements=input_policy.profile(active))
    if recorded:
        result['target_validation'] = 'recorded_evidence'
        result['pending_scopes'] = [dict(number=i, window_context=deepcopy(s['window_context']),
                                         diagnostic='validate_on_execution')
                                    for i, s in enumerate(recipe['steps'], 1) if 'window_context' in s]
    elif observed.get('pending_scopes'):
        result.update(target_validation='pending_scopes', pending_scopes=observed['pending_scopes'])
    return result


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
        else:
            p.add_argument('--set', action='append', default=[])
            p.add_argument('--only', help='run or inspect one workflow step by number or label')
            p.add_argument('--read-only', action='store_true')
            p.add_argument('--input-plan', action='store_true', help='preview input modes without opening or inspecting an app')
            p.add_argument('--background-only', action='store_true', help='refuse physical input, foregrounding and window restoration')
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
    mutating = args.command == 'run' and not args.read_only and not args.input_plan
    launched, executing = False, False
    try:
        source_task = None
        recipe = None
        if args.command != 'discover':
            if getattr(args, 'task', None):
                import recorded_plan
                source_task = app.tasks.load_task(args.task, app.task_directory())
                planned = recorded_plan.get_plan(source_task, app.task_directory())
                if planned.get('grounding_steps'):
                    print(json.dumps(dict(status='needs_grounding', diagnostic='target_grounding_required',
                                          input_dispatched=False, grounding_steps=planned['grounding_steps']),
                                     ensure_ascii=False))
                    return 2
                need(bool(planned.get('envelope')), 'recorded_task_not_compatible')
                recipe = planned['envelope']['recipe']
            else:
                recipe = load(args.recipe)
        overrides = {}
        if args.command == 'run':
            entries = selected_steps(recipe, args.only)
            active = dict(recipe, steps=[step for _, step in entries])
            requirements = input_policy.profile(active['steps'], [n for n, _ in entries])
            if args.input_plan:
                print(json.dumps(dict(status='observed', input_dispatched=False,
                                      input_requirements=requirements), ensure_ascii=False))
                return 0
            if args.background_only and not args.read_only:
                refusal = input_policy.blocked(requirements)
                if refusal:
                    print(json.dumps(refusal, ensure_ascii=False))
                    return 2
            overrides = app.tasks.parse_overrides(args.set)
            _, missing = values_for(active, overrides)
            if missing and not args.read_only:
                print(json.dumps(dict(status='needs_input', missing_parameters=missing,
                                      steps=[], input_dispatched=False), ensure_ascii=False))
                return 2
        if args._worker:
            if args.command == 'discover':
                result = discover(args.exe, args.window, args.hwnd)
            elif args.command == 'register':
                result = register(recipe, args.out, args.hwnd, source_task=source_task)
            else:
                executing = mutating
                result = run(recipe, overrides, read_only=args.read_only, hwnd=args.hwnd,
                             background_only=args.background_only, only=args.only)
        else:
            timeout = worker_timeout(active if args.command == 'run' else recipe) if recipe else 90
            launched = True
            child = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(Path(__file__).resolve()),
                                    '--_worker', *argv], capture_output=True, encoding='utf8', timeout=timeout)
            result = json.loads(child.stdout)
            need(child.returncode in (0, 2, 3) and isinstance(result, dict) and 'status' in result,
                 'invalid_worker_receipt')
    except (Exception, KeyboardInterrupt) as error:
        uncertain = (launched or executing) and mutating
        result = dict(status='uncertain' if uncertain else 'blocked', input_dispatched=None if uncertain else False,
                      diagnostic=getattr(error, 'code', 'workflow_unavailable'))
    print(json.dumps(result, ensure_ascii=False))
    return (0 if result['status'] in ('registered', 'observed', 'verified') else
            3 if result['status'] in ('completed_unverified', 'uncertain', 'delivered_unverified') else 2)


if __name__ == '__main__':
    raise SystemExit(main())
