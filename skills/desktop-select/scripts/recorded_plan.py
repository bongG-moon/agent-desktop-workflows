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
COMPILER = 9
MAX_BYTES = tasks.MAX_FILE_BYTES * 2


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf8')


def fingerprint(task):
    tasks._validate(task)
    return sha256(_canonical(task)).hexdigest()


def _result(status, **fields):
    return dict(status=status, input_dispatched=False, **fields)


def compile_plan(task, *, replacements=None):
    """Return a complete compatible recipe or no recipe at all.

    Explicit fixed/required instructions retain their meaning. Legacy recordings
    containing both a demonstrated value and a parameter compile as required
    input: the demonstrated value is not silently converted to a fixed value.
    Callers retaining legacy replay defaults should leave those tasks on that
    player; the notes make this distinction visible before export.
    """
    # Keep the original instruction and fingerprint in the envelope, including
    # legacy drafts whose supported action can now be resolved locally.
    steps = tasks.resolve_saved_intents(task)['steps']
    if replacements:
        for number, replacement in replacements.items():
            original = steps[number - 1]
            if original['action'] == 'manual' and not tasks.is_recoverable(original):
                return _result('not_compatible', diagnostic=original['reason'], step=number)
            step = deepcopy(replacement)
            if 'window_context' in original:
                step['window_context'] = deepcopy(original['window_context'])
            steps[number - 1] = step
    if len(steps) > 100:
        return _result('not_compatible', diagnostic='execution_plan_step_limit')
    # Establish whole-task compatibility before deriving any executable steps.
    for number, step in enumerate(steps, 1):
        reason = None
        if tasks.has_groundable_draft(step):
            return _result('not_compatible', diagnostic='instruction_requires_structure', step=number,
                           grounding_steps=[tasks.grounding_request(i, s) for i, s in enumerate(steps, 1)
                                            if tasks.has_groundable_draft(s)])
        elif step['action'] == 'manual' and step.get('instruction_draft'):
            reason = step['reason']
        elif step['action'] in ('row-action', 'row-launch', 'row-open', 'visual-click', 'conditional-open'):
            continue
        elif step.get('action') not in (*tasks.VALUE_ACTIONS, 'open'):
            reason = 'execution_plan_special_step'
        elif step.get('match') not in ('automation_id', 'name', 'query'):
            reason = 'execution_plan_stable_id_required'
        elif step['action'] == 'open' and (step.get('expand') or step['role'] not in tasks.INVOKE):
            reason = 'execution_plan_invoke_required'
        if reason:
            return _result('not_compatible', diagnostic=reason, step=number)
    converted, source_steps, notes = [], [], []
    recipe_numbers = {}
    openers = {step['window_context']['after_step'] for step in steps
               if 'after_step' in step.get('window_context', {})}
    for number, step in enumerate(steps, 1):
        recipe_numbers[number] = len(converted) + 1
        context = deepcopy(step.get('window_context'))
        if context is not None and 'after_step' in context:
            context['after_step'] = recipe_numbers[context['after_step']]
        if step['action'] in ('conditional-open','row-open'):
            current = deepcopy(step)
            if context is not None:
                current['window_context'] = context
            converted.append(current)
            source_steps.append(number)
            continue
        if step['action'] in ('row-action', 'row-launch', 'visual-click'):
            # Preserve demonstrated identity versus a live ordinal request.
            # A relative row instruction must still select the current Nth row.
            fields = ('action', 'label', 'row', 'row_hint', 'ordinal', 'alias', 'visual')
            converted.append({k: deepcopy(step[k]) for k in fields if k in step})
            if context is not None:
                converted[-1]['window_context'] = context
            source_steps.append(number)
            continue
        # Original recorded playback stopped at a command whose business result
        # was unverified. Do not silently turn that into immediate continuation.
        if step['action'] == 'open' and number < len(steps) and not step.get('wait_after') and number not in openers:
            # Independent clicks in two separately recorded device windows do not
            # make the next device depend on the first one's business outcome.
            next_step = steps[number]
            current_scope, next_scope = step.get('window_context'), next_step.get('window_context')
            independent = (current_scope and next_scope and next_step['action'] in ('visual-click', 'open', 'conditional-open')
                           and (current_scope['exe'].casefold(), current_scope['title']) !=
                               (next_scope['exe'].casefold(), next_scope['title']))
            if not independent:
                return _result('not_compatible', diagnostic='execution_plan_wait_required', step=number)
        selector = dict(role=step['role'])
        if step['match'] == 'query':
            selector['query'] = step['target']
            if step.get('within'):
                selector['within'] = step['within']
        else:
            selector[step['match']] = step['target']
        if step.get('ancestors') and step['match'] != 'query':
            selector['ancestors'] = deepcopy(step['ancestors'])
        current = dict(action='set-value' if step['action'] in tasks.VALUE_ACTIONS else 'invoke', selector=selector)
        if step['match'] == 'query' and step['action'] == 'open' and step.get('instruction', {}).get('text'):
            current['grounding_text'] = step['instruction']['text']
        if context is not None:
            current['window_context'] = context
        label = step.get('alias') or step.get('label') or step['target']
        if label:
            current['label'] = label
        if step['action'] in tasks.VALUE_ACTIONS and step.get('label') and step['match'] != 'query':
            current['field_label'] = step['label']
        if step.get('command_name'):
            current['command_name'] = step['command_name']
        if step['action'] in tasks.VALUE_ACTIONS:
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
            wait = dict(action='wait', seconds=step['wait_after'])
            if context is not None:
                wait['window_context'] = deepcopy(context)
            converted.append(wait)
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


def compile_saved(task, directory=None):
    """Compile using this original's locally registered interpretation, if any."""
    import compound_plan
    import grounding
    try:
        replacements = (compound_plan.load(task, directory) if any(
            step.get('instruction_draft') for step in task.get('steps', ())) else {})
        if any(s.get('window_context',{}).get('after_step') for s in task.get('steps', ())):
            rows = grounding.row_replacements(task, directory)
            compound_plan.require(not any(replacements.get(n, {}).get('action') == 'conditional-open' for n in rows),
                                  'row_binding_conflicts_with_interpretation')
            replacements.update(rows)
        return compile_plan(task,replacements=replacements)
    except (ValueError,OSError,RuntimeError) as error:
        return _result('plan_unavailable',diagnostic=getattr(error,'code','invalid_interpretation'))


def save_plan(task, directory=None):
    """Atomically replace only this derived plan; never modify the recording.

    Store failure is a separate status so saving the original recording can
    still succeed. Invalid source tasks raise their original validation error.
    """
    result = compile_saved(task, directory)
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
    expected = compile_saved(task, directory)
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
