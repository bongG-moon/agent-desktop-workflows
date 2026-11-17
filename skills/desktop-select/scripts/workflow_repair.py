"""Short-lived, single-use point repairs for a registered Python workflow."""
from copy import deepcopy
from hashlib import sha256
import json
import os
from contextlib import ExitStack
from pathlib import Path
import tempfile
import time

import learned_tasks as tasks
import coordination


def need(ok, reason):
    if not ok:
        raise tasks.TaskError(reason)


def digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return sha256(encoded).hexdigest()


def registered(path):
    return bool(path and path.name == 'workflow.json' and (path.parent / 'scripts/run.py').is_file())


def menu(step):
    if step['action'] == 'context-menu':
        return step['spec']
    if step['action'] == 'result-menu':
        return step['context']['menu']
    return None


def replace_menu(step, spec):
    if step['action'] == 'context-menu':
        step['spec'] = deepcopy(spec)
    else:
        step['context']['menu'] = deepcopy(spec)


def identity(binding):
    return [binding.hwnd, binding.pid, binding.created]


def point_failed(receipt):
    return (receipt.get('diagnostic') in ('context_point_outside_region', 'context_point_unproven',
                                         'context_data_cell_unconfirmed', 'context_visual_patch_changed',
                                         'context_visual_patch_blank', 'context_point_not_data')
            and receipt.get('menu_input_dispatched', receipt['input_dispatched']) is False
            and receipt.get('leaf_dispatched') is False and not receipt.get('menu_items'))


