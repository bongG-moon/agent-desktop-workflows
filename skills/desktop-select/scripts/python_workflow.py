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
import context_menu
import export_evidence
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


def validate_selector(selector):
    if isinstance(selector, dict) and 'query' in selector:
        need(not set(selector) - {'query', 'role', 'within'} and text(selector['query'])
             and selector.get('role') in app.bridge.ROLES.values()
             and ('within' not in selector or text(selector['within'])), 'invalid_semantic_selector')
    else:
        lookup._selector(selector)


def semantic_match(binding, uia, root, selector):
    """Use the same current-window label/name resolver as a direct command."""
    nodes, elements = app.observe(binding, uia)
    need(bool(elements) and uia.same(root, elements[0]), 'target_window_changed')
    index, basis = app.resolve(nodes, selector['query'], role=selector['role'], within=selector.get('within'))
    element, node = elements[index], nodes[index]
    lookup.ancestry(uia, element, root, pid=binding.pid)
    # Promote only an identity witnessed in this live read, never old coordinates.
    stable = app._stable_selector(nodes, index)
    if stable is not None:
        current = lookup.resolve(binding, uia, stable, root=root)
        need(uia.same(element, current), 'target_changed')
    observed_label = app.field_labels(nodes).get(index, {})
    proof = dict(matched_by=basis, query=selector['query'], role=node['role'])
    if node.get('automation_id'):
        proof['automation_id'] = node['automation_id']
    if node.get('name'):
        proof['name'] = node['name']
    proof.update(observed_label)
    return element, stable, proof


def resolve_step(binding, uia, root, selector):
    if 'query' in selector:
        return semantic_match(binding, uia, root, selector)
    return lookup.resolve(binding, uia, selector, root=root), selector, None


def revalidate_step(binding, uia, root, selector, stable, proof, element):
    if stable is not None:
        current = lookup.resolve(binding, uia, stable, root=root)
    else:
        current, _, _ = semantic_match(binding, uia, root, selector)
    need(uia.same(element, current), 'target_changed')
    if proof:
        if selector['role'] not in ('combobox', 'textfield') and proof.get('name'):
            need(current.property(30005) == proof['name'], 'command_name_changed')
        if proof.get('semantic_label'):
            app.tasks.check_display({'label': proof['semantic_label']}, app.field_label(binding, uia, current, root))
    return current


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
        if step.get('action') == 'context-menu':
            need(not set(step) - {'action', 'spec', 'label'}, 'unknown_step_field')
            need(text(step.get('label', 'step'), 200), 'invalid_step_label')
            context_menu.validate_spec(step.get('spec'))
            continue
        if step.get('action') == 'verify-file':
            need(not set(step) - {'action', 'output', 'timeout', 'label'}, 'unknown_step_field')
            need(text(step.get('label', 'step'), 200), 'invalid_step_label')
            export_evidence.validate(step.get('output'))
            timeout = step.get('timeout', 26)
            need(type(timeout) in (int, float) and 0 <= timeout <= 120, 'invalid_file_timeout')
            continue
        need(not set(step) - {'action', 'selector', 'value', 'parameter', 'label', 'command_name', 'field_label', 'popup'}, 'unknown_step_field')
        need(step.get('action') in ('set-value', 'invoke', 'submit'), 'unsupported_workflow_action')
        if 'popup' in step:
            popup = step['popup']
            need(isinstance(popup, dict) and not set(popup) - {'title', 'timeout'}
                 and text(popup.get('title')) and popup['title'] == popup['title'].strip(),
                 'invalid_popup_scope')
            timeout = popup.get('timeout', 3)
            need(type(timeout) in (int, float) and 0 <= timeout <= 10, 'invalid_popup_timeout')
        selector = step.get('selector')
        validate_selector(selector)
        need(text(step.get('label', 'step'), 200), 'invalid_step_label')
        need('command_name' not in step or (step['action'] == 'invoke' and text(step['command_name'])),
             'invalid_command_name')
        need('field_label' not in step or (step['action'] == 'set-value' and text(step['field_label'], 200)),
             'invalid_field_label')
        if step['action'] == 'set-value':
            need(selector['role'] in ('combobox', 'textfield'), 'value_field_required')
            need(('value' in step) != ('parameter' in step), 'fixed_value_or_parameter_required')
            if 'value' in step:
                need(app.tasks.valid_value(step['value'], selector['role']),
                     'invalid_text_value' if selector['role'] == 'textfield' else 'invalid_combo_value')
            else:
                need(text(step['parameter'], 100) and step['parameter'] == step['parameter'].strip()
                     and '=' not in step['parameter'], 'invalid_parameter')
        elif step['action'] == 'submit':
            need(selector['role'] in ('combobox', 'textfield')
                 and 'value' not in step and 'parameter' not in step, 'invalid_submit')
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
    need(len(values) == len(overrides) and all(app.tasks.valid_value(v, 'textfield') for v in values.values()),
         'invalid_override')
    need(not set(values) - parameters.keys(), 'unknown_parameter')
    for step in recipe['steps']:
        key = step.get('parameter', '').casefold()
        if key in values:
            need(app.tasks.valid_value(values[key], step['selector']['role']), 'invalid_override')
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


