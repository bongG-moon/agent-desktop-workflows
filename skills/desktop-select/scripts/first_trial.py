"""Test a saved demonstration once, without creating a business skill."""
import json
from hashlib import sha256
import os
import re
import stat
import tempfile
import time

import input_policy
import learned_tasks as tasks
import recorded_plan
import recovery
import review_drafts


def _compiled(doc, directory):
    if doc.get('kind') != review_drafts.KIND:
        return recorded_plan.get_plan(doc, directory)
    rows = recovery._rows(doc)
    for number, row in enumerate(rows, 1):
        issue = row.get('field_problem') or row.get('scope_problem')
        if issue:
            return dict(status='not_compatible', diagnostic=issue, step=number)
    plan = {key: doc[key] for key in ('name', 'exe', 'window', 'schema')}
    plan['steps'] = [row['step'] for row in rows]
    try:
        tasks._validate(plan)
        return recorded_plan.compile_plan(plan)
    except (tasks.TaskError, ValueError) as error:
        return dict(status='not_compatible', diagnostic=getattr(error, 'code', 'invalid_task'),
                    step=getattr(error, 'step_number', None))


def _previous(path):
    tasks._deny_redirect(path)
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise tasks.TaskError('invalid_first_trial_receipt')
        with path.open('rb') as reader:
            data = reader.read(4 * 1024 * 1024 + 1)
        if len(data) <= 4 * 1024 * 1024:
            result = json.loads(data)
            if isinstance(result, dict) and 'status' in result:
                return dict(result, previous_trial=True, this_call_dispatched_input=False)
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    # Includes interruption after input but before a final receipt was persisted.
    return dict(status='uncertain', diagnostic='first_trial_running_or_interrupted', previous_trial=True,
                host_action='check_current_effect_before_continuing', input_dispatched=None,
                this_call_dispatched_input=False)


def _inspection(app, receipt_id):
    if not re.fullmatch(r'[0-9a-f]{64}', receipt_id):
        raise tasks.TaskError('invalid_first_trial_receipt')
    directory = tasks._directory(app.task_directory() / 'first-trials')
    report = _previous(directory / (receipt_id + '.json'))
    inspection = report.get('result_inspection')
    if not inspection or report.get('trial_receipt') != receipt_id:
        raise tasks.TaskError('result_inspection_unavailable')
    return directory, report, inspection


def inspect_result(app, receipt_id):
    """Read only the exact, recently failed result surface; never replay the query."""
    import grounding
    import result_trial
    directory, report, inspection = _inspection(app, receipt_id)
    resumed = result_trial.prior(directory, receipt_id)
    if resumed and resumed.get('resume_dispatched_input') is not False:
        return resumed
    if not 0 <= time.time() - inspection['observed_at'] <= grounding.TTL:
        raise tasks.TaskError('result_inspection_expired')
    identity = inspection['window']
    app.bridge.set_dpi_awareness()
    binding = app.bridge.Binding(identity)
    def check():
        app.guard(binding, binding, identity['title'])
        app.visibility.require_observable(binding)
    geometry = None
    if inspection.get('result_row'):
        import python_workflow
        with app.bridge.UIA() as uia:
            table, selector = python_workflow._result_target(binding, uia, inspection['selector'], None)
            box = dict(uia.bounds(table))
            rect, image = grounding._capture(app, binding, check)
            if uia.bounds(table) != box:
                raise tasks.TaskError('result_table_changed')
            result_trial.visible_table_box(rect, box)
            geometry = dict(selector=selector, table_box=box, window_rect=list(rect))
    else:
        rect, image = grounding._capture(app, binding, check)
    token, path = grounding.store_frame(grounding._directory(), receipt_id.encode('ascii'), image)
    image_digest = sha256(image).hexdigest()
    image_unchanged = inspection.get('image_sha256') == image_digest
    frame_geometry = dict(geometry, frame=token) if geometry else None
    if not image_unchanged or (geometry and inspection.get('frame_geometry') != frame_geometry):
        inspection['image_sha256'] = image_digest
        if geometry:
            inspection['frame_geometry'] = frame_geometry
        # Bind the host's later point to this exact table/frame, never an old marker.
        report.pop('previous_trial', None)
        report.pop('this_call_dispatched_input', None)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, mode='w', encoding='utf8', delete=False) as stream:
                tmp = stream.name
                json.dump(report, stream, ensure_ascii=False, allow_nan=False)
            os.replace(tmp, directory / (receipt_id + '.json'))
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
    result = dict(status='observed', diagnostic='current_result_window_captured', input_dispatched=False,
                source_step=inspection['source_step'], column=inspection.get('column'),
                window=identity['title'], frame=token, image=str(path), width=rect[2], height=rect[3],
                image_sha256=image_digest, image_unchanged=image_unchanged,
                image_review='reuse_if_already_seen' if image_unchanged else 'review_current_image',
                trial_receipt=receipt_id, result_match='unverified', prior_steps_replayed=False)
    if inspection.get('resume_supported'):
        result['resume_args'] = ['resume-trial','--receipt',receipt_id,'--frame',token]
        if inspection.get('result_row'):
            result.update(result_row=inspection['result_row'], table_box=geometry['table_box'],
                          row_confirmation='confirm_current_visible_rows_in_this_frame')
            result['resume_args'] += ['--row-policy', inspection['result_row']]
        else:
            result['resume_args'] += ['--column',inspection['column'],'--value','<observed exact cell text>']
        result['resume_args'] += ['--point','<cell text x>','<cell text y>','--allow-physical']
    return result


