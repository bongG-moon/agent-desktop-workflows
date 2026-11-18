"""Use the packaged public entry points to prepare, correct and repeat a legacy recording."""
import argparse
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
from zipfile import ZipFile

from smoke_desktop_context import compile_fixture, decoded_cells
from smoke_native import Fixture
from smoke_registered_export import public_call


def exercise(archive, work, *, ask_at_run=False):
    with ZipFile(archive) as package:
        package.extractall(work)
    scripts = work / 'desktop-select' / 'scripts'
    sys.path.insert(0, str(scripts))
    import context_menu
    import desktop_select as app
    from review_drafts import ReviewStorage
    from row_action import foreground
    from teach_capture import manual

    app.bridge.set_dpi_awareness()
    exe = compile_fixture(work)
    book = work / 'output.xlsx'
    task_dir = work / 'tasks'
    env = dict(os.environ, DESKTOP_SELECT_TASK_DIR=str(task_dir), LOCALAPPDATA=str(work / 'user-data'),
               PYTHONNOUSERSITE='1', PYTHONPATH='')
    registered = work / 'export-report'
    runner = registered / 'scripts' / 'run.py'
    times = {}

    def call(label, script, *args):
        result, code, seconds = public_call(script, *args, cwd=work, env=env)
        times[label] = round(seconds, 3)
        print(label, result['status'], result.get('diagnostic', ''), times[label], flush=True)
        return result

    fixture = Fixture(exe, str(book), '--group', ready_timeout=25)
    try:
        title = fixture.ready['title']
        binding = app.bridge.Binding(app.pick_window(app.windows(exe.name), title, fixture.ready['hwnd']))
        assert binding.pid == fixture.proc.pid
        foreground(binding)
        state = fixture.command('state')
        x, y, w, h = state['grid_bounds']
        anchor = context_menu.teach_surface(binding, (x + w // 7, y + h // 2))
        # An old draft with separate captions, a saved click and no menu-item selectors.
        rows = []
        for reason, text in [
            ('capture_target_unavailable', "MES Setup 조건에  'N'을 입력해줘 (이건 실행 전 어떤 값을 넣을 지 물어봐줘)"
             if ask_at_run else "MES SETUP조건을 'N'으로 설정"),
            ('capture_target_unavailable', "Family 조건에 'Hybrid'선택" if ask_at_run else "Family 조건에 'Hybrid' 설정"),
            ('capture_target_unavailable', 'Search 버튼을 눌러줘'),
            ('right_click_requires_menu_teaching', '조회 결과 테이블에서 우클릭' if ask_at_run else '조회된 결과 표에서 우클릭해줘'),
            ('popup_needs_live_binding', 'Export to excel 클릭' if ask_at_run else 'Export to excel 선택'),
            ('unsupported_recorded_action', 'Active Sheet 클릭' if ask_at_run else 'Active sheet 선택'),
        ]:
            raw = manual(reason)
            if ask_at_run and len(rows) < 2:
                label, identifier = [('MES Setup', 'conditionState'), ('Family', 'conditionFamily')][len(rows)]
                raw.update(action='teach-target', label=label, field_label=label,
                           target=dict(kind='element', locator=dict(role='combobox', automation_id=identifier)),
                           recognition=dict(target='identified', action='requires_instruction', result='manual',
                                            reason='combo_choice_unproven'))
            if reason == 'right_click_requires_menu_teaching':
                raw['menu_anchor'] = anchor
            rows.append(dict(step=raw, form=dict(text=text, mode=None, alias='', argument='')))
        name = 'original-demo'
        ReviewStorage(name, exe.name, title, task_dir).save_draft(rows, 'instruction_target_mismatch', 1)
        original = (task_dir / (name + '.json')).read_bytes()
        prepared = call('prepare', scripts / 'desktop_select.py', 'recover', '--task', name,
                        '--prepare', '--save-as', 'resolved-demo')
        assert (prepared['status'] == 'recorded' and prepared['input_dispatched'] is False
                and prepared['observed_scopes'] == 1), prepared
        registration = call('register', scripts / 'python_workflow.py', 'register', '--task', 'resolved-demo',
                            '--out', registered)
        assert registration['status'] == 'registered' and not registration['input_dispatched'], registration
        recipe_path = registered / 'workflow.json'
        stored = json.loads(recipe_path.read_bytes())
        assert [s['seconds'] for s in stored['steps'] if s['action'] == 'wait'] == [5], stored
        status, family = [s for s in stored['steps'] if s['action'] == 'set-value']
        assert family['value'] == 'Hybrid', family
        first_value = 'Y' if ask_at_run else 'N'
        first_args, next_args = [], []
        if ask_at_run:
            parameter = status['parameter']
            assert parameter and 'value' not in status, status
            first_args = ['--set', f'{parameter}={first_value}']
            next_args = ['--set', f'{parameter}=N']
            needs_value = call('missing_value', runner, '--summary', '--allow-physical')
            assert needs_value.get('missing_parameters') == [parameter] and not needs_value['input_dispatched'], needs_value
        else:
            assert status['value'] == 'N', status
        menu = stored['steps'][-1]['context']['menu']
        assert [label.casefold() for label in menu['path']] == ['export to excel', 'active sheet'] and menu['point'] == anchor['point'], menu
        # The fixture now displays a separate sibling panel over the formerly valid point.
        # The rest of the painted result remains visible but exposes no accessible cells.
        fixture.command('cover-recorded-point')
        consent = call('no_consent', runner, '--summary', *first_args)
        assert not consent['input_dispatched'] and fixture.command('state')['queries'] == 0, consent
        remember = ['--remember-physical'] if ask_at_run else []
        first = call('first_test', runner, '--summary', '--allow-physical', *remember, *first_args)
        assert first.get('menu_repair') and first['readability_check']['count'] == 1, first
        state = fixture.command('state')
        assert state['queries'] == 1 and state['ready'] and state['right_clicks'] == 0, state
        ticket = first['menu_repair']['ticket']
        frame = call('repair_image', runner, '--repair-frame', ticket, '--allow-physical', *first_args)
        assert Path(frame['image']).is_file() and not frame['input_dispatched'], frame
        # The deterministic test host identifies the visible data row on the right.
        origin = frame['screen_origin']
        point = [x + w // 2 - origin[0], y + h // 3 - origin[1]]
        resumed = call('resume', runner, '--resume-menu', ticket, '--frame', frame['frame'],
                       '--point', *point, '--allow-physical', '--summary', *first_args)
        assert resumed.get('menu_corrections'), resumed
        if ask_at_run:
            assert resumed.get('repeat_physical_input') == 'approved', resumed
        updated = json.loads(recipe_path.read_bytes())['steps'][-1]['context']['menu']
        assert updated['point'] != menu['point'] and updated['path'] == menu['path'], updated
        assert fixture.command('state')['queries'] == 1
        for run_number in (1, 2):
            if run_number == 2:
                repeated = call('repeat_new_process', runner, '--summary', *(next_args if ask_at_run else ['--allow-physical']))
                assert not repeated.get('menu_repair') and not repeated.get('menu_corrections'), repeated
                assert repeated['readability_check']['count'] == 1, repeated
            deadline = time.monotonic() + 4
            while not book.is_file() and time.monotonic() < deadline:
                time.sleep(.05)
            state = fixture.command('state')
            assert all(state[k] == run_number for k in ('queries', 'right_clicks', 'parents', 'leaves')), state
            assert state['wrong_point'] == state['unrelated'] == 0, state
            cells = decoded_cells(book)
            expected = [first_value if run_number == 1 else 'N', 'Hybrid', str(run_number)]
            assert [cells[k] for k in ('A2', 'B2', 'C2')] == expected, cells
            book.rename(work / f'verified-{run_number}.xlsx')
        assert (task_dir / (name + '.json')).read_bytes() == original
        if ask_at_run:
            call('forget_repeat', runner, '--forget-physical')
            revoked = call('after_forget', runner, '--summary', *next_args)
            assert revoked['status'] == 'needs_consent' and not revoked['input_dispatched'], revoked
        return dict(original_steps=len(rows), original_preserved=True, prepare_scope_count=prepared['observed_scopes'],
                    preparation_lookup=prepared['lookup_modes'], queries=2, right_clicks=2,
                    workbooks_verified=2, separate_process_repeat=True, scope_reads_per_run=1, seconds=times)
    finally:
        fixture.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--ask-at-run', action='store_true',
                        help='Use a quoted sample with a required runtime value and test a remembered repeat.')
    args = parser.parse_args()
    with TemporaryDirectory(prefix='desktop-select-menu-') as scratch:
        print(json.dumps(exercise(args.archive.resolve(), Path(scratch), ask_at_run=args.ask_at_run)))
