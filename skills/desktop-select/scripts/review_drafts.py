"""Persist the recorder's editable form when it cannot yet be a replayable task."""
from contextlib import contextmanager
from copy import deepcopy
import os

import learned_tasks as tasks


KIND = 'recording-review'


def saved_form(step):
    instruction = step.get('instruction', {})
    fallback = f"{step['keys']} 키 누르기" if step.get('action') == 'press-key' else step.get('label', '')
    text = instruction.get('text') or step.get('instruction_draft') or step.get('description') or fallback
    mode = instruction.get('mode')
    return dict(alias=step.get('alias', ''), text=text, mode=mode if mode in ('fixed', 'required') else None,
                argument=instruction.get('parameter', instruction.get('value', '')))


def retain_saved_arguments(source, fresh):
    """Earlier recordings can carry a default and an overridable parameter without an instruction."""
    prior = source.get('saved', {})
    if 'wait_after' in prior and fresh['action'] == 'click':
        fresh = dict(fresh, wait_after=prior['wait_after'])
    if 'instruction' in prior or 'instruction_draft' in prior or not {'value', 'parameter'} & prior.keys():
        return fresh
    current = tasks.recorded_step(fresh)
    if current['action'] != prior['action']:
        raise tasks.TaskError('instruction_action_mismatch')
    for key in ('value', 'parameter'):
        current.pop(key, None)
        if key in prior:
            current[key] = prior[key]
    if current.get('recorded_effect') == 'matched' and prior.get('value') != fresh.get('value'):
        current['recorded_effect'] = 'unverified'
    if 'description' in prior or prior.get('label'):
        current['description'] = prior.get('description') or prior['label']
    tasks._validate_step(current, set())
    return dict(action='saved-step', id=fresh['id'], label=fresh['label'], saved=current)


def rows_from_task(plan):
    """Retain compiled selectors and gestures verbatim; express window links in stable editor IDs."""
    tasks._validate(plan)
    rows = []
    for number, step in enumerate(plan['steps'], 1):
        saved = deepcopy(step)
        context = saved.pop('window_context', None)
        label = step.get('label') or step.get('command_name') or step.get('alias') or step.get('target') or step['action']
        row = dict(id=f'saved-{number}', action='saved-step', label=label, saved=saved)
        if context:
            if 'after_step' in context:
                context['after_id'] = f"saved-{context.pop('after_step')}"
            row['_capture_window'] = context
        rows.append(dict(step=row, form=saved_form(step)))
    return rows


def _window_key(step):
    scope = step.get('_capture_window') or {}
    return scope.get('exe', '').casefold(), scope.get('title', '')


def _replacement_key(row):
    """Match an observed target and gesture without using values or step order."""
    from teach_capture import StepEdits, bridge
    step, form = row['step'], row['form']
    try:
        fields = StepEdits().compile(step, form['alias'], form['text'], form['mode'], form['argument'])
        command = tasks.recorded_step(StepEdits.edited_step(step, fields))
    except (ValueError, bridge.BridgeError):
        if not tasks._point_target(step):
            return None
        try:
            command = tasks._convert(step, allow_point=True)
        except (ValueError, bridge.BridgeError):
            return None
    selector = tasks._recorded_selector(command)
    if not selector or command['action'] == 'manual':
        return None
    if command.get('within') and not selector.get('ancestors'):
        selector = dict(selector, within=command['within'])
    # Two points in one custom table and different keyboard gestures are distinct.
    action = command['action']
    if selector['role'] in ('textfield', 'combobox') and action in (*tasks.VALUE_ACTIONS, *tasks.OBSERVE_ACTIONS):
        action = 'field'
    elif action == 'close-popup':
        action = 'open'
    return (_window_key(step), action, selector, command.get('keys'),
            command.get('result_row'), command.get('result_point'))


