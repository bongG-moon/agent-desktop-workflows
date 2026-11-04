"""Record and replay a real mouse/keyboard flow on our synthetic fixture only.

Development acceptance harness. This compiles only the bundled test fixture,
never product helpers. It never targets a user's business application.
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


class Actor:
    """Simulate a user, with fresh PID/UIA/point guards before each test input."""
    def __init__(self, fixture):
        self.fixture = fixture
        self.request = {'pid': fixture.proc.pid, 'hwnd': fixture.ready['hwnd'], 'exe_name': 'NativeFixture.exe'}
        self.binding = bridge.Binding(self.request)

    def nodes(self):
        windows = [w for w in bridge.inventory()['data']
                   if w['pid'] == self.fixture.proc.pid and self.binding.contains_window(w['hwnd'])]
        result = []
        for window in windows:
            snapshot = bridge.inspect(dict(self.request, hwnd=window['hwnd']))
            assert snapshot['ok'] and snapshot['data']['complete']
            result.extend(snapshot['data']['nodes'])
        return result

    def choose(self, *, name=None, automation_id=None, role=None):
        matches = [n for n in self.nodes() if not n['offscreen'] and n['bounds_valid']
                   and (name is None or n['name'] == name)
                   and (automation_id is None or n['automation_id'] == automation_id)
                   and (role is None or n['role'] == role)]
        unique = {(n['name'], n['role'], tuple(n['bounds'].values())): n for n in matches}
        assert len(unique) == 1, ('fixture_target_not_unique', name, automation_id, len(unique))
        return next(iter(unique.values()))

    def ready(self):
        self.binding.check()
        assert self.binding.contains_window(bridge.foreground()), 'fixture_not_foreground'
        assert not any(bridge.user32.GetAsyncKeyState(k) & 0x8000
                       for k in (1, 2, 4, 5, 6, 16, 17, 18, 91, 92)), 'physical_input_busy'

    def click(self, node, *, ratio=.5, right=False):
        self.ready()
        rect = node['bounds']
        x, y = round(rect['x'] + rect['width'] * ratio), round(rect['y'] + rect['height'] * .5)
        hit = bridge.window_from_point(x, y)
        with bridge.UIA() as uia:
            in_fixture = uia.within(uia.from_point(x, y), uia.from_handle(self.binding.hwnd))
        assert bridge.hwnd_pid(hit) == self.fixture.proc.pid
        assert self.binding.contains_window(hit) or in_fixture, 'nonfixture_point'
        bridge.user32.SetCursorPos(x, y)
        time.sleep(.5)  # Gives the passive recorder a pre-click observation.
        self.ready()
        assert bridge.window_from_point(x, y) == hit, 'fixture_moved'
        sx, sy, sw, sh = [bridge.user32.GetSystemMetrics(i) for i in (76, 77, 78, 79)]
        down, up = (8, 16) if right else (2, 4)
        values = [bridge._mouse(round((x - sx) * 65535 / (sw - 1)), round((y - sy) * 65535 / (sh - 1)), 0x8000 | 0x4000 | 1),
                  bridge._mouse(flags=down), bridge._mouse(flags=up)]
        inputs = (bridge.INPUT * len(values))(*values)
        assert bridge.user32.SendInput(len(inputs), inputs, C.sizeof(bridge.INPUT)) == len(inputs)
        time.sleep(.7)

    def type_notes(self, text):
        self.click(self.choose(automation_id='NotesEditor', role='textfield'))
        self.ready()
        with bridge.UIA() as uia:
            node = uia.node(uia.focused(), uia.from_handle(self.binding.hwnd))
            assert node['automation_id'] == 'NotesEditor'
        values = []
        for key, flags in ((0x11, 0), (0x41, 0), (0x41, 2), (0x11, 2)):
            value = bridge.INPUT(); value.type = 1
            value.ki.wVk, value.ki.dwFlags = key, flags
            values.append(value)
        for char in text:
            for flags in (4, 6):
                value = bridge.INPUT(); value.type = 1
                value.ki.wScan, value.ki.dwFlags = ord(char), flags
                values.append(value)
        inputs = (bridge.INPUT * len(values))(*values)
        assert bridge.user32.SendInput(len(inputs), inputs, C.sizeof(bridge.INPUT)) == len(inputs)
        time.sleep(.85)
        assert self.fixture.command('state')['notes'] == text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', required=True, type=Path)
    args = parser.parse_args()
    if os.name != 'nt' or not args.work_dir.is_absolute() or args.work_dir.exists():
        parser.error('Windows and a new absolute local test directory are required')
    work = args.work_dir
    work.mkdir(parents=True)
    bridge.configure_dpi()
    compiler = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    exe = work / 'NativeFixture.exe'
    subprocess.run([str(compiler), '/nologo', '/target:exe', '/out:' + str(exe),
        '/r:System.Windows.Forms.dll', '/r:System.Drawing.dll', '/r:System.Web.Extensions.dll',
        str(ROOT / 'tests/fixtures/NativeFixture.cs')], capture_output=True, check=True)
    results = []
    def report(case, passed):
        results.append({'case': case, 'passed': bool(passed)})
        print(json.dumps(results[-1]), flush=True)
    fixture, recorder = Fixture(exe), None
    recording = work / 'recording'; recording.mkdir()
    try:
        actor = Actor(fixture)
        request = dict(actor.request, output_dir=str(recording), progress_path=str(recording / 'progress.json'),
                       draft_path=str(recording / 'draft.json'), stop_path=str(recording / 'stop'),
                       name='Synthetic combo submit row export', auto_start=True)
        save(recording / 'request.json', request)
        recorder = subprocess.Popen([sys.executable, '-I', '-X', 'utf8', '-B', str(ROOT / 'scripts/py_helper.py'),
            'record', '--request', str(recording / 'request.json'), '--response', str(recording / 'response.json')],
            creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (recording / 'progress.json').exists() and read_json(recording / 'progress.json')['state'] == 'recording':
                break
            time.sleep(.1)
        else:
            raise RuntimeError('recorder_not_ready')
        fixture.command('activate'); time.sleep(.2)
        actor.click(actor.choose(automation_id='SetupSelector', role='combobox'), ratio=.9)
        actor.click(actor.choose(name='N', role='listitem'))
        actor.type_notes('Lot-42')
        actor.click(actor.choose(automation_id='SubmitButton'))
        actor.type_notes('Row-filter')
        actor.click(actor.choose(name='Row B', role='listitem'))
        actor.click(actor.choose(automation_id='QueryButton'))
        actor.click(actor.choose(automation_id='ResultRegion'), right=True)
        actor.click(actor.choose(name='Export', role='menuitem'))
        actor.click(actor.choose(name='Export to Excel', role='menuitem'))
        demonstrated = fixture.command('state')
        save(work / 'demonstrated.json', demonstrated)
        report('demonstrated_conditions_and_submit', demonstrated['query_combo'] == 'N'
               and demonstrated['query_row'] == 'Row B' and demonstrated['submitted'] == 'Lot-42'
               and demonstrated['export_count'] == 1)
        save(recording / 'stop', {})
        recorder.wait(timeout=8); recorder = None
        draft = read_json(recording / 'draft.json')
        steps = draft['steps']
        report('combo_selection_recorded_as_value', any(s['action'] == 'set-value' and s.get('value') == 'N'
               and s['target']['locator'].get('automation_id') == 'SetupSelector' for s in steps))
        report('submitted_value_preserved_before_clear', any(s['action'] == 'set-value' and s.get('value') == 'Lot-42' for s in steps))
        report('independent_row_selection_preserved', any(s['target']['locator'].get('name') == 'Row B' for s in steps))
        report('no_unexpected_manual_gap_in_supported_flow', not any(s['action'] == 'manual' for s in steps))
    finally:
        if recorder and recorder.poll() is None:
            recorder.kill(); recorder.wait(timeout=3)
        fixture.close()
    # Deliberate fixture-only review: preserve actions/locators/values and originals.
    original = (recording / 'draft.json').read_bytes()
    store = Store(work / 'store'); saved = store.import_recording(recording / 'draft.json')
    reviewed = copy.deepcopy(saved)
    output = work / 'replayed-export.txt'
    for step in reviewed['steps']:
        if step['target']['kind'] == 'region':
            step['target']['reviewed'] = True
        if step['expect']['kind'] in {'manual', 'none'}:
            step['expect'] = {'kind': 'none'}
    reviewed['steps'][-1]['expect'] = {'kind': 'file', 'path': str(output)}
    reviewed = store.save(reviewed, expected_revision=saved['revision'])
    fixture = Fixture(exe)
    try:
        fixture.command('output-path|' + str(output)); fixture.command('activate')
        binding = {'pid': fixture.proc.pid, 'hwnd': fixture.ready['hwnd'], 'exe_name': exe.name}
        run = Runner(store, desktop.DesktopAdapter).start(reviewed['id'], binding)
        state = fixture.command('state')
        save(work / 'replayed.json', {'run': run, 'state': state})
        report('replayed_combo_and_row_conditions', state['query_combo'] == 'N' and state['query_row'] == 'Row B')
        report('replayed_submit_uses_recorded_value', state['submitted'] == 'Lot-42')
        report('replayed_context_export_once', state['context_count'] == 1 and state['export_count'] == 1)
        report('new_export_file_verified', output.is_file() and output.read_text() == 'synthetic export N Row B'
               and run['steps'][-1]['status'] == 'verified')
        report('recording_original_and_step_ids_preserved', (recording / 'draft.json').read_bytes() == original
               and [s['id'] for s in saved['steps']] == [s['id'] for s in reviewed['steps']])
    finally:
        fixture.close()
    summary = {'synthetic_only': True, 'actual_fx_tested': False,
               'passed': sum(r['passed'] for r in results), 'total': len(results), 'results': results}
    save(work / 'summary.json', summary)
    print(json.dumps({k: v for k, v in summary.items() if k != 'results'}), flush=True)
    return 0 if all(r['passed'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
