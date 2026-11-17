"""Run pairs of generated workflow processes against isolated WPF test windows."""
import argparse
from contextlib import ExitStack
import ctypes
from ctypes import wintypes
from hashlib import sha256
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
from zipfile import ZipFile

from smoke_desktop_context import compile_fixture, decoded_cells
from smoke_native import Fixture
from smoke_registered_export import MENU


def install(archive, project):
    project.mkdir()
    with ZipFile(archive) as package:
        package.extractall(project)
    return project / 'desktop-select/scripts/python_workflow.py'


def register(runtime, app, name, steps):
    work = runtime.parents[2]
    source = work / (name + '.json')
    source.write_text(json.dumps(dict(schema=1, name=name, exe='DesktopContextFixture.exe',
        window=app.ready['title'], steps=steps)), encoding='utf-8')
    dest = work / name
    result = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(runtime), 'register', '--recipe', str(source),
                             '--out', str(dest)], capture_output=True, timeout=60)
    receipt = json.loads(result.stdout)
    assert result.returncode == 0 and receipt['status'] == 'registered', receipt
    return dest / 'scripts/run.py'


class Job:
    def __init__(self, runner, *options):
        self.started = time.perf_counter()
        self.process = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', str(runner), '--summary', *options],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       creationflags=subprocess.CREATE_NO_WINDOW)

    def receipt(self):
        stdout, stderr = self.process.communicate(timeout=90)
        end = time.perf_counter()
        if not stdout:
            raise AssertionError((self.process.returncode, stderr.decode('utf8', errors='replace')))
        return end, json.loads(stdout)

    def finish(self, *, workbook=False):
        end, result = self.receipt()
        assert self.process.returncode in (0, 3) and result.get('input_dispatched') is True, result
        assert result['readability_check']['count'] == 1, result
        if workbook:
            assert self.process.returncode == 0 and result.get('outcome') == 'workbook_verified', result
        return end, result

    def close(self):
        if self.process.poll() is None:
            # Stop only the disposable runner process tree started by this test, before closing its target.
            subprocess.run(['taskkill', '/pid', str(self.process.pid), '/T', '/F'], capture_output=True)
            self.process.wait(timeout=10)


def start(stack, runner, *options):
    job = Job(runner, *options)
    stack.callback(job.close)
    return job


def events(app):
    return app.command('state')['events']


def await_event(app, action, job):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        found = [event for event in events(app) if event['action'] == action]
        if found:
            return found[0]['at']
        if job.process.poll() is not None:
            raise AssertionError(('premature completion', action, job.finish(), events(app)))
        time.sleep(.03)
    raise AssertionError(('missing fixture event', action, events(app)))


def set_value(value):
    return dict(action='set-value', selector=dict(role='combobox', automation_id='conditionState'),
                field_label='MES Setup', value=value)


def query_steps(value):
    return [set_value(value), dict(action='wait', seconds=2),
            dict(action='invoke', selector=dict(role='button', automation_id='QueryCommand'))]


def menu_steps(folder):
    return [dict(action='context-menu', spec=dict(target=dict(role='group', automation_id='ResultSurface'),
                                                  point=[.5, .5], path=MENU)),
            dict(action='verify-file', output=dict(kind='directory', path=str(folder)))]


def trace_shape(trace):
    return [(event['action'], event['value']) for event in trace]


def await_quiet_setup():
    """Set up a physical test only when no recent external input is observed."""
    class LastInput(ctypes.Structure):
        _fields_ = [('size', wintypes.UINT), ('tick', wintypes.DWORD)]
    info = LastInput()
    info.size = ctypes.sizeof(info)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            raise OSError('cannot observe test input session')
        age = (ctypes.windll.kernel32.GetTickCount() - info.tick) & 0xffffffff
        if age >= 1500:
            return
        time.sleep(.1)
    raise AssertionError('test desktop is busy with external input')


