"""Read the demonstrated windows and bind a host interpretation to a new local task."""
from copy import deepcopy
from hashlib import sha256
import json
import re

import compound_plan
import learned_tasks as tasks
import review_drafts
from window_context import ContextError, WindowRoutes, validate_context

INTERPRETED_MODES = tasks.INTERPRETED_ACTIONS


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
    from capture_trail import resolve_menu_scope
    ids = {row['step']['id']: number for number, row in enumerate(doc['rows'], 1)}
    captured = [row['step'] for row in doc['rows']]
    edits, rows = StepEdits(), []
    for number, row in enumerate(doc['rows'], 1):
        raw, form = row['step'], row['form']
        entry = dict(text=form['text'], mode=form['mode'], argument=form['argument'], context=None,
                     interpretation_source=deepcopy(raw))
        # A transient popup can be compiled as manual even when the recorder
        # identified a control. Keep its role/name when deciding what to fold.
        locator = raw.get('target', {}).get('locator', {})
        if locator.get('role'):
            entry['capture_role'] = locator['role']
            entry['capture_name'] = locator.get('name')
        try:
            scoped_raw = raw
            if '_capture_window' in raw:
                scoped_raw = dict(raw, _capture_window=resolve_menu_scope(raw['_capture_window'], captured, number))
            entry['context'] = _row_context(scoped_raw, ids, number)
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
    for key in ('target', 'reason', 'label', 'role', 'match', 'alias', 'description', 'menu_anchor',
                'menu_window_point', 'result_surface', 'wait_after'):
        if key in step:
            item[key] = step[key]
    if location := tasks.row_location(step):
        item['row_location'] = location
    if row['mode'] is not None:
        item.update(mode=row['mode'], argument=row['argument'])
    if row['context']:
        item['window_context'] = row['context']
    if 'hint' in row:
        item['recorded_hint'] = {key: row['hint'][key] for key in ('match', 'target', 'role', 'label')
                                 if key in row['hint']}
    if selector := tasks._recorded_selector(row.get('interpretation_source', step)):
        item['recorded_selector'] = selector
    problem = row.get('scope_problem') or row.get('field_problem')
    if problem:
        item['diagnostic'] = problem
    return item


def selected_draft_double(doc, selector):
    """Build an in-memory, single-gesture test. All sibling actions remain non-executable."""
    forms = [row['form'] for row in doc['rows']]
    number = tasks._step_number({'steps': forms},selector)
    raw = doc['rows'][number-1]['step']
    saved = raw.get('saved',raw)
    if saved.get('action') != 'visual-click' or not tasks.is_double_click_step(saved):
        return None
    rows = _rows(doc)
    selected = rows[number-1]
    if selected.get('field_problem') or selected.get('scope_problem'):
        raise tasks.TaskError(selected.get('field_problem') or selected['scope_problem'], step_number=number)
    step = selected['step']
    if step.get('action') != 'visual-click' or not tasks.is_double_click_step(step):
        return None
    # Testing just a child cannot establish that its earlier opener actually ran.
    if (selected['context'] or {}).get('after_step'):
        raise tasks.TaskError('window_origin_step_not_run', step_number=number)
    sequence = [tasks._manual('recording_draft_requires_review') for _ in rows]
    sequence[number-1] = step
    for index,row in enumerate(rows):
        context = row['context'] or {}
        capture_scope = doc['rows'][index]['step'].get('_capture_window', {})
        linked = (capture_scope.get('after_id') == raw['id'] or raw['id'] in capture_scope.get('_openers', ()))
        if linked and row.get('scope_problem'):
            raise tasks.TaskError(row['scope_problem'], step_number=index+1)
        if context.get('after_step') == number:
            sequence[index]['window_context'] = context
    plan = dict(schema=1,name=doc['name'],exe=doc['exe'],window=doc['window'],steps=sequence)
    tasks._validate(plan)
    return plan, number


