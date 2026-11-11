"""Record target locations, then keep the user's own instructions for those targets."""
from __future__ import annotations

from collections import deque
import copy
import queue
import time
import uuid

import win_bridge as bridge
from provider_visibility import visible
import teach_capture as legacy


REGIONS = {'group', 'table', 'list', 'tree', 'custom', 'document'}
CONTROLS = {'combobox', 'textfield', 'button', 'splitbutton', 'hyperlink', 'checkbox',
            'radiobutton', 'spinner', 'tab', 'treeitem', 'listitem', 'menuitem'}


def draft(anchor=None, label='대상 지정 필요', *, step_id=None):
    return {'id': step_id or uuid.uuid4().hex, 'action': 'intent', 'label': label, 'anchor': copy.deepcopy(anchor),
            'instruction': {'mode': 'natural', 'text': '', 'parameters': []}}


def editable_step(step):
    """Open saved plans for editing without interpreting a new demonstration."""
    result = copy.deepcopy(step)
    result.setdefault('id', uuid.uuid4().hex)
    if result.get('action') in {'intent', 'row-action', 'result-menu'}:
        return result
    raw_target = result.get('target')
    anchor = None
    if isinstance(raw_target, str) and result.get('match') in {'automation_id', 'name'}:
        anchor = {'role': result['role'], result['match']: raw_target}
        if result.get('ancestors'):
            anchor['ancestors'] = result['ancestors']
    elif isinstance(raw_target, dict) and raw_target.get('kind') == 'element' and 'window' not in raw_target:
        anchor = _locator(raw_target.get('locator', {}))
    label = result.get('label') or result.get('field_label') or (
        (anchor.get('automation_id') or anchor.get('name')) if anchor else '대상 지정 필요')
    converted = draft(anchor, label, step_id=result['id'])
    spec = result.get('instruction') or {}
    text, parameters = spec.get('text', ''), []
    if spec.get('mode') == 'required':
        parameters = [spec['parameter']]
        text = text or f'{label}에 {{{spec["parameter"]}}} 입력'
    elif not text and spec.get('mode') == 'fixed':
        text = f'{label}을 {spec["value"]}로 설정'
    elif not text and result.get('action') == 'select' and isinstance(raw_target, str):
        # These are prior saved semantics, never a newly observed combo value.
        text = f'{label}을 {result.get("value", "")}로 설정'
    elif not text and result.get('action') == 'open':
        text = label + (' 펼치기' if result.get('expand') else ' 선택')
    converted['instruction'].update(text=text, parameters=parameters)
    if result.get('alias'):
        converted['alias'] = result['alias']
    return converted


def _locator(locator):
    from learned_tasks import _selector
    selected = _selector(locator)
    if selected is None:
        return None
    chain = locator.get('ancestors', [])
    if not isinstance(chain, list) or len(chain) > 4:
        return None
    parents = [_selector(parent) for parent in chain]
    if not all(parents):
        return None
    if parents:
        selected['ancestors'] = parents
    return selected


class PositionUIA(legacy.CaptureUIA):
    def value_observation(self, element):
        # A location demonstration cannot supply a desired or default value.
        return {'value': None, 'value_evidence': {'status': 'unavailable', 'source': 'none'}}


