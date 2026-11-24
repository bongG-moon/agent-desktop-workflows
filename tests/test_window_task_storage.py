"""A step stays bound to its chosen window through editing, storage and partial replay."""
from argparse import Namespace
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, mock

from test_learned_tasks import demo, task
import desktop_select as app
import learned_tasks as tasks


class WindowTaskStorageTests(TestCase):
    existing = dict(exe='Viewer.exe', title='Second viewer', mode='existing')
    args = Namespace(exe='Agent.exe', window='Device Setup', hwnd=None, only=2)

    def test_one_reservation_spans_all_legacy_workers(self):
        events = []

        @contextmanager
        def reserve(*_args):
            events.append('enter')
            try:
                yield
            finally:
                events.append('exit')

        def verified(_cmd, *, mutating, payload):
            self.assertTrue(mutating)
            events.append(payload['step']['target'])
            return dict(status='verified', input_dispatched=True, effect_verification='matched')

        args = Namespace(**{**vars(self.args), 'only': None})
        row = dict(hwnd=11, pid=11, exe_name='Agent.exe', title='Device Setup')
        with (mock.patch.object(app, 'windows', return_value=[row]),
              mock.patch.object(app.coordination, 'window', reserve),
              mock.patch.object(app, 'worker_receipt', side_effect=verified)):
            result = app._replay_recorded(task(demo(), demo('Family')), args, {}, True)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(events, ['enter', 'MESSetupID', 'FamilyID', 'exit'])

    def test_save_edit_read_preserves_window_and_variable(self):
        raw = dict(demo('Family'), window_context=self.existing)
        raw['instruction'] = tasks.compile_instruction(raw, '입력받은 변수를 Family 조건에 입력해줘')
        plan = task(demo(), raw)
        with TemporaryDirectory() as folder:
            tasks.save_task(plan, Path(folder))
            saved = tasks.load_task(plan['name'], Path(folder))['steps'][1]
        self.assertEqual(saved['window_context'], self.existing)
        self.assertEqual(saved['instruction']['mode'], 'required')

    def test_existing_detail_window_reserved_before_first_legacy_step(self):
        main = dict(hwnd=11, pid=11, exe_name='Agent.exe', title='Device Setup', process_started_utc_ticks='1')
        detail = dict(hwnd=22, pid=22, exe_name='Viewer.exe', title='Second viewer', process_started_utc_ticks='2')
        def bind(row):
            return SimpleNamespace(hwnd=row['hwnd'], pid=row['pid'], exe=row['exe_name'], created=row['process_started_utc_ticks'])
        calls = []
        @contextmanager
        def lock(row):
            hwnd = row['hwnd'] if isinstance(row, dict) else row.hwnd
            calls.append(('enter', hwnd))
            try:
                yield
            finally:
                calls.append(('leave', hwnd))
        def send(_cmd, *, mutating, payload):
            calls.append(('action', payload['binding']['hwnd']))
            return dict(status='verified', input_dispatched=True, effect_verification='matched')
        args = Namespace(**{**vars(self.args), 'only': None})
        steps = task(demo(), dict(demo('Family'), window_context=self.existing))
        with (mock.patch.object(app, 'windows', side_effect=lambda exe: [main if exe == main['exe_name'] else detail]),
              mock.patch.object(app.bridge, 'Binding', side_effect=bind), mock.patch.object(app, 'guard'),
              mock.patch.object(app.coordination, 'handle', lock), mock.patch.object(app, 'worker_receipt', send)):
            self.assertEqual(app._replay_recorded(steps, args, {}, True)['status'], 'verified')
        self.assertEqual(calls, [('enter', 11), ('enter', 22), ('action', 11), ('action', 22),
                                 ('leave', 22), ('leave', 11)])

    def test_legacy_parent_reserves_result_menu_folder_before_first_window_and_through_all_children(self):
        events = []
        @contextmanager
        def lease(name, diagnostic):
            events.append(('start', diagnostic))
            try:
                yield
            finally:
                events.append(('end', diagnostic))

        def worker(cmd, *, payload, mutating):
            events.append(payload['step']['action'])
            return dict(status='verified', input_dispatched=True, effect_verification='matched')
        raw = dict(action='result-menu', label='Workbook', context=dict(
            trigger=None, ready=None, output=dict(kind='directory', path=r'C:\DesktopSelectTest'),
            menu=dict(target=dict(role='table', name='Results'), point=[.4, .4], path=['Workbook'])))
        args = Namespace(**{**vars(self.args), 'only': None})
        row = dict(hwnd=11, pid=11, exe_name='Agent.exe', title='Device Setup')
        with (mock.patch.object(app, 'windows', return_value=[row]),
              mock.patch.object(app.coordination, '_lease', side_effect=lease),
              mock.patch.object(app, 'worker_receipt', side_effect=worker)):
            result = app._replay_recorded(task(demo(), raw), args, {}, False)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(events, [('start', 'export_queue_timeout'), ('start', 'window_queue_timeout'), 'select',
                                  'result-menu', ('end', 'window_queue_timeout'), ('end', 'export_queue_timeout')])

    def test_window_queue_timeout_sends_no_step(self):
        row = dict(hwnd=11, pid=11, exe_name='Agent.exe', title='Device Setup')
        with (mock.patch.object(app, 'windows', return_value=[row]),
              mock.patch.object(app.coordination, 'window',
                                side_effect=app.coordination.CoordinationError('window_queue_timeout')),
              mock.patch.object(app, 'worker_receipt') as worker):
            result = app._replay_recorded(task(demo(), demo('Family')), self.args, {}, True)
        self.assertFalse(result['input_dispatched'])
        self.assertEqual(result['steps'][0]['receipt']['diagnostic'], 'window_queue_timeout')
        worker.assert_not_called()

    def test_partial_fallback_sends_only_to_existing_recorded_window(self):
        main = dict(hwnd=11, pid=11, exe_name='Agent.exe', title='Device Setup', process_started_utc_ticks='1')
        viewer = dict(hwnd=2, pid=12, exe_name='Viewer.exe', title='Second viewer', process_started_utc_ticks='2')
        def windows(exe):
            return [main if exe == main['exe_name'] else viewer]
        def bind(row):
            return SimpleNamespace(hwnd=row['hwnd'], pid=row['pid'], exe=row['exe_name'],
                                   created=row['process_started_utc_ticks'])
        with (mock.patch.object(app, 'windows', side_effect=windows),
              mock.patch.object(app.bridge, 'Binding', side_effect=bind),
              mock.patch.object(app, 'guard'),
              mock.patch.object(app, 'worker_receipt') as worker):
            worker.return_value = dict(status='verified', input_dispatched=True, effect_verification='matched')
            raw = dict(demo('Family'), window_context=self.existing)
            result = app._replay_recorded(task(demo(), raw), self.args, {}, True)
            self.assertEqual(result['status'], 'verified')
            payload = worker.call_args.kwargs['payload']
            self.assertEqual((payload['binding'], payload['main_window']), (viewer, main))
            worker.reset_mock()
            raw['window_context'] = dict(exe='Viewer.exe', title='Second viewer', after_step=1, related=True)
            result = app._replay_recorded(task(demo(), raw), self.args, {}, True)
            self.assertFalse(result['input_dispatched'])
            self.assertEqual(result['steps'][0]['receipt']['diagnostic'], 'window_origin_step_not_run')
            worker.assert_not_called()
