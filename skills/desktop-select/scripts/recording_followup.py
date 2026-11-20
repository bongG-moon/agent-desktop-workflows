"""Make the next host operation explicit without repeating live observations."""
from pathlib import Path


def followup(result):
    """Decorate a saved receipt; never turn an uncertain dispatch into a retry."""
    if result.get('status') not in ('recorded', 'needs_grounding') or result.get('input_dispatched') is not False:
        return result
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
            args=['try-recording', '--task', task['name']],
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
        result['next_action'] = dict(kind='bind_existing_observation',
            script=str(scripts / 'desktop_select.py'), args=['recover', '--task', name],
            required_options=['--bindings', '--save-as'],
            evidence='live_recovery' if nested else 'current_receipt',
            template='live_recovery.bindings_template' if nested else 'bindings_template')
        result.pop('recover_args', None)
    return result
