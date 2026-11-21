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


def exercise_legacy_draft(command, check, fixture, binding, folder, exe, title):
    import review_drafts
    import row_vision
    from teach_capture import manual

    steps = [('unsupported_recorded_action', '기존 조회 결과', None),
             ('double_click_needs_review', '나온 테이블 결과를 더블 클릭해서 팝업 창을 띄워줘', None),
             ('drag_needs_review', '상세 창의 메모 가져오기', 'required')]
    rows = [dict(step=manual(reason, str(i)), form=dict(alias='', text=text, mode=mode, argument=''))
            for i, (reason, text, mode) in enumerate(steps, 1)]
    source = review_drafts.ReviewStorage('legacy', exe.name, title, folder)
    source.save_draft(rows, 'invalid_step_instruction', 3)
    before = (folder/'legacy.json').read_bytes()
    frame = command('ground', '--task', 'legacy', '--only', '2')
    check('legacy_draft_can_observe_current_result', frame.get('gesture') == 'double-click'
          and frame['input_dispatched'] is False)
    left, top, _, _ = row_vision._window_rect(binding)
    state = fixture.command('state')
    values = {'<x>': str(state['x'] - left), '<y>': str(state['y'] - top), '<new task>': 'repaired'}
    fixed = command(*(values.get(arg, arg) for arg in frame['bind_args']))
    check('repaired_only_one_step_and_kept_original', fixed.get('input_dispatched') is False
          and fixed.get('test_step_args') and (folder/'legacy.json').read_bytes() == before)
    result = command(*fixed['test_step_args'])
    until(lambda: fixture.command('state')['opened'] == state['opened'] + 1)
    check('replayed_repaired_step_with_other_steps_unfinished', result.get('input_dispatched') is True
          and fixture.command('state')['detailHwnd'] != 0)


def exercise(source, work, double_click=False, list_row=False, toolbar=False):
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
    fixture = Fixture(exe, *(['--details'] if double_click else []), *(['--list'] if list_row else []),
                      *(['--toolbar'] if toolbar else []))
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
        if toolbar:
            with app.bridge.UIA() as uia:
                hit = uia.from_point(x, y)
                check('provider_exposes_toolbar_instead_of_button', hit is not None and hit.property(30003) == 50021)
        old_cursor = app.bridge.cursor()
        if app.bridge.root_window(app.bridge.window_from_point(x, y)) != binding.hwnd:
            raise RuntimeError('fixture_point_not_exposed')
        app.bridge.user32.SetCursorPos(x, y)
        try:
            until(lambda: session.visual.latest is not None and session.visual.latest[4]==(x,y))
        except RuntimeError:
            with app.bridge.UIA() as uia:
                # Surface the provider diagnostic when this fixture cannot be sampled.
                session.visual._sample(uia)
            raise
        if (app.bridge.cursor() != (x,y) or app.bridge.root_window(app.bridge.foreground()) != binding.hwnd
                or app.bridge.root_window(app.bridge.window_from_point(x, y)) != binding.hwnd):
            raise RuntimeError('fixture_input_changed')
        gesture = 'double-click' if double_click else 'click'
        counter = 'opened' if double_click else 'pressed'
        result = app.bridge._send_point(x, y, gesture)
        if result['ok']:
            until(lambda: fixture.command('state')[counter] > 0)
        check('demonstrated_once', result['ok'] and fixture.command('state')[counter] == 1)
        until(lambda: bool(session.notes.snapshot()))
        raw = session.finish()['steps']
        check('recorded_canvas_click', len(raw)==1 and raw[0].get('action')=='visual-click')
        edits = StepEdits()
        sentence = '결과 항목을 더블클릭해서 상세 창을 열어줘' if double_click else '포켓 게임 버튼 눌러서 시행 (pocket)'
        edits.store(raw[0], '', sentence)
        task = learned_tasks.from_recording('Painted icon', exe.name, title, edits.apply(raw))
        directory = work/'tasks'
        learned_tasks.save_task(task, directory)
        saved = learned_tasks.load_task(task['name'], directory)
        check('sentence_and_preclick_target_saved', saved['steps'][0]['label'] == sentence
              and saved['steps'][0]['action']=='visual-click'
              and saved['steps'][0]['visual'].get('gesture', 'click') == gesture
              and recorded_plan.compile_plan(saved).get('envelope'))
        if double_click:
            fixture.command('dismiss')
            foreground(binding)
        moved = fixture.command('move-icon')
        check('target_moved_before_replay', (moved['x'], moved['y']) != (x, y) and moved[counter]==1)
        def command(*args):
            result = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(installed/'scripts/desktop_select.py'), *args],
                                    capture_output=True,encoding='utf8',timeout=60,
                                    env=dict(os.environ, DESKTOP_SELECT_TASK_DIR=str(directory)))
            return json.loads(result.stdout)
        window = ['--exe', exe.name, '--window', title]
        replay = ['replay', '--task', task['name'], *window]
        background = command(*replay, '--background-only')
        check('physical_requirement_checked_before_input', background['input_dispatched'] is False
              and fixture.command('state')[counter]==1)
        result = command(*replay)
        state = fixture.command('state')
        check('replay_clicked_relocated_icon_once', result['input_dispatched'] is True
              and state[counter]==2 and state['missed']==0)
        if toolbar:
            fixture.command('duplicate-icon')
            result = command(*replay)
            after = fixture.command('state')
            check('two_matching_icons_stop_without_input', result.get('input_dispatched') is False
                  and 'visual_target_ambiguous' in json.dumps(result)
                  and (after[counter], after['missed']) == (2, 0))
        if double_click:
            popups = app.owned_popups(binding)
            check('double_click_opened_owned_detail', state['detailHwnd'] in [p['hwnd'] for p in popups])
            popup = [*window, '--popup', str(state['detailHwnd'])]
            read = command('inspect', *popup, '--target', 'CommentText', '--role', 'textfield')
            check('read_actual_detail_field', read.get('value') == 'Ready for inspection'
                  and read['input_dispatched'] is False)
            close = command('open', *popup, '--target', 'CloseDetails', '--role', 'button')
            until(lambda: fixture.command('state')['detailHwnd'] == 0)
            check('closed_only_owned_detail', close.get('input_dispatched') is True
                  and fixture.command('state')['opened'] == 2)
            if list_row:
                foreground(binding)
                exercise_legacy_draft(command, check, fixture, binding, directory, exe, title)
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
    parser.add_argument('--double-click', action='store_true', help='open, read and close details in the owned fixture window')
    parser.add_argument('--list-row', action='store_true', help='exercise a double-click on a native list row')
    parser.add_argument('--toolbar', action='store_true', help='test an unnamed painted toolbar button and its duplicate')
    args=parser.parse_args()
    if args.toolbar and (args.double_click or args.list_row):
        parser.error('--toolbar tests a single click on the painted button')
    work = args.work_dir.resolve()
    work.mkdir(parents=True,exist_ok=False)
    exercise(args.skill_dir.resolve(),work,args.double_click or args.list_row,args.list_row,args.toolbar)
