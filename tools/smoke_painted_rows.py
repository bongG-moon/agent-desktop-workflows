"""Exercise ordinary recorder hooks, persistence and replay on painted WinForms rows.

The fixture tells the demonstrator where its buttons are. The product only
receives mouse events and the same accessibility/pixel evidence as real use.
After demonstration, change the row order and check which buttons actually ran.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from smoke_040_rebuild import install_skill
from smoke_native import Fixture


ROOT = Path(__file__).resolve().parents[1]
TASK = 'Painted launcher first two'
LEGACY_TASK = 'Legacy launcher instructions'


def compile_fixture(work):
    framework = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319'
    target = work / 'PaintedRowsFixture.exe'
    refs = ['System.Windows.Forms', 'System.Drawing', 'System.Web.Extensions']
    subprocess.run([str(framework / 'csc.exe'), '/nologo', '/target:exe', '/out:' + str(target),
                    *['/r:' + str(framework / (name + '.dll')) for name in refs],
                    str(ROOT / 'tests/fixtures/PaintedRowsFixture.cs')], check=True)
    return target


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf8')


def exercise(skill, work, exe, include_legacy, include_workflow):
    installed = work / 'skills/desktop-select'
    install_skill(skill, installed)
    sys.path.insert(0, str(installed / 'scripts'))
    import desktop_select as app
    import learned_tasks as tasks
    import row_action
    import row_vision
    import teach_capture as recorder

    checks = []
    def check(name, condition, detail=None):
        checks.append(dict(case=name, passed=bool(condition)))
        print(json.dumps(checks[-1]), flush=True)
        if not condition:
            raise AssertionError((name, detail))

    app.bridge.set_dpi_awareness()
    fixture = Fixture(exe)
    session = None
    directory = work / 'tasks'
    cli = installed / 'scripts/desktop_select.py'
    env = dict(os.environ, DESKTOP_SELECT_TASK_DIR=str(directory))
    title, hwnd = fixture.ready['title'], fixture.ready['hwnd']

    def run_cli(script, *args):
        run = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(script), *args], cwd=work, env=env,
                             capture_output=True, encoding='utf8', timeout=260)
        if not run.stdout:
            raise AssertionError((script.name, run.returncode, run.stderr))
        return json.loads(run.stdout)

    def replay(verb, task=TASK):
        return run_cli(cli, verb, '--exe', exe.name, '--window', title, '--hwnd', str(hwnd), '--task', task)

    def target_check(point):
        binding.check()
        hit = app.bridge.window_from_point(*point)
        if (not binding.contains_window(hit) or app.bridge.root_window(app.bridge.foreground()) != hwnd
                or app.bridge.window_text(hwnd) != title):
            raise AssertionError('synthetic_demo_target_changed')
        rect, = [r for r in row_vision.visible_rows(binding)
                 if r[0] <= point[0] < r[0] + r[2] and r[1] <= point[1] < r[1] + r[3]]
        row_vision.confirm_point(binding, dict(kind='before', row_bounds=rect, point=point))

    def demonstrate(name):
        frame = fixture.command('state')
        point = frame['points'][frame['order'].index(name)]
        target_check(point)
        assert app.bridge.user32.SetCursorPos(*point), 'synthetic_hover_failed'
        time.sleep(.7)
        target_check(point)
        app.bridge._combo_surface(binding)
        receipt = app.bridge._send_point(*point, 'click')
        assert receipt.get('input_dispatched') is True and receipt.get('ok'), receipt
        # The application processes the injected events on its own UI thread.
        deadline = time.monotonic() + 2
        while fixture.command('state')['calls'][name] != 1:
            if time.monotonic() >= deadline:
                raise AssertionError(('demonstration_not_applied', name))
            time.sleep(.05)

    try:
        binding = app.bridge.Binding(app.pick_window(app.windows(exe.name), title, hwnd))
        fixture.command('activate')
        deadline = time.monotonic() + 5
        while app.bridge.root_window(app.bridge.foreground()) != hwnd and time.monotonic() < deadline:
            time.sleep(.1)
        row_action.foreground(binding)
        session = recorder.CaptureSession(binding, title)
        session.start()
        # Let the actual recorder observe the untouched list before the demo.
        # Do not seed or synthesize any recording evidence from the fixture.
        time.sleep(1.5)
        demonstrate('Atlas')
        demonstrate('Birch')
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            session.poll()
            steps = session.notes.snapshot()
            if len(steps) == 2 and all(s['action'] == 'row-action' for s in steps):
                break
            time.sleep(.1)
        recorded = session.finish()
        write_json(work / 'captured.json', recorded)
        steps = recorded['steps']
        check('ordinary_clicks_record_two_row_actions', [s['action'] for s in steps] == ['row-action'] * 2,
              [(s['action'], s.get('recognition', {}).get('reason')) for s in steps])
        expected = dict(Atlas=1, Birch=1, Cobalt=0)
        check('demonstrated_exact_rows', fixture.command('state')['calls'] == expected)
        edited = recorder.StepEdits().apply(steps)
        plan = tasks.from_recording(TASK, exe.name, title, edited)
        tasks.save_task(plan, directory)
        check('saved_and_loaded_two_visual_row_actions',
              [s['action'] for s in tasks.load_task(TASK, directory)['steps']] == ['row-action'] * 2)

        fixture.command('reset-reorder')
        reset = fixture.command('state')
        check('fixture_moved_first_recorded_row_to_third', reset['order'] == ['Cobalt', 'Birch', 'Atlas']
              and all(n == 0 for n in reset['calls'].values()))
        status = replay('row-status')
        check('row_status_found_both_stopped_rows_after_move', status.get('status') == 'observed'
              and [s['observed_state'] for s in status['steps']] == ['before'] * 2, status)
        first = replay('replay')
        write_json(work / 'replay.json', first)
        check('replay_verified_same_two_rows_individually', first.get('status') == 'verified'
              and len(first['steps']) == 2 and all(s['receipt'].get('input_dispatched') is True
              and s['receipt'].get('observed_state') == 'after' for s in first['steps']), first)
        after = fixture.command('state')
        write_json(work / 'reordered-state.json', after)
        check('reordered_replay_pressed_only_recorded_buttons', after['calls'] == expected)
        second = replay('replay')
        write_json(work / 'repeat.json', second)
        check('replay_of_running_rows_sent_zero_inputs', second.get('status') == 'verified'
              and second['input_dispatched'] is False and fixture.command('state')['calls'] == expected, second)
        if include_workflow:
            fixture.command('reset-reorder')
            generated = installed.parent / 'painted-launcher'
            started = time.perf_counter()
            registered = run_cli(installed / 'scripts/python_workflow.py', 'register',
                                 '--task', TASK, '--out', str(generated), '--hwnd', str(hwnd))
            registration_seconds = time.perf_counter() - started
            check('registered_painted_rows_as_python_skill', registered.get('status') == 'registered'
                  and registered['input_dispatched'] is False, registered)
            runner = generated / 'scripts/run.py'
            preview = run_cli(runner, '--input-plan')
            check('python_skill_declares_physical_pointer', preview.get('input_dispatched') is False
                  and preview['input_requirements']['physical_input_required'] is True, preview)
            started = time.perf_counter()
            run = run_cli(runner)
            write_json(work / 'timings.json', dict(python_registration_seconds=round(registration_seconds,3),
                       python_two_stopped_rows_seconds=round(time.perf_counter()-started,3)))
            write_json(work / 'python-skill-run.json', run)
            check('generated_python_skill_runs_only_taught_buttons', run.get('status') == 'verified'
                  and fixture.command('state')['calls'] == expected, run)
        if include_legacy:
            fixture.command('reset-reorder')
            # Reproduce the 0.4.9 on-disk schema without touching a user's task.
            # One full instruction was stored in the alias; another in the editor.
            legacy = dict(schema=1, name=LEGACY_TASK, exe=exe.name, window=title, steps=[
                dict(action='manual', reason='stable_locator_unavailable', alias='첫번째 요소 실행버튼 누르기',
                     instruction_draft='화면의 대상 이름과 실행할 동작을 입력하세요.'),
                dict(action='manual', reason='stable_locator_unavailable', instruction_draft='두 번째 행을 실행해 줘')])
            tasks.save_task(legacy, directory)
            original = tasks.load_task(LEGACY_TASK, directory)
            expected = dict(Atlas=0, Birch=1, Cobalt=1)
            first = replay('replay', LEGACY_TASK)
            write_json(work / 'legacy-replay.json', first)
            if first.get('status') != 'verified':
                import row_capture
                import row_intent
                frame = row_capture.sample(binding)
                state = fixture.command('state')
                state['glyphs'] = []
                rows = zip(frame.rows, frame.pixels) if frame is not None else ()
                for i, ((rect, _), pixels) in enumerate(rows):
                    width, height = rect[2:]
                    # The fixture only contains invented labels, never user data.
                    (work / f'synthetic-row-{i+1}.ppm').write_bytes(row_vision._ppm_header(rect[2:]) + pixels)
                    state['glyphs'].append(row_intent.play_glyphs(pixels, width, height))
                write_json(work / 'legacy-diagnostic.json', state)
            check('legacy_sentences_ran_first_and_second_current_rows', first.get('status') == 'verified'
                  and fixture.command('state')['calls'] == expected, first)
            second = replay('replay', LEGACY_TASK)
            write_json(work / 'legacy-repeat.json', second)
            check('legacy_repeat_skips_running_rows', second.get('status') == 'verified'
                  and second['input_dispatched'] is False and fixture.command('state')['calls'] == expected, second)
            check('original_legacy_task_preserved', original == tasks.load_task(LEGACY_TASK, directory))
    finally:
        if session and not session.ended:
            session.cancel()
        fixture.close()
        write_json(work / 'summary.json', dict(checks=checks, passed=sum(row['passed'] for row in checks),
                                               total=len(checks), app='WinForms owner-drawn ListBox'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skill-dir', type=Path, default=ROOT / 'skills/desktop-select')
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--compile-only', action='store_true')
    parser.add_argument('--include-legacy', action='store_true', help='Also replay old recordings with row instructions')
    parser.add_argument('--include-workflow', action='store_true', help='Register and run the generated Python skill')
    args = parser.parse_args()
    if os.name != 'nt' or not args.work_dir.is_absolute() or args.work_dir.exists():
        parser.error('Requires Windows and a new absolute work directory')
    args.work_dir.mkdir(parents=True)
    target = compile_fixture(args.work_dir)
    if not args.compile_only:
        exercise(args.skill_dir.resolve(), args.work_dir, target, args.include_legacy, args.include_workflow)
