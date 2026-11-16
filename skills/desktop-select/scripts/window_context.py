"""Bind explicitly recorded top-level windows over one workflow run."""
from collections import defaultdict
import re
import time


OPEN_TIMEOUT = 30


class ContextError(ValueError):
    def __init__(self, code, *, step_number=None):
        super().__init__(code)
        self.code = code
        self.step_number = step_number
        self.input_dispatched = False


def validate_context(spec):
    if not isinstance(spec, dict):
        raise ContextError('invalid_window_context')
    exe, title = spec.get('exe'), spec.get('title')
    if (not isinstance(exe, str) or not exe or exe != exe.strip() or len(exe) > 200
            or not exe.casefold().endswith('.exe') or re.search(r'[<>:"/\\|?*\x00-\x1f]', exe)
            or not isinstance(title, str) or not title.strip() or len(title) > 1024
            or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in title)):
        raise ContextError('invalid_window_context')
    if set(spec) == {'exe', 'title', 'mode'} and spec['mode'] == 'existing':
        return spec
    if (set(spec) - {'related'} == {'exe', 'title', 'after_step'} and type(spec['after_step']) is int
            and spec['after_step'] > 0 and ('related' not in spec or spec['related'] is True)):
        return spec
    raise ContextError('invalid_window_context')


def validate_sequence(steps):
    for number, step in enumerate(steps, 1):
        if isinstance(step, dict) and 'window_context' in step:
            spec = validate_context(step['window_context'])
            source = spec.get('after_step')
            if source is not None and (source >= number or not isinstance(steps[source-1], dict) or
                    steps[source-1].get('action') in ('wait', 'verify-file', 'manual')):
                raise ContextError('invalid_window_origin_step', step_number=number)


def _key(spec):
    return spec['exe'].casefold(), spec['title'], spec.get('after_step'), spec.get('related', False)


def _identity(row):
    return row['hwnd'], row['pid'], row['process_started_utc_ticks']


class WindowRoutes:
    """Snapshot expected windows before the opener and pin their live lifetimes."""
    def __init__(self, steps, owner, title, *, read_only=False, active=None):
        import desktop_select as app
        self.app = app
        self.steps, self.owner, self.title, self.read_only = steps, owner, title, read_only
        self.origins = defaultdict(dict)
        self.baselines, self.outcomes, self.bindings, self.openers = {}, {}, {}, {}
        validate_sequence(steps)
        for number in (range(1, len(steps)+1) if active is None else active):
            spec = steps[number-1].get('window_context')
            if spec is None:
                continue
            key = _key(spec)
            if key[0] == owner.exe.casefold() and key[1] == title:
                raise ContextError('window_context_is_main_window')
            source = spec.get('after_step')
            if source is not None:
                self.origins[source][key] = spec
            elif key not in self.bindings:
                records = self._rows(spec)
                match = self._match(spec, records)
                if match is None and not read_only:
                    raise ContextError('recorded_existing_window_missing')
                self.bindings[key] = self._bind(match) if match is not None else None

    def _rows(self, spec):
        self.app.guard(self.owner, self.owner, self.title)
        return self.app.windows(spec['exe'])

    @staticmethod
    def _match(spec, records):
        matches = [row for row in records if row['title'] == spec['title']]
        if len(matches) > 1:
            raise ContextError('recorded_window_ambiguous')
        return matches[0] if matches else None

    def _bind(self, row):
        binding = self.app.bridge.Binding(row)
        self.app.guard(binding, binding, row['title'])
        return binding

    def before(self, number, origin_binding=None):
        if number in self.origins:
            self.openers[number] = self.owner if origin_binding is None else origin_binding
        if self.read_only:
            return
        for key, spec in self.origins.get(number, {}).items():
            rows = self._rows(spec)
            exact = self._match(spec, rows)
            self.baselines[key] = ({_identity(row) for row in rows},
                                   _identity(exact) if exact is not None else None)

    def after(self, number, receipt):
        if number in self.origins:
            self.outcomes[number] = receipt

    def existing_for_row(self, number):
        """Check the linked title, process, relationship and original lifetime before skipping a button."""
        specs = self.origins.get(number, {})
        if len(specs) != 1:
            raise ContextError('row_requires_one_linked_window')
        key, spec = next(iter(specs.items()))
        current = self._match(spec,self._rows(spec))
        identity = _identity(current) if current is not None else None
        if not self.read_only and identity != self.baselines[key][1]:
            raise ContextError('recorded_window_lifetime_changed')
        if current is None:
            return False
        self.check(spec,self._bind(current))
        return True

    def check(self, spec, binding):
        row = self._match(spec, self._rows(spec))
        if row is None or _identity(row) != (binding.hwnd, binding.pid, binding.created):
            raise ContextError('recorded_window_lifetime_changed')
        self.app.guard(binding, binding, spec['title'])
        if spec.get('related'):
            from capture_windows import related
            opener = self.openers.get(spec['after_step'])
            if opener is None or not related(binding, opener):
                raise ContextError('recorded_window_relationship_changed')

    def resolve(self, spec):
        key = _key(spec)
        if key in self.bindings:
            found = self.bindings[key]
            if found is not None:
                self.check(spec, found)
            return found
        source = spec['after_step']
        if self.read_only and spec.get('related') and source not in self.openers:
            # The opener may belong to a deferred or unselected nested scope.
            if self.steps[source-1].get('window_context') is not None:
                return None
            self.openers[source] = self.owner
        outcome = self.outcomes.get(source)
        already_running = False
        if not self.read_only:
            if outcome is None or key not in self.baselines:
                raise ContextError('window_origin_step_not_run')
            if outcome.get('status') not in ('verified', 'delivered_unverified'):
                raise ContextError('window_origin_not_confirmed')
            opener = self.steps[source-1]['action']
            same_demonstrated_row = (opener == 'row-action' or
                                     (opener == 'row-open' and outcome.get('evidence') == 'recorded_row_and_window') or
                                     (opener == 'row-launch' and outcome.get('evidence') == 'recorded_row_hint'))
            already_running = (same_demonstrated_row
                               and outcome.get('status') == 'verified'
                               and outcome.get('input_dispatched') is False
                               and outcome.get('observed_state') == 'after')
            if outcome.get('input_dispatched') is not True and not already_running:
                raise ContextError('window_origin_not_confirmed')
        deadline = time.monotonic() + (0 if self.read_only else OPEN_TIMEOUT)
        while True:
            row = self._match(spec, self._rows(spec))
            if row is not None:
                if not self.read_only:
                    before, previous_exact = self.baselines[key]
                    identity = _identity(row)
                    if identity in before and not (already_running and identity == previous_exact):
                        raise ContextError('expected_new_window_already_open')
                    if already_running and identity != previous_exact:
                        raise ContextError('existing_row_window_not_confirmed')
                found = self._bind(row)
                self.check(spec, found)
                self.bindings[key] = found
                return found
            if self.read_only:
                return None
            if time.monotonic() >= deadline:
                raise ContextError('recorded_window_not_open')
            time.sleep(.2)
