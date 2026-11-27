"""Continue an existing registered lookup after its operator identifies a locally drawn cell."""
from copy import deepcopy
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time

import grounding
import learned_tasks as tasks
import result_trial
import workflow_repair as repair


def eligible(recipe):
    return any(result_trial.supported(recipe, n) for n in range(1, len(recipe['steps']) + 1))


def _variables(overrides):
    return repair.digest({name.casefold(): value for name, value in overrides.items()})


def _clear_result_state(directory):
    for name in ('result-review.json', 'header-stop.json', 'pending.json'):
        path = directory / name
        tasks._deny_redirect(path)
        if name == 'result-review.json' and path.is_file():
            try:
                _discard_preview(ResultReview._read(path))
            except (ValueError, OSError):
                pass  # Explicit restart discards an unreadable pending result as well.
        path.unlink(missing_ok=True)


def _discard_preview(record):
    token = record.get('preview')
    if isinstance(token, str) and re.fullmatch('[a-f0-9]{64}', token):
        try:
            path = grounding._directory() / (token + '.png')
            tasks._deny_redirect(path)
            path.unlink(missing_ok=True)
        except (OSError, tasks.TaskError):
            pass  # A viewer may still hold the file. Normal frame expiry cleans it up later.


def cancel_run(path, run_id):
    """Cancel local waiting state only. An old ID cannot cancel its replacement."""
    import first_trial
    with repair.coordination.recipe(path):
        directory = repair.RepairSession(path).directory
        state_path = directory / 'result-attempt.json'
        state = first_trial._previous(state_path)
        if state.get('run_id') != run_id or state['status'] in ('finished', 'cancelled'):
            return dict(status='blocked', diagnostic='result_run_not_pending', input_dispatched=False)
        _clear_result_state(directory)
        first_trial._write_receipt(state_path, dict(status='cancelled', run_id=run_id, recipe=state['recipe'],
                                                    finished=time.time()))
        return dict(status='cancelled', run_id=run_id, input_dispatched=False)


