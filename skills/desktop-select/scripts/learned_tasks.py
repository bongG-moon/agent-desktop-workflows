"""Store demonstrated UI identities locally and replay them through Desktop Select."""
from __future__ import annotations

from collections import Counter
import json
import ntpath
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import tempfile

from targeting import _protected, _tree, SelectionError


MAX_STEPS = 300
MAX_FILE_BYTES = 4 * 1024 * 1024
INVOKE = {'button', 'hyperlink', 'menuitem', 'splitbutton'}
CHOOSE = {'listitem', 'tab', 'treeitem'}
EXPAND = {'menuitem', 'combobox', 'treeitem', 'splitbutton'}


class TaskError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _text(value, limit=1024):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= limit
            and not any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def _name(name):
    if (not _text(name, 100) or name != name.strip() or name.endswith('.') or
            re.search(r'[<>:"/\\|?*]', name)):
        raise TaskError('invalid_task_name')
    filename = name + '.json'
    reserved = ntpath.isreserved(filename) if hasattr(ntpath, 'isreserved') else PureWindowsPath(filename).is_reserved()
    if reserved:
        raise TaskError('invalid_task_name')
    return name


def _window(exe, window):
    if (not _text(exe, 200) or not exe.lower().endswith('.exe') or
            re.search(r'[<>:"/\\|?*]', exe) or not _text(window)):
        raise TaskError('invalid_task_window')


def _reason(value):
    return value if isinstance(value, str) and re.fullmatch('[a-z][a-z0-9_]{0,79}', value) else 'action_needs_review'


def _manual(reason):
    return {'action': 'manual', 'reason': _reason(reason)}


def _annotations(raw):
    fields = {}
    for key, limit in (('alias', 120), ('description', 1000)):
        value = raw.get(key, '')
        if value == '':
            continue
        if not _text(value, limit) or len(value.encode('utf-8')) > limit:
            raise TaskError('invalid_step_annotation')
        value = value.strip()
        if key == 'alias' and (value.isdecimal() or '=' in value):
            raise TaskError('invalid_step_alias')
        fields[key] = value
    return fields


def _selector(raw):
    if not isinstance(raw, dict) or not _text(raw.get('role'), 80):
        return None
    for key in ('automation_id', 'name'):
        if _text(raw.get(key)) and not (key == 'automation_id' and raw[key].isdecimal()):
            return {'role': raw['role'], key: raw[key]}
    return None


def _point_target(raw):
    return (raw.get('action') == 'teach-target' and 'value' not in raw
            and raw.get('recognition') == {'target': 'identified', 'action': 'requires_instruction',
                                           'result': 'manual', 'reason': 'combo_choice_unproven'})


