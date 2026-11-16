"""Measure public commands for a registered Excel export against an isolated WPF fixture on Windows."""
import argparse
from contextlib import nullcontext
from hashlib import sha256
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
from zipfile import ZipFile

from build_desktop_select import FILES
from smoke_desktop_context import compile_fixture, decoded_cells
from smoke_native import Fixture


MENU = ['Export to excel', 'Active sheet']


def recipe(exe, title, output):
    return dict(schema=1, name='Registered export timing', exe=exe, window=title, steps=[
        dict(action='set-value', selector=dict(role='combobox', automation_id='conditionState'), value='N'),
        dict(action='set-value', selector=dict(role='combobox', automation_id='conditionFamily'),
             value='Hybrid', allow_option_select=True),
        dict(action='invoke', selector=dict(role='button', automation_id='QueryCommand')),
        dict(action='wait', seconds=5),
        dict(action='context-menu', spec=dict(target=dict(role='group', automation_id='ResultSurface'),
                                              point=[.5, .5], path=MENU)),
        dict(action='wait', seconds=3),
        dict(action='verify-file', output=output),
    ])


def public_call(script, *args, cwd, env):
    started = time.perf_counter()
    call = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(script), *map(str, args)],
                          cwd=cwd, env=env, capture_output=True, encoding='utf8', timeout=180,
                          creationflags=subprocess.CREATE_NO_WINDOW)
    elapsed = time.perf_counter() - started
    if not call.stdout.strip():
        raise RuntimeError((script.name, call.returncode, call.stderr))
    return json.loads(call.stdout), call.returncode, elapsed


