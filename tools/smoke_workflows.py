"""Actual standalone runner acceptance against an owned synthetic Windows app.

No real business window is targeted. Independent fixture state proves effects;
the workflow engine, local storage and agent-desktop binary are the real product.
"""
from __future__ import annotations
import argparse
import copy
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', required=True, type=Path)
    parser.add_argument('--product-dir', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--recording', type=Path, help='Draft from tools/smoke_native.py; synthetic data only')
    args = parser.parse_args()
    if os.name != 'nt' or not args.work_dir.is_absolute():
        parser.error('Windows and an absolute local work directory are required')
    root = args.product_dir.resolve(strict=True)
    work = args.work_dir
    work.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(root / 'scripts'))
    import desktop
    from storage import Store
    from workflows import Runner
    compiler = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    exe = work / 'ActionFixture.exe'
    subprocess.run([str(compiler), '/nologo', '/target:exe', '/out:' + str(exe),
        '/r:System.Windows.Forms.dll', '/r:System.Drawing.dll', '/r:System.Web.Extensions.dll',
        str(root / 'tests/fixtures/ActionFixture.cs')], capture_output=True, check=True)
    proc = subprocess.Popen([str(exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
    lines = queue.Queue()
    def reader():
        for line in proc.stdout:
            lines.put(json.loads(line))
    threading.Thread(target=reader, daemon=True).start()
    def command(name, **data):
        proc.stdin.write(json.dumps(dict(command=name, **data)) + '\n')
        proc.stdin.flush()
        return lines.get(timeout=10) if name != 'quit' else None
    checks = []
    def report(name, passed):
        item = dict(case=name, passed=bool(passed))
        checks.append(item)
        print(json.dumps(item), flush=True)
    try:
        ready = lines.get(timeout=15)
        assert ready['pid'] == proc.pid
        binding = {'pid': proc.pid, 'hwnd': ready['hwnd'], 'exe_name': exe.name}
        observed = desktop.inspect(binding, work / 'observations')
        store = Store(work / 'store')
        def step(action, name, role, expect='none', **data):
            result = {'id': uuid.uuid4().hex, 'label': name, 'action': action,
                'target': {'kind': 'element', 'locator': {'name': name, 'role': role}},
                'expect': {'kind': expect} if isinstance(expect, str) else expect,
                'recognition': {'target': 'identified', 'action': 'supported', 'result': 'pending'}}
            result.update(data)
            return result
        def workflow(steps, parameters=None):
            return {'schema_version': 1, 'id': uuid.uuid4().hex, 'name': 'Synthetic workflow', 'revision': 1,
                'target': {'exe_name': exe.name, 'title': observed['title'], 'class_name': observed['class_name']},
                'parameters': parameters or {}, 'steps': steps}
        changes = [step('set-value', 'MES Setup', 'combobox', 'value', parameter='Setup'),
                   step('set-value', 'Notes', 'textfield', 'value', parameter='Notes'),
                   step('check', 'Enable option', 'checkbox', {'kind': 'checked', 'value': True}),
                   step('click', 'Run query', 'button')]
        saved = store.save(workflow(changes, {'Setup': 'Y', 'Notes': 'initial'}))
        run = Runner(store, desktop.DesktopAdapter).start(saved['id'], binding,
                {'Setup': 'N', 'Notes': 'parameter from workflow'})
        state = command('state')
        report('workflow_parameters_change_actual_controls', state['combo'] == 'N' and state['notes'] == 'parameter from workflow' and state['option'])
        report('workflow_query_once_and_business_status_honest', state['query_count'] == 1 and run['status'] == 'completed_unverified' and not run['task_verified'])
        receipts = [e.get('result', {}).get('receipt') for e in run['steps']]
        report('fresh_ref_session_per_step', len(set(receipts)) == len(changes) and None not in receipts)
        # Human-confirmed result resumes without invoking query twice.
        command('reset')
        saved = store.save(workflow([step('click', 'Run query', 'button', 'manual'),
                                     step('set-value', 'Notes', 'textfield', 'value', value='after confirmation')]))
        runner = Runner(store, desktop.DesktopAdapter)
        paused = runner.start(saved['id'], binding)
        again = runner.resume(paused['id'], binding)
        report('unverified_input_not_repeated', paused['status'] == 'needs_review' and again['status'] == 'needs_review' and command('state')['query_count'] == 1)
        resumed = runner.resume(paused['id'], binding, confirm_step_id=saved['steps'][0]['id'])
        state = command('state')
        report('confirmed_resume_skips_completed_input', resumed['status'] == 'completed' and state['query_count'] == 1 and state['notes'] == 'after confirmation')
        # Repair a known unsent target and retain the already executed prefix.
        command('reset')
        saved = store.save(workflow([step('click', 'Run query', 'button'),
                                     step('set-value', 'Missing synthetic field', 'textfield', 'value', value='repair success')]))
        paused = runner.start(saved['id'], binding)
        report('missing_target_stops_before_input', paused['status'] == 'needs_review' and paused['steps'][1]['input_dispatched'] is False)
        revised = copy.deepcopy(saved)
        revised['steps'][1]['target']['locator']['name'] = 'Notes'
        revised = store.save(revised, expected_revision=saved['revision'])
        repaired = runner.fork_repaired(paused['id'], binding)
        state = command('state')
        report('repaired_revision_continues_without_prefix_replay', repaired['status'] == 'completed_unverified' and state['query_count'] == 1 and state['notes'] == 'repair success')
        report('original_revision_and_run_preserved', store.load(saved['id'], revision=1)['steps'][1]['target']['locator']['name'] == 'Missing synthetic field' and store.load_run(paused['id'])['status'] == 'needs_review')
        # A nonempty old file alone cannot prove export; changed fixture output can.
        result_file = work / 'synthetic-result.txt'
        result_file.write_text('old synthetic file', encoding='utf-8')
        command('output-path', path=str(result_file))
        saved = store.save(workflow([step('click', 'Save synthetic result', 'button', {'kind': 'file', 'path': str(result_file)})]))
        exported = runner.start(saved['id'], binding)
        report('file_postcondition_detects_new_fixture_output', exported['status'] == 'completed' and result_file.read_text().startswith('synthetic result ') and command('state')['save_count'] == 1)
        # CLI may expose state only through explicitly requested local files.
        inventory_file = work / ('workflow-list-' + uuid.uuid4().hex + '.json')
        cli = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(root / 'scripts/cli.py'), 'list',
            '--store', str(store.root), '--output', str(inventory_file)], capture_output=True, text=True, check=True)
        summary = json.loads(cli.stdout)
        report('cli_summary_omits_workflow_business_names', summary['status'] == 'written' and 'Synthetic workflow' not in cli.stdout and not cli.stderr and inventory_file.is_file())
    finally:
        if proc.poll() is None:
            try:
                command('quit'); proc.wait(timeout=3)
            except Exception:
                proc.kill(); proc.wait(timeout=3)
    if args.recording:
        from smoke_native import Fixture
        native_exe = work / 'NativeFixture.exe'
        subprocess.run([str(compiler), '/nologo', '/target:exe', '/out:' + str(native_exe),
            '/r:System.Windows.Forms.dll', '/r:System.Drawing.dll', '/r:System.Web.Extensions.dll',
            str(root / 'tests/fixtures/NativeFixture.cs')], capture_output=True, check=True)
        fixture = Fixture(native_exe)
        try:
            binding = {'pid': fixture.proc.pid, 'hwnd': fixture.ready['hwnd'], 'exe_name': native_exe.name}
            saved = store.import_recording(args.recording)
            edited = copy.deepcopy(saved)
            for item in edited['steps']:
                if item['target']['kind'] == 'region':
                    item['target']['reviewed'] = True
                    item['expect'] = {'kind': 'none'}
            edited = store.save(edited, expected_revision=saved['revision'])
            paused = Runner(store, desktop.DesktopAdapter).start(saved['id'], binding)
            state = fixture.command('state')
            report('actual_recording_import_edit_replay', state['notes'] == 'recorded final' and state['context_count'] == 1)
            report('recorded_unsupported_step_remains_visible', paused['status'] == 'needs_review' and paused['steps'][-1].get('reason') == 'manual_step')
            report('recording_review_preserves_prior_revision', not any(s['target'].get('reviewed') for s in store.load(saved['id'], revision=1)['steps']))
        finally:
            fixture.close()
    summary = {'synthetic_only': True, 'actual_fx_tested': False, 'passed': sum(c['passed'] for c in checks), 'total': len(checks), 'results': checks}
    (work / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in summary.items() if k != 'results'}), flush=True)
    return 0 if all(c['passed'] for c in checks) else 1


if __name__ == '__main__':
    raise SystemExit(main())