def run_attempt(flow, recipe, args, overrides, execute):
    """Keep only the latest registered result attempt. Claim it before entering the worker action."""
    import first_trial
    entered = getattr(args, '_requested_at', None) or time.time()
    params = repair.parameter_args(args, overrides)
    new_args = ['--new-attempt', '--summary', *params]
    if getattr(args, 'background_only', False):
        new_args.append('--background-only')
    if args.hwnd is not None:
        new_args.extend(['--hwnd', str(args.hwnd)])
    fingerprint = repair.digest(recipe)
    with ExitStack() as stack:
        try:
            stack.enter_context(repair.coordination.recipe(args.recipe))
            session = repair.RepairSession(args.recipe)
        except (OSError, ValueError) as error:
            return dict(status='blocked', diagnostic=getattr(error, 'code', 'result_attempt_unavailable'),
                        input_dispatched=False)
        directory = session.directory
        path = directory / 'result-attempt.json'
        state = first_trial._previous(path) if path.exists() else {}
        restart = getattr(args, 'new_attempt', False)
        if not state and not restart and (directory / 'result-review.json').exists():
            legacy = ResultReview._read(directory / 'result-review.json')
            state = dict(status='review', run_id=secrets.token_hex(16), recipe=legacy['recipe'],
                         variables=legacy['variables'], hwnd=legacy['window']['hwnd'], expires=legacy['expires'])
            first_trial._write_receipt(path, state)
        run_id = state.get('run_id')
        active = bool(run_id and state['status'] not in ('cancelled', 'finished'))
        detail_token = getattr(args, 'resume_detail', None)
        review_token = getattr(args, 'confirm_result_locally', None)
        menu_token = getattr(args, 'resume_menu', None) or getattr(args, 'resume_input', None)

        def recovery():
            return dict(cancel_args=['--cancel-run', run_id], new_attempt_args=new_args)

        def blocked(diagnostic):
            return dict(status='blocked', run_id=run_id, diagnostic=diagnostic, steps=[],
                        input_dispatched=False, prior_steps_replayed=False, **recovery())

        if state and not run_id and not restart:
            return dict(status='blocked', diagnostic='result_attempt_state_unreadable',
                        input_dispatched=False, new_attempt_args=new_args)
        if (state.get('finished', 0) >= entered and state.get('recipe') == fingerprint
                and state.get('variables') == _variables(overrides) and state.get('only') == args.only
                and not (detail_token or review_token or menu_token)):
            return dict(status='blocked', diagnostic='concurrent_result_run_finished', run_id=run_id,
                        input_dispatched=False, prior_steps_replayed=False, steps=[])
        if restart:
            _clear_result_state(directory)
            active = False
        if active and state['recipe'] != fingerprint:
            return blocked('pending_result_workflow_changed')
        hwnd = flow.app.bridge.normalize_hwnd(args.hwnd) if args.hwnd is not None else None
        repair.need(args.hwnd is None or hwnd, 'invalid_target')
        if active and (hwnd is not None and state.get('hwnd') not in (None, hwnd)
                       or not detail_token and overrides and state['variables'] != _variables(overrides)):
            return blocked('pending_result_parameters_or_window_changed')
        detail = state.get('detail') if active else None
        if detail_token and not (detail and detail['ticket'] == detail_token):
            return dict(status='blocked', diagnostic='detail_resume_expired', input_dispatched=False, steps=[])
        if detail:
            notice = blocked('detail_read_or_close_pending')
            if time.time() >= detail['expires']:
                return dict(notice, diagnostic='detail_resume_expired')
            if not detail_token:
                notice['next_action'] = dict(kind='resume_detail', args=['--resume-detail', detail['ticket'], '--summary'])
                return notice
        elif active and state['status'] == 'menu':
            if not menu_token:
                notice = blocked('result_menu_continuation_pending')
                try:
                    prior = session.read(None, recipe)
                except (ValueError, OSError):
                    return notice
                follow = repair.frame_reply(session.describe(prior), prior, args, overrides)
                return dict(notice, host_action=follow['host_action'],
                            **{('input_pause' if prior.get('pause') else 'menu_repair'): follow})
        elif active and state['status'] != 'review':
            return dict(blocked(state.get('diagnostic') or 'result_run_interrupted'),
                        previous_input_dispatched=state.get('prior_input'),
                        **state.get('handoff', {}))
        if active and state['status'] == 'review':
            if time.time() >= state.get('expires', 0) or not (directory / 'result-review.json').is_file():
                return blocked('result_review_expired')
            if args.only is not None or menu_token:
                return blocked('result_review_pending')
        if not active and review_token:
            # May be a ticket issued by an earlier installation. The review reader
            # verifies the ticket before it can enter the action path.
            if not (directory / 'result-review.json').is_file():
                return dict(status='blocked', diagnostic='result_review_expired', input_dispatched=False)
        if not active:
            run_id = secrets.token_hex(16)
            state = dict(run_id=run_id, recipe=fingerprint, variables=_variables(overrides), hwnd=hwnd, only=args.only)
        core = {k: state.get(k) for k in ('run_id', 'recipe', 'variables', 'hwnd', 'only')}
        if detail_token or not active:
            first_trial._write_receipt(path, dict(core, status='running'))
        if detail_token:
            # Only a proven read/close suffix is resumable. Never run an opener
            # or accept parameters for its already-executed prefix on this path.
            repair.need(flow.load(args.recipe) == recipe, 'registered_workflow_changed')
            suffix = recipe['steps'][detail['number']-1:]
            repair.need(suffix and all('popup' not in s and s.get('window_context') == detail['routes'][0]['spec']
                       and (s['action'] == 'read-value' or s['action'] == 'invoke' and s.get('verify_closed'))
                       for s in suffix), 'invalid_detail_continuation')
            report = flow.run(recipe, {}, hwnd=detail['scope'][0][0], resume=detail)
            report.update(resumed_at=detail['number'], prior_steps_replayed=False, opener_step=detail['opener_step'],
                          opener_receipt=dict(status='verified', input_dispatched=True,
                                              verification_scope=detail['verification_scope']))
        else:
            report = execute(session)
        report['run_id'] = run_id
        checkpoint = report.pop('_detail_checkpoint', None)
        review = report.get('result_review')
        if checkpoint:
            checkpoint.update(ticket=secrets.token_hex(32), expires=time.time() + grounding.TTL)
            report.update(next_action=dict(kind='resume_detail', args=['--resume-detail', checkpoint['ticket'], '--summary']),
                          **recovery())
            saved = dict(status='detail', detail=checkpoint)
        elif session.pending.exists() and (report.get('menu_repair') or report.get('input_pause')
                or active and state['status'] == 'menu'):
            saved = dict(status='menu')
            if report.get('menu_corrections'):
                core['recipe'] = repair.digest(flow.load(args.recipe))
            report.update(**recovery())
        elif review:
            # A failed/mistyped resume must never discard a live review ticket.
            pending = ResultReview._read(directory / 'result-review.json')
            saved = dict(status='review', expires=pending['expires'])
            if getattr(args, 'include_result_frame', False):
                review['frame'] = ResultReview(session, recipe, args.recipe).preview(flow, pending)
            report.update(next_action=dict(kind='confirm_result', args=review['resume_args']), **recovery())
        elif active and state['status'] == 'review' and (directory / 'result-review.json').is_file():
            saved = dict(status='review', expires=state['expires'])
            report.update(**recovery())
        elif report.get('status') == 'uncertain' or (detail_token and report.get('status') != 'verified'
                or report.get('stopped_at') and report.get('input_dispatched') is not False):
            saved = dict(status='stopped', diagnostic=report.get('diagnostic'), prior_input=report.get('input_dispatched'),
                         handoff={k: report[k] for k in ('host_action','inspect_args','stopped_at') if k in report})
            report.update(**recovery())
        else:
            saved = dict(status='finished')
        first_trial._write_receipt(path, dict(core, **saved, finished=time.time()))
        return report


