"""Store demonstrated locations and free instructions, then bind typed local plans.

Natural text is data for the user's Claude Code model. This module does not
evaluate it, call a model endpoint, or execute a command. A resolution is tied
to the complete saved plan and can contain only independently validated UI
operations, never Python, shell, or unrestricted coordinate commands.
"""
from copy import deepcopy
from hashlib import sha256
import json
import math
import re


MAX_TEXT = 16000
MAX_PLAN_BYTES = 4 * 1024 * 1024
MAX_PARAMETERS = 64
MAX_ACTIONS = 300
MAX_ACTIONS_PER_STEP = 32


class IntentError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _need(condition, code):
    if not condition:
        raise IntentError(code)


def _text(value, limit, *, multiline=False, empty=False):
    return (isinstance(value, str) and (empty or bool(value.strip())) and len(value) <= limit
            and not any((ord(c) < 32 and not (multiline and c in '\r\n\t'))
                        or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def _parameter(value):
    _need(_text(value, 100) and value == value.strip()
          and not any(c in value for c in '{}=,;'), 'invalid_intent_parameter')
    return value


def _parameters(values):
    _need(isinstance(values, list) and len(values) <= MAX_PARAMETERS, 'invalid_intent_parameters')
    result, seen = [], set()
    for value in values:
        name = _parameter(value)
        if name.casefold() not in seen:
            result.append(name)
            seen.add(name.casefold())
    return result


def _subject(text):
    return re.sub(r'\s*(?:조건|콤보박스|콤보|항목|필드)$', '', text.strip()).strip()


def collect_parameters(text, explicit=None):
    """Recognize explicit runtime-input references without parsing the action.

    Unrecognized prose remains unchanged. The UI can always supply names
    explicitly; a model interprets the remainder when preparing a resolution.
    """
    _need(_text(text, MAX_TEXT, multiline=True, empty=True), 'invalid_intent_text')
    names = _parameters([] if explicit is None else explicit)
    # Do not treat JSON/object syntax inside braces as a variable reference.
    names.extend(re.findall(r'\{([\w][\w .-]{0,99})\}', text))
    # A Korean clause can contain several fields. Infer only a compact noun;
    # multiword custom parameter names stay available through braces/UI input.
    name = r'([\w][\w.-]{0,99}(?:\s*(?:조건|콤보박스|콤보|항목|필드))?)'
    for match in re.finditer(r'입력\s*(?:받은|받을|받는)\s*(?:변수|값)(?:를|을)\s*' + name + r'에\s*(?:입력|설정|선택|넣|적용)', text):
        subject = _subject(match[1])
        if subject not in {'이', '그', '해당', '현재', '여기', '거기'}:
            names.append(subject)
    for match in re.finditer(name + r'(?:은|는|을|를)\s*(?:실행\s*(?:할\s*때|시)\s*)?입력\s*받', text):
        names.append(_subject(match[1]))
    for match in re.finditer(r'입력\s*받은\s+' + name + r'\s*변수', text):
        names.append(_subject(match[1]))
    # Inferred fragments are only suggestions that meet the same identifier
    # rules as an explicit name; other prose never makes the save fail.
    clean = []
    for value in names:
        try:
            clean.append(_parameter(value))
        except IntentError:
            continue
    result = list(dict.fromkeys(name.casefold() for name in clean))
    _need(len(result) <= MAX_PARAMETERS, 'too_many_intent_parameters')
    return [next(name for name in clean if name.casefold() == folded) for folded in result]


def _selector(value):
    if not isinstance(value, dict) or not _text(value.get('role'), 80):
        return None
    for key in ('automation_id', 'name'):
        text = value.get(key)
        if _text(text, 1024) and not (key == 'automation_id' and text.isdecimal()):
            return {'role': value['role'], key: text}
    return None


def _anchor(value):
    if value is None:
        return None
    if isinstance(value, dict) and value.get('secure') is True:
        return None
    result = _selector(value)
    if result is None:
        return None
    chain = value.get('ancestors', [])
    if not isinstance(chain, list) or len(chain) > 4:
        return None
    ancestors = [_selector(parent) for parent in chain]
    if not all(ancestors):
        return None
    if ancestors:
        result['ancestors'] = ancestors
    point = value.get('point')
    if point is not None:
        if (not isinstance(point, list) or len(point) != 2 or
                any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in point)):
            return None
        result['point'] = list(point)
    return result


def make_step(raw, text, *, parameters=None, label=None):
    """Keep any free instruction even when the demonstration had no usable target."""
    _need(_text(text, MAX_TEXT, multiline=True, empty=True), 'invalid_intent_text')
    raw = raw if isinstance(raw, dict) else {}
    source = raw.get('anchor')
    if 'anchor' not in raw:
        target = raw.get('target', {})
        source = target.get('locator') if isinstance(target, dict) else None
        if source is None and 'role' in raw:
            source = raw
    anchor = None if raw.get('secure') is True else _anchor(source)
    if label is None:
        label = next((raw.get(k) for k in ('label', 'field_label', 'alias') if _text(raw.get(k), 200)), None)
    if label is None and anchor:
        label = anchor.get('name') or anchor.get('automation_id')
    if not _text(label, 200):
        label = '설명으로 실행할 단계'
    step = {'action': 'intent', 'label': label, 'anchor': anchor,
            'instruction': {'mode': 'natural', 'text': text,
                            'parameters': collect_parameters(text, parameters)}}
    for key, limit in (('alias', 120), ('description', 1000)):
        value = raw.get(key, '')
        if value == '':
            continue
        _need(_text(value, limit) and len(value.encode('utf-8')) <= limit, 'invalid_intent_annotation')
        step[key] = value.strip()
    validate_step(step)
    return step


def validate_step(step):
    required = {'action', 'label', 'anchor', 'instruction'}
    _need(isinstance(step, dict) and required <= set(step) and not set(step) - required - {'alias', 'description'}
          and step.get('action') == 'intent' and _text(step.get('label'), 200), 'invalid_intent_step')
    for key, limit in (('alias', 120), ('description', 1000)):
        if key in step:
            value = step[key]
            _need(_text(value, limit) and value == value.strip() and len(value.encode('utf-8')) <= limit,
                  'invalid_intent_annotation')
            if key == 'alias':
                _need(not value.isdecimal() and '=' not in value, 'invalid_intent_alias')
    anchor = step.get('anchor')
    _need(anchor is None or (_anchor(anchor) is not None and _anchor(anchor) == anchor), 'invalid_intent_anchor')
    instruction = step.get('instruction')
    _need(isinstance(instruction, dict) and set(instruction) == {'mode', 'text', 'parameters'}
          and instruction.get('mode') == 'natural'
          and _text(instruction.get('text'), MAX_TEXT, multiline=True, empty=True), 'invalid_intent_instruction')
    parameters = _parameters(instruction.get('parameters'))
    _need(parameters == instruction['parameters'], 'invalid_intent_parameters')
    inferred = collect_parameters(instruction['text'])
    _need(all(name.casefold() in {p.casefold() for p in parameters} for name in inferred),
          'intent_parameter_missing')
    return step


def plan_digest(plan):
    """Bind a resolution to exact instructions, locations, ordering and app identity."""
    try:
        encoded = json.dumps(plan, ensure_ascii=False, allow_nan=False, sort_keys=True,
                             separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise IntentError('invalid_intent_plan') from error
    _need(len(encoded) <= MAX_PLAN_BYTES, 'intent_plan_too_large')
    return sha256(encoded).hexdigest()


def interpretation_request(plan):
    """Return local model context, not an invocation of a model or external service."""
    steps = []
    for number, step in enumerate(plan['steps'], 1):
        if step.get('action') == 'intent':
            validate_step(step)
            steps.append(dict(number=number, **deepcopy(step)))
    return {'status': 'needs_interpretation', 'diagnostic': 'natural_instructions_require_resolution',
            'input_dispatched': False, 'plan_digest': plan_digest(plan), 'steps': steps,
            'supported_actions': ['select', 'open', 'result-menu']}


def validate_resolution(plan, resolution, validate_action):
    """Validate all intent resolutions before the caller may dispatch any input.

    The callback checks each typed operation's complete schema. A declared
    runtime input must stay a parameter binding, never a guessed fixed value.
    Resolutions are transient and do not overwrite the original demonstration.
    """
    _need(callable(validate_action), 'resolution_validator_required')
    _need(isinstance(resolution, dict) and set(resolution) == {'schema', 'plan_digest', 'steps'}
          and type(resolution.get('schema')) is int and resolution['schema'] == 1,
          'invalid_intent_resolution')
    _need(resolution.get('plan_digest') == plan_digest(plan), 'resolution_plan_changed')
    entries = resolution.get('steps')
    _need(isinstance(entries, list) and len(entries) <= MAX_ACTIONS, 'invalid_resolution_steps')
    wanted = {i: step for i, step in enumerate(plan['steps'], 1) if step.get('action') == 'intent'}
    results, total = {}, 0
    for entry in entries:
        _need(isinstance(entry, dict) and set(entry) == {'number', 'actions'}, 'invalid_resolution_step')
        number, actions = entry.get('number'), entry.get('actions')
        _need(type(number) is int and number in wanted and number not in results, 'resolution_step_mismatch')
        validate_step(wanted[number])
        _need(isinstance(actions, list) and 1 <= len(actions) <= MAX_ACTIONS_PER_STEP, 'invalid_resolution_actions')
        total += len(actions)
        _need(total <= MAX_ACTIONS, 'too_many_resolution_actions')
        declared = {p.casefold(): p for p in wanted[number]['instruction']['parameters']}
        used, validated = set(), []
        for raw in actions:
            _need(isinstance(raw, dict) and raw.get('action') in {'select', 'open', 'result-menu'},
                  'unsupported_resolution_action')
            action = deepcopy(raw)
            if 'parameter' in action:
                name = _parameter(action['parameter'])
                _need(action['action'] == 'select' and 'value' not in action,
                      'invalid_resolution_parameter_binding')
                used.add(name.casefold())
                action['parameter'] = declared.get(name.casefold(), name)
            parameter = action.get('parameter')
            normalized = validate_action(action)
            _need(isinstance(normalized, dict) and normalized.get('action') in {'select', 'open', 'result-menu'},
                  'invalid_resolution_action')
            if parameter is not None:
                _need(normalized.get('parameter') == parameter and 'value' not in normalized,
                      'invalid_resolution_parameter_binding')
            if 'parameter' in normalized:
                name = _parameter(normalized['parameter'])
                _need(normalized['action'] == 'select' and 'value' not in normalized,
                      'invalid_resolution_parameter_binding')
                used.add(name.casefold())
            validated.append(deepcopy(normalized))
        _need(set(declared) <= used, 'resolution_parameter_not_used')
        results[number] = validated
    _need(set(results) == set(wanted), 'resolution_incomplete')
    return results
