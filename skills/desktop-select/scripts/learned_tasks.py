"""Store demonstrated UI identities locally and replay them through Desktop Select."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import json
import ntpath
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import tempfile

from targeting import _protected, _tree, SelectionError
import storage_paths


MAX_STEPS = 300
MAX_FILE_BYTES = 4 * 1024 * 1024
INVOKE = {'button', 'hyperlink', 'menuitem', 'splitbutton'}
CHOOSE = {'listitem', 'tab', 'treeitem'}
EXPAND = {'menuitem', 'combobox', 'treeitem', 'splitbutton'}
VALUE_ACTIONS = {'select': 'combobox', 'fill': 'textfield'}
RECOVERABLE_CAPTURE_REASONS = {
    'capture_target_unavailable', 'stable_locator_unavailable', 'unsupported_recorded_action',
    'unsupported_demonstration', 'combo_value_unobserved', 'combo_choice_unproven',
    'combo_option_click_required', 'value_readback_unavailable', 'value_before_transition_unproven',
    'combo_option_owner_required', 'text_value_unobserved', 'row_capture_requires_review',
}
LEGACY_INSTRUCTION_HINT = '화면의 대상 이름과 실행할 동작을 입력하세요.'


class TaskError(ValueError):
    def __init__(self, code, *, step_number=None):
        super().__init__(code)
        self.code = code
        self.step_number = step_number


@contextmanager
def _at_step(number):
    try:
        yield
    except (ValueError, RuntimeError) as error:
        if not hasattr(error, 'code'):
            raise
        raise TaskError(error.code, step_number=number) from error


def _text(value, limit=1024):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= limit
            and not any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def valid_value(value, role):
    """Keep entered strings literal, allowing an explicit clear only for text fields."""
    return (role in VALUE_ACTIONS.values() and (value == '' and role == 'textfield' or _text(value, 10000))
            and len(value.encode('utf-8')) <= 10000)


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
    if 'window_context' in raw:
        from window_context import validate_context, ContextError
        try:
            fields['window_context'] = dict(validate_context(raw['window_context']))
        except ContextError as error:
            raise TaskError(error.code) from error
    return fields


def _selector(raw):
    if not isinstance(raw, dict) or not _text(raw.get('role'), 80):
        return None
    for key in ('automation_id', 'name'):
        if _text(raw.get(key)) and not (key == 'automation_id' and raw[key].isdecimal()):
            return {'role': raw['role'], key: raw[key]}
    return None


def _point_target(raw):
    locator = raw.get('target', {}).get('locator') if isinstance(raw.get('target'), dict) else None
    role = locator.get('role') if isinstance(locator, dict) else None
    reason = {'combobox': 'combo_choice_unproven', 'textfield': 'text_value_unobserved'}.get(role) if isinstance(role, str) else None
    return (reason is not None and raw.get('action') == 'teach-target' and 'value' not in raw
            and raw.get('recognition') == {'target': 'identified', 'action': 'requires_instruction',
                                           'result': 'manual', 'reason': reason})


def is_recoverable(step):
    return _recoverable_capture(step, RECOVERABLE_CAPTURE_REASONS)


def is_double_click_step(step):
    if not isinstance(step, dict):
        return False
    if step.get('action') == 'visual-click':
        spec = step.get('visual')
        return isinstance(spec, dict) and spec.get('gesture') == 'double-click'
    if step.get('action') != 'manual':
        return False
    return _recoverable_capture(step, {'double_click_needs_review'})


def query_before_double_click(button, following):
    if (not is_double_click_step(following) or button.get('action') not in ('open', 'invoke')
            or button.get('window_context') != following.get('window_context') or 'popup' in button):
        return False
    selector = button.get('selector', button)
    caption = button.get('command_name') or selector.get('name')
    if not caption and button.get('match') == 'name':
        caption = button.get('target')
    return (selector.get('role') == 'button' and isinstance(caption, str) and
            caption.strip().casefold() in {'view', '조회', '검색', 'search', 'query', 'find', 'refresh', '새로고침'})


def _recoverable_capture(step, reasons):
    """An absent capture identity may be replaced by a fresh, named lookup.

    This does not authorize recovery from a protected control, uncertain input,
    a popup binding, or a physical context-menu gesture.
    """
    if not isinstance(step, dict) or step.get('action') in ('right-click', 'result-menu', 'row-action'):
        return False
    if (step.get('role') == 'password' or any(step.get(key) is True for key in
            ('secure', 'protected', 'is_protected', 'is_password', 'password'))):
        return False
    source = step.get('target')
    if isinstance(source, dict):
        if 'window' in source or (step.get('action') == 'teach-target' and not _point_target(step)):
            return False
        locator = source.get('locator') or {}
        candidates = [step, source, locator]
        if isinstance(locator, dict) and isinstance(locator.get('ancestors'), list):
            candidates.extend(locator['ancestors'])
        for candidate in candidates:
            if isinstance(candidate, dict) and (candidate.get('role') == 'password' or any(
                    candidate.get(key) is True for key in ('secure', 'protected', 'is_protected', 'is_password', 'password'))):
                return False
        converted = _convert(step, allow_point=True)
    else:
        converted = step
    return (converted.get('action') == 'manual'
            and converted.get('reason') in reasons)


def _conditional_sentence(sentence):
    """Leave a branched request intact for the host instead of extracting its last verb."""
    return re.search(r'(?:있으|없으|열리|보이|나오|나타나|뜨|표시되|발견되)면(?:\s|[,，])'
                     r'|(?:있든|없든|유무와\s*(?:관계|상관)없이)|\b(?:if|unless|otherwise|whether)\b',
                     sentence, re.I) is not None


def double_click_instruction(sentence):
    """An affirmative gesture, optionally followed by the expected open effect."""
    if not isinstance(sentence, str) or _conditional_sentence(sentence):
        return False
    if re.search(r'하지\s*마|말고|대신|않|금지|그리고|(?:으|라|다)면|경우에|일\s*때|삭제|닫[고기는아]|종료|취소|'
                 r'(?:한|1)\s*번(?:만)?\s*(?:누르|눌러|클릭)|[,;?\n]|'
                 r'\b(?:not|never|instead|then|and|or|single[ -]?click|delete|remove|close|cancel)\b',
                 sentence, re.I):
        return False
    gesture = r'(?:(?:더블\s*클릭|두\s*번\s*클릭)(?:\s*(?:해서|하여|으로|하기|해))?|두\s*번\s*(?:눌러서|눌러|누르기))'
    # Only the expected opening effect can follow the gesture. Other instructions stay editable.
    opening = (r'(?:\s*[\w ()-]*?(?:팝업|창|화면|대화\s*상자|페이지|상세\s*(?:보기|정보))(?:을|를)?'
               r'\s*(?:띄워|열어|열기|표시해))?')
    return bool(re.fullmatch(
        r'[^,;!?\n]*?' + gesture + opening + r'\s*(?:줘|주세요)?[.!]?'
        r'|double[ -]?click\s+[^,;!?]+[.!]?', sentence.strip(), re.I))


def click_request(sentence):
    """Extract the visible name from one affirmative click or launch instruction."""
    if not isinstance(sentence, str) or _conditional_sentence(sentence):
        return None
    text = sentence.strip().rstrip('.!。').rstrip()
    # A parenthesized label may follow either the target or the whole sentence.
    alias = re.search(r'\s*[（(]([^()（）]{1,120})[)）]$', text)
    if alias:
        text = text[:alias.start()].rstrip()
    verb = (r'(?:눌러(?:서?\s*(?:시행|실행|시작|열어|열기))?|누르기|클릭(?:해서\s*(?:시행|실행|시작|열어|열기))?'
            r'|선택해서\s*(?:시행|실행|시작)|실행|시행|열어|열기)')
    match = re.fullmatch(r'(.+?)\s*(?:을|를)?\s*' + verb + r'(?:해)?\s*(?:줘|주세요)?', text)
    if match:
        target = match[1].strip()
    else:
        english = re.fullmatch(r'(?:click|tap|open|launch)\s+(.+)', text, re.I)
        if not english:
            return None
        target = english[1].strip()
    embedded = re.search(r'\s*[（(]([^()（）]{1,120})[)）]$', target)
    if embedded and not alias:
        alias = embedded
        target = target[:embedded.start()].rstrip()
    # Compound, conditional, negative and non-left gestures require the host.
    compound = (r'[,;!?\n]|누르[고지]|눌러서|누른\s*(?:뒤|후)|하고|(?:한|선택)\s*(?:뒤|후)|'
                r'말고|대신|아니라|빼고|제외|다음|모두|둘\s*다|연속|두\s*번|더블|우클릭|오른쪽\s*버튼|'
                r'하지\s*마|않|(?:하면|되면|라면|있으면|없으면)\s|'
                r'\b(?:and|or|then|unless|except|not|double|right-click)\b')
    if not target or re.search(compound, target, re.I):
        return None
    # Location prose identifies the window that owns the demonstrated click.
    # The UIA lookup needs the caption inside this already-bound scope.
    target = re.sub(r'^.+?(?:화면|창)에서\s+', '', target).strip()
    role = 'menuitem' if target.endswith('메뉴') else 'button'
    name = alias[1].strip() if alias else re.sub(r'\s*(?:버튼|메뉴|아이콘|항목)$', '', target).strip()
    if not _text(name, 200) or name in {'이', '그', '해당', '여기', '거기'}:
        return None
    return name, role


def click_menu_path(sentence):
    click = click_request(sentence)
    if click:
        parts = [part.strip() for part in re.split('[>→]', click[0])]
        if 2 <= len(parts) <= 8 and all(_text(part, 200) for part in parts):
            return parts


def _quoted_literal(text):
    match = re.fullmatch(r"""(?:'([^']*)'|"([^"]*)"|‘([^’]*)’|“([^”]*)”)""", text)
    return next(v for v in match.groups() if v is not None) if match else None


