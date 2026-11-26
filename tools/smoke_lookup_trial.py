"""Record a new lookup, then test it with a different ID in a disposable Windows fixture."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from zipfile import ZipFile

from smoke_native import Fixture

ROOT = Path(__file__).resolve().parents[1]
INSTRUCTIONS = ['입력받은 변수를 Equip ID 입력란에 입력해줘', 'VIEW 버튼을 눌러줘', 'Equip ID 일치 확인',
                '해당 결과 행을 더블클릭해 상세 창을 열어줘', 'Comment 값을 읽어줘', 'Exit 버튼을 눌러 상세 창을 닫아줘']
DEMONSTRATED_STEPS = [*INSTRUCTIONS[:2], '녹화한 항목 더블클릭 (조회 결과 행 더블클릭)',
                      '3단계에서 열린 창에서 comment부분 내용 추출', INSTRUCTIONS[-1]]


def build_lookup_fixture(work):
    net = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319'
    exe = work / 'LookupDetailsFixture.exe'
    libs = [net / 'WPF' / (name + '.dll') for name in
            ('PresentationFramework', 'PresentationCore', 'WindowsBase', 'UIAutomationTypes', 'UIAutomationProvider')]
    subprocess.run([str(net/'csc.exe'), '/nologo', '/target:exe', '/out:'+str(exe),
                    *['/r:'+str(lib) for lib in libs], '/r:System.Xaml.dll', '/r:System.Web.Extensions.dll',
                    str(ROOT/'tests/fixtures/LookupDetailsFixture.cs'), str(ROOT/'tests/fixtures/ProviderResultGrid.cs')], check=True)
    return exe


def exercise(package, work, painted=False, external_host=False, demonstrated=False, legacy_point=False):
    work.mkdir(parents=True, exist_ok=False)
    with ZipFile(package) as archive:
        archive.extractall(work / '.claude/skills')
    scripts = work / '.claude/skills/desktop-select/scripts'
    sys.path.insert(0, str(scripts))
    import desktop_select as app
    import control_lookup
    import live_action
    from capture_trail import CaptureTrail, finalize_contexts
    from review_drafts import ReviewStorage
    from teach_capture import StepEdits
    from row_action import foreground

    exe = build_lookup_fixture(work)
    win = app.bridge
    win.set_dpi_awareness()
    fixture = Fixture(exe, *(['--group-only', '--scoped-detail'] if demonstrated else ['--painted'] if painted else []))
    instructions = DEMONSTRATED_STEPS if demonstrated else INSTRUCTIONS
    if demonstrated and not legacy_point:
        # Keep the recorder's own defaults for every demonstrated gesture.
        # Only the parameter and the requested read require user intent.
        instructions = [instructions[0], None, None, instructions[3], None]
    title = fixture.ready['title']
    binding = win.Binding(dict(pid=fixture.ready['pid'], hwnd=fixture.ready['hwnd'], exe_name=exe.name))
    trail = CaptureTrail(binding, title)
    durations = {}

    def settle(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            trail.poll()
            time.sleep(.05)

    def demonstrate(key, gesture='click'):
        spot = fixture.command('point:'+key)
        x, y, hwnd = spot['x'], spot['y'], spot['hwnd']
        def check_window(point):
            hit = win.window_from_point(*point)
            assert win.hwnd_pid(hit) == binding.pid and win.root_window(hit) == hwnd, 'fixture_point_covered'
            assert win.root_window(win.foreground()) == hwnd, 'foreground_changed'
        check_window((x,y))
        assert not win.pointer_keys_down(), 'input_busy'
        assert win.user32.SetCursorPos(x, y)
        settle(.5)
        check_window((x,y))
        assert not win.pointer_keys_down(), 'input_busy'
        assert win.cursor() == (x,y), 'pointer_moved_before_demonstration'
        if gesture == 'drag':
            end = fixture.command('point:note-end')
            assert end['hwnd'] == hwnd
            destination = end['x'], end['y']
            check_window(destination)
            def button(flags):
                event = win._mouse(flags=flags)
                assert win.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event)) == 1, 'drag_send_failed'
            button(0x0002)
            try:
                settle(.2)
                check_window(destination)
                assert win.user32.SetCursorPos(*destination)
                settle(.2)
            finally:
                button(0x0004)
        else:
            delivered = win._send_point(x, y, gesture)
            assert delivered['ok'], delivered
        settle(.75)

    try:
        foreground(binding)
        start = time.monotonic()
        trail.start()
        demonstrate('field')
        with win.UIA() as uia:
            field = control_lookup.resolve(binding,uia,dict(role='textfield',automation_id='queryField',
                          ancestors=[dict(role='group',automation_id='Filters')]))
            live_action._call(field,'SetValue','DEMO',binding.check,lambda _:None)
        settle(.45)
        demonstrate('view')
        if not demonstrated:
            demonstrate('cell')
        demonstrate('cell','double-click')
        assert fixture.command('state')['detailOpen']
        settle(.7)
        demonstrate('note','drag' if demonstrated else 'click')
        demonstrate('exit')
        raw = trail.finish()['steps']
        durations['demonstration'] = round(time.monotonic()-start,3)
        (work/'captured.json').write_text(json.dumps(raw,ensure_ascii=False),encoding='utf8')
        assert len(raw) == len(instructions), [(s['action'],s['label']) for s in raw]
        if demonstrated:
            location = app.tasks.result_location(raw[2])
            assert location['result_surface']['automation_id'] == 'PaintedResults' and location.get('result_point'), raw[2]
            assert raw[3]['recognition']['reason'] == 'drag_needs_review', raw[3]
            assert raw[3]['target']['locator'].get('automation_id') == 'CommentText', raw[3]
            if not legacy_point:
                assert raw[-1]['action'] == 'click' and raw[-1]['target']['locator'].get('automation_id') == 'btnClose', raw[-1]
            if legacy_point:
                # Emulate the earlier recorder's actual output after verifying what the
                # current recorder collected. Preserve related window and opener evidence.
                for step, reason in zip(raw[2:4], ('double_click_needs_review', 'drag_needs_review')):
                    old = {k:step[k] for k in ('id','_capture_window','window_context') if k in step}
                    old.update(label='직접 확인할 단계',action='manual',target={'kind':'manual','locator':{}},
                               recognition={'action':'manual','result':'manual','reason':reason})
                    if reason == 'double_click_needs_review':
                        old['result_surface'] = location['result_surface']
                    step.clear()
                    step.update(old)

        start = time.monotonic()
        directory = work/'tasks'
        store = ReviewStorage('New lookup',exe.name,title,directory)
        edits = StepEdits()
        for step, text in zip(raw, instructions):
            if text is not None:
                edits.store(step,'',text)
        saved = store.save_recording(finalize_contexts(edits.apply(raw)))
        assert saved['status'] == 'recorded'
        if demonstrated and not legacy_point:
            body = store.saved_plan['steps'][2]
            assert body['action'] == 'double-click-result' and body.get('result_point') and 'visual' not in body
        durations['review_save'] = round(time.monotonic()-start,3)
        before = fixture.command('reset')
        assert before['last']=='DEMO' and before['closed']==1 and before['queries']==1
        env = dict(os.environ,DESKTOP_SELECT_TASK_DIR=str(directory))
        trial_args = ['try-recording', '--task',store.name,'--set','Equip ID=LAB-42','--allow-physical']
        review_env = {}
        if demonstrated:
            driver = work/'fixture-driver'
            driver.mkdir()
            shutil.copyfile(ROOT/'tests/fixtures/registered_result_ui_driver.py', driver/'sitecustomize.py')
            if legacy_point:
                picker_receipt = work/'local-picker.json'
                review_env = dict(PYTHONPATH=str(driver), DESKTOP_SELECT_SMOKE_UI_REVIEW=json.dumps(dict(
                    layout=fixture.command('review-layout'), row_policy='recorded', receipt=str(picker_receipt))))
                trial_args.append('--local-result-review')
            elif not external_host:
                first_trace = work/'first-trial-element-trace.txt'
                review_env = dict(PYTHONPATH=str(driver), DESKTOP_SELECT_SMOKE_ELEMENT_TRACE=str(first_trace))
        def call_cli(arguments, entry=scripts/'desktop_select.py', extra_env=None):
            start = time.monotonic()
            call = subprocess.run([sys.executable,'-X','utf8','-B',str(entry), *arguments],
                                  capture_output=True,encoding='utf8',cwd=work,env={**env, **(extra_env or {})},timeout=90)
            if not call.stdout.strip():
                raise RuntimeError(call.stderr)
            return json.loads(call.stdout), round(time.monotonic()-start,3)
        if external_host:
            print(json.dumps(dict(status='saved_waiting_for_first_trial', task=store.name, task_directory=str(directory),
                                  timings=durations, **({'fixture_review_env':review_env} if legacy_point else {}))), flush=True)
            input()  # The host performs the first trial. Verify its evidence and fixture counters below.
            receipts = [json.loads(path.read_text(encoding='utf8')) for path in (directory/'first-trials').glob('*.json')]
            if legacy_point:
                report, = [item for item in receipts if item.get('correction_saved')]
            else:
                report, = receipts
        else:
            report, durations['first_trial'] = call_cli(trial_args, extra_env=review_env)
            if demonstrated and not legacy_point:
                assert set(first_trace.read_text(encoding='utf8').splitlines()) == {'launcher_guard_enabled', 'worker_guard_enabled'}
        (work/'trial.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf8')
        if painted:
            assert report.get('host_action') == 'inspect_current_result_without_replaying', report
            intermediate = fixture.command('state')
            assert (intermediate['queries'],intermediate['opened'],intermediate['closed']) == (2,1,1), intermediate
            frame, durations['inspect_current_window'] = call_cli(report['result_inspection']['args'])
            assert frame.get('input_dispatched') is False and frame.get('image'), frame
            print(json.dumps(dict(inspect_image=frame['image'], width=frame['width'], height=frame['height'],
                                  prompt='Read the displayed Equip ID column and enter image x y for LAB-42.')),
                  flush=True)
            x, y = input().split()
            report, durations['resume_remaining_steps'] = call_cli([
                'resume-trial','--receipt',report['trial_receipt'],'--frame',frame['frame'],
                '--column','Equip ID','--value','LAB-42','--point',x,y,'--allow-physical'])
            (work/'resume.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf8')
        after = fixture.command('state')
        assert (after['last'],after['queries'],after['opened'],after['closed'],after['archiveClicks'],after['detailOpen']) == (
            'LAB-42',2,2,2,0,False), (after,report)
        assert 'Note for LAB-42' in json.dumps(report), report
        repeat, durations['same_request'] = call_cli(trial_args)
        assert fixture.command('state')==after and repeat.get('previous_trial') and 'Note for LAB-42' in json.dumps(repeat), repeat
        summary = dict(status='passed',timings=durations,trial_calls=3 if painted else 1,query_delta=1,double_click_delta=1,
                       exit_delta=1,observed='Note for LAB-42',repeat_dispatched=False)
        if demonstrated:
            if legacy_point:
                assert report.get('correction_saved') and report.get('prior_steps_replayed') is False, report
                assert report['effective_task'] != store.name and json.loads(picker_receipt.read_text())['selected_cell']
                assert report['status'] in ('verified', 'completed_unverified') and 'stopped_at' not in report, report
            else:
                assert report['status'] == 'verified', report
            registered = work/'.claude/skills/lookup-note'
            registration, durations['register'] = call_cli(
                ['register','--task',report['effective_task'],'--out',str(registered)], scripts/'python_workflow.py')
            assert registration['status'] == 'registered', registration
            no_image_trace = work/'registered-element-trace.txt'
            run, durations['registered_run'] = call_cli(
                ['--set','Equip ID=LAB-77','--allow-physical','--summary'],registered/'scripts/run.py',
                dict(PYTHONPATH=str(driver), DESKTOP_SELECT_SMOKE_ELEMENT_TRACE=str(no_image_trace)))
            (work/'registered-run.json').write_text(json.dumps(run,indent=2,ensure_ascii=False),encoding='utf8')
            final = fixture.command('state')
            assert (final['last'],final['queries'],final['opened'],final['closed'],final['archiveClicks'],final['wrongRows'],
                    final['detailOpen']) == ('LAB-77',3,3,3,0,0,False), (final,run)
            assert run.get('status') == 'verified' and 'Note for LAB-77' in json.dumps(run), run
            trace = no_image_trace.read_text(encoding='utf8').splitlines()
            assert 'worker_guard_enabled' in trace and set(trace) <= {'launcher_guard_enabled','worker_guard_enabled'}, trace
            summary.update(registered_observed='Note for LAB-77', total_queries=3, wrong_rows=final['wrongRows'],
                           registered_image_capture=False, local_picker=legacy_point)
        print(json.dumps(summary))
    finally:
        if trail.started and not trail.ended:
            trail.cancel()
        fixture.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package',type=Path)
    parser.add_argument('work',type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--painted',action='store_true',help='draw result cell text as pixels and read a window-image point on stdin')
    mode.add_argument('--demonstrated',action='store_true',help='record a body double-click and text drag on an owner-drawn grid, then register and run again')
    parser.add_argument('--legacy-point',action='store_true',help='omit the old recording position and field locator and select the position once in the local review window')
    parser.add_argument('--external-host',action='store_true',help='pause after saving so a separate host can run the first trial')
    args=parser.parse_args()
    if args.legacy_point and not args.demonstrated:
        parser.error('--legacy-point requires --demonstrated')
    exercise(args.package.resolve(strict=True),args.work.resolve(),args.painted,args.external_host,args.demonstrated,args.legacy_point)
