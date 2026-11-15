"""Exercise the recorder's Tk picker against synthetic Windows app processes.

Tk widget commands select a named, already-open window. The selected
recorder receives scoped hook payloads; no system pointer input is sent.
"""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tkinter as tk

from smoke_multiwindow import DEVICE_EXE, TITLE, compile_fixture, install_skill
from smoke_native import Fixture


def pick(root, session, drive):
    from teach_capture import pick_recording_window
    errors = []
    deadline = time.monotonic() + 10

    def visit(widget):
        for child in widget.winfo_children():
            yield child
            yield from visit(child)

    def tick():
        widgets = {w.winfo_name(): w for w in visit(root)}
        try:
            if time.monotonic() > deadline:
                raise AssertionError('picker_did_not_reach_expected_state')
            tree = widgets.get('windows')
            if tree is not None and tree.get_children():
                rows = {tree.set(key, 'title'): key for key in tree.get_children()}
                if drive(widgets, tree, rows):
                    return
            root.after(50, tick)
        except BaseException as exc:
            errors.append(exc)
            if 'window_picker' in widgets:
                widgets['window_picker'].destroy()

    def timeout():
        errors.append(TimeoutError('picker_callback_failed_to_close_dialog'))
        dialog = root.children.get('window_picker')
        if dialog is not None:
            dialog.destroy()

    root.after(50, tick)
    timer = root.after(12000, timeout)
    try:
        outcome = pick_recording_window(root, session)
    finally:
        root.after_cancel(timer)
    if errors:
        raise errors[0]
    return outcome


