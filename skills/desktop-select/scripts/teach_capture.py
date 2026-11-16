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


def capture_step(step):
    """Copy only data used by a Desktop Select command. All other inputs are review stops."""
    if step['action'] == 'visual-click':
        from visual_target import validate_spec
        return dict(id=step['id'], action='visual-click', label=step['label'],
                    visual=copy.deepcopy(validate_spec(step['visual'])))
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
                 or (action == 'click' and role in {'menuitem', 'button', 'splitbutton', 'hyperlink'})
                 or (action == 'select' and role in {'tab', 'treeitem', 'listitem'})
                 or (action == 'expand' and role in {'treeitem', 'menuitem', 'splitbutton'}))
    if not supported or target.get('kind') != 'element':
        reason = step.get('recognition', {}).get('reason') if action == 'manual' else None
        if action == 'right-click':
            reason = 'right_click_requires_menu_teaching'
        return manual(reason or 'unsupported_recorded_action', step['id'])
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
        self.row_selections = {}
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

    def context_start(self, step_id, surface):
        with self.lock:
            step = manual('right_click_requires_menu_teaching', step_id)
            self.add(step)
            if step_id in self.steps:
                self.steps[step_id]['menu_anchor'] = surface

    def context_complete(self, step):
        with self.lock:
            previous = self.steps.get(step['id'], {})
            if (not self.closed and previous.get('menu_anchor')
                    and previous.get('recognition', {}).get('reason') == 'right_click_requires_menu_teaching'):
                self.steps[step['id']] = step

    def row_step(self, step_id, number, spec, selection=None):
        """Keep the click's original order while the observed row state settles."""
        with self.lock:
            if self.closed or self.limit:
                return
            if step_id not in self.steps and len(self.steps) >= self.max_steps - 1:
                self.add(manual('recording_step_limit'))
                return
            # A queued double click or deleted/invalidated event owns its ID.
            existing = self.steps.get(step_id)
            if existing and existing != self.row_selections.get(step_id) and existing.get('action') != 'row-action' and (
                    existing.get('recognition', {}).get('reason') != 'row_capture_requires_review'):
                return
            label = f'{number}번째 행 버튼' if number else '선택한 행 버튼'
            if selection is not None:
                self.row_selections[step_id] = capture_step(selection)
            self.times.setdefault(step_id, self.event_at or time.monotonic())
            if spec is None:
                self.steps[step_id] = (self.row_selections.get(step_id) or
                                       dict(manual('row_capture_requires_review', step_id), label=label))
            else:
                self.steps[step_id] = dict(id=step_id, action='row-action', row=spec, label=label + ' 누르기')

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(list(self.steps.values()))


def default_text(step):
    if step['action'] in ('row-action', 'result-menu', 'visual-click'):
        return step['label']
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
    if command['action'] == 'open':
        kind = '버튼' if command.get('role') in {'button', 'splitbutton'} else '항목'
        verb = '펼쳐줘' if command.get('expand') else '눌러줘'
        return f'{field} {kind}을 {verb}'
    return ''


