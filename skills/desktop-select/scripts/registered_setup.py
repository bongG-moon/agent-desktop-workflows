"""Discover compact field identities and register a local reusable Python skill."""
import json
from pathlib import Path
import time

import desktop_select as app
import native_targets
import registered_profile as profiles


def discover(owner, title, identifiers=()):
    started = time.monotonic()
    app.guard(owner, owner, title)
    with app.bridge.UIA() as uia:
        root = uia.from_handle(owner.hwnd)
        items = []
        if identifiers:
            for identifier in dict.fromkeys(identifiers):
                items.extend(native_targets.find_all(uia, root, automation_id=identifier))
        else:
            items = native_targets.find_all(uia, root, roles=('combobox', 'button', 'statictext'), limit=256)
        controls, labels, parents = [], [], []
        for item in items:
            basic = uia.basic(item)
            if not basic.get('automation_id') and basic['role'] != 'statictext':
                continue
            parent = uia.parent(item)
            group = next((i for i, known in enumerate(parents) if uia.same(parent, known)), None)
            if group is None:
                group = len(parents)
                parents.append(parent)
            row = dict(basic, bounds=uia.bounds(item), group=group,
                       enabled=item.property(30010) is True)
            if basic['role'] == 'statictext':
                if basic.get('name'):
                    labels.append(row)
            else:
                # LabeledBy is cheap; avoid inspecting every sibling once per field.
                label = uia.related_element(item, 30018)
                if label is not None:
                    try:
                        native_targets.ancestry(uia, label, root, pid=owner.pid)
                        if label.property(30005):
                            row.update(semantic_label=label.property(30005), label_source='labeled_by')
                    except app.bridge.BridgeError:
                        pass
                if identifiers:
                    row['actions'] = uia.actions(item)
                controls.append(row)
        # Pair only same-parent, mutually unique left-hand labels. Other captions
        # remain hints for the one-time model registration, never execute targets.
        def aligned(label, field):
            a, b = label['bounds'], field['bounds']
            height = min(a['height'], b['height'])
            return (label['group'] == field['group'] and height > 0 and
                    min(a['y'] + a['height'], b['y'] + b['height']) - max(a['y'], b['y']) >= .65 * height
                    and 0 <= b['x'] - a['x'] - a['width'] <= height * 6)
        for field in controls:
            candidates = [label for label in labels if aligned(label, field)]
            if ('semantic_label' not in field and len(candidates) == 1
                    and sum(aligned(candidates[0], other) for other in controls) == 1):
                field.update(semantic_label=candidates[0]['name'], label_source='sibling_geometry')
        app.guard(owner, owner, title)
        return dict(status='observed', targets=controls, labels=labels, input_dispatched=False,
                    metrics=dict(full_tree_snapshots=0, elapsed_seconds=round(time.monotonic()-started, 4)))


def _read_resolution(path):
    path = Path(path).absolute()
    app.tasks._directory(path.parent)
    app.tasks._deny_redirect(path)
    try:
        with path.open('rb') as stream:
            raw = stream.read(app.tasks.MAX_FILE_BYTES + 1)
    except OSError as error:
        raise app.tasks.TaskError('resolution_file_unavailable') from error
    if len(raw) > app.tasks.MAX_FILE_BYTES:
        raise app.tasks.TaskError('resolution_too_large')
    try:
        return json.loads(raw, object_pairs_hook=app.tasks._unique_keys)
    except (ValueError, UnicodeError) as error:
        raise app.tasks.TaskError('invalid_resolution_file') from error


def register(args):
    started = time.monotonic()
    if args.recipe:
        if args.resolution:
            raise app.tasks.TaskError('resolution_requires_recorded_task')
        profile = profiles.load(args.recipe)
        plan = None
        exe, title = profile['exe'], profile['window']
    else:
        plan = app.tasks.load_task(args.task, app.task_directory())
        exe, title = plan['exe'], plan['window']
    binding = app.bridge.Binding(app.pick_window(app.windows(exe), title, args.hwnd))
    app.guard(binding, binding, title)
    snapshots = 0
    with app.bridge.UIA() as uia:
        root = uia.from_handle(binding.hwnd)
        if plan is not None:
            resolution = _read_resolution(args.resolution) if args.resolution else None
            if resolution is None and any(s['action'] == 'intent' for s in plan['steps']):
                from confirmed_plan import load
                resolution = load(plan, app.task_directory())
            # One registration observation establishes recorded scopes. It is
            # never repeated by the generated execution skill.
            nodes, elements = app.observe(binding, uia, identities_only=True)
            snapshots = 1

            def resolve_selector(source, operation):
                scoped, available = nodes, elements
                if source['action'] == 'intent':
                    from intent_replay import scope
                    scoped, indexes, _ = scope(nodes, source)
                    available = [elements[i] for i in indexes]
                index, _ = app.tasks.locate_target(scoped, operation if source['action'] == 'intent' else source)
                element = available[index]
                basic = uia.basic(element)
                if not basic.get('automation_id'):
                    raise app.tasks.TaskError('registration_requires_stable_id')
                selector = {'automation_id': basic['automation_id'], 'role': basic['role']}
                chain = native_targets.ancestry(uia, element, root, pid=binding.pid)
                ancestors = []
                for parent in chain[1:-1]:
                    info = uia.basic(parent)
                    if info.get('automation_id'):
                        ancestors.append({'role': info['role'], 'automation_id': info['automation_id']})
                    elif info.get('name'):
                        ancestors.append({'role': info['role'], 'name': info['name']})
                if ancestors:
                    selector['ancestors'] = ancestors
                native_targets.resolve(binding, uia, selector, root=root)
                return selector
            profile = profiles.from_task(plan, resolution, resolve_selector)
        observations = []
        for number, step in enumerate(profile['steps'], 1):
            if step['action'] == 'wait':
                continue
            item = native_targets.resolve(binding, uia, step['selector'], root=root)
            method = 'SetValue' if step['action'] == 'set-value' else 'Invoke'
            if ('SetValue' if method == 'SetValue' else 'Click') not in uia.actions(item):
                raise app.tasks.TaskError('registration_pattern_unavailable')
            observations.append(dict(number=number, selector=step['selector'], method=method))
        app.guard(binding, binding, title)
        result = profiles.export_skill(profile, args.out)
    return dict(result, status='registered', input_dispatched=False, targets=observations,
                metrics=dict(full_tree_snapshots=snapshots, elapsed_seconds=round(time.monotonic()-started, 4)))
