"""Host recording requests cross Windows shell boundaries without reparsing captions."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select as app
from test_recorded_plan import recording


class RecorderRequestTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.path = self.folder / '녹화 요청 [1].json'
        self.request = dict(command='record', task='--새 녹화 A & B', exe='Test App.exe',
                            window='조건  조회 "A" & [$value] (1) ', hwnd=42)
        self.cli = app.parser().parse_args(['recorder', '--request', str(self.path)])

    def write(self, data, encoding='utf-8'):
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding=encoding)

    def test_preserves_exact_strings_including_repeated_and_trailing_spaces(self):
        for encoding in ('utf-8-sig', 'utf-8'):
            with self.subTest(encoding=encoding):
                self.write(self.request, encoding)
                args = vars(app.recorder_request(self.path))
                self.assertEqual(args, dict(self.request, report_ready=True))

    def test_review_and_continuation_use_the_existing_command_defaults(self):
        for command in ('review', 'continue-recording'):
            for extra in ([{}, {'from_step': 2, 'save_as': '저장 사본', 'supplement': '보완 녹화'}]
                          if command == 'continue-recording' else [{}]):
                request = dict(command=command, task='기존 초안', **extra)
                cli = [command, '--task', request['task']]
                for key, value in extra.items():
                    cli.extend(['--' + key.replace('_', '-'), str(value)])
                self.write(request)
                self.assertEqual(vars(app.recorder_request(self.path)),
                                 dict(vars(app.parser().parse_args(cli)), report_ready=True))

    def test_bad_requests_emit_one_terminal_error_before_window_lookup(self):
        invalid = [[], {'command': 'open', 'task': '업무'},
                   dict(self.request, hwnd=True), dict(self.request, window=''),
                   dict(self.request, window=['업무']), dict(self.request, extra='--open')]
        with patch.object(app, 'windows') as lookup:
            for data in invalid:
                with self.subTest(data=data), redirect_stdout(StringIO()) as output:
                    self.write(data)
                    self.assertEqual(app.worker(self.cli), 2)
                    self.assertEqual(json.loads(output.getvalue()), dict(status='blocked',
                                     diagnostic='invalid_recorder_request', input_dispatched=False))
            lookup.assert_not_called()

    def test_bad_file_does_not_fall_back_to_a_different_window(self):
        for raw, diagnostic in ((None, 'recorder_request_unreadable'),
                                (b'{"command":"review","task":"a","task":"b"}', 'invalid_recorder_request'),
                                (b'{', 'invalid_recorder_request'), (b' ' * 65537, 'recorder_request_too_large')):
            if raw is not None:
                self.path.write_bytes(raw)
            with self.subTest(diagnostic=diagnostic), self.assertRaisesRegex(app.SelectionError, diagnostic):
                app.recorder_request(self.path)

    def test_already_saved_review_keeps_original_single_result(self):
        task = recording()
        app.tasks.save_task(task, self.folder)
        self.write(dict(command='review', task=task['name']))
        with patch.object(app, 'task_directory', return_value=self.folder), patch.object(app, 'edit_recording') as ui:
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(app.worker(self.cli), 0)
            self.assertEqual(json.loads(output.getvalue())['diagnostic'], 'task_already_saved')
            self.assertTrue(output.getvalue().isascii())
            ui.assert_not_called()

    def test_split_native_arguments_report_actionable_json(self):
        child = subprocess.run([sys.executable, '-X', 'utf8', '-B', app.__file__, 'record', '--task', '새',
                                '녹화', '--exe', 'Demo.exe', '--window', '업무', '창'],
                               capture_output=True, encoding='utf-8', timeout=15)
        self.assertEqual(child.returncode, 2)
        self.assertEqual(json.loads(child.stdout)['diagnostic'], 'invalid_recorder_arguments')
        self.assertIn('recorder --request', json.loads(child.stdout)['hint'])