def _observed_field(item, inventory):
    query, role = item.get('target'), item.get('role')
    identity = item.get('match')
    _require(identity in (None, 'automation_id', 'name'), 'invalid_recovery_target')
    _require(tasks._text(query) and isinstance(role, str) and role in (tasks.INVOKE | set(tasks.VALUE_ACTIONS.values())),
             'invalid_recovery_target')
    def matches(value):
        return isinstance(value, str) and value.strip().casefold() == query.strip().casefold()
    candidates = [node for node in inventory['targets'] if node.get('role') == role and
                  (node.get(identity) == query if identity else
                   any(matches(node.get(key)) for key in ('automation_id', 'name', 'semantic_label')))]
    within = item.get('within')
    if 'within' in item:
        _require(tasks._text(within) and within == within.strip(), 'invalid_task_scope')
        candidates = [node for node in candidates if within in node.get('binding_scopes', ())]
        _require(candidates, 'target_missing_in_scope')
    _require(len(candidates) == 1, 'target_ambiguous' if candidates else 'target_missing')
    node = candidates[0]
    _require(node.get('enabled') is True, 'target_disabled')
    _require(node.get('offscreen') is False, 'target_offscreen')
    aid = node.get('automation_id')
    if identity:
        match = identity
    elif tasks._text(aid) and not aid.isdecimal():
        match = 'automation_id'
    elif tasks._text(node.get('name')):
        match = 'name'
    else:
        match = 'query'
    target = node.get(match) if match != 'query' else node.get('semantic_label')
    _require(tasks._text(target), 'stable_locator_unavailable')
    if within:
        selector = node['binding_scopes'][within]
        parent = selector['ancestors'][0]
        return node, dict(selector, within=parent.get('automation_id') or parent['name'])
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
    _require(set(item) <= {'number', 'mode', 'target', 'role', 'argument', 'wait_after', 'match', 'within'}, 'invalid_recovery_binding')
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
    if ('within' not in item and hint.get('ancestors') and all(
            step.get(key) == hint.get(key) for key in ('role', 'target', 'match'))):
        step['ancestors'] = deepcopy(hint['ancestors'])
        step['within'] = hint['within']
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


def _interpreted_binding(row, item, inventory=None, *, app=None, surface=None):
    """Bind the host's typed intent without requiring a particular source phrasing."""
    _require(set(item) <= {'number', 'mode', 'selector', 'result_row'}, 'invalid_recovery_binding')
    _require(row['mode'] not in ('fixed', 'required'), 'instruction_binding_conflict')
    source = deepcopy(row.get('interpretation_source', row['step']))
    if row['context']:
        source['window_context'] = deepcopy(row['context'])
    step = tasks.interpret_step(source, row['text'], item['mode'], item.get('selector'),
                                result_row=item.get('result_row'))
    if inventory is None:
        # A closed downstream window cannot be observed now. Only retain its
        # actual demonstrated identity, then resolve it after the opener runs.
        captured = (tasks.result_surface_hint(source) if item['mode'] == 'double-click-result'
                    else tasks._recorded_selector(source))
        _require(captured == step['selector'] and any(k in captured for k in ('name', 'automation_id')),
                 'recorded_target_required_for_deferred_binding')
    else:
        _observe_command(step, inventory, app, surface)
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
            if hint.get('within') and hint.get('role') == role and any(
                    hint['within'] in node.get('binding_scopes', {}) for node in inventory['targets']):
                request['within'] = hint['within']
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
                if identity['match'] in ('automation_id', 'name'):
                    request['match'] = identity['match']
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


def _captured_menu_name(row):
    step = row['step']
    if row.get('capture_role') == 'menuitem' and row.get('capture_name'):
        return row['capture_name']
    if step.get('role') == 'menuitem' and step.get('match') in ('name', 'automation_id'):
        return step['target'] if step['match'] == 'name' else step.get('command_name')


def _menu_tail(row, known_captions=()):
    """Return witnessed captions or the user's complete, explicit menu path."""
    step = row['step']
    if (row.get('field_problem') or row['mode'] not in (None, 'recorded') or 'wait_after' in step
            or row.get('capture_role') not in (None, 'menuitem')):
        return []
    text = row['text'].strip()
    name = _captured_menu_name(row)
    if name and not tasks._text(name):
        return []
    # A witnessed caption may itself read "Do Not Disturb". Check the direction
    # around the literal caption for conditions and negation.
    direction = re.sub(re.escape(name), '', text, flags=re.I) if name else text
    if tasks._conditional_sentence(direction) or re.search(r'하지\s*(?:마|말)|말고|대신|\b(?:never|instead|do not)\b', direction, re.I):
        return []
    route = tasks.click_menu_path(text)
    click = tasks.click_request(text)
    if step['action'] == 'open':
        if step.get('match') == 'query':
            # The editor can turn an unrecognized popup click into a query for
            # its caption. It has no witnessed button identity. A real captured
            # button is excluded above (draft) or by its stable match (task).
            role = step.get('role')
            if (not click or role not in ('button', 'menuitem')
                    or step.get('within') or step.get('expand') or step.get('target') != click[0]):
                return []
        elif step.get('role') != 'menuitem':
            return []
    elif (step['action'] != 'manual' or step.get('reason') not in (
            'popup_needs_live_binding', 'capture_target_unavailable', 'unsupported_recorded_action', 'unsupported_demonstration')):
        return []
    if route:
        return route if not name or _menu_caption(name) == _menu_caption(route[-1]) else []
    if click:
        caption = click[0]
    elif _menu_caption(text) in known_captions:
        caption = text
    else:
        selection = re.fullmatch(r'(.+?)\s*(?:을|를)?\s*선택(?:해\s*(?:줘|주세요))?[.!。]?', text)
        if selection is None:
            selection = re.fullmatch(r'select\s+(.+?)[.!]?', text, re.IGNORECASE)
        caption = selection[1].strip() if selection and tasks._text(selection[1], 200) else None
    if name and caption and _menu_caption(name) != _menu_caption(caption):
        return []
    return [name or caption] if name or caption else []


