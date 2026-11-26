"""Make the next host operation explicit without repeating live observations."""
from pathlib import Path


def _blocked_step(receipt):
    number = receipt.get('step_number')
    return next((row for row in receipt.get('steps', ()) if row.get('number') == number), {})


def _binding_available(receipt):
    """A valid prefix cannot resolve a missing later action."""
    number = receipt.get('step_number')
    rows = receipt['bindings_template'].get('steps', ())
    if number is None:
        return bool(rows)
    return (receipt.get('interpretation_hint', {}).get('number') == number
            or any(row['number'] <= number <= row.get('through', row['number']) for row in rows))


def followup(result):
    """Decorate a saved receipt; never turn an uncertain dispatch into a retry."""
    if result.get('status') not in ('recorded', 'needs_grounding') or result.get('input_dispatched') is not False:
        return result
    original = result.get('task')
    if result['status'] == 'recorded' and isinstance(original, dict) and isinstance(original.get('name'), str):
        review = ['review', '--task', original['name']]
        result.update(edit_args=[*review, '--edit'], restart_args=[*review, '--restart'])
    scripts = Path(__file__).absolute().parent
    recovered = result.get('live_recovery')
    nested = isinstance(recovered, dict)
    if not nested and result.get('status') == 'needs_grounding' and (
            'live_scopes' in result or result.get('host_action') == 'ground_recorded_double_click'):
        recovered = result
    ready = (recovered if isinstance(recovered, dict)
             and recovered.get('host_action') == 'use_prepared_task' else result)
    plan, task = ready.get('execution_plan', {}), ready.get('task', {})
    plan = plan if isinstance(plan, dict) else {}
    task = task if isinstance(task, dict) else {}
    if plan.get('status') == 'saved' and all(isinstance(task.get(k), str) for k in ('name', 'exe', 'window')):
        result['effective_task'] = task['name']
        result['preparation'] = 'ready'
        result['next_action'] = dict(
            kind='run_prepared_recording', script=str(scripts / 'desktop_select.py'),
            args=result.get('first_trial_args', ['try-recording', '--task', task['name'], '--summary']),
            required_parameters=plan.get('required_parameters', []),
            physical_input_required=plan.get('physical_input_required', False),
            register_and_test=dict(script=str(scripts / 'python_workflow.py'),
                args=['register', '--task', task['name'], '--run', '--summary'],
                required_options=['--out']))
        # Preparation already consumed this instruction. Keep its evidence, but
        # do not present the same observe/prepare call as the next operation.
        result.pop('recover_args', None)
        return result
    if not isinstance(recovered, dict):
        return result
    result['preparation'] = 'needs_grounding'
    if recovered.get('retry_args'):
        result['next_action'] = dict(kind='continue_preparation',
            script=str(scripts / 'desktop_select.py'), args=recovered['retry_args'])
    elif recovered.get('host_action') == 'inspect_recorded_step_when_popup_opens':
        result['next_action'] = dict(kind=recovered['host_action'], script=str(scripts / 'desktop_select.py'),
                                     args=recovered['inspect_args'], window=recovered['window'],
                                     opener_step=recovered['opener_step'], instruction=recovered['instruction'])
        result.pop('recover_args', None)
    elif recovered.get('host_action') == 'ground_recorded_double_click' and recovered.get('ground_args'):
        result['next_action'] = dict(kind='ground_recorded_double_click', script=str(scripts / 'desktop_select.py'),
                                     args=recovered['ground_args'], capture_requires_consent=True)
        result.pop('recover_args', None)
    elif recovered.get('bindings_template'):
        name = recovered.get('source_task') or task.get('name')
        if not isinstance(name, str) or not name:
            return result
        if _binding_available(recovered):
            result['next_action'] = dict(kind='bind_existing_observation',
                script=str(scripts / 'desktop_select.py'), args=['recover', '--task', name],
                required_options=['--bindings', '--save-as'],
                evidence='live_recovery' if nested else 'current_receipt',
                template='live_recovery.bindings_template' if nested else 'bindings_template')
        else:
            row = _blocked_step(recovered)
            if row.get('image_args'):
                result['next_action'] = dict(kind='inspect_recorded_step_image',
                    script=str(scripts / 'desktop_select.py'), args=row['image_args'],
                    capture_requires_consent=True)
            else:
                result['next_action'] = dict(kind='review_unresolved_instruction',
                    **{key: row[key] for key in ('number', 'text', 'window') if key in row})
            result['host_action'] = result['next_action']['kind']
        result.pop('recover_args', None)
    return result


def compact_preparation(report):
    """Render one preparation decision. The stored inventory stays available for diagnostics."""
    if report.get('input_dispatched') is not False or report.get('status') not in ('recorded', 'needs_grounding'):
        return report
    nested = report.get('live_recovery')
    receipt = nested if isinstance(nested, dict) else report
    if receipt.get('input_dispatched') is not False:
        return report
    result = dict(report)
    if result['status'] == 'recorded' and isinstance(result.get('task'), dict) and 'steps' in result['task']:
        task = result['task']
        result['task'] = {k: task[k] for k in ('name', 'exe', 'window') if k in task}
        result['task']['step_count'] = len(task['steps'])
    action = result.get('next_action', {})
    if action.get('kind'):
        result['host_action'] = action['kind']
    if receipt.get('status') == 'needs_grounding' and 'live_scopes' in receipt:
        evidence = {k: v for k, v in receipt.items() if k not in ('live_scopes', 'steps', 'next_action')}
        row = _blocked_step(receipt)
        if row:
            evidence['steps'] = [{k: v for k, v in row.items() if k != 'scope_index'}]
        if action.get('kind') != 'bind_existing_observation':
            evidence.pop('bindings_template', None)
        if action:
            evidence['host_action'] = action['kind']
        if isinstance(nested, dict):
            result['live_recovery'] = evidence
        else:
            result = evidence
            if action:
                result['next_action'] = action
    elif isinstance(nested, dict) and result.get('preparation') == 'ready':
        result.pop('live_recovery')
    return result
