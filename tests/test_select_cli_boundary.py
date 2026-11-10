"""The public command must not convert an interrupted write into a safe retry."""
from contextlib import redirect_stdout
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
    def test_provider_timeout_never_retries_and_distinguishes_read_from_write(self):
        for verb, extra in [('select', ['--target', 'mode', '--value', 'new']), ('inspect', [])]:
            args = app.parser().parse_args([verb, '--exe', 'test.exe', '--window', 'work', *extra])
            with self.subTest(verb=verb), redirect_stdout(StringIO()) as output, patch.object(
                    app.subprocess, 'run', side_effect=subprocess.TimeoutExpired('worker', 90)) as run:
                rc = app.supervise(args, [])
                receipt = json.loads(output.getvalue())
                run.assert_called_once()
                expected = (3, 'uncertain', None) if verb == 'select' else (2, 'blocked', False)
                self.assertEqual(expected, (rc, receipt['status'], receipt['input_dispatched']))

    def test_abnormal_exit_must_not_return_even_a_parseable_success(self):
        args = app.parser().parse_args(['open', '--exe', 'test.exe', '--window', 'work', '--target', 'command'])
        with redirect_stdout(StringIO()) as output, patch.object(app.subprocess, 'run',
                return_value=subprocess.CompletedProcess([], 1, stdout='{"status":"verified"}')):
            self.assertEqual(app.supervise(args, []), 3)
            self.assertIsNone(json.loads(output.getvalue())['input_dispatched'])