def _next_menu_index(path, index, row):
    labels = [_menu_caption(label) for label in _menu_tail(row, path[index:index+1])]
    if len(labels) > 1:
        return len(path) if index < len(path) and labels == path else None
    if labels and index < len(path) and labels[0] == path[index]:
        return index + 1


def _menu_hint(rows, number, inventory):
    start = rows[number - 1]
    path = tasks.click_menu_path(start['text'])
    end = number
    tails = []
    for n, following in enumerate(rows[number:number+8], number+1):
        if following.get('scope_problem') or following['context'] != start['context']:
            break
        known = {_menu_caption(label) for label in (path or inventory.get('open_menu_items', []))}
        labels = _menu_tail(following, known)
        if not labels:
            break
        if len(labels) > 1:
            if tails or (path and list(map(_menu_caption, path)) != list(map(_menu_caption, labels))):
                return None
            path, end = labels, n
            break
        caption = labels[0]
        # An unknown click with a caption shared by a live ordinary control
        # needs host inspection. Do not turn a toolbar or dialog into a menu.
        if not _captured_menu_name(following) and any(
                node.get('role') in tasks.INVOKE - {'menuitem'} and node.get('offscreen') is False
                and _menu_caption(node.get('name') or '') == _menu_caption(caption)
                for scope in [inventory, *inventory.get('owned_popups', [])] for node in scope.get('targets', [])):
            break
        if path and (len(tails) >= len(path) or _menu_caption(caption) != _menu_caption(path[len(tails)])):
            break
        tails.append(caption)
        end = n
        if path and len(tails) == len(path):
            break
    if not path:
        path = tails
    if path:
        return dict(number=number, through=end, mode='context-menu', path=path)


def _any_body_cell(text):
    # Only generic region nouns: do not broaden "selected row", named cells,
    # ordinal rows, filters or other record-specific targets.
    return bool(re.fullmatch(
        r'(?:(?:조회(?:된)?|결과|조회\s*결과|데이터|검색\s*결과)\s*)*'
        r'(?:표|테이블|그리드)(?:(?:의|\s)*(?:안|내부|영역|데이터\s*영역))?'
        r'\s*(?:에서|에|를|을)?\s*(?:마우스\s*)?우클릭(?:을)?\s*'
        r'(?:눌러|클릭|해|하|하기)?\s*(?:줘|주세요|기)?[.!。]?'
        r'|right[- ]click\s+(?:in\s+|on\s+)?(?:the\s+)?(?:results?\s+|data\s+)?(?:table|grid)[.!]?',
        text.strip(), re.IGNORECASE))


def _demonstrated_result_point(text):
    # A demonstrative refers to this exact recorded point, never any other cell.
    return bool(re.fullmatch(r'(?:(?:데이터\s*)?(?:조회|검색)\s*결과가\s*(?:나오면|표시되면)\s*)?'
                             r'(?:이|해당)\s*(?:영역|위치)에서\s*우클릭(?:을)?\s*(?:해|눌러)?\s*(?:줘|주세요)?[.!。]?'
                             r'|right[- ]click\s+(?:here|in this area|at this point)[.!]?',
                             text.strip(), re.IGNORECASE))


def _menu(app, rows, item, owner, surface, title, fingerprint, *, prepare=False, detail=None, inventory=None):
    import live_recovery
    number, end = item['number'], item.get('through', item['number'])
    start, path = rows[number - 1], item.get('path')
    _require(start['step'].get('reason') == 'right_click_requires_menu_teaching' and start['step']['action'] == 'manual',
             'recorded_right_click_required')
    allowed = {'number', 'mode', 'through', 'target', 'role', 'frame', 'point', 'path', 'output',
               'menu_anchor', 'menu_window_point'}
    extras = sorted(set(item) - allowed)
    if extras:
        if detail is not None:
            detail['unsupported_fields'] = extras
            detail['recovery_hint'] = '메뉴 연결에 지원하지 않는 키가 있습니다. 메뉴 경로는 path에 지정하세요.'
        raise tasks.TaskError('invalid_recovery_binding')
    for evidence in ('menu_anchor', 'menu_window_point'):
        if evidence in item:
            # Hosts may copy recorded evidence from the receipt. Only the
            # recording can supply this point; changing it needs a fresh frame.
            _require(start['step'].get(evidence) is not None and item[evidence] == start['step'][evidence]
                     and 'frame' not in item and 'point' not in item, 'context_region_changed')
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
    elif start['step'].get('menu_window_point') and 'frame' not in grounded and 'point' not in grounded:
        grounded['menu_window_point'] = start['step']['menu_window_point']
    if prepare:
        grounded['preparing'] = True
    if _any_body_cell(start['text']):
        grounded['point_policy'] = 'any_data_cell'
    context = live_recovery.menu_from_frame(app, owner, surface, title, [fingerprint, number], grounded,
                                            observation=detail, inventory=inventory)
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
        if step['action'] == 'manual' and tasks.is_double_click_step(row['step']):
            step['reason'] = 'double_click_needs_review'
        item = operations.get(number, {})
        if not item and row['step'].get('reason') == 'right_click_requires_menu_teaching':
            # Reserve only a structurally supported demonstrated menu route as
            # the source for a following related window. Actual binding happens
            # later against this window's live region before the task is saved.
            item = _menu_hint(rows, number, {'targets': []}) or {}
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
                           (sequence[origin-1]['action'] == 'manual' and not tasks.is_double_click_step(sequence[origin-1]))):
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
                 item.get('mode') in ('context-menu', 'recorded', 'fixed', 'required', *INTERPRETED_MODES) and
                 item['number'] not in indexed, 'invalid_recovery_binding')
        indexed[item['number']] = item
    return indexed


