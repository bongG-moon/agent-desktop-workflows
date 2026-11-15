"""Store a host's declarative interpretation beside the recording it explains."""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile

import learned_tasks as tasks


def require(ok, reason):
    if not ok:
        raise tasks.TaskError(reason)


def read_json(path):
    """Read one bounded local JSON file without following junctions or links."""
    path = Path(path)
    tasks._directory(path.parent)
    tasks._deny_redirect(path)
    try:
        with path.open('rb') as file:
            data = file.read(tasks.MAX_FILE_BYTES + 1)
        require(len(data) <= tasks.MAX_FILE_BYTES, 'interpretation_too_large')
        return json.loads(data.decode('utf-8-sig'), object_pairs_hook=tasks._unique_keys)
    except (UnicodeError, json.JSONDecodeError, OSError, RecursionError) as error:
        raise tasks.TaskError('interpretation_file_unavailable') from error


def path_for(task, directory):
    base = tasks._directory(directory)
    path = base / 'interpretations' / (tasks._name(task['name']) + '.json')
    tasks._deny_redirect(path.parent)
    tasks._deny_redirect(path)
    return path


def _scope(task, step):
    context = step.get('window_context', {})
    return (task['exe'].casefold(), task['window'], context.get('exe', task['exe']).casefold(),
            context.get('title', task['window']))


def _project_skills(directory):
    # Both <project>/.desktop-select/tasks and the portable
    # <project>/desktop-select/.desktop-select/tasks can use this project's skills.
    store = tasks._directory(directory)
    require(store.name == 'tasks' and store.parent.name == '.desktop-select',
            'project_skill_store_required')
    project = store.parent.parent
    if project.name == 'desktop-select' and (project / 'scripts/desktop_select.py').is_file():
        project = project.parent
    return tasks._directory(project / '.claude/skills')


def _pin_opener(task, index, donor, source_index, replacements):
    """A named child needs its demonstrated opener, even after the list scrolls."""
    source = task['steps'][index].get('window_context', {}).get('after_step')
    other = donor['steps'][source_index].get('window_context', {}).get('after_step')
    if source is None or other is None:
        return
    a, b = task['steps'][source - 1], donor['steps'][other - 1]
    if (a['action'] != 'row-launch' or b['action'] != 'row-launch'
            or a['ordinal'] != b['ordinal'] or _scope(task, a) != _scope(donor, b)
            or 'row_hint' not in b):
        return
    # row-launch intentionally falls back to the current Nth visible row. Once
    # the child's exact title is part of the instruction, that could open its
    # neighbour. row-action requires the demonstrated key, button and state.
    candidate = {k: deepcopy(a[k]) for k in ('label', 'alias', 'window_context') if k in a}
    candidate.update(action='row-action', row=deepcopy(a.get('row_hint', b['row_hint'])))
    require(source not in replacements or candidate == replacements[source], 'conditional_opener_hint_conflict')
    replacements[source] = candidate


