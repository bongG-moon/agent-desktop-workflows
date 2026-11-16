"""Read the demonstrated windows and bind a host interpretation to a new local task."""
from copy import deepcopy
from hashlib import sha256
import json
import re

import compound_plan
import learned_tasks as tasks
import review_drafts
from window_context import ContextError, WindowRoutes, validate_context


def _require(ok, reason):
    if not ok:
        raise tasks.TaskError(reason)


def _fingerprint(document):
    data = json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return sha256(data).hexdigest()


def _row_context(raw, ids, number):
    context = deepcopy(raw.get('_capture_window') or raw.get('window_context'))
    if not context:
        return None
    if '_openers' in context:
        raise tasks.TaskError('recorded_window_opener_required')
    if 'after_id' in context:
        origin = ids.get(context.pop('after_id'), 0)
        _require(0 < origin < number, 'recorded_window_opener_required')
        context['after_step'] = origin
    return validate_context(context)


def _rows(doc):
    if doc.get('kind') != review_drafts.KIND:
        rows = []
        for step in doc['steps']:
            spec = step.get('instruction', {})
            text = step.get('instruction_draft') or spec.get('text') or step.get('description') or step.get('label', '')
            rows.append(dict(step=deepcopy(step), text=text, mode=spec.get('mode'),
                             argument=spec.get('parameter', spec.get('value')), context=step.get('window_context')))
        return rows

    from teach_capture import StepEdits
    ids = {row['step']['id']: number for number, row in enumerate(doc['rows'], 1)}
    edits, rows = StepEdits(), []
    for number, row in enumerate(doc['rows'], 1):
        raw, form = row['step'], row['form']
        entry = dict(text=form['text'], mode=form['mode'], argument=form['argument'], context=None)
        # A transient popup can be compiled as manual even when the recorder
        # identified a control. Keep its role/name when deciding what to fold.
        locator = raw.get('target', {}).get('locator', {})
        if locator.get('role'):
            entry['capture_role'] = locator['role']
            entry['capture_name'] = locator.get('name')
        try:
            entry['context'] = _row_context(raw, ids, number)
        except (tasks.TaskError, ContextError) as error:
            entry['scope_problem'] = error.code
        try:
            edit = edits.compile(raw, form['alias'], form['text'], form['mode'], form['argument'])
            entry['step'] = tasks.recorded_step(edits.edited_step(raw, edit))
        except (ValueError, RuntimeError) as error:
            entry['field_problem'] = getattr(error, 'code', 'invalid_editor_fields')
            # Keep the captured identity and its original security classification.
            # A failed form can be repaired using live evidence, never executed as-is.
            try:
                entry['step'] = tasks.recorded_step(raw)
            except (ValueError, RuntimeError) as capture_error:
                entry['step'] = tasks._manual('invalid_recorded_capture')
                entry['field_problem'] = getattr(capture_error, 'code', 'invalid_recorded_capture')
            if form['alias']:
                entry['step']['alias'] = form['alias']
            signal = tasks._convert(raw, allow_point=True)
            if signal.get('match') in ('name', 'automation_id'):
                entry['hint'] = signal
            # The original stop remains a stop. Keeping this note allows other
            # observed rows to be saved while a downstream popup is still closed.
            if (entry['step']['action'] == 'manual' and form['mode'] is None and
                    tasks._text(form['text'], 1000) and len(form['text'].encode('utf8')) <= 1000 and
                    entry['field_problem'] == 'instruction_target_unavailable'):
                entry['step'] = tasks.instructed_step(dict(raw, alias=form['alias']),
                                      dict(mode='draft', text=form['text'].strip()))
                entry.pop('field_problem')
        if entry['context']:
            entry['step']['window_context'] = deepcopy(entry['context'])
        rows.append(entry)
    return rows