def _blocked(result, diagnostic, **fields):
    # Hosts sometimes display just the first part of a long window inventory.
    # Keep the cause and the next action ahead of the original step evidence.
    header = dict(status='needs_grounding', diagnostic=diagnostic, **fields)
    if result.get('host_action'):
        header.setdefault('host_action', result['host_action'])
    return {**header, **{key: value for key, value in result.items() if key not in header}}


def _retry_after_restore(args):
    retry = ['recover', '--task', args.task]
    for option in ('prepare', 'image', 'only', 'bindings', 'save_as'):
        value = getattr(args, option, None)
        if value:
            retry.append('--' + option.replace('_', '-'))
            if value is not True:
                retry.append(str(value))
    return retry


def double_click_handoff(name, number):
    return dict(host_action='ground_recorded_double_click', ground_args=['ground','--task',name,'--only',str(number)],
                capture_requires_consent=True)


def missing_coordinate(name, number, source, sentence):
    """A missing taught position cannot be recovered by observing pixels in a different frame."""
    if not tasks.is_double_click_step(source) or not tasks.coordinate_request(sentence):
        return None
    missing = []
    if not tasks.result_surface_hint(source):
        missing.append('selector')
    if not tasks.valid_result_point(source.get('result_point')):
        missing.append('result_point')
    if missing:
        return dict(status='needs_grounding', diagnostic='recorded_coordinate_required',
                    host_action='review_recorded_position', source_task=name, step_number=number,
                    required_fields=missing, edit_args=['review', '--task', name, '--edit'], input_dispatched=False)


def missing_double_target(report, plan):
    """A changed visual after earlier steps needs one new observation of this step only."""
    if report.get('diagnostic') not in ('visual_target_not_found','visual_target_ambiguous','visual_target_too_common',
                                        'visual_target_changed_before_input','visual_target_lacks_detail'):
        return False
    number = report.get('source_stopped_at', report.get('stopped_at'))
    if type(number) is not int or not 1 <= number <= len(plan['steps']):
        return False
    last = (report.get('steps') or [{}])[-1]
    if (last.get('source_step',last.get('number')) != number or last.get('receipt',{}).get('input_dispatched') is not False
            or not tasks.is_double_click_step(plan['steps'][number-1])):
        return False
    report.update(double_click_handoff(plan['name'],number))
    return True


def _suggest(number, rows, detail, observation, name):
    if (tasks.is_double_click_step(rows[number-1]['step']) and detail.get('action') == 'manual'
            and tasks.double_click_instruction(detail['text'])):
        detail.update(double_click_handoff(name, number))
        return None
    if detail.get('reason') != 'right_click_requires_menu_teaching':
        return _suggest_binding(number, rows[number-1], observation)
    hint = _menu_hint(rows, number, observation)
    anchor = detail.get('menu_anchor')
    if hint:
        detail['menu_binding_hint'] = hint
        if anchor:
            matches = [node for node in observation['targets']
                       if all(node.get(k) == v for k, v in anchor['target'].items())]
            if len(matches) == 1 and matches[0].get('enabled') is True and matches[0].get('offscreen') is False:
                return hint
        elif detail.get('menu_window_point') and (_any_body_cell(rows[number-1]['text']) or
                                                  _demonstrated_result_point(rows[number-1]['text'])):
            # A window-relative point is only a hint. Require a single actual
            # table and later prove this point belongs to that table.
            tables = [n for n in observation['targets'] if n.get('role') == 'table'
                      and n.get('enabled') is True and n.get('offscreen') is False]
            if len(tables) == 1:
                table = tables[0]
                key = table.get('automation_id')
                name = key if tasks._text(key) and not key.isdecimal() else table.get('name')
                if tasks._text(name):
                    return dict(hint, target=name, role='table')
    detail['image_args'] = ['recover', '--task', name, '--only', str(number), '--image']
    detail['capture_requires_consent'] = True