def popup_surface(owner, title, spec, *, read_only=False):
    """Wait by observing only; never restore, focus or act on the owner as a fallback."""
    deadline = time.monotonic() + (0 if read_only else spec.get('timeout', 3))
    while True:
        app.guard(owner, owner, title)
        matches = [row for row in app.owned_popups(owner) if row['title'] == spec['title']
                   and app.bridge.user32.IsWindowVisible(row['hwnd'])]
        need(len(matches) <= 1, 'ambiguous_popup')
        if matches:
            surface = app.bind_surface(owner, matches[0]['hwnd'])
            need(surface.pid == owner.pid, 'popup_owner_mismatch')
            check_surface(owner, surface, title, spec)
            return surface
        if read_only:
            return None
        remaining = deadline - time.monotonic()
        need(remaining > 0, 'popup_not_found')
        time.sleep(min(.1, remaining))


def check_surface(owner, surface, title, popup=None, *, mutate=False):
    app.guard(owner, surface, title, mutate=mutate)
    if popup is not None:
        need(surface.pid == owner.pid and surface.hwnd != owner.hwnd, 'popup_owner_mismatch')
        need(app.bridge.window_text(surface.hwnd) == popup['title'], 'popup_title_changed')
        matches = [row for row in app.owned_popups(owner) if row['title'] == popup['title']
                   and app.bridge.user32.IsWindowVisible(row['hwnd'])]
        need(len(matches) == 1 and matches[0]['hwnd'] == surface.hwnd, 'popup_scope_changed')


