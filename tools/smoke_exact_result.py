"""Run exact-key lookup, scoped detail read and verified close on an isolated Windows app."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from zipfile import ZipFile

from smoke_lookup_trial import build_lookup_fixture
from smoke_native import Fixture
from smoke_registered_result import wait_for_idle


def recipe(title, exe, painted):
    def selector(role, ident, parent=None):
        result = dict(role=role, automation_id=ident)
        if parent:
            result['ancestors'] = [dict(role='group', automation_id=parent)]
        return result

    table = selector('group', 'PaintedResults' if painted else 'ProviderResults')
    verify = dict(action='verify-value', selector=table, parameter='Equipment', result_column='Equip ID')
    if painted:
        verify['result_adapter'] = dict(kind='windows-ocr-grid', row_count=selector('textfield', 'ResultCount'))
    detail = dict(exe=exe, title='Item details', related=True, owned=True, after_step=4)
    return dict(schema=1, name='Equipment notes', exe=exe, window=title, steps=[
        dict(action='set-value', selector=selector('textfield', 'queryField', 'Filters'), parameter='Equipment'),
        dict(action='invoke', selector=selector('button', 'searchButton', 'Filters'), command_name='VIEW'),
        verify, dict(action='double-click-result'),
        dict(action='read-value', selector=selector('textfield', 'CommentText', 'EquipmentInfo'),
             label='Comment', window_context=detail),
        dict(action='invoke', selector=selector('button', 'btnClose', 'DetailActions'),
             command_name='Exit', verify_closed=True, window_context=detail)])


def exercise(package, work, painted, *, opaque_probe=False):
    work.mkdir(parents=True, exist_ok=False)
    skills = work / '.claude/skills'
    with ZipFile(package) as z:
        z.extractall(skills)
    env = dict(os.environ, DESKTOP_SELECT_TASK_DIR=str(work / 'tasks'), LOCALAPPDATA=str(work / 'local'))
    if not painted:
        driver = work / 'audit'
        driver.mkdir()
        shutil.copyfile(Path(__file__).resolve().parents[1] / 'tests/fixtures/registered_result_ui_driver.py',
                        driver / 'sitecustomize.py')
        env.update(PYTHONPATH=str(driver), DESKTOP_SELECT_SMOKE_ELEMENT_TRACE=str(work / 'element-trace.txt'))

    def run(args, name):
        start = time.monotonic()
        process = subprocess.run([sys.executable, '-X', 'utf8', '-B', *map(str, args)], cwd=work, env=env,
                                 capture_output=True, encoding='utf8', timeout=110)
        if not process.stdout.strip():
            raise RuntimeError(process.stderr)
        result = json.loads(process.stdout)
        (work / (name + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
        return result, round(time.monotonic() - start, 3)

    exe = build_lookup_fixture(work)
    flags = ['--group-only', '--ruled-painted-grid'] if painted else ['--provider-grid']
    fixture = Fixture(exe, *flags, '--scoped-detail')
    try:
        fixture.command('focus')
        workflow = recipe(fixture.ready['title'], exe.name, painted)
        spec = work / 'recipe.json'
        spec.write_text(json.dumps(workflow), encoding='utf8')
        out = skills / 'equipment-notes'
        registered, _ = run([skills / 'desktop-select/scripts/python_workflow.py', 'register',
                             '--recipe', spec, '--out', out], 'register')
        assert registered['status'] == 'registered', registered
        runner = out / 'scripts/run.py'
        measurements = []
        cases = [('first', 'D124'), ('resized', 'X023'), ('missing', 'Y110'), ('empty', 'Y112'),
                 ('substring', 'D124'), ('duplicate', 'D124')]
        if painted:
            cases.append(('scrolled', 'D124'))
        for case, value in cases:
            if case == 'resized':
                fixture.command('resize')
            elif case != 'first':
                fixture.command(case)
            fixture.command('reset')
            wait_for_idle()
            before = fixture.command('state')
            # This disposable project has its own LOCALAPPDATA. Prove the same
            # worker runs after an approved first test, without granting access
            # to any installation or task outside this synthetic project.
            consent = {'first': ['--allow-physical', '--remember-physical'], 'resized': []}.get(
                case, ['--allow-physical'])
            if measurements and measurements[-1]['status'] != 'verified':
                # Each case is a new test with deliberately different application state.
                consent.append('--new-attempt')
            report, wall = run([runner, '--set', 'Equipment=' + value, '--summary', *consent], case)
            after = fixture.command('state')
            assert after['queries'] == before['queries'] + 1 and after['archiveClicks'] == 0, (after, report)
            assert after['wrongRows'] == after['wrongColumns'] == 0, (after, report)
            steps = {row['number']: row.get('receipt', row) for row in report['steps']}
            assert steps[1]['status'] == 'verified' and steps[2]['status'] == 'delivered_unverified', report
            if case in ('first', 'resized'):
                assert report['status'] == 'verified' and report['outcome'] == 'current_result_detail_read_and_closed', report
                assert steps[3]['status'] == 'verified' and steps[3]['value'] == value, report
                assert steps[4]['input_dispatched'] and steps[5]['status'] == 'observed', report
                assert steps[6]['diagnostic'] == 'recorded_window_closed' and steps[6]['status'] == 'verified', report
                assert any(item['number'] == 5 and item['value'] == 'Note for ' + value
                           for item in report['observations']), report
                assert after['opened'] == before['opened'] + 1 and after['closed'] == before['closed'] + 1, (after, report)
                assert after['last'] == value and not after['detailOpen'], (after, report)
            else:
                assert report['stopped_at'] == 3 and steps[3]['status'] == 'blocked', report
                assert not steps[3]['input_dispatched'] and after['opened'] == before['opened'], (after, report)
                expected_diagnostics = {
                    'empty': ('result_value_missing', 'result_value_not_found'),
                    'missing': ('result_value_mismatch', 'result_value_not_found'),
                    'substring': ('result_value_mismatch', 'result_value_not_found'),
                    'duplicate': ('result_value_ambiguous', 'result_cell_ambiguous'),
                    'scrolled': ('result_ocr_incomplete_rows',),
                }
                assert steps[3]['diagnostic'] in expected_diagnostics[case], report
            measurements.append(dict(
                case=case, requested=value, status=report['status'], wall_seconds=wall,
                step_seconds=[row['elapsed_seconds'] for row in report['steps']],
                observation=steps[3].get('result_observation'), diagnostic=steps[3].get('diagnostic'),
                opened=after['opened'] - before['opened'], closed=after['closed'] - before['closed'],
                wrong_rows=after['wrongRows']))
            (work / 'measurements.json').write_text(json.dumps(measurements, indent=2), encoding='utf8')
            print(json.dumps(measurements[-1]), flush=True)
        if not painted:
            audit = (work / 'element-trace.txt').read_text(encoding='utf8').splitlines()
            assert 'worker_guard_enabled' in audit and not any('attempted' in line for line in audit), audit
    finally:
        fixture.close()
    if opaque_probe:
        fixture = Fixture(exe, '--group-only', '--scroll-pane', '--scoped-detail')
        try:
            fixture.command('focus')
            spec = recipe(fixture.ready['title'], exe.name, painted=False)
            spec['steps'][2]['selector']['automation_id'] = 'PaintedResults'
            path = work / 'opaque-exact.json'
            path.write_text(json.dumps(spec), encoding='utf8')
            dest = skills / 'opaque-exact'
            registered, _ = run([skills/'desktop-select/scripts/python_workflow.py', 'register',
                                 '--recipe',path,'--out',dest], 'register-opaque')
            assert registered['status'] == 'registered', registered
            before = fixture.command('state')
            probe, _ = run([dest/'scripts/run.py','--only','3','--set','Equipment=OLD','--summary'], 'opaque-probe')
            after = fixture.command('state')
            last = probe['steps'][-1]['receipt']
            observation = last['result_observation']
            assert probe['status'] == 'blocked' and probe['input_dispatched'] is False, probe
            assert last['diagnostic'] == 'result_column_unavailable' and observation['attempts'] == 1, probe
            assert (before['queries'],before['opened']) == (after['queries'],after['opened']), after
            assert not any('attempted' in line for line in (work/'element-trace.txt').read_text().splitlines())
            print(json.dumps(dict(case='opaque-pane', blocked=last['diagnostic'], observation=observation)), flush=True)
        finally:
            fixture.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package', type=Path)
    parser.add_argument('work', type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--painted', action='store_true', help='use a painted grid with no accessible cells')
    mode.add_argument('--opaque-probe', action='store_true', help='also reject unreadable results on a pane with a scrollbar')
    args = parser.parse_args()
    exercise(args.package.resolve(strict=True), args.work.resolve(), args.painted, opaque_probe=args.opaque_probe)