def replace_rows(rows, selected_ids, replacements):
    """Replace a contiguous selection while keeping forms and proven window routes."""
    from teach_capture import StepEdits, bridge
    from capture_trail import resolve_menu_scope

    selected = set(selected_ids)
    indexes = [i for i, row in enumerate(rows) if row['step']['id'] in selected]
    if (not indexes or len(indexes) != len(selected)
            or indexes != list(range(indexes[0], indexes[-1] + 1))):
        raise ValueError('다시 녹화할 단계를 연속해서 선택하세요.')
    if not replacements:
        raise ValueError('기록된 동작이 없습니다. 다시 녹화하거나 취소하세요.')
    if len(rows) - len(indexes) + len(replacements) > tasks.MAX_STEPS:
        raise ValueError('교체 후 단계 수가 녹화 한도를 넘습니다. 새 동작을 줄여 주세요.')

    first, last = indexes[0], indexes[-1] + 1
    original, fresh = deepcopy(rows), deepcopy(replacements)
    previous = original[first:last]
    origin = previous[0]['step'].get('_capture_window')
    for row in fresh:
        if origin and '_capture_window' not in row['step']:
            row['step']['_capture_window'] = deepcopy(origin)
    merged = original[:first] + fresh + original[last:]
    ids = [row['step']['id'] for row in merged]
    if len(ids) != len(set(ids)):
        raise ValueError('새 녹화의 단계 식별자가 중복됩니다. 다시 녹화하세요.')

    old_keys = [_replacement_key(row) for row in previous]
    new_keys = [_replacement_key(row) for row in fresh]
    linked = {}
    for old_index, key in enumerate(old_keys):
        corrected_single = (len(previous) == len(fresh) == 1 and key is not None and new_keys[0] is not None
                            and key[:2] == new_keys[0][:2] and key[2]['role'] == new_keys[0][2]['role']
                            and key[2].get('ancestors') == new_keys[0][2].get('ancestors')
                            and key[2].get('within') == new_keys[0][2].get('within')
                            and key[3:] == new_keys[0][3:])
        if key is None or old_keys.count(key) != 1 or (new_keys.count(key) != 1 and not corrected_single):
            continue
        source = previous[old_index]
        new_index = 0 if corrected_single else new_keys.index(key)
        replacement = fresh[new_index]
        old_id, new_id = source['step']['id'], replacement['step']['id']
        if key == new_keys[new_index]:
            linked[old_id] = new_id
        default_form = StepEdits().review_rows([replacement['step']])[0]['form']
        if replacement['form'] != default_form:
            continue
        # Re-recording can change a literal value. Carry intentional instructions
        # and required variables, leaving untouched generated text to the new take.
        old_default = StepEdits().review_rows([source['step']])[0]['form']
        if source['step']['action'] != 'saved-step' and source['form'] == old_default:
            continue
        candidate = replacement['step']
        try:
            if source['step']['action'] == 'saved-step':
                candidate = retain_saved_arguments(source['step'], candidate)
                if '_capture_window' in replacement['step']:
                    candidate['_capture_window'] = replacement['step']['_capture_window']
            form = source['form']
            StepEdits().compile(candidate, form['alias'], form['text'], form['mode'], form['argument'])
        except (ValueError, bridge.BridgeError):
            continue
        replacement['step'], replacement['form'] = candidate, deepcopy(form)
        linked[old_id] = new_id

    # A new take may demonstrate the child window and prove its actual opener,
    # even when the original opening action had no recognizable target.
    fresh_steps = [row['step'] for row in fresh]
    opened = {}
    for number, step in enumerate(fresh_steps, 1):
        scope = resolve_menu_scope(step.get('_capture_window', {}), fresh_steps, number)
        opener = scope.get('after_id')
        if scope.get('related') is True and opener in {s['id'] for s in fresh_steps[:number - 1]}:
            opened.setdefault(_window_key(step), set()).add(opener)

    prior_ids = []
    for row in merged:
        step = row['step']
        scope = step.get('_capture_window')
        if scope:
            references = [scope['after_id']] if 'after_id' in scope else list(scope.get('_openers', ()))
            if selected.intersection(references):
                scope = deepcopy(scope)
                proven = opened.get(_window_key(step), set())
                candidates = [linked.get(key, key) for key in references if key not in selected or key in linked]
                complete = len(candidates) == len(references)
                scope.pop('after_id', None)
                scope.pop('_openers', None)
                if len(proven) == 1 and next(iter(proven)) in prior_ids:
                    scope['after_id'] = next(iter(proven))
                elif complete and len(set(candidates)) == 1 and candidates[0] in prior_ids:
                    scope['after_id'] = candidates[0]
                else:
                    scope['_openers'] = tuple(key for key in prior_ids if not complete or key in candidates)
                    # The prior recording cannot prove which new action opened
                    # this window. Keep the existing explicit opener picker.
                    scope.pop('related', None)
                step['_capture_window'] = scope
        prior_ids.append(step['id'])
    return merged


