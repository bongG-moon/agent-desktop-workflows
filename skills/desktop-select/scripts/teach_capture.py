"""Observe one user's demonstration through the scoped Windows recorder."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
from pathlib import Path
import queue
import threading
import time
import unicodedata
import uuid

import win_bridge as bridge
from live_action import combo_readback
import learned_tasks


def _load_recorder():
    here = Path(__file__).resolve()
    path = here.with_name('recorder.py')
    if (not path.exists() and here.parents[2].name == 'skills'
            and (here.parents[3] / 'engine/bridge-manifest.json').is_file()):
        path = here.parents[3] / 'scripts/recorder.py'
    expected = 'c26af522a42426988a7fcefdfb4c48366164d7dccca7a5e88796ed6246654086'
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise bridge.BridgeError('recorder_incomplete')
    spec = importlib.util.spec_from_file_location('_desktop_select_recorder', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


recorder = _load_recorder()


def manual(reason, step_id=None):
    return {'id': step_id or uuid.uuid4().hex, 'label': '직접 확인할 단계',
            'action': 'manual', 'target': {'kind': 'manual', 'locator': {}},
            'recognition': {'action': 'manual', 'result': 'manual', 'reason': reason}}


def capture_step(step):
    """Copy only data used by a Desktop Select command. All other inputs are review stops."""
    target = step['target']
    locator = target.get('locator', {})
    action, role = step['action'], locator.get('role')
    pointed = (action == 'teach-target' and role == 'combobox' and 'window' not in target
               and step.get('recognition') == {'target': 'identified', 'action': 'requires_instruction',
                                               'result': 'manual', 'reason': 'combo_choice_unproven'})
    supported = (pointed or (action == 'set-value' and role == 'combobox' and bool(step.get('value')))
                 or (action == 'click' and role in {'menuitem', 'button', 'splitbutton', 'hyperlink'})
                 or (action == 'select' and role in {'tab', 'treeitem', 'listitem'})
                 or (action == 'expand' and role in {'treeitem', 'menuitem', 'splitbutton'}))
    if not supported or target.get('kind') != 'element':
        reason = step.get('recognition', {}).get('reason') if action == 'manual' else None
        return manual(reason or 'unsupported_recorded_action', step['id'])
    result = {'id': step['id'], 'action': action, 'label': (step.get('field_label')
              or locator.get('automation_id') or locator.get('name') or role),
              'target': {'kind': 'element', 'locator': copy.deepcopy(locator)},
              'recognition': copy.deepcopy(step['recognition'])}
    if 'window' in target:
        result['target']['window'] = copy.deepcopy(target['window'])
    if step.get('field_label'):
        result['field_label'] = step['field_label']
    if action == 'set-value':
        result['value'] = step['value']
        if 'selection' in step:
            result['selection'] = {k: step['selection'][k] for k in ('source', 'result', 'value')}
    return result


class CaptureNotes:
    """Keep final revisions in memory until the user finishes or cancels."""
    def __init__(self):
        self.steps = {}
        self.lock = threading.RLock()
        self.state = 'ready'
        self.partial = False
        self.limit = False
        self.closed = False
        self.hover_target = None

    def set_state(self, state, diagnostic=None, partial=False):
        with self.lock:
            self.state = state
            self.partial |= partial

    def add(self, step):
        with self.lock:
            if self.closed or self.limit:
                return
            if step['id'] not in self.steps and len(self.steps) >= recorder.MAX_STEPS - 1:
                self.limit = self.partial = True
                step = manual('recording_step_limit')
            self.steps[step['id']] = capture_step(step)

    def manual(self, label, reason):
        self.add(manual(reason))

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(list(self.steps.values()))


def default_text(step):
    command = learned_tasks.recorded_step(step)
    field = command.get('label') or command.get('target') or step['label']
    if step['action'] == 'teach-target':
        return ''
    if command['action'] == 'select':
        return f"{field} 값 {command['value']} 선택"
    if command['action'] == 'open':
        return field + (' 펼치기' if command.get('expand') else ' 선택')
    return ''


def instruction_view(step, instruction):
    if instruction and instruction['mode'] == 'fixed':
        return '지정한 값으로 선택: ' + instruction['value']
    if instruction and instruction['mode'] == 'required':
        return '실행 시 입력할 변수: ' + instruction['parameter']
    if step['action'] == 'teach-target':
        return '콤보 대상 확인됨. 고정값이나 입력 변수를 지정하세요.'
    return default_text(step) or '직접 수행하거나 다시 녹화할 단계'


class StepEdits:
    """Bind compiler-checked instructions to demonstrated identities across revisions."""
    def __init__(self):
        self.edits = {}

    @staticmethod
    def identity(step):
        return (step['action'], step['target'], step.get('recognition', {}).get('reason') if
                step['action'] == 'manual' else None)

    def compile(self, step, alias, sentence, mode=None, argument=None):
        alias = alias.strip()
        if (len(alias.encode('utf-8')) > 120 or any(unicodedata.category(c) == 'Cc' for c in alias)):
            raise ValueError('별칭은 120바이트 이내의 한 줄로 입력하세요.')
        if '=' in alias or alias.isdecimal():
            raise ValueError('별칭에는 등호 또는 숫자만으로 된 이름을 사용할 수 없습니다.')
        data = {'alias': alias} if alias else {}
        directive = learned_tasks.compile_instruction(dict(step, **data), sentence.strip(),
                                                       mode=mode, argument=argument)
        if directive is not None:
            data['instruction'] = directive
        return data

    def store(self, step, alias, sentence, mode=None, argument=None):
        data = self.compile(step, alias, sentence, mode, argument)
        self.edits[step['id']] = (self.identity(step), data, (sentence, mode, argument))

    def get(self, step):
        saved = self.edits.get(step['id'])
        return saved[1] if saved and saved[0] == self.identity(step) else {}

    def form(self, step):
        saved = self.edits.get(step['id'])
        return saved[2] if saved and saved[0] == self.identity(step) else ('', None, '')

    def apply(self, steps):
        result = []
        for step in steps:
            saved = self.get(step)
            if saved:
                sentence, mode, argument = self.form(step)
                saved = self.compile(step, saved.get('alias', ''), sentence, mode, argument)
            result.append(dict(step, **saved))
        return result


class _BoundWindow:
    def __init__(self, binding, title):
        self.binding, self.title = binding, title

    def __getattr__(self, key):
        return getattr(self.binding, key)

    def check(self):
        self.binding.check()
        if bridge.window_text(self.hwnd) != self.title:
            raise bridge.BridgeError('business_window_changed')


class _Worker(recorder.CaptureWorker):
    pointed = None

    def _drop_watch(self):
        self.pointed = None
        super()._drop_watch()

    def _emit_point(self):
        obs = self.watched
        if (not self.pointed or obs is not self.pointed or self.watched_step or self.watched_selection
                or obs['window'] != self.binding.hwnd or obs['node'].get('role') != 'combobox'
                or obs['node'].get('secure') is not False or obs['node'].get('enabled') is not True):
            return False
        self.binding.check()
        locator = obs['node'].get('locator')
        if self.uia.secure(obs['element']) or not recorder._locator_valid(locator):
            return False
        try:
            current = bridge._current_combo(self.binding, self.uia, locator)
        except bridge.BridgeError:
            return False
        if not self.uia.same(current['element'], obs['element']):
            return False
        step = self.step('teach-target', obs)
        step['recognition'] = {'target': 'identified', 'action': 'requires_instruction',
                               'result': 'manual', 'reason': 'combo_choice_unproven'}
        self.model.add(step)
        self._drop_watch()
        return True

    def _unobserved_combo(self, reason='combo_value_unobserved'):
        if reason in ('combo_value_unobserved', 'combo_choice_unproven', 'combo_option_click_required') and self._emit_point():
            return
        return super()._unobserved_combo(reason)

    def _manual_watch(self, reason):
        if reason in ('value_readback_unavailable', 'value_before_transition_unproven') and self._emit_point():
            return
        return super()._manual_watch(reason)

    def step(self, action, observation, kind='element', point=None):
        result = super().step(action, observation, kind, point)
        label = observation['node'].get('semantic_label')
        if label and observation['node'].get('secure') is False:
            result['field_label'] = label
        return result

    def watch(self, observation):
        if observation['node'].get('role') != 'combobox':
            self.freeze_watch()
            now = time.monotonic()
            if now - self.last_unreadable_input > .8:
                self.model.manual('', 'free_text_not_recorded')
            self.last_unreadable_input = now
            return
        return super().watch(observation)

    def _commit_value(self, current):
        # An unchanged or unreadable collapsed value after a mere field click
        # cannot teach a selection. A witnessed option click retains its intent.
        if self.watched_selection is None and not self.watched_step:
            if not current or current == self.watched_value:
                return self._unobserved_combo('combo_choice_unproven')
            observed = combo_readback(self.binding, self.uia, self.watched['element'])
            if observed.get('value') != current or observed.get('value_evidence', {}).get('status') != 'observed':
                return self._unobserved_combo('combo_choice_unproven')
        return super()._commit_value(current)

    def _command_owner(self, event, observation):
        # Providers may return a passive caption or icon inside the actual
        # button. Only promote through the same on-screen ancestor chain.
        node = observation['node']
        if (observation.get('combo') is not None or event['action'] != 'click' or node.get('role') not in {'statictext', 'image'}
                or node.get('secure') is not False or set(node.get('available_actions', ())) - {'SetFocus'}):
            return observation
        item, root = observation['element'], observation['root']
        for _ in range(5):
            item = self.uia.parent(item)
            if item is None or self.uia.same(item, root) or self.uia.pid(item) != self.binding.pid or self.uia.secure(item):
                break
            role = bridge.ROLES.get(item.property(30003))
            if role in {'button', 'splitbutton', 'menuitem', 'treeitem', 'tab', 'hyperlink'}:
                if (item.property(30010) is not True or item.property(30022) is not False
                        or not self.uia.within(item, root)
                        or not recorder._inside(self.uia.bounds(item), event.get('x'), event.get('y'))):
                    break
                return dict(observation, node=self.uia.node(item, root), element=item, combo=None)
            if role not in {'statictext', 'image', 'group'} or set(self.uia.actions(item)) - {'SetFocus'}:
                break
        return observation

    def capture_action(self, event, observation):
        pointed = None
        if observation:
            observation = self._command_owner(event, observation)
            node = observation['node']
            if node.get('secure') is not False:
                self.freeze_watch(event.get('at'))
                self.model.manual('', 'protected_input_not_recorded')
                return
            if (event['action'] == 'click' and node.get('role') in {'menuitem', 'treeitem', 'splitbutton'}
                    and 'Expand' in node.get('available_actions', []) and 'collapsed' in node.get('states', [])):
                self.freeze_watch(event.get('at'))
                if observation['element'].property(30070) == 1:
                    step = self.step('expand', observation)
                    self.model.add(step)
                    return step['id']
                self.model.manual('', 'expansion_not_observed')
                return
            if event['action'] == 'click' and node.get('role') != 'listitem':
                pointed = self.combo_for(observation)
        step = super().capture_action(event, observation)
        if pointed and self.watched and self.uia.same(pointed['element'], self.watched['element']):
            self.pointed = self.watched
        return step


class CaptureSession:
    def __init__(self, binding, title):
        self.binding = _BoundWindow(binding, title)
        self.notes = CaptureNotes()
        self.events, self.notices = queue.Queue(maxsize=128), queue.Queue()
        self.worker = _Worker(self.binding, bridge, self.notes, self.events, self.notices)
        self.pump = recorder.HookPump(self.binding, bridge, self.events, self.notices)
        self.started = self.ended = False

    def start(self):
        if self.started or self.ended:
            raise bridge.BridgeError('recording_already_started')
        self.binding.check()
        self.worker.start()
        try:
            if not self.worker.ready.wait(3) or self.worker.error:
                raise bridge.BridgeError('accessibility_worker_unavailable')
            self.worker.enabled.set()
            self.pump.start()
        except BaseException:
            self.cancel()
            raise
        self.notes.set_state('recording')
        self.started = True

    def poll(self):
        """A hook/worker failure must become a visible stop in this demonstration."""
        while True:
            try:
                kind, reason = self.notices.get_nowait()
            except queue.Empty:
                break
            if kind in ('fatal', 'pause'):
                self.pump.stop()
                self.worker.enabled.clear()
                self.notes.set_state('paused', partial=True)
                if kind == 'fatal':
                    self.notes.manual('', reason or 'capture_interrupted')
        if self.started and not self.ended and self.pump.closed.is_set() and self.notes.state != 'paused':
            self.worker.enabled.clear()
            self.notes.manual('', 'hook_stopped')
            self.notes.set_state('paused', partial=True)
        if self.notes.limit:
            self.worker.enabled.clear()
            self.pump.stop()

    def finish(self):
        if not self.started or self.ended:
            raise bridge.BridgeError('recording_not_active')
        self.poll()
        self.pump.stop()
        self.worker.enabled.clear()
        self.ended = True
        try:
            try:
                self.events.put({'kind': 'finish'}, timeout=.5)
            except queue.Full:
                self.notes.manual('', 'recording_queue_full')
            if not self.worker.finished.wait(3):
                self.notes.manual('', 'pending_capture_requires_review')
            self.poll()
            with self.notes.lock:
                if not self.notes.steps:
                    self.notes.manual('', 'empty_recording_requires_review')
                self.notes.closed = True
                return {'status': 'recorded', 'steps': self.notes.snapshot(), 'input_dispatched': False}
        finally:
            self.worker.abort.set()

    def cancel(self):
        self.ended = True
        self.pump.stop()
        self.worker.enabled.clear()
        self.worker.abort.set()
        with self.notes.lock:
            self.notes.closed = True
            self.notes.steps.clear()
            self.notes.hover_target = None
        return {'status': 'cancelled', 'steps': [], 'input_dispatched': False}


def capture(binding, *, title):
    """Block while the user records from a small, local start/finish window."""
    import tkinter as tk
    from tkinter import ttk
    session = CaptureSession(binding, title)
    session.binding.check()
    root = tk.Tk()
    root.title('Desktop Select 녹화')
    result = None
    review = None
    editing = None
    edits = StepEdits()
    message = tk.StringVar(value='시작을 누르고 업무 창에서 작업하세요. 끝나면 이 창으로 돌아오세요.')
    frame = ttk.Frame(root, padding=14)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text=title, wraplength=460).pack(anchor='w')
    ttk.Label(frame, textvariable=message, wraplength=460, padding=(0, 8)).pack(anchor='w')
    table = ttk.Frame(frame)
    table.pack(fill='both', expand=True)
    listing = ttk.Treeview(table, name='steps', columns=('number', 'target', 'alias', 'instruction'),
                           show='headings', selectmode='browse', height=7)
    for key, heading, width in [('number', '단계', 40), ('target', '녹화한 대상', 150),
                                ('alias', '별칭', 105), ('instruction', '실행할 동작', 290)]:
        listing.heading(key, text=heading)
        listing.column(key, width=width, minwidth=32, stretch=key in ('target', 'instruction'))
    scroll = ttk.Scrollbar(table, command=listing.yview)
    listing.configure(yscrollcommand=scroll.set)
    scroll.pack(side='right', fill='y')
    listing.pack(side='left', fill='both', expand=True)
    form = ttk.Frame(frame, padding=(0, 6))
    form.pack(fill='x')
    form.columnconfigure(1, weight=1)
    alias, instruction, argument = tk.StringVar(), tk.StringVar(), tk.StringVar()
    mode = tk.StringVar(value='문장 해석')
    modes = {'문장 해석': None, '고정값': 'fixed', '필수 변수': 'required'}
    for row, (key, label, var) in enumerate((('alias', '단계 별칭', alias), ('instruction', '실행할 동작', instruction))):
        ttk.Label(form, text=label).grid(row=row, column=0, padx=(0, 6), sticky='w')
        ttk.Entry(form, name=key, textvariable=var, width=42).grid(row=row, column=1, sticky='ew')
    ttk.Label(form, text='콤보 값 지정').grid(row=2, column=0, padx=(0, 6), sticky='w')
    choices = ttk.Frame(form)
    choices.grid(row=2, column=1, sticky='w')
    ttk.Combobox(choices, name='mode', values=list(modes), state='readonly', textvariable=mode,
                 width=11).pack(side='left')
    arg_label = ttk.Label(choices, text='값 또는 변수 이름')
    arg_label.pack(side='left', padx=(6, 2))
    arg_box = ttk.Entry(choices, name='argument', textvariable=argument, width=22)
    arg_box.pack(side='left')
    preview = tk.StringVar()
    ttk.Label(form, name='preview', textvariable=preview, wraplength=560).grid(
        row=3, column=0, columnspan=3, sticky='w', pady=(4, 0))
    controls = ttk.Frame(frame)
    controls.pack(anchor='w')

    def steps_now():
        return review['steps'] if review is not None else session.notes.snapshot()

    def fields():
        chosen = modes[mode.get()]
        return alias.get(), instruction.get(), chosen, argument.get() if chosen else None

    def show_error(exc):
        if isinstance(exc, learned_tasks.TaskError):
            reason = {'instruction_target_mismatch': '입력한 문장의 대상이 녹화한 요소와 다릅니다.',
                      'instruction_requires_structure': '콤보 값 지정에서 고정값이나 필수 변수를 선택하고 값을 입력하세요.',
                      'invalid_step_alias': '다른 별칭을 입력하세요.'}.get(
                          exc.code, '이 단계에서 실행할 수 있는 문장과 값을 입력하세요.')
            preview.set(reason + ' (' + exc.code + ')')
        else:
            preview.set(str(exc))

    def refresh_preview(*_):
        arg_box.configure(state='disabled' if modes[mode.get()] is None else 'normal')
        arg_label.configure(text='변수 이름' if modes[mode.get()] == 'required' else '값')
        if editing is not None:
            try:
                data = edits.compile(editing, *fields())
                preview.set('실행 미리보기: ' + instruction_view(editing, data.get('instruction')))
            except ValueError as exc:
                show_error(exc)

    def flush():
        if editing is not None:
            try:
                edits.store(editing, *fields())
            except ValueError as exc:
                show_error(exc)
                return False
        return True

    def load(step):
        sentence, chosen, arg = edits.form(step)
        alias.set(edits.get(step).get('alias', ''))
        instruction.set(sentence)
        mode.set(next(label for label, value in modes.items() if value == chosen))
        argument.set(arg or '')
        refresh_preview()

    def select_row(*_):
        nonlocal editing
        selected = listing.selection()
        if not selected or (editing and editing['id'] == selected[0]):
            return
        if not flush():
            listing.selection_set(editing['id'])
            return
        editing = next((step for step in steps_now() if step['id'] == selected[0]), None)
        if editing is not None:
            load(editing)

    def render(steps):
        nonlocal editing
        for i, step in enumerate(steps, 1):
            saved = edits.get(step)
            if editing is not None and step['id'] == editing['id'] and editing != step:
                previous = editing
                editing = step
                if edits.identity(step) != edits.identity(previous):
                    load(step)
                else:
                    refresh_preview()
            directive = saved.get('instruction')
            text = directive.get('text') if directive else default_text(step)
            text = text or instruction_view(step, directive)
            row = (i, step['label'], saved.get('alias', ''), text)
            if listing.exists(step['id']):
                listing.item(step['id'], values=row)
            else:
                listing.insert('', 'end', iid=step['id'], values=row)
        if steps and not listing.selection():
            listing.selection_set(steps[0]['id'])

    def apply_text():
        if flush():
            render(steps_now())
            refresh_preview()

    listing.bind('<<TreeviewSelect>>', select_row)
    for variable in (alias, instruction, mode, argument):
        variable.trace_add('write', refresh_preview)
    refresh_preview()
    ttk.Button(form, text='동작 적용', command=apply_text).grid(row=0, column=2, rowspan=2, padx=6)

    def start(*_):
        nonlocal result
        if session.started:
            return
        try:
            session.start()
        except Exception as exc:
            diagnostic = exc.code if isinstance(exc, bridge.BridgeError) else 'recording_start_failed'
            result = {'status': 'blocked', 'diagnostic': diagnostic, 'input_dispatched': False}
            root.destroy()
            return
        begin.configure(state='disabled')
        end.configure(state='normal')

    def finish(*_):
        nonlocal review
        if not session.started or review is not None:
            return
        review = session.finish()
        render(review['steps'])
        end.configure(state='disabled')
        save.configure(state='normal')
        message.set('단계를 선택하고 실행할 동작을 입력하세요. 예: 이 항목을 N으로 설정')

    def save_review():
        nonlocal result
        if review is None or not flush():
            return
        try:
            final_steps = edits.apply(review['steps'])
        except ValueError as exc:
            show_error(exc)
            return
        result = dict(review, steps=final_steps)
        root.destroy()

    def cancel(*_):
        nonlocal result
        result = session.cancel()
        root.destroy()

    def tick():
        if session.started and review is None:
            session.poll()
            steps = session.notes.snapshot()
            render(steps)
            warnings = sum((s['action'] == 'teach-target' and not edits.get(s).get('instruction'))
                           or s['action'] == 'manual' or s['recognition'].get('result') == 'mismatch' for s in steps)
            state = '녹화가 멈췄습니다. 마치고 검토하세요' if session.notes.state == 'paused' or session.notes.limit else '녹화 중'
            message.set(f'{state} / {len(steps)}단계 / 확인 필요 {warnings}개'
                        + ('\n최근 단계: ' + steps[-1]['label'] if steps else ''))
        root.after(200, tick)

    begin = ttk.Button(controls, text='시작', command=start)
    end = ttk.Button(controls, text='마치고 검토', command=finish, state='disabled')
    save = ttk.Button(controls, text='저장', command=save_review, state='disabled')
    for button in (begin, end, save, ttk.Button(controls, text='취소', command=cancel)):
        button.pack(side='left', padx=3)
    root.bind('<Alt-s>', start)
    root.bind('<Alt-f>', finish)
    root.protocol('WM_DELETE_WINDOW', cancel)
    def failed(*_):
        nonlocal result
        result = {'status': 'blocked', 'diagnostic': 'recording_ui_failed', 'input_dispatched': False}
        root.destroy()
    root.report_callback_exception = failed
    root.after(200, tick)
    try:
        root.mainloop()
        return result or {'status': 'cancelled', 'steps': [], 'input_dispatched': False}
    finally:
        if not session.ended:
            session.cancel()