def _summary(number, row, doc):
    step, scope = row['step'], row['context'] or doc
    item = dict(number=number, text=row['text'], action=step['action'],
                window=scope.get('title', doc['window']), exe=scope['exe'])
    for key in ('target', 'reason', 'label', 'role', 'match', 'alias', 'description', 'menu_anchor', 'wait_after'):
        if key in step:
            item[key] = step[key]
    if row['mode'] is not None:
        item.update(mode=row['mode'], argument=row['argument'])
    if row['context']:
        item['window_context'] = row['context']
    if 'hint' in row:
        item['recorded_hint'] = {key: row['hint'][key] for key in ('match', 'target', 'role', 'label')
                                 if key in row['hint']}
    problem = row.get('scope_problem') or row.get('field_problem')
    if problem:
        item['diagnostic'] = problem
    return item


def _observed_field(item, inventory):
    query, role = item.get('target'), item.get('role')
    _require(tasks._text(query) and isinstance(role, str) and role in (tasks.INVOKE | set(tasks.VALUE_ACTIONS.values())),
             'invalid_recovery_target')
    def matches(value):
        return isinstance(value, str) and value.strip().casefold() == query.strip().casefold()
    candidates = [node for node in inventory['targets'] if node.get('role') == role and
                  any(matches(node.get(key)) for key in ('automation_id', 'name', 'semantic_label'))]
    _require(len(candidates) == 1, 'target_ambiguous' if candidates else 'target_missing')
    node = candidates[0]
    _require(node.get('enabled') is True, 'target_disabled')
    _require(node.get('offscreen') is False, 'target_offscreen')
    aid = node.get('automation_id')
    if tasks._text(aid) and not aid.isdecimal():
        match = 'automation_id'
    elif tasks._text(node.get('name')):
        match = 'name'
    else:
        match = 'query'
    target = node.get(match) if match != 'query' else node.get('semantic_label')
    _require(tasks._text(target), 'stable_locator_unavailable')
    if match != 'query':
        _require(sum(candidate.get('role') == role and candidate.get(match) == target
                     for candidate in inventory['targets']) == 1, 'target_ambiguous')
    return node, dict(match=match, target=target, role=role)


def _post_click_wait(text):
    # Only a complete single affirmative click followed by a fixed duration.
    # Conditions, other actions and time ranges still need host interpretation.
    match = re.fullmatch(r'([^,;!?\n]{1,180}?)(?:누르고|클릭하고|실행하고|누른\s*(?:뒤|후))\s*'
                         r'(\d{1,3}(?:\.\d{1,2})?)\s*초(?:간)?\s*'
                         r'(?:기다려|기다리기|대기(?:해|하기)?)\s*(?:줘|주세요)?[.!。]?', text.strip())
    if match and not tasks._conditional_sentence(text) and not re.search(
            r'하지\s*말|말고|하고|누르고|대신|다음에', match[1]):
        seconds = float(match[2])
        if 0 < seconds <= 120:
            return seconds


def _field(row, item, inventory):
    _require(set(item) <= {'number', 'mode', 'target', 'role', 'argument', 'wait_after'}, 'invalid_recovery_binding')
    original, mode, argument = row['step'], item['mode'], item.get('argument')
    _require(original['action'] in (*tasks.VALUE_ACTIONS, 'open') or tasks.is_recoverable(original),
             'step_requires_dedicated_binding')
    # The recorder's explicit field mode is a user choice. Do not turn a
    # required parameter into a recorded literal (or change its given name).
    if row['mode'] in ('required', 'fixed'):
        _require(mode == row['mode'] and (row['argument'] == argument or
                 (row['mode'] == 'required' and not row['argument'])), 'instruction_binding_conflict')
    node, step = _observed_field(item, inventory)
    hint = row.get('hint', original)
    if hint.get('match') in ('name', 'automation_id') and (
            node.get(hint['match']) != hint.get('target') or hint.get('role') != step['role']):
        # A recording is a hint. Rebinding to a different actual control needs
        # the new control's current ID, label or name in the user's own wording.
        _require(any(_mentions(row['text'], node.get(key)) for key in ('automation_id', 'name', 'semantic_label')),
                 'live_target_does_not_match_instruction')
    action = {'combobox': 'select', 'textfield': 'fill'}.get(step['role'], 'open')
    _require((action == 'open' and mode == 'recorded') or
             (action in tasks.VALUE_ACTIONS and mode in ('fixed', 'required')), 'instruction_action_mismatch')
    step.update(action=action)
    if action == 'open':
        step['expand'] = False
        if node.get('name') and step['match'] == 'automation_id':
            step['command_name'] = node['name']
    else:
        step['recorded_effect'] = 'unobserved'
        if node.get('semantic_label'):
            step['label'] = node['semantic_label']
    for key in ('alias', 'description', 'window_context', 'wait_after'):
        if key in original:
            step[key] = deepcopy(original[key])
    if 'wait_after' in item:
        step['wait_after'] = item['wait_after']
    sentence = row['text'] or step.get('command_name') or node.get('semantic_label') or step['target']
    wait = _post_click_wait(sentence) if action == 'open' else None
    if wait is not None:
        _require(step.get('wait_after', wait) == wait, 'instruction_binding_conflict')
        step['wait_after'] = wait
    step['instruction'] = tasks.compile_instruction(step, sentence, mode=mode, argument=argument)
    _require(step['instruction'] and step['instruction']['mode'] == mode, 'instruction_requires_structure')
    return step