def exercise(skill, work):
    installed = work/'skills/desktop-select'
    install_skill(skill, installed)
    manager = compile_fixture(work)
    sys.path.insert(0, str(installed/'scripts'))
    import desktop_select as app
    import learned_tasks
    from teach_capture import CaptureSession, CaptureTrail, StepEdits, finalize_contexts
    app.bridge.set_dpi_awareness()
    checks = []

    def check(name, ok):
        checks.append(dict(case=name, passed=bool(ok)))
        print(json.dumps(checks[-1]), flush=True)
        if not ok:
            raise AssertionError(name)

    def call(*args):
        process = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(installed/'scripts/desktop_select.py'),
                                  *args, '--background-only'], capture_output=True, encoding='utf8', timeout=45,
                                 env=dict(os.environ, DESKTOP_SELECT_TASK_DIR=str(work/'tasks')),
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        return json.loads(process.stdout)

    def session_type(binding, title):
        part = CaptureSession(binding, title)
        part.pump.start = part.pump.stop = lambda: None
        return part

    try:
        with ExitStack() as stack:
            manager_proc = Fixture(manager)
            stack.callback(manager_proc.close)
            manager_proc.command('open-decoy')
            assert call('open','--exe',manager.name,'--window',TITLE,'--target','OpenAlpha',
                        '--role','button')['input_dispatched'] is True
            other = Fixture(work/DEVICE_EXE,'Unrelated')
            stack.callback(other.close)
            main = app.bridge.Binding(app.pick_window(app.windows(manager.name),TITLE,manager_proc.ready['hwnd']))
            session = CaptureTrail(main,TITLE,session_type)
            session.start()
            stack.callback(session.cancel)
            root = tk.Tk()
            root.geometry('240x80+40+330')
            stack.callback(root.destroy)
            root.update()
            target, second, unrelated = 'Synthetic device Alpha', 'Synthetic device Decoy', other.ready['title']

            requested_all = False
            def cancel(widgets, tree, rows):
                nonlocal requested_all
                if not requested_all:
                    check('default_list_contains_manager_and_both_preexisting_children',
                          {TITLE,target,second} <= rows.keys() and unrelated not in rows)
                    requested_all = True
                    widgets['all_windows'].invoke()
                    return False
                check('explicit_all_windows_adds_unrelated_fixture',unrelated in rows)
                widgets['cancel'].invoke()
                return True

            assert pick(root,session,cancel) is None
            check('cancel_preserves_original_recording',session.active is session.primary)

            def choose(widgets, tree, rows):
                tree.selection_set(rows[target])
                tree.focus(rows[target])
                tree.event_generate('<<TreeviewSelect>>')
                widgets['use'].invoke()
                return True

            outcome = pick(root,session,choose)
            state = manager_proc.command('state')
            surface = state['devices']['Alpha']
            check('dialog_selected_exact_preexisting_window',outcome=='selected' and
                  session.binding.hwnd == surface['hwnd'] and session.scope['mode']=='existing')
            part = session.active
            hit = surface['targets']['SharedValue']
            with app.bridge.UIA() as uia:
                box = uia.bounds(uia.from_handle(hit))
            event = dict(token=time.monotonic_ns(),x=box['x']+box['width']//2,y=box['y']+box['height']//2,
                         at=time.monotonic())
            part.pump._emit(dict(event,kind='down',action='click',window=surface['hwnd'],
                                 hit_window=hit,scope_proven=True))
            part.pump._emit(dict(event,kind='up',inside=True))
            until(lambda: part.worker.watched is not None)
            assert call('fill','--exe',DEVICE_EXE,'--window',target,'--target','SharedValue',
                        '--value','from-selected-window')['status']=='verified'
            until(lambda: any(s.get('value')=='from-selected-window' for s in part.notes.snapshot()))
            raw = finalize_contexts(StepEdits().apply(session.finish()['steps']))
            matched = [s for s in raw if s.get('value')=='from-selected-window']
            check('edit_recorded_against_selected_window_only',len(matched)==1 and
                  matched[0].get('window_context')==dict(exe=DEVICE_EXE,title=target,mode='existing')
                  and all(s.get('window_context',{}).get('title')==target for s in raw))
            state = manager_proc.command('state')
            check('only_selected_window_received_value',state['writes']==0
                  and state['devices']['Alpha']['value']=='from-selected-window'
                  and state['devices']['Alpha']['writes']==1 and state['devices']['Decoy']['writes']==0
                  and other.command('state')['writes']==0)
            plan = learned_tasks.from_recording('Previously open device',manager.name,TITLE,raw)
            learned_tasks.save_task(plan,work/'tasks')
            assert call('fill','--exe',DEVICE_EXE,'--window',target,'--target','SharedValue',
                        '--value','initial')['status']=='verified'
            replay = call('replay','--exe',manager.name,'--window',TITLE,'--task',plan['name'])
            state = manager_proc.command('state')
            check('saved_recording_replays_only_in_selected_existing_window',replay['status']=='verified'
                  and state['devices']['Alpha']['value']=='from-selected-window'
                  and state['devices']['Alpha']['writes']==3 and state['devices']['Decoy']['writes']==0
                  and state['writes']==0 and other.command('state')['writes']==0)
            # Close the second window while its row is displayed. The stale
            # choice must leave the current selection intact.
            stale_session = CaptureTrail(main,TITLE,session_type)
            stale_session.start()
            stack.callback(stale_session.cancel)
            attempted = False
            def stale(widgets, tree, rows):
                nonlocal attempted
                if attempted:
                    widgets['cancel'].invoke()
                    return True
                tree.selection_set(rows[second])
                tree.event_generate('<<TreeviewSelect>>')
                manager_proc.command('reset')
                attempted = True
                widgets['use'].invoke()
                return False
            result = pick(root,stale_session,stale)
            check('closed_selection_never_attached',result is None and attempted
                  and stale_session.active is stale_session.primary)
    finally:
        (work/'summary.json').write_text(json.dumps(checks,indent=2),encoding='utf8')


def until(condition):
    deadline = time.monotonic()+6
    while time.monotonic()<deadline:
        if condition():
            return
        time.sleep(.05)
    raise AssertionError('recorder_timed_out')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skill-dir',type=Path,required=True)
    parser.add_argument('--work-dir',type=Path,required=True)
    args=parser.parse_args()
    work = args.work_dir.resolve()
    work.mkdir(parents=True,exist_ok=False)
    exercise(args.skill_dir.resolve(),work)
