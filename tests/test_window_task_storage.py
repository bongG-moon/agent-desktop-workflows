"""A step stays bound to its chosen window through editing, storage and partial replay."""
from argparse import Namespace
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

    def test_save_edit_read_preserves_window_and_variable(self):
        raw = dict(demo('Family'), window_context=self.existing)
        raw['instruction'] = tasks.compile_instruction(raw, '입력받은 변수를 Family 조건에 입력해줘')
        plan = task(demo(), raw)
        with TemporaryDirectory() as folder:
            tasks.save_task(plan, Path(folder))
            saved = tasks.load_task(plan['name'], Path(folder))['steps'][1]
        self.assertEqual(saved['window_context'], self.existing)
        self.assertEqual(saved['instruction']['mode'], 'required')

    def test_partial_fallback_sends_only_to_existing_recorded_window(self):
        main = dict(hwnd=1, pid=11, exe_name='Agent.exe', title='Device Setup', process_started_utc_ticks='1')
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
