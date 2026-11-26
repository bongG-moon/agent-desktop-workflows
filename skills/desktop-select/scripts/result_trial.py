"""Resume a confirmed result without replaying earlier steps or losing dependencies."""
from copy import deepcopy
import struct
import time
from pathlib import Path
from types import SimpleNamespace

import first_trial
import grounding
import learned_tasks as tasks
import recovery


def continuation(recipe, start_number, *, confirmed_initial_value=False):
    """Keep original later operations and remap only their in-suffix dependencies.

    An earlier opener is not replayed and cannot supply a fresh window binding.
    Required values are not stored in review tickets, so a suffix needing one
    needs a separate value-bearing continuation rather than an invented value.
    """
    if type(start_number) is not int or not 1 <= start_number <= len(recipe['steps']):
        raise tasks.TaskError('result_resume_unavailable')
    import python_workflow
    python_workflow.validate(recipe)
    suffix = deepcopy(recipe)
    suffix['steps'] = suffix['steps'][start_number-1:]
    for index, step in enumerate(suffix['steps']):
        if 'parameter' in step and not (index == 0 and confirmed_initial_value):
            raise tasks.TaskError('result_resume_parameter_required')
        scope = step.get('window_context', {})
        if 'after_step' in scope:
            if scope['after_step'] < start_number:
                raise tasks.TaskError('result_resume_prior_window_required')
            step['window_context'] = dict(scope, after_step=scope['after_step'] - start_number + 1)
    python_workflow.validate(suffix)
    return suffix


def supported(recipe, check_number):
    if type(check_number) is not int or not 1 <= check_number <= len(recipe['steps']):
        return False
    check = recipe['steps'][check_number - 1]
    if (check.get('action') == 'double-click-result' and check.get('result_row') in ('single', 'first', 'recorded')
            and 'result_column' not in check):
        start = check_number
    elif (check.get('action') == 'verify-value' and check.get('result_column')
          and check_number < len(recipe['steps'])
          and recipe['steps'][check_number]['action'] == 'double-click-result'):
        start = check_number + 1
    else:
        return False
    if any(recipe['steps'][i].get('window_context') or recipe['steps'][i].get('popup')
           for i in range(check_number - 1, start)):
        return False
    try:
        # Legacy value matching still needs its immediately preceding verified
        # value while validating; visual_suffix replaces the linked pair below.
        continuation(recipe, check_number, confirmed_initial_value=start != check_number)
    except (ValueError, KeyError, TypeError):
        return False
    return True


def visual_suffix(recipe, offset, image, point, *, reviewed=False):
    """Build only the already-requested double-click and its dependent detail operations."""
    import visual_target
    if not supported(recipe, offset):
        raise tasks.TaskError('result_resume_unavailable')
    rect, pixels = grounding._pixels(image)
    # A registered local review already binds a column and cell in this exact
    # transient frame. Its caller supplies full-table confirmation at dispatch.
    builder = visual_target._frame_patch if reviewed else visual_target.from_frame
    spec = builder(rect, pixels, tuple(point))
    spec['gesture'] = 'double-click'
    start = offset - 1 if recipe['steps'][offset-1].get('result_row') else offset
    remaining = deepcopy(recipe['steps'][start:])
    remaining[0] = dict(action='visual-click', label=remaining[0].get('label', '현재 결과 행 열기'), visual=spec)
    for step in remaining[1:]:
        scope = step.get('window_context', {})
        if 'after_step' in scope:
            step['window_context'] = dict(scope, after_step=scope['after_step'] - start)
    return dict(recipe, steps=remaining)