def _target(task, index, field, directory, hints):
    require(isinstance(field, dict) and tasks._text(field.get('name'), 200),
            'invalid_conditional_target')
    name = field['name']
    if set(field) == {'name'}:
        return {'name': name}
    # Old unlabelled capture evidence has no recorded association with a branch.
    # Only explicitly named, scoped visual actions can donate a template.
    if set(field) == {'name', 'from_task', 'from_step'}:
        donor = tasks.load_task(field['from_task'], directory)
    elif set(field) == {'name', 'from_skill', 'from_step'}:
        key = field['from_skill']
        require(isinstance(key, str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', key),
                'invalid_reference_skill')
        skill = _project_skills(directory) / key
        donor = read_json(skill / 'source-task.json')
        tasks._validate(donor)
    else:
        raise tasks.TaskError('invalid_conditional_target')
    number = field['from_step']
    require(type(number) is int and 1 <= number <= len(donor['steps']), 'invalid_reference_step')
    source = donor['steps'][number - 1]
    require(source.get('action') == 'visual-click' and _scope(task, task['steps'][index]) == _scope(donor, source),
            'conditional_reference_scope_mismatch')
    label = source['label']
    caption = tasks.click_request(label)
    require(name.casefold() in {label.casefold(), caption[0].casefold() if caption else ''},
            'conditional_reference_name_mismatch')
    if 'from_skill' in field:
        import recorded_plan
        compiled = recorded_plan.compile_plan(donor).get('envelope', {})
        registered = read_json(skill / 'workflow.json')
        require(isinstance(registered, dict) and
                (registered.get('exe'), registered.get('window')) == (donor['exe'], donor['window']),
                'conditional_reference_workflow_mismatch')
        # Compare the named visual and any opener template we are going to use.
        # Unrelated commands can legitimately be promoted to stable selectors.
        required = {number}
        opener = source.get('window_context', {}).get('after_step')
        if opener is not None and 'row_hint' in donor['steps'][opener - 1]:
            required.add(opener)
        entries = registered.get('steps')
        for action_number in required:
            mapped = [i for i, n in enumerate(compiled.get('source_steps', [])) if n == action_number]
            require(len(mapped) == 1 and isinstance(entries, list) and mapped[0] < len(entries)
                    and entries[mapped[0]] == compiled['recipe']['steps'][mapped[0]],
                    'conditional_reference_workflow_mismatch')
    _pin_opener(task, index, donor, number - 1, hints)
    return {'name': name, 'visual': deepcopy(source['visual'])}


def _eligible(task, number):
    require(type(number) is int and 1 <= number <= len(task['steps']), 'invalid_interpreted_step')
    step = task['steps'][number - 1]
    require(bool(step.get('instruction_draft')) and
            (tasks.is_recoverable(step) or step['action'] in ('open', 'select', 'fill')),
            'recorded_instruction_required')
    return step


def prepare(task, instructions, directory):
    """Resolve model-produced names and optional local references, without UI input."""
    from conditional_action import validate_conditional
    require(isinstance(instructions, dict) and set(instructions) == {'schema', 'steps'}
            and type(instructions['schema']) is int and instructions['schema'] == 1
            and isinstance(instructions['steps'], list) and 0 < len(instructions['steps']) <= len(task['steps']),
            'invalid_interpretation')
    replacements, hints = {}, {}
    for item in instructions['steps']:
        require(isinstance(item, dict) and set(item) == {'number', 'text', 'seconds', 'when_present', 'dismiss', 'always'},
                'invalid_interpreted_step')
        number = item['number']
        _eligible(task, number)
        require(number not in replacements, 'duplicate_interpreted_step')
        step = {'action': 'conditional-open', 'label': item['text'], 'seconds': item['seconds']}
        # A recording with its own opener evidence needs no donor to pin it.
        _pin_opener(task, number - 1, task, number - 1, hints)
        for target in ('when_present', 'dismiss', 'always'):
            step[target] = _target(task, number - 1, item[target], directory, hints)
        validate_conditional(step)
        replacements[number] = step
    return replacements | hints


def _targets_digest(replacements):
    data = json.dumps(replacements, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf8')
    return sha256(data).hexdigest()


def load(task, directory):
    from recorded_plan import fingerprint
    path = path_for(task, directory)
    if not path.exists():
        return {}
    stored = read_json(path)
    require(isinstance(stored, dict) and set(stored) == {'source_fingerprint', 'instructions', 'targets_digest', 'schema'}
            and type(stored['schema']) is int and stored['schema'] == 1, 'invalid_interpretation')
    require(stored['source_fingerprint'] == fingerprint(task), 'interpretation_source_changed')
    replacements = prepare(task, stored['instructions'], directory)
    require(_targets_digest(replacements) == stored['targets_digest'], 'interpretation_targets_changed')
    return replacements


def save(task, instructions, directory):
    """Publish only complete, compilable interpretations. Source recordings are immutable."""
    from recorded_plan import compile_plan, fingerprint, get_plan
    from grounding import row_replacements
    replacements = prepare(task, instructions, directory)
    rows = row_replacements(task, directory)
    require(not any(replacements.get(n, {}).get('action') == 'conditional-open' for n in rows),
            'row_binding_conflicts_with_interpretation')
    preview = compile_plan(task, replacements=replacements | rows)
    if preview['status'] != 'compiled':
        return preview
    document = dict(schema=1, source_fingerprint=fingerprint(task), instructions=instructions,
                    targets_digest=_targets_digest(replacements))
    data = (json.dumps(document, ensure_ascii=False, indent=2) + '\n').encode('utf8')
    require(len(data) <= tasks.MAX_FILE_BYTES, 'interpretation_too_large')
    path = path_for(task, directory)
    temp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.instruction-', delete=False) as output:
            temp = Path(output.name)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        path_for(task, directory)
        require(fingerprint(tasks.load_task(task['name'], directory)) == document['source_fingerprint'],
                'interpretation_source_changed')
        os.replace(temp, path)
        temp = None
    except OSError as error:
        raise tasks.TaskError('interpretation_store_unavailable') from error
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    return get_plan(task, directory)