def _once(directory, key, callback):
    folder = tasks._directory(directory / 'first-trials')
    folder.mkdir(exist_ok=True, parents=True)
    path = folder / (key + '.json')
    tasks._deny_redirect(path)
    try:
        with path.open('xb'):
            pass
    except FileExistsError:
        return _previous(path)
    tmp = None
    try:
        result = callback()
    except Exception:
        return _previous(path)
    if result.get('diagnostic') == 'no_interactive_desktop' and result.get('input_dispatched') is False:
        path.unlink(missing_ok=True)
        return result
    # A process crash or interrupted write leaves the original reservation intact.
    try:
        with tempfile.NamedTemporaryFile(dir=folder, delete=False, mode='w', encoding='utf-8') as file:
            tmp = file.name
            json.dump(result, file, ensure_ascii=False, allow_nan=False)
        os.replace(tmp, path)
    except (ValueError, OSError):
        result['receipt_diagnostic'] = 'first_trial_receipt_unavailable'
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
    return result


def run(app, args):
    started = time.monotonic()
    directory = app.task_directory()
    if args.trial_id and not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', args.trial_id):
        raise tasks.TaskError('invalid_first_trial_id')
    overrides = tasks.parse_overrides(args.set)
    name, saved = args.task, {}
    if args.session:
        from recorder_launch import status
        event = status(args.session, wait_seconds=8)
        if event['status'] != 'recorded':
            return event
        name = event['task']['name']
        saved = event.get('live_recovery') or {}
    doc = tasks.load_task(name, directory, allow_review=True)
    source_key = recovery._fingerprint(doc)
    revision = (directory / (doc['name'] + '.json')).stat().st_mtime_ns
    key = recovery._fingerprint([doc['name'], revision, source_key, {k.casefold():v for k,v in overrides.items()},
                                 args.hwnd, args.trial_id])
    existing = tasks._directory(directory / 'first-trials') / (key + '.json')
    if existing.exists():
        import trial_menu
        menu_resumed = trial_menu.prior(app, key)
        if menu_resumed:
            return menu_resumed
        import result_trial
        resumed = result_trial.prior(existing.parent, key)
        if resumed:
            return resumed
        return _previous(existing)
    if saved.get('source_fingerprint') != source_key:
        saved = {}
    plan = _compiled(doc, directory)
    source_steps = None
    if not plan.get('envelope'):
        if not saved:
            # A cold trial can prepare once. Use the exact saved evidence if the
            # recorder already did that work, including an unresolved binding.
            argv = ['_worker', 'recover', '--task', name, '--prepare']
            if not args.background_only:
                argv.append('--restore-window')
            saved = app.worker_receipt(argv, mutating=False, may_queue=not args.background_only)
        if saved.get('host_action') != 'use_prepared_task':
            return saved
        if saved.get('source_fingerprint') != source_key:
            raise tasks.TaskError('recording_draft_changed')
        prepared = tasks.load_task(saved['task']['name'], directory)
        if (prepared['exe'], prepared['window']) != (doc['exe'], doc['window']) or (
                recorded_plan.fingerprint(prepared) != saved.get('execution_plan', {}).get('source_fingerprint')):
            raise tasks.TaskError('prepared_recording_changed')
        plan = _compiled(prepared, directory)
        name = prepared['name']
        source_steps = saved.get('source_steps')
    envelope = plan.get('envelope')
    if not envelope:
        return dict(status='needs_review', input_dispatched=False, execution_plan=app.execution_plan_info(plan))
    recipe = envelope['recipe']
    import python_workflow
    values, missing = python_workflow.values_for(recipe, overrides)
    if missing:
        return dict(status='needs_input', diagnostic='required_parameter_missing',
                    missing_parameters=missing, input_dispatched=False)
    profile = input_policy.profile(recipe['steps'])
    if args.background_only and not profile['background_eligible']:
        return input_policy.blocked(profile)
    if profile['physical_input_required'] and not args.allow_physical:
        return dict(status='needs_consent', diagnostic='first_trial_physical_input_required', required_option='--allow-physical',
                    input_requirements=profile, input_dispatched=False)
    refusal = input_policy.interactive_desktop_blocked(profile)
    if refusal:
        return refusal
    def dispatch():
        if recovery._fingerprint(tasks.read_document(doc['name'], directory)) != source_key:
            return dict(status='blocked', diagnostic='recording_draft_changed', input_dispatched=False)
        prepared_at = time.monotonic()
        payload = dict(recipe=recipe, overrides=overrides, hwnd=args.hwnd, background_only=args.background_only)
        if any(python_workflow.workflow_repair.menu(step) for step in recipe['steps']):
            import trial_menu
            trial_menu.prepare(app, key, doc, name, envelope)
            payload['menu_trial'] = dict(receipt=key, auto_repair_frame=getattr(args, 'auto_repair_frame', False))
        report = app.worker_receipt(['_plan'], mutating=True, payload=payload)
        observations = []
        for row in report.get('steps', []):
            step = recipe['steps'][row['number'] - 1]
            receipt = row['receipt']
            if step['action'] == 'read-value' and receipt.get('status') == 'observed' and 'value' in receipt:
                observations.append(dict(field=step['label'], value=receipt['value'],
                                         source_step=envelope['source_steps'][row['number'] - 1]))
        if observations:
            report['observations'] = observations
        report = python_workflow.summarize_execution(report, list(enumerate(recipe['steps'], 1)))
        for entry in report.get('steps', []):
            number = entry['number']
            entry['source_step'] = envelope['source_steps'][number-1]
            if source_steps:
                entry['original_steps'] = [int(n) for n, prepared in source_steps.items()
                                           if prepared == entry['source_step']]
        number = report.get('stopped_at')
        if type(number) is int and 0 < number <= len(envelope['source_steps']):
            report['source_stopped_at'] = envelope['source_steps'][number-1]
            if source_steps:
                report['original_stopped_steps'] = [int(n) for n, prepared in source_steps.items()
                                                    if prepared == report['source_stopped_at']]
        if (report.get('status') in ('blocked', 'uncertain', 'needs_grounding')
                and report.get('input_dispatched') is not False
                and not (report.get('menu_repair') or report.get('input_pause', {}).get('ticket'))):
            report['host_action'] = 'check_current_effect_before_continuing'
        if report.get('source_stopped_at') and report.get('steps'):
            last = report['steps'][-1]
            receipt = last.get('receipt', {})
            window = receipt.get('result_window')
            step = recipe['steps'][number - 1]
            if (last['number'] == number and step['action'] == 'verify-value' and step.get('result_column')
                    and receipt.get('input_dispatched') is False and window):
                diagnostic = receipt.get('diagnostic') or report.get('diagnostic')
                if diagnostic not in python_workflow.RESULT_IMAGE_ERRORS:
                    report['host_action'] = 'report_unmatched_result'
                else:
                    import result_trial
                    expected = values[step['parameter'].casefold()] if 'parameter' in step else step['value']
                    report.update(host_action='inspect_current_result_without_replaying',
                                  result_inspection=dict(source_step=report['source_stopped_at'], column=step['result_column'],
                                      observed_at=time.time(), window=window, capture_requires_consent=True,
                                      args=['inspect-trial', '--receipt', key]))
                    if result_trial.supported(recipe, number) and diagnostic not in ('result_table_missing', 'target_missing'):
                        report['result_inspection'].update(resume_supported=True, recipe_hash=recovery._fingerprint(recipe),
                            expected_hash=recovery._fingerprint([key, expected]), source_hash=source_key, source_task=doc['name'])
            elif (last['number'] == number and step['action'] == 'double-click-result' and step.get('result_row')
                  and receipt.get('input_dispatched') is False and window):
                if receipt.get('diagnostic') == 'result_row_unavailable':
                    import result_trial
                    report.update(host_action='inspect_current_result_without_replaying',
                        result_inspection=dict(source_step=report['source_stopped_at'], result_row=step['result_row'],
                            selector=receipt.get('result_surface_selector') or step['selector'],
                            observed_at=time.time(), window=window, capture_requires_consent=True,
                            args=['inspect-trial', '--receipt', key]))
                    if result_trial.supported(recipe, number):
                        report['result_inspection'].update(resume_supported=True, recipe_hash=recovery._fingerprint(recipe),
                            source_hash=source_key, source_task=doc['name'])
                else:
                    report['host_action'] = 'report_unmatched_result'
        elif report.get('status') in ('verified', 'completed_unverified'):
            report['host_action'] = 'report_completed_trial'
        report.update(effective_task=report.get('effective_task', name), trial_receipt=key, preparation_seconds=round(prepared_at-started, 3),
                      execution_seconds=round(time.monotonic()-prepared_at, 3))
        if source_steps:
            report['source_steps'] = source_steps
        return report
    return _once(directory, key, dispatch)