def _suggest_binding(number, row, inventory):
    """Use a literal the user actually supplied and a current, unique control."""
    step, text = row['step'], row['text']
    if not text or (step['action'] not in (*tasks.VALUE_ACTIONS, 'open') and not tasks.is_recoverable(step)):
        return None
    hint = row.get('hint', step)
    subject = tasks.instruction_subject(text)
    click = tasks.click_request(text)
    queries = [subject, click[0] if click else None, hint.get('target')]
    roles = tasks.INVOKE | set(tasks.VALUE_ACTIONS.values())
    if hint.get('role') in roles:
        roles = [hint['role']]
    for query in dict.fromkeys(filter(None, queries)):
        choices = []
        for role in roles:
            request = dict(number=number, target=query, role=role)
            try:
                observed, identity = _observed_field(request, inventory)
                identity.update(action={'combobox':'select', 'textfield':'fill'}.get(role, 'open'))
                if observed.get('semantic_label'):
                    identity['label'] = observed['semantic_label']
                if observed.get('name') and identity['action'] == 'open':
                    identity['command_name'] = observed['name']
                explicit = row['mode']
                wait = _post_click_wait(text) if identity['action'] == 'open' else None
                if wait is not None:
                    request['wait_after'] = wait
                    explicit = explicit or 'recorded'
                argument = row['argument'] if explicit in ('fixed', 'required') else None
                spec = tasks.compile_instruction(identity, text, mode=explicit, argument=argument)
                if spec is None or spec['mode'] not in ('fixed', 'required', 'recorded'):
                    continue
                request.update(target=identity['target'], mode=spec['mode'])
                if spec['mode'] != 'recorded':
                    request['argument'] = spec.get('parameter', spec.get('value'))
                _field(row, request, inventory)
                choices.append(request)
            except tasks.TaskError:
                continue
        if len(choices) == 1:
            return choices[0]
        if choices:
            return None
    return None


def _mentions(text, label):
    if not tasks._text(label) or not isinstance(text, str):
        return False
    words = [re.escape(w) for w in label.strip().split()]
    phrase = r'\s+'.join(words)
    return re.search(r'(?<![a-z0-9_])' + phrase + r'(?![a-z0-9_])', text, re.IGNORECASE) is not None


def _menu_caption(label):
    return ' '.join(label.split()).casefold()