def _recorded_lookups(rows, context=None):
    """Use native lookups for fully identified recordings, including their split popup captions."""
    selectors, captions, through = [], [], 0
    for number, row in enumerate(rows, 1):
        if row['context'] != context:
            continue
        if row.get('scope_problem'):
            return None
        if number <= through:
            continue
        step = row.get('hint', row['step'])
        if step.get('reason') == 'right_click_requires_menu_teaching':
            anchor = step.get('menu_anchor')
            hint = _menu_hint(rows, number, {'targets': []})
            if not anchor or not hint:
                return None
            selector = anchor['target']
            through = hint['through']
            captions.extend(hint['path'])
        elif step.get('action') == 'result-menu':
            selector = step['context']['menu']['target']
            captions.extend(step['context']['menu']['path'])
        elif step.get('action') in tasks.CAPTURE_COMMANDS and not row.get('field_problem'):
            selector = step['selector']
        elif step.get('match') in ('automation_id', 'name') and not row.get('field_problem'):
            selector = {step['match']: step['target'], 'role': step['role']}
            if step.get('ancestors'):
                selector['ancestors'] = step['ancestors']
        elif step.get('action') in ('visual-click', 'row-action', 'row-launch', *tasks.OBSERVE_ACTIONS,
                                     'double-click-result', 'close-popup'):
            continue  # These are checked against the post-action screen at execution.
        else:
            return None
        if selector not in selectors:
            selectors.append(selector)
    return dict(selectors=selectors, captions=captions)


def _prepare_lookups(rows, context):
    lookups = _recorded_lookups(rows, context)
    fields = (row.get('hint', row['step']) for row in rows if row['context'] == context)
    scopes = [(step['target'], step['role'], step['within']) for step in fields
              if step.get('match') in ('name', 'automation_id') and step.get('within')]
    if scopes:
        if lookups is None:
            return dict(scoped_fields=scopes)
        lookups['fallback_scopes'] = scopes
    return lookups


def _binding_lookups(operations, rows, context):
    """Generated bindings carry exact identities; hand-written label queries still need discovery."""
    scoped_fields = [(item.get('target'), item.get('role'), item['within']) for number, item in operations.items()
                     if rows[number-1]['context'] == context and item['mode'] != 'context-menu' and 'within' in item]
    if scoped_fields:
        _require(all(all(tasks._text(s) for s in field) for field in scoped_fields), 'invalid_task_scope')
        return dict(scoped_fields=scoped_fields)
    selectors, regions = [], []
    for number, item in operations.items():
        if rows[number-1]['context'] != context:
            continue
        if item['mode'] in INTERPRETED_MODES:
            selector = item.get('selector')
            from python_workflow import validate_selector
            validate_selector(selector)
            if selector not in selectors:
                selectors.append(selector)
            continue
        if item['mode'] == 'context-menu':
            # _menu revalidates the region/point independently. A supplied frame
            # also needs to disambiguate an untyped region name in the inventory.
            if 'frame' in item:
                name, role = item.get('target'), item.get('role')
                if not tasks._text(name) or not tasks._text(role):
                    return None
                if (name, role) not in regions:
                    regions.append((name, role))
            continue
        if item.get('match') not in ('automation_id', 'name'):
            return None
        selector = {item['match']: item.get('target'), 'role': item.get('role')}
        if selector not in selectors:
            selectors.append(selector)
    return dict(selectors=selectors, captions=[], regions=regions)


def _observe_command(step, inventory, app=None, surface=None):
    selector = step['selector']
    candidates = [node for node in inventory['targets'] if node.get('recorded_selector') == selector]
    if not candidates and app is not None and surface is not None and any(k in selector for k in ('anchor', 'ancestors')):
        # A mixed draft can require a full inventory for another unknown step.
        # That inventory deliberately omits unnamed inputs. Resolve this exact
        # recorded input directly and reuse the result for its Enter stage.
        checks = inventory.setdefault('recorded_selector_checks', [])
        previous = next((row for row in checks if row['selector'] == selector), None)
        if previous is not None and previous.get('diagnostic'):
            raise tasks.TaskError(previous['diagnostic'])
        import live_recovery
        try:
            with app.bridge.UIA() as uia:
                surface.check()
                candidates = live_recovery._recorded_targets(app, surface, uia,
                                                             dict(selectors=[selector], captions=[]))
                surface.check()
        except app.bridge.BridgeError as error:
            checks.append(dict(selector=selector, diagnostic=error.code))
            raise
        inventory['targets'].extend(candidates)
        checks.append(dict(selector=selector, status='observed'))
    if not candidates and 'anchor' not in selector:
        identities = {k: selector[k] for k in ('role', 'name', 'automation_id') if k in selector}
        candidates = [node for node in inventory['targets'] if all(node.get(k) == v for k, v in identities.items())]
    _require(len(candidates) == 1, 'target_ambiguous' if candidates else 'target_missing')
    _require(candidates[0].get('enabled') is True, 'target_disabled')
    _require(candidates[0].get('offscreen') is False, 'target_offscreen')