def instruction_view(step, instruction):
    if step.get('ordinary_menu') and instruction and instruction.get('mode') == 'draft':
        return '작성한 설명과 우클릭 위치를 저장합니다. 현재 메뉴에서 지시한 경로를 확인합니다.'
    if step.get('recognition', {}).get('reason') == 'right_click_requires_menu_teaching':
        return '작성한 설명을 바탕으로 현재 창의 우클릭 위치와 하위 메뉴 경로를 확인합니다.'
    if step['action'] == 'result-menu':
        spec = step['context']
        route = (['조회 완료 확인'] if spec['trigger'] else []) + ['표 우클릭', *spec['menu']['path']]
        route.append('Excel 파일 확인' if spec['output'] else '메뉴 전달까지만 확인')
        return ' → '.join(route)
    if instruction and instruction['mode'] == 'row-launch':
        return f"{instruction['ordinal']}번째 행의 실행 버튼을 찾고, 실행 후 같은 행의 상태를 확인합니다."
    if step['action'] == 'row-action' and not instruction:
        return step['label'] + ' (버튼 선택 후 같은 행의 완료 표시 확인)'
    if instruction and instruction['mode'] == 'draft':
        return '설명은 저장됩니다. 실행 전 Claude가 문장을 해석하고 현재 화면에서 대상을 다시 찾습니다.'
    if step['action'] == 'visual-click' and (not instruction or instruction['mode'] == 'visual-click'):
        return '클릭 직전의 항목 모양을 저장했습니다. 재생할 때 해당 창에서 같은 모양을 찾아 한 번 누릅니다.'
    retry = '실행할 때 현재 화면에서 대상을 다시 찾습니다. ' if instruction and instruction.get('query') else ''
    if instruction and instruction['mode'] == 'fixed':
        role = instruction.get('role') or step.get('target', {}).get('locator', {}).get('role')
        if role == 'textfield':
            return retry + ('입력란 비우기' if instruction['value'] == '' else '지정한 값 입력: ' + instruction['value'])
        return retry + '지정한 값으로 선택: ' + instruction['value']
    if instruction and instruction['mode'] == 'required':
        return retry + '실행 시 입력할 변수: ' + instruction['parameter']
    if retry:
        return retry + instruction['text']
    if step['action'] == 'teach-target':
        kind = '입력란' if step['target'].get('locator', {}).get('role') == 'textfield' else '콤보'
        return kind + ' 대상 확인됨. 고정값이나 입력 변수를 지정하세요.'
    if learned_tasks.is_recoverable(step):
        return '대상을 못 읽어도 설명을 저장할 수 있습니다. 대상 이름과 동작을 적으면 실행 시 다시 찾습니다.'
    return default_text(step) or '직접 확인할 단계'


class StepEdits:
    """Bind compiler-checked instructions to demonstrated identities across revisions."""
    def __init__(self):
        self.edits = {}
        self.drafts = {}
        self.failed_step = None

    @staticmethod
    def identity(step):
        if step.get('ordinary_menu') or step.get('menu_anchor'):
            anchor = step.get('menu_anchor') or step['context']['menu']
            return 'observed_context_surface', anchor['target'], anchor['point']
        if step['action'] == 'visual-click':
            return step['action'], step['visual']
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
            root = int(bridge.user32.GetWindow(root, 4) or 0)
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
        return node

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


