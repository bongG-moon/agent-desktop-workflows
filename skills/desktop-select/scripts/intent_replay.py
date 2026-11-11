"""Execute the user's descriptions after the caller model resolves their scoped UI actions."""
import copy

import learned_tasks as tasks
from intent_tasks import IntentError, plan_digest, validate_resolution


def _need(ok, code):
    if not ok:
        raise IntentError(code)


def validate_action(action):
    _need(isinstance(action, dict), 'invalid_resolved_action')
    action = copy.deepcopy(action)
    kind = action.get('action')
    common = {'action', 'completion'}
    _need(action.get('completion', 'verified') in ('verified', 'delivery'), 'invalid_completion_mode')
    if kind == 'result-menu':
        _need(not set(action) - (common | {'context', 'label'}), 'unexpected_resolved_action_data')
        import result_menu
        result_menu.validate_spec(action.get('context'))
        action.setdefault('label', 'Recorded result menu')
        _need(tasks._text(action['label'], 200), 'invalid_resolved_label')
        return action
    _need(kind in ('select', 'open'), 'unsupported_resolved_action')
    allowed = common | {'role', 'target', 'match', 'expand'}
    if kind == 'select':
        allowed.update(('value', 'parameter'))
        _need(('value' in action) != ('parameter' in action), 'selection_value_or_parameter_required')
        _need(action.get('completion', 'verified') == 'verified', 'selection_requires_readback')
        key = 'parameter' if 'parameter' in action else 'value'
        _need(tasks._text(action[key], 200 if key == 'parameter' else 10000), 'invalid_resolved_value')
    _need(not set(action) - allowed, 'unexpected_resolved_action_data')
    _need(action.get('match', 'automation_id') in ('automation_id', 'name') and
          tasks._text(action.get('target'), 1000), 'invalid_resolved_target')
    action.setdefault('match', 'automation_id')
    if kind == 'select':
        _need(action.get('role', 'combobox') == 'combobox' and 'expand' not in action, 'invalid_resolved_role')
        action['role'] = 'combobox'
    else:
        _need(type(action.get('expand', False)) is bool, 'invalid_resolved_action')
        action.setdefault('expand', False)
        roles = tasks.EXPAND if action['expand'] else tasks.INVOKE | tasks.CHOOSE
        _need(action.get('role') in roles, 'invalid_resolved_role')
        if action.get('completion') == 'delivery':
            _need(not action['expand'] and action['role'] in tasks.INVOKE, 'selection_requires_readback')
    return action


def describe_plan(plan):
    return {'plan_digest': plan_digest(plan), 'task': tasks.summary(plan),
            'resolution_schema': {'schema': 1, 'plan_digest': plan_digest(plan),
                                  'steps': [{'number': i, 'actions': []} for i, step in enumerate(plan['steps'], 1)
                                            if step['action'] == 'intent']},
            'hint': 'Interpret each saved instruction with the current anchored UI; do not edit skill source.',
            'input_dispatched': False}


def scope(nodes, step):
    """Approximate positions establish a local group; they are not an executable selector."""
    anchor = step.get('anchor')
    _need(anchor is not None, 'intent_target_required')
    selector = {k: anchor[k] for k in ('role', 'automation_id', 'name') if k in anchor}
    # Prefer the stable ID. A changing caption does not change the demonstrated area.
    if selector.get('automation_id'):
        selector.pop('name', None)
    by_path = {n['path']: n for n in nodes}
    hits = []
    for i, n in enumerate(nodes):
        if n.get('secure') or not all(n.get(k) == v for k, v in selector.items()):
            continue
        chain, path = [], n.get('parent_path')
        while path:
            parent = by_path[path]
            chain.append(parent)
            path = parent.get('parent_path')
        ancestors = iter(chain)
        if all(any(all(p.get(k) == v for k, v in a.items()) for p in ancestors)
               for a in anchor.get('ancestors', [])):
            hits.append(i)
    _need(len(hits) == 1, 'intent_anchor_ambiguous' if hits else 'intent_anchor_missing')
    idx = hits[0]
    grouping = {'group', 'toolbar', 'tab', 'menu', 'table', 'list', 'tree', 'custom', 'document'}
    if nodes[idx].get('role') in grouping:
        base = nodes[idx]['path']
    else:
        base = nodes[idx].get('parent_path') or nodes[0]['path']
        path = base
        while path:
            n = by_path[path]
            if n.get('role') in grouping and (n.get('automation_id') or n.get('name')):
                base = path
                break
            path = n.get('parent_path')
    indexes = [i for i, n in enumerate(nodes) if n['path'] == base or n['path'].startswith(base + '/')]
    region = [dict(nodes[i], parent_path='') if nodes[i]['path'] == base else nodes[i] for i in indexes]
    _need(region and not region[0].get('secure'), 'intent_region_unavailable')
    return region, indexes, idx