def visible_table_box(rect, box):
    """Intersect an owned table's raw screen bounds with its exact captured frame.

    Keep the raw bounds separately for identity/layout checks. This intersection
    is only a visible pixel region, never a scale correction or a new selector.
    """
    if (not isinstance(rect, (list, tuple)) or len(rect) != 4
            or not isinstance(box, dict)
            or any(type(v) is not int for v in rect)
            or any(type(box.get(k)) is not int for k in ('x', 'y', 'width', 'height'))
            or rect[2] <= 0 or rect[3] <= 0 or box['width'] <= 0 or box['height'] <= 0):
        raise tasks.TaskError('result_geometry_invalid')
    left, top = max(rect[0], box['x']), max(rect[1], box['y'])
    right = min(rect[0] + rect[2], box['x'] + box['width'])
    bottom = min(rect[1] + rect[3], box['y'] + box['height'])
    if left >= right or top >= bottom:
        raise tasks.TaskError('result_table_outside_frame')
    return dict(x=left, y=top, width=right-left, height=bottom-top)


def same_table_pixels(first, second, rect, box):
    """Compare the entire captured part of the grid, including all visible rows."""
    try:
        visible = visible_table_box(rect, box)
        for png in (first, second):
            if (len(png) < 41 or png[:8] != grounding.SIGNATURE or png[12:16] != b'IHDR'
                    or struct.unpack_from('>II', png, 16) != tuple(rect[2:])):
                return False
    except (tasks.TaskError, TypeError, struct.error):
        return False
    if first == second:
        return True
    left, top = visible['x'] - rect[0], visible['y'] - rect[1]
    width, height = visible['width'], visible['height']
    before, a = grounding._pixels(first)
    after, b = grounding._pixels(second)
    if before != after or before[2:] != list(rect[2:]):
        return False
    stride, size = rect[2] * 4, width * 4
    offset = (rect[2] * top + left) * 4
    a, b = memoryview(a), memoryview(b)
    return all(a[i:i+size] == b[i:i+size] for i in range(offset, offset+height*stride, stride))


def _path(directory, key):
    return directory / (recovery._fingerprint(['result-resume', key]) + '.json')


def prior(directory, key):
    path = _path(directory, key)
    if not path.exists():
        return None
    result = first_trial._previous(path)
    if result.get('diagnostic') == 'first_trial_running_or_interrupted':
        original = first_trial._previous(directory / (key + '.json'))
        result.update(trial_receipt=key, resumed_after=original.get('stopped_at'), prior_steps_replayed=False,
                      steps=original.get('steps', []), observations=original.get('observations', []))
    return result


def review_locally(app, key, allow_physical):
    """Pick a missing demonstrated position locally, then resume the already-run query."""
    import coordination
    directory = tasks._directory(app.task_directory() / 'first-trials')
    # Two consumers of the same receipt must never present competing pickers.
    with coordination.recipe(_path(directory, key).with_suffix('.review')):
        return _choose_and_resume(app, key, allow_physical)


def _choose_and_resume(app, key, allow_physical):
    import local_result_review
    directory, original, inspection = first_trial._inspection(app, key)
    previous = prior(directory, key)
    if previous and previous.get('resume_dispatched_input') is not False:
        return previous
    if inspection.get('result_row') != 'recorded' or not inspection.get('resume_supported'):
        raise tasks.TaskError('result_resume_unavailable')
    if not allow_physical:
        return dict(status='needs_consent', required_option='--allow-physical', input_dispatched=False,
                    previous_input_dispatched=original.get('input_dispatched'), prior_steps_replayed=False)
    while True:
        frame = first_trial.inspect_result(app, key)
        if 'image' not in frame:
            return frame
        path = Path(frame['image'])
        try:
            _, _, current = first_trial._inspection(app, key)
            geometry = current['frame_geometry']
            if geometry['frame'] != frame['frame']:
                raise tasks.TaskError('result_frame_changed')
            rect = geometry['window_rect']
            visible = visible_table_box(rect, geometry['table_box'])
            bounds = [visible['x'] - rect[0], visible['y'] - rect[1], visible['width'], visible['height']]
            choice = local_result_review.choose(path, table_box=bounds, frame_size=rect[2:], row_policy='recorded')
            if choice is None:
                return dict(original, host_action='local_result_review_cancelled', prior_steps_replayed=False,
                            this_call_dispatched_input=False)
            if choice.get('refresh'):
                continue
            if (choice.get('row_policy') != 'recorded'
                    or not local_result_review.valid_row_choice(choice.get('point'), bounds, choice.get('confirmed'))):
                raise tasks.TaskError('result_row_not_confirmed')
            args = SimpleNamespace(receipt=key, frame=frame['frame'], point=choice['point'],
                                   row_policy='recorded', allow_physical=True, column=None, value=None)
            return resume(app, args)
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass  # The grounding cache also expires frames if immediate cleanup is unavailable.