def _menu_tail(row):
    """Return witnessed captions or the user's complete, explicit menu path."""
    step = row['step']
    if (row.get('field_problem') or row['mode'] not in (None, 'recorded') or 'wait_after' in step
            or row.get('capture_role') not in (None, 'menuitem')):
        return []
    route = tasks.click_menu_path(row['text'])
    name = row.get('capture_name')
    if step['action'] == 'open':
        if step.get('match') == 'query':
            # Older drafts turn an unrecognized click into an unscoped button
            # query for the WHOLE route. An ordinary single button, an alias
            # rebound to another target or an explicitly scoped query stays put.
            click = tasks.click_request(row['text'])
            role = step.get('role')
            if (not click or role not in ('button', 'menuitem') or (role == 'button' and not route)
                    or step.get('within') or step.get('expand') or step.get('target') != click[0]):
                return []
        elif step.get('role') == 'menuitem':
            name = step.get('command_name') or (step.get('target') if step.get('match') == 'name' else name)
        else:
            return []
    elif (step['action'] != 'manual' or step.get('reason') not in (
            'popup_needs_live_binding', 'capture_target_unavailable', 'unsupported_recorded_action', 'unsupported_demonstration')):
        return []
    if name and not tasks._text(name):
        return []
    if route:
        return route if not name or _menu_caption(name) == _menu_caption(route[-1]) else []
    if name:
        return [name]
    text = row['text'].strip()
    click = tasks.click_request(text)
    if click:
        return [click[0]]
    # The recording editor also accepts a bare caption or "Active sheet 선택".
    caption = re.sub(r'\s*(?:을|를)?\s*선택(?:해\s*(?:줘|주세요))?[.!。]?$', '', text).strip()
    return [caption] if tasks._text(caption, 200) else []


def _next_menu_index(path, index, row):
    labels = [_menu_caption(label) for label in _menu_tail(row)]
    if len(labels) > 1:
        return len(path) if index < len(path) and labels == path else None
    if labels and labels[0] in path[index:]:
        return path.index(labels[0], index) + 1


def _menu_hint(rows, number):
    start = rows[number - 1]
    path = tasks.click_menu_path(start['text'])
    end = number
    if number < len(rows):
        following = rows[number]
        if not following.get('scope_problem') and following['context'] == start['context']:
            tail = _menu_tail(following)
            if len(tail) > 1:
                if path and list(map(_menu_caption, path)) != list(map(_menu_caption, tail)):
                    return None
                path, end = tail, number + 1
    if path:
        return dict(number=number, through=end, mode='context-menu', path=path)


def _menu(app, rows, item, owner, surface, title, fingerprint):
    import live_recovery
    number, end = item['number'], item.get('through', item['number'])
    _require(set(item) <= {'number', 'mode', 'through', 'target', 'role', 'frame', 'point', 'path', 'output'},
             'invalid_recovery_binding')
    start, path = rows[number - 1], item.get('path')
    _require(start['step'].get('reason') == 'right_click_requires_menu_teaching' and start['step']['action'] == 'manual',
             'recorded_right_click_required')
    _require(isinstance(path, list) and 0 < len(path) <= 8 and all(tasks._text(label, 200) for label in path),
             'invalid_menu_path')
    _require(type(end) is int and number <= end <= len(rows) and end - number <= len(path), 'invalid_menu_step_range')
    normalized = [_menu_caption(label) for label in path]
    written = tasks.click_menu_path(start['text'])
    if written and [_menu_caption(label) for label in written] != normalized:
        raise tasks.TaskError('menu_step_range_mismatch', step_number=number)
    path_index = 0
    for tail_number, row in enumerate(rows[number:end], number + 1):
        path_index = _next_menu_index(normalized, path_index, row)
        if path_index is None or row.get('scope_problem') or row['context'] != start['context']:
            raise tasks.TaskError('menu_step_range_mismatch', step_number=tail_number)
    grounded = dict(item)
    if (anchor := start['step'].get('menu_anchor')) and 'frame' not in grounded and 'point' not in grounded:
        grounded['menu_anchor'] = anchor
    context = live_recovery.menu_from_frame(app, owner, surface, title, [fingerprint, number], grounded)
    step = dict(action='result-menu', label=' → '.join(path), context=context)
    if start['context']:
        step['window_context'] = deepcopy(start['context'])
    return step, end


