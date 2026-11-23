"""Actual Windows UIA execution with a second test window initially in front.

Only the development fixture is prepared directly. All tested mutations use
the product CLI. This is not an independent-desktop or company-FX test.
"""
import argparse
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from smoke_040_rebuild import compile_fixture, install_skill, cells, write_json, ROOT
from smoke_native import Fixture


def exercise(source, work, *, candidate_cases=True):
    installed = work / 'skills/desktop-select'
    install_skill(source, installed)
    exe = compile_fixture(work)
    target, front = Fixture(exe), None
    user = C.WinDLL('user32', use_last_error=True)
    user.GetForegroundWindow.restype = W.HWND
    user.GetCursorPos.argtypes = [C.POINTER(W.POINT)]
    user.IsIconic.argtypes = [W.HWND]
    checks, results = [], []

    def check(name, ok):
        checks.append(dict(case=name, passed=bool(ok)))
        print(json.dumps(checks[-1]), flush=True)
        assert ok, name

    def state():
        point = W.POINT()
        assert user.GetCursorPos(C.byref(point))
        return (int(user.GetForegroundWindow() or 0), point.x, point.y)

    def call(name, script, *arguments, physical=False):
        # Setup is outside the measured product invocation. UIA/provider calls
        # can activate an app even without an explicit foreground request.
        foreground(app.bridge.Binding(dict(pid=front.ready['pid'], hwnd=front.ready['hwnd'], exe_name=exe.name)))
        target.command('reset-input-events')
        target.command('reset-metrics')
        before, samples, stopped = state(), [], threading.Event()
        def monitor():
            while not stopped.wait(.01):
                samples.append(state())
        thread = threading.Thread(target=monitor, daemon=True)
        thread.start()
        started = time.perf_counter()
        try:
            proc = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(script), *arguments],
                                  cwd=work, capture_output=True, encoding='utf8', timeout=60,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        finally:
            stopped.set()
            thread.join(timeout=2)
        after = state()
        if not proc.stdout.strip():
            raise AssertionError((name, proc.returncode, proc.stderr))
        receipt = json.loads(proc.stdout)
        result = dict(case=name, receipt=receipt, exit_code=proc.returncode,
                      seconds=round(time.perf_counter()-started, 4), before=before, after=after,
                      observed_input_states=sorted(set(samples + [before, after])),
                      foreground_changed=any(s[0] != before[0] for s in samples + [after]),
                      state=target.command('state'), provider_reads=target.command('metrics'), stderr=proc.stderr)
        result['engine_seconds'] = receipt.get('elapsed_seconds')
        if isinstance(result['engine_seconds'], (int, float)):
            result['process_overhead_seconds'] = round(result['seconds'] - result['engine_seconds'], 4)
        results.append(result)
        check(name + '_cursor_unchanged',
              before[0] == front.ready['hwnd'] and all(s[1:] == before[1:] for s in samples + [after]))
        if not physical:
            check(name + '_foreground_unchanged', not result['foreground_changed'])
        return result

    try:
        target.command('text-add')
        book = work / 'background.xlsx'
        target.command('export-path|' + str(book))
        front = Fixture(exe)
        front.command('title|Foreground test window')
        front.command('point-front')
        # Window activation may be deferred or denied at process startup.
        # Establish and verify the test precondition before measuring actions.
        sys.path.insert(0, str(installed / 'scripts'))
        import desktop_select as app
        from row_action import foreground
        app.bridge.set_dpi_awareness()
        time.sleep(.5)
        foreground(app.bridge.Binding(dict(pid=front.ready['pid'], hwnd=front.ready['hwnd'], exe_name=exe.name)))
        time.sleep(.3)
        check('foreground_fixture_precondition', state()[0] == front.ready['hwnd'])
        bind = ['--exe', exe.name, '--window', target.ready['title'], '--hwnd', str(target.ready['hwnd'])]
        cli = installed / 'scripts/desktop_select.py'
        flow = installed / 'scripts/python_workflow.py'
        call('read_only_inspect', cli, 'inspect', *bind, '--target', 'MES Setup')
        r = call('direct_combo', cli, 'select', *bind, '--target', 'MES Setup', '--value', 'N', '--background-only')
        check('direct_combo_verified_once', r['receipt']['status'] == 'verified'
              and r['state']['controls']['conditionState']['writes'] == 1)
        target.command('reset')
        r = call('default_direct_combo', cli, 'select', *bind, '--target', 'MES Setup', '--value', 'P')
        check('default_direct_combo_verified_once', r['receipt']['status'] == 'verified'
              and r['state']['controls']['conditionState']['writes'] == 1
              and r['state']['controls']['conditionState']['value'] == 'P')
        target.command('text-reset')
        r = call('default_direct_fill', cli, 'fill', *bind, '--target', 'searchText',
                 '--value', 'CURRENT-73')
        check('default_direct_fill_verified_once', r['receipt']['status'] == 'verified'
              and r['state']['text_writes'] == 1 and r['state']['text_value'] == 'CURRENT-73')
        r = call('default_direct_invoke', cli, 'open', *bind, '--target', 'QueryCommand', '--role', 'button')
        check('default_direct_invoke_delivered_once', r['receipt']['input_dispatched'] is True
              and r['state']['opens'] == 1 and r['state']['result_entry'] == 'CURRENT-73')
        target.command('reset')
        recipe = dict(schema=1, name='Background fixture', exe=exe.name, window=target.ready['title'], steps=[
            dict(action='set-value', selector=dict(automation_id='conditionState', role='combobox'), value='N'),
            dict(action='set-value', selector=dict(automation_id='conditionFamily', role='combobox'), parameter='Family'),
            dict(action='invoke', selector=dict(automation_id='QueryCommand', role='button')),
            dict(action='invoke', selector=dict(automation_id='ExportCommand', role='button'))])
        recipe_path = work / 'recipe.json'
        write_json(recipe_path, recipe)
        registration = call('register', flow, 'register', '--recipe', str(recipe_path), '--hwnd', str(target.ready['hwnd']),
                            '--out', str(work / 'skills/background-job'))
        check('registration_read_only', registration['receipt']['status'] == 'registered'
              and not registration['receipt']['input_dispatched'])
        runner = work / 'skills/background-job/scripts/run.py'
        r = call('input_plan_without_parameter', runner, '--input-plan')
        check('input_plan_background_eligible', r['receipt']['input_requirements']['background_eligible'])
        r = call('registered_workflow', runner, '--hwnd', str(target.ready['hwnd']), '--set', 'Family=Hybrid', '--background-only')
        check('workflow_values_query_export_once', r['receipt']['status'] == 'completed_unverified'
              and r['state']['opens'] == r['state']['exports'] == 1
              and all(r['state']['controls'][key]['writes'] == 1 for key in ('conditionState', 'conditionFamily')))
        check('real_workbook_matches', cells(book)['A2'] == 'N' and cells(book)['B2'] == 'Hybrid')
        target.command('reset')
        r = call('default_registered_workflow', runner, '--hwnd', str(target.ready['hwnd']), '--set', 'Family=DRAM')
        check('default_workflow_values_query_export_once', r['receipt']['status'] == 'completed_unverified'
              and r['state']['opens'] == r['state']['exports'] == 1
              and all(r['state']['controls'][key]['writes'] == 1 for key in ('conditionState', 'conditionFamily')))
        check('default_workbook_matches_current_parameter',
              cells(book)['A2'] == 'N' and cells(book)['B2'] == 'DRAM')
        if candidate_cases:
            target.command('text-wrap')
            target.command('text-reset')
            anchored = dict(schema=1, name='Covered anchored field', exe=exe.name,
                            window=target.ready['title'], steps=[dict(action='set-value', value='ANCHOR-73',
                selector=dict(anchor=dict(automation_id='entryPanel', role='group'), point=[.5, .5],
                              role='textfield', class_name='RawCompatibilityProvider'))])
            anchor_path = work / 'anchored.json'
            write_json(anchor_path, anchored)
            r = call('default_covered_anchor', flow, 'run', '--recipe', str(anchor_path))
            check('covered_anchor_uses_provider_once', r['receipt']['status'] == 'verified'
                  and r['state']['text_writes'] == 1 and r['state']['text_value'] == 'ANCHOR-73')
            target.command('text-reset')
            r = call('strict_covered_anchor', flow, 'run', '--recipe', str(anchor_path), '--background-only')
            check('strict_covered_anchor_uses_provider_once', r['receipt']['status'] == 'verified'
                  and r['state']['text_writes'] == 1 and r['state']['text_value'] == 'ANCHOR-73')
        target.command('reset')
        recipe['steps'].append(dict(action='submit', selector=dict(automation_id='searchText', role='textfield')))
        write_json(recipe_path, recipe)
        r = call('mixed_refusal', flow, 'run', '--recipe', str(recipe_path), '--background-only')
        check('mixed_stops_before_first_field_or_missing_parameter',
              r['receipt']['diagnostic'] == 'background_requires_desktop_interaction'
              and r['receipt']['blocked_steps'] == [5] and not r['receipt']['input_dispatched']
              and all(r['state']['controls'][key]['writes'] == 0 for key in ('conditionState', 'conditionFamily'))
              and r['state']['opens'] == r['state']['exports'] == 0)
        if candidate_cases:
            target.command('track-focus')
            r = call('mixed_default', flow, 'run', '--recipe', str(recipe_path), '--set', 'Family=Hybrid', physical=True)
            events = r['state']['input_events']
            prefix = [e for e in events if e['action'].startswith(('SetValue:', 'Invoke:'))]
            physical = [e for e in events if e['action'].startswith('SetFocus:') or e['action'] == 'Enter']
            check('mixed_uia_prefix_never_activates', len(prefix) == 4
                  and all(e['foreground'] == front.ready['hwnd'] for e in prefix))
            check('mixed_activates_only_at_submit', r['foreground_changed'] and physical
                  and all(e['foreground'] == target.ready['hwnd'] for e in physical)
                  and r['receipt']['steps'][-1]['receipt']['diagnostic'] == 'submit_enter_sent')
            check('mixed_submit_delivered_once', sum(e['action'] == 'Enter' for e in physical) == 1)
            target.command('reset')
        target.command('minimize')
        r = call('minimized_refusal', cli, 'select', *bind, '--target', 'MES Setup', '--value', 'N', '--background-only')
        check('minimized_not_restored_no_input', r['receipt']['diagnostic'] == 'background_window_minimized'
              and not r['receipt']['input_dispatched'] and user.IsIconic(target.ready['hwnd'])
              and r['state']['controls']['conditionState']['writes'] == 0)
    finally:
        if front:
            front.close()
        target.close()
        write_json(work / 'results.json', results)
        write_json(work / 'summary.json', dict(checks=checks, passed=sum(c['passed'] for c in checks),
                   total=len(checks), company_fx_tested=False, independent_desktop=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skill-dir', type=Path, default=ROOT / 'skills/desktop-select')
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--baseline', action='store_true', help='Benchmark unchanged paths without new cases')
    args = parser.parse_args()
    if os.name != 'nt' or not args.work_dir.is_absolute() or args.work_dir.exists():
        parser.error('Requires Windows and a new absolute work directory')
    args.work_dir.mkdir(parents=True)
    exercise(args.skill_dir.resolve(), args.work_dir, candidate_cases=not args.baseline)