def _convert(raw, *, allow_point=False):
    if not isinstance(raw, dict):
        return _manual('capture_target_unavailable')
    source = raw.get('target')
    recognition = raw.get('recognition')
    recognition = recognition if isinstance(recognition, dict) else {}
    point = allow_point and _point_target(raw)
    if (not isinstance(source, dict) or source.get('kind') != 'element' or raw.get('action') == 'manual'
            or (not point and (recognition.get('action') != 'supported' or recognition.get('target') != 'identified'))):
        return _manual(recognition.get('reason', 'unsupported_demonstration'))
    if 'window' in source:
        return _manual('popup_needs_live_binding')
    locator = source.get('locator')
    selector = _selector(locator)
    if not selector:
        return _manual('stable_locator_unavailable')
    role = selector['role']
    match = 'automation_id' if 'automation_id' in selector else 'name'
    step = {'target': selector[match], 'role': role, 'match': match}
    if role in INVOKE and match == 'automation_id' and _text(locator.get('name')):
        step['command_name'] = locator['name']
    if _text(raw.get('field_label'), 200):
        step['label'] = raw['field_label']
    chain = locator.get('ancestors', [])
    if not isinstance(chain, list) or len(chain) > 4:
        return _manual('invalid_ancestor_chain')
    ancestors = [_selector(parent) for parent in chain]
    if not all(ancestors):
        return _manual('invalid_ancestor_chain')
    if ancestors:
        step['ancestors'] = ancestors
        step['within'] = ancestors[0].get('automation_id') or ancestors[0]['name']
    action = raw.get('action')
    if role == 'listitem' and any(a['role'] == 'combobox' for a in ancestors):
        return _manual('combo_option_owner_required')
    if point and role == 'combobox':
        step.update(action='select', parameter=step.get('label') or step['target'], recorded_effect='unobserved')
    elif action == 'set-value' and role == 'combobox':
        value = raw.get('value')
        selection = raw.get('selection') or {}
        if not isinstance(selection, dict) or selection.get('result') == 'mismatch' or recognition.get('result') == 'mismatch':
            return _manual('combo_selection_mismatch')
        if not _text(value, 10000):
            return _manual('combo_value_unobserved')
        observed = recognition.get('result') == 'checkable'
        clicked = selection.get('source') == 'option_click'
        if not observed and not clicked:
            return _manual('combo_value_unobserved')
        label = raw.get('field_label')
        if not _text(label, 200) or '=' in label:
            label = step['target']
        parameter = label if _text(label, 200) and '=' not in label else 'value'
        step.update(action='select', value=value, parameter=parameter, recorded_effect='matched' if observed else 'unverified')
    elif ((action == 'click' and role in INVOKE) or (action == 'select' and role in CHOOSE)
          or (action == 'expand' and role in EXPAND)):
        step.update(action='open', expand=action == 'expand')
    else:
        return _manual('unsupported_demonstration')
    return step


def _subject_name(subject):
    return re.sub(r'\s*(?:조건|콤보박스|콤보|항목|필드|버튼|메뉴)$', '', subject.strip()).strip()


def _instruction_subject(step, subject):
    candidates = [step.get(k, '') for k in ('label', 'target', 'alias')]
    norm = lambda s: ''.join(s.split()).casefold()
    text = norm(subject)
    without_kind = re.sub(r'(?:조건|콤보박스|콤보|항목|필드|버튼|메뉴)$', '', text)
    if text in {'이항목', '이필드', '이콤보', '이메뉴', '이버튼'}:
        return next(c for c in candidates if c)
    for c in candidates:
        if c and norm(c) in (text, without_kind):
            return c
    raise TaskError('instruction_target_mismatch')


def _parse_sentence(step, sentence, resolve_subject=_instruction_subject):
    """Only parse one bounded, field-specific instruction. No shell or model evaluation."""
    verb = r'(?:선택|설정|입력|변경|해)(?:해)?\s*(?:줘|주세요)?'
    tail = r'(?:[.!。])?'
    required = re.fullmatch(r'입력\s*받은\s*(?:\{([^{}]+)\}\s*)?변수(?:를|을)\s*(.+?)에\s*' + verb + tail, sentence)
    assign = re.fullmatch(r'(.+?)(?:을|를)\s*(.+?)(?:으로|로)\s*' + verb + tail, sentence)
    into = re.fullmatch(r'\{([^{}]+)\}(?:을|를)\s*(.+?)에\s*' + verb + tail, sentence)
    if required or into:
        match = required or into
        field = resolve_subject(step, match[2])
        return 'required', match[1] or field
    if assign:
        resolve_subject(step, assign[1])
        val = assign[2].strip()
        variable = re.fullmatch(r'\{([^{}]+)\}', val)
        if variable:
            return 'required', variable[1]
        if not re.fullmatch(r'[\w +./()-]+', val):
            return None
        return 'fixed', val
    command = re.fullmatch(r'(.+?)(?:을|를)\s*(누르기|눌러|클릭|펼치기|펼쳐|선택|실행)(?:해)?\s*(?:줘|주세요)?' + tail, sentence)
    if command:
        resolve_subject(step, command[1])
        if step.get('action') != 'open' or (command[2] in ('펼치기', '펼쳐') and not step.get('expand')):
            raise TaskError('instruction_action_mismatch')
        return 'recorded', None
    return None