def run(app, args):
    import live_recovery
    directory = app.task_directory()
    doc = tasks.load_task(args.task, directory, allow_review=True)
    rows, fingerprint = _rows(doc), _fingerprint(doc)
    only = args.only
    _require(only is None or (type(only) is int and 1 <= only <= len(rows)), 'unknown_task_step')
    _require(not args.image or only is not None, 'recovery_image_step_required')
    prepare = getattr(args, 'prepare', False)
    _require((not prepare and bool(args.save_as) == bool(args.bindings) and not (args.bindings and (args.image or only)))
             or (prepare and not (args.bindings or args.image or only)), 'invalid_recovery_options')
    destination = args.save_as or (f"{doc['name'][:75]} - {fingerprint[:12]}" if prepare else None)
    # A dialog that only exists after a later action cannot invalidate exact
    # selectors from the recorded prefix. Resolve that prefix at actual dispatch.
    deferred_prefix = 0
    if prepare:
        for number, row in enumerate(rows, 1):
            if row.get('scope_problem') or row.get('field_problem') or row['step']['action'] == 'manual':
                source = row.get('interpretation_source', row['step'])
                if handoff := missing_coordinate(doc['name'], number, source, row['text']):
                    return dict(handoff, source_fingerprint=fingerprint)
                context = row.get('context') or {}
                if number > 1 and context.get('related') and context.get('after_step'):
                    import recorded_plan
                    prefix = {k: doc[k] for k in ('name', 'schema', 'exe', 'window')}
                    prefix['steps'] = [r['step'] for r in rows[:number-1]]
                    if recorded_plan.compile_plan(prefix).get('envelope'):
                        deferred_prefix = number - 1
                break
    if destination:
        _require(tasks._name(destination).casefold() != doc['name'].casefold(), 'task_already_exists')
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
        if getattr(args, 'restore_window', False):
            result['window_restored'] = app.visibility.restore_for_action(owner, doc['window'])
        else:
            # A child-only request can otherwise report a missing popup before
            # it ever observes the minimized business window that owns it.
            app.visibility.require_observable(owner)
    except (ValueError, app.bridge.BridgeError) as error:
        reason = getattr(error, 'code', 'recorded_window_not_open')
        if reason == 'target_window_minimized' and not getattr(args, 'restore_window', False):
            return _blocked(result, reason, step_number=min(operations, default=selection[0]),
                            retry_args=_retry_after_restore(args), host_action='show_recorded_window_then_retry')
        return _blocked(result, reason)

    observations, revised, remap = {}, [], {}
    skip_until = 0
    suggestions, suggested_through = [], 0
    template = dict(source_fingerprint=fingerprint, steps=suggestions)
    pending = {}
    for number, detail in zip(selection, details):
        row, operation = rows[number-1], operations.get(number)
        if number <= skip_until:
            _require(operation is None, 'overlapping_recovery_bindings')
            remap[number] = len(revised)
            continue
        try:
            origin = (row['context'] or {}).get('after_step')
            if number <= deferred_prefix:
                revised.append(deepcopy(row['step']))
                remap[number] = len(revised)
                detail['scope_validation'] = 'at_execution'
                continue
            if prepare and origin in pending:
                raise tasks.TaskError('recorded_window_opener_unresolved')
            if not args.bindings or operation:
                try:
                    surface = _bind_surface(app, doc, owner, rows, sequence, number)
                except tasks.TaskError as error:
                    # Recorded related dialogs often no longer exist after the
                    # demonstration. Keep identified literal steps and validate
                    # their live targets immediately after their actual opener.
                    context = row.get('context')
                    interpreted = operation and operation['mode'] in INTERPRETED_MODES
                    if not ((prepare or interpreted) and error.code == 'recorded_window_not_open' and context
                            and context.get('related') is True and origin in remap
                            and (interpreted or (not row.get('field_problem') and row['step']['action'] != 'manual'
                            and (row['step']['action'] in (*tasks.CAPTURE_COMMANDS, *tasks.OBSERVE_ACTIONS,
                                                          'close-popup')
                                 or row['step'].get('match') in ('automation_id', 'name'))))):
                        raise
                    step = _interpreted_binding(row, operation) if interpreted else deepcopy(row['step'])
                    step['window_context'] = dict(context, after_step=remap[origin])
                    revised.append(step)
                    remap[number] = len(revised)
                    detail.update(scope_validation='deferred_until_opener', opener_step=origin)
                    continue
                # Narrow lookups belong to this recorded opening of the window.
                # A later opening may use the same HWND with different fields.
                context = row['context']
                key = surface.hwnd, surface.pid, surface.created
                if context and (prepare or args.bindings):
                    key += tuple(sorted(context.items()))
                if key not in observations:
                    scoped_lookups = (_binding_lookups(operations, rows, context) if args.bindings
                                      else _prepare_lookups(rows, context) if prepare else None)
                    observation = live_recovery.observe_scope(app, owner, surface, doc['window'],
                                            frame_key=[fingerprint, number] if args.image else None,
                                            **({'bindings_only': True} if args.bindings else {}),
                                            **({'lookups': scoped_lookups} if scoped_lookups else {}))
                    observations[key] = len(result['live_scopes'])
                    result['live_scopes'].append(observation)
                detail['scope_index'] = observations[key]
                observation = result['live_scopes'][observations[key]]
                if args.image and detail.get('menu_window_point') and observation.get('width') and observation.get('height'):
                    detail['recorded_image_point'] = [min(extent-1, round(fraction*extent)) for fraction, extent in
                        zip(detail['menu_window_point'], [observation['width'], observation['height']])]
            if not args.bindings and (prepare or number > suggested_through):
                candidate = (None if row['step']['action'] in tasks.CAPTURE_COMMANDS else
                             _suggest(number, rows, detail, observation, doc['name']))
                if number > suggested_through:
                    if candidate:
                        suggestions.append(candidate)
                    if detail.get('menu_binding_hint'):
                        suggested_through = detail['menu_binding_hint']['through']
                if prepare:
                    operation = candidate
            if not args.bindings and not prepare:
                continue
            if row.get('scope_problem'):
                raise tasks.TaskError(row['scope_problem'])
            if operation and operation['mode'] == 'context-menu':
                step, skip_until = _menu(app, rows, operation, owner, surface, doc['window'], fingerprint,
                                         prepare=prepare, detail=detail, inventory=observation)
            elif operation and operation['mode'] in INTERPRETED_MODES:
                step = _interpreted_binding(row, operation, observation, app=app, surface=surface)
            elif operation:
                step = _field(row, operation, observation)
            else:
                if row.get('field_problem'):
                    raise tasks.TaskError(row['field_problem'])
                step = deepcopy(row['step'])
                if prepare and (step['action'] == 'manual' or step.get('match') == 'query'):
                    raise tasks.TaskError(step.get('reason', 'instruction_requires_structure'))
                if prepare and step.get('match') in ('automation_id', 'name'):
                    key = step['match']
                    matches = [node for node in observation['targets'] if node.get('role') == step['role']
                               and node.get(key) == step['target']]
                    if step.get('within') and (len(matches) > 1 or any(n.get('binding_scopes') for n in matches)):
                        matches = [node for node in matches if step['within'] in node.get('binding_scopes', {})]
                    _require(len(matches) == 1, 'target_ambiguous' if matches else 'target_missing')
                    _require(matches[0].get('enabled') is True, 'target_disabled')
                    _require(matches[0].get('offscreen') is False, 'target_offscreen')
                if prepare and step['action'] in tasks.CAPTURE_COMMANDS:
                    _observe_command(step, observation, app, surface)
            context = step.get('window_context')
            if context and 'after_step' in context:
                _require(context['after_step'] in remap, 'recorded_window_opener_unresolved')
                context['after_step'] = remap[context['after_step']]
            revised.append(step)
            remap[number] = len(revised)
        except (ValueError, app.bridge.BridgeError) as error:
            reason = getattr(error, 'code', 'target_unavailable')
            detail['diagnostic'] = reason
            if reason == 'target_window_minimized':
                # Reading the other controls on this minimized surface would
                # repeat the same failure and can turn it into target_missing.
                if not row['context'] and not getattr(args, 'restore_window', False):
                    return _blocked(result, reason, step_number=number, retry_args=_retry_after_restore(args),
                                    host_action='show_recorded_window_then_retry')
                return _blocked(result, reason, step_number=number, host_action='show_recorded_window_then_retry')
            if reason == 'context_point_occluded':
                detail['recovery_hint'] = '녹화한 창의 표 지점이 가려져 있습니다. 해당 위치가 보이는 상태에서 같은 보정을 계속하세요.'
            if reason == 'menu_step_range_mismatch':
                detail['conflicting_step'] = error.step_number
                detail['recovery_hint'] = f"{error.step_number}번의 창, 컨트롤 및 메뉴 이름을 원문과 대조하세요."
                if error.step_number != number:
                    detail['recovery_hint'] += " 별도 동작이면 through를 그 단계 앞에서 끝내세요."
            if reason in ('recorded_window_not_open', 'invalid_window_origin_step',
                          'recorded_window_opener_unresolved') and row.get('context'):
                detail['opener_step'] = row['context'].get('after_step')
            if deferred_prefix and reason == 'recorded_window_not_open' and number == deferred_prefix + 1:
                if detail.get('recorded_selector') and not row.get('scope_problem'):
                    return _blocked(result, 'instruction_requires_structure', step_number=number,
                        bindings_template=template, host_action='bind_recorded_instructions_from_live_scopes',
                        interpretation_hint=dict(number=number, text=detail['text'],
                            selector=detail['recorded_selector'], window_context=row['context'],
                            supported_modes=sorted(INTERPRETED_MODES), validation='after_recorded_opener'))
                return _blocked(result, reason, step_number=number,
                        host_action='inspect_recorded_step_when_popup_opens', opener_step=origin,
                        instruction=detail['text'], window=detail['window'],
                        inspect_args=['recover', '--task', doc['name'], '--only', str(number)])
            if args.bindings:
                if (reason == 'step_requires_dedicated_binding' and tasks.is_double_click_step(row['step'])
                        and tasks.double_click_instruction(row['text'])):
                    return _blocked(result, reason, step_number=number, **double_click_handoff(doc['name'], number))
                if (row['step'].get('reason') == 'right_click_requires_menu_teaching' and operation
                        and reason in ('invalid_recovery_binding', 'step_requires_dedicated_binding',
                                       'invalid_menu_path', 'invalid_menu_step_range', 'invalid_context_point')):
                    hint = _menu_hint(rows, number, observation)
                    if hint:
                        detail['menu_binding_hint'] = hint
                    if operation['mode'] != 'context-menu':
                        detail['recovery_hint'] = '녹화된 우클릭과 이어지는 항목은 mode가 context-menu인 한 단계로 연결하세요.'
                return _blocked(result, reason, step_number=number, **{key: detail[key] for key in
                                ('unsupported_fields', 'menu_binding_hint', 'recovery_hint') if key in detail})
            if prepare:
                pending[number] = reason
                if 'menu_binding_hint' in detail:
                    detail['image_args'] = ['recover', '--task', doc['name'], '--only', str(number), '--image']
                    detail['capture_requires_consent'] = True
    if not args.bindings and (not prepare or pending):
        result['host_action'] = 'bind_recorded_instructions_from_live_scopes'
        if pending:
            n = next(iter(pending))
            if details[n-1].get('ground_args') and pending[n] in ('double_click_needs_review', 'instruction_requires_structure'):
                return _blocked(result, pending[n], step_number=n, bindings_template=template,
                                **double_click_handoff(doc['name'], n))
            return _blocked(result, pending[n], step_number=n, bindings_template=template)
        result['bindings_template'] = template
        if not result['live_scopes']:
            return _blocked(result, details[0].get('diagnostic', 'target_unavailable'))
        return result
    app.guard(owner, owner, doc['window'])
    task = dict(schema=1, name=destination, exe=doc['exe'], window=doc['window'], steps=revised)
    if prepare:
        import recorded_plan
        plan = recorded_plan.compile_plan(task)
        if not plan.get('envelope'):
            return _blocked(result, plan.get('diagnostic', 'instruction_requires_structure'),
                            step_number=next((i for i, n in remap.items() if n == plan.get('step')), None),
                            bindings_template=template,
                            host_action='bind_recorded_instructions_from_live_scopes')
    try:
        # Compare and publish under the same short lock used to revise editor
        # drafts. The source may have been edited while live targets were read.
        with review_drafts._edit_lock(directory):
            _require(_fingerprint(tasks.read_document(args.task, directory)) == fingerprint, 'recording_draft_changed')
            try:
                tasks.save_task(task, directory)
            except tasks.TaskError as error:
                if not prepare or error.code != 'task_already_exists' or tasks.read_document(destination, directory) != task:
                    raise
    except OSError as error:
        raise tasks.TaskError('task_store_unavailable') from error
    except tasks.TaskError as error:
        if not error.step_number:
            raise
        original_number = next(i for i, n in remap.items() if n == error.step_number)
        return _blocked(result, error.code, step_number=original_number)
    saved = dict(status='recorded', source_task=doc['name'], task=tasks.summary(task), input_dispatched=False,
                 source_fingerprint=fingerprint, source_steps=remap,
                 execution_plan=app.execution_plan_info(app.saved_execution_plan(task)))
    if 'window_restored' in result:
        saved['window_restored'] = result['window_restored']
    if prepare:
        saved['host_action'] = 'use_prepared_task'
        saved['observed_scopes'] = len(observations)
        saved['lookup_modes'] = [scope.get('lookup_mode', 'window_inventory') for scope in result['live_scopes']]
        saved['menu_previews'] = [dict(number=d['number'], **d['menu_preview']) for d in details if 'menu_preview' in d]
        saved['pending_scopes'] = [dict(number=d['number'], opener_step=d['opener_step'],
                                       scope_validation=d['scope_validation']) for d in details
                                   if d.get('scope_validation') == 'deferred_until_opener']
    return saved
