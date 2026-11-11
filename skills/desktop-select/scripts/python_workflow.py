"""Register a small, literal Python workflow beside the original 0.4 recorder.

One UIA session runs known controls. No model plan, replay cache, shell text or
implicit retry is involved. UIA discovery remains available for first setup.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import subprocess
import sys
import time

import desktop_select as app
import control_lookup as lookup
import live_action as live


class WorkflowError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def need(ok, reason):
    if not ok:
        raise WorkflowError(reason)


def text(value, limit=1000):
    return (isinstance(value, str) and bool(value.strip()) and len(value.encode('utf8')) <= limit
            and not any(ord(c) < 32 for c in value))


def validate(recipe):
    need(isinstance(recipe, dict) and recipe.get('schema') == 1, 'invalid_workflow')
    need(not set(recipe) - {'schema', 'name', 'exe', 'window', 'steps'}, 'unknown_workflow_field')
    app.tasks._window(recipe.get('exe'), recipe.get('window'))
    need(text(recipe.get('name'), 200), 'workflow_name_required')
    steps = recipe.get('steps')
    need(isinstance(steps, list) and 0 < len(steps) <= 100, 'invalid_workflow_steps')
    for step in steps:
        need(isinstance(step, dict), 'invalid_workflow_step')
        if step.get('action') == 'wait':
            need(set(step) == {'action', 'seconds'} and type(step.get('seconds')) in (int, float)
                 and 0 <= step['seconds'] <= 120, 'invalid_wait')
            continue
        need(not set(step) - {'action', 'selector', 'value', 'parameter', 'label', 'command_name', 'field_label'}, 'unknown_step_field')
        need(step.get('action') in ('set-value', 'invoke'), 'unsupported_workflow_action')
        selector = step.get('selector')
        lookup._selector(selector)
        need(text(step.get('label', 'step'), 200), 'invalid_step_label')
        need('command_name' not in step or (step['action'] == 'invoke' and text(step['command_name'])),
             'invalid_command_name')
        need('field_label' not in step or (step['action'] == 'set-value' and text(step['field_label'], 200)),
             'invalid_field_label')
        if step['action'] == 'set-value':
            need(selector['role'] == 'combobox', 'combo_required')
            need(('value' in step) != ('parameter' in step), 'fixed_value_or_parameter_required')
            if 'value' in step:
                need(text(step['value'], 10000), 'invalid_combo_value')
            else:
                need(text(step['parameter'], 100) and step['parameter'] == step['parameter'].strip()
                     and '=' not in step['parameter'], 'invalid_parameter')
        else:
            need(selector['role'] in ('button', 'menuitem', 'splitbutton', 'hyperlink')
                 and 'value' not in step and 'parameter' not in step, 'invalid_invoke')
    return recipe


def load(path):
    path = Path(path)
    need(path.is_file() and path.stat().st_size <= 256000, 'workflow_file_unavailable')
    return validate(json.loads(path.read_text(encoding='utf-8-sig')))


def values_for(recipe, overrides):
    parameters = {s['parameter'].casefold(): s['parameter'] for s in recipe['steps'] if 'parameter' in s}
    values = {k.casefold(): v for k, v in overrides.items()}
    need(len(values) == len(overrides) and all(text(v, 10000) for v in values.values()), 'invalid_override')
    need(not set(values) - parameters.keys(), 'unknown_parameter')
    return values, [name for key, name in parameters.items() if key not in values]


def bind(exe, title, hwnd=None):
    app.bridge.set_dpi_awareness()
    row = app.pick_window(app.windows(exe), title, hwnd)
    binding = app.bridge.Binding(row)
    app.guard(binding, binding, title)
    return binding


def wait(binding, title, seconds):
    deadline = time.monotonic() + seconds
    while True:
        app.guard(binding, binding, title)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(.2, remaining))


def combine(previous, current):
    return None if previous is None or current is None else bool(previous or current)


def run(recipe, overrides=None, *, read_only=False, hwnd=None, allow_recorded_selection=False):
    recipe = validate(recipe)
    values, missing = values_for(recipe, overrides or {})
    if missing and not read_only:
        return dict(status='needs_input', missing_parameters=missing, steps=[], input_dispatched=False)
    report = dict(status='observed' if read_only else 'verified', steps=[], input_dispatched=False,
                  business_commit='not_verified')
    started = time.monotonic()
    number, dispatching = 0, False
    try:
        binding = bind(recipe['exe'], recipe['window'], hwnd)
        report['window_restored'] = False if read_only else app.visibility.restore_for_action(binding, recipe['window'])
        with app.bridge.UIA() as uia:
            root = uia.from_handle(binding.hwnd)
            for number, step in enumerate(recipe['steps'], 1):
                app.guard(binding, binding, recipe['window'], mutate=not read_only)
                if step['action'] == 'wait':
                    if read_only:
                        continue
                    wait(binding, recipe['window'], step['seconds'])
                    receipt = dict(status='waited', seconds=step['seconds'], input_dispatched=False,
                                   business_result='not_verified')
                else:
                    selector = step['selector']
                    element = lookup.resolve(binding, uia, selector, root=root)
                    need('command_name' not in step or element.property(30005) == step['command_name'],
                         'command_name_changed')
                    if step.get('field_label'):
                        app.tasks.check_display({'label': step['field_label']},
                                                app.field_label(binding, uia, element, root))
                    if read_only:
                        receipt = dict(status='observed', input_dispatched=False, actions=uia.actions(element))
                        if step['action'] == 'set-value':
                            receipt.update(live.combo_readback(binding, uia, element))
                        else:
                            receipt['command_name'] = element.property(30005) or ''
                    else:
                        def check():
                            app.guard(binding, binding, recipe['window'], mutate=True)
                            current = lookup.resolve(binding, uia, selector, root=root)
                            need(uia.same(element, current), 'target_changed')
                            need('command_name' not in step or current.property(30005) == step['command_name'],
                                 'command_name_changed')
                            if step.get('field_label'):
                                app.tasks.check_display({'label': step['field_label']},
                                                        app.field_label(binding, uia, current, root))
                            if step['action'] != 'set-value' or not allow_recorded_selection:
                                need(('SetValue' if step['action'] == 'set-value' else 'Click') in uia.actions(current),
                                     'workflow_pattern_unavailable')
                        value = values[step['parameter'].casefold()] if 'parameter' in step else step.get('value')
                        dispatching = True
                        receipt = live.execute(binding, uia, element, {'role': selector['role']},
                                               'select' if step['action'] == 'set-value' else 'invoke',
                                               value, still_valid=check)
                        dispatching = False
                report['steps'].append(dict(number=number, label=step.get('label', ''), receipt=receipt))
                report['input_dispatched'] = combine(report['input_dispatched'], receipt['input_dispatched'])
                if read_only or receipt['status'] in ('verified', 'waited'):
                    continue
                delivered = (step['action'] == 'invoke' and receipt['status'] == 'delivered_unverified'
                             and receipt['input_dispatched'] is True
                             and receipt.get('diagnostic') == 'business_result_not_verified')
                if delivered:
                    report['status'] = 'completed_unverified'
                else:
                    report.update(status=receipt['status'], stopped_at=number,
                                  diagnostic=receipt.get('diagnostic', 'step_not_verified'))
                    break
    except Exception as error:
        diagnostic = getattr(error, 'code', 'provider_error')
        failure = dict(status='uncertain' if dispatching else 'blocked',
                       input_dispatched=None if dispatching else False, diagnostic=diagnostic)
        if number and len(report['steps']) < number:
            report['steps'].append(dict(number=number, label=step.get('label', ''), receipt=failure))
            report['stopped_at'] = number
        report['input_dispatched'] = combine(report['input_dispatched'], failure['input_dispatched'])
        report.update(status=failure['status'], diagnostic=diagnostic)
    report['elapsed_seconds'] = round(time.monotonic() - started, 4)
    return report


def discover(exe, title, hwnd=None):
    """Only form controls, with nearby labels; never enumerate option inventories."""
    binding = bind(exe, title, hwnd)
    found = []
    with app.bridge.UIA() as uia:
        root = uia.from_handle(binding.hwnd)
        for element in lookup.find_all(uia, root, roles=('combobox', 'button'), limit=256):
            identifier = element.property(30011)
            if not text(identifier):
                continue
            item = dict(automation_id=identifier, role=app.bridge.ROLES[element.property(30003)],
                        name=element.property(30005) or '', actions=uia.actions(element))
            item.update(app.field_label(binding, uia, element, root))
            found.append(item)
        app.guard(binding, binding, title)
    return dict(status='observed', targets=found, input_dispatched=False)


RUNNER = '''"""Run the locally registered workflow with the sibling Desktop Select skill."""
from pathlib import Path
import sys

HERE = Path(__file__).resolve()
RUNTIME = HERE.parents[2] / "desktop-select" / "scripts"
if not (RUNTIME / "python_workflow.py").is_file():
    raise SystemExit("Install Desktop Select 0.4.3 or later beside this skill.")
sys.path.insert(0, str(RUNTIME))
import python_workflow
raise SystemExit(python_workflow.main(["run", "--recipe", str(HERE.parents[1] / "workflow.json"), *sys.argv[1:]]))
'''


def register(recipe, destination, hwnd=None, *, source_task=None):
    """Validate current targets read-only, then create three local text files."""
    recipe = deepcopy(validate(recipe))
    if source_task is not None:
        app.tasks._validate(source_task)
    destination = Path(destination).resolve()
    need(not destination.exists(), 'workflow_destination_exists')
    need(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', destination.name), 'workflow_folder_name_required')
    observed = run(recipe, read_only=True, hwnd=hwnd)
    if observed['status'] != 'observed':
        return observed
    active = [s for s in recipe['steps'] if s['action'] != 'wait']
    for step, row in zip(active, observed['steps']):
        need(('SetValue' if step['action'] == 'set-value' else 'Click') in row['receipt']['actions'],
             'workflow_pattern_unavailable')
        if step['action'] == 'invoke' and text(row['receipt'].get('command_name')):
            step['command_name'] = row['receipt']['command_name']
    required = list(dict.fromkeys(s['parameter'] for s in active if 'parameter' in s))
    destination.mkdir(parents=True, exist_ok=False)
    (destination / 'scripts').mkdir()
    (destination / 'workflow.json').write_text(json.dumps(recipe, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    (destination / 'scripts/run.py').write_text(RUNNER, encoding='utf8')
    if source_task is not None:
        (destination / 'source-task.json').write_text(json.dumps(source_task, ensure_ascii=False, indent=2) + '\n',
                                                     encoding='utf8')
    guide = ('---\nname: ' + destination.name + '\ndescription: 로컬에 등록한 Windows 업무 순서를 실행한다.\n---\n\n'
             '# 등록된 업무 실행\n\n'
             '업무명, 대상 창과 순서는 `workflow.json`에 있다. 사용자의 실행 요청 범위와 대조한다. '
             '필수 입력은 ' + json.dumps(required, ensure_ascii=False) + '이다. 값을 받지 않으면 실행하지 않는다.\n\n'
             '`python -X utf8 -B scripts/run.py --set "변수명=값"`으로 실행한다. '
             '현재 상태만 확인할 때는 `--read-only`를 붙인다. 이 명령은 대상 창을 복원하지 않는다.\n\n'
             '콤보 확인 실패 또는 불확실한 전달이면 다음 단계로 진행하지 않는다. '
             '조회/내보내기 버튼 전달과 대기는 업무 완료나 파일 생성의 증거가 아니다. 재클릭하지 않는다. '
             '동일 폴더 아래 `desktop-select`가 필요하며 외부 통신과 CUA 드라이버는 사용하지 않는다.\n')
    if source_task is not None:
        guide += '\n녹화한 위치와 자연어 지시 원문은 `source-task.json`에 보관되어 있다. 실행에는 `workflow.json`을 사용한다.\n'
    (destination / 'SKILL.md').write_text(guide, encoding='utf8')
    return dict(status='registered', path=str(destination), required_parameters=required, input_dispatched=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Discover once, register literal actions, run with Python.')
    sub = parser.add_subparsers(dest='command', required=True)
    discovery = sub.add_parser('discover')
    discovery.add_argument('--exe', required=True)
    discovery.add_argument('--window', required=True)
    discovery.add_argument('--hwnd', type=int)
    for command in ('register', 'run'):
        p = sub.add_parser(command)
        if command == 'register':
            source = p.add_mutually_exclusive_group(required=True)
            source.add_argument('--recipe', type=Path)
            source.add_argument('--task', help='saved natural-language recording to register')
        else:
            p.add_argument('--recipe', type=Path, required=True)
        p.add_argument('--hwnd', type=int)
        if command == 'register':
            p.add_argument('--out', type=Path, required=True)
        else:
            p.add_argument('--set', action='append', default=[])
            p.add_argument('--read-only', action='store_true')
    parser.add_argument('--_worker', action='store_true', help=argparse.SUPPRESS)
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(argv)
    mutating = args.command == 'run' and not args.read_only
    launched = False
    try:
        source_task = None
        recipe = None
        if args.command != 'discover':
            if getattr(args, 'task', None):
                import recorded_plan
                source_task = app.tasks.load_task(args.task, app.task_directory())
                planned = recorded_plan.get_plan(source_task, app.task_directory())
                need(bool(planned.get('envelope')), 'recorded_task_not_compatible')
                recipe = planned['envelope']['recipe']
            else:
                recipe = load(args.recipe)
        if args._worker:
            if args.command == 'discover':
                result = discover(args.exe, args.window, args.hwnd)
            elif args.command == 'register':
                result = register(recipe, args.out, args.hwnd, source_task=source_task)
            else:
                result = run(recipe, app.tasks.parse_overrides(args.set),
                             read_only=args.read_only, hwnd=args.hwnd)
        else:
            timeout = 90 + sum(s.get('seconds', 0) for s in recipe['steps']) if recipe else 90
            launched = True
            child = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(Path(__file__).resolve()),
                                    '--_worker', *argv], capture_output=True, encoding='utf8', timeout=timeout)
            result = json.loads(child.stdout)
            need(child.returncode in (0, 2, 3) and isinstance(result, dict) and 'status' in result,
                 'invalid_worker_receipt')
    except (Exception, KeyboardInterrupt) as error:
        uncertain = (launched or args._worker) and mutating
        result = dict(status='uncertain' if uncertain else 'blocked', input_dispatched=None if uncertain else False,
                      diagnostic=getattr(error, 'code', 'workflow_unavailable'))
    print(json.dumps(result, ensure_ascii=False))
    return (0 if result['status'] in ('registered', 'observed', 'verified') else
            3 if result['status'] in ('completed_unverified', 'uncertain', 'delivered_unverified') else 2)


if __name__ == '__main__':
    raise SystemExit(main())