def mutation_jobs(stack, a, b, runner_a, runner_b, *, same, explicit_handles=False):
    for app in (a,) if same else (a, b):
        app.command('trace')
    args_a = ['--background-only', '--hwnd', str(a.ready['hwnd'])] if explicit_handles else []
    args_b = ['--background-only', '--hwnd', str(b.ready['hwnd'])] if explicit_handles else []
    first = start(stack, runner_a, *args_a)
    await_event(a, 'write', first)
    second = start(stack, runner_b, *args_b)
    first.finish()
    second.finish()
    if same:
        trace = events(a)
        assert trace_shape(trace) == [('write', 'N'), ('query', 'N'), ('write', 'Y'), ('query', 'Y')], trace
        assert second.started < trace[1]['at']
        return trace
    left, right = events(a), events(b)
    assert trace_shape(left) == [('write', 'N'), ('query', 'N')], left
    assert trace_shape(right) == [('write', 'Y'), ('query', 'Y')], right
    overlap = min(left[1]['at'], right[1]['at']) - max(left[0]['at'], right[0]['at'])
    assert overlap > 0, (left, right)
    return dict(overlap_seconds=round(overlap, 4), left=left, right=right)


def menu_pair(stack, a, b, runner_a, runner_b, *, same=False, other_field=False, background=False):
    for app in (a,) if same else (a, b):
        app.command('trace')
    # Set up the first app in front, as a user would before beginning this pair.
    # The second process must handle its own activation after the first route.
    await_quiet_setup()
    a.command('activate')
    first = start(stack, runner_a, '--allow-physical')
    await_event(a, 'right', first)
    options = ['--background-only'] if background else ([] if other_field else ['--allow-physical'])
    second = start(stack, runner_b, *options)
    receipts = [first.finish(workbook=True)[1], second.finish(workbook=not other_field)[1]]
    left, right = events(a), events(b)
    menu = ['right', 'parent', 'leaf']
    assert [e['action'] for e in left] == (menu + menu if same else menu), left
    if not same:
        assert [e['action'] for e in right] == (['write'] if other_field else menu), right
    competing_input = left[3]['at'] if same else right[0]['at']
    assert left[0]['at'] <= second.started < left[2]['at'], (left, right, second.started)
    if other_field:
        assert second.started <= competing_input < left[2]['at'], (left, right)
    else:
        assert left[2]['at'] <= competing_input, (left, right)
    workbooks = []
    for result in receipts[:1] if other_field else receipts:
        file = Path(result['verified_outputs'][0]['path'])
        assert decoded_cells(file)['A2'] == ('Y' if file.parent.name == 'exports-right' else 'N'), file
        workbooks.append(str(file))
    assert len(set(workbooks)) == len(workbooks)
    return dict(left=left, right=None if same else right, competitor_started=second.started,
                workbooks_verified=workbooks)


def benchmark(stack, app, builds, repeats):
    samples = {name: [] for name, runner in builds}
    for n in range(repeats):
        for name, runner in (builds if n % 2 == 0 else reversed(builds)):
            app.command('trace')
            job = start(stack, runner)
            ended, result = job.finish()
            trace = events(app)
            assert trace_shape(trace) == [('write', 'N')] and result['status'] == 'verified', (trace, result)
            first, total = trace[0]['at'] - job.started, ended - job.started
            assert 0 < first <= total
            samples[name].append(dict(first_seconds=round(first, 4), total_seconds=round(total, 4),
                                      preparation_seconds=result['preparation_seconds']))
    return dict(samples=samples, medians={name: {key: statistics.median(s[key] for s in group) for key in group[0]}
                                         for name, group in samples.items()})


def shared_output(stack, current, a, b, folder, exporter):
    # A changes a field and expects an export, but never creates one. B actually
    # exports into that directory. Only B may claim its verified workbook.
    a.command('trace')
    b.command('trace')
    steps = [set_value('N'), dict(action='verify-file', output=dict(kind='directory', path=str(folder)), timeout=6)]
    missing = register(current, a, 'missing-export', steps)
    await_quiet_setup()
    b.command('activate')
    first = start(stack, missing)
    await_event(a, 'write', first)
    second = start(stack, exporter, '--allow-physical')
    _, denied = first.receipt()
    _, exported = second.finish(workbook=True)
    assert first.process.returncode == 2 and denied['status'] == 'blocked', denied
    assert denied['diagnostic'] == 'export_workbook_not_observed' and not denied.get('verified_outputs'), denied
    path = Path(exported['verified_outputs'][0]['path'])
    assert path.parent == folder and decoded_cells(path)['A2'] == 'Y'
    return dict(first=denied['diagnostic'], second=exported['outcome'], verified_file=str(path))