def _runtime_value_request(sentence):
    """Return the preceding assignment, or '' when the request refers to the recorded field."""
    if '물어' not in sentence:
        return None
    text = sentence.strip()
    match = re.search(
        r'(?P<sep>^|[.,，。(（])\s*(?:(?:이건|이\s*값은)\s*)?'
        r'(?:매번\s*)?실행\s*(?:전(?:에)?|시(?:에)?|할\s*때(?:마다)?)\s*'
        r'(?:사용자(?:에게|한테)\s*)?(?:(?:어떤|무슨)\s*)?(?:값(?:을|으로)?\s*)?'
        r'(?:(?:넣을|입력할|선택할|설정할)\s*지\s*)?'
        r'(?:물어봐|물어보(?:기|세요))\s*(?:줘|주세요)?[.!?]?\s*(?P<close>[)）]?)[.!。]?\Z', text)
    if not match or match['close'] != {'(': ')', '（': '）'}.get(match['sep'], ''):
        return None
    return text[:match.start()].strip()


def _field_request(sentence):
    """Recognize a single target-first assignment, preserving the literal's spelling."""
    if not isinstance(sentence, str) or _conditional_sentence(sentence):
        return None
    match = re.fullmatch(r'(.{1,200}?)에\s*(.+?)\s*(?:을|를)?\s*'
                         r'(?:선택|설정|입력|넣어)(?:해)?\s*(?:줘|주세요)?[.!。]?', sentence.strip())
    if not match:
        return None
    subject, literal = match[1].strip(), match[2].strip()
    if not subject or re.search(r'[,;!?\n]|하고|말고|대신|\s후\s|\s뒤\s', subject):
        return None
    variable = re.fullmatch(r'\{([^{}]+)\}', literal)
    if variable or re.fullmatch(r'(?:실행\s*(?:할\s*)?때\s*)?입력\s*받(?:은|는)\s*(?:변수|값)', literal):
        return subject, 'required', variable[1] if variable else None
    quoted = _quoted_literal(literal)
    if quoted is not None:
        return subject, 'fixed', quoted
    if re.fullmatch(r'[\w./+()-]+', literal) and literal not in ('값', '변수'):
        return subject, 'fixed', literal
    return None


def instruction_subject(sentence):
    """Return the field named in a supported sentence for an explicit alias link."""
    if not isinstance(sentence, str):
        return None
    assignment = _runtime_value_request(sentence)
    if assignment is not None:
        sentence = assignment
    field = _field_request(sentence)
    if field:
        return re.sub(r'\s*(?:조건|콤보박스|콤보|항목|필드|입력란)$', '', field[0]).strip()
    patterns = (r'입력\s*받은\s*(?:\{[^{}]+\}\s*)?변수(?:를|을)\s*(.+?)에\s*',
                r'\{[^{}]+\}(?:을|를)\s*(.+?)에\s*',
                r'(.+?)(?:을|를)\s*')
    for pattern in patterns:
        match = re.match(pattern, sentence.strip())
        if match:
            subject = match[1].strip()
            if ''.join(subject.split()) in {'이항목', '이필드', '이콤보', '이메뉴', '이버튼', '이입력란'}:
                return None
            return re.sub(r'\s*(?:조건|콤보박스|콤보|항목|필드|버튼|메뉴|입력란)$', '', subject).strip()
    return None


def _convert(raw, *, allow_point=False):
    if not isinstance(raw, dict):
        return _manual('capture_target_unavailable')
    source = raw.get('target')
    locator = source.get('locator') if isinstance(source, dict) else None
    if any(isinstance(candidate, dict) and (candidate.get('role') == 'password' or any(
            candidate.get(key) is True for key in ('secure', 'protected', 'is_protected', 'is_password', 'password')))
            for candidate in [raw, source, locator, *((locator or {}).get('ancestors', []) if isinstance(locator, dict)
                               and isinstance(locator.get('ancestors', []), list) else [])]):
        return _manual('target_protected')
    if isinstance(source, dict) and 'window' in source:
        return _manual('popup_needs_live_binding')
    special = _capture_command(raw)
    if special is not None:
        return special
    if raw.get('action') == 'right-click':
        return _manual('right_click_requires_menu_teaching')
    recognition = raw.get('recognition')
    recognition = recognition if isinstance(recognition, dict) else {}
    point = allow_point and _point_target(raw)
    if (not isinstance(source, dict) or source.get('kind') != 'element' or raw.get('action') == 'manual'
            or (not point and (recognition.get('action') != 'supported' or recognition.get('target') != 'identified'))):
        return _manual(recognition.get('reason', 'unsupported_demonstration'))
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
    if role == 'textfield' and any(a['role'] == 'combobox' for a in ancestors):
        return _manual('text_input_owned_by_combo')
    if point and role in VALUE_ACTIONS.values():
        step.update(action='select' if role == 'combobox' else 'fill',
                    parameter=step.get('label') or step['target'], recorded_effect='unobserved')
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
    elif action == 'set-value' and role == 'textfield':
        if recognition.get('result') == 'mismatch':
            return _manual('text_value_mismatch')
        value = raw.get('value')
        if not valid_value(value, role) or recognition.get('result') != 'checkable':
            return _manual('text_value_unobserved')
        label = step.get('label') or step['target']
        parameter = label if _text(label, 200) and '=' not in label else 'value'
        step.update(action='fill', value=value, parameter=parameter, recorded_effect='matched')
    elif ((action == 'click' and role in INVOKE) or (action == 'select' and role in CHOOSE)
          or (action == 'expand' and role in EXPAND)):
        step.update(action='open', expand=action == 'expand')
    else:
        return _manual('unsupported_demonstration')
    return step