def replay_intents(plan, exe, window, overrides, invoke, *, only=None, resolution=None):
    if only is not None:
        only = tasks._step_number(plan, only)
    sequence = [(n, s) for n, s in enumerate(plan['steps'], 1) if only is None or only == n]
    for n, step in sequence:
        if step['action'] == 'intent' and not step['instruction']['text'].strip():
            return dict(status='needs_instruction', stopped_at=n, input_dispatched=False,
                        diagnostic='instruction_text_required', label=step['label'])
    values = {k.casefold(): v for k, v in overrides.items()}
    _need(len(values) == len(overrides) and all(tasks._text(v, 10000) for v in values.values()), 'invalid_override')
    actions = validate_resolution(plan, resolution, validate_action) if resolution is not None else None
    # Splitting legacy steps for preflight must not turn one ambiguous alias
    # into permission to change several unrelated fields. Keep the original
    # whole-sequence override contract before checking individual legacy steps.
    legacy = [step for _, step in sequence if step['action'] != 'intent']
    for key in values:
        targets = [step for step in legacy if key in
                   ((tasks._parameter(step) or '').casefold(), step.get('alias', '').casefold())]
        if len(targets) > 1 and not all(step.get('instruction', {}).get('mode') == 'required'
                                       and (tasks._parameter(step) or '').casefold() == key for step in targets):
            raise tasks.TaskError('ambiguous_parameter')
    required = []
    legacy_missing = []
    for n, step in sequence:
        if step['action'] == 'intent':
            required.extend(step['instruction']['parameters'])
            if actions is not None:
                required.extend(a['parameter'] for a in actions[n] if 'parameter' in a)
        else:
            keys = {(tasks._parameter(step) or '').casefold(), step.get('alias', '').casefold()}
            subset = {k: v for k, v in overrides.items() if k.casefold() in keys}
            # Reuse legacy validation before any real dispatch. This private callback
            # validates scheduling only; its receipt is never returned as an executed action.
            check = tasks.replay(dict(plan, steps=[step]), exe, window, subset,
                                 lambda _: dict(status='verified', input_dispatched=False,
                                                effect_verification='matched'))
            if check['status'] == 'needs_input':
                legacy_missing.extend(check['missing_parameters'])
            elif check['status'] != 'verified':
                return dict(check, stopped_at=n)
    missing = list(dict.fromkeys([*(k for k in required if k.casefold() not in values), *legacy_missing]))
    if missing:
        return dict(status='needs_input', input_dispatched=False, missing_parameters=missing,
                    diagnostic='required_parameter_missing')
    for n, step in sequence:
        if step['action'] == 'intent' and step['anchor'] is None:
            return dict(status='needs_target', stopped_at=n, input_dispatched=False,
                        diagnostic='use_edit_to_point_at_this_step', instruction=step['instruction']['text'])
    if actions is None and any(s['action'] == 'intent' for _, s in sequence):
        return dict(describe_plan(plan), status='needs_interpretation')
    allowed = {p.casefold() for p in required}
    allowed.update((tasks._parameter(s) or '').casefold() for _, s in sequence if s['action'] != 'intent')
    allowed.update(s.get('alias', '').casefold() for _, s in sequence if s['action'] != 'intent')
    _need(not set(values) - allowed, 'unknown_parameter')
    report = dict(status='verified', input_dispatched=False, steps=[])
    delivery_only = False
    for n, step in sequence:
        if step['action'] != 'intent':
            keys = {(tasks._parameter(step) or '').casefold(), step.get('alias', '').casefold()}
            subset = {k: v for k, v in overrides.items() if k.casefold() in keys}
            # Isolate the legacy path so a mixed task can use older recorded actions.
            legacy = dict(plan, steps=[step])
            answer = tasks.replay(legacy, exe, window, subset, invoke)
            report['steps'].append({'number': n, 'receipt': answer})
            sent = answer.get('input_dispatched')
            report['input_dispatched'] = None if sent is None else bool(report['input_dispatched'] or sent)
            if answer['status'] != 'verified':
                return dict(report, status=answer['status'], stopped_at=n, diagnostic=answer.get('diagnostic'))
            continue
        for sub, template in enumerate(actions[n], 1):
            action = copy.deepcopy(template)
            if 'parameter' in action:
                action['value'] = values[action.pop('parameter').casefold()]
            completion = action.pop('completion', 'verified')
            # Private dispatch envelope: only validated resolutions can construct it.
            envelope = dict(action='intent-execute', intent=step, operation=action)
            try:
                receipt = invoke(envelope)
            except Exception as error:
                receipt = dict(status='uncertain', input_dispatched=None,
                               diagnostic=getattr(error, 'code', 'dispatch_interrupted'))
            if (not isinstance(receipt, dict) or receipt.get('status') not in
                    ('verified', 'blocked', 'uncertain', 'mismatch', 'delivered_unverified')
                    or 'input_dispatched' not in receipt
                    or (receipt['input_dispatched'] is not None and type(receipt['input_dispatched']) is not bool)
                    or (receipt.get('status') == 'verified' and
                        (receipt.get('effect_verification') != 'matched' or
                         type(receipt['input_dispatched']) is not bool))):
                receipt = dict(status='uncertain', input_dispatched=None, diagnostic='invalid_action_receipt')
            report['steps'].append({'number': n, 'action_number': sub, 'receipt': receipt})
            sent = receipt.get('input_dispatched')
            report['input_dispatched'] = None if sent is None else bool(report['input_dispatched'] or sent)
            if receipt['status'] == 'verified':
                continue
            known_delivery = (receipt['status'] == 'delivered_unverified' and sent is True and
                              (receipt.get('method') == 'Invoke' and
                               receipt.get('diagnostic') == 'business_result_not_verified' or
                               action['action'] == 'result-menu' and receipt.get('leaf_dispatched') is True and
                               receipt.get('menu_path_completed') is True and
                               receipt.get('diagnostic') == 'menu_delivered_no_output_condition'))
            if completion == 'delivery' and known_delivery:
                delivery_only = True
                continue
            return dict(report, status=receipt['status'], stopped_at=n, action_number=sub,
                        diagnostic=receipt.get('diagnostic'))
    if delivery_only:
        report.update(status='completed_unverified', business_commit='not_verified',
                      diagnostic='requested_commands_delivered_results_not_all_verified')
    return report