def validate(doc):
    def require(condition):
        if not condition:
            raise tasks.TaskError('invalid_recording_draft')

    require(isinstance(doc, dict) and set(doc) == {
        'kind', 'schema', 'name', 'exe', 'window', 'rows', 'diagnostic', 'step_number'})
    require(doc['kind'] == KIND and type(doc['schema']) is int and doc['schema'] == 1)
    tasks._name(doc['name'])
    tasks._window(doc['exe'], doc['window'])
    rows = doc['rows']
    require(isinstance(rows, list) and 0 < len(rows) <= tasks.MAX_STEPS)
    index = doc['step_number']
    require(index is None or type(index) is int and 1 <= index <= len(rows))
    require(tasks._text(doc['diagnostic'], 200))
    ids = set()
    for row in rows:
        require(isinstance(row, dict) and set(row) == {'step', 'form'})
        step, form = row['step'], row['form']
        require(isinstance(step, dict) and tasks._text(step.get('id'), 200)
                and step['id'] not in ids and isinstance(step.get('label'), str))
        ids.add(step['id'])
        require(step.get('action') in ('manual', 'click', 'set-value', 'select', 'expand', 'teach-target', 'submit', 'press-key',
                                       'result-menu', 'row-action', 'visual-click', 'saved-step'))
        if step['action'] == 'saved-step':
            require(set(step) <= {'id', 'action', 'label', 'saved', '_capture_window'}
                    and isinstance(step.get('saved'), dict) and 'window_context' not in step['saved'])
            tasks._validate_step(step['saved'], set())
        elif step['action'] == 'result-menu':
            context = step.get('context')
            require(isinstance(context, dict) and {'trigger', 'menu', 'output'} <= context.keys()
                    and isinstance(context['menu'], dict) and isinstance(context['menu'].get('path'), list)
                    and all(isinstance(item, str) for item in context['menu']['path']))
        elif step['action'] in ('row-action', 'visual-click'):
            require(isinstance(step.get('row' if step['action'] == 'row-action' else 'visual'), dict))
        else:
            require(isinstance(step.get('target'), dict) and isinstance(step['target'].get('locator', {}), dict))
            ancestors = step['target'].get('locator', {}).get('ancestors', [])
            require(isinstance(ancestors, list) and all(isinstance(node, dict) for node in ancestors))
        require(isinstance(step.get('recognition', {}), dict))
        if 'row_cue' in step:
            from row_vision import validate_cue
            require(step['action'] == 'manual' and tasks.is_recoverable(step)
                    and step['recognition'].get('reason') == 'row_capture_requires_review')
            validate_cue(step['row_cue'])
        if 'ordinary_menu' in step:
            require(step['action'] == 'result-menu' and step['ordinary_menu'] is True)
        if 'menu_anchor' in step:
            require(step['action'] == 'manual')
            tasks._menu_anchor(step['menu_anchor'], step.get('recognition', {}).get('reason'))
        if 'menu_window_point' in step:
            require(step['action'] == 'manual')
            tasks._menu_window_point(step['menu_window_point'], step.get('recognition', {}).get('reason'))
        if step['action'] == 'set-value':
            require(isinstance(step.get('value'), str))
        if step['action'] == 'press-key':
            import key_chords
            require(bool(key_chords.virtual_keys(step.get('keys'))))
        if '_capture_window' in step:
            scope = step['_capture_window']
            require(isinstance(scope, dict))
            tasks._window(scope.get('exe'), scope.get('title'))
            if '_openers' in scope:
                require(isinstance(scope['_openers'], (list, tuple)) and
                        all(isinstance(key, str) for key in scope['_openers']))
            if 'after_id' in scope:
                require(isinstance(scope['after_id'], str))
        require(isinstance(form, dict) and set(form) == {'alias', 'text', 'mode', 'argument'})
        require(all(isinstance(form[key], str) for key in ('alias', 'text', 'argument'))
                and form['mode'] in (None, 'required', 'fixed'))
    return doc


