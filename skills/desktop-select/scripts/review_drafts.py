"""Persist the recorder's editable form when it cannot yet be a replayable task."""
from contextlib import contextmanager
from copy import deepcopy
import os

import learned_tasks as tasks


KIND = 'recording-review'


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
        require(step.get('action') in ('manual', 'click', 'set-value', 'select', 'expand', 'teach-target',
                                       'result-menu', 'row-action', 'visual-click'))
        if step['action'] == 'result-menu':
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
        if 'ordinary_menu' in step:
            require(step['action'] == 'result-menu' and step['ordinary_menu'] is True)
        if 'menu_anchor' in step:
            require(step['action'] == 'manual')
            tasks._menu_anchor(step['menu_anchor'], step.get('recognition', {}).get('reason'))
        if step['action'] == 'set-value':
            require(isinstance(step.get('value'), str))
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
        steps.append(dict(number=number, id=step['id'], label=step['label'],
                          window=scope.get('title', doc['window']), **row['form']))
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
    def __init__(self, name, exe, title, directory, *, existing=None):
        self.name, self.exe, self.title, self.directory = name, exe, title, directory
        current = existing
        if current is None:
            try:
                current = tasks.read_document(name, directory)
            except tasks.TaskError as error:
                if error.code != 'task_not_found':
                    raise
        if current is not None:
            if current.get('kind') != KIND:
                raise tasks.TaskError('task_already_exists')
            validate(current)
            if current['exe'].casefold() != exe.casefold() or current['window'] != title:
                raise tasks.TaskError('recorded_window_mismatch')
        self.previous = current
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