def instruction_subject(sentence):
    """Extract a displayed field name for explicit user binding, never for execution."""
    found = []
    def remember(_step, subject):
        value = _subject_name(subject)
        found.append(value)
        return value
    if not isinstance(sentence, str):
        return None
    parsed = _parse_sentence({'action': 'open', 'expand': True}, sentence.strip(), remember)
    return found[0] if parsed and len(found) == 1 else None


def bind_instruction_subject(raw, others, sentence, *, mode=None, argument=None):
    """Propose a user-confirmed alias only for an otherwise unnamed taught combo.

    Known labels and the identities/names of other controls retain their guard.
    The caller must show this explicit association before storing the alias.
    """
    step = _convert(raw, allow_point=True)
    if step.get('role') != 'combobox' or step.get('action') != 'select':
        raise TaskError('instruction_target_unavailable')
    subject = instruction_subject(sentence)
    if not subject or subject in {'이 항목', '이 필드', '이 콤보'}:
        raise TaskError('instruction_requires_structure')
    normalize = lambda text: ''.join(text.split()).casefold()
    if step.get('label') and normalize(step['label']) != normalize(step['target']):
        _instruction_subject(step, subject)  # An actual known label cannot be renamed by this shortcut.
    for other in others:
        other_step = _convert(other, allow_point=True)
        other_step.update(_annotations(other))
        if (other_step.get('target'), other_step.get('match'), other_step.get('ancestors')) == (
                step.get('target'), step.get('match'), step.get('ancestors')):
            continue
        try:
            _instruction_subject(other_step, subject)
        except TaskError:
            continue
        raise TaskError('instruction_target_mismatch')
    linked = dict(raw, alias=subject)
    _annotations(linked)
    directive = compile_instruction(linked, sentence, mode=mode, argument=argument)
    if directive is None or directive.get('mode') not in ('fixed', 'required'):
        raise TaskError('instruction_requires_structure')
    return subject


def _binding(step, text, mode, argument):
    if mode == 'recorded':
        if step.get('action') != 'open' or argument not in (None, ''):
            raise TaskError('instruction_action_mismatch')
        return {'mode': mode, 'text': text}
    if mode not in ('required', 'fixed') or step.get('action') != 'select' or step.get('role') != 'combobox':
        raise TaskError('instruction_action_mismatch')
    if not _text(argument, 200 if mode == 'required' else 10000):
        raise TaskError('invalid_instruction_argument')
    if mode == 'required' and ('=' in argument or argument != argument.strip()):
        raise TaskError('invalid_instruction_argument')
    return {'mode': mode, 'text': text, 'parameter' if mode == 'required' else 'value': argument}


def compile_instruction(raw_step, sentence, *, mode=None, argument=None):
    if not isinstance(raw_step, dict) or not isinstance(sentence, str):
        raise TaskError('invalid_step_instruction')
    sentence = sentence.strip()
    if sentence and (not _text(sentence, 1000) or len(sentence.encode('utf-8')) > 1000):
        raise TaskError('invalid_step_instruction')
    if not sentence and mode is None and argument is None:
        return None
    if isinstance(raw_step.get('target'), dict):
        step = _convert(raw_step, allow_point=True)
        step.update(_annotations(raw_step))
    else:
        step = raw_step
    if step.get('action') not in ('select', 'open'):
        raise TaskError('instruction_target_unavailable')
    parsed = _parse_sentence(step, sentence)
    if mode is None:
        if not parsed or argument is not None:
            raise TaskError('instruction_requires_structure')
        mode, argument = parsed
    elif parsed:
        actual_mode, actual_arg = parsed
        if mode != actual_mode or (argument != actual_arg
                and not (mode == 'required' and isinstance(argument, str) and argument.casefold() == actual_arg.casefold())):
            raise TaskError('instruction_binding_conflict')
    return _binding(step, sentence, mode, argument)


