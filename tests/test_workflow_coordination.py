"""Cross-process window/input reservations and active-time watchdog accounting."""
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'
sys.path.insert(0, str(SCRIPTS))
import coordination as c


@unittest.skipUnless(os.name == 'nt', 'Windows named mutexes')
class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.window = dict(hwnd=os.getpid() + 0x70000000, exe_name='Fixture.exe')
        self.other = dict(self.window, hwnd=self.window['hwnd'] + 1)

    def child(self, body):
        return [sys.executable, '-B', '-c', 'import sys, os, time; from pathlib import Path; '
                f'sys.path.insert(0, {str(SCRIPTS)!r}); import coordination as c;\n' + body]

    def test_same_caption_different_handles_can_run_during_window_ownership(self):
        script = self.child(f"with c.window({self.other!r}, 'Shared caption'): print('other window')")
        with c.window(self.window, 'Shared caption'):
            result = c.run_worker(script, capture_output=True, timeout=5, encoding='utf8')
        self.assertEqual((result.returncode, result.stdout.strip()), (0, 'other window'))

    def test_high_bit_aliases_share_the_window_mutex_across_processes(self):
        hwnd = 0x81000000 + os.getpid()
        with c.handle({'hwnd': hwnd - 2**32}):
            command = self.child("c.QUEUE_SECONDS = 0\ntry:\n"
                                 f" with c.handle({{'hwnd': {0xffffffff00000000 | hwnd}}}): print('free')\n"
                                 "except c.CoordinationError as error: print(error.code)")
            self.assertEqual(subprocess.check_output(command, text=True, timeout=5).strip(), 'window_queue_timeout')

    def test_same_window_cross_process_waits_without_spending_worker_watchdog(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / 'ready'
            command = self.child(f"Path({str(ready)!r}).touch()\n"
                                 f"with c.window({self.window!r}, 'New title'): print(sys.stdin.read())")
            held = threading.Event()
            def holder():
                with c.window(self.window, 'Previous title'):
                    held.set()
                    deadline = time.monotonic() + 8
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    time.sleep(1.5)
            thread = threading.Thread(target=holder)
            thread.start()
            try:
                self.assertTrue(held.wait(3))
                start = time.monotonic()
                result = c.run_worker(command, timeout=1.2, input='done', encoding='utf8')
                self.assertGreater(time.monotonic() - start, 1.2)
                self.assertGreater(result.queue_seconds, 1.2)
                self.assertEqual((result.returncode, result.stdout.strip()), (0, 'done'))
            finally:
                thread.join(9)

    def test_no_queue_never_extends_watchdog_for_hung_worker(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            c.run_worker(self.child('time.sleep(20)'), timeout=.3, encoding='utf8')

    def test_directory_and_named_workbook_share_output_until_release_across_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / 'Export'
            folder.mkdir()
            named = dict(action='verify-file', output=dict(kind='file', path=str(folder / 'result.xlsx')))
            scoped = dict(action='result-menu', context=dict(output=dict(kind='directory', path=str(folder).upper())))
            other = dict(action='verify-file', output=dict(kind='directory', path=directory))
            def attempt(step):
                command = self.child("c.QUEUE_SECONDS = 0\ntry:\n"
                                     f" with c.reserve_outputs([{step!r}]): print('free')\n"
                                     "except c.CoordinationError as error: print(error.code)")
                return subprocess.check_output(command, text=True, timeout=5).strip()
            for holder, peer in [(named, scoped), (scoped, named)]:
                with c.reserve_outputs([holder]):
                    self.assertEqual(attempt(peer), 'export_queue_timeout')
                    self.assertEqual(attempt(other), 'free')
                self.assertEqual(attempt(peer), 'free')

    def test_crashed_owner_releases_window_and_desktop_mutexes(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            ready = Path(directory) / 'ready'
            # Use a private stand-in for the shared pointer. No real desktop input.
            lock_name = 'testInput' + os.urandom(8).hex()
            guards = f"c.window({self.window!r}, 'Fixture'), c._lease({lock_name!r}, 'timeout')"
            proc = subprocess.Popen(self.child(f"with {guards}:\n Path({str(ready)!r}).touch()\n time.sleep(30)"))
            def stop():
                proc.kill()
                proc.wait()
            cleanup.callback(stop)
            deadline = time.monotonic() + 5
            while not ready.is_file() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(ready.is_file())
            stop()
            result = c.run_worker(self.child(f"with {guards}: print('free')"), timeout=3, encoding='utf8')
            self.assertEqual((result.returncode, result.stdout.strip()), (0, 'free'))

    def test_reverse_multiwindow_jobs_avoid_hold_and_wait_cycle(self):
        done = []
        gate = threading.Barrier(2)
        def job(owner, title, secondary, second_title):
            gate.wait(timeout=2)
            with c.window(owner, title, [dict(exe='Fixture.exe', title=second_title)]):
                with c.handle(secondary):
                    done.append(('start', title))
                    time.sleep(.03)
                    done.append(('end', title))
        threads = [threading.Thread(target=job, args=order, daemon=True) for order in (
            (self.window, 'A', self.other, 'B'), (self.other, 'B', self.window, 'A'))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
        self.assertEqual([event for event, _ in done], ['start', 'end', 'start', 'end'])
        self.assertNotEqual(done[0][1], done[2][1])
