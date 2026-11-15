"""Follow a demonstrated workflow through explicitly chosen or related windows."""
from __future__ import annotations

import time
from collections import deque

import capture_windows as windows
import learned_tasks
import win_bridge as bridge


def finalize_contexts(steps):
    """Preserve trigger identity through review edits, then number the final steps."""
    numbers = {s['id']: i for i, s in enumerate(steps, 1)}
    result = []
    for number, raw in enumerate(steps, 1):
        step = raw.copy()
        scope = step.pop('_capture_window', None)
        if scope:
            context = scope.copy()
            if '_openers' in context:
                raise ValueError(f'{number}단계를 선택하고 ‘창을 연 단계 지정’을 눌러 연결하세요.')
            trigger = context.pop('after_id', None)
            if trigger is not None:
                if trigger not in numbers or numbers[trigger] >= number:
                    raise ValueError(f'{number}단계를 연 동작이 없습니다. 이 창의 단계를 다시 녹화하세요.')
                context['after_step'] = numbers[trigger]
            step['window_context'] = context
        result.append(step)
    return result


class CaptureTrail:
    """One recorder per approved surface; only its input events enter its model."""

    def __init__(self, binding, title, session_type=None):
        if session_type is None:
            from teach_capture import CaptureSession
            session_type = CaptureSession
        self.session_type = session_type
        self.initial_binding, self.initial_title = binding, title
        self.primary = self.active = session_type(binding, title)
        self.parts = [(self.primary, None)]
        self.connected = {(binding.hwnd, binding.pid): self.primary}
        self.notes = self
        self.max_steps = learned_tasks.MAX_STEPS
        self.started = self.ended = False
        self.baseline = None
        self.appeared = {}
        self.link_times = []
        self.outside = deque(maxlen=16)
        self.last_inventory = 0.
        self.active_hint = ''
        self._last_candidate = None
        self._next_attempt = 0.
        self._resume = []

    @property
    def binding(self):
        return self.active.binding

    @property
    def state(self):
        return self.active.notes.state

    @property
    def limit(self):
        return (sum(len(part.notes.steps) for part, _ in self.parts) >= self.max_steps
                or any(part.notes.limit for part, _ in self.parts))

    @property
    def scope(self):
        return next(scope for part, scope in self.parts if part is self.active)

    def binding_for(self, step):
        scope = step.get('_capture_window')
        return next((part.binding for part, description in reversed(self.parts)
                     if scope is description), None)

    def start(self):
        try:
            self.baseline = windows.visible_window_keys()
        except (bridge.BridgeError, OSError):
            # Keep the original recorder available when enumeration fails.
            self.baseline = None
        self.primary.notes.max_steps = self.max_steps
        self.primary.pump.outside_marker = lambda key, at: self.outside.append((key, at))
        self.primary.start()
        self.started = True
        for previous, scope in self._resume:
            key = previous.hwnd, previous.pid
            try:
                previous.check()
                found = windows.candidate(key, self.primary.binding, self.initial_title,
                                          automatic=False, baseline=self.baseline)
                if found and found[1] == previous.title and found[0].created == previous.created:
                    self._attach(*found, key, scope)
            except (bridge.BridgeError, OSError, ValueError):
                continue
        self.active = self.primary

    def successor(self):
        """Resume previously linked scopes after teaching a row or menu in the editor."""
        following = CaptureTrail(self.initial_binding,self.initial_title,self.session_type)
        following._resume = [(part.binding, scope) for part, scope in self.parts
                             if scope and part.notes.state != 'closed']
        return following

    def _ordered(self):
        result = []
        for part, scope in self.parts:
            with part.notes.lock:
                for step in part.notes.snapshot():
                    if scope:
                        step['_capture_window'] = scope
                    result.append((part.notes.times[step['id']], step))
        return sorted(result, key=lambda row: row[0])

    def snapshot(self):
        steps = [step for _,step in self._ordered()]
        if len(steps) > self.max_steps:
            from teach_capture import manual
            return steps[:self.max_steps-1] + [manual('recording_step_limit', 'multi-window-limit')]
        return steps

    def _openers(self, at):
        # A startup can take tens of seconds. A window seen before this action
        # cannot be its result, even if the user later foregrounds it.
        result = []
        for part, _ in self.parts:
            with part.notes.lock:
                for key, step in part.notes.steps.items():
                    when = part.notes.times[key]
                    if not 0 <= at-when <= 90:
                        continue
                    reason = step.get('recognition', {}).get('reason')
                    if (step['action'] in ('click','select','expand','row-action','result-menu')
                            or reason in ('row_capture_requires_review', 'stable_locator_unavailable',
                                          'capture_target_unavailable', 'unsupported_recorded_action')):
                        result.append((when, part, key))
        # A completed transition followed by another demonstrated action in
        # the same origin starts a new sequence. Two actions both preceding
        # the first arrival remain ambiguous, regardless of arrival order.
        sources = {key: part for _, part, key in result}
        last_action = {}
        for when, part, _ in result:
            last_action[part] = max(last_action.get(part, when), when)
        used = set()
        for seen, scope in self.link_times:
            opener = scope.get('after_id')
            if opener in sources and last_action[sources[opener]] > seen:
                used.add(opener)
        return sorted((row for row in result if row[2] not in used), key=lambda row: row[0])

    def _inventory(self):
        now = time.monotonic()
        keys = windows.visible_window_keys()
        if self.baseline is not None:
            self.appeared = {key: self.appeared.get(key, now) for key in keys
                             if key not in self.baseline}
        self.last_inventory = now

    def choice_scope(self):
        """Take a stable recorder scope on the GUI thread for listing in a worker."""
        return tuple(part.binding for part in self.connected.values()), self.binding

    def connect_choice(self, choice):
        """Confirm the displayed window again; selecting it sends no desktop input."""
        self.active_hint = ''
        try:
            self.primary.binding.check()
            confirmed = windows.confirm_choice(choice)
            if self.connect((confirmed[0].hwnd, confirmed[0].pid), confirmed=confirmed):
                return True
        except (bridge.BridgeError, OSError, ValueError) as exc:
            if getattr(exc, 'code', None) == 'recorded_window_ambiguous':
                self.active_hint = '프로그램과 창 제목이 같은 창이 여러 개입니다. 제목을 구분한 후 새로 고치세요.'
            elif getattr(exc, 'code', None) in ('recorded_window_lifetime_changed','target_lifetime_changed',
                                                'target_window_mismatch','target_executable_mismatch',
                                                'target_unavailable','business_window_changed'):
                self.active_hint = '선택한 창이 닫혔거나 변경됐습니다. 목록을 새로 고치고 다시 선택하세요.'
        if not self.active_hint:
            self.active_hint = '창을 연결할 수 없습니다. 목록을 새로 고치고 다시 선택하세요.'
        return False

    def connect(self, key, *, automatic=False, confirmed=None):
        if not key or not self.started or self.ended or self.limit:
            return False
        existing = self.connected.get(key)
        if existing is not None:
            existing.binding.check()
            if confirmed and (existing.binding.created != confirmed[0].created or existing.binding.title != confirmed[1]):
                return False
            self.active = existing
            return True
        if len(self.connected) >= 16:
            self.active_hint = '연결된 창이 많습니다. 현재 단계를 저장하고 녹화를 나누세요.'
            return False
        if key not in self.appeared and key not in (self.baseline or ()):
            self._inventory()
        when = self.appeared.get(key)
        options = self._openers(when) if when is not None else []
        if automatic and not options:
            return False
        if automatic:
            # A down event can launch a window before its worker finishes processing
            # the click. Do not assign an older button while that event is in flight.
            for part, _ in self.parts:
                down = getattr(part.pump, 'last_scoped_down', None)
                if isinstance(down, (int,float)) and down <= when:
                    with part.notes.lock:
                        if down > max(part.notes.times.values(), default=0):
                            return False
            matches = {}
            for _, parent, _ in options:
                if parent not in matches:
                    try:
                        matches[parent] = windows.candidate(key, parent.binding, parent.binding.title,
                                                           automatic=True, baseline=self.baseline)
                    except (bridge.BridgeError, OSError, ValueError):
                        matches[parent] = None
            options = [item for item in options if matches[item[1]]]
            found = matches[options[-1][1]] if options else None
        elif confirmed is not None:
            found = confirmed
            # Independent applications opened by other means are prerequisites.
            # Record a causal link only if a demonstrated opener belongs to
            # the same process, an owning window or an ancestor process.
            if options:
                related = set()
                for part in {part for _, part, _ in options if not part.ended}:
                    try:
                        if windows.related(found[0], part.binding):
                            related.add(part)
                    except (bridge.BridgeError, OSError):
                        pass
                options = [row for row in options if row[1] in related]
        else:
            parent = self.active
            found = windows.candidate(key, parent.binding, parent.binding.title, automatic=False,
                                      baseline=self.baseline)
        if not found:
            return False
        binding, title = found
        scope = dict(exe=binding.exe, title=title)
        if key in (self.baseline or ()) or (confirmed and not options):
            scope['mode'] = 'existing'
        elif options:
            # Earlier launches can complete after a later launch. Timing and
            # foreground order alone cannot distinguish those two device windows.
            if len(options) == 1:
                scope['after_id'] = options[0][2]
            else:
                scope['_openers'] = tuple(record[2] for record in options)
            if automatic or confirmed:
                scope['related'] = True
        else:
            self.active_hint = '이 창을 연 녹화 단계가 없습니다. 창을 열기 전부터 다시 녹화하세요.'
            return False

        before_attach = time.monotonic()
        self._attach(binding, title, key, scope)
        if when is not None:
            self.link_times.append((when, scope))
        hook_installing_at = getattr(self.active, 'hook_installing_at', before_attach)
        if automatic and any(surface == key and options[0][0] <= at < hook_installing_at
                             for surface, at in tuple(self.outside)):
            # Preserve an editable step if the new surface became interactive
            # before its relationship and exact identity could be established.
            from teach_capture import manual
            step = manual('capture_target_unavailable')
            step['label'] = '창 연결 직전 동작'
            self.active.notes.add(step)
        self.active_hint = (f'{title} 창에서 이어서 녹화합니다. 검토할 때 창을 연 단계를 지정하세요.'
                            if '_openers' in scope else f'{title} 창에서 이어서 녹화합니다.')
        return True

    def _attach(self, binding, title, key, scope):
        part = self.session_type(binding, title)
        part.linked = True
        part.notes.max_steps = max(1, self.max_steps - sum(len(p.notes.steps) for p,_ in self.parts))
        others = frozenset(handle for handle, _ in self.connected)
        part.binding.other_roots = others
        for previous, _ in self.parts:
            previous.binding.other_roots |= {binding.hwnd}
        try:
            part.start(prime_rows=False)
        except Exception as exc:
            part.cancel()
            for previous, _ in self.parts:
                previous.binding.other_roots -= {binding.hwnd}
            raise bridge.BridgeError('linked_window_recorder_unavailable') from exc
        self.parts.append((part, scope))
        self.connected[key] = self.active = part

    def poll(self):
        for part, _ in tuple(self.parts):
            if not part.ended:
                part.poll()
                if part.ended and part is not self.primary:
                    self.connected.pop((part.binding.hwnd, part.binding.pid), None)
                    for previous, _ in self.parts:
                        previous.binding.other_roots -= {part.binding.hwnd}
                    if self.active is part:
                        self.active = self.primary
        if self.ended or not self.started or self.primary.notes.state == 'paused' or self.limit:
            return
        try:
            if time.monotonic() - self.last_inventory >= .25:
                self._inventory()
            key = windows.foreground_key()
        except (bridge.BridgeError, OSError):
            self.last_inventory = time.monotonic()
            return
        if not key:
            return
        if key in self.connected:
            if self.active is not self.connected[key]:
                try:
                    self.connect(key)
                except (bridge.BridgeError, OSError):
                    return
                self.active_hint = f'{self.binding.title} 창으로 돌아왔습니다.'
            return
        # Recheck after a delayed UIA observation publishes the opener, but
        # avoid repeatedly enumerating application windows for an unrelated app.
        if key not in self.appeared:
            return
        options = self._openers(self.appeared[key])
        signature = key, self.appeared[key], tuple(item[2] for item in options)
        if options and (self._last_candidate != signature or time.monotonic() >= self._next_attempt):
            self._last_candidate = signature
            self._next_attempt = time.monotonic() + 1.
            try:
                self.connect(key, automatic=True)
            except (bridge.BridgeError, OSError):
                self.active_hint = '새 창의 연결을 확인하지 못했습니다. 필요하면 이어갈 창을 선택하세요.'

    def finish(self, *, report_empty=True):
        self.ended = True
        for part, _ in self.parts:
            if part.started and not part.ended:
                part.finish(report_empty=False)
        steps = self.snapshot()
        if not steps and report_empty:
            from teach_capture import manual
            steps = [manual('empty_recording_requires_review')]
        return dict(status='recorded', steps=steps, input_dispatched=False)

    def cancel(self):
        self.ended = True
        for part, _ in self.parts:
            part.cancel()
        return dict(status='cancelled', steps=[], input_dispatched=False)