CAPTURE_COMMANDS = frozenset({'field-input', 'submit', 'choose-item'})


def _capture_command(raw):
    """Retain a safely witnessed field, submit or non-combo item as a literal task step."""
    if raw.get('action') in CAPTURE_COMMANDS and 'selector' in raw:
        result = dict(raw)
        _validate_capture_command(result)
        return result
    source = raw.get('target', {})
    if not isinstance(source, dict):
        return None
    locator = source.get('locator', {}) if isinstance(source, dict) else {}
    if source.get('kind') != 'element' or not isinstance(locator, dict):
        return None
    chain = locator.get('ancestors', [])
    if not isinstance(chain, list) or any(not isinstance(node, dict) for node in chain):
        return None
    action, role = raw.get('action'), locator.get('role')
    if any(isinstance(node, dict) and (node.get('role') == 'password' or any(
            node.get(key) is True for key in ('secure', 'protected', 'is_protected', 'is_password', 'password')))
           for node in [raw, source, locator, *chain]):
        return None
    if not ((action == 'set-value' and role == 'textfield' and 'anchor' in locator)
            or (action == 'submit' and role in ('textfield', 'combobox'))
            or (action == 'select' and role == 'listitem'
                and not any(a.get('role') == 'combobox' for a in chain))):
        return None
    from copy import deepcopy
    if 'anchor' in locator:
        selector = deepcopy(locator)
    else:
        selector = _selector(locator)
        if not selector:
            return None
        if locator.get('ancestors'):
            selector['ancestors'] = deepcopy(locator['ancestors'])
    recognition = raw.get('recognition', {})
    if recognition.get('target') != 'identified' or recognition.get('action') != 'supported':
        return None
    result = dict(action='field-input' if action == 'set-value' else 'choose-item' if action == 'select' else 'submit',
                  selector=selector, label=raw.get('field_label') or raw.get('label') or
                  locator.get('name') or ('입력란' if role == 'textfield' else '항목'))
    if action == 'set-value':
        if recognition.get('result') != 'checkable' or not valid_value(raw.get('value'), role):
            return None
        result['value'] = raw['value']
    _validate_capture_command(result)
    return result


def _validate_capture_command(step):
    from python_workflow import validate_selector
    selector = step.get('selector')
    validate_selector(selector)
    allowed = {'action', 'selector', 'label', 'instruction', 'instruction_draft', 'alias', 'description', 'window_context'}
    if not _text(step.get('label'), 200):
        raise TaskError('invalid_task_target')
    if step['action'] == 'field-input':
        if selector['role'] != 'textfield' or ('value' in step) == ('parameter' in step):
            raise TaskError('invalid_task_selection')
        allowed.update(('value', 'parameter'))
        if 'value' in step and not valid_value(step['value'], 'textfield'):
            raise TaskError('invalid_task_selection')
        if 'parameter' in step and (not _text(step['parameter'], 100) or '=' in step['parameter']):
            raise TaskError('invalid_parameter')
    elif step['action'] == 'submit':
        if selector['role'] not in ('textfield', 'combobox'):
            raise TaskError('invalid_submit')
    elif step['action'] == 'choose-item':
        if selector['role'] not in CHOOSE:
            raise TaskError('invalid_item_selection')
    else:
        raise TaskError('invalid_task_action')
    if set(step) - allowed:
        raise TaskError('unexpected_task_step_data')
    if 'instruction_draft' in step:
        draft = step['instruction_draft']
        if ('instruction' in step or not _text(draft, 1000) or len(draft.encode('utf8')) > 1000
                or draft != draft.strip()):
            raise TaskError('invalid_step_instruction')
    if 'instruction' in step:
        if _command_instruction(step, step['instruction'].get('text'),
                                mode=step['instruction'].get('mode'),
                                argument=step['instruction'].get('parameter', step['instruction'].get('value'))) != step['instruction']:
            raise TaskError('invalid_step_instruction')
        if step['action'] == 'field-input':
            key = 'parameter' if step['instruction']['mode'] == 'required' else 'value'
            if step.get(key) != step['instruction'].get(key):
                raise TaskError('instruction_binding_conflict')


def _has_saved_binding(step, sentence, mode, argument):
    previous = step.get('instruction')
    return (mode in ('fixed', 'required') and isinstance(previous, dict)
            and previous.get('mode') == mode and previous.get('text') == sentence
            and previous.get('value' if mode == 'fixed' else 'parameter') == argument)


def _command_instruction(step, sentence, mode=None, argument=None):
    if not _text(sentence, 1000):
        raise TaskError('invalid_step_instruction')
    if step['action'] == 'field-input':
        fake = dict(action='fill', role='textfield', target=step['label'], label=step['label'],
                    **_annotations(step))
        parsed = _parse_sentence(fake, sentence, saved_binding=_has_saved_binding(step, sentence, mode, argument))
        if mode is None:
            if not parsed:
                return dict(mode='draft', text=sentence)
            mode, argument = parsed
        elif parsed and not _instruction_agrees(parsed, sentence, mode, argument):
            raise TaskError('instruction_binding_conflict')
        return _binding(fake, sentence, mode, argument)
    if mode not in (None, 'recorded') or argument not in (None, ''):
        raise TaskError('instruction_action_mismatch')
    if step['action'] == 'submit':
        affirmative = re.fullmatch(
            r'(?:.+?(?:에서|에)\s*)?(?:Enter|Return|엔터)(?:\s*키)?(?:을|를)?\s*'
            r'(?:눌러(?:서)?|누르기|입력|적용)(?:\s*(?:적용|조회))?(?:해)?\s*(?:줘|주세요)?[.!。]?'
            r'|(?:press|send|submit)\s+(?:Enter|Return)[.!]?', sentence, re.I)
        if not affirmative or _conditional_sentence(sentence):
            return dict(mode='draft', text=sentence)
        subject = re.match(r'(.+?)(?:에서|에)\s*(?:Enter|Return|엔터)', sentence, re.I)
        if subject and ''.join(subject[1].split()) not in ('현재입력란', '해당입력란'):
            try:
                _instruction_subject(dict(label=step['label'], target=step['selector'].get('name', '')), subject[1])
            except TaskError:
                return dict(mode='draft', text=sentence)
    else:
        click = click_request(sentence)
        selection = re.fullmatch(r'(.+?)\s*(?:을|를)?\s*선택(?:해)?\s*(?:줘|주세요)?[.!。]?', sentence)
        subject = click[0] if click else selection[1] if selection else None
        try:
            if subject is None:
                raise TaskError('instruction_target_unavailable')
            _instruction_subject(dict(label=step['label'], target=step['selector'].get('name', '')), subject)
        except TaskError:
            return dict(mode='draft', text=sentence)
    return dict(mode='recorded', text=sentence)