def _save_recorded_point(app, envelope, number, geometry, screen_point, response):
    import recorded_plan
    import result_cell
    recipe = envelope['recipe']
    corrected = deepcopy(envelope['source'])
    source_number = envelope['source_steps'][number - 1]
    original = corrected['steps'][source_number - 1]
    step = recipe['steps'][number - 1]
    target = dict(action='double-click-result', label=step['label'], result_row='recorded',
                  selector=deepcopy(geometry['selector']), result_point=result_cell.body_point(geometry['table_box'], screen_point))
    for field in ('window_context', 'alias', 'description'):
        if field in original:
            target[field] = deepcopy(original[field])
    corrected['steps'][source_number - 1] = target
    corrected['name'] = corrected['name'][:65] + ' - 검증 ' + recovery._fingerprint(corrected)[:10]
    plan = recorded_plan.compile_plan(corrected)
    expected = deepcopy(recipe)
    expected['name'] = corrected['name']
    expected['steps'][number - 1].update(selector=deepcopy(target['selector']), result_point=target['result_point'])
    if plan.get('envelope', {}).get('recipe') != expected:
        raise tasks.TaskError('result_trial_source_mapping_unavailable')
    directory = app.task_directory()
    try:
        tasks.save_task(corrected, directory)
    except tasks.TaskError as error:
        if error.code != 'task_already_exists' or tasks.load_task(corrected['name'], directory) != corrected:
            raise
    response.update(effective_task=corrected['name'], correction_saved=True,
                    register_args=['register', '--task', corrected['name'], '--out', '<project skill folder>'])


