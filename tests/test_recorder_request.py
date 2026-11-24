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

    def test_exe_only_opens_one_named_window_in_the_original_request(self):
        self.write(dict(command='record', task='처음 녹화', exe='Test.exe'))
        row = dict(title='업무  "창"', hwnd=1)
        with (patch.object(app.bridge, 'set_dpi_awareness'), patch.object(app, 'guard') as guard,
              patch.object(app, 'task_directory', return_value=self.folder),
              patch.object(app, 'windows', return_value=[dict(title='', hwnd=2), row]) as windows,
              patch.object(app.bridge, 'Binding') as bind, patch.object(app, 'edit_recording') as edit):
            edit.return_value = dict(status='cancelled', input_dispatched=False)
            self.assertEqual(app.run(self.cli), edit.return_value)
            windows.assert_called_once_with('Test.exe')
            bind.assert_called_once_with(row)
            guard.assert_called_once_with(bind.return_value, bind.return_value, row['title'])
            self.assertEqual(edit.call_args.args[1].title, row['title'])
            self.assertTrue(edit.call_args.kwargs['report_ready'])

    def test_candidate_choices_are_returned_without_opening_an_editor(self):
        rows = [dict(title='A', hwnd=1), dict(title='A', hwnd=2), dict(title='B', hwnd=3)]
        for window, expected in ((None, rows), ('A', rows[:2])):
            request = dict(command='record', task='처음 녹화', exe='Test.exe')
            if window is not None:
                request['window'] = window
            self.write(request)
            with (self.subTest(window=window), patch.object(app.bridge, 'set_dpi_awareness'),
                  patch.object(app, 'windows', return_value=rows), patch.object(app, 'edit_recording') as edit):
                self.assertEqual(app.run(self.cli), dict(status='needs_input', diagnostic='ambiguous_window',
                                                         windows=expected, input_dispatched=False))
                edit.assert_not_called()

    def test_known_hwnd_never_reads_a_foreign_title_or_traverses_other_windows(self):
        with (patch.object(app.bridge, '_windows'), patch.object(app.bridge, '_sig') as enumerate_windows,
              patch.object(app.bridge, 'root_window', return_value=42) as root,
              patch.object(app.bridge, 'user32', create=True) as api, patch.object(app.bridge, 'hwnd_pid', return_value=7),
              patch.object(app.bridge, '_process_identity', return_value=('Other.exe', '123')) as identity,
              patch.object(app.bridge, 'window_text', return_value='Title') as caption):
            api.IsWindowVisible.return_value = True
            self.assertEqual(app.windows('Test.exe', only_hwnd=42), [])
            caption.assert_not_called()
            identity.return_value = ('Test.exe', '4545')
            self.assertEqual(app.windows('Test.exe', only_hwnd=42), [
                dict(title='Title', hwnd=42, exe_name='Test.exe', pid=7, process_started_utc_ticks='4545')])
            root.return_value = 8
            self.assertEqual(app.windows('Test.exe', only_hwnd=42), [])
            caption.assert_called_once_with(42)
            enumerate_windows.assert_not_called()

    def test_exact_request_and_inventory_bind_the_same_high_bit_window(self):
        canonical = 0xbadc1242
        extended = 0xffffffff00000000 | canonical
        title = self.request['window']
        exe = self.request['exe']
        with (patch.object(app.bridge, 'set_dpi_awareness'), patch.object(app.bridge, '_windows'),
              patch.object(app.bridge, 'user32', create=True) as api,
              patch.object(app.bridge, 'kernel32', create=True),
              patch.object(app.bridge, 'hwnd_pid', return_value=123),
              patch.object(app.bridge, '_process_identity', return_value=(exe, '456')),
              patch.object(app.bridge, 'window_text', return_value=title),
              patch.object(app.C, 'WINFUNCTYPE', return_value=lambda callback: callback, create=True),
              patch.object(app.bridge, '_sig', side_effect=lambda lib, key, *rest: getattr(lib, key)),
              patch.object(app, 'task_directory', return_value=self.folder),
              patch.object(app, 'edit_recording', return_value={'status': 'cancelled'}) as editor):
            api.GetAncestor.return_value = extended
            api.EnumWindows.side_effect = lambda callback, arg: callback(extended, arg)
            for representation in (None, canonical, canonical - (1 << 32), extended):
                with self.subTest(representation=representation):
                    request = dict(self.request)
                    if representation is None:
                        del request['hwnd']
                    else:
                        request['hwnd'] = representation
                    self.write(request)
                    self.assertEqual(app.run(self.cli), {'status': 'cancelled'})
                    owner, storage = editor.call_args.args
                    self.assertEqual((owner.hwnd, owner.pid, owner.created, storage.title),
                                     (canonical, 123, '456', title))
                    self.assertTrue(editor.call_args.kwargs['report_ready'])
            self.assertEqual(editor.call_count, 4)
            self.assertEqual(api.EnumWindows.call_count, 1)

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
                   dict(self.request, hwnd=True), dict(self.request, hwnd=-1),
                   dict(self.request, hwnd=0x10000002a), dict(self.request, window=''),
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

    def test_explicit_edit_round_trips_saved_task_and_remaps_child_after_reorder(self):
        from capture_trail import finalize_contexts
        from teach_capture import StepEdits
        task = recording()
        task['steps'][2]['wait_after'] = 1
        task['steps'][2]['window_context'] = dict(title='Child', exe='Child.exe', after_step=1, related=True)
        app.tasks.save_task(task, self.folder)
        self.write(dict(command='review', task=task['name'], edit=True))

        def edit(owner, storage, *, report_ready):
            self.assertIsNone(owner)
            self.assertTrue(report_ready)
            edits = StepEdits()
            rows = edits.restore_rows(storage.initial_rows())
            rows[0], rows[1] = rows[1], rows[0]
            return storage.save_recording(finalize_contexts(edits.apply(rows)))

        with (patch.object(app, 'task_directory', return_value=self.folder),
              patch.object(app, 'edit_recording', side_effect=edit), patch.object(app, 'windows') as windows):
            result = app.run(self.cli)
        windows.assert_not_called()
        self.assertEqual(result['task']['name'], task['name'] + ' - 수정본')
        saved = app.tasks.load_task(result['task']['name'], self.folder)
        self.assertEqual(saved['steps'][:2], [task['steps'][1], task['steps'][0]])
        self.assertEqual(saved['steps'][2], dict(task['steps'][2], window_context=dict(
                                task['steps'][2]['window_context'], after_step=2)))
        self.assertEqual(app.tasks.load_task(task['name'], self.folder), task)

    def test_split_native_arguments_report_actionable_json(self):
        child = subprocess.run([sys.executable, '-X', 'utf8', '-B', app.__file__, 'record', '--task', '새',
                                '녹화', '--exe', 'Demo.exe', '--window', '업무', '창'],
                               capture_output=True, encoding='utf-8', timeout=15)
        self.assertEqual(child.returncode, 2)
        self.assertEqual(json.loads(child.stdout)['diagnostic'], 'invalid_recorder_arguments')
        self.assertIn('recorder --request', json.loads(child.stdout)['hint'])
