"""Compile reviewed 0.4 recording instructions into a local literal execution plan.

The recording remains the source of truth. This derived file is accepted only
when its source, compiler version and complete recipe match a fresh compilation.
It contains data, never generated code or an instruction to a remote model.
"""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile

import learned_tasks as tasks


SCHEMA = 1
COMPILER = 1
MAX_BYTES = tasks.MAX_FILE_BYTES * 2


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf8')


def fingerprint(task):
    tasks._validate(task)
    return sha256(_canonical(task)).hexdigest()


def _result(status, **fields):
    return dict(status=status, input_dispatched=False, **fields)


def compile_plan(task):
    """Return a complete compatible recipe or no recipe at all.

    Explicit fixed/required instructions retain their meaning. Legacy recordings
    containing both a demonstrated value and a parameter compile as required
    input: the demonstrated value is not silently converted to a fixed value.
    Callers retaining legacy replay defaults should leave those tasks on that
    player; the notes make this distinction visible before export.
    """
    tasks._validate(task)
    if len(task['steps']) > 100:
        return _result('not_compatible', diagnostic='execution_plan_step_limit')
    # Establish whole-task compatibility before deriving any executable steps.
    for number, step in enumerate(task['steps'], 1):
        reason = None
        if step.get('action') not in ('select', 'open'):
            reason = 'execution_plan_special_step'
        elif step.get('match') != 'automation_id':
            reason = 'execution_plan_stable_id_required'
        elif step['action'] == 'open' and (step.get('expand') or step['role'] not in tasks.INVOKE):
            reason = 'execution_plan_invoke_required'
        if reason:
            return _result('not_compatible', diagnostic=reason, step=number)
    converted, source_steps, notes = [], [], []
    for number, step in enumerate(task['steps'], 1):
        # Original recorded playback stopped at a command whose business result
        # was unverified. Do not silently turn that into immediate continuation.
        if step['action'] == 'open' and number < len(task['steps']) and not step.get('wait_after'):
            return _result('not_compatible', diagnostic='execution_plan_wait_required', step=number)
        selector = dict(automation_id=step['target'], role=step['role'])
        if step.get('ancestors'):
            selector['ancestors'] = deepcopy(step['ancestors'])
        current = dict(action='set-value' if step['action'] == 'select' else 'invoke', selector=selector)
        label = step.get('alias') or step.get('label')
        if label:
            current['label'] = label
        if step['action'] == 'select' and step.get('label'):
            current['field_label'] = step['label']
        if step.get('command_name'):
            current['command_name'] = step['command_name']
        if step['action'] == 'select':
            instruction = step.get('instruction', {})
            if instruction.get('mode') == 'fixed':
                current['value'] = instruction['value']
            elif instruction.get('mode') == 'required':
                current['parameter'] = instruction['parameter']
            else:
                current['parameter'] = step['parameter']
                notes.append(dict(step=number, diagnostic='legacy_parameter_requires_input',
                                  parameter=step['parameter']))
        converted.append(current)
        source_steps.append(number)
        if step['action'] == 'open' and 'wait_after' in step:
            converted.append(dict(action='wait', seconds=step['wait_after']))
            source_steps.append(number)
    if len(converted) > 100:
        return _result('not_compatible', diagnostic='execution_plan_step_limit')
    recipe = dict(schema=1, name=task['name'], exe=task['exe'], window=task['window'], steps=converted)
    # The existing fast runner is the only execution contract. An otherwise
    # valid recording may exceed it; leave that task on the original player.
    from python_workflow import validate, WorkflowError
    from win_bridge import BridgeError
    try:
        validate(recipe)
    except (WorkflowError, BridgeError, tasks.TaskError) as error:
        return _result('not_compatible', diagnostic='execution_plan_contract_mismatch',
                       detail=getattr(error, 'code', 'invalid_recipe'))
    envelope = dict(schema=SCHEMA, compiler=COMPILER, source_fingerprint=fingerprint(task),
                    source=deepcopy(task), recipe=recipe, source_steps=source_steps, notes=notes)
    return _result('compiled', envelope=envelope)


def plan_path(task, directory=None):
    """Derived plans live below the existing, non-redirected local task store."""
    tasks._name(task['name'])
    base = tasks._directory(directory)
    folder = base / 'execution-plans'
    tasks._deny_redirect(folder)
    path = folder / (task['name'] + '.json')
    tasks._deny_redirect(path)
    return path


def save_plan(task, directory=None):
    """Atomically replace only this derived plan; never modify the recording.

    Store failure is a separate status so saving the original recording can
    still succeed. Invalid source tasks raise their original validation error.
    """
    result = compile_plan(task)
    if result['status'] != 'compiled':
        return result
    data = (json.dumps(result['envelope'], ensure_ascii=False, indent=2) + '\n').encode('utf8')
    if len(data) > MAX_BYTES:
        return _result('plan_unavailable', diagnostic='execution_plan_too_large')
    temp = None
    try:
        path = plan_path(task, directory)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='wb', dir=path.parent, prefix='.plan-', delete=False) as output:
            temp = Path(output.name)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        # Recheck paths immediately before publication, including the parent.
        plan_path(task, directory)
        os.replace(temp, path)
        temp = None
        return _result('saved', path=str(path), envelope=result['envelope'])
    except (OSError, tasks.TaskError) as error:
        return _result('plan_unavailable', diagnostic=getattr(error, 'code', 'execution_plan_store_unavailable'))
    finally:
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass


def load_plan(task, directory=None):
    """Never expose a stale or altered recipe, even if its fingerprint was kept."""
    expected = compile_plan(task)
    if expected['status'] != 'compiled':
        return expected
    try:
        path = plan_path(task, directory)
        if not path.is_file():
            return _result('missing', diagnostic='execution_plan_not_saved')
        with path.open('rb') as source:
            data = source.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            return _result('invalid', diagnostic='execution_plan_too_large')
        payload = json.loads(data.decode('utf8'), object_pairs_hook=tasks._unique_keys)
        if not isinstance(payload, dict):
            return _result('invalid', diagnostic='invalid_execution_plan')
        if (type(payload.get('schema')) is not int or type(payload.get('compiler')) is not int
                or payload['schema'] != SCHEMA or payload['compiler'] != COMPILER):
            return _result('stale', diagnostic='execution_plan_version_changed')
        if payload.get('source_fingerprint') != expected['envelope']['source_fingerprint']:
            return _result('stale', diagnostic='execution_plan_source_changed')
        if _canonical(payload) != _canonical(expected['envelope']):
            return _result('invalid', diagnostic='execution_plan_content_changed')
        return _result('loaded', path=str(path), envelope=expected['envelope'])
    except (json.JSONDecodeError, UnicodeError, RecursionError, tasks.TaskError) as error:
        return _result('invalid', diagnostic=getattr(error, 'code', 'invalid_execution_plan'))
    except OSError:
        return _result('plan_unavailable', diagnostic='execution_plan_store_unavailable')


def get_plan(task, directory=None):
    """Reuse a matching plan, or regenerate from the current validated recording."""
    existing = load_plan(task, directory)
    if existing['status'] in ('loaded', 'not_compatible', 'plan_unavailable'):
        return existing
    return save_plan(task, directory)