def _instruction_subject(step, subject):
    candidates = [step.get(k, '') for k in ('label', 'target', 'alias', 'command_name')]
    norm = lambda s: ''.join(s.split()).casefold()
    text = norm(subject)
    without_kind = re.sub(r'(?:조건|콤보박스|콤보|목록항목|항목|필드|버튼|메뉴|입력란|형식|옵션)$', '', text)
    if text in {'이항목', '이필드', '이콤보', '이메뉴', '이버튼', '이입력란'}:
        return next(c for c in candidates if c)
    for c in candidates:
        if c and norm(c) in (text, without_kind):
            return c
    raise TaskError('instruction_target_mismatch')


def _parse_sentence(step, sentence, *, saved_binding=False):
    """Only parse one bounded, field-specific instruction. No shell or model evaluation."""
    verb = r'(?:선택|설정|입력|변경|해)(?:해)?\s*(?:줘|주세요)?'
    tail = r'(?:[.!。])?'
    # Older explicit bindings could resolve a display name or free-form note
    # differently. Apply the new inference when interpreting a fresh request.
    example = _runtime_value_request(sentence) if not saved_binding else None
    if example is not None and step.get('action') in VALUE_ACTIONS:
        if not example:
            return 'required', _instruction_subject(step, '이 항목')
        if (_runtime_value_request(example) is not None or _conditional_sentence(example)
                or re.search(r'(?:입력|설정|선택|변경|실행|조회|클릭)(?:하고|해서|한\s*(?:뒤|후)|해(?:줘|주세요)\s*[.,，。])'
                             r'|누르고|말고|대신', example)):
            return None
        parsed = _parse_sentence(step, example)
        if parsed and parsed[0] == 'required':
            return parsed
        if parsed and parsed[0] == 'fixed' and parsed[1]:
            return 'required', _instruction_subject(step, instruction_subject(example) or '이 항목')
        return None
    field = _field_request(sentence)
    if field and step.get('action') in VALUE_ACTIONS:
        subject = _instruction_subject(step, field[0])
        return field[1], subject if field[2] is None else field[2]
    required = re.fullmatch(r'입력\s*받은\s*(?:\{([^{}]+)\}\s*)?변수(?:를|을)\s*(.+?)에\s*' + verb + tail, sentence)
    assign = re.fullmatch(r'(.+?)(?:을|를)\s*(.+?)(?:으로|로)\s*' + verb + tail, sentence)
    into = re.fullmatch(r'\{([^{}]+)\}(?:을|를)\s*(.+?)에\s*' + verb + tail, sentence)
    clear = re.fullmatch(r'(.+?)(?:을|를)\s*비워\s*(?:줘|주세요)?' + tail, sentence)
    if clear and step.get('action') == 'fill':
        _instruction_subject(step, clear[1])
        return 'fixed', ''
    if required or into:
        match = required or into
        field = _instruction_subject(step, match[2])
        return 'required', match[1] or field
    if assign:
        _instruction_subject(step, assign[1])
        val = assign[2].strip()
        variable = re.fullmatch(r'\{([^{}]+)\}', val)
        if variable:
            return 'required', variable[1]
        if step.get('action') == 'fill' and val.startswith('"'):
            try:
                literal = json.loads(val)
            except (ValueError, RecursionError):
                return None
            return ('fixed', literal) if valid_value(literal, 'textfield') else None
        if not saved_binding and step.get('action') == 'select':
            literal = _quoted_literal(val)
            if literal is not None and valid_value(literal, 'combobox'):
                return 'fixed', literal
        if not re.fullmatch(r'[\w +./()-]+', val):
            return None
        # Descriptions such as "이번 시험에 맞는 상태" are not literal option
        # values. Keep the user's prose for the host to interpret once.
        if re.search(r'(?:쓰던|사용한|선택한|맞는|같은|원하는|필요한|적절한|알맞은)\s*(?:값|상태|조건)', val):
            return None
        return 'fixed', val
    command = re.fullmatch(r'(.+?)(?:을|를)\s*(누르기|눌러|클릭|펼치기|펼쳐|선택|실행)(?:해)?\s*(?:줘|주세요)?' + tail, sentence)
    if command:
        _instruction_subject(step, command[1])
        if step.get('action') != 'open' or (command[2] in ('펼치기', '펼쳐') and not step.get('expand')):
            raise TaskError('instruction_action_mismatch')
        return 'recorded', None
    click = click_request(sentence)
    if click and step.get('action') == 'open' and not step.get('expand'):
        _instruction_subject(step, click[0])
        return 'recorded', None
    return None


def _binding(step, text, mode, argument):
    if mode == 'recorded':
        if step.get('action') != 'open' or argument not in (None, ''):
            raise TaskError('instruction_action_mismatch')
        return {'mode': mode, 'text': text}
    if (mode not in ('required', 'fixed') or step.get('action') not in VALUE_ACTIONS
            or step.get('role') != VALUE_ACTIONS[step['action']]):
        raise TaskError('instruction_action_mismatch')
    if not (_text(argument, 200) if mode == 'required' else valid_value(argument, step['role'])):
        raise TaskError('invalid_instruction_argument')
    if mode == 'required' and ('=' in argument or argument != argument.strip()):
        raise TaskError('invalid_instruction_argument')
    return {'mode': mode, 'text': text, 'parameter' if mode == 'required' else 'value': argument}


def _instruction_agrees(parsed, sentence, mode, argument):
    actual_mode, actual_arg = parsed
    if mode != actual_mode:
        return False
    if argument == actual_arg:
        return True
    if mode != 'required' or not isinstance(argument, str):
        return False
    # A plain "input variable" sentence names only the field. Its field label
    # supplies an initial parameter name, which the explicit editor can rename.
    # Braced names are explicit instructions and must still match.
    example = _runtime_value_request(sentence)
    if example is not None and '{' not in example:
        return True
    field = _field_request(example or sentence)
    return (argument.casefold() == actual_arg.casefold() or (field is not None and field[1:] == ('required', None))
            or bool(re.match(r'^입력\s*받은\s*변수(?:를|을)\s*', sentence)))


