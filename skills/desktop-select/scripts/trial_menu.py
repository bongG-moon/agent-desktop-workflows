"""Use the normal single-use menu repair for a first trial, without a skill install."""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace

import learned_tasks as tasks
import recovery
import workflow_repair as repair


def _need(ok, reason):
    if not ok:
        raise tasks.TaskError(reason)


def _folder(app, key):
    _need(isinstance(key, str) and re.fullmatch(r'[0-9a-f]{64}', key), 'invalid_first_trial_receipt')
    # Keep paths below the existing receipt length on legacy Windows hosts.
    return tasks._directory(app.task_directory() / 'first-trials' / ('menu-' + key[:24]))


def _write(path, data):
    tasks._deny_redirect(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, mode='w', encoding='utf8') as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read(path):
    tasks._deny_redirect(path)
    with path.open('rb') as stream:
        data = stream.read(tasks.MAX_FILE_BYTES + 1)
    _need(len(data) <= tasks.MAX_FILE_BYTES, 'invalid_menu_trial')
    return json.loads(data.decode('utf8'), object_pairs_hook=tasks._unique_keys)


def prepare(app, key, document, effective_task, envelope):
    folder = _folder(app, key)
    state = dict(receipt=key, source_task=document['name'], source_hash=recovery._fingerprint(document),
        effective_task=effective_task, source=envelope['source'], recipe=envelope['recipe'],
        source_steps=envelope['source_steps'])
    if folder.exists():
        # _once retires only a proven zero-input desktop refusal. Reuse its
        # untouched preparation, never a corrected workflow or pending attempt.
        _need(_read(folder / 'source.json') == state
              and _read(folder / 'workflow.json') == envelope['recipe']
              and not (folder / 'last-attempt.json').exists()
              and not repair.RepairSession(folder / 'workflow.json').pending.exists(),
              'prepared_recording_changed')
        return
    folder.mkdir(parents=True, exist_ok=False)
    _write(folder / 'workflow.json', envelope['recipe'])
    _write(folder / 'source.json', state)


def _state(app, key):
    import python_workflow as flow
    folder = _folder(app, key)
    state = _read(folder / 'source.json')
    _need(state.get('receipt') == key, 'invalid_menu_trial')
    _need(recovery._fingerprint(tasks.read_document(state['source_task'], app.task_directory())) == state['source_hash'],
          'recording_draft_changed')
    recipe = flow.load(folder / 'workflow.json')
    original = deepcopy(recipe)
    _need(len(original['steps']) == len(state['recipe']['steps']), 'prepared_recording_changed')
    # Only persist() may replace a menu point/patch; all intended actions stay exact.
    for before, current in zip(state['recipe']['steps'], original['steps']):
        a, b = repair.menu(before), repair.menu(current)
        if a and b:
            for field in ('point', 'visual_patch'):
                if field in a:
                    b[field] = a[field]
                else:
                    b.pop(field, None)
    _need(original == state['recipe'], 'prepared_recording_changed')
    return folder, state, recipe


def prior(app, key):
    import first_trial
    folder = _folder(app, key)
    pointer = folder / 'last-attempt.json'
    if not pointer.exists():
        return None
    attempt = _read(pointer)
    _need(isinstance(attempt, str) and re.fullmatch(r'[0-9a-f]{64}', attempt), 'invalid_menu_trial')
    return first_trial._previous(app.task_directory() / 'first-trials' / (attempt + '.json'))


def _public(app, key, report, overrides):
    report = dict(report, trial_receipt=key)
    values = [part for name, value in overrides.items() for part in ('--set', name + '=' + value)]
    script = str(Path(app.__file__).resolve())
    for field in ('menu_repair', 'input_pause'):
        pending = report.get(field)
        if not pending or not pending.get('ticket'):
            continue
        pending = report[field] = dict(pending)
        if field == 'menu_repair':
            pending.update(frame_script=script,
                frame_args=['inspect-menu-trial', '--receipt', key, '--allow-physical', *values])
            if pending.get('frame'):
                pending.update(resume_script=script,
                    resume_args=['resume-menu-trial', '--receipt', key, '--frame', pending['frame'],
                                 '--point', '<data cell x>', '<data cell y>', '--allow-physical', *values])
            report['host_action'] = 'inspect_current_menu_without_replaying'
        else:
            pending.update(resume_script=script,
                resume_args=['resume-menu-trial', '--receipt', key, '--allow-physical', *values])
            report['host_action'] = 'wait_for_user_idle_then_resume'
    if report.get('frame') and report.get('resume_args'):
        report.update(resume_script=script,
            resume_args=['resume-menu-trial', '--receipt', key, '--frame', report['frame'],
                         '--point', '<data cell x>', '<data cell y>', '--allow-physical', *values])
    if (report.get('status') in ('blocked', 'uncertain', 'needs_grounding')
            and report.get('input_dispatched') is not False):
        report.setdefault('host_action', 'check_current_effect_before_continuing')
    return report