def anchor_from_observation(binding, uia, observation, point):
    """Use combo ownership or a named ancestor, without reading selected values."""
    if not observation:
        raise bridge.BridgeError('target_position_unavailable')
    owner = uia.combo_observation(observation)
    chosen = owner or observation
    root = uia.from_handle(binding.hwnd)
    current = chosen['element']
    if root is None or not uia.within(current, root):
        raise bridge.BridgeError('target_position_outside_window')
    # Do not collect a label below a protected or foreign ancestor.
    ancestor = current
    for _ in range(32):
        if ancestor is None or uia.pid(ancestor) != binding.pid or uia.secure(ancestor):
            raise bridge.BridgeError('protected_target_position')
        if uia.same(ancestor, root):
            break
        ancestor = uia.parent(ancestor)
    else:
        raise bridge.BridgeError('target_position_unavailable')
    for _ in range(24):
        if current is None or uia.same(current, root):
            break
        role = bridge.ROLES.get(current.property(30003))
        selector = _locator(uia.locator(current, root))
        if selector and role in CONTROLS | REGIONS:
            if role == 'combobox':
                from live_action import _combo_visible
                shown = _combo_visible(binding, uia, current, root)
            else:
                shown = visible(binding, uia, current, root)
            if not shown or current.property(30010) is not True:
                raise bridge.BridgeError('target_position_not_visible')
            # A complete identity-only traversal ensures that the anchor is
            # unique. No recorded path/handle or chosen option is persisted.
            hits, pending, visited = [], deque([(root, 0)]), 0
            while pending:
                item, depth = pending.popleft()
                visited += 1
                if visited > 2500 or depth > 18 or uia.pid(item) != binding.pid:
                    raise bridge.BridgeError('target_position_tree_incomplete')
                if uia.secure(item):
                    continue
                if (bridge.ROLES.get(item.property(30003)) == selector['role']
                        and bridge.match_locator(uia.locator(item, root), selector)):
                    hits.append(item)
                pending.extend((child, depth + 1) for child in uia.children(item))
            if len(hits) != 1 or not uia.same(hits[0], current):
                raise bridge.BridgeError('target_position_ambiguous')
            if role in REGIONS:
                box = uia.bounds(current)
                x, y = point
                if not legacy.recorder._inside(box, x, y):
                    raise bridge.BridgeError('target_position_moved')
                selector['point'] = [(x - box['x']) / box['width'], (y - box['y']) / box['height']]
            label = uia.semantic_label(current, root).get('semantic_label')
            binding.check()
            return {'anchor': selector, 'label': label or selector.get('automation_id') or selector['name']}
        current = uia.parent(current)
    raise bridge.BridgeError('target_position_unavailable')


def pick_anchor(binding, point):
    binding.check()
    with PositionUIA(binding) as uia:
        observation = uia.observe_point(binding, *point)
        return anchor_from_observation(binding, uia, observation, point)


def _read_sample(sender, window, title, op, args):
    try:
        import desktop_select as app
        bridge.set_dpi_awareness()
        binding = bridge.Binding(window)
        app.guard(binding, binding, title)
        result = pick_anchor(binding, args[0])
        app.guard(binding, binding, title)
        sender.send((True, result))
    except Exception as error:
        sender.send((False, getattr(error, 'code', 'target_position_unavailable')))
    finally:
        sender.close()


class PositionNotes(legacy.CaptureNotes):
    def add(self, step):
        with self.lock:
            if self.closed or self.limit:
                return
            if step['id'] not in self.steps and len(self.steps) >= self.max_steps:
                self.limit = True
                return
            if self.steps and step.get('anchor') is not None:
                last = next(reversed(self.steps.values()))
                if last.get('anchor') == step['anchor']:
                    return  # Opening and choosing within one combo teach one location.
            self.steps[step['id']] = copy.deepcopy(step)

    def manual(self, label, reason):
        self.add(draft(label='대상 지정 필요'))


class PositionWorker(legacy.recorder.CaptureWorker):
    def finalize_watch(self, *_args, **_kwargs):
        return None

    def handle(self, event):
        if not self.check_binding():
            return
        if event['kind'] == 'down':
            observation = self.observe_down(event)
            try:
                picked = anchor_from_observation(self.binding, self.uia, observation, (event['x'], event['y']))
                item = draft(**picked)
            except bridge.BridgeError:
                item = draft()
            self.captures[event['token']] = (event, item)
        elif event['kind'] == 'up':
            down, item = self.captures.pop(event['token'], (None, None))
            if not down or not event['inside'] or abs(event['x'] - down['x']) > 8 or abs(event['y'] - down['y']) > 8:
                return
            self.model.add(item)
        elif event['kind'] == 'escape':
            self.enabled.clear()
            self.notices.put(('pause', None))
        # Typing, chosen options, scrolling and shortcuts are not instructions.

    def tick(self):
        if time.monotonic() - self.last_identity > .5:
            self.last_identity = time.monotonic()
            self.check_binding()


class PositionSession(legacy.CaptureSession):
    def __init__(self, binding, title):
        self.binding = legacy._BoundWindow(binding, title)
        self.notes = PositionNotes()
        self.events, self.notices = queue.Queue(maxsize=128), queue.Queue()
        class PositionBridge(legacy._CaptureBridge):
            def UIA(inner):
                return PositionUIA(inner.binding)
        self.worker = PositionWorker(self.binding, PositionBridge(self.binding), self.notes, self.events, self.notices)
        self.pump = legacy.recorder.HookPump(self.binding, bridge, self.events, self.notices)
        self.started = self.ended = False