def exercise(archive_path, work, legacy, repeats, *, start_minimized=False):
    skill = work / 'desktop-select'
    with ZipFile(archive_path) as archive:
        # Install only the published runtime paths, all below the disposable project.
        for name in FILES:
            target = skill / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read('desktop-select/' + name))
    manifest = json.loads((skill / 'runtime-manifest.json').read_text(encoding='utf8'))
    if (manifest['version'] == '0.4.17') != legacy:
        raise ValueError('Use --legacy only for the 0.4.17 baseline ZIP')
    # Both historical benchmark packages predate the embedded readability check.
    expect_readability_check = manifest['version'] not in ('0.4.17', '0.4.18')
    sys.path.insert(0, str(skill / 'scripts'))
    import desktop_select as app
    from row_action import foreground

    app.bridge.set_dpi_awareness()
    helper = skill / 'scripts/python_workflow.py'
    registered = work / 'device-report'
    runner = registered / 'scripts/run.py'
    fixture_dir = work / 'vendor' / 'suite' / 'client' / 'bin'
    fixture_dir.mkdir(parents=True)
    output_dir = fixture_dir.parents[1] / 'ExcelData'
    output_dir.mkdir()
    profile = work / 'user-profile'
    (profile / 'Documents').mkdir(parents=True)
    (profile / 'Downloads').mkdir()
    env = dict(os.environ, USERPROFILE=str(profile), DESKTOP_SELECT_TASK_DIR=str(work / 'tasks'),
               PYTHONNOUSERSITE='1', PYTHONPATH='')
    exe = compile_fixture(fixture_dir)
    metrics = dict(version=manifest['version'], zip_sha256=sha256(archive_path.read_bytes()).hexdigest(),
                   company_fx_tested=False, llm_time_included=False, requested_wait_seconds=8,
                   start_minimized=start_minimized,
                   preparation_profile=('representative windows and registered read-only calls' if legacy else
                                        'registered summary command only'), runs=[])
    for iteration in range(1, repeats + 1):
        book = output_dir / f'report-{iteration}.xlsx'
        assert not book.exists()
        fixture = Fixture(exe, str(book), '--group', ready_timeout=25)
        try:
            title, hwnd = fixture.ready['title'], fixture.ready['hwnd']
            # Common fixture setup, outside all timed host calls, scoped to this fixture's HWND and PID.
            binding = app.bridge.Binding(app.pick_window(app.windows(exe.name), title, hwnd))
            assert binding.pid == fixture.proc.pid
            foreground(binding)
            before = fixture.command('state')
            if iteration == 1:
                output = dict(kind='directory', path=str(output_dir))
                if not legacy:
                    discovered, code, seconds = public_call(helper, 'discover-output', '--exe', exe.name,
                        '--window', title, '--hwnd', hwnd, cwd=work, env=env)
                    assert code == 0 and discovered.get('status') == 'observed', discovered
                    assert discovered.get('input_dispatched') is False and discovered['directories'] == [str(output_dir)], discovered
                    assert discovered.get('suggested_output') == output, discovered
                    output = discovered['suggested_output']
                    metrics['one_time_discovery_seconds'] = round(seconds, 4)
                spec = recipe(exe.name, title, output)
                source = work / 'source-recipe.json'
                source.write_text(json.dumps(spec), encoding='utf8')
                receipt, code, seconds = public_call(helper, 'register', '--recipe', source, '--out', registered,
                                                     '--hwnd', hwnd, cwd=work, env=env)
                assert code == 0 and receipt.get('status') == 'registered' and not receipt['input_dispatched'], receipt
                stored = json.loads((registered / 'workflow.json').read_text(encoding='utf8'))
                assert [s['action'] for s in stored['steps']] == [s['action'] for s in spec['steps']]
                assert stored['steps'][-1]['output'] == output
                metrics['one_time_registration_seconds'] = round(seconds, 4)

            host_times = {}
            if start_minimized:
                app.bridge.user32.ShowWindow(hwnd, 6)  # Minimize the test window only.
                assert app.bridge.user32.IsIconic(hwnd)
                preview, code, _ = public_call(runner, '--read-only', cwd=registered, env=env)
                assert code == 2 and preview.get('diagnostic') == 'target_window_minimized', preview
                assert preview['input_dispatched'] is False and app.bridge.user32.IsIconic(hwnd)
            if legacy:
                windows, code, seconds = public_call(skill / 'scripts/desktop_select.py', 'windows', '--exe', exe.name,
                                                     cwd=work, env=env)
                assert code == 0 and windows.get('status') == 'observed', windows
                assert any(w['hwnd'] == hwnd and w['pid'] == fixture.proc.pid for w in windows['windows'])
                host_times['windows'] = seconds
                preview, code, seconds = public_call(runner, '--read-only', cwd=registered, env=env)
                assert code == 0 and preview.get('status') == 'observed' and not preview['input_dispatched'], preview
                host_times['registered_read_only'] = seconds
            current = fixture.command('state')
            assert all(current[k] == value for k, value in before.items()
                       if not (start_minimized and k.endswith('_bounds'))), (before, current)
            receipt, code, execution = public_call(runner, *([] if legacy else ['--summary']), cwd=registered, env=env)
            actual = fixture.command('state')
            counters = {k: actual[k] for k in ('writes', 'queries', 'generation', 'right_clicks', 'menus', 'parents',
                                               'leaves', 'wrong_point', 'unrelated')}
            assert receipt.get('status') == 'completed_unverified' and len(receipt['steps']) == 7, (code, receipt, counters)
            assert [s['number'] for s in receipt['steps']] == list(range(1, 8)), receipt
            steps = [s['receipt'] if legacy else s for s in receipt['steps']]
            assert [s['status'] for s in steps] == ['verified', 'verified', 'delivered_unverified', 'waited',
                                                    'delivered_unverified', 'waited', 'verified'], receipt
            assert steps[4]['menu_items'] == MENU and steps[-1]['diagnostic'] == 'export_workbook_verified', receipt
            readability_check = receipt.get('readability_check')
            if expect_readability_check:
                assert isinstance(readability_check, dict), receipt
                assert readability_check.get('scope') == 'window_root' and readability_check.get('status') == 'observed', readability_check
                assert type(readability_check.get('count')) is int and readability_check['count'] == 1, readability_check
                assert readability_check.get('input_dispatched') is False, readability_check
            if legacy:
                assert code == 3 and steps[-1]['file_verified'] is True, (code, receipt)
            else:
                assert code == 0 and receipt.get('outcome') == 'workbook_verified', (code, receipt)
                if start_minimized:
                    assert receipt.get('window_restored') is True and not app.bridge.user32.IsIconic(hwnd), receipt
                assert receipt['verified_outputs'] == [dict(number=7, path=str(book))], receipt
            assert counters == dict(writes=1, queries=1, generation=1, right_clicks=1, menus=1,
                                    parents=1, leaves=1, wrong_point=0, unrelated=0), counters
            assert (actual['status'], actual['family'], actual['result_status'], actual['result_family']) == (
                'N', 'Hybrid', 'N', 'Hybrid'), actual
            cells = decoded_cells(book)
            assert cells == dict(A1='MES Setup', B1='Family', C1='Generation', A2='N', B2='Hybrid', C2='1'), cells
            prep = sum(host_times.values())
            host_times['registered_run'] = execution
            metrics['runs'].append(dict(iteration=iteration, host_calls=len(host_times),
                host_call_seconds={name: round(value, 4) for name, value in host_times.items()},
                external_preparation_seconds=round(prep, 4), execution_command_seconds=round(execution, 4),
                total_command_seconds=round(prep + execution, 4), worker_preparation_seconds=receipt.get('preparation_seconds'),
                worker_elapsed_seconds=receipt.get('elapsed_seconds'), step_seconds=[s['elapsed_seconds'] for s in receipt['steps']],
                aggregate_status=receipt['status'], exit_code=code, outcome=receipt.get('outcome'), counters=counters,
                readability_check=readability_check, workbook_verified=True, checked_cells=cells))
        finally:
            fixture.close()
    metrics['median_total_command_seconds'] = statistics.median(r['total_command_seconds'] for r in metrics['runs'])
    (work / 'benchmark.json').write_text(json.dumps(metrics, indent=2), encoding='utf8')
    print(json.dumps(metrics, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zip', type=Path, required=True)
    parser.add_argument('--legacy', action='store_true', help='measure the representative 0.4.17 host preparation')
    parser.add_argument('--start-minimized', action='store_true', help='also check read-only diagnosis and automatic restoration')
    parser.add_argument('--work-dir', type=Path, help='retain the benchmark and fixture in a new absolute directory')
    parser.add_argument('--repeats', type=int, default=1)
    args = parser.parse_args()
    if args.start_minimized and args.legacy:
        parser.error('--start-minimized tests the new workflow runner')
    if os.name != 'nt' or not args.zip.is_file() or not 1 <= args.repeats <= 10:
        parser.error('Requires Windows, a local package ZIP and 1..10 repeats')
    if args.work_dir:
        if not args.work_dir.is_absolute() or args.work_dir.exists():
            parser.error('--work-dir must be a new absolute directory')
        args.work_dir.mkdir(parents=True)
    with (nullcontext(args.work_dir) if args.work_dir else tempfile.TemporaryDirectory()) as folder:
        exercise(args.zip.resolve(), Path(folder), args.legacy, args.repeats, start_minimized=args.start_minimized)


if __name__ == '__main__':
    main()
