"""Benchmark installed recorder ZIPs against disposable native Windows fixtures.

Startup/picker checks use unmodified product processes and close only their own
windows with WM_CLOSE. The separately labelled save check injects deterministic
capture data in a harness child and invokes real Tk review/save callbacks.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, redirect_stdout
import ctypes as C
from ctypes import wintypes as W
from hashlib import sha256
import json
import os
from pathlib import Path
import queue
import statistics
import subprocess
import sys
import threading
import time
from zipfile import ZipFile

from smoke_native import Fixture


TITLE = 'FxMenuAgent synthetic fixture  "QA"  A  B '
TASK = 'Synthetic recorder schema fixture'
FIXTURE = r'''
using System;
using System.Diagnostics;
using System.Drawing;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;
sealed class StartupFixture : Form {
    readonly JavaScriptSerializer json = new JavaScriptSerializer();
    StartupFixture(string title) {
        Text=title; StartPosition=FormStartPosition.Manual;
        Location=new Point(35,35); ClientSize=new Size(530,150);
        Controls.Add(new Label {Text="SYNTHETIC FIXTURE - no business services", Dock=DockStyle.Top, Height=40});
        Shown+=delegate {new Thread(Read) {IsBackground=true}.Start(); Write("ready");};
    }
    void Write(string kind) {
        Console.WriteLine(json.Serialize(new {kind=kind,pid=Process.GetCurrentProcess().Id,hwnd=Handle.ToInt64(),title=Text,writes=0}));
        Console.Out.Flush();
    }
    void Read() {
        string line; while((line=Console.ReadLine())!=null) {
            string command=line;
            try {Invoke(new Action(delegate {if(command=="quit") Close(); else Write(command);}));} catch {return;}
        }
    }
    [STAThread] static void Main(string[] args) {
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new StartupFixture(args[0]));
    }
}
'''


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def install(archive, destination):
    destination.mkdir()
    with ZipFile(archive) as package:
        package.extractall(destination)
    return destination / 'desktop-select'


def compile_fixture(work):
    source = work / 'RecorderStartupFixture.cs'
    source.write_text(FIXTURE, encoding='utf-8')
    exe = work / 'RecorderStartupFixture.exe'
    compiler = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    subprocess.run([str(compiler), '/nologo', '/target:exe', '/out:' + str(exe),
                    '/r:System.Windows.Forms.dll', '/r:System.Drawing.dll',
                    '/r:System.Web.Extensions.dll', str(source)], capture_output=True, check=True)
    return exe


class Native:
    def __init__(self):
        self.user = C.WinDLL('user32', use_last_error=True)
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        self.user.SetProcessDpiAwarenessContext.argtypes = [C.c_void_p]
        self.user.SetProcessDpiAwarenessContext(C.c_void_p(-4))
        self.callback = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        self.user.EnumWindows.argtypes = [self.callback, W.LPARAM]
        self.user.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
        self.user.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
        self.user.GetClassNameW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
        self.user.IsWindowVisible.argtypes = [W.HWND]
        self.user.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
        self.user.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
        self.kernel.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
        self.kernel.CreateToolhelp32Snapshot.restype = W.HANDLE
        self.kernel.CloseHandle.argtypes = [W.HANDLE]

    def windows(self, pids):
        rows = []
        def visit(hwnd, _):
            pid = W.DWORD()
            self.user.GetWindowThreadProcessId(hwnd, C.byref(pid))
            if pid.value in pids and self.user.IsWindowVisible(hwnd):
                title, kind, bounds = C.create_unicode_buffer(1024), C.create_unicode_buffer(256), W.RECT()
                self.user.GetWindowTextW(hwnd, title, len(title))
                self.user.GetClassNameW(hwnd, kind, len(kind))
                self.user.GetWindowRect(hwnd, C.byref(bounds))
                if title.value:
                    rows.append(dict(hwnd=int(hwnd), pid=pid.value, title=title.value,
                                     window_class=kind.value, bounds=[bounds.left, bounds.top, bounds.right, bounds.bottom]))
            return True
        self.user.EnumWindows(self.callback(visit), 0)
        return rows

    def descendants(self, parent):
        class Entry(C.Structure):
            _fields_ = [('size', W.DWORD), ('usage', W.DWORD), ('pid', W.DWORD), ('heap', C.c_size_t),
                        ('module', W.DWORD), ('threads', W.DWORD), ('parent', W.DWORD),
                        ('priority', W.LONG), ('flags', W.DWORD), ('exe', W.WCHAR * 260)]
        self.kernel.Process32FirstW.argtypes = [W.HANDLE, C.POINTER(Entry)]
        self.kernel.Process32NextW.argtypes = [W.HANDLE, C.POINTER(Entry)]
        snapshot = self.kernel.CreateToolhelp32Snapshot(2, 0)
        if snapshot == W.HANDLE(-1).value:
            raise C.WinError(C.get_last_error())
        pairs = []
        try:
            item = Entry(size=C.sizeof(Entry))
            ok = self.kernel.Process32FirstW(snapshot, C.byref(item))
            while ok:
                pairs.append((item.pid, item.parent))
                ok = self.kernel.Process32NextW(snapshot, C.byref(item))
        finally:
            self.kernel.CloseHandle(snapshot)
        owned = {parent}
        while True:
            more = {pid for pid, ppid in pairs if ppid in owned}
            if more <= owned:
                return owned
            owned |= more

    def close(self, row):
        current = self.windows({row['pid']})
        assert any(item['hwnd'] == row['hwnd'] and item['title'] == row['title'] for item in current)
        assert row['title'].startswith('Desktop Select 녹화')
        assert self.user.PostMessageW(row['hwnd'], 0x0010, 0, 0), 'own_window_close_failed'

    def screenshot(self, row, path):
        """Render only the test-owned window, without reading unrelated desktop pixels."""
        from PIL import Image
        gdi = C.WinDLL('gdi32', use_last_error=True)
        self.user.GetWindowDC.argtypes = [W.HWND]
        self.user.GetWindowDC.restype = W.HDC
        self.user.ReleaseDC.argtypes = [W.HWND, W.HDC]
        self.user.PrintWindow.argtypes = [W.HWND, W.HDC, W.UINT]
        for name, args in [('CreateCompatibleDC', [W.HDC]), ('CreateCompatibleBitmap', [W.HDC, C.c_int, C.c_int]),
                           ('SelectObject', [W.HDC, W.HANDLE])]:
            getattr(gdi, name).argtypes = args
            getattr(gdi, name).restype = W.HANDLE
        gdi.DeleteDC.argtypes = [W.HDC]
        gdi.DeleteObject.argtypes = [W.HANDLE]
        gdi.GetBitmapBits.argtypes = [W.HANDLE, W.LONG, C.c_void_p]
        row.update(next(item for item in self.windows({row['pid']}) if item['hwnd'] == row['hwnd']))
        x0, y0, x1, y1 = row['bounds']
        width, height = x1-x0, y1-y0
        source = self.user.GetWindowDC(row['hwnd'])
        memory = gdi.CreateCompatibleDC(source)
        bitmap = gdi.CreateCompatibleBitmap(source, width, height)
        previous = gdi.SelectObject(memory, bitmap)
        try:
            assert self.user.PrintWindow(row['hwnd'], memory, 2), 'own_window_render_failed'
            pixels = C.create_string_buffer(width * height * 4)
            assert gdi.GetBitmapBits(bitmap, len(pixels), pixels) == len(pixels)
            Image.frombuffer('RGB', (width, height), pixels, 'raw', 'BGRX', 0, 1).save(path)
        finally:
            gdi.SelectObject(memory, previous)
            gdi.DeleteObject(bitmap)
            gdi.DeleteDC(memory)
            self.user.ReleaseDC(row['hwnd'], source)


def command(skill, *options):
    return [sys.executable, '-X', 'utf8', '-B', str(skill / 'scripts/desktop_select.py'), *options]


def cli(skill, *options):
    run = subprocess.run(command(skill, *options), capture_output=True, encoding='utf-8',
                         timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    assert run.returncode in (0, 2, 3), (run.stdout, run.stderr)
    return json.loads(run.stdout)


def until(function, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = function()
        if value:
            return value
        time.sleep(.05)
    raise TimeoutError('expected_recorder_state_not_observed')


def startup(native, skill, work, name, app, *, baseline=False, hint='FXMENUAGNET', expected='recorder_ready', shot=False):
    if baseline:
        request = work / (name + '-request.json')
        write(request, dict(command='record', task=name, exe=app.name, window=TITLE))
        options = ['recorder', '--request', str(request)]
    else:
        options = ['recorder', '--app', hint, '--task', name]
    started = time.perf_counter()
    process = subprocess.Popen(command(skill, *options), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
    output = queue.Queue()
    def reader():
        for line in process.stdout:
            output.put((time.perf_counter(), line))
    threading.Thread(target=reader, daemon=True).start()
    own = None
    try:
        received, line = output.get(timeout=15)
        receipt = json.loads(line)
        assert receipt['status'] == expected and receipt['input_dispatched'] is False, receipt
        if expected == 'recorder_ready':
            assert receipt['window'] == TITLE, receipt
        pids = native.descendants(process.pid)
        matches = until(lambda: [r for r in native.windows(pids) if r['title'].startswith('Desktop Select 녹화')])
        assert len(matches) == 1, matches
        own = matches[0]
        if baseline:
            assert process.poll() is None, 'baseline_must_stay_attached'
            exited = None
        else:
            assert process.wait(timeout=3) == 0
            exited = time.perf_counter() - started
            assert receipt['status_args'] == ['recorder', '--session', receipt['session'], '--wait', '8', '--summary'], receipt
            assert own['pid'] != process.pid and native.windows({own['pid']}), 'detached_editor_not_visible'
        if shot:
            time.sleep(.15)
            native.screenshot(own, work / (name + '.png'))
        native.close(own)
        own = None
        if baseline:
            assert process.wait(timeout=5) in (0, 2, 3)
            _, line = output.get(timeout=3)
            final = json.loads(line)
        else:
            final = cli(skill, *receipt['status_args'])
        assert final['status'] == 'cancelled' and final['input_dispatched'] is False, final
        return dict(case=name, mode='unmodified_installed_zip_native_ui', ready_seconds=received-started,
                    parent_return_seconds=exited, editor_window=matches[0], receipt=receipt, final=final,
                    fixture_exe=app.name, fixture_title=TITLE, input_dispatched=False)
    finally:
        if own is not None:
            native.close(own)
        # On assertion/timeout, discover only descendants of our own launcher.
        # Never close a recorder belonging to another test or user session.
        for remaining in native.windows(native.descendants(process.pid)):
            if remaining['title'].startswith('Desktop Select 녹화'):
                native.close(remaining)
        if process.poll() is None:
            process.wait(timeout=8)
        if process.stderr is not None:
            diagnostic = process.stderr.read()
            if diagnostic:
                (work / (name + '-stderr.txt')).write_text(diagnostic, encoding='utf-8')


def editor_child(skill, work, app_exe, candidate):
    """Synthetic observations only; the product editor, storage and receipts are real."""
    from copy import deepcopy
    from unittest.mock import patch
    import tkinter as tk
    from tkinter import ttk
    sys.path.insert(0, str(skill / 'scripts'))
    import desktop_select as app
    import teach_capture as capture
    raw = [dict(id='setup', label='Setup', action='set-value', value='N',
                target=dict(kind='element', locator=dict(role='combobox', automation_id='SetupSelector')),
                recognition=dict(target='identified', action='supported', result='checkable'))]
    class Session:
        def __init__(self, binding, title):
            self.binding, self.started, self.ended, self.active_hint = binding, False, False, ''
            self.notes = capture.CaptureNotes()
        def start(self):
            self.started = True
            for item in raw:
                self.notes.add(deepcopy(item))
        def poll(self):
            pass
        def finish(self, **_):
            self.ended = True
            return dict(status='recorded', steps=self.notes.snapshot(), input_dispatched=False)
        def cancel(self):
            self.ended = True
            return dict(status='cancelled', steps=[], input_dispatched=False)
    original, failures = tk.Tk, []
    def create():
        root = original()
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        actions = iter(['시작', '마치고 검토', '저장'])
        def tick():
            try:
                close = next((w for w in descendants(root)
                              if isinstance(w, ttk.Button) and w.cget('text') == '처음부터 다시 녹화'), None)
                if close is not None:
                    next(w for w in descendants(root) if isinstance(w, ttk.Button)
                         and w.cget('text') == '닫기').invoke()
                    return
                label = next(actions)
                next(w for w in descendants(root) if isinstance(w, ttk.Button) and w.cget('text') == label).invoke()
                if label != '저장':
                    root.after(100, tick)
            except Exception as error:
                failures.append(repr(error))
                root.destroy()
        root.after(100, tick)
        root.after(10000, lambda: (failures.append('synthetic_save_timeout'), root.destroy()))
        return root
    with patch.object(tk, 'Tk', create), patch.object(capture, 'CaptureTrail', Session):
        if candidate:
            import recorder_launch as launch
            import secrets
            token = secrets.token_hex(16)
            folder = launch._folder(token)
            folder.mkdir(parents=True)
            launch._write(folder, 'request.json', dict(app='FXMENUAGNET', task=TASK, created=time.time()))
            (folder / 'running.lock').write_bytes(b'0')
            with (folder / 'events.jsonl').open('w', encoding='utf-8') as events, redirect_stdout(events):
                code = launch._child(token)
            receipt = launch.status(token)
        else:
            request = work / 'synthetic-save-request.json'
            write(request, dict(command='record', task=TASK, exe=app_exe, window=TITLE))
            code = app.worker(app.parser().parse_args(['recorder', '--request', str(request)]))
            receipt = None
    assert code == 0 and not failures, (code, failures)
    document = app.tasks.read_document(TASK, app.task_directory())
    review = app.run(app.parser().parse_args(['review', '--task', TASK]))
    assert review['diagnostic'] == 'task_already_saved' and document['schema'] == 1
    if candidate:
        assert receipt['status'] == 'recorded' and receipt['input_dispatched'] is False, receipt
    write(work / 'synthetic-save.json', dict(mode='synthetic_capture_in_harness_child_real_tk_storage',
                                           document=document, review=review, session_receipt=receipt))


def run(args):
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    baseline, candidate = install(args.baseline, work / 'baseline'), install(args.candidate, work / 'candidate')
    exe, native = compile_fixture(work), Native()
    results = dict(candidate_sha256=sha256(args.candidate.read_bytes()).hexdigest(),
                   baseline_sha256=sha256(args.baseline.read_bytes()).hexdigest(), runs=[])
    def record(value):
        results['runs'].append(value)
        write(work / 'summary.json', results)
        print(json.dumps(dict(case=value['case'], ready_seconds=round(value['ready_seconds'], 3),
                              parent_return_seconds=value['parent_return_seconds'])), flush=True)
    try:
        with ExitStack() as stack:
            fixture = Fixture(exe, TITLE)
            stack.callback(fixture.close)
            assert fixture.ready['title'] == TITLE
            timing_installs = ([('candidate', candidate)] if args.candidate_only else
                               [('baseline', baseline), ('candidate', candidate)])
            for index in range(1 if args.candidate_only else args.repeats):
                for name, skill in timing_installs:
                    record(startup(native, skill, work, f'{name}-{index+1}', exe,
                                   baseline=name == 'baseline', shot=index == 0))
            record(startup(native, candidate, work, 'exact-caption', exe, hint=TITLE))
            second = Fixture(exe, 'FxMenuAgent second synthetic fixture')
            try:
                record(startup(native, candidate, work, 'multiple-picker', exe,
                               expected='recorder_selection_required', shot=True))
            finally:
                second.close()
            record(startup(native, candidate, work, 'no-match-picker', exe, hint='NoSuchStartupFixture012345',
                           expected='recorder_selection_required', shot=True))
            synthetic = {}
            for name, skill in [('baseline', baseline), ('candidate', candidate)]:
                folder = work / (name + '-save')
                folder.mkdir()
                child = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(Path(__file__).resolve()),
                                        '--editor-child', '--skill', str(skill), '--work', str(folder),
                                        '--fixture-exe', exe.name, *(['--synthetic-session'] if name == 'candidate' else [])],
                                       capture_output=True, encoding='utf-8', timeout=20,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
                (folder / 'stdout.jsonl').write_text(child.stdout, encoding='utf-8')
                (folder / 'stderr.txt').write_text(child.stderr, encoding='utf-8')
                assert child.returncode == 0, (name, child.stdout, child.stderr)
                synthetic[name] = json.loads((folder / 'synthetic-save.json').read_text(encoding='utf-8'))
            assert synthetic['baseline']['document'] == synthetic['candidate']['document'], synthetic
            assert synthetic['baseline']['review'] == synthetic['candidate']['review'], synthetic
            stored = synthetic['candidate']['session_receipt']
            old_saved = json.loads((work / 'baseline-save/stdout.jsonl').read_text(encoding='utf-8').splitlines()[-1])
            def normalized_receipt(value, installation):
                value = json.loads(json.dumps(value))
                token = value.pop('session', None)
                trial = ['try-recording', '--task', value['effective_task']]
                if token:
                    assert value.pop('status_args') == ['recorder', '--session', token, '--wait', '8', '--summary']
                    assert value.pop('first_trial_args') == ['try-recording', '--session', token, '--summary']
                    assert value['next_action']['args'] == ['try-recording', '--session', token, '--summary']
                else:
                    assert value['next_action']['args'] in (trial, [*trial, '--summary'])
                value['next_action']['args'] = trial
                plan = value.get('execution_plan', {})
                if 'path' in plan:
                    plan['path'] = str(Path(plan['path']).relative_to(installation))
                followup = value.get('next_action', {})
                for action in (followup, followup.get('register_and_test', {})):
                    if 'script' in action:
                        action['script'] = str(Path(action['script']).relative_to(installation))
                return value
            assert normalized_receipt(old_saved, baseline) == normalized_receipt(stored, candidate), (old_saved, stored)
            status = cli(candidate, *stored['status_args'])
            assert status['status'] == stored['status'] and status['next_action'] == stored['next_action'], status
            assert cli(candidate, 'recorder', '--session', stored['session']) == stored
            results['synthetic_save'] = dict(schema_unchanged=True, review_receipt_unchanged=True,
                                             saved_receipt_unchanged_except_install_path_and_session=True,
                                             durable_saved_session=status, mode=synthetic['candidate']['mode'])
            assert fixture.command('state')['writes'] == 0
            results['fixture_writes'] = 0
        results['median_ready_seconds'] = {
            name: statistics.median(r['ready_seconds'] for r in results['runs'] if r['case'].startswith(name + '-'))
            for name, _ in timing_installs}
        results['timing_repeats_per_version'] = 1 if args.candidate_only else args.repeats
        results['median_candidate_parent_return_seconds'] = statistics.median(
            r['parent_return_seconds'] for r in results['runs'] if r['case'].startswith('candidate-'))
        results['status'] = 'passed'
    finally:
        write(work / 'summary.json', results)
    print(json.dumps(dict(status=results['status'], medians=results['median_ready_seconds'], output=str(work))), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--repeats', type=int, choices=range(3, 6), default=3)
    parser.add_argument('--candidate-only', action='store_true',
                        help='one candidate launch plus picker/save checks; skip repeated baseline startup timings')
    parser.add_argument('--editor-child', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--skill', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--fixture-exe', help=argparse.SUPPRESS)
    parser.add_argument('--synthetic-session', action='store_true', help=argparse.SUPPRESS)
    options = parser.parse_args()
    if os.name != 'nt':
        parser.error('Windows is required')
    if options.editor_child:
        editor_child(options.skill, options.work, options.fixture_exe, options.synthetic_session)
    else:
        if options.candidate is None or options.baseline is None:
            parser.error('--candidate and --baseline are required')
        run(options)
