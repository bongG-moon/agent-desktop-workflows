"""Export confirmed UI targets as a small local, model-free workflow skill."""
from copy import deepcopy
import json
import math
import ntpath
from pathlib import Path
import re
import stat

from learned_tasks import TaskError


MAX_BYTES = 2 * 1024 * 1024
MAX_STEPS = 100
INVOKE_ROLES = {'button', 'splitbutton', 'hyperlink', 'menuitem'}


class ProfileError(TaskError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _need(ok, code):
    if not ok:
        raise ProfileError(code)


def _text(value, limit=1024):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= limit
            and not any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def _selector(value, *, ancestor=False):
    _need(isinstance(value, dict), 'invalid_registered_selector')
    allowed = {'role', 'automation_id', 'name'} if ancestor else {'role', 'automation_id', 'ancestors'}
    _need(not set(value) - allowed and _text(value.get('role'), 80), 'invalid_registered_selector')
    keys = [key for key in ('automation_id', 'name') if key in value]
    _need(bool(keys) and (ancestor or keys == ['automation_id'])
          and all(_text(value[key]) for key in keys), 'invalid_registered_selector')
    if 'ancestors' in value:
        chain = value['ancestors']
        _need(isinstance(chain, list) and len(chain) <= 16, 'invalid_registered_ancestors')
        for parent in chain:
            _selector(parent, ancestor=True)
    return deepcopy(value)


def validate(profile):
    """Return an independent copy after validating the complete, closed schema."""
    _need(isinstance(profile, dict) and set(profile) == {'schema', 'name', 'exe', 'window', 'steps'}
          and type(profile.get('schema')) is int and profile['schema'] == 1, 'invalid_registered_profile')
    _need(_text(profile.get('name'), 200), 'invalid_registered_name')
    exe = profile.get('exe')
    _need(_text(exe, 200) and exe == ntpath.basename(exe) and exe.casefold().endswith('.exe')
          and not re.search(r'[<>:"/\\|?*]', exe), 'invalid_registered_exe')
    _need(_text(profile.get('window')), 'invalid_registered_window')
    steps = profile.get('steps')
    _need(isinstance(steps, list) and 1 <= len(steps) <= MAX_STEPS, 'invalid_registered_steps')
    parameter_spellings = {}
    result = deepcopy(profile)
    for step in result['steps']:
        _need(isinstance(step, dict), 'invalid_registered_step')
        action = step.get('action')
        if action == 'wait':
            seconds = step.get('seconds')
            _need(set(step) == {'action', 'seconds'} and type(seconds) in (int, float)
                  and math.isfinite(seconds) and 0 <= seconds <= 120, 'invalid_registered_wait')
            continue
        _need(action in {'set-value', 'invoke'}, 'unsupported_registered_action')
        allowed = {'action', 'selector', 'label'}
        if action == 'set-value':
            allowed.update({'value', 'parameter', 'default'})
        _need(not set(step) - allowed, 'unexpected_registered_step_data')
        if 'label' in step:
            _need(_text(step['label'], 1000), 'invalid_registered_label')
        selector = _selector(step.get('selector'))
        if action == 'set-value':
            _need(selector['role'] == 'combobox', 'invalid_registered_role')
            _need(('value' in step) != ('parameter' in step), 'registered_value_or_parameter_required')
            if 'value' in step:
                _need('default' not in step and _text(step['value'], 10000), 'invalid_registered_value')
            else:
                name = step['parameter']
                _need(_text(name, 200) and name == name.strip() and not any(c in name for c in '{}=,;'),
                      'invalid_registered_parameter')
                # One input is shared by case-insensitive references, preserving
                # the first spelling in the exported command documentation.
                step['parameter'] = parameter_spellings.setdefault(name.casefold(), name)
                if 'default' in step:
                    _need(_text(step['default'], 10000), 'invalid_registered_default')
        else:
            _need(selector['role'] in INVOKE_ROLES, 'invalid_registered_role')
    _need(len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf8')) <= MAX_BYTES,
          'registered_profile_too_large')
    return result


def _local_path(path):
    path = Path(path).absolute()
    _need(not str(path).startswith(('\\\\', '//')), 'local_registered_path_required')
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        _need(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 1024,
              'redirected_registered_path')
    return path


def _unique_keys(pairs):
    result = dict(pairs)
    _need(len(result) == len(pairs), 'duplicate_registered_key')
    return result


def load(path):
    path = _local_path(path)
    try:
        with path.open('rb') as stream:
            data = stream.read(MAX_BYTES + 1)
    except OSError as error:
        raise ProfileError('registered_profile_unavailable') from error
    _need(len(data) <= MAX_BYTES, 'registered_profile_too_large')
    try:
        profile = json.loads(data.decode('utf-8-sig'), object_pairs_hook=_unique_keys)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ProfileError('invalid_registered_json') from error
    return validate(profile)


def from_task(plan, resolution, resolve_selector):
    """Resolve each confirmed operation once while leaving the original task intact."""
    import learned_tasks as tasks
    from intent_replay import validate_action
    from intent_tasks import validate_resolution

    tasks._validate(plan)
    _need(callable(resolve_selector), 'registered_resolver_required')
    for step in plan['steps']:
        _need(step['action'] in {'select', 'open', 'intent'}, 'registered_step_requires_supported_action')
    has_intent = any(step['action'] == 'intent' for step in plan['steps'])
    _need(not has_intent or resolution is not None, 'registered_resolution_required')
    resolved = validate_resolution(plan, resolution, validate_action) if has_intent else {}
    prepared = []
    for number, source in enumerate(plan['steps'], 1):
        if source['action'] == 'intent':
            operations = resolved[number]
        else:
            operation = {key: deepcopy(source[key]) for key in ('action', 'target', 'role', 'match', 'expand')
                         if key in source}
            if source['action'] == 'select':
                spec = source.get('instruction')
                if spec:
                    key = 'parameter' if spec['mode'] == 'required' else 'value'
                    operation[key] = spec[key]
                else:
                    operation['parameter'] = source['parameter']
            operations = [operation]
        for operation in operations:
            _need(operation['action'] in {'select', 'open'} and not operation.get('expand', False),
                  'registered_step_requires_supported_action')
            if operation['action'] == 'open':
                _need(operation['role'] in INVOKE_ROLES, 'registered_step_requires_supported_action')
            prepared.append((source, operation))
    _need(1 <= len(prepared) <= MAX_STEPS, 'invalid_registered_steps')
    steps = []
    for source, operation in prepared:
        selector = resolve_selector(deepcopy(source), deepcopy(operation))
        step = {'action': 'set-value' if operation['action'] == 'select' else 'invoke',
                'selector': _selector(selector)}
        label = source.get('alias') or source.get('label')
        if label:
            step['label'] = label
        if step['action'] == 'set-value':
            key = 'parameter' if 'parameter' in operation else 'value'
            step[key] = operation[key]
            if source['action'] == 'select' and 'instruction' not in source:
                step['default'] = source['value']
        steps.append(step)
    return validate({'schema': 1, 'name': plan['name'], 'exe': plan['exe'], 'window': plan['window'], 'steps': steps})


RUNNER = '''"""Run this registered local workflow using its sibling Desktop Select runtime."""
from pathlib import Path
import sys

skill = Path(__file__).resolve().parents[1]
runtime = skill.parent / 'desktop-select' / 'scripts'
if not (runtime / 'registered_runtime.py').is_file():
    raise SystemExit('Desktop Select 0.6.0 이상을 같은 skills 폴더에 설치해 주세요.')
sys.path.insert(0, str(runtime))
from registered_runtime import main

if __name__ == '__main__':
    raise SystemExit(main(profile_path=skill / 'workflow.json'))
'''


def export_skill(profile, out):
    """Create a new local skill directory, never alter a source recording or existing skill."""
    profile = validate(profile)
    out = _local_path(out)
    _need(bool(re.fullmatch(r'[a-z][a-z0-9-]{1,79}', out.name)), 'invalid_registered_skill_slug')
    _need(out.parent.is_dir(), 'registered_export_parent_missing')
    _need(not out.exists(), 'registered_export_already_exists')
    parameters = list(dict.fromkeys(step['parameter'] for step in profile['steps'] if 'parameter' in step))
    parameter_text = ', '.join(parameters) if parameters else '없음'
    guide = f'''---
name: {out.name}
description: 등록된 Windows 업무 화면의 조건 설정과 버튼 실행을 로컬 Python으로 수행합니다.
---

# {profile['name']}

Desktop Select 0.6.0 이상이 같은 skills 폴더의 `desktop-select`에 설치되어 있어야 합니다.
등록한 대상과 순서가 `workflow.json`에 저장되어 있습니다. 실행할 때 문장을 다시 해석하거나 코드를 수정하지 않습니다.

사용자 요청에 포함된 입력값을 `--set`으로 전달하세요. 빠진 필수 변수만 질문하세요. 실행할 변수: {parameter_text}.
변수를 생략하면 필요한 값을 알려 줍니다. 기존 녹화에서 명시적으로 저장한 기본값이 있는 변수만 기본값을 사용할 수 있습니다.

현재 창과 등록 대상을 입력 없이 확인:

```powershell
python -X utf8 -B scripts/run.py --read-only
```

작업 실행:

```powershell
python -X utf8 -B scripts/run.py --set "Family=Hybrid"
```

`Family`는 사용 예입니다. 이 작업의 변수 이름으로 바꾸고, 변수가 없으면 `--set`을 생략하세요. 여러 변수는 `--set "이름=값"`을 반복합니다.
현재 작업의 실행 파일과 창 제목이 일치하는 창을 사용합니다. 다른 창을 임의로 선택하지 마세요.
조건값의 읽기 검증과 버튼 호출 전달 결과를 구분해 보고합니다. 대기 시간이 지났거나 버튼이 호출된 것만으로 조회 완료나 Excel 파일 생성을 확인했다고 말하지 마세요.
불명확한 입력 결과가 나오면 같은 입력을 반복하지 말고 현재 결과를 보고하세요.
이 스킬은 기존 CUA MCP와 별개이며 CUA 드라이버나 외부 모델 연결을 추가하지 않습니다.
'''
    try:
        out.mkdir()
        scripts = out / 'scripts'
        scripts.mkdir()
        files = {'SKILL.md': guide,
                 'workflow.json': json.dumps(profile, ensure_ascii=False, allow_nan=False, indent=2) + '\n',
                 'scripts/run.py': RUNNER}
        for relative, content in files.items():
            _local_path(out / relative)
            with (out / relative).open('x', encoding='utf8', newline='\n') as stream:
                stream.write(content)
        return {'status': 'exported', 'path': str(out), 'files': list(files),
                'parameters': parameters, 'input_dispatched': False}
    except FileExistsError as error:
        raise ProfileError('registered_export_already_exists') from error
    except OSError as error:
        raise ProfileError('registered_export_unavailable') from error
