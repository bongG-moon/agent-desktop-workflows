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
import input_policy


def need(ok, reason):
    if not ok:
        raise tasks.TaskError(reason)


def digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return sha256(encoded).hexdigest()


def registered(path):
    return bool(path and path.name == 'workflow.json' and (path.parent / 'scripts/run.py').is_file())


def approval_path(path):
    """A copied project must not carry another user's permission with it."""
    home = os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()
    directory = tasks._directory(Path(home) / 'DesktopSelect' / 'repeat-input')
    result = directory / (digest(os.path.normcase(str(path.resolve()))) + '.txt')
    tasks._deny_redirect(result)
    return result


def approval_state(path):
    try:
        marker = approval_path(path)
        if not marker.exists():
            return ''
        if marker.is_file() and marker.stat().st_size <= 80:
            return marker.read_text(encoding='ascii')
    except (OSError, ValueError):
        pass
    return None


def approved(path, recipe):
    return approval_state(path) == digest(recipe)


def write_approval(path, value):
    marker = approval_path(path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=marker.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(value.encode('ascii'))
        os.replace(temporary, marker)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def forget_approval(path):
    import secrets
    with coordination.recipe(path):
        # A forget must also invalidate enrollment pending in a repair ticket.
        write_approval(path, 'revoked:' + secrets.token_hex(24))


def tested_rows(recipe, rows, start, stop):
    if [r['number'] for r in rows] != list(range(start, stop)):
        return False
    modes = input_policy.profile(recipe['steps'][start - 1:stop - 1])['steps']
    for row, mode in zip(rows, modes):
        receipt = row['receipt']
        if mode['action'] == 'read-value':
            if not (receipt.get('status') == 'observed' and receipt.get('input_dispatched') is False
                    and isinstance(receipt.get('value'), str) and receipt.get('value_evidence', {}).get('status') == 'observed'):
                return False
        elif receipt.get('status') not in ('verified', 'waited', 'delivered_unverified'):
            return False
        if (mode['input_mode'] in ('physical_pointer', 'physical_keyboard')
                and receipt.get('input_dispatched') is not True):
            return False
    return True


def remember_test(path, recipe, report, prior_state, start=1):
    """Remember a scoped input mechanism only after the entire tested sequence finished."""
    rows = report.get('steps', [])
    if (not rows or report.get('input_dispatched') is not True
            or not tested_rows(recipe, rows, start, len(recipe['steps']) + 1)):
        return
    terminal = rows[-1]['receipt']
    # A final export without an output condition proves the requested menu path was
    # sent. This is still not a claim that the business result was verified.
    last_menu_sent = (report.get('status') == 'delivered_unverified'
                      and report.get('stopped_at') == len(recipe['steps'])
                      and terminal.get('diagnostic') == 'menu_delivered_no_output_condition'
                      and terminal.get('menu_path_completed') is True and terminal.get('leaf_dispatched') is True)
    if (not last_menu_sent and (report.get('status') not in ('verified', 'completed_unverified')
                                or 'stopped_at' in report)):
        return
    try:
        with coordination.recipe(path):
            if prior_state is not None and approval_state(path) == prior_state:
                write_approval(path, digest(recipe))
                report['repeat_physical_input'] = 'approved'
    except (OSError, ValueError):
        report['repeat_physical_input'] = 'not_saved'


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


def matches_identity(binding, saved):
    from win_bridge import normalize_hwnd
    return (len(saved) == 3 and binding.hwnd == normalize_hwnd(saved[0])
            and binding.pid == saved[1] and binding.created == saved[2])


INPUT_CHANGED = ('foreground_or_input_changed', 'physical_input_changed')


def unsent_menu(receipt):
    return (receipt.get('status') == 'blocked'
            and receipt.get('menu_input_dispatched', receipt['input_dispatched']) is False
            and receipt.get('leaf_dispatched') is False and not receipt.get('menu_items'))


def point_failed(receipt):
    return (unsent_menu(receipt) and receipt.get('diagnostic') in ('context_point_outside_region', 'context_point_unproven',
                                         'context_data_cell_unconfirmed', 'context_visual_patch_changed',
                                         'context_visual_patch_blank', 'context_point_not_data'))


def input_paused(receipt):
    return unsent_menu(receipt) and receipt.get('diagnostic') in INPUT_CHANGED


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
        return original
    import context_menu
    for number in changed:
        context_menu.validate_spec(menu(revised['steps'][number - 1]))
    replace_workflow(path, original, revised, load)
    report['menu_corrections'] = changed
    return revised


def replace_workflow(path, original, revised, load):
    """Atomically replace the exact workflow that was tested under its recipe lock."""
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

    def start(self, recipe, overrides, number, scope, only, checked_fields, query_sent, routes=None,
              *, pause=False, remember=False, approval_before=None):
        import secrets
        ticket = secrets.token_hex(24)
        pending = dict(ticket=ticket, recipe=digest(recipe), variables=digest(overrides),
                       number=number, scope=scope, only=only, checked_fields=checked_fields,
                       query_sent=query_sent, routes=routes or [], pause=pause, remember=remember, approval_before=approval_before,
                       expires=time.time() + 600)
        self.discard()
        return self.write(pending)

    def write(self, record):
        remaining = record['expires'] - time.time()
        need(remaining > 0, 'menu_repair_expired')
        with self.pending.open('x', encoding='utf8') as stream:
            json.dump(record, stream)
        return self.describe(record)

    @staticmethod
    def describe(record):
        ticket = record['ticket']
        remaining = record['expires'] - time.time()
        if record.get('pause'):
            return dict(ticket=ticket, step=record['number'], expires_seconds=int(remaining),
                        host_action='wait_for_user_idle_then_resume', resume_args=['--resume-input', ticket, '--summary'])
        return dict(ticket=ticket, step=record['number'], expires_seconds=int(remaining),
                    host_action='inspect_menu_repair_frame', frame_args=['--repair-frame', ticket, '--allow-physical'])

    def read(self, ticket, recipe):
        import secrets
        need(self.pending.is_file() and self.pending.stat().st_size < tasks.MAX_FILE_BYTES, 'menu_repair_expired')
        record = json.loads(self.pending.read_text(encoding='utf8'))
        need((ticket is None or secrets.compare_digest(record['ticket'], ticket)) and time.time() < record['expires'],
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
    need(matches_identity(owner, owner_id), 'menu_repair_window_changed')
    ctx = step.get('window_context')
    binding = flow.bind(ctx['exe'], ctx['title'], surface_id[0]) if ctx else owner
    need(matches_identity(binding, surface_id), 'menu_repair_window_changed')
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


def parameter_args(args, overrides):
    """Keep CLI spelling in the reply without storing variable values in a ticket."""
    supplied = getattr(args, 'set', None)
    if supplied is None or tasks.parse_overrides(supplied) != overrides:
        supplied = [key + '=' + value for key, value in overrides.items()]
    return [token for item in supplied for token in ('--set', item)]


def frame_reply(observed, record, args, overrides):
    observed = dict(observed)
    recipe_path = getattr(args, 'recipe', None)
    if recipe_path is not None and 'resume_args' in observed:
        runner = Path(recipe_path).resolve().parent / 'scripts' / 'run.py'
        if runner.is_file():
            observed['resume_script'] = str(runner)
    if digest(overrides) == record['variables']:
        if 'resume_args' in observed:
            observed['resume_args'] = [*observed['resume_args'], *parameter_args(args, overrides)]
    else:
        # Standalone inspection still needs no variables. Their plaintext is
        # deliberately absent from the ticket, so this reply cannot recover it.
        observed['resume_requires_original_set'] = True
    return observed


def reserve_surface(stack, recipe, record):
    """Use the same declared-window and live-handle exclusions for every frame."""
    from win_bridge import normalize_hwnd
    contexts = coordination.scopes_for(recipe['steps'], recipe['exe'])
    owner = dict(hwnd=normalize_hwnd(record['scope'][0][0]), exe_name=recipe['exe'])
    stack.enter_context(coordination.window(owner, recipe['window'], contexts))
    surface_hwnd = normalize_hwnd(record['scope'][1][0])
    if surface_hwnd != owner['hwnd']:
        stack.enter_context(coordination.handle({'hwnd': surface_hwnd}))


def attach_frame(flow, recipe, args, overrides, session, repair):
    repair['frame_args'] = [*repair['frame_args'], *parameter_args(args, overrides)]
    if not (getattr(args, 'auto_repair_frame', False) and getattr(args, 'allow_physical', False)
            and not getattr(args, 'background_only', False)):
        return
    try:
        need(flow.load(args.recipe) == recipe, 'workflow_changed_during_repair')
        record = session.read(repair['ticket'], recipe)
        with ExitStack() as stack:
            reserve_surface(stack, recipe, record)
            observed = inspect_or_adjust(flow, recipe, record)
        repair.update(frame_reply(observed, record, args, overrides))
        repair['host_action'] = 'inspect_menu_repair_image'
        if getattr(args, 'summary', False) and repair.get('image') and repair.get('resume_args'):
            # The host already has the frame. Keep the explicit capture command
            # for ticket-only replies and callers of the detailed interface.
            repair.pop('frame_args', None)
    except (OSError, ValueError, flow.app.bridge.BridgeError, flow.app.SelectionError) as error:
        # The completed run remains authoritative. A failed read-only capture
        # neither consumes its ticket nor replaces its original action receipt.
        repair['frame_capture'] = dict(status='unavailable', input_dispatched=False,
                                      diagnostic=getattr(error, 'code', 'menu_repair_frame_unavailable'))


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
        need(matches_identity(parent, row['parent']) and matches_identity(child, row['child']),
             'menu_repair_window_changed')
        if record.get('detail'):
            need(child.pid == parent.pid and flow.app.bridge.owned(child.hwnd, parent.hwnd),
                 'detail_window_owner_changed')
            cookies = record['window_cookies']
            need(len(cookies) == 2 and all(type(v) is int and v > 0 for v in cookies), 'invalid_detail_continuation')
            routes.detail_lifetimes.update(zip((parent.hwnd, child.hwnd), cookies))
        routes.openers[opener] = parent
        routes.check(spec, child)
        routes.bindings[flow.window_context._key(spec)] = child


def detail_checkpoint(routes, owner, report, only):
    """Offer only a confirmed opened window and its still-unsent detail suffix."""
    rows = report.get('steps', [])
    if not rows:
        return None
    last = rows[-1]
    number = report.get('stopped_at')
    if number is None:
        if only is None or last['receipt'].get('verification_scope') != 'recorded_window_opened':
            return None
        number = last['number'] + 1
    elif (last['number'] != number or last['receipt'].get('input_dispatched') is not False
          or report['status'] == 'uncertain'):
        return None
    remaining = routes.steps[number-1:]
    if not remaining:
        return None
    spec = remaining[0].get('window_context') or {}
    opener = spec.get('after_step')
    if not opener or any('popup' in step or step.get('window_context') != spec or
            not (step['action'] == 'read-value' or step['action'] == 'invoke' and step.get('verify_closed'))
            for step in remaining):
        return None
    proof = routes.outcomes.get(opener, {})
    if (routes.steps[opener-1]['action'] != 'double-click-result' or
            proof.get('verification_scope') != 'recorded_window_opened' or proof.get('input_dispatched') is not True):
        return None
    saved = saved_routes(routes, number)
    if len(saved) != 1:
        return None
    child, parent = saved[0]['child'], saved[0]['parent']
    if child[1] != parent[1] or not routes.app.bridge.owned(child[0], parent[0]):
        return None
    import window_context
    cookies = [window_context.window_cookie(routes.app.bridge, hwnd, create=True) for hwnd in (parent[0], child[0])]
    if not all(cookies):
        return None
    return dict(detail=True, number=number, scope=[identity(owner), child], routes=saved, checked_fields=[], window_cookies=cookies,
                only=None, query_sent=False, opener_step=opener, verification_scope='recorded_window_opened')


def header_stop(report, recipe):
    """Inspect the failed header or its dependent detail without resending a query."""
    number = report.get('stopped_at')
    if (report.get('status') not in ('blocked', 'uncertain', 'mismatch') or report.get('input_dispatched') is False
            or type(number) is not int or not 1 <= number <= len(recipe['steps'])):
        return None
    step = recipe['steps'][number - 1]
    opener = step.get('window_context', {}).get('after_step', number)
    if recipe['steps'][opener - 1].get('result_row') == 'header':
        return dict(host_action='inspect_current_state_without_replaying',
                    inspect_args=['--read-only', '--only', str(number), '--summary'])


def compact_stop(report):
    if report.get('host_action') != 'inspect_current_state_without_replaying':
        return report
    result = {k: v for k, v in report.items() if k not in ('input_requirements', 'inspection_owner', 'state_inspection')}
    result['steps'] = [dict({k: v for k, v in row.items() if k != 'receipt'},
                            **{k: v for k, v in row.get('receipt', {}).items()
                               if k in ('status', 'diagnostic', 'input_dispatched', 'verification_scope')})
                       for row in report.get('steps', [])]
    return result


def run_registered(flow, recipe, args, overrides, scene_session=None, *, enable_result_review=True,
                   allow_recorded_selection=False):
    if (enable_result_review and not args.repair_frame
            and any(s['action'] == 'double-click-result' or 'result_column' in s for s in recipe['steps'])):
        import registered_result
        return registered_result.run_attempt(flow, recipe, args, overrides,
                    lambda session: _run_registered(flow, recipe, args, overrides, scene_session, locked_session=session))
    return _run_registered(flow, recipe, args, overrides, scene_session,
                           enable_result_review=enable_result_review, allow_recorded_selection=allow_recorded_selection)


def _run_registered(flow, recipe, args, overrides, scene_session=None, *, enable_result_review=True,
                    allow_recorded_selection=False, locked_session=None):
    """One worker owns the stored ticket until its checked input has been consumed."""
    import review_drafts
    if args.hwnd is not None:
        args.hwnd = flow.app.bridge.normalize_hwnd(args.hwnd)
        need(args.hwnd, 'invalid_target')
    session = locked_session or RepairSession(args.recipe)
    resume_input = getattr(args, 'resume_input', None)
    remember = getattr(args, 'remember_physical', False)
    with ExitStack() as stack:
        try:
            if locked_session is None:
                stack.enter_context(coordination.recipe(args.recipe))
            # Retain exclusion with older installations using this ticket directory.
            stack.enter_context(review_drafts._edit_lock(session.directory))
        except coordination.CoordinationError as error:
            return dict(status='blocked', diagnostic=error.code, input_dispatched=False)
        except OSError:
            return dict(status='blocked', diagnostic='registered_workflow_busy', input_dispatched=False)
        resume = None
        replacements = {}
        result_review = None
        surface_options = {'background_only': True} if getattr(args, 'background_only', False) else {}
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
            if getattr(args, 'repeat_approval_used', False):
                need(approved(args.recipe, current), 'registered_physical_consent_required')
            if surface_options.get('background_only'):
                refusal = input_policy.blocked(input_policy.profile([s for _, s in flow.selected_steps(current, args.only)]))
                if refusal:
                    # A read-only reminder can reuse an outstanding ticket without consuming it.
                    if session.pending.is_file():
                        try:
                            prior = session.read(None, current)
                            if (not prior.get('pause') and prior['variables'] == digest(overrides)
                                    and prior['only'] == args.only and (args.hwnd is None
                                        or args.hwnd == flow.app.bridge.normalize_hwnd(prior['scope'][0][0]))):
                                refusal['menu_repair'] = session.describe(prior)
                        except (OSError, ValueError, KeyError, IndexError, TypeError):
                            pass
                    return refusal
            approval_before = approval_state(args.recipe) if remember else None
            recipe = run_recipe = current
            result_ticket = getattr(args, 'confirm_result_locally', None)
            local_review = getattr(args, 'local_result_review', False)
            header_run = not args.only and any(s.get('result_row') == 'header' for s in recipe['steps'])
            if header_run:
                import first_trial
                stop_path = session.directory / 'header-stop.json'
                stop_key = digest([recipe, {k.casefold(): v for k, v in overrides.items()}])
                if stop_path.exists() and not getattr(args, 'new_attempt', False):
                    previous = first_trial._previous(stop_path)
                    need('key' in previous and isinstance(previous.get('stop'), dict), 'header_stop_unreadable')
                    if (previous.get('key') == stop_key and
                            (args.hwnd is None or previous.get('hwnd') in (None, args.hwnd))):
                        return dict(previous['stop'], status='blocked', input_dispatched=False, steps=[],
                                    previous_input_dispatched=previous['prior_input'], prior_steps_replayed=False)
            if (enable_result_review and not args.only and not args.repair_frame and not args.resume_menu and not resume_input
                    and any(('result_column' in s or 'result_row' in s) and s.get('result_row') != 'header'
                            for s in recipe['steps'])):
                import registered_result
                result_review = registered_result.ResultReview(session, recipe, args.recipe)
                if registered_result.eligible(recipe):
                    existing = result_review.existing(overrides, result_ticket, args.hwnd)
                    if existing:
                        if (local_review or result_ticket) and not existing.get('claimed'):
                            return result_review.confirm(flow, existing, args.recipe, overrides, try_elements=True)
                        return result_review.feedback(existing, overrides=overrides)
                hint = result_review.hint()
                if hint:
                    try:
                        flow.checked_result_hints(recipe, hint)
                    except (ValueError, flow.app.bridge.BridgeError):
                        pass  # An obsolete cache can always be rebuilt from the recording.
                    else:
                        surface_options['result_surface_hint'] = hint
                surface_options['remember_result_elements'] = True
            need(not result_ticket, 'result_review_expired')
            if args.repair_frame or args.resume_menu or resume_input:
                resume = session.read(args.repair_frame or args.resume_menu or resume_input, recipe)
                need(bool(resume.get('pause')) == bool(resume_input), 'wrong_continuation_type')
                if args.resume_menu or resume_input:
                    active = [step for number, step in flow.selected_steps(recipe, resume['only'])
                              if number >= resume['number']]
                    checks = recipe['steps'] if resume['only'] is not None else ()
                    stack.enter_context(coordination.reserve_outputs(active, verify_steps=checks))
                # Hold the same window scopes through inspection and the
                # resumed operation. flow.run reenters them on this thread.
                reserve_surface(stack, recipe, resume)
                if args.repair_frame:
                    return frame_reply(inspect_or_adjust(flow, recipe, resume), resume, args, overrides)
                need(digest(overrides) == resume['variables'], 'menu_repair_parameters_changed')
                if not resume_input:
                    point = inspect_or_adjust(flow, recipe, resume, args.frame, args.point)
                    run_recipe = deepcopy(recipe)
                    replacements[resume['number']] = point
                    replace_menu(run_recipe['steps'][resume['number'] - 1], point)
                remember = resume.get('remember', False) and resume['only'] is None
                approval_before = resume.get('approval_before')
            session.discard()
        except (OSError, ValueError, flow.app.bridge.BridgeError, flow.app.SelectionError) as error:
            return dict(status='blocked', diagnostic=getattr(error, 'code', 'menu_repair_unavailable'),
                        input_dispatched=False)
        started = time.monotonic()
        report = flow.run(run_recipe, overrides, hwnd=resume['scope'][0][0] if resume else args.hwnd,
                          only=resume['only'] if resume else args.only, track_menu=True, resume=resume,
                          scene_session=scene_session, allow_recorded_selection=allow_recorded_selection, **surface_options)
        if header_run:
            stop = header_stop(report, recipe)
            if stop:
                report.update(stop)
                try:
                    hwnd = report.get('inspection_owner', {}).get('hwnd', args.hwnd)
                    first_trial._write_receipt(stop_path, dict(status='blocked', key=stop_key, hwnd=hwnd,
                        stop=dict(stop, diagnostic=report.get('diagnostic'), stopped_at=report['stopped_at']),
                        prior_input=report['input_dispatched']))
                except (OSError, ValueError):
                    report['repair_storage'] = dict(status='not_saved', diagnostic='header_stop_not_saved')
                return report
            if report.get('status') in ('completed_unverified', 'verified') and not report.get('stopped_at'):
                stop_path.unlink(missing_ok=True)
        if result_review and not resume:
            learned = [dict(row['receipt']['result_element'], number=row['number']) for row in report.get('steps',[])
                       if row.get('receipt',{}).get('result_element') and row['receipt']['status'] == 'verified']
            try:
                result_review.remember_elements(learned)
            except (OSError, ValueError, flow.app.tasks.TaskError):
                report['result_surface_hint_saved'] = False
            try:
                record = result_review.offer(flow, report, overrides, remember=remember, approval_before=approval_before)
                if record:
                    follow = result_review.feedback(record, overrides=overrides)
                    report.update(host_action=follow['host_action'], result_review=follow['result_review'])
                    if local_review:
                        continuation = result_review.confirm(flow, record, args.recipe, overrides)
                        continuation['elapsed_seconds'] = round(time.monotonic() - started, 4)
                        if continuation.get('resumed_at'):
                            prefix = report['steps'][:-1]
                            if continuation.get('result_match_source') == 'local_visual_confirmation':
                                observed = report['steps'][-1]['receipt']
                                observed.update(status='verified', provider_diagnostic=observed['diagnostic'],
                                                diagnostic='result_confirmed_on_local_screen',
                                                value_evidence=dict(source='local_visual_confirmation'))
                                prefix = report['steps']
                            continuation['steps'] = prefix + continuation['steps']
                            continuation['original_elapsed_seconds'] = report.get('elapsed_seconds')
                            continuation['input_dispatched'] = flow.combine(report['input_dispatched'], continuation['input_dispatched'])
                            continuation.pop('resumed_at')
                            if continuation['status'] == 'verified' and any(
                                    row['receipt']['status'] == 'delivered_unverified' for row in report['steps']):
                                continuation['status'] = 'completed_unverified'
                            return continuation
                        if continuation.get('input_dispatched') is None:
                            continuation['steps'] = report['steps']
                            continuation['previous_input_dispatched'] = report['input_dispatched']
                            return continuation
                        report['review_diagnostic'] = continuation.get('diagnostic')
                        if continuation.get('result_geometry'):
                            report['result_geometry'] = continuation['result_geometry']
                        report['original_elapsed_seconds'] = report.get('elapsed_seconds')
                        report['elapsed_seconds'] = continuation['elapsed_seconds']
                    return report
            except (OSError, ValueError, flow.app.bridge.BridgeError, flow.app.SelectionError) as error:
                report['review_diagnostic'] = getattr(error, 'code', 'result_review_unavailable')
                return report
        if resume:
            report['resumed_at'] = resume['number']
        scope = report.pop('menu_repair_scope', None)
        routes = report.pop('menu_repair_routes', None)
        try:
            updated = persist(args.recipe, recipe, report, flow.load, replacements)
            if remember:
                remember_test(args.recipe, updated, report, approval_before, resume['number'] if resume else 1)
            if (resume and report.get('status') == 'blocked' and report.get('input_dispatched') is False
                    and report.get('steps') == [] and report.get('diagnostic') == 'window_root_unreadable'
                    and report.get('readability_check', {}).get('status') == 'unavailable'
                    and report['readability_check'].get('diagnostic') == 'window_root_unreadable'):
                # The claim was consumed before entering the runner. Restore it only
                # for a positive, pre-input transient receipt on the identical live scope.
                need(flow.load(args.recipe) == recipe, 'workflow_changed_during_repair')
                recorded_surface(flow, recipe, resume)
                continuation = session.write(resume)
                if resume.get('pause'):
                    report['input_pause'] = frame_reply(continuation, resume, args, overrides)
                else:
                    report['menu_repair'] = continuation
                    attach_frame(flow, recipe, args, overrides, session, continuation)
            if scope:
                number = report['stopped_at']
                row = next(r for r in report['steps'] if r['number'] == number)
                # A scope alone is not proof that another menu input is safe.
                # Refuse stale or malformed scopes after uncertain/leaf delivery.
                pause = input_paused(row['receipt'])
                if not pause and not point_failed(row['receipt']):
                    return report
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
                if remember:
                    prior_rows = [r for r in report['steps'] if r['number'] < number]
                    remember = tested_rows(recipe, prior_rows, resume['number'] if resume else 1, number)
                continuation = session.start(updated, overrides, number, scope, only, fields, query_sent, routes,
                                             pause=pause, remember=remember, approval_before=approval_before)
                if pause:
                    report['input_pause'] = frame_reply(continuation, {'variables': digest(overrides)}, args, overrides)
                else:
                    report['menu_repair'] = continuation
                    attach_frame(flow, updated, args, overrides, session, continuation)
        except (OSError, ValueError, flow.app.bridge.BridgeError) as error:
            # The actual menu/leaf receipt remains authoritative even if the disk write failed.
            report['repair_storage'] = dict(status='not_saved',
                                            diagnostic=getattr(error, 'code', 'menu_repair_storage_unavailable'))
        return report