def summary(doc):
    steps = []
    for number, row in enumerate(doc['rows'], 1):
        step = row['step']
        scope = step.get('_capture_window', {})
        item = dict(number=number, id=step['id'], label=step['label'],
                    window=scope.get('title', doc['window']), **row['form'])
        if location := tasks.row_location(step.get('saved', step)):
            item['row_location'] = location
        steps.append(item)
    return dict(name=doc['name'], exe=doc['exe'], window=doc['window'], steps=steps)


def receipt(doc, status):
    return dict(status=status, draft=True, diagnostic='recording_draft_requires_review',
                save_diagnostic=doc['diagnostic'], step_number=doc['step_number'], task=summary(doc),
                host_action='observe_and_recover_recorded_steps', recover_args=['recover', '--task', doc['name']],
                resume_args=['review', '--task', doc['name']],
                input_dispatched=False)


@contextmanager
def _edit_lock(directory):
    # Editors may review the same draft for minutes. Serialize only the on-disk
    # compare-and-replace. OS locks are released even if an editor exits abruptly.
    path = tasks._directory(directory) / '.recording-review.lock'
    tasks._deny_redirect(path)
    with path.open('a+b') as handle:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class ReviewStorage:
    def __init__(self, name, exe, title, directory, *, existing=None, source=None, new_only=False):
        self.name, self.exe, self.title, self.directory = name, exe, title, directory
        current = existing
        if current is None:
            try:
                current = tasks.read_document(name, directory)
            except tasks.TaskError as error:
                if error.code != 'task_not_found':
                    raise
        if current is not None:
            if new_only or source is not None or current.get('kind') != KIND:
                raise tasks.TaskError('task_already_exists')
            validate(current)
            if current['exe'].casefold() != exe.casefold() or current['window'] != title:
                raise tasks.TaskError('recorded_window_mismatch')
        self.previous = current
        self.source = source
        self.saved_plan = None

    def _write(self, document):
        if self.previous is None:
            return tasks._write_document(document, self.directory)
        try:
            with _edit_lock(self.directory):
                if tasks.read_document(self.name, self.directory) != self.previous:
                    raise tasks.TaskError('recording_draft_changed')
                return tasks._write_document(document, self.directory, replace_review=True)
        except OSError as error:
            raise tasks.TaskError('task_store_unavailable') from error

    def initial_rows(self):
        if self.source is not None:
            if self.source.get('kind') == KIND:
                return deepcopy(self.source['rows'])
            return rows_from_task(self.source)
        return deepcopy(self.previous['rows']) if self.previous is not None else None

    def save_draft(self, rows, diagnostic, step_number):
        document = validate(dict(kind=KIND, schema=1, name=self.name, exe=self.exe, window=self.title,
                                 rows=rows, diagnostic=diagnostic, step_number=step_number))
        self._write(document)
        return receipt(document, 'recorded')

    def save_recording(self, steps):
        plan = tasks.from_recording(self.name, self.exe, self.title, steps)
        if self.previous is None:
            tasks.save_task(plan, self.directory)
        else:
            tasks._validate(plan)
            self._write(plan)
        self.saved_plan = plan
        return dict(status='recorded', task=tasks.summary(plan), input_dispatched=False)
