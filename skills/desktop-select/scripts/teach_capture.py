"""Observe one user's demonstration through the scoped Windows recorder."""
from __future__ import annotations

import copy
import importlib.util
import json
import queue
import threading
import time
import unicodedata
import uuid

import win_bridge as bridge
import key_chords
from live_action import combo_readback
import live_action
import learned_tasks
from capture_trail import CaptureTrail, finalize_contexts
from runtime_integrity import checked_path


def _load_recorder():
    path = checked_path('recorder.py')
    spec = importlib.util.spec_from_file_location('_desktop_select_recorder', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


recorder = _load_recorder()


def manual(reason, step_id=None):
    label = '일반 녹화에서 감지한 우클릭' if reason == 'right_click_requires_menu_teaching' else '직접 확인할 단계'
    step = {'id': step_id or uuid.uuid4().hex, 'label': label,
            'action': 'manual', 'target': {'kind': 'manual', 'locator': {}},
            'recognition': {'action': 'manual', 'result': 'manual', 'reason': reason}}
    if learned_tasks.is_recoverable(step):
        step['label'] = '설명으로 다시 찾을 단계'
    return step


def _capture_locator_valid(locator):
    if recorder._locator_valid(locator):
        return True
    if not isinstance(locator, dict) or 'anchor' not in locator:
        return False
    try:
        import control_lookup
        control_lookup._selector(locator)
        return locator.get('role') == 'textfield'
    except bridge.BridgeError:
        return False


def capture_step(step):
    """Copy only data used by a Desktop Select command. All other inputs are review stops."""
    if step['action'] == 'visual-click':
        from visual_target import validate_spec
        result = dict(id=step['id'], action='visual-click', label=step['label'],
                      visual=copy.deepcopy(validate_spec(step['visual'])))
        if surface := learned_tasks.result_surface_hint(step):
            result['result_surface'] = surface
        return result
    target = step['target']
    locator = target.get('locator', {})
    action, role = step['action'], locator.get('role')
    if role == 'textfield':
        chain = [step, target, locator, *locator.get('ancestors', [])]
        if any(isinstance(item, dict) and (item.get('role') == 'password' or any(item.get(key) is True
               for key in ('secure', 'protected', 'is_password', 'password'))) for item in chain):
            return manual('protected_input_not_recorded', step['id'])
        if any(isinstance(item, dict) and item.get('role') == 'combobox' for item in locator.get('ancestors', [])):
            return manual('textfield_requires_combo', step['id'])
    # A known field with an unreadable value still teaches its location. Its
    # intended value is supplied later by the user's instruction, not guessed.
    if (action == 'manual' and role in {'combobox', 'textfield'} and target.get('kind') == 'element'
            and 'window' not in target and recorder._locator_valid(locator)
            and step.get('recognition', {}).get('target') == 'identified'
            and step.get('recognition', {}).get('reason') in {
                'combo_value_unobserved', 'combo_choice_unproven', 'combo_option_click_required',
                'value_readback_unavailable', 'value_before_transition_unproven', 'text_value_unobserved'}):
        step = dict(step, action='teach-target', recognition={'target': 'identified',
                    'action': 'requires_instruction', 'result': 'manual',
                    'reason': 'combo_choice_unproven' if role == 'combobox' else 'text_value_unobserved'})
        action = 'teach-target'
    pointed = (action == 'teach-target' and role in {'combobox', 'textfield'} and 'window' not in target
               and step.get('recognition') == {'target': 'identified', 'action': 'requires_instruction',
                    'result': 'manual', 'reason': 'combo_choice_unproven' if role == 'combobox' else 'text_value_unobserved'})
    supported = (pointed or (action == 'set-value' and role == 'combobox' and bool(step.get('value')))
                 or (action == 'set-value' and role == 'textfield' and isinstance(step.get('value'), str))
                 or (action == 'submit' and role == 'textfield')
                 or (action == 'press-key' and role in key_chords.TARGET_ROLES
                     and _capture_locator_valid(locator) and key_chords.virtual_keys(step.get('keys')))
                 or (action == 'click' and role in {'menuitem', 'button', 'splitbutton', 'hyperlink'})
                 or (action == 'select' and role in {'tab', 'treeitem', 'listitem'})
                 or (action == 'expand' and role in {'treeitem', 'menuitem', 'splitbutton'}))
    if not supported or target.get('kind') != 'element':
        reason = step.get('recognition', {}).get('reason') if action == 'manual' else None
        if action == 'right-click':
            reason = 'right_click_requires_menu_teaching'
        unknown = manual(reason or 'unsupported_recorded_action', step['id'])
        if reason == 'keyboard_transition_needs_review' and key_chords.virtual_keys(step.get('keys')):
            unknown['description'] = step['keys'] + ' 키 누르기'
        # The click is only a hint. Preserve its containing result surface
        # without saving the clicked cell's business value or authorizing input.
        if (action in ('click', 'select', 'manual') and target.get('kind') in ('element', 'region')
                and 'window' not in target and learned_tasks.is_recoverable(unknown)):
            chain = [locator, *locator.get('ancestors', [])]
            protected = any(isinstance(node, dict) and (node.get('role') == 'password' or any(
                node.get(key) is True for key in ('secure', 'protected', 'is_password', 'password', 'is_protected')))
                for node in [step, target, *chain])
            if protected:
                return manual('protected_input_not_recorded', step['id'])
            if role in {'textfield', 'combobox', 'statictext'} and _capture_locator_valid(locator):
                # Clicking a field can demonstrate where to read, without
                # changing its value. Keep that identity as a hint only.
                unknown['target'] = dict(kind='element', locator=copy.deepcopy(locator))
                if step.get('field_label'):
                    unknown['field_label'] = step['field_label']
                    unknown['label'] = step['field_label']
            elif surface := learned_tasks.recorded_result_surface(locator):
                unknown['target'] = dict(kind='element', locator=surface)
        return unknown
    result = {'id': step['id'], 'action': action, 'label': (step.get('field_label')
              or locator.get('automation_id') or locator.get('name') or role),
              'target': {'kind': 'element', 'locator': copy.deepcopy(locator)},
              'recognition': copy.deepcopy(step['recognition'])}
    if 'window' in target:
        result['target']['window'] = copy.deepcopy(target['window'])
        if role == 'menuitem' and learned_tasks._text(locator.get('name'), 200):
            # A transient branch may disappear before the route is complete.
            # Keep the witnessed caption for the host's later live inspection.
            result['description'] = locator['name'].strip()
    if step.get('field_label'):
        result['field_label'] = step['field_label']
    if action == 'press-key':
        result['keys'] = step['keys']
    if action == 'set-value':
        result['value'] = step['value']
        if 'selection' in step:
            result['selection'] = {k: step['selection'][k] for k in ('source', 'result', 'value')}
    return result


class CaptureNotes:
    """Keep final revisions in memory until the user finishes or cancels."""
    def __init__(self):
        self.steps = {}
        self.times = {}
        self.event_at = None
        self.completed_double_down = 0
        self.row_fallbacks = {}
        self.lock = threading.RLock()
        self.state = 'ready'
        self.partial = False
        self.limit = False
        self.max_steps = recorder.MAX_STEPS
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
            if step['id'] not in self.steps and len(self.steps) >= self.max_steps - 1:
                self.limit = self.partial = True
                step = manual('recording_step_limit')
            self.times.setdefault(step['id'], self.event_at or time.monotonic())
            self.steps[step['id']] = capture_step(step)

    def manual(self, label, reason):
        self.add(manual(reason))

    def context_start(self, step_id, surface, window_point=None):
        with self.lock:
            step = manual('right_click_requires_menu_teaching', step_id)
            self.add(step)
            if step_id in self.steps:
                if surface:
                    self.steps[step_id]['menu_anchor'] = surface
                elif window_point is not None:
                    self.steps[step_id]['menu_window_point'] = window_point

    def context_complete(self, step):
        with self.lock:
            previous = self.steps.get(step['id'], {})
            if (not self.closed and previous.get('menu_anchor')
                    and previous.get('recognition', {}).get('reason') == 'right_click_requires_menu_teaching'):
                self.steps[step['id']] = step

    def row_step(self, step_id, number, spec, selection=None, *, cue=None, press=None):
        """Keep the click's original order while the observed row state settles."""
        with self.lock:
            if self.closed or self.limit:
                return
            if step_id not in self.steps and len(self.steps) >= self.max_steps - 1:
                self.add(manual('recording_step_limit'))
                return
            # A queued double click or deleted/invalidated event owns its ID.
            existing = self.steps.get(step_id)
            if existing and existing != self.row_fallbacks.get(step_id) and existing.get('action') != 'row-action' and (
                    existing.get('recognition', {}).get('reason') != 'row_capture_requires_review'):
                return
            label = f'{number}번째 행 버튼' if number else '선택한 행 버튼'
            if existing is None:
                if selection is not None:
                    fallback = capture_step(selection)
                elif press is not None:
                    fallback = dict(id=step_id, action='row-action', row=press.click_spec(), label=label + ' 누르기')
                else:
                    fallback = dict(manual('row_capture_requires_review', step_id), label=label)
                    if cue is not None:
                        fallback['row_cue'] = cue
                self.row_fallbacks[step_id] = fallback
            self.times.setdefault(step_id, self.event_at or time.monotonic())
            if spec is None:
                self.steps[step_id] = self.row_fallbacks[step_id]
            else:
                self.steps[step_id] = dict(id=step_id, action='row-action', row=spec, label=label + ' 누르기')
            if self.steps[step_id]['action'] == 'row-action':
                self.steps[step_id]['_ordinary_row_press'] = True

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(list(self.steps.values()))


def default_text(step):
    if step['action'] == 'saved-step':
        from review_drafts import saved_form
        return saved_form(step['saved'])['text']
    if step['action'] in ('row-action', 'result-menu', 'visual-click'):
        return step['label']
    if step.get('recognition', {}).get('reason') == 'double_click_needs_review':
        return '녹화한 항목 더블클릭'
    if step.get('recognition', {}).get('reason') == 'keyboard_transition_needs_review':
        return step.get('description', '')
    if (step.get('recognition', {}).get('reason') == 'row_capture_requires_review'
            and step.get('label', '').endswith('번째 행 버튼')):
        return step['label'] + '을 눌러줘'
    try:
        command = learned_tasks.recorded_step(step)
    except (ValueError, bridge.BridgeError):
        # An incomplete location can still be reviewed and described.
        return ''
    field = (step.get('alias') or command.get('label') or command.get('command_name')
             or command.get('target') or step['label'])
    if step['action'] == 'teach-target':
        field = step.get('alias') or step.get('field_label') or step['label']
        kind = '입력란' if step['target'].get('locator', {}).get('role') == 'textfield' else '조건'
        return f'입력받은 변수를 {field} {kind}에 입력해줘'
    if command['action'] == 'select':
        return f"{field} 조건을 {command['value']}로 설정해줘"
    if command['action'] == 'fill':
        return f"{field} 입력란을 {json.dumps(command['value'], ensure_ascii=False)}로 설정해줘"
    if command['action'] == 'field-input':
        return f"이 입력란을 {json.dumps(command.get('value', ''), ensure_ascii=False)}로 설정해줘"
    if command['action'] == 'submit':
        return '이 입력란에서 Enter를 눌러 적용해줘'
    if command['action'] == 'press-key':
        return command['keys'] + ' 키 누르기'
    if command['action'] == 'choose-item':
        return f"{command['selector'].get('name', field)} 항목을 선택해줘"
    if command['action'] == 'open':
        kind = '버튼' if command.get('role') in {'button', 'splitbutton'} else '항목'
        verb = '펼쳐줘' if command.get('expand') else '눌러줘'
        return f'{field} {kind}을 {verb}'
    return ''


def review_interpretation(step, data, *, compiled=None):
    """Describe exactly what the current editor form would save."""
    command = compiled if compiled is not None else learned_tasks.recorded_step(StepEdits.edited_step(step, data))
    action = command['action']
    instruction = command.get('instruction') or {}
    field = action in ('select', 'fill', 'field-input')
    if action == 'manual' and learned_tasks._point_target(step):
        field = learned_tasks._convert(step, allow_point=True)['action'] in learned_tasks.VALUE_ACTIONS
    if command.get('instruction_draft') or data.get('instruction', {}).get('mode') == 'draft':
        return '실행 전 설명 확인', None, '', field
    if field:
        binding = instruction if instruction.get('mode') in ('fixed', 'required') else command
        if 'parameter' in binding:
            label = '실행할 때 입력 · ' + binding['parameter']
            if 'value' in binding:
                label += ' (녹화값 ' + binding['value'] + ')'
            return label, 'required', binding['parameter'], True
        if 'value' in binding:
            value = binding['value']
            return ('고정값 ' + value if value else '고정값 · 입력란 비우기'), 'fixed', value, True
        return '값 지정', None, '', True
    if action == 'manual':
        label = {'double_click_needs_review': '더블클릭 대상 확인 필요',
                 'right_click_requires_menu_teaching': '우클릭 위치 확인 필요'}.get(command.get('reason'), '실행 전 대상 확인')
    elif action == 'visual-click':
        label = '더블클릭' if command['visual'].get('gesture') == 'double-click' else '클릭'
    elif action == 'result-menu':
        context = command['context']
        route = (['조회 완료 확인'] if context['trigger'] else []) + ['표 우클릭', *context['menu']['path']]
        if context['output']:
            route.append('Excel 파일 확인')
        label = ' → '.join(route)
    elif action == 'open':
        label = '펼치기' if command.get('expand') else '클릭'
    elif action in learned_tasks.OBSERVE_ACTIONS:
        label = ('값 읽기 · ' if action == 'read-value' else '값 일치 확인 · ') + command['label']
    elif action == 'close-popup':
        label = command['label'] + ' 클릭 후 창 닫힘 확인'
    elif action == 'row-launch':
        label = f"{command['ordinal']}번째 행 실행"
    elif action == 'press-key':
        label = command['keys'] + ' 키 누르기'
    else:
        label = {'submit': 'Enter 입력', 'choose-item': '항목 선택', 'row-action': '행 버튼 실행',
                 'row-open': '행 열기', 'conditional-open': '조건에 따라 열기'}.get(action, step['label'])
    return label, None, '', False


class StepEdits:
    """Bind compiler-checked instructions to demonstrated identities across revisions."""
    def __init__(self):
        self.edits = {}
        self.drafts = {}
        self.failed_step = None

    @staticmethod
    def identity(step):
        if step['action'] == 'saved-step':
            return 'saved-step', step['saved']
        if step.get('ordinary_menu') or step.get('menu_anchor'):
            anchor = step.get('menu_anchor') or step['context']['menu']
            return 'observed_context_surface', anchor['target'], anchor['point']
        if step['action'] == 'visual-click':
            return step['action'], step['visual']
        if (step.get('_ordinary_row_press') or step.get('recognition', {}).get('reason') == 'row_capture_requires_review'):
            # The same press may be upgraded from position to button or state
            # evidence while the user types. Its event ID stays constant.
            return 'recorded_row_press', step['id']
        if step['action'] == 'row-action':
            return step['action'], step['row'], step['label']
        if step['action'] == 'result-menu':
            return step['action'], step['context'], step['label']
        target = step['target']
        locator = target.get('locator', {})
        identity = learned_tasks._selector(locator)
        if identity is None:
            if learned_tasks.is_recoverable(step):
                # The same recorded event can acquire a different recognition
                # diagnostic while its provider settles. The prose is its
                # identity until execution resolves a live target.
                return 'description_target',
            return step['action'], target, step.get('recognition', {}).get('reason')
        # Runtime handles, screen coordinates and duplicated peer names vary
        # while one clicked field settles. They are not the recorded identity.
        action = identity['role'] if identity['role'] in {'combobox', 'textfield'} else step['action']
        return (action, identity, [learned_tasks._selector(p) for p in locator.get('ancestors', [])],
                target.get('window'))

    def compile(self, step, alias, sentence, mode=None, argument=None):
        alias = alias.strip()
        if mode is None:
            argument = None
        if (len(alias.encode('utf-8')) > 120 or any(unicodedata.category(c) == 'Cc' for c in alias)):
            raise ValueError('별칭은 120바이트 이내의 한 줄로 입력하세요.')
        if '=' in alias or alias.isdecimal():
            raise ValueError('별칭에는 등호 또는 숫자만으로 된 이름을 사용할 수 없습니다.')
        data = {'alias': alias} if alias else {}
        if step['action'] == 'saved-step':
            from review_drafts import saved_form
            saved = dict(step['saved'])
            original = saved_form(saved)
            saved.pop('alias', None)
            saved.update(data)
            if (sentence.strip(), mode, argument or '') != (original['text'], original['mode'], original['argument']):
                source = dict(saved, _capture_window=step['_capture_window']) if '_capture_window' in step else saved
                directive = learned_tasks.compile_instruction(source, sentence, mode=mode, argument=argument)
                if directive is None:
                    raise learned_tasks.TaskError('instruction_requires_structure')
                scoped_intent = directive['mode'] in (*learned_tasks.OBSERVE_ACTIONS, 'close-popup')
                saved = learned_tasks.instructed_step(source if scoped_intent else saved, directive)
            if 'instruction' in saved:
                data['instruction'] = saved['instruction']
            return dict(data, saved=saved)
        if (mode is None and step.get('recognition', {}).get('reason') == 'right_click_requires_menu_teaching'
                and learned_tasks.click_menu_path(sentence)):
            # Preserve the route as a note until recovery checks the region.
            note = dict(mode='draft', text=sentence.strip())
            learned_tasks.instructed_step(step, note)
            return dict(data, instruction=note)
        if step.get('ordinary_menu') and mode is None:
            text = sentence.strip()
            if text and text != step['label']:
                if not learned_tasks._text(text, 1000) or len(text.encode('utf-8')) > 1000:
                    raise learned_tasks.TaskError('invalid_step_instruction')
                data['instruction'] = dict(mode='draft', text=text)
            return data
        if step['action'] in ('row-action', 'result-menu', 'visual-click') and sentence.strip() == step['label'] and mode is None:
            return data
        # A recorded literal can contain punctuation outside the sentence
        # parser. Persist our own generated draft as literal data, never code.
        if (mode is None and argument is None and step['action'] == 'set-value'
                and sentence.strip() == default_text(step)):
            mode, argument = 'fixed', step['value']
        directive = learned_tasks.compile_instruction(dict(step, **data), sentence.strip(),
                                                       mode=mode, argument=argument)
        if directive is not None:
            data['instruction'] = directive
        return data

    def value_sentence(self, step, alias, sentence, mode, argument):
        """Keep the user's words unless their deliberate value edit contradicts those words."""
        if mode is None:
            return sentence
        if sentence.strip():
            try:
                self.compile(step, alias, sentence, mode, argument)
                return sentence
            except learned_tasks.TaskError as error:
                if error.code != 'instruction_binding_conflict':
                    raise
        fields = self.compile(step, alias, sentence) if sentence.strip() else {}
        command = learned_tasks.recorded_step(self.edited_step(step, fields))
        field = command.get('label') or command.get('target') or step.get('field_label') or step['label']
        if command.get('match') == 'query':
            field = command['target']
        role = command.get('role')
        if command['action'] == 'field-input' or role == 'textfield':
            subject = field + ' 입력란'
        else:
            subject = field + ' 조건'
        if mode == 'required':
            updated = f'입력받은 {{{argument}}} 변수를 {subject}에 입력해줘'
        else:
            updated = f'{subject}을 {json.dumps(argument, ensure_ascii=False)}로 설정해줘'
        self.compile(step, alias, updated, mode, argument)
        return updated

    def store(self, step, alias, sentence, mode=None, argument=None):
        self.drafts[step['id']] = (self.identity(step), (alias, sentence, mode, argument))
        data = self.compile(step, alias, sentence, mode, argument)
        self.edits[step['id']] = (self.identity(step), data, (sentence, mode, argument))

    def get(self, step):
        saved = self.edits.get(step['id'])
        return saved[1] if saved and saved[0] == self.identity(step) else {}

    def form(self, step):
        draft = self.drafts.get(step['id'])
        if draft and draft[0] == self.identity(step):
            return draft[1][1:]
        saved = self.edits.get(step['id'])
        return saved[2] if saved and saved[0] == self.identity(step) else (default_text(step), None, '')

    def forget(self, step_id):
        self.edits.pop(step_id, None)
        self.drafts.pop(step_id, None)

    def alias(self, step):
        draft = self.drafts.get(step['id'])
        return draft[1][0] if draft and draft[0] == self.identity(step) else self.get(step).get('alias', '')

    def review_rows(self, steps):
        rows = []
        for step in steps:
            text, mode, argument = self.form(step)
            form = dict(alias=self.alias(step), text=text, mode=mode, argument=argument or '')
            rows.append({'step': step, 'form': form})
        return rows

    def restore_rows(self, rows):
        steps = []
        scopes = {}
        for row in rows:
            step = copy.deepcopy(row['step'])
            scope = step.get('_capture_window')
            if scope:
                # All steps captured in the same window share its opener choice.
                key = json.dumps(scope, ensure_ascii=False, sort_keys=True)
                step['_capture_window'] = scopes.setdefault(key, scope)
            form = row['form']
            try:
                self.store(step, form['alias'], form['text'], form['mode'], form['argument'])
            except (ValueError, bridge.BridgeError):
                # store() retained the original editor fields before compiling.
                pass
            steps.append(step)
        return steps

    def apply(self, steps):
        result = []
        self.failed_step = None
        for step in steps:
            saved = self.get(step)
            draft = self.drafts.get(step['id'])
            # Persist the visible generated instructions too. Otherwise an
            # untouched row can silently keep old recording-only semantics.
            if ((step['action'] != 'result-menu' or step.get('ordinary_menu'))
                    and (saved or (draft and draft[0] == self.identity(step)) or default_text(step))):
                sentence, mode, argument = self.form(step)
                try:
                    saved = self.compile(step, self.alias(step), sentence, mode, argument)
                except (ValueError, bridge.BridgeError):
                    self.failed_step = step['id']
                    raise
            result.append(self.edited_step(step, saved))
        return result

    @staticmethod
    def edited_step(step, fields):
        if step.get('ordinary_menu') and fields.get('instruction', {}).get('mode') == 'draft':
            # A user override supersedes the witnessed route. Keep its surface
            # and captions as context; only the new sentence supplies intent.
            menu = step['context']['menu']
            scope = {k: step[k] for k in ('_capture_window', 'window_context') if k in step}
            step = dict(manual('right_click_requires_menu_teaching', step['id']), **scope,
                        description=step['label'], menu_anchor={k: menu[k] for k in ('target', 'point')})
        return dict(step, **fields)


class _BoundWindow:
    def __init__(self, binding, title):
        self.binding, self.title = binding, title
        self.other_roots = frozenset()

    def __getattr__(self, key):
        return getattr(self.binding, key)

    def check(self):
        self.binding.check()
        if bridge.window_text(self.hwnd) != self.title:
            raise bridge.BridgeError('business_window_changed')

    def _other_recorder(self, hwnd):
        if not self.other_roots or not hwnd:
            return False
        root = bridge.root_window(hwnd)
        for _ in range(12):
            if not root or root == self.hwnd:
                return False
            if root in self.other_roots:
                return True
            root = bridge.owner_window(root)
        return False

    def contains_window(self, hwnd):
        return not self._other_recorder(hwnd) and self.binding.contains_window(hwnd)

    def recording_candidate(self, hwnd):
        return not self._other_recorder(hwnd) and self.binding.recording_candidate(hwnd)


class _HookWindows:
    """Only the mouse hook may consider a native embedded renderer as a candidate."""
    def __init__(self, bound):
        self.bound = bound
        self.hwnd = bound.hwnd
        self.contains_window = bound.contains_window

    def embedded(self, hwnd):
        from visual_target import native_child
        return (native_child(self.bound, hwnd) and not self.bound._other_recorder(hwnd)
                and bridge.hwnd_pid(hwnd) != self.bound.pid)

    def recording_candidate(self, hwnd):
        return (self.bound.recording_candidate(hwnd) or self.embedded(hwnd) or
                bool(hwnd and bridge.hwnd_pid(hwnd) == self.bound.pid
                     and bridge.window_class(bridge.root_window(hwnd)) == '#32768'))


class _CaptureUIA(bridge.UIA):
    """Compatibility reads local to this recorder, leaving the pinned core intact."""
    def __init__(self, binding):
        super().__init__()
        self.binding = binding

    def observe_point(self, binding, x, y, window=None):
        result = super().observe_point(binding, x, y, window)
        result['pointer'] = (x, y)
        return result

    def value_observation(self, element):
        if element.property(30003) == 50004:
            try:
                return live_action.text_readback(self.binding, self, element)
            except bridge.BridgeError:
                # A combo's inner editor still needs its node/owner mapping,
                # but must never become an independently captured input value.
                return {'value': None, 'value_evidence': {'status': 'unavailable', 'source': 'none'}}
        from provider_values import value_observation
        return value_observation(self, element)

    def node(self, element, root, path='0', parent=''):
        from provider_visibility import visible
        node = super().node(element, root, path, parent)
        node['offscreen'] = not visible(self.binding, self, element, root)
        if (node['role'] == 'textfield' and node.get('secure') is False
                and node.get('enabled') is True and not recorder._locator_valid(node.get('locator'))):
            locator = self.anchored_field(element, root)
            if locator is not None:
                node['locator'] = locator
        return node

    def anchored_field(self, element, root):
        """Retain a demonstrated unnamed field using a unique owned ancestor."""
        import control_lookup
        from provider_visibility import inside, valid
        try:
            live_action.textfield_scope(self.binding, self, element)
            bounds = self.bounds(element)
            class_name = element.property(30012)
            if not valid(bounds) or not isinstance(class_name, str) or not class_name:
                return None
            center = (bounds['x'] + bounds['width'] / 2, bounds['y'] + bounds['height'] / 2)
            parent = self.parent(element)
            for _ in range(32):
                if parent is None:
                    return None
                if self.pid(parent) != self.binding.pid or self.secure(parent):
                    return None
                identity = learned_tasks._selector(self.basic(parent))
                box = self.bounds(parent)
                if (identity and identity['role'] in {'group', 'window', 'tab', 'toolbar'}
                        and inside(bounds, box)):
                    selector = dict(anchor=identity, role='textfield', class_name=class_name,
                                    point=[(center[0]-box['x'])/box['width'], (center[1]-box['y'])/box['height']])
                    try:
                        current = control_lookup.resolve(self.binding, self, selector, root=root)
                        if self.same(current, element):
                            return selector
                    except bridge.BridgeError:
                        pass
                if self.same(parent, root):
                    return None
                parent = self.parent(parent)
        except bridge.BridgeError:
            return None

    def semantic_label(self, element, root):
        from desktop_select import field_label
        try:
            return field_label(self.binding, self, element, root)
        except (bridge.BridgeError, ValueError):
            return {}  # A missing display label does not discard a known locator.


class _CaptureBridge:
    def __init__(self, binding):
        self.binding = binding

    def __getattr__(self, name):
        return getattr(bridge, name)

    def UIA(self):
        return _CaptureUIA(self.binding)


def _semantic_list_selection(node):
    actions = node.get('available_actions', ())
    return (node.get('role') == 'listitem' and recorder._locator_valid(node.get('locator'))
            and 'Select' in actions and 'Click' not in actions)


class _GuiThreadInfo(recorder.ctypes.Structure):
    _fields_ = [('size', recorder.wintypes.DWORD), ('flags', recorder.wintypes.DWORD),
                ('active', recorder.wintypes.HWND), ('focus', recorder.wintypes.HWND),
                ('capture', recorder.wintypes.HWND), ('menu', recorder.wintypes.HWND),
                ('move', recorder.wintypes.HWND), ('caret', recorder.wintypes.HWND), ('rect', recorder.wintypes.RECT)]


class _RowHookPump(recorder.HookPump):
    visual_tracker = None

    def _key_event(self, code, *, ctrl, shift, alt, win):
        if code == 13 and not (ctrl or shift or alt or win):
            return {'kind': 'enter'}
        keys = key_chords.from_vk(code, ctrl=ctrl, shift=shift, alt=alt, win=win)
        if keys:
            return dict(kind='press-key', keys=keys)
        if (ctrl or shift or alt or win) and code in (9, 13, 27):
            return {'kind': 'shortcut'}
        return super()._key_event(code, ctrl=ctrl, shift=shift, alt=alt, win=win)

    def _emit(self, event):
        if event.get('kind') in ('enter', 'press-key'):
            # A Tab, Escape or submit can move focus before the UIA worker runs.
            C, W = recorder.ctypes, recorder.wintypes
            info = _GuiThreadInfo(size=C.sizeof(_GuiThreadInfo))
            read = bridge._sig(bridge.user32, 'GetGUIThreadInfo', W.BOOL, W.DWORD, C.POINTER(_GuiThreadInfo))
            if read(0, C.byref(info)):
                event['submit_focus'] = bridge.normalize_hwnd(info.focus)
                event['submit_window'] = bridge.root_window(event['submit_focus'])
        if self.visual_tracker is not None and (event.get('kind') in
                ('scroll', 'candidate_scroll', 'typing', 'enter', 'press-key', 'tab', 'escape', 'shortcut', 'pause', 'outside')
                or (event.get('kind') == 'down' and event.get('action') != 'click')):
            self.visual_tracker.invalidate(event.get('at'))
        if event.get('kind') == 'down' and event.get('scope_proven'):
            self.last_scoped_down = event['at']
        if event.get('kind') == 'outside' and getattr(self, 'outside_marker', None):
            # Track only the surface and timestamp of a possible first click.
            # Coordinates, captions, keys and contents from other apps are discarded.
            try:
                hit = bridge.root_window(bridge.window_from_point(*bridge.cursor()))
                self.outside_marker((hit, bridge.hwnd_pid(hit)), event['at'])
            except (bridge.BridgeError, OSError):
                pass
        if (event.get('kind') == 'down' and event.get('action') == 'click'
                and event.get('window') == self.binding.hwnd):
            if event.get('scope_proven'):
                self.row_tracker.pin_down(event)
            elif isinstance(self.binding, _HookWindows) and self.binding.embedded(event.get('hit_window')):
                # A native child relationship alone never authorizes UIA/keyboard
                # capture. Only a protected-checked pre-click sample may match it.
                event = dict(event, visual_embedded=True)
            if self.visual_tracker is not None and (event.get('scope_proven') or event.get('visual_embedded')):
                if self.visual_tracker.pin_down(event) and event.get('visual_embedded'):
                    self.last_scoped_down = event['at']
        return super()._emit(event)


class _Worker(recorder.CaptureWorker):
    pointed = None
    row_tracker = None
    visual_tracker = None
    menu_gesture = None
    command_snapshot = ()

    def _cache_commands(self):
        """Keep recent button intent while an input may close its containing dialog."""
        import control_lookup
        self._last_command_refresh = time.monotonic()
        if getattr(self.binding, 'is_linked_window', False) is not True:
            return
        commands = []
        peers = {}
        try:
            root = self.uia.from_handle(self.binding.hwnd)
            items = control_lookup.find_all(self.uia, root, roles=('button', 'splitbutton', 'hyperlink'), limit=128)
            for item in items:
                node = self.uia.node(item, root)
                locator = node.get('locator')
                if (node.get('secure') is not False or node.get('enabled') is not True or node.get('offscreen')
                        or 'Click' not in node.get('available_actions', ()) or not recorder._locator_valid(locator)):
                    continue
                raw = dict(id=uuid.uuid4().hex, action='click', label=node.get('name') or node.get('role'),
                           target=dict(kind='element', locator=copy.deepcopy(locator)),
                           recognition=dict(target='identified', action='supported', result='manual'))
                commands.append((node['bounds'], raw))
                hwnd = node.get('hwnd')
                if type(hwnd) is int and hwnd > 0:
                    peers[raw['id']] = hwnd, bridge.window_class(hwnd), bridge.window_text(hwnd)
            keys = [json.dumps(raw['target'], sort_keys=True) for _, raw in commands]
            self.command_snapshot = (time.monotonic(), [(box, raw) for box, raw in commands
                                      if keys.count(json.dumps(raw['target'], sort_keys=True)) == 1])
            self._command_peers = peers
        except (bridge.BridgeError, OSError, AttributeError, TypeError):
            # A button may already have closed its dialog while this refresh
            # runs. Retain the last bounded pre-click evidence for its queued
            # down/up event instead of erasing it on that late read failure.
            pass

    def _refresh_commands(self):
        """Refresh already checked native buttons without repeated UIA tree searches."""
        self._last_command_refresh = time.monotonic()
        if not self.command_snapshot:
            return
        C, W = recorder.ctypes, recorder.wintypes
        read = bridge._sig(bridge.user32, 'GetWindowRect', W.BOOL, W.HWND, C.POINTER(W.RECT))
        enabled = bridge._sig(bridge.user32, 'IsWindowEnabled', W.BOOL, W.HWND)
        commands = []
        try:
            self.binding.check()
            for _, raw in self.command_snapshot[1]:
                peer = getattr(self, '_command_peers', {}).get(raw['id'])
                if not peer:
                    continue
                hwnd, class_name, caption = peer
                if (bridge.hwnd_pid(hwnd) != self.binding.pid or bridge.root_window(hwnd) != self.binding.hwnd
                        or bridge.window_class(hwnd) != class_name or bridge.window_text(hwnd) != caption
                        or not enabled(hwnd) or not bridge.user32.IsWindowVisible(hwnd)):
                    continue
                rect = W.RECT()
                if read(hwnd, C.byref(rect)) and rect.right > rect.left and rect.bottom > rect.top:
                    commands.append((dict(x=rect.left,y=rect.top,width=rect.right-rect.left,
                                          height=rect.bottom-rect.top), raw))
            if commands:
                self.command_snapshot = time.monotonic(), commands
        except (bridge.BridgeError, OSError):
            pass

    def _command_before(self, event):
        if (event.get('action') != 'click' or not event.get('scope_proven')
                or event.get('window') != self.binding.hwnd):
            return None
        for at, observation in reversed(self.hover_history):
            if not 0 <= event['at'] - at <= 1.2 or not observation:
                continue
            node = observation['node']
            if (observation.get('window') == self.binding.hwnd and node.get('secure') is False
                    and node.get('enabled') is True and not node.get('offscreen')
                    and node.get('role') in {'button', 'splitbutton', 'hyperlink', 'menuitem'}
                    and 'Click' in node.get('available_actions', ())
                    and recorder._locator_valid(node.get('locator'))
                    and recorder._inside(node.get('bounds'), event['x'], event['y'])):
                return dict(id=uuid.uuid4().hex, action='click', label=node.get('name') or node['role'],
                            target=dict(kind='element', locator=copy.deepcopy(node['locator'])),
                            recognition=dict(target='identified', action='supported', result='manual'))
        if self.command_snapshot:
            at, commands = self.command_snapshot
            if 0 <= event['at'] - at <= 4:
                matches = [raw for box, raw in commands if recorder._inside(box, event['x'], event['y'])]
                if len(matches) == 1:
                    # The inventory identifies a button, not a demonstrated
                    # gesture. Separate real clicks must remain separate steps.
                    return dict(copy.deepcopy(matches[0]), id=uuid.uuid4().hex)
        return None

    def _unique_list_selection(self, observation):
        node = observation['node']
        locator = node.get('locator')
        if node.get('role') != 'listitem' or 'Select' not in node.get('available_actions', ()):
            return False
        selector = learned_tasks._selector(locator)
        if not selector:
            return False
        import control_lookup
        try:
            selector['ancestors'] = [learned_tasks._selector(parent) for parent in locator.get('ancestors', [])]
            current = control_lookup.resolve(self.binding, self.uia, selector, root=observation['root'])
            return self.uia.same(current, observation['element'])
        except (bridge.BridgeError, TypeError, ValueError):
            return False

    def target(self, observation, kind, point=None):
        if (kind == 'element' and point is None and observation.get('_freezing_observed_field')
                and observation.get('_recorded_field_target')):
            # The value and locator were read before the user's transition.
            # This stores the demonstration, it sends no input to the closed dialog.
            return copy.deepcopy(observation['_recorded_field_target'])
        locator = observation['node'].get('locator')
        if isinstance(locator, dict) and 'anchor' in locator:
            if kind != 'element' or point is not None or not _capture_locator_valid(locator):
                raise recorder.RecorderError('stable_locator_unavailable')
            import control_lookup
            current = control_lookup.resolve(self.binding, self.uia, locator, root=observation['root'])
            if not self.uia.same(current, observation['element']):
                raise recorder.RecorderError('capture_target_changed')
            target = dict(kind='element', locator=copy.deepcopy(locator))
            if observation['window'] != self.binding.hwnd:
                target['window'] = dict(title=self.bridge.window_text(observation['window']),
                                        class_name=self.bridge.window_class(observation['window']))
            return target
        return super().target(observation, kind, point)

    def observe_down(self, event):
        if event.get('kind') == 'down':
            if event.get('action') == 'right-click':
                from menu_capture import capture_surface, window_point_hint
                self.menu_gesture = None
                event['menu_anchor'] = capture_surface(self.binding, self.uia, event, self.hover_history)
                if not event['menu_anchor']:
                    event['menu_window_point'] = window_point_hint(self.binding, event)
            elif self.menu_gesture is not None:
                pick = self.menu_gesture.at_down(self.binding, self.uia, event)
                if pick:
                    # The menu observer proved this HWND, point and item. The
                    # legacy capture worker must not interpret it as a combo.
                    event.update(menu_pick=pick, menu_gesture_id=self.menu_gesture.step_id, scope_proven=True)
                    return None
                self.menu_gesture = None
            if (event.get('scope_proven') is False and event.get('window')
                    and bridge.window_class(event['window']) == '#32768'):
                return None
        # Pin pixels completed BEFORE the hook reported mouse-down. The live
        # UIA lookup may finish after the application's click handler.
        tracker = self.row_tracker
        if (self.visual_tracker is not None and event.get('kind') == 'down' and event.get('action') == 'click'
                and (event.get('scope_proven') or event.get('visual_embedded'))
                and event.get('window') == self.binding.hwnd):
            # The sampler pins a pre-down crop. Never substitute pixels collected
            # by the time the slower UIA lookup or mouse-up handler finishes.
            visual = self.visual_tracker.before(event)
            if visual is not None:
                event['visual'] = visual
            if event.get('visual_embedded'):
                if visual is not None:
                    self.binding.check()
                    event['scope_proven'] = True
                else:
                    # A rapid click can precede the first pixel sample. Only
                    # retain a description slot if the exact original native
                    # receiver is still an exposed, non-editable canvas.
                    from visual_target import _safe_patch, native_child
                    point = event['x'], event['y']
                    hit = event['hit_window']
                    try:
                        if (native_child(self.binding, hit) and
                                _safe_patch(self.binding, self.uia, [*point, 1, 1], point, exposed=True) == hit):
                            event['scope_proven'] = True
                    except (bridge.BridgeError, OSError):
                        pass
                return None
        if tracker and event.get('kind') == 'down' and event.get('window') == self.binding.hwnd:
            tracker.next_press((event['x'],event['y']))
        frame = tracker.before(event['at'], event.get('token')) if tracker and event.get('action') == 'click' else None
        try:
            observation = super().observe_down(event)
        except (bridge.BridgeError, OSError):
            if event.get('visual') is None:
                raise
            # An activating canvas can dispose its accessibility peer before
            # this worker runs. Its pre-click sample was already scope-checked.
            self.binding.check()
            return None
        if (event.get('kind') == 'down' and event.get('action') == 'click'
                and event.get('scope_proven') is True and event.get('window') == self.binding.hwnd
                and isinstance(getattr(self.binding, 'exe', None), str)
                and self.binding.exe.casefold() == 'notepad.exe'
                and (observation is None or (observation['node'].get('secure') is False
                     and observation['node'].get('role') in ('window', 'pane', 'document')))):
            # Notepad's find bar draws over the document. UIA can return the
            # document at the actual click instead of the overlaid command.
            from notepad_find import contains_click, locate
            from visual_target import NOTEPAD_FIND_NEXT
            try:
                found = locate(self.binding, exposed=True)
                if (found['hit'] == event.get('hit_window')
                        and contains_click(found, event['x'], event['y'])):
                    event['visual'] = dict(NOTEPAD_FIND_NEXT)
                    return None
            except (bridge.BridgeError, OSError):
                pass
        if (not tracker or not observation or event.get('window') != self.binding.hwnd
                or not event.get('scope_proven') or observation.get('combo') is not None):
            return observation
        node = observation['node']
        role = node.get('role')
        if role in {'statictext', 'image'}:
            owner = self._command_owner(event, observation)
            if owner['node'].get('role') == 'listitem' and self._unique_list_selection(owner):
                return owner
        if (node.get('secure') is not False or node.get('enabled') is not True
                or role not in ('list', 'listitem', 'row', 'table', 'image', 'statictext', 'custom', 'group')
                or event.get('action') != 'click'):
            return observation
        if 'combo' not in observation and self.combo_for(observation) is not None:
            return observation
        # A named native selection is stronger evidence than the generic row
        # image tracker, which also sees ordinary dialog option lists as rows.
        if self._unique_list_selection(observation):
            return observation
        import row_capture
        import row_vision
        point = [event['x'],event['y']]
        press, number, cue = None, None, None
        if frame is not None or role in ('row', 'listitem', 'list', 'table'):
            try:
                # A first click may precede the first usable pixel sample. Its
                # current owned row can still supply an editable ordinal hint.
                # A post-click image must never supply the before-state.
                layout = frame.rows if frame else row_vision._row_layout(self.binding)[1]
                rect, meta = row_vision._point_row(layout, point)
                if not row_vision.recorded_row_hit(self.uia, observation['element'], observation['root'],
                                                   self.binding.pid, point, rect):
                    return observation
                peers = [r for r,m in layout if m['scope'] == meta['scope'] and m['role'] == meta['role']]
                number = sorted(peers,key=lambda r:(r[1],r[0])).index(rect)+1
                cue = dict(ordinal=number, size=rect[2:], point=[point[0]-rect[0], point[1]-rect[1]], **meta)
                if frame is not None:
                    press = row_capture.prepare(frame, point, event['at'], cue=cue)
            except bridge.BridgeError:
                pass
        if not press and _semantic_list_selection(node):
            return observation
        if press or number or role in ('row','listitem'):
            return dict(observation, painted_row=(number,press), row_cue=cue)
        return observation

    def handle(self, event):
        if event['kind'] in ('down', 'outside', 'scroll', 'candidate_scroll', 'tab', 'escape', 'shortcut', 'press-key', 'pause'):
            self._pending_field_activation = None
        if event['kind'] == 'typing' and self.watched:
            activation = self.watched.get('_recorded_activation_at')
            if activation is not None and event['at'] >= activation:
                self.watched['_recorded_typing_at'] = event['at']
                self._capture_typed_value()
        pending = getattr(self, '_pending_commands', None)
        if pending is None:
            pending = self._pending_commands = {}
        if event['kind'] == 'down' and (raw := self._command_before(event)):
            pending[event['token']] = event, raw
            try:
                self.binding.check()
            except bridge.BridgeError:
                # The paired hook event will decide whether a click completed.
                # This path records a witnessed intent and never sends input.
                return
        elif event['kind'] == 'up' and event.get('token') in pending:
            down, raw = pending.pop(event['token'])
            complete = (event.get('inside') and abs(event['x'] - down['x']) <= 8
                        and abs(event['y'] - down['y']) <= 8)
            try:
                self.binding.check()
                missing = self.captures.get(event['token'], (None, None))[1] is None
            except bridge.BridgeError:
                missing = True
            if complete and missing and (down.get('visual') or {}).get('surface') != 'win32-toolbar-glyph':
                self.captures.pop(event['token'], None)
                self.model.event_at = down['at']
                try:
                    if not self.capture_double(down, None):
                        self.model.add(raw)
                        self.recent_click = dict(token=down['token'], step_id=raw['id'],observation=None,
                                                 watched_step_id=self.watched_step['id'] if self.watched_step else None)
                        self._remember_double_target(down)
                finally:
                    self.model.event_at = None
                return
        if event['kind'] in ('outside', 'scroll', 'candidate_scroll', 'typing', 'enter', 'press-key', 'tab',
                             'escape', 'shortcut', 'pause'):
            self.menu_gesture = None
        if self.row_tracker and event['kind'] in ('scroll','candidate_scroll'):
            self.row_tracker.invalidate(event['at'])
        self.model.event_at = event.get('at')
        if event['kind'] == 'up':
            down, _ = self.captures.get(event.get('token'), (None, None))
            if down:
                self.model.event_at = down.get('at', self.model.event_at)
                if not event['inside'] or abs(event['x'] - down['x']) > 8 or abs(event['y'] - down['y']) > 8:
                    self.menu_gesture = None
        try:
            if event['kind'] == 'press-key':
                self._capture_key(event)
                return
            if event['kind'] == 'enter' and self._capture_submit(event):
                return
            result = super().handle(event)
            if event['kind'] == 'up' and down and self.recent_click and self.recent_click['token'] == down['token']:
                self._remember_double_target(down)
            return result
        finally:
            self.model.event_at = None

    def _key_target(self, event):
        """Use the pre-key native focus, even when Tab has already moved it."""
        focus, window = event.get('submit_focus'), event.get('submit_window')
        if not focus or window != self.binding.hwnd:
            return None
        watched = self.watched
        proved = (watched and watched.get('_recorded_field_target') and watched['window'] == window
                  and watched.get('_key_field_seen_at', float('inf')) <= event['at'])
        if (proved and focus != window and watched.get('_recorded_native_focus') == focus
                and not bridge.user32.IsWindow(focus)):
            # Escape or a shortcut may already have destroyed its HWND. Use
            # only the prior qualified field matched by the hook before dispatch.
            return copy.deepcopy(watched['_recorded_field_target']), True
        if bridge.root_window(focus) != window or bridge.hwnd_pid(focus) != self.binding.pid:
            return None
        self.binding.check()
        if proved and watched.get('_key_virtual_focus') and self.uia.same(watched['element'], self.uia.focused()):
            live_action.key_target_ready(self.binding, self.uia, watched['element'], {'role': 'textfield'})
            return copy.deepcopy(watched['_recorded_field_target']), True
        if focus == window:
            return None
        element = self.uia.from_handle(focus)
        if element is None or bridge.normalize_hwnd(element.property(30020)) != focus:
            return None
        role = bridge.ROLES.get(element.property(30003))
        # A shared HWND can host several virtual editors. Only a dedicated
        # field or button identifies itself after focus has already moved.
        if role not in key_chords.TARGET_ROLES or (role not in ('textfield', 'button')
                and not self.uia.same(element, self.uia.focused())):
            return None
        root = live_action.key_target_ready(self.binding, self.uia, element, {'role': role})
        same_field = bool(watched and watched['window'] == window and self.uia.same(element, watched['element']))
        if proved and same_field:
            return copy.deepcopy(watched['_recorded_field_target']), True
        locator = self.uia.locator(element, root)
        if role == 'textfield' and (watched and watched['node'].get('role') == 'combobox'
                or any(node.get('role') == 'combobox' for node in locator.get('ancestors', ()))):
            from control_lookup import ancestry
            chain = ancestry(self.uia, element, root, pid=self.binding.pid)
            owner = next((peer for peer in chain[1:-1] if peer.property(30003) == 50003), None)
            if owner is not None:
                live_action.key_target_ready(self.binding, self.uia, owner, {'role': 'combobox'})
                locator = self.uia.locator(owner, root)
                same_field = bool(watched and self.uia.same(watched['element'], owner))
                target = dict(kind='element', locator=locator) if recorder._locator_valid(locator) else None
                return target, same_field
        if role == 'textfield' and not recorder._locator_valid(locator):
            locator = self.uia.anchored_field(element, root)
        if _capture_locator_valid(locator):
            return dict(kind='element', locator=locator), same_field
        return None, same_field  # Known unprotected focus; keep the chord for review.

    def _capture_key(self, event):
        try:
            proof = self._key_target(event)
        except (bridge.BridgeError, recorder.RecorderError, OSError):
            proof = None
        point = self.watched_step if proof and proof[1] else None
        self.freeze_watch(event['at'])
        self.option_history.clear()
        if proof is None:
            self.model.manual('', 'keyboard_transition_needs_review')
            return
        if proof[0] is None:
            self.model.add(dict(manual('keyboard_transition_needs_review'), keys=event['keys']))
            return
        step = dict(id=uuid.uuid4().hex, action='press-key', keys=event['keys'], label=event['keys'],
                    target=proof[0], recognition=dict(target='identified', action='supported', result='manual'))
        with self.model.lock:
            # A click solely to focus this field needs no invented input value.
            if (point and point['action'] == 'teach-target' and self.model.steps
                    and next(reversed(self.model.steps)) == point['id'] and self.model.steps[point['id']]['action'] == 'teach-target'):
                step['id'] = point['id']
                self.model.times[step['id']] = event['at']
            self.model.add(step)

    def _capture_submit(self, event):
        """An observed Enter on a writable focused field becomes an explicit submit."""
        observation = self.watched
        if not observation or observation['node'].get('role') != 'textfield':
            return False
        try:
            self.binding.check()
            before_focus = (event.get('submit_focus') and
                            event['submit_focus'] == observation.get('_recorded_native_focus', observation['node'].get('hwnd')) and
                            event.get('submit_window') == observation['window'] and
                            observation.get('_recorded_field_target') is not None)
            if not before_focus:
                focused = self.uia.focused()
                if focused is None or not self.uia.same(focused, observation['element']):
                    return False
                live_action.textfield_scope(self.binding, self.uia, observation['element'])
            if not _capture_locator_valid(observation['node'].get('locator')):
                return False
            self.freeze_watch(event.get('at'))
            observation['_freezing_observed_field'] = bool(before_focus)
            try:
                step = self.step('submit', observation)
            finally:
                observation.pop('_freezing_observed_field', None)
            step['label'] = '입력란에서 Enter 적용'
            step['recognition'] = dict(target='identified', action='supported', result='manual')
            self.model.add(step)
            return True
        except (bridge.BridgeError, recorder.RecorderError, OSError):
            return False

    def _capture_typed_value(self):
        observation = self.watched
        if (not observation or observation['node'].get('role') != 'textfield'
                or not observation.get('_recorded_field_target')
                or '_recorded_typing_at' not in observation or not self.watched_samples):
            return
        at, value = self.watched_samples[-1]
        if at < observation['_recorded_typing_at'] + .05 or not isinstance(value, str):
            return
        observation['_freezing_observed_field'] = True
        try:
            recorder.CaptureWorker._commit_value(self, value)
            if self.watched_step and self.watched_step['action'] == 'set-value' and self.watched_step.get('value') == value:
                observation['_typed_value_observed'] = True
        finally:
            observation.pop('_freezing_observed_field', None)

    def _remember_field_activation(self, event, observation, step_id, previous_ids):
        """Retain a witnessed passive activation, never an arbitrary unknown click."""
        if (not observation or event.get('action') != 'click' or event.get('scope_proven') is not True
                or event.get('window') != self.binding.hwnd or observation.get('window') != self.binding.hwnd):
            return
        node = observation['node']
        if (node.get('secure') is not False or node.get('enabled') is not True
                or node.get('role') not in {'window', 'pane', 'tabgroup', 'group', 'statictext', 'image', 'custom', 'textfield'}
                or set(node.get('available_actions', [])) - {'SetFocus'}
                or not recorder._inside(node.get('bounds'), event.get('x'), event.get('y'))):
            return
        with self.model.lock:
            if step_id is None:
                added = set(self.model.steps) - previous_ids
                if len(added) != 1:
                    return
                step_id = added.pop()
            raw = self.model.steps.get(step_id, {})
            if (raw.get('action') != 'manual' or raw.get('recognition', {}).get('reason')
                    not in {'unsupported_recorded_action', 'stable_locator_unavailable'}):
                return
        self._pending_field_activation = dict(id=step_id, window=event['window'],
            x=event['x'], y=event['y'], at=event['at'])

    def _refine_field_activation(self, observation):
        """A later typed field may refine only its own exact activation step."""
        pending = getattr(self, '_pending_field_activation', None)
        if not pending or not observation.get('_recorded_field_target'):
            return
        if (observation.get('window') != pending['window'] or self.last_key < pending['at']
                or not recorder._inside(observation['node'].get('bounds'), pending['x'], pending['y'])):
            self._pending_field_activation = None
            return
        with self.model.lock:
            raw = self.model.steps.get(pending['id'], {})
            if (raw.get('action') != 'manual' or raw.get('recognition', {}).get('reason')
                    not in {'unsupported_recorded_action', 'stable_locator_unavailable'}):
                self._pending_field_activation = None
                return
        step = self.step('teach-target', observation)
        step['id'] = pending['id']
        step['recognition'] = dict(target='identified', action='requires_instruction',
                                   result='manual', reason='text_value_unobserved')
        observation['_recorded_activation_at'] = pending['at']
        observation['_recorded_typing_at'] = self.last_key
        self.model.add(step)
        self.watched_step = step
        self.pointed = observation
        self._pending_field_activation = None
        self._capture_typed_value()

    def tick(self):
        if self.menu_gesture is not None:
            events = getattr(self, 'events', None)
            if events is not None and not events.empty():
                # A hook can already have queued down/up while a preceding
                # UIA read was running. Handle those timestamps before taking
                # another snapshot of a menu that may now be closed.
                return
            # A native menu can disappear immediately on mouse-down. Keep
            # its complete pre-down inventory fresh instead of spending this
            # interval rediscovering the underlying edit/combo/button tree.
            self._poll_menu()
            if self.menu_gesture is not None:
                from context_menu import _native_menu_owner
                if _native_menu_owner(self.binding) is not None:
                    return
        pending = getattr(self, '_pending_commands', {})
        if pending:
            try:
                self.binding.check()
            except bridge.BridgeError:
                if time.monotonic() - min(down['at'] for down, _ in pending.values()) < 2:
                    return
                pending.clear()
        super().tick()
        if (self.watched and self.watched['node'].get('role') == 'textfield'
                and time.monotonic() - getattr(self, '_last_command_refresh', 0) >= 1):
            self._refresh_commands()
        self._poll_menu()
        if (self.row_tracker and self.hovered and self.hovered.get('window') == self.binding.hwnd
                and self.hovered['node'].get('role') in ('list','listitem','row','table')):
            self.row_tracker.interest.set()

    def _poll_menu(self):
        gesture = self.menu_gesture
        if gesture is not None:
            # A native command can close its popup on down. The matching up
            # still decides whether the demonstrated click was completed.
            # Keep this exact gesture until that queued pair is processed.
            now = time.monotonic()
            if any(down.get('menu_pick') is not None and down.get('menu_gesture_id') == gesture.step_id
                   and 0 <= now - down.get('at', -10) <= 2
                   for down, _ in tuple(getattr(self, 'captures', {}).values())):
                return
            state = gesture.tick(self.binding, self.uia, time.monotonic())
            if state == 'complete':
                self.model.context_complete(gesture.step())
            if state != 'pending':
                events = getattr(self, 'events', None)
                if state == 'expired' and events is not None and not events.empty():
                    # The popup may have closed during this very poll, after
                    # its click was queued by the hook but before handle(down).
                    # Preserve the pre-down evidence only until the queue is
                    # processed. Its original timestamp limits still apply.
                    return
                self.menu_gesture = None

    def _remember_double_target(self, down):
        # Manual first clicks don't return an ID from the base worker. Keep the actual
        # step and pre-press sample so a paired second click only replaces that step.
        previous = self.recent_click
        with self.model.lock:
            last = next(reversed(self.model.steps), None)
            if previous['step_id'] is None and last and self.model.times[last] == down['at']:
                previous['step_id'] = last
            first = self.model.steps.get(previous['step_id'], {})
            previous['protected'] = first.get('recognition', {}).get('reason') == 'protected_input_not_recorded'
            # A visual or unidentified click can still have a known table. Use the
            # hit already collected on this press; capturing a second tree is unnecessary.
            obs = previous.get('observation')
            if (first and not previous['protected'] and down.get('scope_proven') is True
                    and down.get('window') == self.binding.hwnd and obs and obs.get('window') == self.binding.hwnd
                    and obs['node'].get('secure') is False
                    and obs['node'].get('role') in {'table', 'list', 'group', 'row', 'listitem', 'statictext', 'custom'}
                    and (first['action'] == 'visual-click' or
                         (first['action'] == 'manual' and learned_tasks.is_recoverable(first)))):
                surface = learned_tasks.recorded_result_surface(obs['node'].get('locator'))
                if surface:
                    first['target'] = dict(kind='element', locator=surface)
        if (down.get('visual') and 'surface' not in down['visual']
                and down.get('scope_proven') is True and down.get('window') == self.binding.hwnd
                and not down.get('menu_pick') and not previous['protected']):
            previous['double_visual'] = down['visual']

    def capture_double(self, down, observation):
        previous = self.recent_click or {}
        previous_id = previous.get('step_id')
        with self.model.lock:
            surface = learned_tasks.result_surface_hint(self.model.steps.get(previous_id, {}))
        result = super().capture_double(down,observation)
        if result:
            with self.model.lock:
                self.model.completed_double_down = down['at']
        if result and previous_id and self.row_tracker:
            self.row_tracker.cancel_step(previous_id)
        if result:
            self.menu_gesture = None
            spec = previous.get('double_visual')
            if previous_id and (previous.get('protected') or
                                (observation and observation['node'].get('secure') is not False)):
                self.model.add(manual('protected_input_not_recorded', previous_id))
                return result
            if (spec and previous_id and down.get('scope_proven') is True and down.get('window') == self.binding.hwnd
                    and not down.get('menu_pick') and (observation is None or observation['node'].get('secure') is False)):
                with self.model.lock:
                    step = self.model.steps.get(previous_id, {})
                    if step.get('recognition', {}).get('reason') == 'double_click_needs_review':
                        self.model.add(dict(id=previous_id, action='visual-click', label='녹화한 항목 더블클릭',
                                            visual=dict(spec, gesture='double-click')))
            if (surface and previous_id and down.get('scope_proven') is True
                    and down.get('window') == self.binding.hwnd):
                with self.model.lock:
                    step = self.model.steps.get(previous_id, {})
                    if learned_tasks.is_double_click_step(step):
                        step['result_surface'] = surface
        return result

    def _drop_watch(self):
        self.pointed = None
        super()._drop_watch()

    def _emit_point(self):
        obs = self.watched
        if (not self.pointed or obs is not self.pointed
                or (self.watched_step and self.watched_step['action'] != 'teach-target') or self.watched_selection
                or obs['window'] != self.binding.hwnd or obs['node'].get('role') not in {'combobox', 'textfield'}
                or obs['node'].get('secure') is not False or obs['node'].get('enabled') is not True):
            return False
        self.binding.check()
        locator = obs['node'].get('locator')
        if self.uia.secure(obs['element']) or not _capture_locator_valid(locator):
            return False
        role = obs['node']['role']
        if role == 'textfield':
            try:
                root = live_action.textfield_scope(self.binding, self.uia, obs['element'])
                if ('anchor' not in locator and
                        learned_tasks._selector(self.uia.locator(obs['element'], root)) != learned_tasks._selector(locator)):
                    return False
            except bridge.BridgeError:
                return False
        elif not self.watched_step:
            try:
                current = bridge._current_combo(self.binding, self.uia, locator)
            except bridge.BridgeError:
                return False
            if not self.uia.same(current['element'], obs['element']):
                return False
        step = self.step('teach-target', obs)
        if self.watched_step:
            step['id'] = self.watched_step['id']
        step['recognition'] = {'target': 'identified', 'action': 'requires_instruction',
                               'result': 'manual',
                               'reason': 'combo_choice_unproven' if role == 'combobox' else 'text_value_unobserved'}
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
        if observation['node'].get('role') == 'textfield':
            try:
                live_action.textfield_scope(self.binding, self.uia, observation['element'])
            except bridge.BridgeError as error:
                self.freeze_watch()
                self.model.manual('', 'protected_input_not_recorded' if error.code == 'target_protected' else error.code)
                return
            node = observation['node']
            if node.get('secure') is not False or node.get('enabled') is not True or 'SetValue' not in node.get('available_actions', []):
                self.freeze_watch()
                self.model.manual('', 'text_field_not_writable')
                return
            if self.watched and self.uia.same(self.watched['element'], observation['element']):
                return
            self.freeze_watch()
            observed = live_action.text_readback(self.binding, self.uia, observation['element'])
            value = observed.get('value') if observed.get('value_evidence', {}).get('status') == 'observed' else None
            self.watched, self.watched_value, self.watched_step = observation, value, None
            self.watched_selection = None
            self.selection_deadline = 0
            self.watched_samples.clear()
            self.watched_samples.append((time.monotonic(), value))
            try:
                observation['_recorded_field_target'] = copy.deepcopy(self.target(observation, 'element'))
                native = bridge.normalize_hwnd(observation['element'].property(30020))
                if (native and bridge.hwnd_pid(native) == self.binding.pid
                        and bridge.root_window(native) == observation['window']):
                    observation['_recorded_native_focus'] = native
                if observation['element'].property(30009) is True:
                    if (not native or native == observation['window']) and observation['element'].property(30008) is True:
                        observation['_key_virtual_focus'] = True
                    observation['_key_field_seen_at'] = time.monotonic()
            except (bridge.BridgeError, recorder.RecorderError):
                pass
            except (AttributeError, TypeError, ValueError):
                pass
            self._refine_field_activation(observation)
            return
        if observation['node'].get('role') != 'combobox':
            self.freeze_watch()
            now = time.monotonic()
            if now - self.last_unreadable_input > .8:
                self.model.manual('', 'free_text_not_recorded')
            self.last_unreadable_input = now
            return
        return super().watch(observation)

    def _commit_value(self, current):
        if self.watched and self.watched['node'].get('role') == 'textfield':
            if self.watched.get('_freezing_observed_field'):
                return recorder.CaptureWorker._commit_value(self, current)
            try:
                live_action.textfield_scope(self.binding, self.uia, self.watched['element'])
            except bridge.BridgeError as error:
                if (error.code in ('target_window_mismatch', 'element_disappeared')
                        and self._freeze_observed_field()):
                    return
                self._manual_watch('protected_input_not_recorded' if error.code == 'target_protected' else error.code)
                return
            if not isinstance(current, str):
                self._manual_watch('value_readback_unavailable')
                return
            # A location-only click does not demonstrate assigning the existing
            # text. Preserve its variable draft until a changed value is read.
            if self.watched_step and self.watched_step['action'] == 'teach-target' and current == self.watched_value:
                return
            changed = current != self.watched_value
            result = super()._commit_value(current)
            if (changed and self.watched_step and self.watched_step['action'] == 'set-value'
                    and self.watched_step.get('value') == current):
                self._cache_commands()
            return result
        # An unchanged or unreadable collapsed value after a mere field click
        # cannot teach a selection. A witnessed option click retains its intent.
        if self.watched_selection is None and (not self.watched_step or self.watched_step['action'] == 'teach-target'):
            if not current or current == self.watched_value:
                return self._unobserved_combo('combo_choice_unproven')
            observed = combo_readback(self.binding, self.uia, self.watched['element'])
            if observed.get('value') != current or observed.get('value_evidence', {}).get('status') != 'observed':
                return self._unobserved_combo('combo_choice_unproven')
        return super()._commit_value(current)

    def finalize_watch(self, force, sample_only=False):
        if self.watched and self.watched['node'].get('role') == 'textfield':
            try:
                live_action.textfield_scope(self.binding, self.uia, self.watched['element'])
            except bridge.BridgeError as error:
                if (error.code in ('target_window_mismatch', 'element_disappeared')
                        and self._freeze_observed_field()):
                    return
                self._manual_watch('protected_input_not_recorded' if error.code == 'target_protected' else error.code)
                return
        result = super().finalize_watch(force, sample_only)
        if not sample_only:
            self._capture_typed_value()
        return result

    def _freeze_observed_field(self, cutoff=None):
        observation = self.watched
        if (not observation or observation['node'].get('role') != 'textfield'
                or observation['node'].get('secure') is not False
                or observation['node'].get('enabled') is not True
                or not observation.get('_recorded_field_target')):
            return False
        cutoff = time.monotonic() if cutoff is None else cutoff
        samples = [(at, value) for at, value in self.watched_samples
                   if at <= cutoff and (not self.last_key or at >= self.last_key + .05)
                   and isinstance(value, str)]
        if not samples:
            if (observation.get('_typed_value_observed') and self.watched_step
                    and self.watched_step['action'] == 'set-value'):
                self._drop_watch()
                return True
            return False
        if (self.watched_step and self.watched_step['action'] == 'teach-target'
                and samples[-1][1] == self.watched_value):
            return False
        observation['_freezing_observed_field'] = True
        try:
            recorder.CaptureWorker._commit_value(self, samples[-1][1])
        finally:
            observation.pop('_freezing_observed_field', None)
            self._drop_watch()
        return True

    def freeze_watch(self, cutoff=None):
        if self._freeze_observed_field(cutoff):
            return
        return super().freeze_watch(cutoff)

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
            if role in {'button', 'splitbutton', 'menuitem', 'treeitem', 'tab', 'hyperlink', 'listitem'}:
                from provider_visibility import visible
                if (item.property(30010) is not True or not visible(self.binding, self.uia, item, root)
                        or not self.uia.within(item, root)
                        or not recorder._inside(self.uia.bounds(item), event.get('x'), event.get('y'))):
                    break
                return dict(observation, node=self.uia.node(item, root), element=item, combo=None)
            if role not in {'statictext', 'image', 'group'} or set(self.uia.actions(item)) - {'SetFocus'}:
                break
        return observation

    def capture_action(self, event, observation):
        pointed = None
        if event['action'] == 'right-click':
            self.freeze_watch(event.get('at'))
            surface = event.get('menu_anchor')
            if surface:
                from menu_capture import MenuGesture
                step_id = uuid.uuid4().hex
                self.model.context_start(step_id, surface)
                self.menu_gesture = MenuGesture(step_id, surface, event, self.binding.info()['dpi'])
                return step_id
            if event.get('menu_window_point') is not None:
                step_id = uuid.uuid4().hex
                self.model.context_start(step_id, None, event['menu_window_point'])
                return step_id
            self.model.manual('', 'right_click_requires_menu_teaching')
            return
        if event.get('menu_pick') is not None and self.menu_gesture is not None:
            self.menu_gesture.clicked = event['menu_pick']
            self.menu_gesture.last_poll = 0
            self._poll_menu()
            return
        if observation:
            observed_role = observation['node'].get('role')
            if (event['action'] == 'click' and event.get('scope_proven', True) and
                    ((observed_role == 'textfield' and not _capture_locator_valid(observation['node'].get('locator')))
                     or observed_role in {'window','pane','tabgroup','group','statictext','image','custom'})):
                # An edit provider may expose its real bounds only after the
                # activation click. Refine that same witnessed click before
                # deciding that the field has no stable target.
                try:
                    self.binding.check()
                    focused = self.uia.focused()
                    root = observation['root']
                    if (focused is not None and self.uia.pid(focused) == self.binding.pid
                            and self.uia.within(focused, root) and not self.uia.secure(focused)
                            and focused.property(30003) == 50004
                            and recorder._inside(self.uia.bounds(focused), event['x'], event['y'])):
                        node = self.uia.node(focused, root)
                        if _capture_locator_valid(node.get('locator')):
                            observation = dict(observation, element=focused, node=node, combo=None)
                except (bridge.BridgeError, OSError):
                    pass
            if event['action'] == 'click' and 'painted_row' in observation:
                self.freeze_watch(event.get('at'))
                number, press = observation['painted_row']
                node = observation['node']
                selection = None
                # An ordinary list item can both support semantic selection and
                # paint an independent action button. Keep any supported original
                # selection unless the demonstrated button's own effect settles.
                if _semantic_list_selection(node):
                    selection = self.step('select', observation)
                step_id = selection['id'] if selection else uuid.uuid4().hex
                self.model.row_step(step_id, number, None, selection, cue=observation.get('row_cue'), press=press)
                if press:
                    self.row_tracker.follow(step_id,press)
                return step_id
            observation = self._command_owner(event, observation)
            node = observation['node']
            if node.get('secure') is not False:
                self.freeze_watch(event.get('at'))
                self.model.manual('', 'protected_input_not_recorded')
                return
            if (event['action'] == 'click' and node.get('role') == 'listitem'
                    and self.combo_for(observation) is None
                    and (_semantic_list_selection(node) or self._unique_list_selection(observation))):
                self.freeze_watch(event.get('at'))
                step = self.step('select', observation)
                self.model.add(step)
                return step['id']
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
                if pointed is None and node.get('role') == 'textfield':
                    pointed = observation
            if pointed is observation and node.get('role') == 'textfield':
                observation.setdefault('_recorded_activation_at', event.get('at', time.monotonic()))
            if (pointed is observation and isinstance(node.get('locator'), dict)
                    and 'anchor' in node['locator']):
                self.watch(observation)
                if self.watched and self.uia.same(self.watched['element'], observation['element']):
                    self._cache_commands()
                    self.pointed = self.watched
                    if self.watched_step is None:
                        step = self.step('teach-target', self.watched)
                        step['recognition'] = dict(target='identified', action='requires_instruction',
                                                   result='manual', reason='text_value_unobserved')
                        self.model.add(step)
                        self.watched_step = step
                    return self.watched_step['id']
        if (event.get('visual') is not None and 'gesture' not in event['visual']
                and pointed is None and self._use_visual(event, observation)):
            self.freeze_watch(event.get('at'))
            self.binding.check()
            label = ('메모장 다음 찾기' if event['visual'].get('surface') == 'notepad-find-next'
                     else '녹화한 화면 항목 누르기')
            step = dict(id=uuid.uuid4().hex, action='visual-click', label=label,
                        visual=event['visual'])
            self.model.add(step)
            return step['id']
        with self.model.lock:
            previous_ids = set(self.model.steps)
        step = super().capture_action(event, observation)
        self._remember_field_activation(event, observation, step, previous_ids)
        if pointed and self.watched and self.uia.same(pointed['element'], self.watched['element']):
            if self.watched['node'].get('role') == 'textfield':
                self._cache_commands()
            self.pointed = self.watched
            # Store the clicked field immediately. Reading the selected value
            # can later refine this same step, but is not a prerequisite to
            # teaching a location for an explicit instruction.
            if (self.watched_step is None and self.watched['window'] == self.binding.hwnd
                    and self.watched['node'].get('secure') is False
                    and self.watched['node'].get('enabled') is True
                    and recorder._locator_valid(self.watched['node'].get('locator'))):
                point = self.step('teach-target', self.watched)
                point['recognition'] = {'target': 'identified', 'action': 'requires_instruction',
                    'result': 'manual', 'reason': 'combo_choice_unproven'
                    if self.watched['node']['role'] == 'combobox' else 'text_value_unobserved'}
                self.model.add(point)
                self.watched_step = point
                return point['id']
        return step

    def _use_visual(self, event, observation):
        if (event['action'] != 'click' or event.get('scope_proven') is not True
                or event.get('window') != self.binding.hwnd):
            return False
        if observation is None:
            # A valid sampled crop already has its own protected-element and
            # window checks; the accessibility peer can disappear on click.
            return True
        node = observation['node']
        role = node.get('role')
        actions = set(node.get('available_actions', ()))
        shell = role in ('window','pane','group','custom','toolbar','document')
        if (observation.get('window') != self.binding.hwnd or node.get('secure') is not False
                or not (shell or role in ('statictext','image','button'))
                or role == 'document' and event.get('visual') is None
                or actions.intersection(('Check','Select','Expand','Collapse'))):
            return False
        if ((node.get('enabled') is not True or 'SetValue' in actions)
                and not (shell and event.get('visual') is not None)):
            return False
        locator = node.get('locator')
        if (role=='button' and (event.get('visual') or {}).get('surface')=='win32-toolbar-glyph'
                and not (locator or {}).get('automation_id')):
            # A Win32 button name can come solely from its transient tooltip.
            # The sampler proved the real native button and its pre-press glyph.
            return True
        return not (role == 'button' and 'Click' in actions and recorder._locator_valid(locator)
                    and learned_tasks._selector(locator))


class CaptureSession:
    def __init__(self, binding, title):
        self.binding = _BoundWindow(binding, title)
        self.notes = CaptureNotes()
        self.events, self.notices = queue.Queue(maxsize=128), queue.Queue()
        self.worker = _Worker(self.binding, _CaptureBridge(self.binding), self.notes, self.events, self.notices)
        from row_capture import Tracker
        from visual_target import HoverTracker
        self.rows = self.worker.row_tracker = Tracker(self.binding, self.notes)
        self.visual = self.worker.visual_tracker = HoverTracker(self.binding)
        self.pump = _RowHookPump(_HookWindows(self.binding), bridge, self.events, self.notices)
        self.pump.row_tracker = self.rows
        self.pump.visual_tracker = self.visual
        self.started = self.ended = False

    def start(self, *, prime_rows=True):
        if self.started or self.ended:
            raise bridge.BridgeError('recording_already_started')
        self.binding.check()
        self.worker.start()
        try:
            if not self.worker.ready.wait(3) or self.worker.error:
                raise bridge.BridgeError('accessibility_worker_unavailable')
            self.rows.start()
            self.visual.start()
            # Prime the pre-click sample as the recorder opens. A slow or
            # unavailable row provider cannot hold up ordinary semantic capture.
            if prime_rows:
                self.rows.initial_sample.wait(1.8)
            self.worker.enabled.set()
            self.hook_installing_at = time.monotonic()
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
                self.visual.stop()
                self.worker.enabled.clear()
                if (reason == 'target_lifetime_changed' and getattr(self, 'linked', False)
                        and not bridge.user32.IsWindow(self.binding.hwnd)):
                    with self.notes.lock:
                        if getattr(self.pump, 'last_scoped_down', 0) > max(self.notes.times.values(), default=0):
                            self.notes.manual('', 'capture_target_unavailable')
                        self.notes.closed = True
                        self.notes.set_state('closed')
                    self.rows.stop()
                    self.worker.abort.set()
                    self.ended = True
                    return
                self.notes.set_state('paused', partial=True)
                if kind == 'fatal':
                    self.notes.manual('', reason or 'capture_interrupted')
        if self.started and not self.ended and self.pump.closed.is_set() and self.notes.state != 'paused':
            self.worker.enabled.clear()
            self.visual.stop()
            self.notes.manual('', 'hook_stopped')
            self.notes.set_state('paused', partial=True)
        if self.notes.limit:
            self.worker.enabled.clear()
            self.pump.stop()
            self.visual.stop()

    def finish(self, *, report_empty=True):
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
                if report_empty and not self.notes.steps:
                    self.notes.manual('', 'empty_recording_requires_review')
                self.notes.closed = True
                return {'status': 'recorded', 'steps': self.notes.snapshot(), 'input_dispatched': False}
        finally:
            self.rows.stop()
            self.visual.stop()
            self.worker.abort.set()

    def cancel(self):
        self.ended = True
        self.rows.stop()
        self.visual.stop()
        self.pump.stop()
        self.worker.enabled.clear()
        self.worker.abort.set()
        with self.notes.lock:
            self.notes.closed = True
            self.notes.steps.clear()
            self.notes.row_fallbacks.clear()
            self.notes.hover_target = None
        return {'status': 'cancelled', 'steps': [], 'input_dispatched': False}


def pick_recording_window(root, session):
    """Let the user choose a live window by title during recording."""
    import tkinter as tk
    from tkinter import ttk
    from capture_windows import window_choices
    import recorder_theme

    dialog = tk.Toplevel(root, name='window_picker')
    dialog.title('열린 창 선택')
    dialog.transient(root)
    theme = recorder_theme.configure(dialog)
    scale = dialog.winfo_fpixels('1i') / 96.0
    panel = ttk.Frame(dialog, padding=round(24 * scale))
    panel.pack(fill='both', expand=True)
    ttk.Label(panel, text='Desktop Select', style='Brand.TLabel').pack(anchor='w')
    ttk.Label(panel, text='이어서 녹화할 창', style='Title.TLabel').pack(anchor='w', pady=(round(4 * scale), round(16 * scale)))
    tools = ttk.Frame(panel)
    tools.pack(fill='x')
    status = tk.StringVar(dialog, value='')
    all_windows = tk.BooleanVar(dialog, value=False)
    scope_title = tk.StringVar(dialog, value='관련 창')
    ttk.Label(tools, textvariable=scope_title, style='Section.TLabel').pack(side='left')
    include = ttk.Checkbutton(tools, name='all_windows', text='다른 프로그램의 창도 보기', variable=all_windows)
    include.pack(side='right')
    table = ttk.Frame(panel, style='Card.TFrame')
    tree = ttk.Treeview(table, name='windows', show='headings', columns=('title', 'exe', 'status'),
                        selectmode='browse', height=7, style='Steps.Treeview')
    unit = theme['font'].measure('가')
    for key, label, width in (('title','창 제목',unit*22), ('exe','프로그램',unit*15), ('status','상태',unit*10)):
        tree.heading(key, text=label, anchor='w')
        tree.column(key, width=width, minwidth=70, anchor='w', stretch=key=='title')
    scroll = ttk.Scrollbar(table, command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    scroll.pack(side='right', fill='y')
    tree.pack(side='left', fill='both', expand=True)
    status_label = ttk.Label(panel, textvariable=status, style='Muted.TLabel', wraplength=round(560 * scale))
    footer = ttk.Frame(panel)
    footer.pack(side='bottom', fill='x', pady=(round(16 * scale), 0))
    status_label.pack(side='bottom', fill='x', pady=(round(8 * scale), 0))
    table.pack(fill='both', expand=True, pady=(round(12 * scale), 0))
    rows, result = {}, None
    loading, scheduled = False, None

    def close(value=None):
        nonlocal result, scheduled
        result = value
        if scheduled is not None:
            root.after_cancel(scheduled)
            scheduled = None
        dialog.destroy()

    def chosen():
        selected = tree.selection()
        return rows.get(selected[0]) if selected else None

    def selection_changed(*_):
        item = chosen()
        usable = item and not item['ambiguous'] and not loading
        use.state(['!disabled'] if usable else ['disabled'])
        if item and item['ambiguous']:
            status.set('같은 프로그램에 같은 제목의 창이 있습니다. 제목을 구분한 뒤 새로 고침하세요.')
        elif item:
            status.set(item['title'])

    def select(*_):
        item = chosen()
        if session.ended:
            close()
        elif item and not loading and not item['ambiguous']:
            try:
                session.active_hint = ''
                if session.connect_choice(item):
                    close('selected')
                else:
                    tree.selection_remove(*tree.selection())
                    use.state(['disabled'])
                    status.set(session.active_hint or '창이 바뀌었습니다. 새로 고침한 뒤 다시 선택하세요.')
            except (bridge.BridgeError, OSError, ValueError):
                tree.selection_remove(*tree.selection())
                use.state(['disabled'])
                status.set('창을 연결하지 못했습니다. 새로 고침한 뒤 다시 선택하세요.')
        return 'break'

    def refresh(*_):
        nonlocal loading, scheduled
        if loading:
            return
        if session.ended:
            close()
            return
        loading = True
        tree.delete(*tree.get_children())
        rows.clear()
        use.state(['disabled'])
        reload_button.state(['disabled'])
        include.state(['disabled'])
        show_all = all_windows.get()
        scope_title.set('모든 창' if show_all else '관련 창')
        status.set('열린 창을 찾고 있습니다…')
        connected, active = session.choice_scope()
        scanned = queue.SimpleQueue()

        def scan():
            try:
                scanned.put(window_choices(connected, active, all_windows=show_all))
            except Exception:
                scanned.put(None)

        def collect():
            nonlocal loading, scheduled
            scheduled = None
            if not dialog.winfo_exists():
                return
            if session.ended:
                close()
                return
            try:
                choices = scanned.get_nowait()
            except queue.Empty:
                scheduled = root.after(40, collect)
                return
            loading, scheduled = False, None
            reload_button.state(['!disabled'])
            include.state(['!disabled'])
            if choices is None:
                status.set('창 목록을 읽지 못했습니다. 새로 고침하세요.')
                return
            for item in choices:
                tags = []
                if item['current']:
                    tags.append('현재 녹화 창')
                elif item['connected']:
                    tags.append('연결됨')
                if item['minimized']:
                    tags.append('최소화')
                if item['ambiguous']:
                    tags.append('동일 제목')
                key = tree.insert('', 'end', values=(item['title'], item['exe'], ', '.join(tags)))
                rows[key] = item
            status.set('이어서 녹화할 창을 선택하세요.' if choices else '현재 선택할 창이 없습니다.')

        threading.Thread(target=scan, name='desktop-select-window-list', daemon=True).start()
        scheduled = root.after(0, collect)

    reload_button = ttk.Button(footer, name='refresh', text='새로 고침', command=refresh)
    reload_button.pack(side='left')
    ttk.Button(footer, name='point', text='화면에서 가리키기', command=lambda:close('point')).pack(side='left', padx=4)
    use = ttk.Button(footer, name='use', text='이 창 선택', command=select, style='Primary.TButton')
    use.pack(side='right')
    ttk.Button(footer, name='cancel', text='취소', command=close, style='Quiet.TButton').pack(side='right', padx=4)
    include.configure(command=refresh)
    recorder_theme.fit_window(dialog, minimum_width=600, minimum_height=360)
    tree.bind('<<TreeviewSelect>>', selection_changed)
    tree.bind('<Return>', select)
    # A double click in empty space must not act on a previously selected row.
    tree.bind('<Double-1>', lambda e:select() if tree.identify_row(e.y) in tree.selection() else None)
    dialog.bind('<Escape>', lambda _:close())
    dialog.bind('<F5>', refresh)
    dialog.protocol('WM_DELETE_WINDOW', close)
    dialog.grab_set()
    tree.focus_set()
    refresh()
    try:
        root.wait_window(dialog)
    finally:
        if scheduled is not None:
            try:
                root.after_cancel(scheduled)
            except tk.TclError:
                pass  # The recorder itself may have closed.
    return result


def capture_one_step(root, owner, title):
    """Observe one demonstration in its original window. A cancelled or incomplete take edits nothing."""
    import tkinter as tk
    from tkinter import ttk
    import recorder_theme
    take = CaptureSession(owner, title)
    dialog = tk.Toplevel(root)
    dialog.title('이 단계 다시 녹화')
    dialog.transient(root)
    recorder_theme.configure(dialog)
    scale = dialog.winfo_fpixels('1i') / 96.0
    panel = ttk.Frame(dialog, padding=round(24 * scale))
    panel.pack(fill='both', expand=True)
    ttk.Label(panel, text='Desktop Select', style='Brand.TLabel').pack(anchor='w')
    ttk.Label(panel, text='이 단계 다시 녹화', style='Title.TLabel').pack(anchor='w', pady=(round(4 * scale), round(14 * scale)))
    ttk.Label(panel, text=title, wraplength=round(460 * scale), style='Muted.TLabel').pack(anchor='w')
    ttk.Label(panel, text='대상 창에서 한 동작을 시연한 뒤 적용을 누르세요.',
              wraplength=round(460 * scale)).pack(anchor='w', pady=round(12 * scale))
    result = None
    error = None
    def accept():
        nonlocal result, error
        try:
            result = take.finish(report_empty=False)['steps']
        except (bridge.BridgeError, OSError, ValueError) as exc:
            error = exc
        finally:
            dialog.destroy()
    ttk.Button(panel, text='취소', command=dialog.destroy, style='Quiet.TButton').pack(side='left')
    ttk.Button(panel, text='이 동작 적용', command=accept, style='Primary.TButton').pack(side='right')
    on_windows = root.tk.call('tk', 'windowingsystem') == 'win32'
    recorder_theme.fit_window(dialog, minimum_height=210)
    try:
        take.start()
        if on_windows:
            root.attributes('-disabled', True)
        root.wait_window(dialog)
    finally:
        if on_windows:
            root.attributes('-disabled', False)
        if not take.ended:
            take.cancel()
        if dialog.winfo_exists():
            dialog.destroy()
    if error:
        raise error
    if result is not None and (len(result) != 1 or take.notes.partial):
        raise ValueError(f'{len(result)}개 동작이 기록됐습니다. 대상 창에서 한 동작을 다시 시연하세요.')
    return result[0] if result else None


def saved_recording_actions(receipt, *, notice=''):
    """Offer revisions after the session's saved result has already been published."""
    import tkinter as tk
    from tkinter import ttk
    import recorder_theme

    task = receipt['task']
    choice = None
    root = tk.Tk()
    root.title('Desktop Select 녹화')
    root.resizable(False, False)
    recorder_theme.configure(root)
    scale = root.winfo_fpixels('1i') / 96.0
    frame = ttk.Frame(root, padding=round(24 * scale))
    frame.grid(sticky='nsew')
    heading = '초안 저장 완료' if receipt.get('draft') else '녹화 저장 완료'
    ttk.Label(frame, text='Desktop Select', style='Brand.TLabel').grid(row=0, column=0, columnspan=2, sticky='w')
    ttk.Label(frame, text=heading, style='Title.TLabel').grid(row=1, column=0, columnspan=2, sticky='w',
                                                          pady=(round(4 * scale), round(16 * scale)))
    card = ttk.Frame(frame, padding=round(16 * scale), style='Card.TFrame')
    card.grid(row=2, column=0, columnspan=2, sticky='ew')
    ttk.Label(card, text=task['name'], wraplength=round(400 * scale),
              style='Card.Section.TLabel').pack(anchor='w')
    ttk.Label(card, text=f"{len(task['steps'])}개 단계 저장됨", style='Card.Muted.TLabel').pack(anchor='w', pady=(round(6 * scale), 0))
    ttk.Label(frame, text=notice or '수정하거나 처음부터 다시 녹화하면 새 사본으로 저장합니다.',
              wraplength=round(420 * scale), style='Muted.TLabel').grid(row=3, column=0, columnspan=2,
                    sticky='w', pady=(round(14 * scale), round(20 * scale)))

    def finish(action=None):
        nonlocal choice
        choice = action
        root.destroy()

    ttk.Button(frame, text='저장한 녹화 수정', command=lambda: finish('edit'),
               style='Primary.TButton').grid(row=4, column=0, sticky='ew', padx=(0, round(8 * scale)))
    ttk.Button(frame, text='처음부터 다시 녹화', command=lambda: finish('restart')).grid(
               row=4, column=1, sticky='ew')
    ttk.Button(frame, text='닫기', command=finish, style='Quiet.TButton').grid(
               row=5, column=1, sticky='e', pady=(round(12 * scale), 0))
    root.bind('<Escape>', lambda _: finish())
    root.protocol('WM_DELETE_WINDOW', finish)
    recorder_theme.fit_window(root, minimum_height=260)
    root.mainloop()
    return choice


def capture(binding, *, title, save_recording=None, save_draft=None, initial_draft=None, initial_step=None,
            on_ready=None, locate_window=None):
    """Block while the user records from a small, local start/finish window."""
    import tkinter as tk
    from tkinter import ttk
    import recorder_theme
    from recorder_menu import PopupMenu
    def new_session():
        return (CaptureTrail(binding, title) if type(getattr(binding, 'hwnd', None)) is int
                else CaptureSession(binding, title))

    if binding is None and initial_draft is None:
        raise ValueError('recording_window_required')
    session = new_session() if binding is not None else None
    if session is not None:
        session.binding.check()
    root = tk.Tk()
    root.title('Desktop Select 녹화')
    result = None
    editing = None
    edits = StepEdits()
    review = None if initial_draft is None else dict(status='recorded', steps=edits.restore_rows(initial_draft),
                                                     input_dispatched=False)
    completed_steps = []
    bound_windows = {}
    picking_window = False
    poll_timer = point_timer = focus_timer = None
    previous_columns = None
    theme = recorder_theme.configure(root)
    font = theme['font']
    scale = root.winfo_fpixels('1i') / 96.0
    def px(value):
        return round(value * scale)
    style = ttk.Style(root)
    message = tk.StringVar(value='시작을 누른 뒤 대상 창에서 원하는 순서대로 조작하세요.')
    phase = tk.StringVar(value='녹화 준비')
    frame = ttk.Frame(root, padding=(px(24), px(20)))
    frame.pack(fill='both', expand=True)
    header = ttk.Frame(frame)
    header.pack(fill='x')
    heading = ttk.Frame(header)
    heading.pack(side='left')
    ttk.Label(heading, text='Desktop Select', style='Brand.TLabel').pack(anchor='w', pady=(0, 3))
    ttk.Label(heading, textvariable=phase, style='Title.TLabel').pack(anchor='w')
    primary = ttk.Frame(header)
    primary.pack(side='right')
    active_title = tk.StringVar(value=title)
    active_window = ttk.Frame(frame, padding=(px(14), px(10)), style='Card.TFrame')
    active_window.pack(fill='x', pady=(px(16), px(10)))
    ttk.Label(active_window, text='작업 창', style='Card.Muted.TLabel').pack(side='left', padx=(0, 12))
    window_caption = ttk.Label(active_window, textvariable=active_title, wraplength=640, style='Card.TLabel')
    window_caption.pack(side='left')
    status_line = ttk.Label(frame, textvariable=message, wraplength=540, style='Muted.TLabel')
    status_line.pack(anchor='w', pady=(0, 10))
    table = ttk.Frame(frame, style='Card.TFrame', padding=1)
    table.pack(fill='both', expand=True)
    listing = ttk.Treeview(table, name='steps', columns=('number', 'window', 'instruction', 'status'),
                           show='headings', selectmode='browse', height=7, style='Steps.Treeview')
    for key, heading, chars in [('number', '단계', 3), ('window', '작업 창', 14),
                                ('instruction', '실행할 동작', 34), ('status', '상태', 9)]:
        width = font.measure('가') * chars + 12
        listing.heading(key, text=heading, anchor='w')
        listing.column(key, width=width, minwidth=min(width, 60), stretch=key == 'instruction', anchor='w')
    listing.tag_configure('unresolved', foreground='#9A6B28')
    scroll = ttk.Scrollbar(table, command=listing.yview)
    listing.configure(yscrollcommand=scroll.set)
    scroll.pack(side='right', fill='y')
    listing.pack(side='left', fill='both', expand=True)
    empty_hint = ttk.Label(table, text='기록한 단계가 여기에 표시됩니다.', style='Card.Muted.TLabel')
    empty_hint.place(relx=.5, rely=.5, anchor='center')
    window_detail = ttk.Label(frame, name='step_window', wraplength=540, style='Muted.TLabel')
    form = ttk.Frame(frame, padding=px(18), style='Card.TFrame')
    form.columnconfigure(1, weight=1)
    alias, instruction, argument = tk.StringVar(), tk.StringVar(), tk.StringVar()
    mode = tk.StringVar(value='문장 해석')
    modes = {'문장 해석': None, '고정값': 'fixed', '실행할 때 입력': 'required'}
    explicit_value = (None, '')
    interpreted_value = (None, '')
    syncing = False
    value_changed = False
    name_issue = False
    edit_title = tk.StringVar(value='선택한 단계')
    ttk.Label(form, textvariable=edit_title, style='Card.Section.TLabel').grid(row=0, column=0, columnspan=2, sticky='w')
    target_summary = tk.StringVar()
    ttk.Label(form, name='target_summary', textvariable=target_summary,
              style='Card.Muted.TLabel').grid(row=0, column=2, sticky='e')
    sentence_entry = ttk.Entry(form, name='instruction', textvariable=instruction, width=32)
    sentence_entry.grid(row=1, column=0, columnspan=3, pady=(10, 6), sticky='ew')
    sentence_hint = tk.StringVar()
    ttk.Label(form, name='instruction_hint', textvariable=sentence_hint,
              style='Card.Muted.TLabel', wraplength=px(540)).grid(
                  row=5, column=0, columnspan=3, sticky='w', pady=(px(6), 0))
    alias_form = ttk.Frame(form, style='Card.TFrame')
    alias_form.columnconfigure(1, weight=1)
    ttk.Label(alias_form, text='화면에서 쓰는 이름', style='Card.TLabel').grid(row=0, column=0, padx=(0, px(10)))
    alias_entry = ttk.Entry(alias_form, name='alias', textvariable=alias, width=20)
    alias_entry.grid(row=0, column=1, sticky='ew')
    name_hint = tk.StringVar()
    ttk.Label(alias_form, textvariable=name_hint, style='Card.Muted.TLabel',
              wraplength=px(560)).grid(row=1, column=0, columnspan=2, sticky='w', pady=(px(8), px(4)))
    ttk.Button(alias_form, name='use_sentence_name', text='설명에서 이름 가져오기',
               command=lambda: link_subject()).grid(row=2, column=1, sticky='w', pady=(px(4), 0))
    choices = ttk.Frame(form, style='Card.TFrame')
    mode_box = ttk.Combobox(choices, name='mode', values=list(modes), state='readonly', textvariable=mode, width=11)
    mode_box.pack(side='left')
    arg_label = ttk.Label(choices, text='값 또는 변수 이름', style='Card.TLabel')
    arg_label.pack(side='left', padx=(6, 2))
    arg_box = ttk.Entry(choices, name='argument', textvariable=argument, width=22)
    arg_box.pack(side='left')
    preview = tk.StringVar()
    preview_button = ttk.Button(form, name='preview', textvariable=preview, style='Summary.TButton',
                                command=lambda: toggle_value(), width=1)
    preview_button.grid(row=2, column=0, columnspan=3, sticky='ew', pady=(2, 3))
    controls = ttk.Frame(frame)
    controls.pack(side='bottom', fill='x', before=table, pady=(16, 0))
    shown_rows = {}
    unfinished = set()

    def steps_now():
        return review['steps'] if review is not None else completed_steps + session.notes.snapshot()

    def show_step_window(step, steps):
        if step is None:
            window_detail.pack_forget()
            return
        scope = step.get('_capture_window', {})
        window_title = scope.get('title', title)
        exe = scope.get('exe') or getattr(binding, 'exe', '')
        if isinstance(exe, str) and exe:
            window_title = f'{window_title} ({exe})'
        if '_openers' in scope:
            origin = '창을 연 단계 지정 필요'
        elif 'after_id' in scope:
            opener = next((i for i, item in enumerate(steps, 1) if item['id'] == scope['after_id']), None)
            origin = f'{opener}단계에서 열린 창' if opener is not None else '창을 연 단계 확인 필요'
        else:
            origin = '기존 창' if scope else '기본 창'
        detail = f'{origin}   {window_title}'
        if window_detail.cget('text') != detail:
            window_detail.configure(text=detail)
        if not window_detail.winfo_manager():
            window_detail.pack(side='bottom', before=table, fill='x', pady=(4, 0))

    def location_repairable(step):
        return (step['id'] in unfinished and
                (step['action'] in ('row-action', 'result-menu', 'visual-click')
                 or learned_tasks.is_recoverable(step)
                 or step.get('recognition', {}).get('reason') == 'row_capture_requires_review'))

    def step_status(step, directive):
        if '_openers' in step.get('_capture_window', {}):
            return '창을 연 단계 지정'
        if directive:
            return '설명 초안' if directive['mode'] == 'draft' else '준비됨'
        if step['action'] == 'manual':
            return '확인 필요'
        if step['action'] == 'teach-target':
            return '값 확인'
        if step.get('recognition', {}).get('result') == 'mismatch':
            return '값 확인'
        return '준비됨'

    def fields(*, normalize=False):
        if editing is not None and editing['action'] == 'result-menu' and not editing.get('ordinary_menu'):
            return alias.get(), '', None, None
        chosen, arg = explicit_value
        sentence = instruction.get()
        if normalize and value_changed and chosen is not None:
            sentence = edits.value_sentence(editing, alias.get(), sentence, chosen, arg)
        return alias.get(), sentence, chosen, arg

    def show_error(exc):
        if isinstance(exc, learned_tasks.TaskError):
            reason = {'instruction_target_mismatch': '단계 편집의 ‘대상 이름 수정’에서 화면에 보이는 이름을 연결하거나 ‘이 항목을 N으로 설정’처럼 입력하세요.',
                      'instruction_target_unavailable': '이 단계는 보호된 입력이나 별도 절차가 필요한 동작입니다. 일반 대상 인식 실패는 설명을 저장한 뒤 실행할 때 다시 찾을 수 있습니다.',
                      'instruction_requires_structure': '값을 문장에 적거나 이 줄을 눌러 지정하세요.',
                      'invalid_step_alias': '다른 대상 이름을 입력하세요.'}.get(
                          exc.code, '이 단계에서 실행할 수 있는 문장과 값을 입력하세요.')
            preview.set(reason + ' (' + exc.code + ')')
        else:
            preview.set(str(exc))

    def display_value(chosen, arg):
        nonlocal syncing
        syncing = True
        try:
            mode.set(next(label for label, value in modes.items() if value == chosen))
            argument.set(arg or '')
        finally:
            syncing = False
        arg_box.configure(state='disabled' if chosen is None else 'normal')
        arg_label.configure(text='변수 이름' if chosen == 'required' else '값')

    def toggle_value():
        if choices.winfo_manager():
            choices.grid_remove()
        else:
            alias_form.grid_remove()
            display_value(*(explicit_value if explicit_value[0] is not None else interpreted_value))
            choices.grid(row=3, column=0, columnspan=3, sticky='w', pady=(0, 5))
            mode_box.focus_set()
        refresh_preview()

    def edit_alias():
        choices.grid_remove()
        selector = learned_tasks._recorded_selector(editing.get('saved', editing)) or {}
        identifier = selector.get('automation_id') or selector.get('name') or selector.get('query')
        target = f'현재 인식된 요소는 {identifier}입니다. ' if identifier else ''
        name_hint.set(target + '이 요소가 화면에서 어떤 이름인지 연결합니다. 예를 들어 MES Setup이라고 보이면 같은 이름을 입력하세요.')
        alias_form.grid(row=3, column=0, columnspan=3, sticky='ew', pady=(0, 5))
        alias_entry.focus_set()
        alias_entry.selection_range(0, 'end')
        refresh_preview()

    def refresh_preview(*_):
        nonlocal interpreted_value, name_issue
        if editing is not None and not syncing:
            name_issue = False
            try:
                data = edits.compile(editing, *fields(normalize=True))
                compiled = update_readiness(editing, data) if review is not None else None
                summary, chosen, arg, editable = review_interpretation(editing, data, compiled=compiled)
                interpreted_value = chosen, arg
                if editable:
                    summary += '  ▴' if choices.winfo_manager() else '  ▾'
                preview.set(summary)
            except (ValueError, bridge.BridgeError, KeyError) as exc:
                name_issue = getattr(exc, 'code', None) == 'instruction_target_mismatch'
                show_error(exc)
                unfinished.add(editing['id'])
                interpreted_value = None, ''
                # An old draft can carry an invalid explicit value on any kind
                # of step. Keep it editable so the user can clear that override.
                editable = explicit_value[0] is not None
                if not editable:
                    try:
                        editable = review_interpretation(editing, {})[3]
                    except (ValueError, bridge.BridgeError, KeyError):
                        pass
            preview_button.configure(state='normal' if editable else 'disabled',
                                     cursor='hand2' if editable else '', takefocus=editable)
            if not editable:
                choices.grid_remove()
            selector = learned_tasks._recorded_selector(editing.get('saved', editing)) or {}
            subject = alias.get().strip() or editing.get('field_label') or selector.get('name') or selector.get('automation_id')
            caption = f'대상  {subject[:50]}' if isinstance(subject, str) and subject else '실행할 때 대상 확인'
            target_summary.set('대상 이름 확인 필요' if name_issue else caption)
            if review is not None:
                update_save_button(review['steps'])
                update_step_menu(editing)

    def change_value(*_):
        nonlocal explicit_value, value_changed
        if syncing or editing is None:
            return
        chosen = modes[mode.get()]
        explicit_value = chosen, argument.get() if chosen else ''
        value_changed = chosen is not None
        if chosen is None:
            display_value(None, '')
        else:
            arg_box.configure(state='normal')
            arg_label.configure(text='변수 이름' if chosen == 'required' else '값')
        refresh_preview()

    def change_sentence(*_):
        nonlocal explicit_value, value_changed
        if syncing or editing is None:
            return
        # An unfinished variable name belongs to its recoverable draft.
        # An empty fixed string, however, is a valid old instruction to clear.
        if explicit_value != ('required', ''):
            explicit_value = None, ''
        value_changed = False
        refresh_preview()
        if choices.winfo_manager():
            display_value(*(explicit_value if explicit_value[0] is not None else interpreted_value))

    def flush():
        nonlocal syncing, value_changed
        if editing is not None:
            try:
                values = fields(normalize=True)
            except (ValueError, bridge.BridgeError) as exc:
                # StepEdits.store keeps even invalid text for a draft save.
                try:
                    edits.store(editing, *fields())
                except (ValueError, bridge.BridgeError):
                    pass
                show_error(exc)
                return False
            try:
                edits.store(editing, *values)
            except (ValueError, bridge.BridgeError) as exc:
                show_error(exc)
                return False
            if value_changed:
                syncing = True
                try:
                    instruction.set(values[1])
                finally:
                    syncing = False
                value_changed = False
        return True

    def load(step):
        nonlocal syncing, explicit_value, value_changed
        sentence, chosen, arg = edits.form(step)
        fixed_menu = step['action'] == 'result-menu' and not step.get('ordinary_menu')
        sentence_entry.configure(state='readonly' if fixed_menu else 'normal')
        sentence_hint.set('시연한 메뉴 순서를 사용합니다. 변경하려면 단계 편집에서 다시 녹화하세요.' if fixed_menu else
                          'Enter는 입력 확정, Ctrl+Enter는 적용하고 다음 단계')
        explicit_value = chosen, arg or ''
        value_changed = False
        choices.grid_remove()
        alias_form.grid_remove()
        syncing = True
        try:
            alias.set(edits.alias(step))
            instruction.set(step['label'] if fixed_menu else sentence)
        finally:
            syncing = False
        display_value(chosen, arg)
        selected_number = next((i for i, s in enumerate(steps_now(), 1) if s['id'] == step['id']), '')
        edit_title.set(f'{selected_number}단계 실행할 동작')
        show_step_window(step, steps_now())
        update_step_menu(step)
        refresh_preview()

    def select_row(*_):
        nonlocal editing
        selected = listing.selection()
        delete_button.state(['!disabled'] if selected and review is not None else ['disabled'])
        if not selected or (editing and editing['id'] == selected[0]):
            return
        flush()
        editing = next((step for step in steps_now() if step['id'] == selected[0]), None)
        if editing is not None:
            load(editing)
            if review is not None:
                render(review['steps'])

    def update_readiness(step, data):
        compiled = learned_tasks.recorded_step(edits.edited_step(step, data))
        if compiled['action'] == 'manual' or 'instruction_draft' in compiled:
            unfinished.add(step['id'])
        else:
            unfinished.discard(step['id'])
        return compiled

    def update_save_button(steps):
        label = '초안 저장' if unfinished or unresolved_window(steps) else '저장'
        save.configure(text=label, state='normal' if steps else 'disabled')

    def update_review_status(steps):
        """Name a draft as such when its saved instructions still require review."""
        unfinished.clear()
        for step in steps:
            try:
                raw = edits.compile(step, edits.alias(step), *edits.form(step))
                update_readiness(step, raw)
            except (ValueError, bridge.BridgeError):
                unfinished.add(step['id'])
        missing = len(unfinished)
        unlinked = unresolved_window(steps)
        update_save_button(steps)
        phase.set(f'단계 검토 ({len(steps)}개)')
        message.set(f'{unlinked}단계의 창 연결을 나중에 지정하려면 초안 저장을 누르세요.' if unlinked else
                    f'{missing}개 단계가 확인 필요 상태입니다. 설명을 확인하고 초안 저장을 누르세요.' if missing
                    else '실행 문장을 확인하고 저장하세요.' if steps else '단계를 추가하거나 취소하세요.')

    def render(steps):
        nonlocal editing
        if steps:
            empty_hint.place_forget()
        else:
            empty_hint.place(relx=.5, rely=.5, anchor='center')
        edit_button.state(['!disabled'] if review is not None else ['disabled'])
        if review is not None:
            update_review_status(steps)
        keep = {step['id'] for step in steps}
        for row_id in listing.get_children():
            if row_id not in keep:
                listing.delete(row_id)
                shown_rows.pop(row_id, None)
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
            text, _, _ = edits.form(step)
            if not text:
                text = step['label'] if step['label'] != '설명으로 다시 찾을 단계' else '대상 이름을 읽지 못한 클릭'
            state = step_status(step, directive)
            if review is not None and step['id'] in unfinished and state == '준비됨':
                state = '확인 필요'
            row = (i, step.get('_capture_window', {}).get('title', title), text, state)
            if shown_rows.get(step['id']) != row:
                tags = () if state == '준비됨' else ('unresolved',)
                if listing.exists(step['id']):
                    listing.item(step['id'], values=row, tags=tags)
                else:
                    listing.insert('', i - 1, iid=step['id'], values=row, tags=tags)
                shown_rows[step['id']] = row
        if steps and not listing.selection():
            listing.selection_set(steps[0]['id'])
        show_step_window(editing, steps)

    def apply_text():
        flush()
        render(steps_now())
        refresh_preview()

    def focus_instruction_after_idle():
        # Tree selection events must load the next row before typing resumes.
        nonlocal focus_timer
        if focus_timer is not None:
            root.after_cancel(focus_timer)
        selected = listing.selection()
        def focus():
            nonlocal focus_timer
            focus_timer = None
            if selected and listing.selection() == selected:
                select_row()  # A newly mapped tree may not have delivered its selection event yet.
                if (editing is not None and editing['id'] == selected[0]
                        and str(sentence_entry.cget('state')) == 'normal'):
                    sentence_entry.focus_set()
        focus_timer = root.after_idle(focus)

    def apply_next():
        flush()
        steps = steps_now()
        render(steps)
        index = next((i for i, step in enumerate(steps) if editing and step['id'] == editing['id']), -1)
        if 0 <= index < len(steps) - 1:
            listing.selection_set(steps[index + 1]['id'])
            listing.see(steps[index + 1]['id'])
        else:
            if save.cget('text') == '저장':
                message.set('마지막 단계까지 적용했습니다. 저장을 누르면 완료됩니다.')
        focus_instruction_after_idle()

    def link_subject():
        if editing is None:
            return
        subject = learned_tasks.instruction_subject(instruction.get())
        if not subject:
            edit_alias()
            preview.set('설명에서 이름을 찾지 못했습니다. 화면에서 보이는 이름을 입력하세요.')
            return
        alias.set(subject)
        apply_text()

    def delete_step():
        nonlocal editing
        selected = listing.selection()
        if editing is None or not selected or selected[0] != editing['id']:
            return
        if review is None:
            message.set('마치고 검토를 누른 뒤 불필요한 단계를 삭제하세요.')
            return
        removed = editing['id']
        old_index = next(i for i, s in enumerate(review['steps']) if s['id'] == removed)
        review['steps'][:] = [step for step in review['steps'] if step['id'] != removed]
        prior = []
        for step in review['steps']:
            scope = step.get('_capture_window', {})
            if scope.get('after_id') == removed:
                del scope['after_id']
                scope['_openers'] = tuple(prior)
            elif removed in scope.get('_openers', ()):
                scope['_openers'] = tuple(key for key in scope['_openers'] if key != removed)
            prior.append(step['id'])
        edits.forget(removed)
        editing = None
        alias.set('')
        instruction.set('')
        argument.set('')
        preview.set('')
        render(review['steps'])
        if review['steps']:
            key = review['steps'][min(old_index, len(review['steps']) - 1)]['id']
            listing.selection_set(key)
            listing.see(key)
        else:
            form.pack_forget()
            save.configure(state='disabled')

    def move_step(offset):
        if review is None or editing is None:
            return
        from capture_trail import resolve_menu_scope
        steps = review['steps']
        index = next(i for i, s in enumerate(steps) if s['id'] == editing['id'])
        dest = index + offset
        if not 0 <= dest < len(steps):
            return
        ordered = steps.copy()
        ordered[index], ordered[dest] = ordered[dest], ordered[index]
        positions = {s['id']: n for n, s in enumerate(ordered, 1)}
        for n, s in enumerate(steps, 1):
            scope = s.get('_capture_window', {})
            opener = resolve_menu_scope(scope, steps, n).get('after_id')
            if opener and (positions.get(opener, 0) >= positions[s['id']]
                           or resolve_menu_scope(scope, ordered, positions[s['id']]).get('after_id') != opener):
                message.set('이 단계는 작업 창을 연 단계 뒤에 있어야 합니다.')
                return
        flush()
        steps[:] = ordered
        listing.move(editing['id'], '', dest)
        listing.see(editing['id'])
        render(steps)
        load(editing)

    def relearn_step():
        nonlocal editing
        if review is None or editing is None:
            return
        flush()
        source = editing
        scope = source.get('_capture_window')
        target_title = scope['title'] if scope else title
        try:
            owner = bound_windows.get(id(scope))
            if owner is None and isinstance(session, CaptureTrail):
                owner = session.binding_for(source)
            elif owner is None and session is not None and not scope:
                owner = session.binding
            if owner is not None:
                try:
                    owner.check()
                except bridge.BridgeError:
                    owner = None
            if owner is None:
                if locate_window is None:
                    raise ValueError('작업 창을 다시 열어 주세요.')
                owner = locate_window(scope)
            replacement = capture_one_step(root, owner, target_title)
        except (bridge.BridgeError, OSError, ValueError) as exc:
            message.set(f'{target_title}에서 다시 녹화하지 못했습니다 ({exc}).')
            return
        if replacement is None:
            return
        key = source['id']
        if source['action'] == 'saved-step':
            from review_drafts import retain_saved_arguments
            try:
                replacement = retain_saved_arguments(source, replacement)
            except (bridge.BridgeError, ValueError):
                message.set('이번 시연에 기존 값이나 변수를 적용할 수 없어 이전 단계를 유지했습니다.')
                return
        replacement = dict(replacement, id=key)
        if scope:
            replacement['_capture_window'] = scope
        text, chosen, arg = edits.form(source)
        nickname = edits.alias(source)
        text = text or default_text(replacement)
        def executable(raw, fields):
            try:
                compiled = learned_tasks.recorded_step(edits.edited_step(raw, fields))
                return compiled['action'] != 'manual' and 'instruction_draft' not in compiled
            except (bridge.BridgeError, ValueError):
                return False
        try:
            proposed = edits.compile(replacement, nickname, text, chosen, arg)
        except (bridge.BridgeError, ValueError):
            proposed = None
        if executable(source, edits.get(source)) and (proposed is None or not executable(replacement, proposed)
                or replacement['action'] == 'manual' and not replacement.get('visual_hint')):
            message.set('이번 시연으로 기존 지시를 실행할 대상을 확인하지 못해 이전 단계를 유지했습니다.')
            return
        edits.forget(key)
        try:
            edits.store(replacement, nickname, text, chosen, arg)
        except (bridge.BridgeError, ValueError):
            pass  # store preserves the original fields for draft review.
        steps = review['steps']
        steps[steps.index(source)] = replacement
        editing = replacement
        render(steps)
        load(editing)
        message.set('선택한 단계에 새 동작을 적용했습니다. 저장하면 반영됩니다.')

    listing.bind('<<TreeviewSelect>>', select_row)
    alias.trace_add('write', refresh_preview)
    instruction.trace_add('write', change_sentence)
    mode.trace_add('write', change_value)
    argument.trace_add('write', change_value)
    editing_controls = ttk.Frame(form, style='Card.TFrame')
    editing_controls.grid(row=4, column=0, columnspan=3, sticky='ew', pady=(4, 0))
    edit_button = ttk.Menubutton(editing_controls, name='step_tools', text='단계 편집', style='Quiet.TMenubutton')
    step_tools = PopupMenu(edit_button, tearoff=False)
    repair_menu = PopupMenu(step_tools, tearoff=False)
    repair_menu.add_command(label='행 버튼 지정', description='행과 버튼, 실행 후 상태를 지정합니다.',
                            command=lambda: teach_extra('row', replace=True))
    repair_menu.add_command(label='우클릭 메뉴 지정', description='우클릭할 지점과 메뉴 순서를 지정합니다.',
                            command=lambda: teach_extra('menu', replace=True))
    step_tools.bind_to(edit_button)
    edit_button.pack(side='left')
    delete_button = ttk.Button(editing_controls, name='delete_step', text='선택 단계 삭제',
                               style='Danger.TButton', command=delete_step, state='disabled')
    delete_button.pack(side='left', padx=(px(8), 0))

    def update_step_menu(step):
        step_tools.delete(0, 'end')
        step_tools.add_command(label='이 단계 다시 녹화', description='선택한 동작 하나를 다시 시연합니다.', command=relearn_step)
        step_tools.add_command(label='위로 이동', command=lambda: move_step(-1))
        step_tools.add_command(label='아래로 이동', command=lambda: move_step(1))
        selector = learned_tasks._recorded_selector(step.get('saved', step))
        scope = step.get('_capture_window', {})
        if name_issue or location_repairable(step) or '_openers' in scope or 'after_id' in scope:
            step_tools.add_separator()
        if name_issue and selector and any(selector.get(key) for key in ('automation_id', 'name', 'query')):
            step_tools.add_command(label='대상 이름 수정', description='화면의 이름과 인식된 요소가 다른 경우에만 수정합니다.', command=edit_alias)
        if (session is not None or locate_window is not None) and location_repairable(step):
            step_tools.add_cascade(label='위치 직접 보완', menu=repair_menu)
        if '_openers' in scope or 'after_id' in scope:
            step_tools.add_command(label='창을 연 단계 지정', command=choose_opener)

    def choose_opener():
        if not editing:
            return
        scope = editing.get('_capture_window', {})
        choices = []
        for number, step in enumerate(steps_now(), 1):
            # One choice updates the whole window, including its earliest step.
            if step.get('_capture_window') == scope:
                break
            if '_openers' in scope and step['id'] not in scope['_openers']:
                continue
            try:
                data = edits.compile(step, edits.alias(step), *edits.form(step))
                action = learned_tasks.recorded_step(dict(step, **data))['action']
            except (ValueError, bridge.BridgeError):
                continue
            if action not in ('manual', 'wait', 'verify-file', *learned_tasks.OBSERVE_ACTIONS):
                choices.append((step['id'], f'{number}단계   {step["label"]}'))
        dialog = tk.Toplevel(root)
        dialog.title('창을 연 단계 지정')
        dialog.transient(root)
        recorder_theme.configure(dialog)
        panel = ttk.Frame(dialog, padding=px(24))
        panel.pack(fill='both', expand=True)
        ttk.Label(panel, text='Desktop Select', style='Brand.TLabel').pack(anchor='w')
        ttk.Label(panel, text='창을 연 단계 지정', style='Title.TLabel').pack(anchor='w', pady=(px(4), px(14)))
        ttk.Label(panel, text=f'{scope["title"]} 창을 언제 열지 선택하세요.',
                  style='Muted.TLabel', wraplength=px(480)).pack(anchor='w')
        picker = ttk.Combobox(panel, state='readonly', width=44,
                              values=[label for _, label in choices] + ['실행 전에 직접 열어 둘 창'])
        picker.pack(fill='x', pady=8)
        def select():
            if picker.current() < 0:
                return
            flush()
            if picker.current() == len(choices):
                scope['mode'] = 'existing'
                scope.pop('related', None)
                scope.pop('after_id', None)
            else:
                scope['after_id'] = choices[picker.current()][0]
            scope.pop('_openers', None)
            dialog.destroy()
            load(editing)
            render(steps_now())
            message.set('이 창의 모든 단계에 반영했습니다.')
        footer = ttk.Frame(panel)
        footer.pack(fill='x', pady=(px(16), 0))
        ttk.Button(footer, text='취소', command=dialog.destroy, style='Quiet.TButton').pack(side='left')
        ttk.Button(footer, text='연결', command=select, style='Primary.TButton').pack(side='right')
        dialog.bind('<Escape>', lambda _event: dialog.destroy())
        dialog.grab_set()
        picker.focus_set()
        recorder_theme.fit_window(dialog, minimum_height=240)
    ttk.Button(editing_controls, text='적용하고 다음 단계', command=apply_next).pack(side='right')
    def apply_next_key(_event):
        apply_next()
        return 'break'
    # Plain Enter belongs to the IME when it confirms a Korean composition.
    sentence_entry.bind('<Control-Return>', apply_next_key)

    def start(*_):
        nonlocal result
        if review is not None or session.started:
            return
        remaining = learned_tasks.MAX_STEPS - len(completed_steps)
        if remaining < 1:
            message.set(f'최대 {learned_tasks.MAX_STEPS}단계에 도달했습니다. 마치고 검토를 누르세요.')
            return
        session.notes.max_steps = remaining
        try:
            session.start()
        except Exception as exc:
            diagnostic = exc.code if isinstance(exc, bridge.BridgeError) else 'recording_start_failed'
            result = {'status': 'blocked', 'diagnostic': diagnostic, 'input_dispatched': False}
            root.destroy()
            return
        update_phase()

    def finish(*_):
        nonlocal review
        if review is not None or not (session.started or completed_steps):
            return
        review = {'status': 'recorded', 'steps': completed_steps.copy(), 'input_dispatched': False}
        if session.started:
            review['steps'].extend(session.finish(report_empty=not completed_steps)['steps'])
        render(review['steps'])
        update_phase()
        focus_instruction_after_idle()

    def choose_window():
        nonlocal picking_window, point_timer
        if not isinstance(session, CaptureTrail) or not session.started or session.ended or review is not None or picking_window:
            return
        from capture_windows import pointed_key
        original = session
        picking_window = True
        try:
            choice = pick_recording_window(root, session)
        finally:
            picking_window = False
        if review is not None or session is not original or session.ended:
            return
        if choice == 'selected':
            active_title.set(session.binding.title)
            session.active_hint = session.active_hint or f'{session.binding.title} 창에서 이어서 녹화합니다.'
            message.set(session.active_hint)
            return
        if choice != 'point':
            return
        picking_window = True
        message.set('3초 동안 이어서 녹화할 창 위에 마우스를 두세요. 클릭할 필요가 없습니다.')
        def connect_pointed():
            nonlocal picking_window
            picking_window = False
            if review is not None or session is not original or session.ended:
                return
            try:
                session.active_hint = ''
                if not session.connect(pointed_key()):
                    session.active_hint = session.active_hint or '창을 구분할 수 없습니다. 열린 창의 제목을 확인하고 다시 가리키세요.'
                    message.set(session.active_hint)
                    return
                active_title.set(session.binding.title)
                message.set(f'{session.binding.title} 창이 연결됐습니다. 이 창에서 작업하세요.')
            except (bridge.BridgeError, OSError, ValueError):
                session.active_hint = '선택한 창이 바뀌었습니다. 이어갈 창을 다시 선택하세요.'
                message.set(session.active_hint)
        point_timer = root.after(3000, connect_pointed)

    def add_instruction(*, after_selected=False):
        nonlocal review
        flush()
        current = steps_now()
        selected = listing.selection()
        anchor = next((step for step in current if selected and step['id'] == selected[0]), None)
        if after_selected and anchor is None:
            return
        if not after_selected:
            anchor = current[-1] if current else None
        scope = anchor.get('_capture_window') if anchor else getattr(session, 'scope', None)
        if review is None:
            if session.started:
                completed_steps.extend(session.finish(report_empty=False)['steps'])
            review = {'status': 'recorded', 'steps': completed_steps.copy(), 'input_dispatched': False}
        if len(review['steps']) >= learned_tasks.MAX_STEPS:
            message.set(f'작업에는 최대 {learned_tasks.MAX_STEPS}단계를 저장할 수 있습니다.')
            return
        step = dict(manual('capture_target_unavailable'), label='직접 입력한 단계')
        if scope:
            step['_capture_window'] = scope
        position = review['steps'].index(anchor) + 1 if after_selected else len(review['steps'])
        review['steps'].insert(position, step)
        render(review['steps'])
        update_phase()
        listing.selection_set(step['id'])
        listing.see(step['id'])
        select_row()
        sentence_entry.focus_set()

    def teach_extra(kind, *, replace=False):
        nonlocal session, previous_columns, editing
        if session is None and locate_window is None:
            return
        if replace and (review is None or editing is None or not location_repairable(editing)):
            return
        if replace:
            flush()  # Keep even an uncompiled sentence while replacing its missing target.
            source = editing
            sentence, _, _ = edits.form(source)
            nickname = edits.alias(source)
        if not replace and not flush():
            return
        replace_id = editing['id'] if replace else None
        if review is None and session.started:
            if isinstance(session, CaptureTrail):
                for part, scope in session.parts:
                    if scope:
                        bound_windows[id(scope)] = part.binding
            completed_steps.extend(session.finish(report_empty=False)['steps'])
            last_binding, last_scope = session.binding, getattr(session, 'scope', None)
            session = session.successor() if isinstance(session, CaptureTrail) else new_session()
            render(completed_steps)
            update_phase()
        elif session is not None:
            last_binding, last_scope = session.binding, getattr(session, 'scope', None)
        if replace:
            last_scope = editing.get('_capture_window')
            remembered = bound_windows.get(id(last_scope))
            last_binding = remembered or (session.binding_for(editing) if isinstance(session, CaptureTrail)
                                         else session.binding if session is not None and not last_scope else None)
            if last_binding is None and locate_window is not None:
                try:
                    last_binding = locate_window(last_scope)
                except (bridge.BridgeError, OSError, ValueError) as exc:
                    message.set(f'이 단계의 작업 창을 연결하지 못했습니다 ({exc}).')
                    return
            if last_binding is None:
                message.set('해당 단계를 녹화한 창을 다시 연결해야 합니다.')
                return
        steps = review['steps'] if review is not None else completed_steps
        if not replace and len(steps) >= learned_tasks.MAX_STEPS:
            message.set(f'작업에는 최대 {learned_tasks.MAX_STEPS}단계를 저장할 수 있습니다.')
            return
        if kind == 'row':
            from row_teach import teach_row_dialog
            answer = teach_row_dialog(root, last_binding, previous_columns)
            step = None
            if answer:
                step, previous_columns = answer
        else:
            from context_teach import previous_trigger, teach_context_dialog
            step = teach_context_dialog(root, last_binding)
            if (not replace and step and steps and steps[-1].get('_capture_window') == last_scope
                    and previous_trigger(steps[-1], step['context'])):
                if recorder_theme.confirm(root, '조회 한 번으로 합치기',
                        '바로 앞 단계에서 같은 조회 버튼을 눌렀습니다. 기존 조회 단계를 이번 조회와 우클릭 단계로 교체할까요?',
                        confirm_text='한 단계로 합치기'):
                    previous = steps.pop()
                    step = dict(step, id=previous['id'])
                    edits.forget(previous['id'])
                    if editing is not None and editing['id'] == previous['id']:
                        editing = None
                    if listing.exists(previous['id']):
                        listing.delete(previous['id'])
                        shown_rows.pop(previous['id'], None)
                else:
                    step = None
        if step is not None:
            if last_scope:
                step['_capture_window'] = last_scope
            if replace:
                index = next(i for i, s in enumerate(steps) if s['id'] == replace_id)
                # Keep the demonstrated position and identity. Only the step the
                # user explicitly repaired gets a new executable definition.
                step = dict(step, id=replace_id)
                if sentence.strip() and len(sentence.strip()) <= 200:
                    step['label'] = sentence.strip()
                if sentence.strip():
                    step['description'] = sentence.strip()
                try:
                    learned_tasks.recorded_step(step)
                except (ValueError, bridge.BridgeError) as exc:
                    message.set(f'보완 내용을 적용하지 못했습니다. 기존 설명은 유지됩니다 ({exc}).')
                    return
                steps[index] = step
                edits.forget(replace_id)
                edits.store(step, nickname, step['label'])
                editing = None
            else:
                steps.append(step)
            render(steps)
            listing.selection_set(step['id'])
            listing.see(step['id'])
            select_row()
        update_phase()

    def unresolved_window(steps):
        from capture_trail import resolve_menu_scope
        preceding = set()
        reviewed = None
        for number, step in enumerate(steps, 1):
            scope = step.get('_capture_window', {})
            if '_openers' in scope:
                if reviewed is None:
                    reviewed = [edits.edited_step(s, edits.get(s)) for s in steps]
                scope = resolve_menu_scope(scope, reviewed, number)
            if '_openers' in scope or ('after_id' in scope and scope['after_id'] not in preceding):
                return number
            preceding.add(step['id'])

    def persist_review(steps):
        """Preserve editable rows if they cannot yet form a valid task."""
        number = unresolved_window(steps)
        if number:
            return persist_editor_rows(steps, learned_tasks.TaskError('recorded_window_opener_required'), number)
        try:
            final_steps = finalize_contexts(edits.apply(steps))
        except (ValueError, bridge.BridgeError) as exc:
            number = next((i for i, s in enumerate(steps, 1) if s['id'] == edits.failed_step), None)
            return persist_editor_rows(steps, exc, number)
        if save_recording is None:
            return dict(review, steps=final_steps)
        try:
            return save_recording(final_steps)
        except ValueError as exc:
            if not getattr(exc, 'step_number', None):
                raise
            return persist_editor_rows(steps, exc, exc.step_number)

    def persist_editor_rows(steps, issue, number):
        if save_draft is not None:
            return save_draft(edits.review_rows(steps), getattr(issue, 'code', 'invalid_editor_fields'), number)
        if number and 1 <= number <= len(steps):
            listing.selection_set(steps[number - 1]['id'])
            listing.see(steps[number - 1]['id'])
        show_error(issue)
        return None

    def save_review():
        nonlocal result
        if review is None or not review['steps']:
            return
        try:
            flush()  # store() keeps even uncompiled text, including the active entry.
            receipt = persist_review(review['steps'])
        except Exception as exc:
            # This is the persistence boundary: leave the editor and inputs
            # alive after disk errors and unexpected serializer failures alike.
            code = getattr(exc, 'code', 'task_store_unavailable' if isinstance(exc, OSError) else 'recording_save_failed')
            number = getattr(exc, 'step_number', None)
            location = f'{number}단계 ' if number else ''
            message.set(f'{location}파일을 저장하지 못했습니다 ({code}). 작성 내용이 이 창에 있습니다. 다시 저장하세요.')
            return
        if receipt is not None:
            result = receipt
            root.destroy()

    def cancel(*_):
        nonlocal result
        result = session.cancel() if session is not None else None
        root.destroy()

    def tick():
        nonlocal poll_timer
        if review is None and session.started:
            session.poll()
            if isinstance(session, CaptureTrail):
                active_title.set(session.binding.title)
            steps = steps_now()
            render(steps)
            warnings = sum(step_status(s, edits.get(s).get('instruction')) != '준비됨' for s in steps)
            phase.set('녹화 일시 정지' if session.notes.state == 'paused' or session.notes.limit else '녹화 중')
            if not picking_window:
                hint = getattr(session, 'active_hint', '')
                message.set(f'{len(steps)}단계 기록' + (f'   ·   확인 필요 {warnings}개' if warnings else '')
                            + (f'   {hint}' if hint else ''))
            if steps and steps[-1]['id'] != tick.last_id:
                listing.see(steps[-1]['id'])
                tick.last_id = steps[-1]['id']
        if review is None:
            poll_timer = root.after(200, tick)

    tick.last_id = None
    begin = ttk.Button(primary, text='시작', command=start, style='Primary.TButton')
    link_window = ttk.Button(primary, text='열린 창 선택', command=choose_window)
    end = ttk.Button(primary, text='마치고 검토', command=finish, style='Primary.TButton')
    save = ttk.Button(primary, text='저장', command=save_review, style='Primary.TButton')
    add_button = ttk.Menubutton(controls, name='add_step', text='단계 추가', style='Add.TMenubutton')
    add_menu = PopupMenu(add_button, tearoff=False)
    add_menu.add_command(label='선택한 단계 아래에 추가', description='선택한 단계 바로 뒤에 설명을 입력합니다.',
                         command=lambda: add_instruction(after_selected=True))
    add_menu.add_command(label='맨 아래에 추가', description='마지막 단계 뒤에 설명을 입력합니다.', command=add_instruction)
    def update_add_menu():
        selected = listing.selection()
        exists = bool(selected and any(step['id'] == selected[0] for step in steps_now()))
        add_menu.entryconfigure(0, state='normal' if exists else 'disabled')
    add_menu.configure(postcommand=update_add_menu)
    add_menu.bind_to(add_button)
    add_button.pack(side='left')
    ttk.Button(controls, text='취소', command=cancel, style='Quiet.TButton').pack(side='right')

    def update_phase():
        for button in (begin, end, save, link_window):
            button.pack_forget()
        if review is not None:
            count = len(review['steps'])
            update_review_status(review['steps'])
            save.pack()
            if count:
                form.pack(side='bottom', fill='x', after=controls, pady=(8, 0))
            else:
                form.pack_forget()
        elif session.started:
            phase.set('녹화 중')
            message.set('대상 창에서 동작을 수행한 뒤 마치고 검토를 누르세요.')
            if isinstance(session, CaptureTrail):
                link_window.pack(side='left', padx=(0, 5))
            end.pack(side='left')
        else:
            phase.set('녹화 준비' if not completed_steps else '녹화 일시 정지')
            if completed_steps:
                begin.configure(text='이어 녹화')
                begin.pack(side='left', padx=(0, 4))
                end.pack(side='left')
                message.set(f'{len(completed_steps)}단계 기록')
            else:
                begin.configure(text='시작')
                begin.pack()
        recorder_theme.fit_window(root, minimum_width=640, minimum_height=400)

    if review is not None:
        render(review['steps'])
        if type(initial_step) is int and 1 <= initial_step <= len(review['steps']):
            item = review['steps'][initial_step - 1]['id']
            listing.selection_set(item)
            listing.see(item)
            select_row()
    update_phase()
    if review is not None:
        focus_instruction_after_idle()
    def resize(event):
        if event.widget is root:
            status_line.configure(wraplength=max(210, event.width - 40))
            window_detail.configure(wraplength=max(210, event.width - 40))
            window_caption.configure(wraplength=max(200, event.width - 160))
            style.configure('Summary.TButton', wraplength=max(160, event.width - 84))
    root.bind('<Configure>', resize)
    root.bind('<Alt-s>', start)
    root.bind('<Alt-f>', finish)
    root.protocol('WM_DELETE_WINDOW', cancel)
    def failed(*_):
        nonlocal result
        result = {'status': 'blocked', 'diagnostic': 'recording_ui_failed', 'input_dispatched': False}
        root.destroy()
    root.report_callback_exception = failed
    if on_ready is not None:
        def report_ready(event=None):
            nonlocal on_ready
            if event is not None and event.widget is not root:
                return
            if on_ready is not None and root.winfo_viewable():
                callback, on_ready = on_ready, None
                callback()

        root.bind('<Map>', report_ready, add='+')
        report_ready()  # Some window managers may have mapped the editor during setup.
    if review is None:
        poll_timer = root.after(200, tick)
    try:
        root.mainloop()
        return result or {'status': 'cancelled', 'steps': [], 'input_dispatched': False}
    finally:
        for timer in (poll_timer, point_timer, focus_timer):
            if timer:
                root.after_cancel(timer)
        if session is not None and not session.ended:
            session.cancel()
