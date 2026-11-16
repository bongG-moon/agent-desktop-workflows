"""Run a legacy six-row draft through the public recovery CLI and a real Windows app.

The synthetic host supplies the same declarative decisions a host model would
make. Only the fixture's initial window identity and final outcome use its pipe.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from zipfile import ZipFile

from smoke_desktop_context import compile_fixture, decoded_cells
from smoke_native import Fixture


def exercise(package, work):
    with ZipFile(package) as archive:
        archive.extractall(work)
    skill = work / 'desktop-select' / 'scripts'
    sys.path.insert(0, str(skill))
    import desktop_select as app
    import review_drafts
    import row_action
    from teach_capture import manual

    app.bridge.set_dpi_awareness()
    exe = compile_fixture(work)
    book = work / 'result.xlsx'
    fixture = Fixture(exe, str(book), '--group', ready_timeout=25)
    try:
        title = fixture.ready['title']
        owner = app.bridge.Binding(app.pick_window(app.windows(exe.name), title, fixture.ready['hwnd']))
        row_action.foreground(owner)
        store = work / 'tasks'
        env = dict(os.environ, DESKTOP_SELECT_TASK_DIR=str(store))

        def cli(script, *args):
            proc = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(script), *map(str, args)], cwd=work,
                                  env=env, capture_output=True, encoding='utf8', timeout=120)
            result = json.loads(proc.stdout)
            assert proc.returncode in (0, 2, 3), (result, proc.stderr)
            return result

        name = 'legacy'
        baseline = fixture.command('state')
        rows = []
        for reason, text in [
            ('capture_target_unavailable', "MES Setup에 'N'을 선택해줘"),
            ('capture_target_unavailable', 'Family에 입력받은 변수를 넣어줘'),
            ('capture_target_unavailable', 'Search 버튼을 누르고 5초 기다려줘'),
            ('right_click_requires_menu_teaching', '결과 데이터에서 우클릭'),
            ('popup_needs_live_binding', 'Export to excel 선택'),
            ('unsupported_recorded_action', 'Active sheet 선택'),
        ]:
            raw = manual(reason)
            rows.append(dict(step=raw, form=dict(text=text, mode=None, alias='', argument='')))
        review_drafts.ReviewStorage(name, exe.name, title, store).save_draft(rows, 'instruction_target_mismatch', 1)
        original = (store / 'legacy.json').read_bytes()
        tool = skill / 'desktop_select.py'
        initial = cli(tool, 'replay', '--task', name, '--exe', exe.name, '--window', title)
        overview = initial['live_recovery']
        assert initial['input_dispatched'] is False and len(overview['steps']) == 6, initial
        scope = overview['live_scopes'][overview['steps'][0]['scope_index']]
        by_id = {t['automation_id']: t for t in scope['targets'] if t.get('automation_id')}
        assert by_id['conditionState']['semantic_label'] == 'MES Setup' and by_id['conditionFamily']['semantic_label'] == 'Family', scope
        assert by_id['QueryCommand']['name'] == 'Search'
        current = cli(tool, 'recover', '--task', name, '--only', 4, '--image')['live_scopes'][0]
        grid = next(t for t in current['targets'] if t.get('automation_id') == 'ResultSurface')
        assert grid['role'] == 'group' and Path(current['image']).is_file()
        box, origin = grid['screen_bounds'], current['screen_origin']
        point = [box[a] - origin[i] + box[size]//2 for i, (a, size) in enumerate((('x', 'width'), ('y', 'height')))]
        decisions = [
            dict(number=1, mode='fixed', target='conditionState', role='combobox', argument='N'),
            dict(number=2, mode='required', target='conditionFamily', role='combobox', argument='Family'),
            dict(number=3, mode='recorded', target='QueryCommand', role='button', wait_after=5),
            dict(number=4, through=6, mode='context-menu', target='ResultSurface', role='group',
                 frame=current['frame'], point=point, path=['Export to excel', 'Active sheet'],
                 output=dict(kind='file', path=str(book))),
        ]
        document = work / 'bindings.json'
        document.write_text(json.dumps(dict(source_fingerprint=overview['source_fingerprint'], steps=decisions)),
                            encoding='utf8')
        repaired = cli(tool, 'recover', '--task', name, '--bindings', document, '--save-as', 'report')
        assert repaired['status'] == 'recorded' and len(repaired['task']['steps']) == 4, repaired
        reg = cli(skill / 'python_workflow.py', 'register', '--task', 'report', '--out', work / 'report-skill')
        assert reg['status'] == 'registered' and (store / 'legacy.json').read_bytes() == original, reg
        assert fixture.command('state') == baseline
        receipt = cli(work / 'report-skill/scripts/run.py', '--set', 'Family=Hybrid')
        deadline = time.monotonic() + 3
        state = fixture.command('state')
        while not book.exists() and time.monotonic() < deadline:
            time.sleep(.1)
            state = fixture.command('state')
        assert [state[k] for k in ('queries','right_clicks','parents','leaves','wrong_point','unrelated')] == [1,1,1,1,0,0], (
            state, receipt.get('diagnostic'), receipt['steps'][-1], grid['screen_bounds'], point)
        assert receipt['steps'][-1]['receipt'].get('workbook_verified') is True, receipt['steps'][-1]
        cells = decoded_cells(book)
        assert cells['A2'] == 'N' and cells['B2'] == 'Hybrid' and cells['C2'] == '1', cells
        return dict(live_scopes=len(overview['live_scopes']), original_steps=6, derived_steps=4,
                    source_preserved=True, preflight_input=False, queries=1, right_clicks=1, leaf_calls=1,
                    cells=[cells[k] for k in ('A2','B2','C2')], status=receipt['status'])
    finally:
        fixture.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('zip', type=Path)
    with tempfile.TemporaryDirectory(prefix='ds-recover-') as folder:
        print(json.dumps(exercise(parser.parse_args().zip.resolve(), Path(folder))))