def _semantic_instruction(raw_step, sentence, mode, argument, target, role, within):
    """Bind a new instruction to a live query, never to the missed click's coordinates."""
    if not is_recoverable(raw_step):
        raise TaskError('instruction_target_unavailable')
    explicit = any(value is not None for value in (mode, argument, target, role, within))
    subject = instruction_subject(sentence)
    click = click_request(sentence)
    if click:
        subject = click[0]
    query = target if target is not None else subject
    if query is None and _text(raw_step.get('alias')):
        query = raw_step['alias']
    if query is None:
        if explicit:
            raise TaskError('instruction_target_unavailable')
        return {'mode': 'draft', 'text': sentence}
    if not _text(query) or query != query.strip():
        raise TaskError('invalid_task_target')
    if within is not None and (not _text(within) or within != within.strip()):
        raise TaskError('invalid_task_scope')
    # Classification only identifies the small action grammar; the existing
    # parser below still checks the subject, operation, and bound argument.
    command = not _field_request(sentence) and re.fullmatch(r'(.+?)(?:을|를)\s*(누르기|눌러|클릭|펼치기|펼쳐|선택|실행)'
                           r'(?:해)?\s*(?:줘|주세요)?(?:[.!。])?', sentence)
    action = 'open' if mode == 'recorded' or (mode is None and (command or click)) else 'select'
    inferred_role = click[1] if click else ('menuitem' if command and re.search(r'메뉴\s*$', command[1]) else 'button')
    inferred_input = 'textfield' if re.search(r'입력란\s*(?:에|을|를)', sentence) else 'combobox'
    role = role if role is not None else (inferred_input if action == 'select' else inferred_role)
    if action == 'select' and role == 'textfield':
        action = 'fill'
    if not isinstance(role, str) or role not in ({VALUE_ACTIONS[action]} if action in VALUE_ACTIONS else INVOKE):
        raise TaskError('instruction_action_mismatch')
    step = {'action': action, 'target': query, 'match': 'query', 'role': role}
    query_label = subject if target is not None and subject and ''.join(subject.split()).casefold() != ''.join(query.split()).casefold() else None
    if query_label is not None:
        if not _text(query_label, 200):
            raise TaskError('invalid_task_target')
        step['label'] = query_label
    if within is not None:
        step['within'] = within
    if action == 'open':
        # Semantic recovery invokes a newly found command. Expanding a menu
        # still needs its recorded capability and is not inferred here.
        step['expand'] = False
    try:
        parsed = _parse_sentence(step, sentence)
    except TaskError as error:
        if not explicit and error.code in ('instruction_target_mismatch', 'instruction_action_mismatch'):
            return {'mode': 'draft', 'text': sentence}
        raise
    if mode is None:
        if not parsed or argument is not None:
            if explicit:
                raise TaskError('instruction_requires_structure')
            return {'mode': 'draft', 'text': sentence}
        mode, argument = parsed
    elif parsed and not _instruction_agrees(parsed, sentence, mode, argument):
        raise TaskError('instruction_binding_conflict')
    spec = _binding(step, sentence, mode, argument)
    spec.update(query=query, role=role)
    if query_label is not None:
        spec['query_label'] = query_label
    if within is not None:
        spec['within'] = within
    return spec


def compile_instruction(raw_step, sentence, *, mode=None, argument=None, target=None, role=None, within=None):
    if not isinstance(raw_step, dict) or not isinstance(sentence, str):
        raise TaskError('invalid_step_instruction')
    sentence = sentence.strip()
    if sentence and (not _text(sentence, 1000) or len(sentence.encode('utf-8')) > 1000):
        raise TaskError('invalid_step_instruction')
    if not sentence and all(value is None for value in (mode, argument, target, role, within)):
        return None
    special = _capture_command(raw_step)
    if special is not None:
        if any(value is not None for value in (target, role, within)):
            raise TaskError('instruction_target_mismatch')
        return _command_instruction(special, sentence, mode, argument)
    if _conditional_sentence(sentence) and all(value is None for value in (mode, argument, target, role, within)):
        return {'mode': 'draft', 'text': sentence}
    if is_double_click_step(raw_step):
        if any(value is not None for value in (mode, argument, target, role, within)):
            raise TaskError('double_click_target_requires_grounding')
        if ((raw_step.get('action') == 'visual-click' or 'visual_hint' in raw_step)
                and (double_click_instruction(sentence) or
                     (raw_step.get('action') == 'visual-click' and sentence == raw_step.get('label')))):
            return {'mode': 'visual-click', 'text': sentence}
        return {'mode': 'draft', 'text': sentence}
    if ('visual_hint' in raw_step and 'gesture' not in raw_step['visual_hint']
            and is_recoverable(raw_step) and click_request(sentence)
            and all(value is None for value in (mode, argument, target, role, within))):
        return {'mode': 'visual-click', 'text': sentence}
    if raw_step.get('action') == 'visual-click':
        if all(value is None for value in (mode, argument, target, role, within)):
            if sentence == raw_step.get('label') or click_request(sentence):
                return {'mode': 'visual-click', 'text': sentence}
        raw_step = {**_manual('capture_target_unavailable'), **_annotations(raw_step)}
    if raw_step.get('action') in ('row-action', 'row-launch'):
        # A taught gesture is the default. An explicit edit supplies a new
        # intent rather than authorizing the old gesture under another label.
        raw_step = {**_manual('row_capture_requires_review'), **_annotations(raw_step)}
    if is_recoverable(raw_step) and all(value is None for value in (mode, argument, target, role, within)):
        from row_intent import parse
        ordinal = parse(sentence)
        if ordinal is not None:
            return {'mode': 'row-launch', 'ordinal': ordinal, 'text': sentence}
    if isinstance(raw_step.get('target'), dict):
        step = _convert(raw_step, allow_point=True)
        step.update(_annotations(raw_step))
    else:
        step = raw_step
    if step.get('action') not in (*VALUE_ACTIONS, 'open'):
        return _semantic_instruction(raw_step, sentence, mode, argument, target, role, within)
    if any(value is not None for value in (target, role, within)):
        if ((target is not None and target != step.get('target'))
                or (role is not None and role != step.get('role'))
                or (within is not None and within != step.get('within'))):
            raise TaskError('instruction_target_mismatch')
    # Older saved instructions could bind a quoted display name to a different
    # provider code or runtime parameter. Keep that explicit binding on load.
    parsed = _parse_sentence(step, sentence, saved_binding=_has_saved_binding(step, sentence, mode, argument))
    if mode is None:
        if not parsed or argument is not None:
            if argument is not None:
                raise TaskError('instruction_requires_structure')
            return {'mode': 'draft', 'text': sentence}
        mode, argument = parsed
    elif parsed and not _instruction_agrees(parsed, sentence, mode, argument):
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


