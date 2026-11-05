"""Exercise blank-parent combo evidence against an independent Windows UIA provider.

Development-only: compiles the bundled fixture and targets only its PID/HWND.
The fixture's stdin model state is independent of UIA readback. This covers the
recorder value path and real workflow replay, not physical recording acceptance
or the unavailable company FX installation. No model/network or global hooks.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import desktop
import recorder
import win_bridge as bridge
from smoke_native import Fixture, save
from storage import Store
from workflows import Runner


def compile_fixture(work):
    framework = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319'
    references = [framework / 'WPF' / name for name in (
        'PresentationFramework.dll', 'PresentationCore.dll', 'WindowsBase.dll',
        'UIAutomationTypes.dll', 'UIAutomationProvider.dll')]
    references.extend(framework / name for name in ('System.Xaml.dll', 'System.Web.Extensions.dll'))
    exe = work / 'ComboEvidenceFixture.exe'
    result = subprocess.run([str(framework / 'csc.exe'), '/nologo', '/target:exe', '/out:' + str(exe),
        *['/r:' + str(path) for path in references], str(ROOT / 'tests/fixtures/ComboEvidenceFixture.cs')],
        capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('fixture_compile_failed: ' + result.stdout + result.stderr)
    return exe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', required=True, type=Path)
    parser.add_argument('--compile-only', action='store_true', help='Build fixture without opening a window')
    args = parser.parse_args()
    if os.name != 'nt' or not args.work_dir.is_absolute() or args.work_dir.exists():
        parser.error('Windows and a new absolute local test directory are required')
    work = args.work_dir
    work.mkdir(parents=True)
    exe = compile_fixture(work)
    if args.compile_only:
        print(json.dumps({'compiled': True, 'fixture_only': True}))
        return
    bridge.configure_dpi()
    checks = []
    def report(case, passed):
        checks.append({'case': case, 'passed': bool(passed)})
        print(json.dumps(checks[-1]), flush=True)
    fixture = Fixture(exe)
    try:
        request = {'pid': fixture.proc.pid, 'hwnd': fixture.ready['hwnd'], 'exe_name': exe.name}
        binding = bridge.Binding(request)
        observed = desktop.inspect(request, work / 'observations')
        save(work / 'initial.json', observed)
        nodes = {n['automation_id']: n for n in observed['nodes'] if n['role'] == 'combobox'}
        expected_ids = {'SelectionEvidenceCombo', 'EditorEvidenceCombo', 'OpaqueCombo',
                        'IgnoredWriteCombo', 'ConflictingCombo'}
        report('all_custom_combos_observed', set(nodes) == expected_ids and observed['complete'])
        with bridge.UIA() as uia:
            native_nodes, elements, complete = bridge.read_tree(binding, uia)
            pairs = {n['automation_id']: e for n, e in zip(native_nodes, elements) if n['role'] == 'combobox'}
            report('parent_valuepattern_is_actually_blank', complete and set(pairs) == expected_ids
                   and all(e.property(30043) is True and e.property(30045) == '' for e in pairs.values()))
        report('selected_item_evidence_resolves_value', nodes['SelectionEvidenceCombo']['value'] == 'Y'
               and nodes['SelectionEvidenceCombo'].get('value_evidence', {}).get('source') == 'selection')
        report('editor_evidence_resolves_value', nodes['EditorEvidenceCombo']['value'] == 'Y'
               and nodes['EditorEvidenceCombo'].get('value_evidence', {}).get('source') == 'editor')
        report('unselected_option_names_are_not_values', nodes['OpaqueCombo']['value'] is None
               and nodes['OpaqueCombo'].get('value_evidence', {}).get('status') == 'unavailable')
        report('contradictory_selection_and_editor_are_unknown', nodes['ConflictingCombo']['value'] is None
               and nodes['ConflictingCombo'].get('value_evidence', {}).get('status') == 'conflict')

        # Invoke the real recorder's value-watch entrypoint with real UIA objects.
        # The fixture command simulates a completed user choice without hooks/input.
        for key in ('SelectionEvidenceCombo', 'EditorEvidenceCombo', 'OpaqueCombo'):
            fixture.command('reset')
            recording = work / ('record-' + key); recording.mkdir()
            payload = {'output_dir': str(recording), 'progress_path': str(recording / 'progress.json'),
                       'draft_path': str(recording / 'draft.json'), 'stop_path': str(recording / 'stop'),
                       'name': 'Synthetic combo evidence recording'}
            model = recorder.RecordingModel(payload, dict(exe_name=exe.name, title=observed['title'],
                                                          class_name=observed['class_name']))
            worker = recorder.CaptureWorker(binding, bridge, model, queue.Queue(), queue.Queue())
            with bridge.UIA() as uia:
                worker.uia = uia
                native_nodes, elements, complete = bridge.read_tree(binding, uia)
                index = bridge.unique(native_nodes, {'automation_id': key, 'role': 'combobox'})
                root = uia.from_handle(binding.hwnd)
                observation = {'element': elements[index], 'root': root, 'window': binding.hwnd,
                               'node': native_nodes[index]}
                worker.capture_action({'action': 'click', 'at': time.monotonic()}, observation)
                fixture.command('set|' + key + '|N')
                worker.finalize_watch(True)
            save(recording / 'steps.json', model.steps)
            if key == 'OpaqueCombo':
                passed = bool(model.steps) and all(s['action'] == 'manual' for s in model.steps)
                report('recorder_preserves_unobservable_combo_as_review', passed)
            else:
                report('recorder_captures_' + key, len(model.steps) == 1
                       and model.steps[0]['action'] == 'set-value' and model.steps[0].get('value') == 'N')

        store = Store(work / 'store')
        def run_combo(key, value):
            step = {'id': uuid.uuid4().hex, 'label': key, 'action': 'set-value', 'value': value,
                    'target': {'kind': 'element', 'locator': {'automation_id': key, 'role': 'combobox'}},
                    'expect': {'kind': 'value', 'value': value},
                    'recognition': {'target': 'identified', 'action': 'supported', 'result': 'checkable'}}
            workflow = {'schema_version': 1, 'id': uuid.uuid4().hex, 'name': 'Synthetic evidence replay',
                        'revision': 1, 'target': {'exe_name': exe.name, 'title': observed['title'],
                        'class_name': observed['class_name']}, 'parameters': {}, 'steps': [step]}
            saved = store.save(workflow)
            return Runner(store, desktop.DesktopAdapter).start(saved['id'], request)
        fixture.command('reset')
        legacy = run_combo('SelectionEvidenceCombo', '')
        state = fixture.command('state')['controls']['SelectionEvidenceCombo']
        report('legacy_empty_combo_stops_before_dispatch', legacy['status'] == 'needs_review'
               and legacy['steps'][0]['input_dispatched'] is False and state == {'value': 'Y', 'writes': 0})
        for key in ('SelectionEvidenceCombo', 'EditorEvidenceCombo'):
            fixture.command('reset')
            run = run_combo(key, 'N')
            state = fixture.command('state')['controls'][key]
            report('replay_verifies_' + key, run['status'] == 'completed' and run['task_verified']
                   and state == {'value': 'N', 'writes': 1})
        fixture.command('reset')
        ignored = run_combo('IgnoredWriteCombo', 'N')
        before = fixture.command('state')['controls']['IgnoredWriteCombo']
        resumed = Runner(store, desktop.DesktopAdapter).resume(ignored['id'], request)
        after = fixture.command('state')['controls']['IgnoredWriteCombo']
        report('ignored_setvalue_is_not_success', ignored['status'] == 'needs_review'
               and not ignored['task_verified'] and before == {'value': 'Y', 'writes': 1})
        report('unmatched_readback_does_not_repeat_input', resumed['status'] == 'needs_review' and after == before)
        fixture.command('reset')
        opaque = run_combo('OpaqueCombo', 'N')
        state = fixture.command('state')['controls']['OpaqueCombo']
        report('opaque_value_is_not_false_success', opaque['status'] == 'needs_review'
               and not opaque['task_verified'] and state['value'] == 'N')
    finally:
        fixture.close()
    summary = {'passed': sum(c['passed'] for c in checks), 'total': len(checks), 'checks': checks,
               'fixture': 'synthetic blank-parent UIA provider', 'company_fx_tested': False,
               'physical_recording_tested': False}
    save(work / 'summary.json', summary)
    print(json.dumps({'passed': summary['passed'], 'total': summary['total']}), flush=True)
    raise SystemExit(0 if all(c['passed'] for c in checks) else 1)


if __name__ == '__main__':
    main()
