"""Check step-specific windows against ordinary WinForms processes.

The manager opens two independent, unowned windows of the same second EXE.
Every window has the same edit control. The fixture's pipes provide an
independent assertion about who received each value and how often.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from smoke_040_rebuild import install_skill
from smoke_native import Fixture


ROOT = Path(__file__).resolve().parents[1]
TITLE = 'Synthetic desk manager'
TASK = 'Two independent device windows'
DEVICE_EXE = 'DeskDeviceFixture.exe'


def compile_fixture(work):
    framework = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319'
    manager = work / 'DeskManagerFixture.exe'
    subprocess.run([
        str(framework / 'csc.exe'), '/nologo', '/target:exe', '/out:' + str(manager),
        *['/r:' + str(framework / (name+'.dll')) for name in (
            'System.Windows.Forms', 'System.Drawing', 'System.Web.Extensions')],
        str(ROOT / 'tests/fixtures/LinkedWindowFixture.cs')], check=True)
    shutil.copyfile(manager, work / DEVICE_EXE)
    return manager


def exercise(skill, work, manager, probe_only, record):
    installed = work / 'skills/desktop-select'
    install_skill(skill, installed)
    sys.path.insert(0, str(installed / 'scripts'))
    import desktop_select as app
    import learned_tasks as tasks
    app.bridge.set_dpi_awareness()

    fixture = Fixture(manager)
    checks = []
    env = dict(os.environ, DESKTOP_SELECT_TASK_DIR=str(work / 'tasks'))
    cli = installed / 'scripts/desktop_select.py'
    workflow = installed / 'scripts/python_workflow.py'

    def check(label, condition, detail):
        checks.append(dict(case=label, passed=bool(condition)))
        print(json.dumps(checks[-1]), flush=True)
        if not condition:
            raise AssertionError((label, detail))

    def call(script, *args):
        args = [sys.executable, '-X', 'utf8', '-B', str(script), *args]
        result = subprocess.run(args, capture_output=True, encoding='utf8', timeout=120,
                                cwd=work, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        if not result.stdout:
            raise AssertionError((result.returncode, result.stderr))
        return json.loads(result.stdout)

    def state_ok(state, first, second):
        return (state['value'] == 'manager-result' and state['writes'] == 1
                and state['launched'] == dict(Alpha=1, Beta=1)
                and {k: (v['value'],v['writes']) for k,v in state['devices'].items()} ==
                       dict(Alpha=(first,1), Beta=(second,1), Decoy=('initial',0)))

    def launch(name):
        return dict(action='open', role='button', target='Open'+name, match='automation_id',
                    command_name='Open '+name, expand=False)

    def fill(value, context=None):
        step = dict(action='fill', role='textfield', target='SharedValue', match='automation_id',
                    recorded_effect='matched', instruction=dict(mode='fixed', value=value,
                    text='SharedValue를 '+value+'로 입력해줘'))
        if context:
            step['window_context'] = context
        return step

    def child(name, after=None):
        return dict(exe=DEVICE_EXE, title='Synthetic device '+name,
                    **({'after_step':after} if after else {'mode':'existing'}))

    def demonstrate():
        # Feed the same scoped down/up event payloads as the native hook.
        # No global hook or physical pointer is needed in this shared-desktop
        # test; each control is identified from the real fixture HWND via UIA.
        from teach_capture import CaptureSession, CaptureTrail, StepEdits, finalize_contexts
        from unittest.mock import patch
        import capture_windows

        def session_type(binding, title):
            session = CaptureSession(binding, title)
            session.pump.start = session.pump.stop = lambda: None
            return session

        main = app.bridge.Binding(app.pick_window(app.windows(manager.name),TITLE,fixture.ready['hwnd']))
        trail = CaptureTrail(main,TITLE,session_type)
        def event(control, surface):
            hit = surface['targets'][control]
            at = time.monotonic()
            part = trail.connected[(surface['hwnd'],surface['pid'])]
            # The low-level Windows hook supplies physical coordinates.
            with app.bridge.UIA() as uia:
                box = uia.bounds(uia.from_handle(hit))
            payload = dict(token=time.monotonic_ns(),x=box['x']+box['width']//2,
                           y=box['y']+box['height']//2,at=at)
            part.pump._emit(dict(payload,kind='down',action='click',window=surface['hwnd'],
                                 hit_window=hit,scope_proven=True))
            part.pump._emit(dict(payload,kind='up',inside=True))

        def await_case(pred):
            deadline = time.monotonic()+4
            while time.monotonic()<deadline:
                if pred():
                    return
                time.sleep(.05)
            raise AssertionError(('demonstration_observation_timed_out',trail.snapshot()))

        def enter(surface, value):
            event('SharedValue',surface)
            part = trail.connected[(surface['hwnd'],surface['pid'])]
            await_case(lambda: part.worker.watched is not None)
            actual = call(cli,'fill','--exe',part.binding.exe,'--window',surface['title'],
                          '--target','SharedValue','--value',value,'--background-only')
            assert actual['status']=='verified',actual
            await_case(lambda: any(s.get('value')==value for s in part.notes.snapshot()))

        trail.start()
        try:
            for name in ('Alpha','Beta'):
                current = fixture.command('state')
                event('Open'+name,current)
                await_case(lambda: any(s.get('target',{}).get('locator',{}).get('automation_id')=='Open'+name
                                       for s in trail.primary.notes.snapshot()))
                actual = call(cli,'open','--exe',manager.name,'--window',TITLE,'--target','Open'+name,
                              '--role','button','--background-only')
                assert actual['input_dispatched'] is True,actual
                surface = fixture.command('state')['devices'][name]
                # Supply the foreground identity without activating a real
                # user's desktop. All inventory, ancestry and binding is live.
                key = (surface['hwnd'],surface['pid'])
                with patch.object(capture_windows,'foreground_key',return_value=key):
                    def linked():
                        trail.poll()
                        return key in trail.connected
                    await_case(linked)
                enter(surface,name.lower()+'-result')
                with patch.object(capture_windows,'foreground_key',return_value=(main.hwnd,main.pid)):
                    trail.poll()
                assert trail.active is trail.primary
            enter(fixture.command('state'),'manager-result')
            raw = trail.finish()['steps']
            edited = finalize_contexts(StepEdits().apply(raw))
            plan = tasks.from_recording(TASK,manager.name,TITLE,edited)
            scopes = [s.get('window_context',{}).get('after_step') for s in plan['steps']]
            check('recorder_linked_new_children_and_returned_to_manager', scopes==[None,1,None,3,None], scopes)
            check('automatic_windows_recorded_with_process_evidence',
                  all(plan['steps'][i]['window_context']['related'] for i in (1,3)),scopes)
            return plan
        finally:
            if not trail.ended:
                trail.cancel()

    try:
        base = ['--exe',manager.name,'--window',TITLE,'--hwnd',str(fixture.ready['hwnd'])]
        fixture.command('open-decoy')
        inspect = call(cli,'inspect',*base,'--target','OpenAlpha','--role','button')
        check('fixture_manager_native_button',inspect.get('status') in ('resolved','observed'), inspect)
        if probe_only:
            opened = call(cli,'open',*base,'--target','OpenAlpha','--role','button','--background-only')
            check('real_manager_button_started_a_process',opened.get('input_dispatched') is True, opened)
            filled = call(cli,'fill','--exe',DEVICE_EXE,'--window','Synthetic device Alpha',
                          '--target','SharedValue','--value','one','--background-only')
            state = fixture.command('state')
            check('independent_window_pattern_and_oracle',filled.get('status') == 'verified'
                  and state['devices']['Alpha']['value']=='one' and state['devices']['Decoy']['writes']==0, filled)
            return

        plan = dict(schema=1, name=TASK, exe=manager.name, window=TITLE,
                    steps=[launch('Alpha'),fill('alpha-result',child('Alpha',1)),
                           launch('Beta'),fill('beta-result',child('Beta',3)),fill('manager-result')])
        if record:
            plan = demonstrate()
            fixture.command('reset')
            fixture.command('open-decoy')
        tasks.save_task(plan,Path(env['DESKTOP_SELECT_TASK_DIR']))
        destination = installed.parent/'fixture-two-devices'
        registered = call(workflow,'register','--task',TASK,'--out',str(destination),'--hwnd',str(fixture.ready['hwnd']))
        check('registered_before_child_windows_exist',registered.get('status')=='registered'
              and registered.get('input_dispatched') is False, registered)
        runner = destination/'scripts/run.py'
        inspection = call(runner,'--read-only')
        checks_before = fixture.command('state')
        receipts = [row['receipt'] for row in inspection.get('steps',[])]
        check('read_only_deferred_both_children',inspection.get('status')=='observed'
              and sum(r.get('status')=='deferred' for r in receipts)==2
              and inspection.get('input_dispatched') is False and checks_before['launched']==dict(Alpha=0,Beta=0),
              inspection)
        for label, entry, args in [('recorded',cli,['replay',*base,'--task',TASK]),('generated',runner,[])]:
            partial = call(entry,*args,'--only','2','--background-only')
            snapshot = fixture.command('state')
            reported = [partial, *(row['receipt'] for row in partial.get('steps',[]))]
            check(label+'_partial_new_window_requires_opener',partial.get('input_dispatched') is False
                  and any(row.get('diagnostic')=='window_origin_step_not_run' for row in reported)
                  and snapshot['launched']==dict(Alpha=0,Beta=0) and snapshot['devices']['Decoy']['writes']==0,partial)
        for label, entry, args in [('normal_replay',cli,['replay',*base,'--task',TASK]),
                                   ('registered_python',runner,[])]:
            if label=='registered_python':
                fixture.command('reset')
                fixture.command('open-decoy')
            run = call(entry,*args,'--background-only')
            state = fixture.command('state')
            check(label+'_sent_to_right_windows',state_ok(state,'alpha-result','beta-result')
                  and len(run.get('steps',[]))==5 and run.get('status') in ('verified','completed_unverified'),run)
            pids = [state['pid'], *(r['pid'] for r in state['devices'].values())]
            check(label+'_distinct_unowned_top_levels',len(set(pids))==4 and
                  all(not app.bridge.owned(r['hwnd'],state['hwnd']) for r in state['devices'].values()),pids)
        # A linked existing window can be revisited without running its opener.
        existing = dict(plan,name='Existing device',steps=[fill('existing-result',child('Decoy'))])
        tasks.save_task(existing,Path(env['DESKTOP_SELECT_TASK_DIR']))
        run = call(cli,'replay',*base,'--task',existing['name'],'--background-only')
        state = fixture.command('state')
        check('explicit_existing_child',run.get('status')=='verified'
              and state['devices']['Decoy']['value']=='existing-result'
              and state['devices']['Alpha']['writes']==state['devices']['Beta']['writes']==1,run)
        # Same title/exe is common across sessions. Never write the unrelated twin.
        twin = Fixture(work/DEVICE_EXE,'Decoy')
        try:
            result = call(cli,'replay',*base,'--task',existing['name'],'--background-only')
            check('matching_existing_twins_rejected',result.get('input_dispatched') is False
                  and result.get('status') in ('blocked','needs_review') and twin.command('state')['writes']==0
                  and fixture.command('state')['devices']['Decoy']['writes']==1,result)
        finally:
            twin.close()
        # A newly appeared window can match the recorded EXE and exact title
        # yet have been started by another application during the opener.
        from window_context import WindowRoutes, ContextError
        spec = dict(child('Impostor', 1), related=True)
        owner = app.bridge.Binding(app.pick_window(app.windows(manager.name),TITLE,fixture.ready['hwnd']))
        routes = WindowRoutes([launch('Alpha'),fill('must-not-write',spec)],owner,TITLE)
        routes.before(1)
        unrelated = Fixture(work/DEVICE_EXE, 'Impostor')
        try:
            routes.after(1,dict(status='delivered_unverified',input_dispatched=True))
            try:
                routes.resolve(spec)
            except ContextError as error:
                reason = error.code
            else:
                reason = 'accepted'
            check('fresh_unrelated_matching_window_rejected',reason=='recorded_window_relationship_changed'
                  and unrelated.command('state')['writes']==0,reason)
        finally:
            unrelated.close()
    finally:
        fixture.close()
        (work/'summary.json').write_text(json.dumps(checks,indent=2),encoding='utf8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skill-dir',type=Path,default=ROOT/'skills/desktop-select')
    parser.add_argument('--work-dir',type=Path,required=True)
    parser.add_argument('--probe-only',action='store_true')
    parser.add_argument('--record',action='store_true',
                        help='Exercise the live UIA recording pipeline with synthetic scoped input events')
    args = parser.parse_args()
    if os.name!='nt' or not args.work_dir.is_absolute() or args.work_dir.exists():
        parser.error('A new absolute Windows work directory is required')
    args.work_dir.mkdir(parents=True)
    exercise(args.skill_dir.resolve(),args.work_dir,compile_fixture(args.work_dir),args.probe_only,args.record)