def instructed_step(raw_step, spec):
    """Turn a provisional instruction into the same source step used by replay.

    Query metadata exists only while editing. The saved source step carries the
    query identity and its instruction contains only fixed/required/recorded.
    A free-form draft is retained as text on a manual step for host interpretation.
    """
    if not isinstance(raw_step, dict) or not isinstance(spec, dict):
        raise TaskError('invalid_step_instruction')
    special = _capture_command(raw_step)
    if special is not None:
        sentence = spec.get('text')
        if spec.get('mode') == 'draft':
            if set(spec) != {'mode', 'text'} or not _text(sentence, 1000):
                raise TaskError('invalid_step_instruction')
            special.pop('instruction', None)
            special['instruction_draft'] = sentence
        else:
            checked = _command_instruction(special, sentence, spec.get('mode'),
                                          spec.get('parameter', spec.get('value')))
            if checked != spec:
                raise TaskError('invalid_step_instruction')
            special['instruction'] = checked
            special.pop('instruction_draft', None)
            if special['action'] == 'field-input':
                special.pop('value', None)
                special.pop('parameter', None)
                key = 'parameter' if spec['mode'] == 'required' else 'value'
                special[key] = spec[key]
        special.update(_annotations(raw_step))
        _validate_capture_command(special)
        return special
    if (spec.get('mode') == 'visual-click' and 'visual_hint' in raw_step
            and (is_recoverable(raw_step) or is_double_click_step(raw_step))):
        if compile_instruction(raw_step, spec.get('text')) != spec:
            raise TaskError('invalid_step_instruction')
        marker = raw_step['visual_hint']
        if is_double_click_step(raw_step):
            marker = dict(marker, gesture='double-click')
        return _visual_step(dict(raw_step, visual=marker), spec['text'])
    if raw_step.get('action') == 'visual-click':
        if spec.get('mode') == 'visual-click':
            if compile_instruction(raw_step, spec.get('text')) != spec:
                raise TaskError('invalid_step_instruction')
            return _visual_step(raw_step, spec['text'])
        if spec.get('mode') == 'draft':
            marker = _visual_step(raw_step, raw_step.get('label'))['visual']
            reason = 'double_click_needs_review' if is_double_click_step(raw_step) else 'capture_target_unavailable'
            step = instructed_step({**_manual(reason), **_annotations(raw_step)}, spec)
            return dict(step, visual_hint=marker)
        raw_step = {**_manual('capture_target_unavailable'), **_annotations(raw_step)}
    row_hint = None
    if raw_step.get('action') in ('row-action', 'row-launch'):
        row_hint = raw_step.get('row') if raw_step['action'] == 'row-action' else raw_step.get('row_hint')
        raw_step = {**_manual('row_capture_requires_review'), **_annotations(raw_step)}
    if spec.get('mode') == 'row-launch':
        text = spec.get('text')
        if (not is_recoverable(raw_step) or type(spec.get('ordinal')) is not int
                or compile_instruction(raw_step, text) != spec):
            raise TaskError('invalid_step_instruction')
        step = {'action': 'row-launch', 'ordinal': spec['ordinal'], 'label': text, **_annotations(raw_step)}
        if row_hint is not None:
            from copy import deepcopy
            _validate_row({'action': 'row-action', 'label': 'row', 'row': row_hint})
            step['row_hint'] = deepcopy(row_hint)
        return step
    step = (_convert(raw_step, allow_point=True) if isinstance(raw_step.get('target'), dict)
            else dict(raw_step))
    step.update(_annotations(raw_step))
    if spec.get('mode') == 'draft':
        if (step.get('action') not in ('manual', *VALUE_ACTIONS, 'open')
                or set(spec) != {'mode', 'text'}
                or not _text(spec.get('text'), 1000) or len(spec['text'].encode('utf-8')) > 1000
                or spec['text'] != spec['text'].strip()):
            raise TaskError('invalid_step_instruction')
        if step.get('action') == 'manual':
            reason = step.get('reason')
            if reason in RECOVERABLE_CAPTURE_REASONS and not is_recoverable(raw_step):
                reason = 'action_needs_review'
            result = {**_manual(reason), **_annotations(raw_step), 'instruction_draft': spec['text']}
            if 'menu_anchor' in raw_step:
                result['menu_anchor'] = _menu_anchor(raw_step['menu_anchor'], reason)
            if 'menu_window_point' in raw_step:
                result['menu_window_point'] = _menu_window_point(raw_step['menu_window_point'], reason)
            if 'visual_hint' in raw_step:
                if not (is_recoverable(result) or is_double_click_step(result)):
                    raise TaskError('invalid_visual_step')
                result['visual_hint'] = raw_step['visual_hint']
            return result
        step.pop('instruction', None)
        step['instruction_draft'] = spec['text']
        if 'wait_after' in raw_step:
            step['wait_after'] = raw_step['wait_after']
        return step
    if 'query' in spec:
        compiled = compile_instruction(raw_step, spec.get('text'), mode=spec.get('mode'),
                                       argument=spec.get('parameter', spec.get('value')),
                                       target=spec.get('query'), role=spec.get('role'), within=spec.get('within'))
        if compiled != spec:
            raise TaskError('invalid_step_instruction')
        step = {'action': 'open' if spec['mode'] == 'recorded' else ('fill' if spec['role'] == 'textfield' else 'select'),
                'target': spec['query'], 'match': 'query', 'role': spec['role'], **_annotations(raw_step)}
        if 'within' in spec:
            step['within'] = spec['within']
        if 'query_label' in spec:
            step['label'] = spec['query_label']
        if step['action'] in VALUE_ACTIONS:
            step['recorded_effect'] = 'unobserved'
        else:
            step['expand'] = False
        spec = {key: value for key, value in spec.items() if key not in ('query', 'role', 'within', 'query_label')}
    step['instruction'] = _check_instruction(step, spec)
    for key in ('value', 'parameter', 'instruction_draft'):
        step.pop(key, None)
    if 'wait_after' in raw_step:
        step['wait_after'] = raw_step['wait_after']
    return step


def recorded_step(raw):
    if not isinstance(raw, dict):
        return _manual('capture_target_unavailable')
    if raw.get('action') == 'saved-step':
        step = dict(raw['saved'])
        if 'window_context' in raw:
            step['window_context'] = raw['window_context']
        _validate_step(step, set())
        return step
    spec = raw.get('instruction')
    if 'instruction_draft' in raw:
        if spec is not None:
            raise TaskError('invalid_step_instruction')
        spec = {'mode': 'draft', 'text': raw['instruction_draft']}
    if raw.get('action') == 'visual-click':
        if spec is not None:
            return instructed_step(raw, spec)
        return _visual_step(raw, raw.get('label'))
    if raw.get('action') == 'result-menu':
        if spec is not None:
            raise TaskError('invalid_step_instruction')
        step = {'action': 'result-menu', 'label': raw.get('label'), 'context': raw.get('context'), **_annotations(raw)}
        _validate_context(step)
        return step
    if raw.get('action') == 'row-action':
        step = {'action': 'row-action', 'label': raw.get('label'), 'row': raw.get('row'), **_annotations(raw)}
        _validate_row(step)
        if spec is not None:
            return instructed_step(raw, spec)
        return step
    if spec is not None:
        return instructed_step(raw, spec)
    special = _capture_command(raw)
    if special is not None:
        special.update(_annotations(raw))
        return special
    step = _convert(raw)
    step.update(_annotations(raw))
    if 'menu_anchor' in raw:
        step['menu_anchor'] = _menu_anchor(raw['menu_anchor'], step.get('reason'))
    if 'menu_window_point' in raw:
        step['menu_window_point'] = _menu_window_point(raw['menu_window_point'], step.get('reason'))
    if 'wait_after' in raw:
        step['wait_after'] = raw['wait_after']
    return step


