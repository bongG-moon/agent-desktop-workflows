"""Continue an existing registered lookup after its operator identifies a locally drawn cell."""
from copy import deepcopy
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
        record = self._read(self.pending)
        active = (record and record['recipe'] == self.fingerprint and record['expires'] > time.time())
        if ticket:
            repair.need(active and re.fullmatch('[a-f0-9]{64}', ticket)
                        and secrets.compare_digest(record['ticket'], ticket), 'result_review_expired')
            repair.need(not overrides or record['variables'] == _variables(overrides), 'result_review_parameters_changed')
            return record
        if active and record['variables'] == _variables(overrides) and (not hwnd or hwnd == record['window']['hwnd']):
            return record
        self.pending.unlink(missing_ok=True)
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
        if (not receipt.get('result_window') or receipt['input_dispatched'] is not False
                or receipt.get('diagnostic') not in flow.RESULT_IMAGE_ERRORS):
            return None
        step = self.recipe['steps'][number - 1]
        values, _ = flow.values_for(self.recipe, overrides)
        expected = values[step['parameter'].casefold()] if 'parameter' in step else step['value']
        ticket = secrets.token_hex(32)
        record = dict(ticket=ticket, recipe=self.fingerprint, number=number, window=receipt['result_window'],
                      variables=_variables(overrides), expected=repair.digest([ticket, expected]),
                      prior_input=report['input_dispatched'], expires=time.time() + grounding.TTL)
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
        values, _ = flow.values_for(self.recipe, overrides)
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
                                                             column=step['result_column'])
                    except (ValueError, flow.app.bridge.BridgeError) as error:
                        if selector != {'role':'table'} or getattr(error, 'code', None) != 'result_table_missing':
                            raise
                        group_boxes = {}
                    else:
                        recorded_box = dict(uia.bounds(table))
                    rect, png = grounding._capture(flow.app, binding, guard, group_boxes=group_boxes)
                    geometry['capture_rect'] = list(rect)
                    if group_boxes is None:
                        geometry['table_absolute'] = recorded_box
                        geometry['table_relative'] = [recorded_box['x'] - rect[0], recorded_box['y'] - rect[1],
                                                      recorded_box['width'], recorded_box['height']]
                        visible = result_trial.visible_table_box(rect, recorded_box)
                        table_box = [visible['x'] - rect[0], visible['y'] - rect[1], visible['width'], visible['height']]
                        geometry['visible_relative'] = table_box
                frame, path = grounding.store_frame(grounding._directory(), key.encode('ascii'), png)
                opened = time.monotonic()
                choice = local_result_review.choose(path, step['result_column'], table_box=table_box,
                                                    expected=expected, frame_size=rect[2:])
                local_review_seconds += time.monotonic() - opened
                if not choice or not choice.get('refresh'):
                    break
                path.unlink(missing_ok=True)
            if choice is None:
                return self.feedback(record, 'local_result_review_cancelled')
            if repair.digest([key, choice['value']]) != record['expected']:
                return self.feedback(record, 'confirmed_result_value_mismatch')
            x, y = choice['point']
            left, head_y, right = choice['header']
            repair.need(0 <= left < right < rect[2] and left <= x <= right
                        and 0 <= head_y < y < rect[3], 'result_point_outside_column')
            repair.need(time.time() < record['expires'], 'result_review_expired')
            head = (rect[0] + (right + left)//2, rect[1] + head_y)
            point = (rect[0]+x, rect[1]+y)
            # Reviewing the snapshot never reserves desktop input. Only this
            # explicitly confirmed physical continuation needs the target exposed.
            with flow.coordination.desktop_input():
                guard()
                flow.row_action.foreground(binding)
                now, current = grounding._capture(flow.app, binding, guard)
                repair.need(now == rect, 'result_frame_changed')
                with flow.app.bridge.UIA() as uia:
                    if table_box is None:
                        selector, box = result_cell.bound_group(binding, uia, head, point)
                        key_type = 'automation_id' if 'automation_id' in selector else 'name'
                        repair.need(group_boxes.get((key_type, selector[key_type])) == box, 'result_table_changed')
                    else:
                        table, current_selector = flow._result_target(binding, uia, original_selector, hint,
                                                                     column=step['result_column'])
                        repair.need(current_selector == selector, 'result_table_changed')
                        box = uia.bounds(table)
                        repair.need(box == recorded_box, 'result_table_changed')
                        self._check_header(flow, binding, uia, table, step['result_column'], head, point)
                    repair.need(result_trial.same_table_pixels(png, current, rect, box), 'result_frame_changed')
                    repair.need(box['x'] <= rect[0]+left < rect[0]+right < box['x']+box['width']
                                and box['y'] <= head[1] < point[1] < box['y']+box['height'], 'result_point_outside_table')
                    flow.check_result_point(binding, uia, selector, point, reviewed_frame=rect)
            suffix = result_trial.visual_suffix(self.recipe, number, png, [x, y], reviewed=True)
            repair.need(flow.load(recipe_path) == self.recipe, 'workflow_changed_during_repair')
            confirmation = dict(owner=owner, frame=frame, receipt=key, point=[x,y], selector=selector,
                                window_rect=rect, table_box=box)
            if original_selector != selector:
                confirmation['original_selector'] = original_selector
            self._write(self.pending, dict(record, claimed=True))
            result = flow.run(suffix, hwnd=owner['hwnd'], result_confirmation=confirmation)
            result['local_review_seconds'] = round(local_review_seconds,4)
            learned = dict(number=number, selector=selector) if selector.get('role') == 'group' else None
            return self._finish(result, record, recipe_path, number, 'local_visual_confirmation', learned)
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
            suffix = deepcopy(self.recipe)
            suffix['steps'] = suffix['steps'][number-1:]
            suffix['steps'][0].pop('parameter', None)
            suffix['steps'][0]['value'] = expected
            for action in suffix['steps'][2:]:
                action['window_context']['after_step'] = 2
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

    def _finish(self, result, record, path, offset, source, learned):
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
            if record.get('remember'):
                repair.remember_test(path, self.recipe, result, record['approval_before'], offset+1)
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
