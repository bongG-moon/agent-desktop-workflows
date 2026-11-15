"""Record and replay an opaque Windows icon. Uses the physical pointer on the fixture only."""
import argparse
from pathlib import Path
import json
import os
import subprocess
import sys
import time

from smoke_040_rebuild import install_skill
from smoke_native import Fixture


ROOT = Path(__file__).resolve().parents[1]


def until(predicate, seconds=6):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.05)
    raise RuntimeError('fixture_did_not_reach_expected_state')


def exercise(source, work):
    installed = work/'skills/desktop-select'
    install_skill(source, installed)
    sys.path.insert(0, str(installed/'scripts'))
    import desktop_select as app
    import learned_tasks
    import recorded_plan
    from row_action import foreground
    from teach_capture import CaptureSession, StepEdits

    app.bridge.set_dpi_awareness()
    exe = work/'PaintedIconFixture.exe'
    compiler = Path(os.environ['WINDIR'])/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    subprocess.run([str(compiler), '/nologo', '/out:' + str(exe), '/r:System.Windows.Forms.dll',
                    '/r:System.Drawing.dll', '/r:System.Web.Extensions.dll',
                    str(ROOT/'tests/fixtures/PaintedIconFixture.cs')], check=True, capture_output=True)
    fixture = Fixture(exe)
    checks = []
    def check(name, ok):
        checks.append(dict(case=name, passed=bool(ok)))
        print(json.dumps(checks[-1]), flush=True)
        if not ok:
            raise AssertionError(name)
    session = None
    try:
        opened = fixture.ready
        title = opened['title']
        binding = app.bridge.Binding(app.pick_window(app.windows(exe.name), title, opened['hwnd']))
        foreground(binding)
        session = CaptureSession(binding, title)
        session.start()
        observed = fixture.command('state')
        x, y = observed['x'], observed['y']
        old_cursor = app.bridge.cursor()
        if app.bridge.root_window(app.bridge.window_from_point(x, y)) != binding.hwnd:
            raise RuntimeError('fixture_point_not_exposed')
        app.bridge.user32.SetCursorPos(x, y)
        until(lambda: session.visual.latest is not None and session.visual.latest[4]==(x,y))
        if (app.bridge.cursor() != (x,y) or app.bridge.root_window(app.bridge.foreground()) != binding.hwnd
                or app.bridge.root_window(app.bridge.window_from_point(x, y)) != binding.hwnd):
            raise RuntimeError('fixture_input_changed')
        result = app.bridge._send_point(x, y, 'click')
        if result['ok']:
            until(lambda: fixture.command('state')['pressed'] > 0 or fixture.command('state')['missed'] > 0)
        check('demonstrated_once', result['ok'] and fixture.command('state')['pressed'] == 1)
        until(lambda: bool(session.notes.snapshot()))
        raw = session.finish()['steps']
        check('recorded_canvas_click', len(raw)==1 and raw[0].get('action')=='visual-click')
        edits = StepEdits()
        sentence = '포켓 게임 버튼 눌러서 시행 (pocket)'
        edits.store(raw[0], '', sentence)
        task = learned_tasks.from_recording('Painted icon', exe.name, title, edits.apply(raw))
        directory = work/'tasks'
        learned_tasks.save_task(task, directory)
        saved = learned_tasks.load_task(task['name'], directory)
        check('sentence_and_preclick_target_saved', saved['steps'][0]['label'] == sentence
              and saved['steps'][0]['action']=='visual-click' and recorded_plan.compile_plan(saved).get('envelope'))
        moved = fixture.command('move-icon')
        check('icon_moved_before_replay', moved['x']!=x and moved['pressed']==1)
        cli = [sys.executable, '-X', 'utf8', '-B', str(installed/'scripts/desktop_select.py'), 'replay',
               '--task', task['name'], '--exe', exe.name, '--window', title]
        def replay(flags):
            result = subprocess.run([*cli,*flags],capture_output=True,encoding='utf8',timeout=60,
                                    env=dict(os.environ, DESKTOP_SELECT_TASK_DIR=str(directory)))
            return json.loads(result.stdout)
        background = replay(['--background-only'])
        check('physical_requirement_checked_before_input', background['input_dispatched'] is False
              and fixture.command('state')['pressed']==1)
        result = replay([])
        state = fixture.command('state')
        check('replay_clicked_relocated_icon_once', result['input_dispatched'] is True
              and state['pressed']==2 and state['missed']==0)
        if app.bridge.root_window(app.bridge.foreground())==binding.hwnd and app.bridge.cursor()==(moved['x'],moved['y']):
            app.bridge.user32.SetCursorPos(*old_cursor)
    finally:
        if session is not None and not session.ended:
            session.cancel()
        fixture.close()
        (work/'summary.json').write_text(json.dumps(checks,indent=2),encoding='utf8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skill-dir',type=Path,required=True)
    parser.add_argument('--work-dir',type=Path,required=True)
    args=parser.parse_args()
    work = args.work_dir.resolve()
    work.mkdir(parents=True,exist_ok=False)
    exercise(args.skill_dir.resolve(),work)