def resume(app, args):
    import coordination
    import python_workflow
    from win_bridge import BridgeError
    directory, report, inspection = first_trial._inspection(app, args.receipt)
    if not inspection.get('resume_supported'):
        raise tasks.TaskError('result_resume_unavailable')
    receipt_id = args.receipt
    resume_path = _path(directory, receipt_id)
    with coordination.recipe(resume_path):
        previous = prior(directory, receipt_id)
        if previous and (previous.get('resume_dispatched_input') is not False or
                         (previous.get('frame') == args.frame and previous.get('image_point') == args.point)):
            return previous
        if not args.allow_physical:
            return dict(status='needs_consent', diagnostic='first_trial_physical_input_required',
                        required_option='--allow-physical', input_dispatched=False)
        if not 0 <= time.time() - inspection['observed_at'] <= grounding.TTL:
            raise tasks.TaskError('result_inspection_expired')
        row_mode = inspection.get('result_row')
        if row_mode:
            if (getattr(args, 'row_policy', None) != row_mode or args.column is not None or args.value is not None
                    or inspection.get('frame_geometry', {}).get('frame') != args.frame):
                raise tasks.TaskError('confirmed_result_row_mismatch')
        elif (getattr(args, 'row_policy', None) is not None or args.column != inspection['column'] or
              recovery._fingerprint([receipt_id, args.value]) != inspection['expected_hash']):
            raise tasks.TaskError('confirmed_result_value_mismatch')
        root = app.task_directory()
        source = tasks.read_document(inspection['source_task'], root)
        if recovery._fingerprint(source) != inspection['source_hash']:
            raise tasks.TaskError('recording_draft_changed')
        document = tasks.load_task(report['effective_task'], root, allow_review=True)
        envelope = first_trial._compiled(document, root).get('envelope', {})
        recipe = envelope.get('recipe')
        offset = report['stopped_at']
        if (not recipe or recovery._fingerprint(recipe) != inspection['recipe_hash']
                or not supported(recipe, offset)):
            raise tasks.TaskError('prepared_recording_changed')
        _, png = grounding.read_frame(grounding._directory(), receipt_id.encode('ascii'), args.frame)
        suffix = visual_suffix(recipe, offset, png, args.point, reviewed=bool(row_mode))
        remaining = suffix['steps']
        python_workflow.validate(suffix)
        confirmation = dict(owner=inspection['window'], frame=args.frame, receipt=receipt_id, point=args.point,
                            selector=deepcopy(recipe['steps'][offset-1]['selector']))
        if row_mode:
            confirmation.update({k: deepcopy(v) for k,v in inspection['frame_geometry'].items() if k != 'frame'})
            offset -= 1

        def dispatch():
            result = app.worker_receipt(['_plan'], mutating=True, payload=dict(recipe=suffix, overrides={},
                hwnd=inspection['window']['hwnd'], background_only=False, result_confirmation=confirmation))
            result = python_workflow.summarize_execution(result, list(enumerate(remaining, 1)))
            prior_steps = deepcopy(report['steps'])
            # Preserve the original unsuccessful UIA read alongside the separate
            # host observation. The visual observation is not provider evidence.
            observation = dict(column=args.column, value=args.value, evidence='host_read_scoped_image', frame=args.frame)
            if row_mode:
                observation = dict(result_row=row_mode, evidence='host_confirmed_current_rows', frame=args.frame)
                prior_steps = prior_steps[:-1]
            else:
                prior_steps[-1]['visual_observation'] = observation
            observations = list(report.get('observations', []))
            for row in result.get('steps', []):
                number = row['number']
                row['number'] = offset + number
                row['source_step'] = envelope['source_steps'][row['number'] - 1]
                info = row.get('receipt', row)
                if remaining[number - 1]['action'] == 'read-value' and info.get('status') == 'observed':
                    observed_step = remaining[number - 1]
                    observed_selector = observed_step.get('selector', {})
                    field = (observed_step.get('label') or observed_selector.get('name')
                             or observed_selector.get('automation_id') or 'value')
                    observations.append(dict(source_step=row['source_step'], field=field,
                                             value=info['value']))
            response = dict(result, steps=prior_steps + result.get('steps', []), trial_receipt=receipt_id,
                frame=args.frame, image_point=args.point, resumed_after=offset, prior_steps_replayed=False,
                resume_dispatched_input=result.get('input_dispatched'), observations=observations,
                effective_task=report['effective_task'],
                input_dispatched=python_workflow.combine(report['input_dispatched'],result.get('input_dispatched')))
            if row_mode:
                response['result_row_observation'] = observation
            if result.get('stopped_at'):
                response['stopped_at'] = result['stopped_at'] + offset
                response['source_stopped_at'] = envelope['source_steps'][response['stopped_at'] - 1]
            if result.get('status') in ('completed_unverified', 'verified') and 'stopped_at' not in result:
                response['host_action'] = 'report_completed_trial'
                if row_mode == 'recorded' and result.get('input_dispatched') is True:
                    geometry = inspection['frame_geometry']
                    absolute = [axis + pos for axis, pos in zip(geometry['window_rect'][:2], args.point)]
                    try:
                        _save_recorded_point(app, envelope, offset + 1, geometry, absolute, response)
                    except (ValueError, OSError, BridgeError) as error:
                        response.update(correction_saved=False, correction_diagnostic=getattr(error, 'code', 'result_point_save_failed'))
            elif result.get('input_dispatched') is False:
                response['host_action'] = 'inspect_current_result_without_replaying'
                response['result_inspection'] = inspection
            else:
                response['host_action'] = 'check_current_effect_before_continuing'
            return response
        if previous:
            # Only a proved zero-input suffix can yield to a different frame or point.
            # The per-trial lock covers both retirement and the new reservation.
            resume_path.unlink()
        return first_trial._once(root, resume_path.stem, dispatch)