def _save_correction(app, key, folder, state, report):
    """Promote proven point changes to a new recording; never overwrite its source."""
    import recorded_plan
    current = _read(folder / 'workflow.json')
    if current == state['recipe']:
        return
    source = deepcopy(state['source'])
    for number, step in enumerate(current['steps'], 1):
        spec = repair.menu(step)
        if not spec or spec == repair.menu(state['recipe']['steps'][number-1]):
            continue
        original = source['steps'][state['source_steps'][number-1]-1]
        _need(original['action'] == 'result-menu', 'menu_trial_source_mapping_unavailable')
        original['context']['menu'] = deepcopy(spec)
    suffix = recovery._fingerprint(current)[:8]
    source['name'] = state['source']['name'][:70] + ' - 검증 ' + suffix
    compiled = recorded_plan.compile_plan(source)
    expected = dict(current, name=source['name'])
    _need(compiled.get('envelope', {}).get('recipe') == expected, 'menu_trial_source_mapping_unavailable')
    path = app.task_directory() / (source['name'] + '.json')
    if path.exists():
        _need(tasks.load_task(source['name'], app.task_directory()) == source, 'menu_trial_copy_changed')
    else:
        tasks.save_task(source, app.task_directory())
    plan = recorded_plan.get_plan(source, app.task_directory())
    _need(bool(plan.get('envelope')), 'menu_trial_plan_unavailable')
    report.update(effective_task=source['name'], correction_saved=True,
                  register_script=str(Path(app.__file__).with_name('python_workflow.py')),
                  register_args=['register', '--task', source['name'], '--out', '<project skill folder>'])


def execute(app, payload):
    """Called inside the already-counted plan worker for initial or resumed input."""
    import python_workflow as flow
    request = payload['menu_trial']
    key = request['receipt']
    folder, state, recipe = _state(app, key)
    _need(recipe == payload['recipe'], 'prepared_recording_changed')
    operation = request.get('operation', 'run')
    _need(operation in ('run', 'inspect', 'resume'), 'invalid_menu_trial')
    overrides = payload['overrides']
    args = SimpleNamespace(recipe=folder / 'workflow.json', only=None, hwnd=payload.get('hwnd'),
        repair_frame=None, resume_menu=None, resume_input=None, frame=None, point=None, set=[],
        auto_repair_frame=bool(request.get('auto_repair_frame')), allow_physical=True,
        remember_physical=False, background_only=False)
    if operation != 'run':
        record = repair.RepairSession(args.recipe).read(request['ticket'], recipe)
        if operation == 'inspect':
            _need(not record.get('pause'), 'menu_trial_frame_not_required')
            args.repair_frame = record['ticket']
        elif record.get('pause'):
            _need(request.get('frame') is None and request.get('point') is None, 'invalid_menu_trial')
            args.resume_input = record['ticket']
        else:
            _need(isinstance(request.get('frame'), str) and isinstance(request.get('point'), list)
                  and len(request['point']) == 2 and all(type(n) is int for n in request['point']),
                  'menu_repair_frame_and_point_required')
            args.resume_menu, args.frame, args.point = record['ticket'], request['frame'], request['point']
    report = repair.run_registered(flow, recipe, args, overrides, enable_result_review=False,
                                   allow_recorded_selection=True)
    if operation != 'inspect':
        try:
            _save_correction(app, key, folder, state, report)
        except (ValueError, OSError) as error:
            report['correction_saved'] = False
            report['correction_diagnostic'] = getattr(error, 'code', 'menu_trial_copy_unavailable')
    return _public(app, key, report, overrides)


def continue_menu(app, args, *, inspect=False):
    import first_trial
    key = args.receipt
    folder, state, recipe = _state(app, key)
    previous = prior(app, key)
    if not args.allow_physical:
        return dict(status='needs_consent', diagnostic='first_trial_physical_input_required',
                    required_option='--allow-physical', input_dispatched=False)
    try:
        record = repair.RepairSession(folder / 'workflow.json').read(None, recipe)
    except ValueError:
        if previous:
            return previous
        raise
    overrides = tasks.parse_overrides(args.set)
    _need(repair.digest(overrides) == record['variables'], 'menu_repair_parameters_changed')
    request = dict(receipt=key, operation='inspect' if inspect else 'resume', ticket=record['ticket'])
    if not inspect:
        request.update(frame=args.frame, point=args.point)
    payload = dict(recipe=recipe, overrides=overrides, menu_trial=request)
    if inspect:
        return app.worker_receipt(['_plan'], mutating=False, payload=payload)
    attempt = recovery._fingerprint(['menu-trial-resume', key, record['ticket'], args.frame, args.point, repair.digest(overrides)])
    _write(folder / 'last-attempt.json', attempt)
    def dispatch():
        report = app.worker_receipt(['_plan'], mutating=True, payload=payload)
        report.update(prior_steps_replayed=False, resume_dispatched_input=report.get('input_dispatched'),
                      effective_task=report.get('effective_task', state['effective_task']))
        return report
    return first_trial._once(app.task_directory(), attempt, dispatch)