def from_recording(name, exe, window, steps):
    _name(name)
    _window(exe, window)
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise TaskError('invalid_recorded_step_count')
    converted = []
    for number, raw in enumerate(steps, 1):
        with _at_step(number):
            converted.append(recorded_step(raw))
    counts = Counter(s['parameter'].casefold() for s in converted
                     if 'parameter' in s and s['action'] not in CAPTURE_COMMANDS)
    used = set()
    for number, s in enumerate(converted, 1):
        if 'parameter' in s and s['action'] not in CAPTURE_COMMANDS:
            parameter = s['parameter']
            if counts[parameter.casefold()] > 1:
                parameter += f' [{number}]'
            while parameter.casefold() in used:
                parameter += f' [{number}]'
            s['parameter'] = parameter
            used.add(parameter.casefold())
    return {'schema': 1, 'name': name, 'exe': exe, 'window': window, 'steps': converted}


def _visual_step(raw, label):
    from visual_target import validate_spec
    if not _text(label, 1000) or len(label.encode('utf8')) > 1000:
        raise TaskError('invalid_visual_label')
    validate_spec(raw.get('visual'))
    return dict(action='visual-click', label=label, visual=raw['visual'], **_annotations(raw))


def _validate_row(step):
    from row_vision import validate_spec
    from win_bridge import BridgeError
    if not _text(step.get('label'), 200) or set(step) - {'action', 'label', 'alias', 'description', 'row', 'window_context'}:
        raise TaskError('invalid_row_step')
    try:
        validate_spec(step.get('row'))
    except BridgeError as error:
        raise TaskError(error.code) from error


def _menu_window_point(point, reason):
    if (reason != 'right_click_requires_menu_teaching' or not isinstance(point, list) or len(point) != 2
            or not all(type(value) in (int, float) and 0 <= value < 1 for value in point)):
        raise TaskError('invalid_context_window_hint')
    return point


def _menu_anchor(anchor, reason):
    import context_menu
    from win_bridge import BridgeError
    if (reason != 'right_click_requires_menu_teaching' or not isinstance(anchor, dict)
            or set(anchor) != {'target', 'point'}):
        raise TaskError('invalid_context_anchor')
    try:
        context_menu._validate_surface(anchor['target'], anchor['point'])
    except BridgeError as error:
        raise TaskError(error.code) from error
    return anchor


def _validate_context(step):
    import result_menu
    if not _text(step.get('label'), 200) or set(step) - {'action', 'label', 'alias', 'description', 'context', 'window_context'}:
        raise TaskError('invalid_result_menu_step')
    result_menu.validate_spec(step.get('context'))


def summary(plan):
    """The local row patches are used by the player and stay out of CLI responses."""
    return dict(plan, steps=[{**{k: v for k, v in step.items() if k not in ('row', 'row_hint', 'context', 'visual', 'visual_hint')},
                              **({'has_visual_hint': True} if 'visual_hint' in step else {}),
                              **({'menu_path': step['context']['menu']['path']} if 'context' in step else {})}
                             for step in plan['steps']])


def resolve_saved_intents(plan):
    """Recover supported instructions in old tasks in memory, leaving the source file intact."""
    _validate(plan)
    steps = []
    for step in plan['steps']:
        if step.get('action') == 'manual' and (is_recoverable(step) or is_double_click_step(step)):
            text = step.get('instruction_draft', '')
            is_user_text = bool(text and text != LEGACY_INSTRUCTION_HINT)
            if not is_user_text:
                text = step.get('alias') or step.get('description', '')
            if text:
                # Only a complete instruction is executable. A legacy alias
                # like '조회 단계' remains the name of the step.
                try:
                    spec = compile_instruction(step, text)
                except TaskError:
                    # Older aliases and drafts were allowed to be arbitrary
                    # notes. Keep them available for review if parsing fails.
                    spec = None
                if spec and spec['mode'] != 'draft':
                    step = instructed_step(step, spec)
        steps.append(step)
    resolved = dict(plan, steps=steps)
    _validate(resolved)
    return resolved


def grounding_request(number, step):
    """Report a local, scoped host handoff; exclude screenshots and recorded data."""
    sentence = (step.get('grounding_text') or step.get('instruction_draft')
                or step.get('instruction', {}).get('text') or step.get('description') or step.get('alias'))
    result = dict(number=number, instruction=sentence or '', reason=step.get('reason', 'target_unavailable'))
    if 'window_context' in step:
        result['window_context'] = dict(step['window_context'])
    query = step.get('selector', {}).get('query') or (step.get('target') if step.get('match') == 'query' else None)
    if query:
        result['query'] = query
    return result


def has_groundable_draft(step):
    return bool(step.get('instruction_draft')) and (step['action'] != 'manual' or is_recoverable(step)
                                                     or is_double_click_step(step))


def _validate_step(s, parameters):
    if not isinstance(s, dict) or not isinstance(s.get('action'), str):
        raise TaskError('invalid_task_step')
    annotation = _annotations(s)
    if any(s[key] != annotation.get(key) for key in ('alias', 'description') if key in s):
        raise TaskError('invalid_step_annotation')
    if s['action'] in CAPTURE_COMMANDS:
        _validate_capture_command(s)
        return
    if s['action'] == 'visual-click':
        if _visual_step(s, s.get('label')) != s:
            raise TaskError('invalid_visual_step')
        return
    if s.get('action') == 'row-action':
        _validate_row(s)
        return
    if s.get('action') == 'row-launch':
        from row_intent import parse
        if (set(s) - set(annotation) - {'row_hint'} != {'action', 'ordinal', 'label'}
                or not _text(s.get('label'), 1000) or len(s['label'].encode('utf-8')) > 1000
                or type(s.get('ordinal')) is not int or parse(s['label']) != s['ordinal']):
            raise TaskError('invalid_row_step')
        if 'row_hint' in s:
            _validate_row({'action': 'row-action', 'label': 'row', 'row': s['row_hint']})
        return
    if s.get('action') == 'result-menu':
        _validate_context(s)
        return
    if s.get('action') == 'manual':
        expected = {'action', 'reason'}
        if 'menu_window_point' in s:
            _menu_window_point(s['menu_window_point'], s.get('reason'))
            expected.add('menu_window_point')
        if 'menu_anchor' in s:
            _menu_anchor(s['menu_anchor'], s.get('reason'))
            expected.add('menu_anchor')
        if 'visual_hint' in s:
            from visual_target import validate_spec
            if not (is_recoverable(s) or is_double_click_step(s)) or 'instruction_draft' not in s:
                raise TaskError('invalid_visual_step')
            validate_spec(s['visual_hint'])
            if 'gesture' in s['visual_hint'] and not is_double_click_step(s):
                raise TaskError('invalid_visual_step')
            expected.add('visual_hint')
        if 'instruction_draft' in s:
            expected.add('instruction_draft')
            draft = s['instruction_draft']
            if (not _text(draft, 1000)
                    or len(draft.encode('utf-8')) > 1000 or draft != draft.strip()):
                raise TaskError('invalid_step_instruction')
        if set(s) - set(annotation) != expected or s['reason'] != _reason(s['reason']):
            raise TaskError('invalid_task_step')
        return
    allowed = {'action', 'role', 'target', 'match', 'ancestors', 'within', 'label', 'command_name', *annotation}
    if 'wait_after' in s:
        allowed.add('wait_after')
        if (s.get('action') != 'open' or s.get('expand') or s.get('role') not in INVOKE
                or type(s['wait_after']) not in (int, float) or not 0 < s['wait_after'] <= 120):
            raise TaskError('invalid_step_wait')
    if 'label' in s and not _text(s['label'], 200):
        raise TaskError('invalid_task_target')
    if 'command_name' in s and (s.get('role') not in INVOKE or s.get('match') != 'automation_id'
                                or not _text(s['command_name'])):
        raise TaskError('invalid_task_target')
    if s.get('match') not in ('name', 'automation_id', 'query') or not _text(s.get('target')):
        raise TaskError('invalid_task_target')
    ancestors = s.get('ancestors', [])
    if (not isinstance(ancestors, list) or len(ancestors) > 4
            or any(not isinstance(a, dict) or a != _selector(a) for a in ancestors)):
        raise TaskError('invalid_task_scope')
    scope = (ancestors[0].get('automation_id') or ancestors[0].get('name')) if ancestors else None
    if s['match'] == 'query':
        if ('ancestors' in s or s['target'] != s['target'].strip() or ('within' in s
                and (not _text(s['within']) or s['within'] != s['within'].strip()))):
            raise TaskError('invalid_task_scope')
    elif s.get('within') != scope:
        raise TaskError('invalid_task_scope')
    instruction = None
    draft = s.get('instruction_draft')
    if 'instruction_draft' in s:
        allowed.add('instruction_draft')
        if ('instruction' in s or not _text(draft, 1000)
                or len(draft.encode('utf-8')) > 1000 or draft != draft.strip()):
            raise TaskError('invalid_step_instruction')
    if 'instruction' in s:
        allowed.add('instruction')
        instruction = _check_instruction(s, s['instruction'])
    if s['match'] == 'query' and ((instruction is None and draft is None) or not isinstance(s.get('role'), str) or (s.get('action') == 'open'
            and (s.get('role') not in INVOKE or s.get('expand'))) or (s.get('action') in VALUE_ACTIONS
            and s.get('recorded_effect') != 'unobserved')):
        raise TaskError('invalid_task_instruction_query')
    if s.get('action') in VALUE_ACTIONS:
        allowed.add('recorded_effect')
        if s.get('role') != VALUE_ACTIONS[s['action']] or s.get('recorded_effect') not in ('matched', 'unverified', 'unobserved'):
            raise TaskError('invalid_task_selection')
        if s['action'] == 'fill' and any(a['role'] == 'combobox' for a in ancestors):
            raise TaskError('text_input_owned_by_combo')
        if instruction is None:
            allowed.update(('value', 'parameter'))
            key = s.get('parameter')
            if draft is not None:
                if (('value' in s and not valid_value(s['value'], s['role'])) or ('parameter' in s
                        and (not _text(key, 1000) or '=' in key))):
                    raise TaskError('invalid_task_selection')
            elif (not valid_value(s.get('value'), s['role']) or not _text(key, 1000) or '=' in key
                  or key.casefold() in parameters or s.get('recorded_effect') == 'unobserved'):
                raise TaskError('invalid_task_selection')
            if draft is None:
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