def run(recipe, overrides=None, *, read_only=False, hwnd=None, allow_recorded_selection=False):
    recipe = validate(recipe)
    values, missing = values_for(recipe, overrides or {})
    if missing and not read_only:
        return dict(status='needs_input', missing_parameters=missing, steps=[], input_dispatched=False)
    report = dict(status='observed' if read_only else 'verified', steps=[], input_dispatched=False,
                  business_commit='not_verified')
    started = time.monotonic()
    number, dispatching = 0, False
    anchor_proof = None
    stage_started = started
    try:
        binding = bind(recipe['exe'], recipe['window'], hwnd)
        # Capture every requested output before window restoration or any input.
        baselines = {i: export_evidence.baseline(step['output']) for i, step in enumerate(recipe['steps'], 1)
                     if step['action'] == 'verify-file'} if not read_only else {}
        report['window_restored'] = False
        owner_prepared = False
        with app.bridge.UIA() as uia:
            for number, step in enumerate(recipe['steps'], 1):
                stage_started = time.monotonic()
                app.guard(binding, binding, recipe['window'])
                # A popup may disable its owner. Only actions on that owner
                # require it to be enabled/restored; missing popups never do.
                if (not read_only and not owner_prepared and 'popup' not in step
                        and step['action'] in ('set-value', 'invoke', 'submit', 'context-menu')):
                    report['window_restored'] = app.visibility.restore_for_action(binding, recipe['window'])
                    owner_prepared = True
                if step['action'] == 'wait':
                    if read_only:
                        continue
                    wait(binding, recipe['window'], step['seconds'])
                    receipt = dict(status='waited', seconds=step['seconds'], input_dispatched=False,
                                   business_result='not_verified')
                elif step['action'] == 'verify-file':
                    if read_only:
                        export_evidence.inventory(step['output'])
                        receipt = dict(status='observed', input_dispatched=False,
                                       output_validation='path_observed', file_verification='pending_execution')
                    else:
                        ok, diagnostic = export_evidence.wait(step['output'], baselines[number],
                                                              timeout=step.get('timeout', 26))
                        app.guard(binding, binding, recipe['window'])
                        receipt = dict(status='verified' if ok else 'blocked', input_dispatched=False,
                                       diagnostic=diagnostic, file_verified=ok,
                                       baseline_scope='before_workflow', business_commit='not_verified')
                elif step['action'] == 'context-menu':
                    check_surface(binding, binding, recipe['window'], mutate=not read_only)
                    if read_only:
                        context_menu._region(binding, uia, step['spec']['target'])
                        receipt = dict(status='observed', input_dispatched=False,
                                       region_observed=True, menu_verification='pending_execution')
                    else:
                        def context_guard():
                            check_surface(binding, binding, recipe['window'], mutate=True)
                        dispatching = True
                        receipt = context_menu.execute(binding, step['spec'], context_guard)
                        dispatching = False
                else:
                    lookup_started = time.monotonic()
                    popup = step.get('popup')
                    surface = popup_surface(binding, recipe['window'], popup, read_only=read_only) if popup else binding
                    if surface is None:
                        pending = dict(number=number, popup=deepcopy(popup), diagnostic='popup_not_open')
                        report.setdefault('pending_scopes', []).append(pending)
                        receipt = dict(status='deferred', input_dispatched=False, scope_validation='pending',
                                       diagnostic='popup_not_open', popup=deepcopy(popup))
                        report['steps'].append(dict(number=number, label=step.get('label', ''), receipt=receipt,
                                                    elapsed_seconds=round(time.monotonic() - stage_started, 4)))
                        continue
                    check_surface(binding, surface, recipe['window'], popup, mutate=not read_only)
                    root = uia.from_handle(surface.hwnd)
                    selector = step['selector']
                    anchor_proof = None
                    if not read_only and 'anchor' in selector:
                        # Point-based identity needs the actual target surface
                        # exposed. Ordinary UIA actions remain background-capable.
                        from row_action import foreground
                        foreground(surface)
                        check_surface(binding, surface, recipe['window'], popup, mutate=True)
                        anchor_proof = lookup.bind_anchored(surface, uia, selector, root=root)
                        element, stable, resolution = anchor_proof.element, None, None
                    else:
                        element, stable, resolution = resolve_step(surface, uia, root, selector)
                    need('command_name' not in step or element.property(30005) == step['command_name'],
                         'command_name_changed')
                    if step.get('field_label'):
                        app.tasks.check_display({'label': step['field_label']},
                                                app.field_label(surface, uia, element, root))
                    lookup_elapsed = round(time.monotonic() - lookup_started, 4)
                    if read_only:
                        receipt = dict(status='observed', input_dispatched=False, actions=uia.actions(element))
                        if resolution is not None:
                            receipt['target_resolution'] = resolution
                            if stable is not None:
                                receipt['resolved_selector'] = stable
                        if step['action'] == 'set-value':
                            reader = live.text_readback if selector['role'] == 'textfield' else live.combo_readback
                            receipt.update(reader(surface, uia, element))
                        elif step['action'] == 'submit':
                            live.submit_ready(surface, uia, element, {'role': selector['role']})
                            receipt['submit_validation'] = 'editable_field_observed'
                        else:
                            receipt['command_name'] = element.property(30005) or ''
                    else:
                        def check():
                            check_surface(binding, surface, recipe['window'], popup, mutate=True)
                            current = (anchor_proof.revalidate() if anchor_proof is not None else
                                       revalidate_step(surface, uia, root, selector, stable, resolution, element))
                            need('command_name' not in step or current.property(30005) == step['command_name'],
                                 'command_name_changed')
                            if step.get('field_label'):
                                app.tasks.check_display({'label': step['field_label']},
                                                        app.field_label(surface, uia, current, root))
                            if step['action'] != 'set-value' or selector['role'] == 'textfield' or not allow_recorded_selection:
                                capability = {'set-value': 'SetValue', 'invoke': 'Click', 'submit': 'SetFocus'}[step['action']]
                                need(capability in uia.actions(current),
                                     'workflow_pattern_unavailable')
                        value = values[step['parameter'].casefold()] if 'parameter' in step else step.get('value')
                        dispatching = True
                        operation = ('fill' if selector['role'] == 'textfield' else 'select') if step['action'] == 'set-value' else step['action']
                        try:
                            receipt = live.execute(surface, uia, element, {'role': selector['role']}, operation,
                                                   value, still_valid=check)
                        finally:
                            if anchor_proof is not None:
                                anchor_proof.close()
                                anchor_proof = None
                        dispatching = False
                        if resolution is not None:
                            receipt['target_resolution'] = resolution
                    receipt['lookup_seconds'] = lookup_elapsed
                    if popup:
                        receipt['popup'] = dict(title=popup['title'])
                report['steps'].append(dict(number=number, label=step.get('label', ''), receipt=receipt,
                                            elapsed_seconds=round(time.monotonic() - stage_started, 4)))
                report['input_dispatched'] = combine(report['input_dispatched'], receipt['input_dispatched'])
                if receipt.get('physical_input'):
                    report['physical_input'] = True
                if read_only or (step['action'] not in ('context-menu', 'submit') and receipt['status'] in ('verified', 'waited')):
                    continue
                delivered = (step['action'] == 'invoke' and receipt['status'] == 'delivered_unverified'
                             and receipt['input_dispatched'] is True
                             and receipt.get('diagnostic') == 'business_result_not_verified')
                delivered_menu = (step['action'] == 'context-menu' and receipt['status'] == 'delivered_unverified'
                                  and receipt['input_dispatched'] is True and receipt.get('leaf_dispatched') is True
                                  and receipt.get('menu_path_completed') is True
                                  and receipt.get('diagnostic') == 'menu_leaf_sent')
                delivered_submit = (step['action'] == 'submit' and receipt['status'] == 'delivered_unverified'
                                    and receipt['input_dispatched'] is True and receipt.get('keyboard_events_sent') == 2
                                    and receipt.get('diagnostic') == 'submit_enter_sent')
                if delivered or delivered_menu or delivered_submit:
                    report['status'] = 'completed_unverified'
                else:
                    report.update(status=receipt['status'], stopped_at=number,
                                  diagnostic=receipt.get('diagnostic', 'step_not_verified'))
                    if step['action'] in ('context-menu', 'submit') and receipt['status'] in ('verified', 'waited'):
                        report.update(status='blocked', diagnostic='submission_completion_unproven')
                    break
    except Exception as error:
        diagnostic = getattr(error, 'code', 'provider_error')
        failure = dict(status='uncertain' if dispatching else 'blocked',
                       input_dispatched=None if dispatching else False, diagnostic=diagnostic)
        if number and len(report['steps']) < number:
            report['steps'].append(dict(number=number, label=step.get('label', ''), receipt=failure,
                                        elapsed_seconds=round(time.monotonic() - stage_started, 4)))
            report['stopped_at'] = number
        report['input_dispatched'] = combine(report['input_dispatched'], failure['input_dispatched'])
        report.update(status=failure['status'], diagnostic=diagnostic)
    finally:
        if anchor_proof is not None:
            anchor_proof.close()
    report['elapsed_seconds'] = round(time.monotonic() - started, 4)
    return report


