"""Make a new six-step demonstration, then test with a different ID in a disposable Windows fixture."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from zipfile import ZipFile

from smoke_native import Fixture

ROOT = Path(__file__).resolve().parents[1]
INSTRUCTIONS = ['입력받은 변수를 Equip ID 입력란에 입력해줘', 'VIEW 버튼을 눌러줘', 'Equip ID 일치 확인',
                '해당 결과 행을 더블클릭해 상세 창을 열어줘', 'Comment 값을 읽어줘', 'Exit 버튼을 눌러 상세 창을 닫아줘']


def exercise(package, work, painted=False, external_host=False):
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

    net = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319'
    exe = work / 'LookupDetailsFixture.exe'
    libs = [net / 'WPF' / (name + '.dll') for name in
            ('PresentationFramework', 'PresentationCore', 'WindowsBase', 'UIAutomationTypes', 'UIAutomationProvider')]
    subprocess.run([str(net/'csc.exe'), '/nologo', '/target:exe', '/out:'+str(exe),
                    *['/r:'+str(lib) for lib in libs], '/r:System.Xaml.dll', '/r:System.Web.Extensions.dll',
                    str(ROOT/'tests/fixtures/LookupDetailsFixture.cs')], check=True)
    win = app.bridge
    win.set_dpi_awareness()
    fixture = Fixture(exe, *(['--painted'] if painted else []))
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
        def check():
            hit = win.window_from_point(x, y)
            assert win.hwnd_pid(hit) == binding.pid and win.root_window(hit) == hwnd, 'fixture_point_covered'
            assert win.root_window(win.foreground()) == hwnd and not win.pointer_keys_down(), 'foreground_changed'
        check()
        assert win.user32.SetCursorPos(x, y)
        settle(.5)
        check()
        assert win.cursor() == (x,y) and win._send_point(x, y, gesture)['ok']
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
        demonstrate('cell')
        demonstrate('cell','double-click')
        assert fixture.command('state')['detailOpen']
        settle(.7)
        demonstrate('note')
        demonstrate('exit')
        raw = trail.finish()['steps']
        durations['demonstration'] = round(time.monotonic()-start,3)
        (work/'captured.json').write_text(json.dumps(raw,ensure_ascii=False),encoding='utf8')
        assert len(raw) == len(INSTRUCTIONS), [(s['action'],s['label']) for s in raw]

        start = time.monotonic()
        directory = work/'tasks'
        store = ReviewStorage('New lookup',exe.name,title,directory)
        edits = StepEdits()
        for step, text in zip(raw, INSTRUCTIONS):
            edits.store(step,'',text)
        saved = store.save_recording(finalize_contexts(edits.apply(raw)))
        assert saved['status'] == 'recorded'
        durations['review_save'] = round(time.monotonic()-start,3)
        before = fixture.command('reset')
        assert before['last']=='DEMO' and before['closed']==1 and before['queries']==1
        env = dict(os.environ,DESKTOP_SELECT_TASK_DIR=str(directory))
        launcher = [sys.executable,'-X','utf8','-B',str(scripts/'desktop_select.py')]
        trial_args = ['try-recording', '--task',store.name,'--set','Equip ID=LAB-42','--allow-physical']
        def call_cli(arguments):
            start = time.monotonic()
            call = subprocess.run(launcher + arguments,capture_output=True,encoding='utf8',cwd=work,env=env,timeout=90)
            if not call.stdout.strip():
                raise RuntimeError(call.stderr)
            return json.loads(call.stdout), round(time.monotonic()-start,3)
        if external_host:
            print(json.dumps(dict(status='saved_waiting_for_first_trial', task=store.name, task_directory=str(directory),
                                  timings=durations)), flush=True)
            input()  # The host performs the first trial. Verify its evidence and fixture counters below.
            receipts = list((directory/'first-trials').glob('*.json'))
            assert len(receipts) == 1, 'expected one first-trial receipt'
            report = json.loads(receipts[0].read_text(encoding='utf8'))
        else:
            report, durations['first_trial'] = call_cli(trial_args)
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
        print(json.dumps(dict(status='passed',timings=durations,trial_calls=3 if painted else 1,query_delta=1,double_click_delta=1,
                              exit_delta=1,observed='Note for LAB-42',repeat_dispatched=False)))
    finally:
        if trail.started and not trail.ended:
            trail.cancel()
        fixture.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package',type=Path)
    parser.add_argument('work',type=Path)
    parser.add_argument('--painted',action='store_true',help='draw result cell text as pixels and read a window-image point on stdin')
    parser.add_argument('--external-host',action='store_true',help='pause after saving so a separate host can run the first trial')
    args=parser.parse_args()
    exercise(args.package.resolve(strict=True),args.work.resolve(),args.painted,args.external_host)