def exercise(archive, baseline, work, repeats):
    exe = compile_fixture(work)
    current = install(archive, work / 'project-a')
    other = install(archive, work / 'project-b')
    old = install(baseline, work / 'previous')
    report = dict(package_sha256=sha256(archive.read_bytes()).hexdigest(),
                  baseline_sha256=sha256(baseline.read_bytes()).hexdigest())
    def record(key, value):
        report[key] = value
        (work / 'results.json').write_text(json.dumps(report, indent=2), encoding='utf8')
    with ExitStack() as stack:
        apps = []
        folders = []
        for side in ('left', 'right'):
            folder = work / ('exports-' + side)
            folder.mkdir()
            app = Fixture(exe, str(folder / 'report.xlsx'), '--group')
            stack.callback(app.close)
            app.ready = app.command('title|Desktop Select parallel fixture ' + side)
            app.command('position-' + side)
            app.command('numbered-output')
            folders.append(folder)
            apps.append(app)
        a, b = apps
        one = register(current, a, 'one-field', [set_value('N')])
        old_one = register(old, a, 'one-field', [set_value('N')])
        timing = benchmark(stack, a, [('baseline', old_one), ('candidate', one)], repeats)
        record('timing', timing)
        print('uncontended median seconds', timing['medians'], flush=True)
        query_a = register(current, a, 'query-a', query_steps('N'))
        query_same = register(other, a, 'query-on-same-window', query_steps('Y'))
        query_b = register(other, b, 'query-b', query_steps('Y'))
        record('same_window', mutation_jobs(stack, a, a, query_a, query_same, same=True))
        independent = mutation_jobs(stack, a, b, query_a, query_b, same=False)
        record('different_background_windows', independent)
        b.command('title|' + a.ready['title'])
        try:
            record('identical_titles_explicit_windows',
                   mutation_jobs(stack, a, b, query_a, query_same, same=False, explicit_handles=True))
        finally:
            b.command('title|' + b.ready['title'])
        print('window exclusivity and independent overlap', independent['overlap_seconds'], flush=True)
        for app in apps:
            # Wait for the genuine button result from the preceding registered workflow.
            deadline = time.monotonic() + 5
            while not app.command('state')['ready']:
                if time.monotonic() > deadline:
                    raise AssertionError('query result did not appear')
                time.sleep(.05)
        menu_a = register(current, a, 'export-a', menu_steps(folders[0]))
        menu_b = register(other, b, 'export-b', menu_steps(folders[1]))
        field_b = register(other, b, 'one-field', [set_value('N')])
        a.command('delayed-menu')
        record('two_menus', menu_pair(stack, a, b, menu_a, menu_b))
        record('menu_and_automatically_background_field', menu_pair(stack, a, b, menu_a, field_b, other_field=True))
        record('menu_and_background_field', menu_pair(stack, a, b, menu_a, field_b, other_field=True, background=True))
        record('same_registered_menu_twice', menu_pair(stack, a, a, menu_a, menu_a, same=True))
        record('same_output_directory', shared_output(stack, current, a, b, folders[1], menu_b))
        for app in apps:
            state = app.command('state')
            assert state['wrong_point'] == state['unrelated'] == 0
        record('passed', True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zip', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--work-dir', type=Path, required=True, help='A new disposable directory')
    parser.add_argument('--repeats', type=int, choices=range(1, 16), default=7)
    args = parser.parse_args()
    work = args.work_dir.resolve()
    work.mkdir(parents=True)
    exercise(args.zip.resolve(strict=True), args.baseline.resolve(strict=True), work, args.repeats)
    print('passed', work / 'results.json')