def _routes(rows, operations):
    # Invalid links should not prevent observation of other available surfaces.
    sequence = []
    menu_end = 0
    for number, row in enumerate(rows, 1):
        step = dict(action=row['step']['action'])
        item = operations.get(number, {})
        if item.get('mode') == 'context-menu' and type(item.get('through')) is int:
            menu_end = item['through']
        if number <= menu_end:
            step['action'] = 'result-menu'
        if number in operations and step['action'] == 'manual':
            # A source step can be repaired together with its dependent rows.
            # Its proposed role is only for routing. The binding is checked live
            # before any dependent change can be saved.
            if tasks.is_recoverable(row['step']) or item['mode'] == 'context-menu':
                step['action'] = 'result-menu' if item['mode'] == 'context-menu' else 'open'
        context = row['context']
        if context:
            origin = context.get('after_step')
            if origin and (not 1 <= origin < number or rows[origin-1].get('scope_problem') or
                           sequence[origin-1]['action'] == 'manual'):
                row['scope_problem'] = 'invalid_window_origin_step'
            else:
                step['window_context'] = context
        sequence.append(step)
    return sequence


def _bind_surface(app, doc, owner, rows, sequence, number):
    row = rows[number - 1]
    if row.get('scope_problem'):
        raise tasks.TaskError(row['scope_problem'])
    if not row['context']:
        return owner
    routes = WindowRoutes(sequence, owner, doc['window'], read_only=True, active=[number])
    surface = routes.resolve(row['context'])
    _require(surface is not None, 'recorded_window_not_open')
    return surface


def _binding_map(file, fingerprint, count):
    data = compound_plan.read_json(file)
    _require(isinstance(data, dict) and set(data) == {'source_fingerprint', 'steps'}, 'invalid_recovery_bindings')
    _require(data['source_fingerprint'] == fingerprint, 'recording_draft_changed')
    _require(isinstance(data['steps'], list) and len(data['steps']) <= count, 'invalid_recovery_bindings')
    indexed = {}
    for item in data['steps']:
        _require(isinstance(item, dict) and type(item.get('number')) is int and 1 <= item['number'] <= count and
                 item.get('mode') in ('context-menu', 'recorded', 'fixed', 'required') and
                 item['number'] not in indexed, 'invalid_recovery_binding')
        indexed[item['number']] = item
    return indexed