def _validate(plan):
    if (not isinstance(plan, dict) or set(plan) != {'schema', 'name', 'exe', 'window', 'steps'}
            or type(plan['schema']) is not int or plan['schema'] != 1):
        raise TaskError('invalid_task')
    _name(plan['name'])
    _window(plan['exe'], plan['window'])
    if not isinstance(plan['steps'], list) or not 1 <= len(plan['steps']) <= MAX_STEPS:
        raise TaskError('invalid_task_steps')
    parameters = set()
    for number, step in enumerate(plan['steps'], 1):
        with _at_step(number):
            _validate_step(step, parameters)
    if any('window_context' in s for s in plan['steps']):
        from window_context import validate_sequence, ContextError
        try:
            validate_sequence(plan['steps'], allow_unbound_double=True)
        except ContextError as error:
            raise TaskError(error.code, step_number=error.step_number) from error


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
        try:
            directory = storage_paths.default_store()
        except storage_paths.StorageError as error:
            raise TaskError(error.code) from error
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
    return _write_document(plan, directory)


def _write_document(document, directory, *, replace_review=False):
    """Publish complete JSON. Only a revision-checked review editor may replace a file."""
    data = (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    if len(data) > MAX_FILE_BYTES:
        raise TaskError('task_too_large')
    try:
        path = _directory(directory) / (_name(document['name']) + '.json')
        _deny_redirect(path)
        if not replace_review:
            check_new_task(document['name'], directory)
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
            if replace_review:
                os.replace(temp, path)
            elif os.name == 'nt':
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


def read_document(name, directory=None):
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
        if not isinstance(plan, dict) or not isinstance(plan.get('name'), str):
            raise TaskError('invalid_task')
        if plan['name'].casefold() != name.casefold():
            raise TaskError('task_name_mismatch')
        return plan
    except (json.JSONDecodeError, UnicodeError, RecursionError) as error:
        raise TaskError('invalid_task') from error
    except OSError as error:
        raise TaskError('task_store_unavailable') from error


def load_task(name, directory=None, *, allow_review=False):
    plan = read_document(name, directory)
    if plan.get('kind') == 'recording-review':
        if allow_review:
            from review_drafts import validate
            return validate(plan)
        raise TaskError('recording_draft_requires_review')
    _validate(plan)
    return plan


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
        if not separator or not _text(key, 1000) or not valid_value(val, 'textfield'):
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


def recorded_overrides(sequence, overrides):
    """Bind supplied names and aliases using their recorded parameter contracts."""
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
            if s['action'] not in VALUE_ACTIONS:
                raise TaskError('unknown_parameter')
            if number in values or not valid_value(value, s['role']):
                raise TaskError('invalid_override')
            values[number] = value
    return values


def replay(plan, exe, window, overrides, invoke, *, only=None):
    """Invoke each requested step at most once. Unverified delivery stops the sequence."""
    check_binding(plan, exe, window)
    if only is not None:
        only = _step_number(plan, only)
    sequence = [(i, s) for i, s in enumerate(plan['steps'], 1) if only is None or only == i]
    values = recorded_overrides(sequence, overrides)
    results = []
    report = {'status': 'verified', 'steps': results, 'input_dispatched': False}
    for number, s in sequence:
        if has_groundable_draft(s):
            unresolved = [grounding_request(i, step) for i, step in sequence if has_groundable_draft(step)]
            return dict(report, status='needs_review', stopped_at=number, diagnostic='instruction_requires_structure',
                        grounding_steps=unresolved)
        if s['action'] == 'manual':
            return dict(report, status='needs_review', stopped_at=number, diagnostic=s['reason'])
    missing = [s['instruction']['parameter'] for number, s in sequence if number not in values
               and s.get('instruction', {}).get('mode') == 'required']
    if missing:
        return dict(report, status='needs_input', diagnostic='required_parameter_missing',
                    missing_parameters=list(dict.fromkeys(missing)))
    for number, s in sequence:
        step = s.copy()
        if step['action'] in VALUE_ACTIONS:
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
        if receipt.get('physical_input'):
            report['physical_input'] = True
        sent = receipt['input_dispatched']
        if sent is None or report['input_dispatched'] is None:
            report['input_dispatched'] = None
        elif sent is True:
            report['input_dispatched'] = True
        if receipt['status'] != 'verified':
            return dict(report, status=receipt['status'], stopped_at=number, diagnostic=receipt.get('diagnostic'))
    return report