def _check_instruction(step, spec):
    if not isinstance(spec, dict):
        raise TaskError('invalid_step_instruction')
    compiled = compile_instruction(step, spec.get('text'), mode=spec.get('mode'),
                                   argument=spec.get('parameter', spec.get('value')))
    if compiled != spec:
        raise TaskError('invalid_step_instruction')
    return compiled


def recorded_step(raw):
    if not isinstance(raw, dict):
        return _manual('capture_target_unavailable')
    if raw.get('action') == 'intent':
        from intent_tasks import validate_step
        step = {k: value for k, value in raw.items() if k != 'id'}
        validate_step(step)
        return step
    if raw.get('action') == 'result-menu':
        step = {'action': 'result-menu', 'label': raw.get('label'), 'context': raw.get('context'), **_annotations(raw)}
        _validate_context(step)
        return step
    if raw.get('action') == 'row-action':
        step = {'action': 'row-action', 'label': raw.get('label'), 'row': raw.get('row'), **_annotations(raw)}
        _validate_row(step)
        return step
    spec = raw.get('instruction')
    step = _convert(raw, allow_point=spec is not None)
    step.update(_annotations(raw))
    if spec is not None:
        step['instruction'] = _check_instruction(step, spec)
        step.pop('parameter', None)
        step.pop('value', None)
    return step


def from_recording(name, exe, window, steps):
    _name(name)
    _window(exe, window)
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise TaskError('invalid_recorded_step_count')
    converted = [recorded_step(raw) for raw in steps]
    counts = Counter(s['parameter'].casefold() for s in converted if 'parameter' in s)
    used = set()
    for number, s in enumerate(converted, 1):
        if 'parameter' in s:
            parameter = s['parameter']
            if counts[parameter.casefold()] > 1:
                parameter += f' [{number}]'
            while parameter.casefold() in used:
                parameter += f' [{number}]'
            s['parameter'] = parameter
            used.add(parameter.casefold())
    return {'schema': 1, 'name': name, 'exe': exe, 'window': window, 'steps': converted}


def _validate_row(step):
    from row_vision import validate_spec
    from win_bridge import BridgeError
    if not _text(step.get('label'), 200) or set(step) - {'action', 'label', 'alias', 'description', 'row'}:
        raise TaskError('invalid_row_step')
    try:
        validate_spec(step.get('row'))
    except BridgeError as error:
        raise TaskError(error.code) from error


def _validate_context(step):
    import result_menu
    if not _text(step.get('label'), 200) or set(step) - {'action', 'label', 'alias', 'description', 'context'}:
        raise TaskError('invalid_result_menu_step')
    result_menu.validate_spec(step.get('context'))


def summary(plan):
    """The local row patches are used by the player and stay out of CLI responses."""
    return dict(plan, steps=[{**{k: v for k, v in step.items() if k not in ('row', 'context')},
                              **({'menu_path': step['context']['menu']['path']} if 'context' in step else {})}
                             for step in plan['steps']])


