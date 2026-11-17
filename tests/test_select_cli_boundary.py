"""The public command must not convert an interrupted write into a safe retry."""
from contextlib import contextmanager, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select as app


class BoundaryTests(unittest.TestCase):
    def test_direct_write_takes_window_before_action_but_inspection_does_not(self):
        record = dict(hwnd=123, pid=21, exe_name='Test.exe', title='Work')
        events = []

        @contextmanager
        def reserve(owner, title, contexts):
            self.assertEqual((title, contexts), ('Work', []))
            events.append('enter')
            try:
                yield
            finally:
                events.append('exit')

        with (patch.object(app, 'windows', return_value=[record]), patch.object(app.bridge, 'Binding'),
              patch.object(app.bridge, 'set_dpi_awareness'), patch.object(app.coordination, 'window', reserve),
              patch.object(app, 'action', side_effect=lambda args, *_a, **_kw: events.append(args.command))):
            app.run(app.parser().parse_args(['open', '--exe', 'Test.exe', '--window', 'Work', '--target', 'Query']))
            app.run(app.parser().parse_args(['inspect', '--exe', 'Test.exe', '--window', 'Work']))
        self.assertEqual(events, ['enter', 'open', 'exit', 'inspect'])

    def test_provider_timeout_never_retries_and_distinguishes_read_from_write(self):
        for verb, extra in [('select', ['--target', 'mode', '--value', 'new']), ('inspect', [])]:
            args = app.parser().parse_args([verb, '--exe', 'test.exe', '--window', 'work', *extra])
            runner, method = ((app.subprocess, 'run') if verb == 'inspect' else (app.coordination, 'run_worker'))
            with self.subTest(verb=verb), redirect_stdout(StringIO()) as output, patch.object(
                    runner, method, side_effect=subprocess.TimeoutExpired('worker', 90)) as run:
                rc = app.supervise(args, [])
                receipt = json.loads(output.getvalue())
                run.assert_called_once()
                expected = (3, 'uncertain', None) if verb == 'select' else (2, 'blocked', False)
                self.assertEqual(expected, (rc, receipt['status'], receipt['input_dispatched']))

    def test_abnormal_exit_must_not_return_even_a_parseable_success(self):
        args = app.parser().parse_args(['open', '--exe', 'test.exe', '--window', 'work', '--target', 'command'])
        with redirect_stdout(StringIO()) as output, patch.object(app.coordination, 'run_worker',
                return_value=subprocess.CompletedProcess([], 1, stdout='{"status":"verified"}')):
            self.assertEqual(app.supervise(args, []), 3)
            self.assertIsNone(json.loads(output.getvalue())['input_dispatched'])

    def test_only_explicit_restore_pauses_recovery_watchdog_during_input_queue(self):
        for extra in ([], ['--restore-window']):
            argv = ['recover', '--task', 'demonstration', *extra]
            runner = app.coordination if extra else app.subprocess
            method = 'run_worker' if extra else 'run'
            with self.subTest(extra=extra), redirect_stdout(StringIO()) as output, patch.object(
                    runner, method, side_effect=subprocess.TimeoutExpired('worker', 90)) as call:
                self.assertEqual(app.supervise(app.parser().parse_args(argv), argv), 2)
                self.assertFalse(json.loads(output.getvalue())['input_dispatched'])
                call.assert_called_once()