def run(app, args):
    import live_recovery
    directory = app.task_directory()
    doc = tasks.load_task(args.task, directory, allow_review=True)
    rows, fingerprint = _rows(doc), _fingerprint(doc)
    only = args.only
    _require(only is None or (type(only) is int and 1 <= only <= len(rows)), 'unknown_task_step')
    _require(not args.image or only is not None, 'recovery_image_step_required')
    _require(bool(args.save_as) == bool(args.bindings) and not (args.bindings and (args.image or only)),
             'invalid_recovery_options')
    selection = list(range(1, len(rows)+1)) if only is None else [only]
    operations = {}
    if args.bindings:
        tasks.check_new_task(args.save_as, directory)
        operations = _binding_map(args.bindings, fingerprint, len(rows))
    sequence = _routes(rows, operations)
    details = [_summary(n, rows[n-1], doc) for n in selection]
    result = dict(status='observed', source_task=doc['name'], source_fingerprint=fingerprint, steps=details,
                  live_scopes=[], input_dispatched=False)
    try:
        owner = app.bridge.Binding(app.pick_window(app.windows(doc['exe']), doc['window'], None))
    except (ValueError, app.bridge.BridgeError) as error:
        return dict(result, status='needs_grounding', diagnostic=getattr(error, 'code', 'recorded_window_not_open'))

    observations, revised, remap = {}, [], {}
    skip_until = 0
    for number, detail in zip(selection, details):
        row, operation = rows[number-1], operations.get(number)
        if number <= skip_until:
            _require(operation is None, 'overlapping_recovery_bindings')
            remap[number] = len(revised)
            continue
        try:
            if not args.bindings or operation:
                surface = _bind_surface(app, doc, owner, rows, sequence, number)
                key = surface.hwnd, surface.pid, surface.created
                if key not in observations:
                    observation = live_recovery.observe_scope(app, owner, surface, doc['window'],
                                            frame_key=[fingerprint, number] if args.image else None)
                    observations[key] = len(result['live_scopes'])
                    result['live_scopes'].append(observation)
                detail['scope_index'] = observations[key]
                observation = result['live_scopes'][observations[key]]
            if not args.bindings:
                continue
            if row.get('scope_problem'):
                raise tasks.TaskError(row['scope_problem'])
            if operation and operation['mode'] == 'context-menu':
                step, skip_until = _menu(app, rows, operation, owner, surface, doc['window'], fingerprint)
            elif operation:
                step = _field(row, operation, observation)
            else:
                if row.get('field_problem'):
                    raise tasks.TaskError(row['field_problem'])
                step = deepcopy(row['step'])
            context = step.get('window_context')
            if context and 'after_step' in context:
                context['after_step'] = remap[context['after_step']]
            revised.append(step)
            remap[number] = len(revised)
        except (ValueError, app.bridge.BridgeError) as error:
            reason = getattr(error, 'code', 'target_unavailable')
            detail['diagnostic'] = reason
            if reason == 'menu_step_range_mismatch':
                detail['conflicting_step'] = error.step_number
                detail['recovery_hint'] = f"{error.step_number}번의 창, 컨트롤 및 메뉴 이름을 원문과 대조하세요."
                if error.step_number != number:
                    detail['recovery_hint'] += " 별도 동작이면 through를 그 단계 앞에서 끝내세요."
            if reason in ('recorded_window_not_open', 'invalid_window_origin_step') and row.get('context'):
                detail['opener_step'] = row['context'].get('after_step')
            if args.bindings:
                return dict(result, status='needs_grounding', diagnostic=reason, step_number=number)
    if not args.bindings:
        result['host_action'] = 'bind_recorded_instructions_from_live_scopes'
        suggested = []
        menu_end = 0
        for detail in details:
            if 'scope_index' not in detail:
                continue
            n = detail['number']
            inventory = result['live_scopes'][detail['scope_index']]
            if detail.get('reason') == 'right_click_requires_menu_teaching':
                hint = _menu_hint(rows, n)
                anchor = detail.get('menu_anchor')
                if hint:
                    detail['menu_binding_hint'] = hint
                    menu_end = hint['through']
                if anchor and hint:
                    target = anchor['target']
                    matches = [node for node in inventory['targets'] if all(node.get(k) == v for k, v in target.items())]
                    if len(matches) == 1 and matches[0].get('enabled') is True and matches[0].get('offscreen') is False:
                        suggested.append(hint)
                        continue
                if not anchor or hint:
                    detail['image_args'] = ['recover', '--task', doc['name'], '--only', str(n), '--image']
                continue
            if n <= menu_end:
                continue
            item = _suggest_binding(n, rows[n-1], inventory)
            if item:
                suggested.append(item)
        result['bindings_template'] = dict(source_fingerprint=fingerprint, steps=suggested)
        if not result['live_scopes']:
            result.update(status='needs_grounding', diagnostic=details[0].get('diagnostic', 'target_unavailable'))
        return result
    app.guard(owner, owner, doc['window'])
    task = dict(schema=1, name=args.save_as, exe=doc['exe'], window=doc['window'], steps=revised)
    try:
        # Compare and publish under the same short lock used to revise editor
        # drafts. The source may have been edited while live targets were read.
        with review_drafts._edit_lock(directory):
            _require(_fingerprint(tasks.read_document(args.task, directory)) == fingerprint, 'recording_draft_changed')
            tasks.save_task(task, directory)
    except OSError as error:
        raise tasks.TaskError('task_store_unavailable') from error
    except tasks.TaskError as error:
        if not error.step_number:
            raise
        original_number = next(i for i, n in remap.items() if n == error.step_number)
        return dict(result, status='needs_grounding', diagnostic=error.code, step_number=original_number)
    return dict(status='recorded', source_task=doc['name'], task=tasks.summary(task), input_dispatched=False,
                source_fingerprint=fingerprint, source_steps=remap,
                execution_plan=app.execution_plan_info(app.saved_execution_plan(task)))