def persist(path, original, report, load, replacements=None):
    """Only the exact menu that was actually dispatched can change its saved point."""
    revised, changed = deepcopy(original), []
    for row in report.get('steps', []):
        number, receipt = row['number'], row['receipt']
        previous = menu(original['steps'][number - 1])
        if not previous or (receipt.get('leaf_dispatched') is not True
                            or receipt.get('menu_path_completed') is not True
                            or receipt.get('menu_items') != previous['path']):
            continue
        replacement = deepcopy((replacements or {}).get(number, previous))
        correction = receipt.get('point_correction')
        if correction and correction['from'] == replacement['point']:
            replacement['point'] = correction['to']
            replacement.pop('visual_patch', None)
        if replacement != previous:
            need(replacement['target'] == previous['target'] and replacement['path'] == previous['path'],
                 'menu_correction_target_changed')
            replace_menu(revised['steps'][number - 1], replacement)
            changed.append(number)
    if not changed:
        return
    import context_menu
    for number in changed:
        context_menu.validate_spec(menu(revised['steps'][number - 1]))
    tasks._directory(path.parent)
    tasks._deny_redirect(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write((json.dumps(revised, ensure_ascii=False, indent=2) + '\n').encode())
            output.flush()
            os.fsync(output.fileno())
        need(load(path) == original, 'workflow_changed_during_repair')
        os.replace(temporary, path)
        need(load(path) == revised, 'workflow_changed_during_repair')
        report['menu_corrections'] = changed
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class RepairSession:
    def __init__(self, path):
        self.directory = tasks._directory(Path(tempfile.gettempdir()) / 'desktop-select-menu' /
                                          digest(os.path.normcase(str(path.absolute()))))
        self.directory.mkdir(parents=True, exist_ok=True)
        self.pending = self.directory / 'pending.json'
        tasks._deny_redirect(self.pending)

    def start(self, recipe, overrides, number, scope, only, checked_fields, query_sent, routes=None):
        import secrets
        ticket = secrets.token_hex(24)
        pending = dict(ticket=ticket, recipe=digest(recipe), variables=digest(overrides),
                       number=number, scope=scope, only=only, checked_fields=checked_fields,
                       query_sent=query_sent, routes=routes or [], expires=time.time() + 600)
        self.discard()
        return self.write(pending)

    def write(self, record):
        remaining = record['expires'] - time.time()
        need(remaining > 0, 'menu_repair_expired')
        with self.pending.open('x', encoding='utf8') as stream:
            json.dump(record, stream)
        ticket = record['ticket']
        return dict(ticket=ticket, step=record['number'], expires_seconds=int(remaining),
                    host_action='inspect_menu_repair_frame', frame_args=['--repair-frame', ticket, '--allow-physical'])

    def read(self, ticket, recipe):
        import secrets
        need(self.pending.is_file() and self.pending.stat().st_size < tasks.MAX_FILE_BYTES, 'menu_repair_expired')
        record = json.loads(self.pending.read_text(encoding='utf8'))
        need(secrets.compare_digest(record['ticket'], ticket) and time.time() < record['expires'],
             'menu_repair_expired')
        need(record['recipe'] == digest(recipe), 'workflow_changed_during_repair')
        return record

    def discard(self):
        self.pending.unlink(missing_ok=True)


def capture_region(app, binding, uia, spec, guard):
    import context_menu
    import grounding
    region = context_menu._region(binding, uia, spec['target'])
    x, y, width, height = context_menu._rect(uia, region)
    window, png = grounding._capture(app, binding, guard)
    left, top = x - window[0], y - window[1]
    need(width > 0 and height > 0 and left >= 0 and top >= 0 and
         left + width <= window[2] and top + height <= window[3], 'context_region_changed')
    need(context_menu._rect(uia, region) == (x, y, width, height), 'context_region_changed')
    _, pixels = grounding._pixels(png)
    stride = window[2] * 4
    crop = b''.join(pixels[row*stride + left*4 : row*stride + (left+width)*4]
                    for row in range(top, top + height))
    return (x, y, width, height), grounding._png(width, height, crop), window, png


def recorded_surface(flow, recipe, record):
    index = record['number'] - 1
    step = recipe['steps'][index]
    owner_id, surface_id = record['scope']
    owner = flow.bind(recipe['exe'], recipe['window'], owner_id[0])
    need(identity(owner) == owner_id, 'menu_repair_window_changed')
    ctx = step.get('window_context')
    binding = flow.bind(ctx['exe'], ctx['title'], surface_id[0]) if ctx else owner
    need(identity(binding) == surface_id, 'menu_repair_window_changed')
    resolved = None
    if ctx:
        resolved = flow.window_context.WindowRoutes(recipe['steps'], owner, recipe['window'],
                                                    read_only=True, active=[record['number']])
        restore_routes(flow, recipe, record, resolved)
    def guard():
        flow.app.guard(owner, owner, recipe['window'])
        flow.app.guard(binding, binding, ctx['title'] if ctx else recipe['window'])
        if ctx:
            resolved.check(ctx, binding)
    guard()
    return binding, guard


def inspect_or_adjust(flow, recipe, record, frame=None, point=None):
    import grounding
    import live_recovery
    step = recipe['steps'][record['number'] - 1]
    binding, guard = recorded_surface(flow, recipe, record)
    with flow.app.bridge.UIA() as uia:
        spec = menu(step)
        rect, crop, window, png = capture_region(flow.app, binding, uia, spec, guard)
        scope = json.dumps([record['ticket'], record['recipe'], record['scope'],
                            rect[0]-window[0], rect[1]-window[1]]).encode()
        if frame is None:
            token, path = grounding.store_frame(grounding._directory(), scope, crop)
            return dict(status='observed', input_dispatched=False, image=str(path), frame=token,
                        step=record['number'], screen_origin=list(rect[:2]), width=rect[2], height=rect[3],
                        coordinates='image_pixels', menu_path=spec['path'],
                        resume_args=['--resume-menu', record['ticket'], '--frame', token, '--point',
                                     '<x>', '<y>', '--allow-physical', '--summary'])
        _, previous = grounding.read_frame(grounding._directory(), scope, frame)
        need(live_recovery._same_patch(previous, crop, point), 'grounding_frame_changed')
        screen = rect[0]+point[0], rect[1]+point[1]
        result = flow.context_menu.adjust_from_frame(binding, uia, spec, screen, window=window, png=png)
        guard()
        return result


def saved_routes(routes, number):
    # Record only scopes whose lifetime and relationship had already been established.
    saved = []
    for step in routes.steps[number-1:]:
        spec = step.get('window_context', {})
        opener = spec.get('after_step')
        if not opener or opener >= number or any(r['spec'] == spec for r in saved):
            continue
        import window_context
        child = routes.bindings.get(window_context._key(spec))
        parent = routes.openers.get(opener)
        if child is not None and parent is not None:
            saved.append(dict(spec=spec, child=identity(child), parent=identity(parent)))
    return saved


def restore_routes(flow, recipe, record, routes):
    for row in record['routes']:
        spec = row['spec']
        opener = spec['after_step']
        origin = recipe['steps'][opener-1].get('window_context') or recipe
        title = origin.get('title') or recipe['window']
        parent = flow.bind(origin['exe'], title, row['parent'][0])
        child = flow.bind(spec['exe'], spec['title'], row['child'][0])
        need(identity(parent) == row['parent'] and identity(child) == row['child'], 'menu_repair_window_changed')
        routes.openers[opener] = parent
        routes.check(spec, child)
        routes.bindings[flow.window_context._key(spec)] = child


def run_registered(flow, recipe, args, overrides, scene_session=None):
    """One worker owns the stored ticket until its checked input has been consumed."""
    import review_drafts
    session = RepairSession(args.recipe)
    with ExitStack() as stack:
        try:
            stack.enter_context(coordination.recipe(args.recipe))
            # Retain exclusion with older installations using this ticket directory.
            stack.enter_context(review_drafts._edit_lock(session.directory))
        except coordination.CoordinationError as error:
            return dict(status='blocked', diagnostic=error.code, input_dispatched=False)
        except OSError:
            return dict(status='blocked', diagnostic='registered_workflow_busy', input_dispatched=False)
        resume = None
        replacements = {}
        try:
            current = flow.load(args.recipe)
            if current != recipe:
                # Another queued run may have persisted a proven correction.
                # Keep the exact consented actions, windows, paths and variables.
                original = deepcopy(current)
                for previous, step in zip(recipe['steps'], original['steps']):
                    before, now = menu(previous), menu(step)
                    if before and now:
                        for key in ('point', 'visual_patch'):
                            if key in before:
                                now[key] = before[key]
                            else:
                                now.pop(key, None)
                need(original == recipe, 'workflow_changed_during_repair')
            recipe = run_recipe = current
            if args.repair_frame or args.resume_menu:
                resume = session.read(args.repair_frame or args.resume_menu, recipe)
                if args.resume_menu:
                    active = [step for number, step in flow.selected_steps(recipe, resume['only'])
                              if number >= resume['number']]
                    checks = recipe['steps'] if resume['only'] is not None else ()
                    stack.enter_context(coordination.reserve_outputs(active, verify_steps=checks))
                # Hold the same window scopes through inspection and the
                # resumed operation. flow.run reenters them on this thread.
                contexts = coordination.scopes_for(recipe['steps'], recipe['exe'])
                owner = dict(hwnd=resume['scope'][0][0], exe_name=recipe['exe'])
                stack.enter_context(coordination.window(owner, recipe['window'], contexts))
                surface_hwnd = resume['scope'][1][0]
                if surface_hwnd != owner['hwnd']:
                    stack.enter_context(coordination.handle({'hwnd': surface_hwnd}))
                if args.repair_frame:
                    return inspect_or_adjust(flow, recipe, resume)
                need(digest(overrides) == resume['variables'], 'menu_repair_parameters_changed')
                point = inspect_or_adjust(flow, recipe, resume, args.frame, args.point)
                run_recipe = deepcopy(recipe)
                replacements[resume['number']] = point
                replace_menu(run_recipe['steps'][resume['number'] - 1], point)
            session.discard()
        except (OSError, ValueError, flow.app.bridge.BridgeError, flow.app.SelectionError) as error:
            return dict(status='blocked', diagnostic=getattr(error, 'code', 'menu_repair_unavailable'),
                        input_dispatched=False)
        report = flow.run(run_recipe, overrides, hwnd=resume['scope'][0][0] if resume else args.hwnd,
                          only=resume['only'] if resume else args.only, track_menu=True, resume=resume,
                          scene_session=scene_session)
        if resume:
            report['resumed_at'] = resume['number']
        scope = report.pop('menu_repair_scope', None)
        routes = report.pop('menu_repair_routes', None)
        try:
            persist(args.recipe, recipe, report, flow.load, replacements)
            if (resume and report.get('status') == 'blocked' and report.get('input_dispatched') is False
                    and report.get('steps') == [] and report.get('diagnostic') == 'window_root_unreadable'
                    and report.get('readability_check', {}).get('status') == 'unavailable'
                    and report['readability_check'].get('diagnostic') == 'window_root_unreadable'):
                # The claim was consumed before entering the runner. Restore it only
                # for a positive, pre-input transient receipt on the identical live scope.
                need(flow.load(args.recipe) == recipe, 'workflow_changed_during_repair')
                recorded_surface(flow, recipe, resume)
                report['menu_repair'] = session.write(resume)
            if scope:
                number = report['stopped_at']
                row = next(r for r in report['steps'] if r['number'] == number)
                # Preserve the original scope selection across multiple before-input repairs.
                only = resume['only'] if resume else args.only
                fields = list(resume['checked_fields']) if resume else []
                for entry in report['steps']:
                    prior = recipe['steps'][entry['number'] - 1]
                    if (entry['receipt'].get('status') == 'verified' and prior['action'] == 'set-value'
                            and not prior.get('popup') and not prior.get('window_context')
                            and 'anchor' not in prior['selector']):
                        fields = [n for n in fields if recipe['steps'][n-1]['selector'] != prior['selector']]
                        fields.append(entry['number'])
                query_sent = row['receipt'].get('query_dispatched') is True or bool(
                    resume and number == resume['number'] and resume['query_sent'])
                updated = flow.load(args.recipe) if report.get('menu_corrections') else recipe
                report['menu_repair'] = session.start(updated, overrides, number, scope, only, fields, query_sent, routes)
        except (OSError, ValueError, flow.app.bridge.BridgeError) as error:
            # The actual menu/leaf receipt remains authoritative even if the disk write failed.
            report['repair_storage'] = dict(status='not_saved',
                                            diagnostic=getattr(error, 'code', 'menu_repair_storage_unavailable'))
        return report