class ResultReview:
    """Per registration. The caller holds its recipe lock; no desktop lock is held during review."""
    def __init__(self, session, recipe, recipe_path):
        self.recipe = recipe
        self.fingerprint = repair.digest(recipe)
        self.pending = session.directory / 'result-review.json'
        self.temporary_hint = session.directory / 'result-surface.json'
        skill = Path(recipe_path).absolute().parent
        if (skill.parent.name.casefold() == 'skills' and skill.parent.parent.name.casefold() == '.claude'
                and skill.parent.parent.parent != Path.home()):
            root = skill.parent.parent.parent / '.desktop-select' / 'result-elements' / skill.name
        else:
            local = Path(os.environ.get('LOCALAPPDATA') or (Path.home() / 'AppData/Local'))
            root = local / 'DesktopSelect/result-elements' / repair.digest(os.path.normcase(str(skill)))
        self.hint_path = root / 'result-surface.json'
        self.hint_origin = None

    @staticmethod
    def _read(path):
        tasks._deny_redirect(path)
        if not path.exists():
            return {}
        repair.need(path.is_file() and path.stat().st_nlink == 1 and path.stat().st_size <= tasks.MAX_FILE_BYTES,
                    'invalid_result_review')
        with path.open(encoding='utf8') as stream:
            record = json.load(stream, object_pairs_hook=tasks._unique_keys)
        repair.need(isinstance(record, dict), 'invalid_result_review')
        return record

    @staticmethod
    def _write(path, record):
        tasks._deny_redirect(path)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False).encode())
            os.replace(temporary, path)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def existing(self, overrides, ticket, hwnd=None):
        from win_bridge import normalize_hwnd
        record = self._read(self.pending)
        active = (record and record['recipe'] == self.fingerprint and record['expires'] > time.time())
        if ticket:
            repair.need(active and re.fullmatch('[a-f0-9]{64}', ticket)
                        and secrets.compare_digest(record['ticket'], ticket), 'result_review_expired')
            repair.need(not overrides or record['variables'] == _variables(overrides), 'result_review_parameters_changed')
            return record
        if hwnd is not None:
            hwnd = normalize_hwnd(hwnd)
            repair.need(hwnd, 'invalid_target')
        if record:
            repair.need(active, 'result_review_expired')
            repair.need(record['variables'] == _variables(overrides) and (
                        hwnd is None or hwnd == normalize_hwnd(record['window']['hwnd'])),
                        'pending_result_parameters_or_window_changed')
            return record
        return None

    def hint(self):
        for path in (self.hint_path, self.temporary_hint):
            try:
                tasks._directory(path.parent)
                record = self._read(path)
            except (OSError, ValueError, tasks.TaskError):
                continue
            if record.get('recipe') == self.fingerprint:
                self.hint_origin = path
                return record.get('hints') or record.get('hint')
        self.hint_origin = None
        return None

    def remember_elements(self, changes):
        if not changes:
            return
        previous = self.hint()
        hints = previous if isinstance(previous, list) else [previous] if previous else []
        merged = {h['number']: h for h in hints if isinstance(h, dict) and type(h.get('number')) is int}
        if self.hint_origin == self.hint_path and all(merged.get(h['number']) == h for h in changes):
            return
        merged.update((h['number'], h) for h in changes)
        items = [merged[n] for n in sorted(merged)]
        record = dict(recipe=self.fingerprint, hints=items) if len(items) > 1 else dict(recipe=self.fingerprint, hint=items[0])
        try:
            tasks._directory(self.hint_path.parent).mkdir(parents=True, exist_ok=True)
            self._write(self.hint_path, record)
        except (OSError, tasks.TaskError):
            # Portable/read-only installations may still use this worker's local cache.
            if self.hint_origin != self.temporary_hint or any(h not in hints for h in changes):
                self._write(self.temporary_hint, record)

    def offer(self, flow, report, overrides, *, remember=False, approval_before=None):
        number = report.get('stopped_at')
        rows = report.get('steps', [])
        if (report.get('status') != 'blocked' or not rows or rows[-1]['number'] != number
                or not result_trial.supported(self.recipe, number)):
            return None
        receipt = rows[-1]['receipt']
        step = self.recipe['steps'][number - 1]
        row_policy = step.get('result_row')
        image_errors = result_trial.ROW_REVIEW_ERRORS if row_policy else flow.RESULT_IMAGE_ERRORS
        if (not receipt.get('result_window') or receipt['input_dispatched'] is not False
                or receipt.get('diagnostic') not in image_errors):
            return None
        ticket = secrets.token_hex(32)
        record = dict(ticket=ticket, recipe=self.fingerprint, number=number, window=receipt['result_window'],
                      variables=_variables(overrides),
                      prior_input=report['input_dispatched'], expires=time.time() + grounding.TTL)
        if row_policy:
            record['row_policy'] = row_policy
        else:
            values, _ = flow.values_for(self.recipe, overrides)
            expected = values[step['parameter'].casefold()] if 'parameter' in step else step['value']
            record['expected'] = repair.digest([ticket, expected])
        if receipt.get('result_surface_selector'):
            record['selector'] = receipt['result_surface_selector']
        if remember and repair.tested_rows(self.recipe, rows[:-1], 1, number):
            record.update(remember=True, approval_before=approval_before)
        self._write(self.pending, record)
        return record

    def feedback(self, record, diagnostic='result_requires_local_confirmation', overrides=None):
        if record.get('claimed'):
            return dict(status='uncertain', diagnostic='result_resume_already_claimed', steps=[],
                        host_action='check_current_effect_before_continuing', input_dispatched=None,
                        prior_steps_replayed=False, this_call_dispatched_input=False)
        args = ['--confirm-result-locally', record['ticket'], '--allow-physical', '--summary']
        for name, value in sorted((k.casefold(), v) for k, v in (overrides or {}).items()):
            args.extend(['--set', f'{name}={value}'])
        return dict(status='blocked', diagnostic=diagnostic, stopped_at=record['number'], steps=[],
                    host_action='confirm_result_locally_without_replaying', input_dispatched=False,
                    previous_input_dispatched=record['prior_input'], prior_steps_replayed=False,
                    result_review=dict(step=record['number'], expires_seconds=max(0, int(record['expires'] - time.time())),
                                       resume_args=args))

    def preview(self, flow, record):
        """Expose only a masked crop of an identified result surface. It never grants click authority."""
        owner = record['window']
        step = self.recipe['steps'][record['number']-1]
        try:
            repair.need(record.get('recipe') == self.fingerprint and not record.get('claimed'), 'result_review_unavailable')
            flow.app.bridge.set_dpi_awareness()
            binding = flow.app.bridge.Binding(owner)
            def guard():
                repair.need(time.time() < record['expires'], 'result_review_expired')
                flow.app.guard(binding, binding, owner['title'])
                flow.app.visibility.require_observable(binding)
                repair.need(owner.get('window_cookie') and
                            flow.window_context.window_cookie(flow.app.bridge, binding.hwnd) == owner['window_cookie'],
                            'result_window_lifetime_unconfirmed')
            with flow.coordination.window(owner, owner['title'], []), flow.app.bridge.UIA() as uia:
                guard()
                table, selector = flow._result_target(binding, uia, step['selector'], record.get('selector'),
                                                       column=step.get('result_column'))
                bounds = dict(uia.bounds(table))
                frame, png = grounding._capture(flow.app, binding, guard, table_box=bounds)
                current, _ = flow._result_target(binding, uia, selector, None, column=step.get('result_column'))
                repair.need(uia.same(current, table) and uia.bounds(table) == bounds, 'result_table_changed')
                guard()
            scope = ('registered-result-preview/' + record['ticket']).encode('ascii')
            token, path = grounding.store_frame(grounding._directory(), scope, png)
            if record.get('preview') != token:
                _discard_preview(record)
                self._write(self.pending, dict(record, preview=token))
            return dict(status='observed', scope='visible_result_table', image=str(path), frame=token,
                        width=frame[2], height=frame[3], expires_seconds=max(0, int(record['expires'] - time.time())),
                        input_dispatched=False)
        except (OSError, ValueError, flow.app.bridge.BridgeError, flow.app.SelectionError) as error:
            return dict(status='unavailable', diagnostic=getattr(error, 'code', 'result_frame_unavailable'),
                        input_dispatched=False)

    def confirm(self, flow, record, recipe_path, overrides, *, try_elements=False):
        """Keep an uncertain click claimed even if the worker or the provider failed."""
        started = time.monotonic()
        try:
            owner = record['window']
            scopes = flow.coordination.scopes_for(self.recipe['steps'][record['number']:], owner['exe_name'])
            with flow.coordination.window(owner, owner['title'], scopes):
                reply = self._confirm(flow, record, recipe_path, overrides, try_elements)
        except (OSError, ValueError, flow.coordination.CoordinationError,
                flow.app.bridge.BridgeError, flow.app.SelectionError) as error:
            current = self._read(self.pending)
            reply = self.feedback(current or record, getattr(error, 'code', 'result_review_unavailable'))
            if getattr(error, 'result_geometry', None):
                reply['result_geometry'] = error.result_geometry
        if overrides and reply.get('result_review'):
            reply['result_review'] = self.feedback(record, overrides=overrides)['result_review']
        reply['elapsed_seconds'] = round(time.monotonic() - started, 4)
        return reply

    def _confirm(self, flow, record, recipe_path, overrides, try_elements):
        import local_result_review
        import result_cell
        owner = record['window']
        # A ticket can resume in a new worker that never bound the original window.
        # Set DPI awareness before Win32, UIA or Tk read coordinates.
        flow.app.bridge.set_dpi_awareness()
        binding = flow.app.bridge.Binding(owner)
        def guard():
            flow.app.guard(binding, binding, owner['title'])
            flow.app.visibility.require_observable(binding)

        number, key = record['number'], record['ticket']
        step = self.recipe['steps'][number-1]
        original_selector = step['selector']
        hint = record.get('selector')
        row_policy = step.get('result_row')
        repair.need(not row_policy or record.get('row_policy') == row_policy, 'result_row_policy_changed')
        values, _ = flow.values_for(self.recipe, overrides)
        expected = None
        if not row_policy:
            expected = values.get(step['parameter'].casefold()) if 'parameter' in step else step['value']
        path = None
        local_review_seconds = 0
        geometry = {}
        try:
            while True:
                repair.need(time.time() < record['expires'], 'result_review_expired')
                if try_elements:
                    result = self._try_elements(flow, binding, guard, record, recipe_path, step, expected)
                    if result is not None:
                        return result
                try_elements = True  # A refresh may expose data that was unavailable before.
                table_box = None
                recorded_box = None
                selector = original_selector
                group_boxes = None
                geometry = {}
                with flow.app.bridge.UIA() as uia:
                    try:
                        table, selector = flow._result_target(binding, uia, original_selector, hint,
                                                             column=step.get('result_column'))
                    except (ValueError, flow.app.bridge.BridgeError) as error:
                        if row_policy or selector != {'role':'table'} or getattr(error, 'code', None) != 'result_table_missing':
                            raise
                        group_boxes = {}
                    else:
                        recorded_box = dict(uia.bounds(table))
                    rect, png = grounding._capture(flow.app, binding, guard, group_boxes=group_boxes)
                    geometry['capture_rect'] = list(rect)
                    if group_boxes is None:
                        repair.need(uia.bounds(table) == recorded_box, 'result_table_changed')
                        geometry['table_absolute'] = recorded_box
                        geometry['table_relative'] = [recorded_box['x'] - rect[0], recorded_box['y'] - rect[1],
                                                      recorded_box['width'], recorded_box['height']]
                        visible = result_trial.visible_table_box(rect, recorded_box)
                        table_box = [visible['x'] - rect[0], visible['y'] - rect[1], visible['width'], visible['height']]
                        geometry['visible_relative'] = table_box
                frame, path = grounding.store_frame(grounding._directory(), key.encode('ascii'), png)
                opened = time.monotonic()
                options = dict(row_policy=row_policy) if row_policy else {}
                choice = local_result_review.choose(path, step.get('result_column'), table_box=table_box,
                                                    expected=expected, frame_size=rect[2:], **options)
                local_review_seconds += time.monotonic() - opened
                if not choice or not choice.get('refresh'):
                    break
                path.unlink(missing_ok=True)
            if choice is None:
                return self.feedback(record, 'local_result_review_cancelled')
            x, y = choice['point']
            if row_policy:
                repair.need(choice.get('row_policy') == row_policy and
                            local_result_review.valid_row_choice(choice['point'], table_box, choice.get('confirmed')),
                            'result_row_not_confirmed')
            else:
                if repair.digest([key, choice['value']]) != record['expected']:
                    return self.feedback(record, 'confirmed_result_value_mismatch')
                left, head_y, right = choice['header']
                repair.need(0 <= left < right < rect[2] and left <= x <= right
                            and 0 <= head_y < y < rect[3], 'result_point_outside_column')
            repair.need(time.time() < record['expires'], 'result_review_expired')
            # Reviewing the snapshot never reserves desktop input. Only this
            # explicitly confirmed physical continuation needs the target exposed.
            with flow.coordination.desktop_input():
                guard()
                flow.row_action.foreground(binding)
                now, current = grounding._capture(flow.app, binding, guard)
                repair.need(now[2:] == rect[2:], 'result_frame_changed')
                head = None if row_policy else (now[0] + (right + left)//2, now[1] + head_y)
                point = (now[0]+x, now[1]+y)
                with flow.app.bridge.UIA() as uia:
                    if table_box is None:
                        selector, box = result_cell.bound_group(binding, uia, head, point)
                        key_type = 'automation_id' if 'automation_id' in selector else 'name'
                        recorded_box = group_boxes.get((key_type, selector[key_type]))
                    else:
                        table, current_selector = flow._result_target(binding, uia, original_selector, hint,
                                                                     column=step.get('result_column'))
                        repair.need(current_selector == selector, 'result_table_changed')
                        box = uia.bounds(table)
                    result_trial.check_table_layout(recorded_box, rect, box, now)
                    if table_box is not None and not row_policy:
                        self._check_header(flow, binding, uia, table, step['result_column'], head, point)
                    repair.need(result_trial.same_table_pixels(png, current, now, box), 'result_frame_changed')
                    if row_policy:
                        repair.need(box['x'] <= point[0] < box['x']+box['width']
                                    and box['y'] <= point[1] < box['y']+box['height'], 'result_point_outside_table')
                    else:
                        repair.need(box['x'] <= now[0]+left < now[0]+right < box['x']+box['width']
                                    and box['y'] <= head[1] < point[1] < box['y']+box['height'], 'result_point_outside_table')
                    flow.check_result_point(binding, uia, selector, point, reviewed_frame=now)
                    if row_policy == 'recorded':
                        import window_placement
                        placement = window_placement.capture(binding.hwnd, expected_rect=now)
            suffix = result_trial.visual_suffix(self.recipe, number, png, [x, y], reviewed=True)
            repair.need(flow.load(recipe_path) == self.recipe, 'workflow_changed_during_repair')
            confirmation = dict(owner=owner, frame=frame, receipt=key, point=[x,y], selector=selector,
                                window_rect=now, table_box=box)
            if original_selector != selector:
                confirmation['original_selector'] = original_selector
            self._write(self.pending, dict(record, claimed=True))
            result = flow.run(suffix, hwnd=owner['hwnd'], result_confirmation=confirmation)
            result['local_review_seconds'] = round(local_review_seconds,4)
            learned = dict(number=number, selector=selector) if not row_policy and selector.get('role') == 'group' else None
            tested_recipe = self.recipe
            if (row_policy == 'recorded' and result.get('input_dispatched') is True and not result.get('stopped_at')
                    and result.get('status') in ('verified', 'completed_unverified')):
                try:
                    revised = deepcopy(self.recipe)
                    revised['steps'][number-1].update(selector=selector, result_point=result_cell.body_point(
                        box, point, placement=placement))
                    flow.validate(revised)
                    repair.replace_workflow(recipe_path, self.recipe, revised, flow.load)
                    tested_recipe = revised
                    result['correction_saved'] = True
                except (ValueError, OSError, flow.app.bridge.BridgeError) as error:
                    result.update(correction_saved=False, correction_diagnostic=getattr(error, 'code', 'result_point_save_failed'))
            return self._finish(result, record, recipe_path, number - 1 if row_policy else number,
                                'local_row_confirmation' if row_policy else 'local_visual_confirmation', learned,
                                tested_recipe=tested_recipe)
        except (OSError, ValueError, flow.app.bridge.BridgeError, flow.app.SelectionError) as error:
            if geometry:
                error.result_geometry = dict(geometry, **getattr(error, 'geometry', {}))
            raise
        finally:
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass  # Frame expiry also removes local temporary images.

    def _try_elements(self, flow, binding, guard, record, recipe_path, step, expected):
        import result_cell
        owner = record['window']
        number, hint = record['number'], record.get('selector')
        if step.get('result_row'):
            with flow.app.bridge.UIA() as uia:
                probe = flow.observe_result_row(binding, uia, step, guard, wait_seconds=0)
            if probe['status'] == 'observed':
                suffix = result_trial.continuation(self.recipe, number)
                repair.need(flow.load(recipe_path) == self.recipe, 'workflow_changed_during_repair')
                self._write(self.pending, dict(record, claimed=True))
                result = flow.run(suffix, hwnd=owner['hwnd'], result_owner=owner)
                return self._finish(result, record, recipe_path, number-1, 'accessibility_row', None)
            if probe.get('diagnostic') not in result_trial.ROW_REVIEW_ERRORS:
                reply = self.feedback(record, probe['diagnostic'])
                reply.update(host_action='report_current_result', result_observation=probe.get('result_observation'))
                return reply
            return None
        if expected is None:
            # Old tickets deliberately contain only a salted hash. Use the original
            # argument for a live provider. An opaque window can still be reviewed.
            guard()
            with flow.app.bridge.UIA() as uia:
                try:
                    surface, _ = flow._result_target(binding, uia, step['selector'], hint, column=step['result_column'])
                except (ValueError, flow.app.bridge.BridgeError) as error:
                    if getattr(error, 'code', None) in ('result_table_missing', 'target_missing'):
                        return None
                    raise
                if result_cell.bare_group(uia, surface):
                    return None
            reply = self.feedback(record, 'required_parameter_missing')
            reply.update(status='needs_input', host_action='resume_with_original_parameters',
                         missing_parameters=[step['parameter']])
            return reply
        # A separate request may arrive after the application's data provider
        # finished populating. Recheck the current result, without querying again.
        with flow.app.bridge.UIA() as uia:
            probe = flow.observe_result(binding, uia, step, expected, guard, wait_seconds=0,
                                        reviewed_group=hint, remember=True)
        if probe['status'] == 'verified':
            suffix = result_trial.continuation(self.recipe, number, confirmed_initial_value=True)
            suffix['steps'][0].pop('parameter', None)
            suffix['steps'][0]['value'] = expected
            element = probe.get('result_element')
            options = dict(result_surface_hint=dict(number=1, **element)) if element else {}
            repair.need(flow.load(recipe_path) == self.recipe, 'workflow_changed_during_repair')
            self._write(self.pending, dict(record, claimed=True))
            result = flow.run(suffix, hwnd=owner['hwnd'], result_owner=owner, **options)
            return self._finish(result, record, recipe_path, number-1, 'accessibility',
                                dict(number=number, **element) if element else None)
        if probe.get('diagnostic') not in flow.RESULT_IMAGE_ERRORS:
            reply = self.feedback(record, probe['diagnostic'])
            reply.update(host_action='report_current_result', result_observation=probe.get('result_observation'))
            return reply
        return None

    def _finish(self, result, record, path, offset, source, learned, *, tested_recipe=None):
        if result['input_dispatched'] is False:
            self._write(self.pending, record)
            result.update(self.feedback(record, result.get('diagnostic', 'result_resume_unavailable')))
            return result
        for row in result.get('steps', []):
            row['number'] += offset
        if result.get('stopped_at'):
            result['stopped_at'] += offset
        result.update(resumed_at=offset+1, prior_steps_replayed=False, previous_input_dispatched=record['prior_input'],
                      resumed_action_seconds=result.get('elapsed_seconds'), result_match_source=source)
        if result.get('status') in ('completed_unverified','verified') and not result.get('stopped_at'):
            result.setdefault('host_action', 'report_execution_receipt')
            if record.get('remember') and result.get('correction_saved') is not False:
                repair.remember_test(path, tested_recipe or self.recipe, result, record['approval_before'], offset+1)
            _discard_preview(record)
            self.pending.unlink(missing_ok=True)
            if learned:
                try:
                    self.remember_elements([learned])
                except (OSError, ValueError):
                    result['result_surface_hint_saved'] = False
        return result

    @staticmethod
    def _check_header(flow, binding, uia, table, column, head, point):
        """Use provider column boundaries too whenever the grid exposes headers."""
        headers = flow.lookup.find_all(uia, table, roles=('headeritem',))
        try:
            root = uia.from_handle(binding.hwnd)
            visible = [item for item in headers if flow.live._visible(uia, item, binding, root)]
            if not visible:
                return
            matches = []
            for item in visible:
                box = uia.bounds(item)
                if (box['x'] <= head[0] < box['x'] + box['width'] and
                        box['y'] <= head[1] < box['y'] + box['height'] and
                        box['x'] <= point[0] < box['x'] + box['width']):
                    matches.append(item)
            repair.need(len(matches) == 1 and (matches[0].property(30005) or '').strip().casefold()
                        == column.strip().casefold(), 'result_point_outside_column')
        finally:
            for item in headers:
                item.close()
