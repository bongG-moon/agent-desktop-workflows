"""Resume only a host-confirmed result row and its recorded detail dialog."""
from copy import deepcopy
import struct
import time

import first_trial
import grounding
import learned_tasks as tasks
import recovery


def supported(recipe, check_number):
    if type(check_number) is not int or not 1 <= check_number <= len(recipe['steps']):
        return False
    check = recipe['steps'][check_number - 1]
    suffix = recipe['steps'][check_number:]
    if (check.get('action') != 'verify-value' or not check.get('result_column')
            or check.get('window_context') or check.get('popup') or len(suffix) < 2
            or suffix[0]['action'] != 'double-click-result' or suffix[0].get('window_context')):
        return False
    scope = suffix[1].get('window_context') or {}
    return (scope.get('related') is True and scope.get('after_step') == check_number + 1
            and all(step.get('window_context') == scope and 'popup' not in step for step in suffix[1:])
            and all(step['action'] == 'read-value' for step in suffix[1:-1])
            and (suffix[-1]['action'] == 'read-value' or
                 suffix[-1]['action'] == 'invoke' and suffix[-1].get('verify_closed') is True))


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
    remaining = deepcopy(recipe['steps'][offset:])
    remaining[0] = dict(action='visual-click', label=remaining[0]['label'], visual=spec)
    for step in remaining[1:]:
        step['window_context']['after_step'] = 1
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


def resume(app, args):
    import coordination
    import python_workflow
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
        if (args.column != inspection['column'] or
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
        suffix = visual_suffix(recipe, offset, png, args.point)
        remaining = suffix['steps']
        python_workflow.validate(suffix)
        confirmation = dict(owner=inspection['window'], frame=args.frame, receipt=receipt_id, point=args.point,
                            selector=deepcopy(recipe['steps'][offset-1]['selector']))

        def dispatch():
            result = app.worker_receipt(['_plan'], mutating=True, payload=dict(recipe=suffix, overrides={},
                hwnd=inspection['window']['hwnd'], background_only=False, result_confirmation=confirmation))
            result = python_workflow.summarize_execution(result, list(enumerate(remaining, 1)))
            prior_steps = deepcopy(report['steps'])
            # Preserve the original unsuccessful UIA read alongside the separate
            # host observation. The visual observation is not provider evidence.
            prior_steps[-1]['visual_observation'] = dict(column=args.column, value=args.value,
                                                         evidence='host_read_scoped_image', frame=args.frame)
            observations = list(report.get('observations', []))
            for row in result.get('steps', []):
                number = row['number']
                row['number'] = offset + number
                row['source_step'] = envelope['source_steps'][row['number'] - 1]
                info = row.get('receipt', row)
                if remaining[number - 1]['action'] == 'read-value' and info.get('status') == 'observed':
                    observations.append(dict(source_step=row['source_step'], field=remaining[number - 1]['label'],
                                             value=info['value']))
            response = dict(result, steps=prior_steps + result.get('steps', []), trial_receipt=receipt_id,
                frame=args.frame, image_point=args.point, resumed_after=offset, prior_steps_replayed=False,
                resume_dispatched_input=result.get('input_dispatched'), observations=observations,
                effective_task=report['effective_task'],
                input_dispatched=python_workflow.combine(report['input_dispatched'],result.get('input_dispatched')))
            if result.get('stopped_at'):
                response['stopped_at'] = result['stopped_at'] + offset
                response['source_stopped_at'] = envelope['source_steps'][response['stopped_at'] - 1]
            if result.get('status') in ('completed_unverified', 'verified') and 'stopped_at' not in result:
                response['host_action'] = 'report_completed_trial'
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