class _RowHookPump(recorder.HookPump):
    visual_tracker = None

    def _emit(self, event):
        if self.visual_tracker is not None and (event.get('kind') in
                ('scroll', 'candidate_scroll', 'typing', 'enter', 'tab', 'escape', 'shortcut', 'pause', 'outside')
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

    def observe_down(self, event):
        if event.get('kind') == 'down':
            if event.get('action') == 'right-click':
                from menu_capture import capture_surface
                self.menu_gesture = None
                event['menu_anchor'] = capture_surface(self.binding, self.uia, event, self.hover_history)
            elif self.menu_gesture is not None:
                pick = self.menu_gesture.at_down(self.binding, self.uia, event)
                if pick:
                    # The menu observer proved this HWND, point and item. The
                    # legacy capture worker must not interpret it as a combo.
                    event.update(menu_pick=pick, scope_proven=True)
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
        if (not tracker or not observation or event.get('window') != self.binding.hwnd
                or not event.get('scope_proven') or observation.get('combo') is not None):
            return observation
        node = observation['node']
        role = node.get('role')
        if (node.get('secure') is not False or node.get('enabled') is not True
                or role not in ('list', 'listitem', 'row', 'table', 'image', 'statictext', 'custom', 'group')
                or event.get('action') != 'click'):
            return observation
        if 'combo' not in observation and self.combo_for(observation) is not None:
            return observation
        import row_capture
        import row_vision
        point = [event['x'],event['y']]
        press, number = None,None
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
                if frame is not None:
                    press = row_capture.prepare(frame, point, event['at'])
            except bridge.BridgeError:
                pass
        if not press and _semantic_list_selection(node):
            return observation
        if press or number or role in ('row','listitem'):
            return dict(observation, painted_row=(number,press))
        return observation

    def handle(self, event):
        if event['kind'] in ('outside', 'scroll', 'candidate_scroll', 'typing', 'enter', 'tab',
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
            return super().handle(event)
        finally:
            self.model.event_at = None

    def tick(self):
        super().tick()
        self._poll_menu()
        if (self.row_tracker and self.hovered and self.hovered.get('window') == self.binding.hwnd
                and self.hovered['node'].get('role') in ('list','listitem','row','table')):
            self.row_tracker.interest.set()

    def _poll_menu(self):
        gesture = self.menu_gesture
        if gesture is not None:
            state = gesture.tick(self.binding, self.uia, time.monotonic())
            if state == 'complete':
                self.model.context_complete(gesture.step())
            if state != 'pending':
                self.menu_gesture = None

    def capture_double(self, down, observation):
        previous_id = (self.recent_click or {}).get('step_id')
        result = super().capture_double(down,observation)
        if result and previous_id and self.row_tracker:
            self.row_tracker.cancel_step(previous_id)
        if result:
            self.menu_gesture = None
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
        if self.uia.secure(obs['element']) or not recorder._locator_valid(locator):
            return False
        role = obs['node']['role']
        if role == 'textfield':
            try:
                root = live_action.textfield_scope(self.binding, self.uia, obs['element'])
                if learned_tasks._selector(self.uia.locator(obs['element'], root)) != learned_tasks._selector(locator):
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
            try:
                live_action.textfield_scope(self.binding, self.uia, self.watched['element'])
            except bridge.BridgeError as error:
                self._manual_watch('protected_input_not_recorded' if error.code == 'target_protected' else error.code)
                return
            if not isinstance(current, str):
                self._manual_watch('value_readback_unavailable')
                return
            # A location-only click does not demonstrate assigning the existing
            # text. Preserve its variable draft until a changed value is read.
            if self.watched_step and self.watched_step['action'] == 'teach-target' and current == self.watched_value:
                return
            return super()._commit_value(current)
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
                self._manual_watch('protected_input_not_recorded' if error.code == 'target_protected' else error.code)
                return
        return super().finalize_watch(force, sample_only)

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
            self.model.manual('', 'right_click_requires_menu_teaching')
            return
        if event.get('menu_pick') is not None and self.menu_gesture is not None:
            self.menu_gesture.clicked = event['menu_pick']
            self.menu_gesture.last_poll = 0
            self._poll_menu()
            return
        if observation:
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
                self.model.row_step(step_id, number, None, selection)
                if press:
                    self.row_tracker.follow(step_id,press)
                return step_id
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
                if pointed is None and node.get('role') == 'textfield':
                    pointed = observation
        if event.get('visual') is not None and pointed is None and self._use_visual(event, observation):
            self.freeze_watch(event.get('at'))
            self.binding.check()
            step = dict(id=uuid.uuid4().hex, action='visual-click', label='녹화한 화면 항목 누르기',
                        visual=event['visual'])
            self.model.add(step)
            return step['id']
        step = super().capture_action(event, observation)
        if pointed and self.watched and self.uia.same(pointed['element'], self.watched['element']):
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
        shell = role in ('window','group','custom')
        if (observation.get('window') != self.binding.hwnd or node.get('secure') is not False
                or role not in ('window','group','custom','statictext','image','button')
                or actions.intersection(('Check','Select','Expand','Collapse'))):
            return False
        if ((node.get('enabled') is not True or 'SetValue' in actions)
                and not (shell and event.get('visual') is not None)):
            return False
        locator = node.get('locator')
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
            self.notes.row_selections.clear()
            self.notes.hover_target = None
        return {'status': 'cancelled', 'steps': [], 'input_dispatched': False}


def pick_recording_window(root, session):
    """Let the user choose a live window by title during recording."""
    import tkinter as tk
    from tkinter import ttk
    from capture_windows import window_choices

    dialog = tk.Toplevel(root, name='window_picker')
    dialog.title('열린 창 선택')
    dialog.transient(root)
    panel = ttk.Frame(dialog, padding=14)
    panel.pack(fill='both', expand=True)
    tools = ttk.Frame(panel)
    tools.pack(fill='x')
    status = tk.StringVar(dialog, value='')
    all_windows = tk.BooleanVar(dialog, value=False)
    scope_title = tk.StringVar(dialog, value='관련 창')
    ttk.Label(tools, textvariable=scope_title, style='Title.TLabel').pack(side='left')
    include = ttk.Checkbutton(tools, name='all_windows', text='다른 프로그램의 창도 보기', variable=all_windows)
    include.pack(side='right')
    table = ttk.Frame(panel)
    table.pack(fill='both', expand=True, pady=(8, 4))
    tree = ttk.Treeview(table, name='windows', show='headings', columns=('title', 'exe', 'status'),
                        selectmode='browse', height=7, style='Steps.Treeview')
    for key, label, width in (('title','창 제목',290), ('exe','프로그램',170), ('status','상태',140)):
        tree.heading(key, text=label, anchor='w')
        tree.column(key, width=width, minwidth=70, anchor='w', stretch=key=='title')
    scroll = ttk.Scrollbar(table, command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    scroll.pack(side='right', fill='y')
    tree.pack(side='left', fill='both', expand=True)
    ttk.Label(panel, textvariable=status, style='Muted.TLabel', wraplength=560).pack(anchor='w')
    footer = ttk.Frame(panel)
    footer.pack(fill='x', pady=(8, 0))
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
    ttk.Button(footer, name='cancel', text='취소', command=close).pack(side='right', padx=4)
    include.configure(command=refresh)
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


def capture(binding, *, title, save_recording=None, save_draft=None, initial_draft=None, initial_step=None):
    """Block while the user records from a small, local start/finish window."""
    import tkinter as tk
    from tkinter import font as tkfont, ttk
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
    previous_columns = None
    style = ttk.Style(root)
    if 'clam' in style.theme_names():
        style.theme_use('clam')
    bg, surface, ink, muted, accent = '#F5F7FA', '#FFFFFF', '#182435', '#55677A', '#1556AC'
    font = tkfont.nametofont('TkDefaultFont')
    text_size = abs(font.cget('size')) or 10
    family = '맑은 고딕'
    style.configure('TFrame', background=bg)
    style.configure('TLabel', background=bg, foreground=ink, font=(family, text_size))
    style.configure('TButton', font=(family, text_size), padding=(10, 5))
    style.configure('Card.TFrame', background=surface)
    style.configure('Card.TLabel', background=surface)
    style.configure('Title.TLabel', font=(family, text_size + 4, 'bold'))
    style.configure('Muted.TLabel', foreground=muted)
    style.configure('Note.TLabel', background=surface, foreground=muted)
    style.configure('Primary.TButton', background=accent, foreground='white', borderwidth=0, padding=(16, 7))
    style.map('Primary.TButton', background=[('disabled', '#D2DBE5'), ('active', '#104888')],
              foreground=[('disabled', '#5F7082'), ('!disabled', 'white')])
    style.configure('Steps.Treeview', font=(family, text_size), background=surface,
                    fieldbackground=surface, foreground=ink, rowheight=font.metrics('linespace') + 14,
                    borderwidth=0)
    style.map('Steps.Treeview', background=[('selected', '#DCEAFE')], foreground=[('selected', '#123D74')])
    style.configure('Steps.Treeview.Heading', font=(family, text_size, 'bold'), anchor='w',
                    background='#E9EEF4', foreground=muted, padding=(6, 6), borderwidth=0)
    root.configure(background=bg)
    message = tk.StringVar(value='시작을 누른 뒤 대상 창에서 원하는 순서대로 조작하세요.')
    phase = tk.StringVar(value='녹화 준비')
    frame = ttk.Frame(root, padding=(16, 12))
    frame.pack(fill='both', expand=True)
    header = ttk.Frame(frame)
    header.pack(fill='x')
    ttk.Label(header, textvariable=phase, style='Title.TLabel').pack(side='left')
    primary = ttk.Frame(header)
    primary.pack(side='right')
    active_title = tk.StringVar(value=title)
    active_window = ttk.Frame(frame)
    active_window.pack(fill='x', pady=(0, 6))
    ttk.Label(active_window, text='녹화 대상 창', style='Muted.TLabel').pack(side='left', padx=(0, 8))
    ttk.Label(active_window, textvariable=active_title, wraplength=540, style='Muted.TLabel').pack(side='left')
    status_line = ttk.Label(frame, textvariable=message, wraplength=540, style='Muted.TLabel')
    status_line.pack(anchor='w', pady=(0, 10))
    table = ttk.Frame(frame)
    table.pack(fill='both', expand=True)
    listing = ttk.Treeview(table, name='steps', columns=('number', 'window', 'instruction', 'status'),
                           show='headings', selectmode='browse', height=5, style='Steps.Treeview')
    for key, heading, chars in [('number', '단계', 4), ('window', '작업 창', 14),
                                ('instruction', '실행할 동작', 26), ('status', '상태', 9)]:
        width = font.measure('가') * chars + 12
        listing.heading(key, text=heading, anchor='w')
        listing.column(key, width=width, minwidth=min(width, 60), stretch=key == 'instruction', anchor='w')
    listing.tag_configure('unresolved', foreground='#805110')
    scroll = ttk.Scrollbar(table, command=listing.yview)
    listing.configure(yscrollcommand=scroll.set)
    scroll.pack(side='right', fill='y')
    listing.pack(side='left', fill='both', expand=True)
    window_detail = ttk.Label(frame, name='step_window', wraplength=540, style='Muted.TLabel')
    form = ttk.Frame(frame, padding=12, style='Card.TFrame')
    form.columnconfigure(1, weight=1)
    alias, instruction, argument = tk.StringVar(), tk.StringVar(), tk.StringVar()
    mode = tk.StringVar(value='문장 해석')
    modes = {'문장 해석': None, '고정값': 'fixed', '필수 변수': 'required'}
    edit_title = tk.StringVar(value='선택한 단계')
    ttk.Label(form, textvariable=edit_title, style='Card.TLabel').grid(row=0, column=0, columnspan=3, sticky='w')
    sentence_entry = ttk.Entry(form, name='instruction', textvariable=instruction, width=32)
    sentence_entry.grid(row=1, column=0, columnspan=3, pady=(5, 2), sticky='ew')
    advanced_form = ttk.Frame(form, style='Card.TFrame')
    advanced_form.columnconfigure(1, weight=1)
    ttk.Label(advanced_form, text='단계 별칭', style='Card.TLabel').grid(row=0, column=0, padx=(0, 8), sticky='w')
    ttk.Entry(advanced_form, name='alias', textvariable=alias, width=20).grid(row=0, column=1, sticky='ew')
    ttk.Label(advanced_form, text='값 지정', style='Card.TLabel').grid(row=1, column=0, padx=(0, 8), sticky='w')
    choices = ttk.Frame(advanced_form, style='Card.TFrame')
    choices.grid(row=1, column=1, sticky='w', pady=4)
    mode_box = ttk.Combobox(choices, name='mode', values=list(modes), state='readonly', textvariable=mode, width=11)
    mode_box.pack(side='left')
    arg_label = ttk.Label(choices, text='값 또는 변수 이름', style='Card.TLabel')
    arg_label.pack(side='left', padx=(6, 2))
    arg_box = ttk.Entry(choices, name='argument', textvariable=argument, width=22)
    arg_box.pack(side='left')
    preview = tk.StringVar()
    preview_label = ttk.Label(form, name='preview', textvariable=preview, wraplength=520, style='Note.TLabel')
    preview_label.grid(row=2, column=0, columnspan=3, sticky='w', pady=(3, 4))
    controls = ttk.Frame(frame)
    controls.pack(fill='x', pady=(8, 0))
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
            window_detail.pack(after=table, fill='x', pady=(4, 0))

    def row_repairable(step):
        return (step['action'] == 'row-action' or learned_tasks.is_recoverable(step)
                or step.get('recognition', {}).get('reason') == 'row_capture_requires_review')

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

    def fields():
        if editing is not None and editing['action'] == 'result-menu' and not editing.get('ordinary_menu'):
            return alias.get(), '', None, None
        chosen = modes[mode.get()]
        return alias.get(), instruction.get(), chosen, argument.get()

    def show_error(exc):
        if isinstance(exc, learned_tasks.TaskError):
            reason = {'instruction_target_mismatch': '화면 이름이 다르면 ‘문장의 이름을 이 대상에 연결’을 누르세요. 또는 ‘이 항목을 N으로 설정’처럼 입력하세요.',
                      'instruction_target_unavailable': '이 단계는 보호된 입력이나 별도 절차가 필요한 동작입니다. 일반 대상 인식 실패는 설명을 저장한 뒤 실행할 때 다시 찾을 수 있습니다.',
                      'instruction_requires_structure': '값 지정에서 고정값이나 필수 변수를 선택하고 값을 입력하세요.',
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
                if (editing['action'] == 'manual' and not instruction.get().strip()
                        and row_repairable(editing)):
                    if editing.get('recognition', {}).get('reason') == 'row_capture_requires_review':
                        preview.set('행은 찾았습니다. 예를 들어 “첫 번째 행의 실행 버튼을 눌러줘”라고 적거나, 행 버튼과 완료 표시를 지정하세요.')
                    else:
                        preview.set('화면에 보이는 이름과 동작을 적거나, 목록 안의 버튼이면 행 버튼으로 지정하세요.')
                else:
                    preview.set(instruction_view(editing, data.get('instruction')))
                if review is not None:
                    update_readiness(editing, data)
            except (ValueError, bridge.BridgeError, KeyError) as exc:
                show_error(exc)
                unfinished.add(editing['id'])
            if review is not None:
                update_save_button(review['steps'])

    def flush():
        if editing is not None:
            try:
                edits.store(editing, *fields())
            except (ValueError, bridge.BridgeError) as exc:
                show_error(exc)
                return False
        return True

    def load(step):
        sentence, chosen, arg = edits.form(step)
        configured = step['action'] in ('row-action', 'result-menu', 'visual-click')
        fixed_menu = step['action'] == 'result-menu' and not step.get('ordinary_menu')
        sentence_entry.configure(state='readonly' if fixed_menu else 'normal')
        mode_box.configure(state='disabled' if configured else 'readonly')
        alias.set(edits.alias(step))
        instruction.set(step['label'] if fixed_menu else sentence)
        mode.set(next(label for label, value in modes.items() if value == chosen))
        argument.set(arg or '')
        selected_number = next((i for i, s in enumerate(steps_now(), 1) if s['id'] == step['id']), '')
        scope = step.get('_capture_window')
        if scope and ('_openers' in scope or 'after_id' in scope):
            opener_btn.pack(side='left', padx=(0, 5), before=details_btn)
        else:
            opener_btn.pack_forget()
        edit_title.set(f'{selected_number}단계   {step["label"]}')
        show_step_window(step, steps_now())
        if session is not None and row_repairable(step):
            repair_btn.configure(text='행 버튼 다시 지정' if step['action'] == 'row-action' else '행 버튼으로 지정')
            repair_btn.pack(side='left', padx=(0, 5), before=details_btn)
        else:
            repair_btn.pack_forget()
        alias_btn.configure(state='disabled' if configured else 'normal')
        refresh_preview()

    def select_row(*_):
        nonlocal editing
        selected = listing.selection()
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
        phase.set(f'단계 검토   {len(steps)}')
        message.set(f'{unlinked}단계의 창 연결을 나중에 지정하려면 초안 저장을 누르세요.' if unlinked else
                    f'{missing}단계는 실행 전 보완해야 합니다. 해당 행을 선택해 수정하세요.' if missing
                    else '단계를 선택해 실행 문장을 수정하세요.' if steps else '단계를 추가하거나 취소하세요.')

    def render(steps):
        nonlocal editing
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

    def link_subject():
        if editing is None:
            return
        subject = learned_tasks.instruction_subject(instruction.get())
        if not subject:
            preview.set('문장의 이름을 찾지 못했습니다. 단계 별칭에 화면 이름을 직접 입력하세요.')
            return
        alias.set(subject)
        apply_text()

    def delete_step():
        nonlocal editing
        if editing is None:
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

    listing.bind('<<TreeviewSelect>>', select_row)
    for variable in (alias, instruction, mode, argument):
        variable.trace_add('write', refresh_preview)
    refresh_preview()
    editing_controls = ttk.Frame(form, style='Card.TFrame')
    editing_controls.grid(row=3, column=0, columnspan=3, sticky='ew', pady=(4, 0))
    def toggle_details():
        if advanced_form.winfo_manager():
            advanced_form.grid_remove()
            details_btn.configure(text='상세 설정')
        else:
            advanced_form.grid(row=4, column=0, columnspan=3, sticky='ew', pady=(8, 0))
            details_btn.configure(text='상세 닫기')

    # The instruction is the first editable field. Alias and parameter controls
    # remain available without competing with the step description.
    details_btn = ttk.Button(editing_controls, text='상세 설정', command=toggle_details)
    details_btn.pack(side='left')
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
            if action not in ('manual', 'wait', 'verify-file'):
                choices.append((step['id'], f'{number}단계   {step["label"]}'))
        dialog = tk.Toplevel(root)
        dialog.title('창을 연 단계 지정')
        dialog.transient(root)
        panel = ttk.Frame(dialog, padding=14)
        panel.pack(fill='both', expand=True)
        ttk.Label(panel, text=f'{scope["title"]} 창을 언제 열지 선택하세요.').pack(anchor='w')
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
        ttk.Button(panel, text='연결', command=select).pack(side='right')
        dialog.grab_set()
        picker.focus_set()
    opener_btn = ttk.Button(editing_controls, text='창을 연 단계 지정', command=choose_opener)
    repair_btn = ttk.Button(editing_controls, command=lambda: teach_extra('row', replace=True))
    ttk.Button(editing_controls, text='선택 단계 삭제', command=delete_step).pack(side='left', padx=4)
    ttk.Button(editing_controls, text='적용하고 다음 단계', command=apply_next).pack(side='right')
    alias_btn = ttk.Button(advanced_form, text='문장의 이름을 이 대상에 연결', command=link_subject)
    alias_btn.grid(row=0, column=2, padx=(5, 0))
    sentence_entry.bind('<Return>', lambda _e: apply_next())

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

    def choose_window():
        nonlocal picking_window
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
        root.after(3000, connect_pointed)

    def add_instruction():
        nonlocal review
        flush()
        scope = (editing.get('_capture_window') if review is not None and editing is not None
                 else getattr(session, 'scope', None))
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
        review['steps'].append(step)
        render(review['steps'])
        update_phase()
        listing.selection_set(step['id'])
        listing.see(step['id'])
        select_row()
        sentence_entry.focus_set()

    def teach_extra(kind, *, replace=False):
        nonlocal session, previous_columns, editing
        if session is None:
            return
        if replace and (review is None or editing is None or not row_repairable(editing)):
            return
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
        else:
            last_binding, last_scope = session.binding, getattr(session, 'scope', None)
        if replace:
            last_scope = editing.get('_capture_window')
            remembered = bound_windows.get(id(last_scope))
            last_binding = remembered or (session.binding_for(editing) if isinstance(session, CaptureTrail) else session.binding)
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
            from tkinter import messagebox
            step = teach_context_dialog(root, last_binding)
            if (step and steps and steps[-1].get('_capture_window') == last_scope
                    and previous_trigger(steps[-1], step['context'])):
                if messagebox.askyesno('조회 한 번으로 합치기',
                        '바로 앞 단계에서 같은 조회 버튼을 눌렀습니다. 기존 조회 단계를 이번 조회와 우클릭 단계로 교체할까요?',
                        parent=root):
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
                steps[index] = step
                edits.forget(replace_id)
                editing = None
            else:
                steps.append(step)
            render(steps)
            listing.selection_set(step['id'])
            listing.see(step['id'])
            select_row()
        update_phase()

    def unresolved_window(steps):
        preceding = set()
        for number, step in enumerate(steps, 1):
            scope = step.get('_capture_window', {})
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
            root.after(200, tick)

    tick.last_id = None
    begin = ttk.Button(primary, text='시작', command=start, style='Primary.TButton')
    link_window = ttk.Button(primary, text='열린 창 선택', command=choose_window)
    end = ttk.Button(primary, text='마치고 검토', command=finish, style='Primary.TButton')
    save = ttk.Button(primary, text='저장', command=save_review, style='Primary.TButton')
    add_button = ttk.Menubutton(controls, text='단계 추가 ▾')
    add_menu = tk.Menu(add_button, tearoff=False)
    add_menu.add_command(label='직접 설명 입력', command=add_instruction)
    if session is not None:
        add_menu.add_command(label='행 버튼 가르치기', command=lambda: teach_extra('row'))
        add_menu.add_command(label='조회 후 우클릭 메뉴 가르치기', command=lambda: teach_extra('menu'))
    add_button.configure(menu=add_menu)
    add_button.pack(side='left')
    ttk.Button(controls, text='취소', command=cancel).pack(side='right')

    def update_phase():
        for button in (begin, end, save, link_window):
            button.pack_forget()
        if review is not None:
            count = len(review['steps'])
            update_review_status(review['steps'])
            save.pack()
            if count:
                form.pack(fill='x', before=controls, pady=(8, 0))
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

    if review is not None:
        render(review['steps'])
        if type(initial_step) is int and 1 <= initial_step <= len(review['steps']):
            item = review['steps'][initial_step - 1]['id']
            listing.selection_set(item)
            listing.see(item)
            select_row()
    update_phase()
    def resize(event):
        if event.widget is root:
            status_line.configure(wraplength=max(210, event.width - 40))
            window_detail.configure(wraplength=max(210, event.width - 40))
            preview_label.configure(wraplength=max(190, event.width - 72))
    root.bind('<Configure>', resize)
    root.bind('<Alt-s>', start)
    root.bind('<Alt-f>', finish)
    root.protocol('WM_DELETE_WINDOW', cancel)
    def failed(*_):
        nonlocal result
        result = {'status': 'blocked', 'diagnostic': 'recording_ui_failed', 'input_dispatched': False}
        root.destroy()
    root.report_callback_exception = failed
    if review is None:
        root.after(200, tick)
    try:
        root.mainloop()
        return result or {'status': 'cancelled', 'steps': [], 'input_dispatched': False}
    finally:
        if session is not None and not session.ended:
            session.cancel()
