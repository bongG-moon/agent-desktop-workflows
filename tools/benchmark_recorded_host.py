"""Real Claude Code first-run/repair benchmark using a synthetic saved recording.

The harness creates recording-schema fixtures from observed native identities,
not from a human demonstration. It never runs the saved workflow itself. Once
the model starts, all field changes, queries and exports must come from public
CLI commands selected by the model. The private fixture channel only prepares
the obstruction, reads state and closes the synthetic window.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

import benchmark_host_workflow as host
from smoke_desktop_context import compile_fixture, decoded_cells
from smoke_native import Fixture


ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = 'Synthetic saved device report'
EFFECTIVE = 'Synthetic saved device report execution'
REGISTERED = 'synthetic-device-report'


def prepare(root, source):
    project, skill = host.prepare(root, source)
    exe = root / 'DesktopContextFixture.exe'
    if not exe.is_file():
        compile_fixture(root)
    return project, skill, exe


def seed_recording(root, project, skill, fixture, book):
    """Observe the fixture, then seed a clearly identified synthetic recording."""
    sys.path.insert(0, str(skill / 'scripts'))
    import context_menu
    from context_teach import raw_result_step
    import desktop_select as app
    import learned_tasks as tasks
    import recorded_plan
    from row_action import foreground

    app.bridge.set_dpi_awareness()
    window = fixture.ready
    binding = app.bridge.Binding(app.pick_window(app.windows('DesktopContextFixture.exe'),
                                               window['title'], window['hwnd']))
    assert binding.pid == fixture.proc.pid
    foreground(binding)
    state = fixture.command('state')
    with app.bridge.UIA() as uia:
        nodes, elements = app.observe(binding, uia, identities_only=True)

        def observed(identity):
            node, = [node for node in nodes if node.get('automation_id') == identity]
            return {'role': node['role'], 'automation_id': node['automation_id']}

        def combo(identity, label, instruction):
            raw = {'action': 'teach-target', 'field_label': label,
                   'target': {'kind': 'element', 'locator': observed(identity)},
                   'recognition': {'target': 'identified', 'action': 'requires_instruction',
                                   'result': 'manual', 'reason': 'combo_choice_unproven'}}
            raw['instruction'] = tasks.compile_instruction(raw, instruction)
            return raw

        steps = [combo('conditionState', 'MES Setup', 'MES SETUP조건을 N으로 해줘'),
                 combo('conditionFamily', 'Family', '입력받은 변수를 FAMILY조건에 입력해줘')]
        region = observed('ResultSurface')
        index, = [i for i, node in enumerate(nodes) if node.get('automation_id') == 'ResultSurface']
        assert region['role'] == 'group' and not list(uia.children(elements[index]))
        trigger = observed('QueryCommand')
        ready = {'kind': 'text', 'target': observed('ResultIndicator'), 'expected': 'RESULT READY'}
    x, y, width, height = state['grid_bounds']
    menu = context_menu.teach_at(binding, (x + width // 7, y + height // 2),
                                 ['Export to excel', 'Active sheet'],
                                 target=region['automation_id'], role=region['role'])
    steps.append(raw_result_step('Search and export current results', menu, trigger, ready,
                                 {'kind': 'file', 'path': str(book)}))
    directory = project / '.desktop-select/tasks'
    original = tasks.from_recording(ORIGINAL, 'DesktopContextFixture.exe', window['title'], steps)
    tasks.save_task(original, directory)
    effective = deepcopy(original)
    effective['name'] = EFFECTIVE
    tasks.save_task(effective, directory)
    execution_plan = recorded_plan.save_plan(effective, directory)
    assert execution_plan['status'] == 'saved', execution_plan
    receipt = dict(
        status='recorded', input_dispatched=False, task=tasks.summary(effective),
        execution_plan=app.execution_plan_info(execution_plan))
    if (skill / 'scripts/recording_followup.py').is_file():
        import recording_followup
        receipt = recording_followup.followup(receipt)
        assert receipt.get('preparation') == 'ready' and receipt.get('effective_task') == EFFECTIVE
    host.save(root / 'synthetic-saved-receipt.json', receipt)
    original_path = directory / (ORIGINAL + '.json')
    before = fixture.command('state')
    assert before['writes'] == before['queries'] == before['right_clicks'] == before['leaves'] == 0
    fixture.command('cover-recorded-point')
    fixture.command('trace')
    after = fixture.command('state')
    assert after['writes'] == after['queries'] == after['right_clicks'] == after['leaves'] == 0
    info = dict(syntheticRecording=True, humanDemonstration=False,
                origin='native UIA identities and public recording schema',
                originalTask=ORIGINAL, effectiveTask=EFFECTIVE,
                originalPath=str(original_path), originalSha256=host.digest(original_path),
                originalMenu=menu, preparedReceipt=receipt, initialState=after,
                fixturePid=fixture.proc.pid, fixtureWindow=window['title'], output=str(book),
                inputDispatchedDuringPreparation=False,
                obstruction='separate sibling panel covers the initially valid recorded point')
    host.save(root / 'seed.json', info)
    return info


def request(seed):
    return (
        f'저장한 작업 "{ORIGINAL}"을 현재 열린 "{seed["fixtureWindow"]}" 화면에서 첫 시험하고, '
        '현재 화면에 맞지 않는 위치는 화면을 보고 보정한 뒤 이 프로젝트의 업무 스킬로 등록해줘. '
        f'스킬 폴더 이름은 {REGISTERED}로 해줘. Family는 이번 입력값 Hybrid로 실행하고 '
        'MES Setup은 녹화에 저장된 고정값 N을 유지해. '
        '원본 녹화는 그대로 남겨줘. 이 합성 시험 창에서 조회와 결과 표 우클릭, '
        'Export to excel > Active sheet 내보내기, 필요한 마우스 조작과 보정용 화면 캡처까지 허용해. '
        '조회부터 중복 실행하지 말고 결과를 확인해줘. '
        '이 녹화는 테스트 하네스가 생성한 합성 녹화이며 실제 사람의 시연 기록은 아니야.'
    )


def verified_output(receipt):
    """Trials retain step receipts; registered summary runs also aggregate outputs."""
    return receipt.get('status') == 'verified' and bool(receipt.get('verified_outputs') or any(
        row.get('receipt', {}).get('status') == 'verified'
        and row['receipt'].get('workbook_verified') is True
        for row in receipt.get('steps', [])))


def permission_validator(root, project, skill):
    """Approve individual public CLI calls, never shell-wide permission rules."""
    registered = project / '.claude/skills' / REGISTERED

    def path(value, directory=None):
        # Bash may use /c/Users/... while PowerShell uses C:\\Users\\....
        value = value.replace('\\', '/')
        if re.match(r'^/[A-Za-z]/', value):
            value = value[1] + ':' + value[2:]
        result = Path(value)
        return (result if result.is_absolute() else (directory or project) / result).resolve()

    def receipt_grants():
        """Only returned receipt arguments authorize continuation, never arbitrary IDs."""
        found = dict(images=set(), tickets=set(), frame_pairs=set(), frames={}, tasks={ORIGINAL, EFFECTIVE},
                     frameless=set())
        def walk(value):
            if isinstance(value, dict):
                if isinstance(value.get('effective_task'), str):
                    found['tasks'].add(value['effective_task'])
                if isinstance(value.get('image'), str):
                    found['images'].add(path(value['image']))
                token = value.get('frame')
                if (isinstance(token, str) and re.fullmatch('[a-f0-9]{64}', token)
                        and type(value.get('width')) is int and type(value.get('height')) is int):
                    found['frames'][token] = (value['width'], value['height'])
                for key in ('args', 'resume_args', 'frame_args'):
                    arguments = value.get(key)
                    if not isinstance(arguments, list) or not all(isinstance(v, str) for v in arguments):
                        continue
                    pairs = dict(zip(arguments, arguments[1:]))
                    command = arguments[0] if arguments and not arguments[0].startswith('--') else 'run'
                    for flag, length in (('--receipt', 64), ('--resume-menu', 48),
                                         ('--repair-frame', 48), ('--resume-input', 48)):
                        ticket = pairs.get(flag, '')
                        if re.fullmatch('[a-f0-9]{' + str(length) + '}', ticket):
                            found['tickets'].add((command, flag, ticket))
                            if re.fullmatch('[a-f0-9]{64}', pairs.get('--frame', '')):
                                found['frame_pairs'].add((command, flag, ticket, pairs['--frame']))
                            elif command == 'resume-menu-trial' and '--frame' not in arguments and '--point' not in arguments:
                                found['frameless'].add((command, ticket))
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        try:
            lines = (root / 'events.jsonl').read_text(encoding='utf-8').splitlines()
            for line in lines:
                event = json.loads(line)['event']
                if event.get('type') == 'user':
                    for block in host.tool_blocks(event):
                        if block.get('type') == 'tool_result':
                            for receipt in host.receipt_objects(block.get('content')):
                                walk(receipt)
        except (OSError, ValueError, KeyError):
            pass
        return found

    def validate(request):
        name, inputs = request.get('tool_name'), request.get('input', {})
        if name == 'Skill':
            return inputs.get('skill') in ('desktop-select', REGISTERED)
        if name == 'Read':
            value = inputs.get('file_path')
            if not isinstance(value, str):
                return False
            candidate = path(value)
            if candidate.is_relative_to(project.resolve()):
                return True
            return (candidate.parent == (Path(tempfile.gettempdir()) / 'desktop-select-grounding').resolve()
                    and re.fullmatch('[a-f0-9]{64}\\.png', candidate.name) is not None
                    and candidate in receipt_grants()['images'])
        if name not in ('PowerShell', 'Bash'):
            return False
        command = inputs.get('command')
        if not isinstance(command, str):
            return False
        # Hosts commonly restate the already selected cwd. Accept only an exact
        # project path followed by one validated Python call, never arbitrary cd.
        command_directory = project
        for directory in (project, skill, registered):
            spellings = [str(directory), directory.as_posix()]
            if directory.drive:
                spellings.append('/' + directory.drive[0].lower() + directory.as_posix()[2:])
            for spelling in spellings:
                for quote in ('"', "'"):
                    for lead in ('cd ', 'Set-Location ', 'Set-Location -LiteralPath '):
                        for separator in ('; ', ' && '):
                            prefix = lead + quote + spelling + quote + separator
                            if command.startswith(prefix):
                                command = command[len(prefix):]
                                command_directory = directory
        if command.startswith('& python ') or command.startswith('& python.exe '):
            command = command[2:]
        if any(char in command for char in '\r\n;|&<>`$(){}#'):
            return False
        matches = list(re.finditer(r'"([^"\r\n]*)"|\'([^\'\r\n]*)\'|([^\s"\']+)', command))
        if not matches:
            return False
        gaps = command[:matches[0].start()] + ''.join(
            command[match.end():matches[i + 1].start()] if i + 1 < len(matches) else command[match.end():]
            for i, match in enumerate(matches))
        if gaps.strip():
            return False
        tokens = [next(part for part in match.groups() if part is not None) for match in matches]
        if len(tokens) < 6 or tokens[0].lower() not in ('python', 'python.exe') or tokens[1:4] != ['-X', 'utf8', '-B']:
            return False
        script = path(tokens[4], command_directory)
        tail = tokens[5:]
        runners = {path(str(skill / 'scripts/python_workflow.py')): 'workflow',
                   path(str(skill / 'scripts/desktop_select.py')): 'desktop',
                   path(str(registered / 'scripts/run.py')): 'runner'}
        kind = runners.get(script)
        if kind is None:
            return False
        if tail == ['--help']:
            return True
        action = tail.pop(0) if kind != 'runner' else 'run'
        contracts = {
            ('desktop', 'show'): ({'--task'}, set(), {'--task'}),
            ('desktop', 'plan'): ({'--task'}, set(), {'--task'}),
            ('desktop', 'recover'): ({'--task'}, {'--prepare', '--restore-window'}, {'--task'}),
            ('desktop', 'try-recording'): ({'--task', '--set'},
                {'--allow-physical', '--background-only', '--auto-repair-frame'}, {'--task'}),
            ('desktop', 'inspect-trial'): ({'--receipt'}, set(), {'--receipt'}),
            ('desktop', 'inspect-menu-trial'): ({'--receipt', '--set'}, {'--allow-physical'},
                                               {'--receipt', '--allow-physical'}),
            ('desktop', 'resume-menu-trial'): ({'--receipt', '--frame', '--point', '--set'}, {'--allow-physical'},
                                              {'--receipt', '--allow-physical'}),
            ('desktop', 'resume-trial'): ({'--receipt', '--frame', '--point', '--row-policy'},
                                         {'--allow-physical'}, {'--receipt', '--frame', '--point', '--allow-physical'}),
            ('workflow', 'register'): ({'--task', '--out', '--set'},
                {'--summary', '--run', '--allow-physical', '--auto-repair-frame', '--background-only'}, {'--task', '--out'}),
            ('runner', 'run'): ({'--set', '--resume-menu', '--repair-frame', '--resume-input', '--frame', '--point'},
                {'--summary', '--allow-physical', '--auto-repair-frame', '--read-only', '--input-plan', '--background-only'}, set()),
        }
        contract = contracts.get((kind, action))
        if contract is None:
            return False
        valued, flags, required = contract
        parsed = {}
        while tail:
            flag = tail.pop(0)
            if flag in parsed and flag != '--set':
                return False
            if flag in flags:
                parsed[flag] = True
                continue
            if flag not in valued:
                return False
            if flag == '--point':
                if len(tail) < 2 or any(not value.isdecimal() or int(value) > 10000 for value in tail[:2]):
                    return False
                parsed[flag] = tuple(map(int, tail[:2]))
                del tail[:2]
                continue
            if not tail:
                return False
            value = tail.pop(0)
            if flag == '--set':
                supplied = parsed.setdefault(flag, [])
                if value not in ('Family=Hybrid', 'MES Setup=N') or value in supplied:
                    return False
                supplied.append(value)
                continue
            parsed[flag] = value
            if flag == '--task' and value in receipt_grants()['tasks']:
                continue
            if flag == '--out' and path(value, command_directory) == registered.resolve():
                continue
            if flag in ('--resume-menu', '--repair-frame', '--resume-input', '--receipt'):
                continue
            if flag == '--frame':
                continue
            if flag == '--row-policy' and value in ('single', 'first'):
                continue
            return False
        if not required <= parsed.keys() or ('--allow-physical' in parsed and '--background-only' in parsed):
            return False
        continuing = [flag for flag in ('--resume-menu', '--repair-frame', '--resume-input', '--receipt') if flag in parsed]
        if len(continuing) > 1:
            return False
        grants = receipt_grants() if continuing else None
        for flag in continuing:
            if (action, flag, parsed[flag]) not in grants['tickets']:
                return False
        if '--frame' in parsed or '--point' in parsed:
            if not continuing or not {'--frame', '--point'} <= parsed.keys():
                return False
            frame = parsed['--frame']
            if (action, continuing[0], parsed[continuing[0]], frame) not in grants['frame_pairs']:
                return False
            width, height = grants['frames'].get(frame, (0, 0))
            if not (0 <= parsed['--point'][0] < width and 0 <= parsed['--point'][1] < height):
                return False
        if '--resume-menu' in parsed and not {'--frame', '--point', '--allow-physical'} <= parsed.keys():
            return False
        if action == 'resume-menu-trial' and '--frame' not in parsed and (action, parsed['--receipt']) not in grants['frameless']:
            return False
        return True
    return validate


def observe_model(root, fixture, project, skill, args, prompt):
    """Time actual model tools and independently observe native fixture effects."""
    root.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    states, errors = [], []
    started = time.perf_counter()

    def poll():
        with (root / 'fixture-observations.jsonl').open('w', encoding='utf-8') as output:
            while not stop.is_set():
                try:
                    state = fixture.command('state')
                    row = dict(elapsedSeconds=round(time.perf_counter() - started, 6), state=state)
                    if not states or row['state'] != states[-1]['state']:
                        states.append(row)
                        output.write(json.dumps(row, ensure_ascii=False) + '\n')
                        output.flush()
                except Exception as error:
                    errors.append(type(error).__name__ + ': ' + str(error))
                    break
                stop.wait(.05)

    thread = threading.Thread(target=poll, daemon=True)
    thread.start()
    run_args = SimpleNamespace(**vars(args))
    run_args.prompt = prompt
    run_args.app = 'Synthetic device report'
    run_args.mode = 'workflow'
    run_args.permission_validator = permission_validator(root, project, skill)
    try:
        status = host.run(root, project, skill, run_args)
    finally:
        stop.set()
        thread.join(timeout=12)
    summary = json.loads((root / 'summary.json').read_text(encoding='utf-8'))
    first = next((row for row in states if row['state']['writes'] > states[0]['state']['writes']
                  or row['state']['queries'] > states[0]['state']['queries']), None)
    receipts = summary.get('receipts', [])
    repair = next((row for row in receipts if row['receipt'].get('menu_repair')), None)
    completed = next((row for row in receipts if row['receipt'].get('menu_corrections')), None)
    verified = next((row for row in receipts if verified_output(row['receipt'])), None)
    trial = next((row for row in receipts if row['receipt'].get('trial_receipt')), None)
    registration = next((row for row in receipts if row['receipt'].get('status') == 'registered'), None)
    poll_lead = summary['cliStartedMonotonic'] - started
    first_mutation = round(first['elapsedSeconds'] - poll_lead, 6) if first else None
    prompt_sent = summary.get('promptSentSeconds')
    calls = summary.get('toolCalls', [])
    hook_calls = [call for call in calls if 'Invoke-CompanyAgent.ps1' in str(call.get('input', {}).get('command', ''))]
    public_calls = [call for call in calls if call.get('tool') in ('PowerShell', 'Bash')
                    and any(script in str(call.get('input', {}).get('command', ''))
                            for script in ('desktop_select.py', 'python_workflow.py', 'scripts/run.py', 'scripts\\run.py'))]
    phases = dict(driverStatus=status, actualModelToolCount=len(summary.get('toolCalls', [])),
                  workflowModelToolCount=len(calls) - len(hook_calls), publicCliToolCount=len(public_calls),
                  publicCliPreToolBlockedCount=sum(call.get('blockedByPreToolHook', False) for call in public_calls),
                  inheritedHookToolCount=len(hook_calls),
                  imageReadCount=sum(call.get('tool') == 'Read' and str(
                      call.get('input', {}).get('file_path', '')).lower().endswith('.png') for call in calls),
                  deniedToolCount=sum(not decision['allowed'] for decision in summary.get('permissionDecisions', [])),
                  firstToolSeconds=next((call['startedSeconds'] for call in summary.get('toolCalls', [])), None),
                  firstPublicCliSeconds=public_calls[0]['startedSeconds'] if public_calls else None,
                  promptSentSeconds=prompt_sent,
                  fixturePollingLeadSeconds=round(poll_lead, 6),
                  firstObservedMutationSeconds=first_mutation,
                  firstObservedMutationFromPromptSeconds=(round(first_mutation - prompt_sent, 6)
                      if prompt_sent is not None and first_mutation is not None else None),
                  repairResponseSeconds=repair['elapsedSeconds'] if repair else None,
                  correctionResponseSeconds=completed['elapsedSeconds'] if completed else None,
                  firstTrialResponseSeconds=trial['elapsedSeconds'] if trial else None,
                  firstTrialPreparationSeconds=trial['receipt'].get('preparation_seconds') if trial else None,
                  firstTrialExecutionSeconds=trial['receipt'].get('execution_seconds') if trial else None,
                  registrationResponseSeconds=registration['elapsedSeconds'] if registration else None,
                  verifiedOutputResponseSeconds=verified['elapsedSeconds'] if verified else None,
                  verifiedOutputFromPromptSeconds=(round(verified['elapsedSeconds'] - prompt_sent, 6)
                      if prompt_sent is not None and verified else None),
                  finalSeconds=summary.get('finalSeconds'), durationApiMs=summary.get('durationApiMs'),
                  observationErrors=errors, fixturePollingIntervalSeconds=.05,
                  timingCaveat='Phase timings use the CLI launch origin. Fixture observations are polled every 50 ms.')
    host.save(root / 'phases.json', phases)
    return summary, phases


def verify(root, project, seed, fixture, book, expected_queries):
    state = fixture.command('state')
    recipe_path = project / '.claude/skills' / REGISTERED / 'workflow.json'
    recipe = json.loads(recipe_path.read_text(encoding='utf-8')) if recipe_path.is_file() else None
    menu = next((step['context']['menu'] for step in recipe['steps']
                 if step['action'] == 'result-menu'), None) if recipe else None
    cells = decoded_cells(book) if book.is_file() else None
    checks = dict(
        registeredSkill=bool(recipe and (recipe_path.parent / 'scripts/run.py').is_file()
                             and (recipe_path.parent / 'SKILL.md').is_file()),
        workbookCells=bool(cells and [cells.get(cell) for cell in ('A2', 'B2', 'C2')]
                           == ['N', 'Hybrid', str(expected_queries)]),
        singleQueryPerRequestedRun=state['queries'] == expected_queries,
        oneMenuPerRequestedRun=all(state[key] == expected_queries
                                  for key in ('right_clicks', 'menus', 'parents', 'leaves')),
        noWrongPointOrUnrelatedAction=state['wrong_point'] == state['unrelated'] == 0,
        fixedAndRuntimeConditions=(state['status'], state['family'], state['result_status'], state['result_family'])
                                  == ('N', 'Hybrid', 'N', 'Hybrid'),
        originalRecordingPreserved=host.digest(Path(seed['originalPath'])) == seed['originalSha256'],
        repairedPointPersisted=bool(menu and menu['point'] != seed['originalMenu']['point']
                                   and menu['target'] == seed['originalMenu']['target']
                                   and menu['path'] == seed['originalMenu']['path']))
    result = dict(passed=all(checks.values()), checks=checks, state=state, cells=cells,
                  registeredRecipe=str(recipe_path), savedMenu=menu, expectedQueries=expected_queries)
    # Keep the exact synthetic frames that the real model received, since the
    # runtime's short-lived frame store expires independently of this evidence.
    images = []
    summary_path = root / 'summary.json'
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding='utf-8'))
        for call in summary.get('toolCalls', []):
            if call.get('tool') != 'Read':
                continue
            source = Path(call.get('input', {}).get('file_path', ''))
            if (source.parent != Path(tempfile.gettempdir()) / 'desktop-select-grounding'
                    or not re.fullmatch('[a-f0-9]{64}\\.png', source.name) or not source.is_file()):
                continue
            target = root / ('model-frame-' + source.name)
            target.write_bytes(source.read_bytes())
            images.append(dict(source=str(source), evidence=str(target), sha256=host.digest(target)))
    result['modelFrameEvidence'] = images
    host.save(root / 'verification.json', result)
    return result


def run(root, project, skill, exe, args):
    if (root / 'seed.json').exists():
        raise ValueError('A saved-flow fixture was already seeded here; use a fresh run root.')
    book = project / 'report.xlsx'
    fixture = Fixture(exe, str(book), '--group', ready_timeout=25)
    try:
        seed = seed_recording(root, project, skill, fixture, book)
        summary, phases = observe_model(root, fixture, project, skill, args, request(seed))
        verification = verify(root, project, seed, fixture, book, 1)
        repeat = None
        if verification['passed'] and args.repeat:
            book.rename(root / 'verified-first.xlsx')
            repeat_root = root / 'repeat'
            repeat_prompt = (f'등록된 프로젝트 업무 스킬 {REGISTERED}를 Family=Hybrid로 한 번 실행해줘. '
                             'MES Setup=N은 저장된 고정값을 유지해. 같은 합성 창의 마우스 조작은 허용해. '
                             '완료된 출력만 확인해서 알려줘.')
            repeat_summary, repeat_phases = observe_model(repeat_root, fixture, project, skill, args, repeat_prompt)
            repeat = verify(repeat_root, project, seed, fixture, book, 2)
            repeat['noSecondPointRepair'] = not any(row['receipt'].get('menu_repair')
                or row['receipt'].get('menu_corrections') for row in repeat_summary.get('receipts', []))
            repeat['passed'] = repeat['passed'] and repeat['noSecondPointRepair']
            host.save(repeat_root / 'verification.json', repeat)
        result = dict(syntheticOnly=True, actualHumanRecording=False, actualClaudeCodeDecision=True,
                      model=summary.get('model'), originalSettingsHashesUnchanged=summary.get('originalSettingsHashesUnchanged'),
                      first=verification, phases=phases, repeat=repeat,
                      passed=verification['passed'] and (repeat is None or repeat['passed']))
        host.save(root / 'recorded-benchmark.json', result)
        print(json.dumps(dict(status='verified' if result['passed'] else 'failed',
                             report=str(root / 'recorded-benchmark.json'), phases=phases), ensure_ascii=False), flush=True)
        return 0 if result['passed'] else 2
    finally:
        fixture.close()


def repeat_existing(root, project, skill, exe, args):
    """Repeat an unchanged registration in a fresh synthetic application process."""
    seed = json.loads((root / 'seed.json').read_text(encoding='utf-8'))
    first = json.loads((root / 'verification.json').read_text(encoding='utf-8'))
    if not first['passed']:
        raise ValueError('Only a previously verified first run can be repeated.')
    book = Path(seed['output'])
    if book.exists():
        raise ValueError('The expected output already exists; preserve it before a new repeat.')
    if not re.fullmatch('repeat-[a-z0-9-]+', args.repeat_label):
        raise ValueError('Use a simple repeat-... evidence label.')
    repeat_root = root / args.repeat_label
    if repeat_root.exists():
        raise ValueError('This restarted repeat was already attempted.')
    fixture = Fixture(exe, str(book), '--group', ready_timeout=25)
    try:
        fixture.command('cover-recorded-point')
        fixture.command('trace')
        invocation = (f'/{REGISTERED} Family=Hybrid로 한 번 실행해줘. ' if args.repeat_slash
                      else f'등록된 프로젝트 업무 스킬 {REGISTERED}를 Family=Hybrid로 한 번 실행해줘. ')
        prompt = (invocation +
                  'MES Setup=N은 저장된 고정값을 유지해. 같은 합성 창의 마우스 조작은 허용해. '
                  '완료된 출력만 확인해서 알려줘.')
        summary, phases = observe_model(repeat_root, fixture, project, skill, args, prompt)
        result = verify(repeat_root, project, seed, fixture, book, 1)
        result['noSecondPointRepair'] = not any(row['receipt'].get('menu_repair')
            or row['receipt'].get('menu_corrections') for row in summary.get('receipts', []))
        result['passed'] = result['passed'] and result['noSecondPointRepair']
        result.update(separateModelProcess=True, separateFixtureProcess=True, phases=phases,
                      invocation='slash' if args.repeat_slash else 'natural_language')
        host.save(repeat_root / 'verification.json', result)
        print(json.dumps(dict(status='verified' if result['passed'] else 'failed',
                              report=str(repeat_root / 'verification.json'), phases=phases)), flush=True)
        return 0 if result['passed'] else 2
    finally:
        fixture.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--skill', required=True, type=Path)
    parser.add_argument('--claude')
    parser.add_argument('--model')
    parser.add_argument('--timeout', type=float, default=420)
    parser.add_argument('--budget', type=float, default=5)
    parser.add_argument('--repeat', action='store_true')
    parser.add_argument('--repeat-existing', action='store_true',
                        help='Use the verified registration in this root with a fresh fixture and model.')
    parser.add_argument('--repeat-label', default='repeat-restarted', help='New evidence folder for --repeat-existing.')
    parser.add_argument('--repeat-slash', action='store_true',
                        help='Start the restarted repeat request with the registered /skill name.')
    parser.add_argument('--run', action='store_true', help='Launch the fixture and send a real model request.')
    args = parser.parse_args()
    if args.repeat_slash and not args.repeat_existing:
        parser.error('--repeat-slash requires --repeat-existing.')
    if os.name != 'nt':
        parser.error('The native synthetic fixture requires Windows.')
    root = args.root.resolve()
    project, skill, exe = prepare(root, args.skill)
    if not args.run:
        print(json.dumps(dict(status='prepared', project=str(project), fixture=str(exe),
                              modelRequestsSent=0, windowsOpened=0)))
        return 0
    return repeat_existing(root, project, skill, exe, args) if args.repeat_existing else run(root, project, skill, exe, args)


if __name__ == '__main__':
    raise SystemExit(main())