def discover(exe, title, hwnd=None):
    """Only form controls, with nearby labels; never enumerate option inventories."""
    binding = bind(exe, title, hwnd)
    found = []
    with app.bridge.UIA() as uia:
        root = uia.from_handle(binding.hwnd)
        for element in lookup.find_all(uia, root, roles=('combobox', 'textfield', 'button', 'splitbutton'), limit=256):
            if element.property(30003) == 50004:
                try:
                    live.textfield_scope(binding, uia, element)
                except app.bridge.BridgeError as error:
                    if error.code == 'textfield_requires_combo':
                        continue
                    raise
            identifier = element.property(30011)
            name = element.property(30005) or ''
            role = app.bridge.ROLES[element.property(30003)]
            if text(identifier) and not identifier.isdecimal():
                selector = dict(automation_id=identifier, role=role)
            elif text(name):
                selector = dict(name=name, role=role)
            else:
                continue
            item = dict(selector, name=name, actions=uia.actions(element), selector=selector)
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
    raise SystemExit("Install Desktop Select 0.4.8 or later beside this skill.")
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
    rows = {row['number']: row for row in observed['steps']}
    for number, step in enumerate(recipe['steps'], 1):
        if step['action'] in ('wait', 'context-menu', 'verify-file'):
            continue
        row = rows[number]
        if row['receipt']['status'] == 'deferred':
            need('popup' in step, 'invalid_deferred_scope')
            continue
        capability = {'set-value': 'SetValue', 'invoke': 'Click', 'submit': 'SetFocus'}[step['action']]
        need(capability in row['receipt']['actions'],
             'workflow_pattern_unavailable')
        if row['receipt'].get('resolved_selector'):
            step['selector'] = row['receipt']['resolved_selector']
        resolution = row['receipt'].get('target_resolution', {})
        if step['action'] == 'set-value' and resolution.get('semantic_label'):
            step['field_label'] = resolution['semantic_label']
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
             '콤보 선택값과 입력란 문자열을 필수 변수로 받을 수 있다. '
             '예를 들어 `--set "Family=Hybrid" --set "검색어=00123"`처럼 이번 값을 전달한다. '
             '입력란을 비우라는 요청은 해당 변수에 `--set "검색어="`를 준다. 콤보에는 빈 값을 쓰지 않는다. '
             '값 확인 실패 또는 불확실한 전달이면 다음 단계로 진행하지 않는다. '
             '조회/내보내기 버튼 전달과 대기는 업무 완료나 파일 생성의 증거가 아니다. 재클릭하지 않는다. '
             '동일 폴더 아래 `desktop-select`가 필요하며 외부 통신과 CUA 드라이버는 사용하지 않는다.\n')
    if source_task is not None:
        guide += '\n녹화한 위치와 자연어 지시 원문은 `source-task.json`에 보관되어 있다. 실행에는 `workflow.json`을 사용한다.\n'
    if observed.get('pending_scopes'):
        guide += ('\n등록 시 아직 열리지 않은 팝업의 대상은 미확인 상태로 보존했다. '
                  '실행할 때 지정된 제목의 소속 팝업이 하나로 확인된 뒤에만 해당 단계에 입력한다. '
                  '팝업이 없거나 모호하면 멈추며 원래 창에 대신 입력하지 않는다.\n')
    if any(step['action'] == 'verify-file' for step in active):
        guide += ('\n파일 확인은 모든 입력 전 기준 상태와 비교하여 새로 생기거나 변경된 로컬 통합 문서를 검사한다. '
                  '파일 확인 실패 후 내보내기를 다시 클릭하지 않는다.\n')
    if any(step['action'] == 'submit' for step in active):
        guide += ('\nsubmit 단계는 지정한 입력란과 실제 키보드 포커스를 확인한 뒤 Enter를 한 번 보낸다. '
                  '실제 창을 앞으로 가져오며 실행 중 마우스와 키보드를 사용하지 않는다. '
                  '키 전달은 조회 완료의 증거가 아니며 불확실하면 반복하지 않는다.\n')
    if any(step['action'] == 'context-menu' for step in active):
        guide += ('\ncontext-menu 단계는 대상 창을 앞으로 가져오고 실제 마우스로 표를 우클릭한 뒤 메뉴를 선택한다. '
                  '해당 단계가 끝날 때까지 마우스와 키보드를 사용하지 않는다.\n')
    if any('anchor' in step.get('selector', {}) for step in active):
        guide += ('\n상위 영역과 상대 위치로 찾는 입력란은 실행할 때 대상 창을 앞으로 가져와 확인한다. '
                  '대상을 찾는 동안 마우스와 키보드를 사용하지 않는다. '
                  '같은 종류 입력란의 배치가 바뀌면 역할과 클래스 검사만으로 구별할 수 없으므로 지정 위치를 다시 확인한다. '
                  '등록과 --read-only는 창을 앞으로 가져오지 않으므로 사용자가 해당 영역을 보이게 해 두어야 한다.\n')
    (destination / 'SKILL.md').write_text(guide, encoding='utf8')
    result = dict(status='registered', path=str(destination), required_parameters=required, input_dispatched=False)
    if observed.get('pending_scopes'):
        result.update(target_validation='pending_scopes', pending_scopes=observed['pending_scopes'])
    return result


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
    launched, executing = False, False
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
        overrides = {}
        if args.command == 'run':
            overrides = app.tasks.parse_overrides(args.set)
            _, missing = values_for(recipe, overrides)
            if missing and not args.read_only:
                print(json.dumps(dict(status='needs_input', missing_parameters=missing,
                                      steps=[], input_dispatched=False), ensure_ascii=False))
                return 2
        if args._worker:
            if args.command == 'discover':
                result = discover(args.exe, args.window, args.hwnd)
            elif args.command == 'register':
                result = register(recipe, args.out, args.hwnd, source_task=source_task)
            else:
                executing = mutating
                result = run(recipe, overrides, read_only=args.read_only, hwnd=args.hwnd)
        else:
            timeout = 90 + sum(s.get('seconds', 0) + s.get('popup', {}).get('timeout', 3 if 'popup' in s else 0)
                               + (s.get('timeout', 26) if s['action'] == 'verify-file' else 0)
                               + (3 * len(s['spec']['path']) + 3 if s['action'] == 'context-menu' else 0)
                               for s in recipe['steps']) if recipe else 90
            launched = True
            child = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(Path(__file__).resolve()),
                                    '--_worker', *argv], capture_output=True, encoding='utf8', timeout=timeout)
            result = json.loads(child.stdout)
            need(child.returncode in (0, 2, 3) and isinstance(result, dict) and 'status' in result,
                 'invalid_worker_receipt')
    except (Exception, KeyboardInterrupt) as error:
        uncertain = (launched or executing) and mutating
        result = dict(status='uncertain' if uncertain else 'blocked', input_dispatched=None if uncertain else False,
                      diagnostic=getattr(error, 'code', 'workflow_unavailable'))
    print(json.dumps(result, ensure_ascii=False))
    return (0 if result['status'] in ('registered', 'observed', 'verified') else
            3 if result['status'] in ('completed_unverified', 'uncertain', 'delivered_unverified') else 2)


if __name__ == '__main__':
    raise SystemExit(main())