def _validate(plan):
    if (not isinstance(plan, dict) or set(plan) != {'schema', 'name', 'exe', 'window', 'steps'}
            or type(plan['schema']) is not int or plan['schema'] != 1):
        raise TaskError('invalid_task')
    _name(plan['name'])
    _window(plan['exe'], plan['window'])
    if not isinstance(plan['steps'], list) or not 1 <= len(plan['steps']) <= MAX_STEPS:
        raise TaskError('invalid_task_steps')
    parameters = set()
    for s in plan['steps']:
        if not isinstance(s, dict):
            raise TaskError('invalid_task_step')
        annotation = _annotations(s)
        if any(s[key] != annotation.get(key) for key in ('alias', 'description') if key in s):
            raise TaskError('invalid_step_annotation')
        if s.get('action') == 'intent':
            from intent_tasks import validate_step
            validate_step(s)
            continue
        if s.get('action') == 'row-action':
            _validate_row(s)
            continue
        if s.get('action') == 'result-menu':
            _validate_context(s)
            continue
        if s.get('action') == 'manual':
            if set(s) - set(annotation) != {'action', 'reason'} or s['reason'] != _reason(s['reason']):
                raise TaskError('invalid_task_step')
            continue
        allowed = {'action', 'role', 'target', 'match', 'ancestors', 'within', 'label', 'command_name', *annotation}
        if 'label' in s and not _text(s['label'], 200):
            raise TaskError('invalid_task_target')
        if 'command_name' in s and (s.get('role') not in INVOKE or s.get('match') != 'automation_id'
                                    or not _text(s['command_name'])):
            raise TaskError('invalid_task_target')
        if s.get('match') not in ('name', 'automation_id') or not _text(s.get('target')):
            raise TaskError('invalid_task_target')
        ancestors = s.get('ancestors', [])
        if (not isinstance(ancestors, list) or len(ancestors) > 4
                or any(not isinstance(a, dict) or a != _selector(a) for a in ancestors)):
            raise TaskError('invalid_task_scope')
        scope = (ancestors[0].get('automation_id') or ancestors[0].get('name')) if ancestors else None
        if s.get('within') != scope:
            raise TaskError('invalid_task_scope')
        instruction = None
        if 'instruction' in s:
            allowed.add('instruction')
            instruction = _check_instruction(s, s['instruction'])
        if s.get('action') == 'select':
            allowed.add('recorded_effect')
            if s.get('role') != 'combobox' or s.get('recorded_effect') not in ('matched', 'unverified', 'unobserved'):
                raise TaskError('invalid_task_selection')
            if instruction is None:
                allowed.update(('value', 'parameter'))
                key = s.get('parameter')
                if (not _text(s.get('value'), 10000) or not _text(key, 1000) or '=' in key
                        or key.casefold() in parameters or s.get('recorded_effect') == 'unobserved'):
                    raise TaskError('invalid_task_selection')
                parameters.add(key.casefold())
        elif s.get('action') == 'open':
            allowed.add('expand')
            if (type(s.get('expand')) is not bool or not isinstance(s.get('role'), str)
                    or s['role'] not in (EXPAND if s['expand'] else INVOKE | CHOOSE)):
                raise TaskError('invalid_task_action')
        else:
            raise TaskError('invalid_task_action')
        if set(s) - allowed:
            raise TaskError('unexpected_task_step_data')


def check_display(step, observed):
    """A reusable ID must not authorize a differently labelled field or command."""
    pairs = [(step.get('command_name'), observed.get('name'))]
    if observed.get('label_source') in ('labeled_by', 'sibling_geometry'):
        pairs.append((step.get('label'), observed.get('semantic_label')))
    for recorded, current in pairs:
        if recorded and isinstance(current, str) and current.strip():
            if current.casefold().split() != recorded.casefold().split():
                raise TaskError('recorded_label_changed')


def locate_target(nodes, step):
    """Match the recorded field and its named ancestor chain on this fresh tree."""
    try:
        _, parents = _tree(nodes)
    except SelectionError as error:
        raise TaskError(error.code) from error
    matches = []
    for i, candidate in enumerate(nodes):
        if candidate.get('role') != step['role'] or candidate.get(step['match']) != step['target']:
            continue
        ancestors = iter(parents[i])
        if all(any(all(nodes[j].get(key) == val for key, val in recorded.items()) for j in ancestors)
               for recorded in step.get('ancestors', [])):
            matches.append(i)
    if len(matches) != 1:
        raise TaskError('target_ambiguous' if matches else 'target_missing')
    index = matches[0]
    if any(_protected(nodes[j]) for j in (index, *parents[index])):
        raise TaskError('target_protected')
    if nodes[index].get('enabled') is not True:
        raise TaskError('target_disabled')
    check_display(step, nodes[index])
    return index, step['match']


