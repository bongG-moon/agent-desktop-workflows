"""Keep a saved prefix and join a new demonstration recorded in the same window."""
from copy import deepcopy

import learned_tasks as tasks
import review_drafts


def _new_name(source, suffix, directory):
    for i in range(1, 1000):
        tail = f' - {suffix}' + (f' {i}' if i > 1 else '')
        name = source[:100-len(tail)].rstrip() + tail
        try:
            tasks.check_new_task(name, directory)
        except tasks.TaskError as error:
            if error.code == 'task_already_exists':
                continue
            raise
        return name
    raise tasks.TaskError('unused_recording_name_required')


def run(app, args):
    directory = app.task_directory()
    source = tasks.load_task(args.task, directory)
    number = len(source['steps']) + 1 if args.from_step is None else args.from_step
    if not 1 <= number <= len(source['steps']) + 1:
        raise tasks.TaskError('invalid_step_number')
    destination = args.save_as or _new_name(source['name'], '수정본', directory)
    tasks.check_new_task(destination, directory)
    supplement = args.supplement
    if not supplement:
        owner = app.bridge.Binding(app.pick_window(app.windows(source['exe']), source['window'], None))
        app.guard(owner, owner, source['window'])
        supplement = _new_name(source['name'], '추가 녹화', directory)
        if destination.casefold() == supplement.casefold():
            raise tasks.TaskError('supplement_name_conflict')
        storage = review_drafts.ReviewStorage(supplement, source['exe'], source['window'], directory)
        saved = app.edit_recording(owner, storage, report_ready=getattr(args, 'report_ready', False),
                                   section=(number, len(source['steps'])))
        if saved['status'] != 'recorded':
            return saved
    if args.task.casefold() == supplement.casefold():
        raise tasks.TaskError('supplement_name_conflict')
    extra = tasks.load_task(supplement, directory, allow_review=True)
    if extra.get('kind') == review_drafts.KIND:
        return dict(status='needs_review', diagnostic='supplement_requires_recovery',
                    source_task=source['name'], supplement=supplement, from_step=number,
                    save_as=destination, recover_args=['recover', '--task', supplement], input_dispatched=False)
    tasks.check_binding(extra, source['exe'], source['window'])
    steps = deepcopy(extra['steps'])
    for step in steps:
        context = step.get('window_context', {})
        if 'after_step' in context:
            context['after_step'] += number - 1
    plan = dict(source, name=destination, steps=source['steps'][:number - 1] + steps)
    try:
        tasks.save_task(plan, directory)
    except tasks.TaskError as error:
        return dict(status='needs_review', diagnostic=error.code, step_number=error.step_number,
                    source_task=source['name'], supplement=supplement, from_step=number, save_as=destination,
                    recover_args=['recover', '--task', supplement], input_dispatched=False)
    return dict(status='recorded', task=tasks.summary(plan), source_task=source['name'], supplement=supplement,
                from_step=number, execution_plan=app.execution_plan_info(app.saved_execution_plan(plan)),
                input_dispatched=False)
