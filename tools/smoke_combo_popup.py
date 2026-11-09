"""Record actual mouse input on transient synthetic custom-combo popups.

This development harness compiles only the bundled fixture into a new test
directory. It uses the production recorder process, HookPump and CaptureWorker;
it never calls capture_action or writes a selected value into the fixture during
recording. Ground-truth counters come from fixture stdin, independently of UIA.
Input is confined to fresh, visible elements in this fixture's PID and owned
windows. This is synthetic acceptance, not company FX acceptance.
"""
from __future__ import annotations

import argparse
import copy
import ctypes as C
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import desktop
import win_bridge as bridge
from smoke_native import Fixture, read_json, save
from storage import Store
from workflows import Runner


def compile_fixture(work):
    framework = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319'
    refs = [framework / 'WPF' / name for name in (
        'PresentationFramework.dll', 'PresentationCore.dll', 'WindowsBase.dll',
        'UIAutomationTypes.dll', 'UIAutomationProvider.dll')]
    refs += [framework / name for name in ('System.Xaml.dll', 'System.Web.Extensions.dll')]
    exe = work / 'ComboPopupFixture.exe'
    result = subprocess.run([str(framework / 'csc.exe'), '/nologo', '/utf8output', '/target:exe', '/out:' + str(exe),
        *['/r:' + str(path) for path in refs], str(ROOT / 'tests/fixtures/ComboPopupFixture.cs')],
        capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.returncode:
        raise RuntimeError('fixture_compile_failed: ' + result.stdout + result.stderr)
    return exe


class Actor:
    def __init__(self, fixture, exe):
        self.fixture = fixture
        self.request = {'pid': fixture.proc.pid, 'hwnd': fixture.ready['hwnd'], 'exe_name': exe.name}
        self.binding = bridge.Binding(self.request)
        self.request['process_started_utc_ticks'] = self.binding.created
        self.input_batches = 0
        self.activation_checks = []
        self.minimized_recorder_pids = []

    def activate(self):
        # The recorder announces recording before its own initial activation
        # finishes. Allow that short startup race to settle, without retrying
        # any mouse input or activating a window outside this fixture.
        for attempt in range(1, 4):
            self.binding.check()
            self.fixture.command('activate')
            time.sleep(.12)
            result = bridge.activate(self.request)
            time.sleep(.15)
            foreground = bridge.foreground()
            stable = bridge.root_window(foreground) == self.binding.hwnd
            diagnostic = {'attempt': attempt, 'foreground_pid': bridge.hwnd_pid(foreground),
                          'foreground_verified': result.get('foreground_verified') is True,
                          'stable_fixture_foreground': stable}
            self.activation_checks.append(diagnostic)
            print(json.dumps({'fixture_activation': diagnostic}), flush=True)
            if diagnostic['foreground_verified'] and stable:
                self.ready()
                return
        raise AssertionError('fixture_activation_unverified')

    def prepare_recording_foreground(self, recorder_process):
        """Minimize only this test's own recorder UI when it owns foreground."""
        foreground = bridge.foreground()
        root = bridge.root_window(foreground)
        foreground_pid = bridge.hwnd_pid(foreground)
        print(json.dumps({'recording_focus': {'foreground_pid': foreground_pid,
                         'fixture_pid': self.fixture.proc.pid, 'recorder_pid': recorder_process.pid}}), flush=True)
        if foreground_pid == recorder_process.pid:
            assert recorder_process.poll() is None, 'recorder_exited_before_activation'
            assert bridge.hwnd_pid(root) == recorder_process.pid, 'recorder_window_identity_changed'
            # The HookPump/CaptureWorker continue recording while their own Tk
            # test window is minimized. Never minimize a different application.
            bridge.user32.ShowWindow(root, 6)
            time.sleep(.15)
            assert bridge.user32.IsIconic(root), 'recorder_ui_minimize_unverified'
            self.minimized_recorder_pids.append(recorder_process.pid)
        else:
            assert (foreground_pid == self.fixture.proc.pid
                    and self.binding.contains_window(root)), 'unexpected_foreground_process'

    def nodes(self):
        result = []
        for window in bridge.inventory()['data']:
            if window['pid'] == self.fixture.proc.pid and self.binding.contains_window(window['hwnd']):
                observed = bridge.inspect(dict(self.request, hwnd=window['hwnd']))
                assert observed['ok'] and observed['data']['complete'], 'fixture_inspect_incomplete'
                result += [dict(node, observation_hwnd=window['hwnd']) for node in observed['data']['nodes']]
        return result

    def choose(self, key, role=None):
        matches = [node for node in self.nodes() if node['automation_id'] == key
                   and (role is None or node['role'] == role)
                   and not node['offscreen'] and node['bounds_valid']]
        # Owned WPF popups may also appear in the owner's UIA tree. Prefer the
        # snapshot rooted at the actual foreground surface; do not collapse
        # different controls merely because their AutomationIds are equal.
        foreground = bridge.root_window(bridge.foreground())
        on_surface = [node for node in matches if node['observation_hwnd'] == foreground]
        if on_surface:
            matches = on_surface
        assert len(matches) == 1, ('fixture_target_not_unique', key, len(matches))
        return matches[0]

    def ready(self):
        self.binding.check()
        assert self.binding.contains_window(bridge.foreground()), 'fixture_not_foreground'
        assert not any(bridge.user32.GetAsyncKeyState(key) & 0x8000
                       for key in (1, 2, 4, 5, 6, 16, 17, 18, 91, 92)), 'physical_input_busy'

    def _send(self, values):
        inputs = (bridge.INPUT * len(values))(*values)
        assert bridge.user32.SendInput(len(inputs), inputs, C.sizeof(bridge.INPUT)) == len(inputs)
        self.input_batches += 1

    def click(self, node, *, hover=.30, press=.045, settle=.35):
        self.ready()
        rect = node['bounds']
        x, y = round(rect['x'] + rect['width'] / 2), round(rect['y'] + rect['height'] / 2)
        hit = bridge.window_from_point(x, y)
        assert bridge.hwnd_pid(hit) == self.fixture.proc.pid and self.binding.contains_window(hit)
        with bridge.UIA() as uia:
            actual = uia.from_point(x, y)
            assert actual is not None and uia.pid(actual) == self.fixture.proc.pid
            scoped_root = uia.from_handle(bridge.root_window(hit))
            assert scoped_root is not None and uia.within(actual, scoped_root), 'fixture_point_scope_changed'
            # A WPF button may hit-test its TextBlock/content peer. Accept only
            # the exact expected control on that point's ancestor chain, within
            # this same fixture window and with unchanged geometry and role.
            current, matched = actual, False
            for _ in range(16):
                if current is None or uia.pid(current) != self.fixture.proc.pid:
                    break
                if (current.property(30011) == node['automation_id']
                        and bridge.ROLES.get(current.property(30003), 'unknown') == node['role']):
                    bounds = uia.bounds(current)
                    matched = (all(abs(bounds[k] - rect[k]) <= 2 for k in ('x', 'y', 'width', 'height'))
                               and bounds['x'] <= x < bounds['x'] + bounds['width']
                               and bounds['y'] <= y < bounds['y'] + bounds['height'])
                    break
                if uia.same(current, scoped_root):
                    break
                current = uia.parent(current)
            assert matched, 'fixture_target_moved'
        bridge.user32.SetCursorPos(x, y)
        if hover:
            time.sleep(hover)
        self.ready()
        assert bridge.window_from_point(x, y) == hit, 'fixture_window_moved'
        # Separate down and up exercises the real low-level hook, never a direct
        # recorder method. The fixture closes its popup on the resulting click.
        self._send([bridge._mouse(flags=2)])
        time.sleep(press)
        self._send([bridge._mouse(flags=4)])
        if settle:
            time.sleep(settle)

    def select(self, key, *, fast=False):
        self.click(self.choose(key + 'Open', 'button'), settle=.30)
        option = self.choose(key + 'OptionN', 'listitem')
        self.click(option, hover=0 if fast else .30, press=.015 if fast else .045)

    def escape(self):
        self.ready()
        values = []
        for flags in (0, 2):
            key = bridge.INPUT(); key.type = 1
            key.ki.wVk, key.ki.dwFlags = 0x1B, flags
            values.append(key)
        self._send(values)
        time.sleep(.4)


def record_case(work, name, fixture, actor, action):
    directory = work / ('record-' + name)
    directory.mkdir()
    fixture.command('reset')
    request = dict(actor.request, output_dir=str(directory), progress_path=str(directory / 'progress.json'),
                   draft_path=str(directory / 'draft.json'), stop_path=str(directory / 'stop'),
                   name='Synthetic popup ' + name, auto_start=True)
    save(directory / 'request.json', request)
    process = subprocess.Popen([sys.executable, '-I', '-X', 'utf8', '-B', str(ROOT / 'scripts/py_helper.py'),
        'record', '--request', str(directory / 'request.json'), '--response', str(directory / 'response.json')],
        creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('recorder_exited_before_ready')
            if (directory / 'progress.json').exists() and read_json(directory / 'progress.json')['state'] == 'recording':
                break
            time.sleep(.1)
        else:
            raise RuntimeError('recorder_not_ready')
        time.sleep(.3)
        actor.prepare_recording_foreground(process)
        actor.activate()
        action()
        time.sleep(.5)
        state = fixture.command('state')
        save(directory / 'ground-truth.json', state)
        save(directory / 'stop', {})
        process.wait(timeout=10)
        return directory, read_json(directory / 'draft.json'), state
    finally:
        if process.poll() is None:
            save(directory / 'stop', {})
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=3)


def value_steps(draft, key):
    return [step for step in draft['steps'] if step['target'].get('locator', {}).get('automation_id') == key
            and step['action'] == 'set-value']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', required=True, type=Path)
    parser.add_argument('--compile-only', action='store_true')
    parser.add_argument('--case', choices=('readable', 'opaque', 'adjacent', 'reselect', 'fast', 'cancel', 'rejected'),
                        help='Run a single recording case for diagnosis; default runs all and replay')
    args = parser.parse_args()
    if os.name != 'nt' or not args.work_dir.is_absolute() or args.work_dir.exists():
        parser.error('Windows and a new absolute local test directory are required')
    work = args.work_dir
    work.mkdir(parents=True)
    exe = compile_fixture(work)
    if args.compile_only:
        print(json.dumps({'compiled': True, 'fixture_only': True}))
        return 0
    bridge.configure_dpi()
    results, recordings = [], {}
    suite_completed = False
    actor = None
    def report(case, passed):
        results.append({'case': case, 'passed': bool(passed)})
        print(json.dumps(results[-1]), flush=True)
    fixture = Fixture(exe)
    try:
        actor = Actor(fixture, exe)
        initial = actor.nodes()
        save(work / 'initial.json', {'nodes': initial})
        combos = {node['automation_id']: node for node in initial if node['role'] == 'combobox'}
        report('distinct_blank_name_custom_combos', set(combos) == {'ComboAlpha', 'ComboBeta', 'ComboGamma', 'ComboReject'}
               and all(node['name'] == '' for node in combos.values()))
        report('explicit_label_identifies_field', combos['ComboBeta'].get('semantic_label') == 'Release state')

        cases = {
            'readable': lambda: actor.select('ComboAlpha'),
            'opaque': lambda: actor.select('ComboBeta'),
            'adjacent': lambda: (actor.select('ComboBeta'), actor.select('ComboGamma')),
            'reselect': lambda: (actor.select('ComboBeta'), actor.select('ComboBeta')),
            'fast': lambda: actor.select('ComboBeta', fast=True),
            'cancel': lambda: (actor.click(actor.choose('ComboBetaOpen', 'button')), actor.escape()),
            'rejected': lambda: actor.select('ComboReject'),
        }
        for name, action in cases.items():
            if args.case and name != args.case:
                continue
            directory, draft, truth = record_case(work, name, fixture, actor, action)
            recordings[name] = (directory, draft)
            controls = truth['controls']
            if name == 'adjacent':
                report('same_option_names_preserve_two_field_identities', all(
                    controls[key]['value'] == 'N' and controls[key]['clicks'] == 1
                    and any(s.get('value') == 'N' for s in value_steps(draft, key))
                    for key in ('ComboBeta', 'ComboGamma')))
            elif name == 'cancel':
                report('escape_does_not_invent_selected_option', controls['ComboBeta']['cancels'] == 1
                       and controls['ComboBeta']['clicks'] == 0 and controls['ComboBeta']['value'] == 'Y'
                       and not any(s.get('selection', {}).get('source') == 'option_click' for s in draft['steps'])
                       and not any(s.get('value') == 'N' for s in value_steps(draft, 'ComboBeta')))
            else:
                key = 'ComboAlpha' if name == 'readable' else 'ComboReject' if name == 'rejected' else 'ComboBeta'
                steps = value_steps(draft, key)
                selected = [s for s in steps if s.get('value') == 'N']
                wanted = 'Y' if name == 'rejected' else 'N'
                clicks = 2 if name == 'reselect' else 1
                report(name + '_actual_click_and_recorded_intent', controls[key]['value'] == wanted
                       and controls[key]['clicks'] == clicks and controls[key]['opens'] == clicks
                       and controls[key]['popup'] is False and bool(selected))
                expected_result = 'matched' if name == 'readable' else 'mismatch' if name == 'rejected' else 'unavailable'
                report(name + '_effect_separate_from_intent', bool(selected)
                       and selected[-1].get('selection', {}).get('source') == 'option_click'
                       and selected[-1]['selection'].get('result') == expected_result)
            report(name + '_never_records_blank_combo_input', not any(
                s['action'] == 'set-value' and s['target'].get('locator', {}).get('role') == 'combobox'
                and not s.get('value') for s in draft['steps']))

        if not args.case:
            store = Store(work / 'store')
            for name, key in (('readable', 'ComboAlpha'), ('opaque', 'ComboBeta')):
                directory, draft = recordings[name]
                original = (directory / 'draft.json').read_bytes()
                imported = store.import_recording(directory / 'draft.json')
                selected = [s for s in value_steps(imported, key) if s.get('value') == 'N']
                if not selected:
                    report(name + '_recording_replays', False)
                    continue
                # Use the actual recorded action/identity/value unchanged. Other
                # gaps remain in the original and are not silently marked done.
                reviewed = copy.deepcopy(imported)
                reviewed['steps'] = [selected[-1]]
                saved = store.save(reviewed, expected_revision=imported['revision'])
                fixture.command('reset')
                actor.activate()
                run = Runner(store, desktop.DesktopAdapter).start(saved['id'], actor.request)
                state = fixture.command('state')['controls'][key]
                save(work / ('replay-' + name + '.json'), {'run': run, 'ground_truth': state})
                report(name + '_recording_replays', state['value'] == 'N' and state['writes'] == 1)
                report(name + '_runtime_effect_not_inferred_from_recording',
                       (run['status'] == 'completed' and run['task_verified']) if name == 'readable'
                       else (run['status'] == 'needs_review' and not run['task_verified']))
                if name == 'opaque':
                    resumed = Runner(store, desktop.DesktopAdapter).resume(run['id'], actor.request)
                    after = fixture.command('state')['controls'][key]
                    report('unverified_replay_never_repeats_input', resumed['status'] == 'needs_review' and after == state)
                report(name + '_original_recording_preserved', (directory / 'draft.json').read_bytes() == original
                       and selected[-1]['id'] == saved['steps'][0]['id'])
        suite_completed = True
    finally:
        fixture.close()
        summary = {'synthetic_only': True, 'company_fx_tested': False,
                   'suite_completed': suite_completed, 'completed_recording_cases': list(recordings),
                   'physical_recording_tested': bool(recordings),
                   'injected_mouse_keyboard': bool(actor and actor.input_batches),
                   'activation_checks': actor.activation_checks if actor else [],
                   'recorder_ui_minimized_for_test': bool(actor and actor.minimized_recorder_pids),
                   'minimized_recorder_pids': actor.minimized_recorder_pids if actor else [],
                   'passed': sum(r['passed'] for r in results),
                   'total': len(results), 'results': results}
        save(work / 'summary.json', summary)
    print(json.dumps({key: value for key, value in summary.items() if key != 'results'}), flush=True)
    return 0 if all(r['passed'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