def capture(binding, *, title, steps=None):
    import tkinter as tk
    from tkinter import ttk
    from row_teach import SampleJob
    import learned_tasks
    import intent_tasks

    session = PositionSession(binding, title)
    session.binding.check()
    items = [editable_step(step) for step in (steps or [])]
    seen = set()
    result, editing, job, sampling, timer = None, None, None, False, None
    previous_columns = None
    root = tk.Tk()
    root.title('Desktop Select 위치와 동작')
    frame = ttk.Frame(root, padding=14)
    frame.pack(fill='both', expand=True)
    message = tk.StringVar(value='위치를 알려주고 각 단계에 실행할 동작을 적으세요. 현재 선택값은 녹화하지 않습니다.')
    ttk.Label(frame, text=title, wraplength=740).pack(anchor='w')
    ttk.Label(frame, textvariable=message, wraplength=740, padding=(0, 8)).pack(anchor='w')
    listing = ttk.Treeview(frame, name='steps', columns=('number', 'target', 'text', 'status'), show='headings', height=8)
    for key, label, width in [('number', '단계', 45), ('target', '알려준 대상', 170),
                              ('text', '실행할 동작', 340), ('status', '상태', 140)]:
        listing.heading(key, text=label)
        listing.column(key, width=width)
    listing.pack(fill='both', expand=True)
    form = ttk.Frame(frame, padding=(0, 8))
    form.pack(fill='x')
    form.columnconfigure(1, weight=1)
    alias, parameters = tk.StringVar(), tk.StringVar()
    ttk.Label(form, text='단계 별칭').grid(row=0, column=0, sticky='w')
    ttk.Entry(form, name='alias', textvariable=alias).grid(row=0, column=1, sticky='ew')
    ttk.Label(form, text='실행할 동작').grid(row=1, column=0, sticky='nw')
    sentence = tk.Text(form, name='instruction', height=3, width=70, wrap='word')
    sentence.grid(row=1, column=1, sticky='ew', pady=5)
    ttk.Label(form, text='실행 때 받을 변수').grid(row=2, column=0, sticky='w')
    ttk.Entry(form, name='parameters', textvariable=parameters).grid(row=2, column=1, sticky='ew')
    ttk.Label(form, text='여러 변수는 쉼표로 구분합니다. 예: FAMILY, 제품코드', foreground='#555').grid(row=3, column=1, sticky='w')
    preview = tk.StringVar(value='대상을 추가하거나 녹화를 시작하세요.')
    ttk.Label(form, name='preview', textvariable=preview, wraplength=700).grid(row=4, column=0, columnspan=2, sticky='w', pady=5)

    def status(item):
        if item['action'] != 'intent':
            return '설정된 동작'
        if item.get('anchor') is None:
            return '대상 지정 필요'
        return '실행 시 동작 해석' if item['instruction']['text'].strip() else '동작 설명 필요'

    def render():
        keys = {item['id'] for item in items}
        for key in listing.get_children():
            if key not in keys:
                listing.delete(key)
        for number, item in enumerate(items, 1):
            text = item.get('instruction', {}).get('text', item.get('label', ''))
            row = (number, item.get('alias') or item['label'], text.replace('\n', ' '), status(item))
            if listing.exists(item['id']):
                listing.item(item['id'], values=row)
            else:
                listing.insert('', 'end', iid=item['id'], values=row)

    def error(exc):
        code = getattr(exc, 'code', str(exc))
        preview.set({'invalid_step_annotation': '별칭을 120바이트 이내로 입력하세요.',
                     'invalid_natural_instruction': '동작 설명을 확인하세요.',
                     'target_position_unavailable': '대상을 읽지 못했습니다. 동작 설명을 먼저 적고 나중에 대상을 다시 지정할 수 있습니다.'}.get(
                         code, '입력 내용을 확인하세요: ' + code))

    def flush():
        if editing is None:
            return True
        try:
            annotation = learned_tasks._annotations({'alias': alias.get()})
            if editing['action'] == 'intent':
                text = sentence.get('1.0', 'end-1c')
                variables = [value.strip() for value in parameters.get().split(',') if value.strip()]
                source = dict(editing)
                source.pop('alias', None)
                updated = intent_tasks.make_step(source, text, parameters=variables)
                editing.clear()
                editing.update(updated, id=selected_id[0], **annotation)
            else:
                editing.pop('alias', None)
                editing.update(annotation)
            render()
            preview.set(status(editing) + ' — 설명은 그대로 저장하고 실제 동작은 실행할 때 해석합니다.')
            return True
        except ValueError as exc:
            error(exc)
            return False

    selected_id = [None]

    def load(item):
        nonlocal editing
        editing = item
        selected_id[0] = item['id']
        configured = item['action'] != 'intent'
        alias.set(item.get('alias', ''))
        sentence.configure(state='normal')
        sentence.delete('1.0', 'end')
        sentence.insert('1.0', item['label'] if configured else item['instruction']['text'])
        sentence.configure(state='disabled' if configured else 'normal')
        parameters.set(', '.join(item.get('instruction', {}).get('parameters', [])))
        preview.set(status(item))

    def select(item):
        listing.selection_set(item['id'])
        listing.see(item['id'])
        load(item)

    def selected(*_):
        values = listing.selection()
        if not values or (editing and editing['id'] == values[0]):
            return
        if not flush():
            if editing:
                listing.selection_set(editing['id'])
            return
        item = next((item for item in items if item['id'] == values[0]), None)
        if item:
            load(item)

    listing.bind('<<TreeviewSelect>>', selected)

    def sync():
        if session.started and not session.ended:
            session.poll()
        for item in session.notes.snapshot():
            if item['id'] not in seen:
                seen.add(item['id'])
                items.append(item)
        render()
        if items and editing is None:
            select(items[0])

    def stop():
        if session.started and not session.ended:
            session.finish(report_empty=False)
            sync()

    def start():
        nonlocal session
        if sampling or not flush():
            return
        if session.started and not session.ended:
            return
        if session.ended:
            session = PositionSession(binding, title)
        session.notes.max_steps = max(0, learned_tasks.MAX_STEPS - len(items))
        if not session.notes.max_steps:
            message.set('단계 수 제한에 도달했습니다. 필요 없는 단계를 삭제하세요.')
            return
        try:
            session.start()
            message.set('클릭한 위치만 기록 중입니다. 실제로 클릭하면 업무 화면도 반응합니다. 클릭 없이 알려주려면 대상 추가를 사용하세요.')
        except Exception as exc:
            error(exc)

    def finish():
        stop()
        message.set('녹화를 멈췄습니다. 각 단계의 동작을 적은 뒤 전체 저장을 누르세요.')

    def move(offset):
        if not flush() or not items:
            return
        index = items.index(editing) if editing in items else 0
        following = max(0, min(len(items) - 1, index + offset))
        select(items[following])
        sentence.focus_set()
        if following == index and offset > 0:
            message.set('마지막 단계입니다. 대상 추가로 다음 위치를 알려주거나 전체 저장을 누르세요.')

    def remove():
        nonlocal editing
        if editing is None or sampling:
            return
        index = items.index(editing)
        seen.add(editing['id'])
        items.remove(editing)
        editing = None
        render()
        if items:
            select(items[min(index, len(items) - 1)])
        else:
            sentence.configure(state='normal')
            sentence.delete('1.0', 'end')
            alias.set('')
            parameters.set('')
            preview.set('대상 추가로 새 단계를 만드세요.')

    def add_or_retarget(replace=False):
        nonlocal sampling, job
        if sampling or not flush():
            return
        if replace and (editing is None or editing['action'] != 'intent'):
            preview.set('설정된 동작은 동작 수정에서 다시 가르치세요.')
            return
        if not replace and len(items) >= learned_tasks.MAX_STEPS:
            message.set('단계 수 제한에 도달했습니다.')
            return
        stop()
        destination = editing if replace else None
        sampling = True
        message.set('3초 안에 업무 화면의 대상 위로 마우스를 옮기세요. 클릭하지 않아도 됩니다.')

        def begin_sample():
            nonlocal job
            try:
                job = SampleJob(session.binding, 'anchor', (bridge.cursor(),), reader=_read_sample)
                root.after(40, collect)
            except Exception as exc:
                complete(None, exc)

        def complete(picked, exc=None):
            nonlocal job, sampling
            if job is not None:
                job.close()
                job = None
            sampling = False
            preserve_form = not flush()
            if destination is not None:
                if picked is not None:
                    destination.update(anchor=picked['anchor'], label=picked['label'])
                selected_item = destination
            else:
                selected_item = draft(**picked) if picked is not None else draft()
                items.append(selected_item)
            render()
            if not preserve_form:
                select(selected_item)
                sentence.focus_set()
            message.set('대상을 지정했습니다. 실행할 동작을 자유롭게 적으세요.' if picked else
                        '대상을 읽지 못했습니다. 설명을 먼저 적거나 대상 다시 지정을 누르세요.')

        def collect():
            try:
                picked = job.poll()
                if picked is None:
                    root.after(40, collect)
                else:
                    complete(picked)
            except Exception as exc:
                complete(None, exc)

        root.after(3000, begin_sample)

    def extra(kind, replace=False):
        nonlocal previous_columns
        if sampling or not flush():
            return
        stop()
        if not replace and len(items) >= learned_tasks.MAX_STEPS:
            return
        if kind == 'row':
            from row_teach import teach_row_dialog
            answer = teach_row_dialog(root, session.binding, previous_columns)
            item = None
            if answer:
                item, previous_columns = answer
        else:
            from context_teach import teach_context_dialog
            item = teach_context_dialog(root, session.binding)
        if item:
            if replace and editing in items:
                item['id'] = editing['id']
                if editing.get('alias'):
                    item['alias'] = editing['alias']
                items[items.index(editing)] = item
            else:
                items.append(item)
            render()
            select(item)

    def edit_action():
        if editing and editing['action'] in {'row-action', 'result-menu'}:
            extra('row' if editing['action'] == 'row-action' else 'menu', replace=True)
        else:
            sentence.focus_set()

    def save():
        nonlocal result
        if sampling or not flush():
            return
        stop()
        if not items:
            message.set('저장할 단계가 없습니다. 대상을 추가하세요.')
            return
        result = {'status': 'recorded', 'steps': copy.deepcopy(items), 'input_dispatched': False}
        root.destroy()

    def cancel():
        nonlocal result
        result = {'status': 'cancelled', 'steps': [], 'input_dispatched': False}
        root.destroy()

    nav = ttk.Frame(frame)
    nav.pack(anchor='w', pady=5)
    for text, command in [('이전', lambda: move(-1)), ('적용 후 다음', lambda: move(1)), ('다음', lambda: move(1)),
                          ('동작 수정', edit_action), ('단계 삭제', remove), ('대상 다시 지정 (3초)', lambda: add_or_retarget(True))]:
        ttk.Button(nav, text=text, command=command).pack(side='left', padx=2)
    controls = ttk.Frame(frame)
    controls.pack(anchor='w')
    for text, command in [('대상 추가 (3초)', add_or_retarget), ('시작', start), ('마치고 검토', finish),
                          ('전체 저장', save), ('취소', cancel)]:
        ttk.Button(controls, text=text, command=command).pack(side='left', padx=2)
    extras = ttk.Frame(frame)
    extras.pack(anchor='w', pady=5)
    ttk.Label(extras, text='추가 설정:').pack(side='left')
    ttk.Button(extras, text='행 버튼 가르치기', command=lambda: extra('row')).pack(side='left', padx=2)
    ttk.Button(extras, text='조회 후 우클릭 메뉴 가르치기', command=lambda: extra('menu')).pack(side='left', padx=2)
    root.protocol('WM_DELETE_WINDOW', cancel)
    render()
    if items:
        select(items[0])

    def tick():
        nonlocal timer
        if session.started and not session.ended:
            sync()
        timer = root.after(200, tick)

    def failed(*_):
        nonlocal result
        result = {'status': 'blocked', 'diagnostic': 'recording_ui_failed', 'input_dispatched': False}
        root.destroy()

    root.report_callback_exception = failed
    timer = root.after(200, tick)
    try:
        root.mainloop()
        return result or {'status': 'cancelled', 'steps': [], 'input_dispatched': False}
    finally:
        if job is not None:
            job.close()
        if not session.ended:
            session.cancel()