def _directory(directory):
    if directory is None:
        base = os.environ.get('LOCALAPPDATA')
        if not base:
            raise TaskError('task_store_unavailable')
        directory = Path(base) / 'DesktopSelect' / 'tasks'
    path = Path(directory).absolute()
    if str(path).startswith(('\\\\', '//')):
        raise TaskError('local_task_directory_required')
    for part in (*reversed(path.parents), path):
        _deny_redirect(part)
    return path


def _deny_redirect(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 1024:
        raise TaskError('redirected_task_path')


def check_new_task(name, directory=None):
    try:
        path = _directory(directory) / (_name(name) + '.json')
        _deny_redirect(path)
        if path.exists():
            raise TaskError('task_already_exists')
        return path
    except OSError as error:
        raise TaskError('task_store_unavailable') from error


def save_task(plan, directory=None):
    _validate(plan)
    data = (json.dumps(plan, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    if len(data) > MAX_FILE_BYTES:
        raise TaskError('task_too_large')
    try:
        path = check_new_task(plan['name'], directory)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='wb', dir=path.parent, prefix='.new-', delete=False) as f:
            temp = Path(f.name)
            try:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            except BaseException:
                f.close()
                temp.unlink(missing_ok=True)
                raise
        try:
            if os.name == 'nt':
                os.rename(temp, path)   # Windows RenameFile refuses to replace an existing file.
            else:
                os.link(temp, path)     # Atomic exclusive publication on POSIX.
        finally:
            temp.unlink(missing_ok=True)
        return path
    except FileExistsError as error:
        raise TaskError('task_already_exists') from error
    except OSError as error:
        raise TaskError('task_store_unavailable') from error


def _unique_keys(pairs):
    d = dict(pairs)
    if len(d) != len(pairs):
        raise TaskError('duplicate_task_key')
    return d


def load_task(name, directory=None):
    try:
        path = _directory(directory) / (_name(name) + '.json')
        _deny_redirect(path)
        if not path.is_file():
            raise TaskError('task_not_found')
        with path.open('rb') as file:
            data = file.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise TaskError('task_too_large')
        plan = json.loads(data.decode('utf-8'), object_pairs_hook=_unique_keys)
        _validate(plan)
        if plan['name'].casefold() != name.casefold():
            raise TaskError('task_name_mismatch')
        return plan
    except (json.JSONDecodeError, UnicodeError, RecursionError) as error:
        raise TaskError('invalid_task') from error
    except OSError as error:
        raise TaskError('task_store_unavailable') from error


def list_tasks(directory=None):
    try:
        path = _directory(directory)
        if not path.exists():
            return []
        names = []
        for child in path.iterdir():
            if child.suffix.casefold() == '.json':
                _deny_redirect(child)
                if child.is_file():
                    names.append(child.stem)
        return sorted(names, key=str.casefold)
    except OSError as error:
        raise TaskError('task_store_unavailable') from error


def parse_overrides(items):
    result = {}
    seen = set()
    for item in items or []:
        key, separator, val = item.partition('=')
        if not separator or not _text(key, 1000) or not _text(val, 10000):
            raise TaskError('invalid_override')
        if key.casefold() in seen:
            raise TaskError('duplicate_override')
        seen.add(key.casefold())
        result[key] = val
    return result


def _step_number(plan, ref):
    if isinstance(ref, str):
        ref = ref.strip()
        if ref.isdecimal() and len(ref) <= 4:
            ref = int(ref)
        else:
            candidates = [i for i, step in enumerate(plan['steps'], 1)
                          if ref and step.get('alias', '').casefold() == ref.casefold()]
            if len(candidates) != 1:
                raise TaskError('ambiguous_step_alias' if candidates else 'unknown_step_alias')
            return candidates[0]
    if type(ref) is not int or not 1 <= ref <= len(plan['steps']):
        raise TaskError('invalid_step_number')
    return ref


def get_step(plan, ref):
    _validate(plan)
    return plan['steps'][_step_number(plan, ref) - 1]


def check_binding(plan, exe, window):
    _validate(plan)
    if not isinstance(exe, str) or exe.casefold() != plan['exe'].casefold() or window != plan['window']:
        raise TaskError('recorded_window_mismatch')


def _parameter(step):
    instruction = step.get('instruction', {})
    return instruction.get('parameter') if instruction else step.get('parameter')


def replay(plan, exe, window, overrides, invoke, *, only=None, resolution=None):
    """Invoke each requested step at most once. Unverified delivery stops the sequence."""
    check_binding(plan, exe, window)
    if any(s['action'] == 'intent' for s in plan['steps']):
        from intent_replay import replay_intents
        return replay_intents(plan, exe, window, overrides, invoke, only=only, resolution=resolution)
    if only is not None:
        only = _step_number(plan, only)
    sequence = [(i, s) for i, s in enumerate(plan['steps'], 1) if only is None or only == i]
    values = {}
    for key, value in overrides.items():
        if not _text(key, 1000):
            raise TaskError('unknown_parameter')
        targets = [(i, s) for i, s in sequence if key.casefold() in
                   ((_parameter(s) or '').casefold(), s.get('alias', '').casefold())]
        if not targets:
            raise TaskError('unknown_parameter')
        if len(targets) > 1 and not all(s.get('instruction', {}).get('mode') == 'required'
                                        and _parameter(s).casefold() == key.casefold() for _, s in targets):
            raise TaskError('ambiguous_parameter')
        for number, s in targets:
            if s.get('instruction', {}).get('mode') == 'fixed':
                raise TaskError('fixed_value_override')
            if s['action'] != 'select':
                raise TaskError('unknown_parameter')
            if number in values or not _text(value, 10000):
                raise TaskError('invalid_override')
            values[number] = value
    results = []
    report = {'status': 'verified', 'steps': results, 'input_dispatched': False}
    for number, s in sequence:
        if s['action'] == 'manual':
            return dict(report, status='needs_review', stopped_at=number, diagnostic=s['reason'])
    missing = [s['instruction']['parameter'] for number, s in sequence if number not in values
               and s.get('instruction', {}).get('mode') == 'required']
    if missing:
        return dict(report, status='needs_input', diagnostic='required_parameter_missing',
                    missing_parameters=list(dict.fromkeys(missing)))
    for number, s in sequence:
        step = s.copy()
        if step['action'] == 'select':
            if number in values:
                step['value'] = values[number]
            elif 'instruction' in step:
                step['value'] = step['instruction']['value']
        try:
            receipt = invoke(step)
        except Exception as error:
            dispatched = getattr(error, 'input_dispatched', None)
            receipt = {'status': 'blocked' if dispatched is False else 'uncertain',
                       'input_dispatched': dispatched, 'diagnostic': _reason(getattr(error, 'code', 'dispatch_interrupted'))}
        if (not isinstance(receipt, dict) or 'input_dispatched' not in receipt
                or receipt.get('status') not in ('verified', 'delivered_unverified', 'mismatch', 'uncertain', 'blocked')
                or (receipt.get('status') == 'verified' and (receipt.get('effect_verification') != 'matched'
                                                            or type(receipt['input_dispatched']) is not bool))):
            receipt = {'status': 'uncertain', 'input_dispatched': None, 'diagnostic': 'invalid_action_receipt'}
        results.append({'number': number, 'receipt': receipt})
        sent = receipt['input_dispatched']
        if sent is None or report['input_dispatched'] is None:
            report['input_dispatched'] = None
        elif sent is True:
            report['input_dispatched'] = True
        if receipt['status'] != 'verified':
            return dict(report, status=receipt['status'], stopped_at=number, diagnostic=receipt.get('diagnostic'))
    return report
