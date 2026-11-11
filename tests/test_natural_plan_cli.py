"""Recorded prose and compiled execution stay connected without repeating interpretation."""
from copy import deepcopy
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select as app
import recorded_plan
from test_recorded_plan import recording


class NaturalPlanCLI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.task = recording()
        app.tasks.save_task(self.task, self.folder)
        for target, value in [('task_directory', self.folder), ('bridge.set_dpi_awareness', None)]:
            p = patch('desktop_select.' + target, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def args(self, verb='replay', extra=()):
        flags = [verb, '--task', self.task['name']]
        if verb == 'replay':
            flags += ['--exe', self.task['exe'], '--window', self.task['window']]
        return app.parser().parse_args([*flags, *extra])

    def test_plan_is_local_only_and_keeps_source_prose(self):
        with patch.object(app, 'windows') as windows, patch.object(app, 'worker_receipt') as worker:
            result = app.run(self.args('plan'))
        self.assertEqual(result['status'], 'planned')
        self.assertFalse(result['input_dispatched'])
        windows.assert_not_called()
        worker.assert_not_called()
        saved = recorded_plan.load_plan(self.task, self.folder)
        self.assertEqual(saved['envelope']['source'], self.task)

    def test_normal_replay_uses_one_worker_and_literal_plan(self):
        receipt = dict(status='completed_unverified', input_dispatched=True)
        with patch.object(app, 'worker_receipt', return_value=receipt) as worker:
            result = app.run(self.args(extra=['--set', 'Family=Hybrid']))
        self.assertEqual(result['execution_engine'], 'saved_python_plan')
        worker.assert_called_once()
        self.assertEqual(worker.call_args.args[0], ['_plan'])
        payload = worker.call_args.kwargs['payload']
        self.assertEqual(payload['overrides'], {'Family': 'Hybrid'})
        self.assertEqual(payload['recipe']['steps'][0]['value'], 'N')
        self.assertEqual(payload['recipe']['steps'][1]['parameter'], 'Family')

    def test_missing_parameter_prevents_worker_and_window_discovery(self):
        with patch.object(app, 'worker_receipt') as worker, patch.object(app, 'windows') as windows:
            result = app.run(self.args())
        self.assertEqual(result['status'], 'needs_input')
        self.assertEqual(result['missing_parameters'], ['Family'])
        self.assertFalse(result['input_dispatched'])
        worker.assert_not_called()
        windows.assert_not_called()

    def test_manual_step_falls_back_as_whole_task_before_any_input(self):
        task = deepcopy(self.task)
        task['steps'].append(dict(action='manual', reason='capture_target_unavailable'))
        with patch.object(app.tasks, 'load_task', return_value=task), patch.object(app, 'worker_receipt') as worker:
            result = app.run(self.args(extra=['--set', 'Family=Hybrid']))
        self.assertEqual(result['status'], 'needs_review')
        self.assertFalse(result['input_dispatched'])
        worker.assert_not_called()

    def test_fixed_override_is_rejected_before_dispatch(self):
        with patch.object(app, 'worker_receipt') as worker, self.assertRaises(app.tasks.TaskError):
            app.run(self.args(extra=['--set', 'MES Setup=P', '--set', 'Family=Hybrid']))
        worker.assert_not_called()

    def test_only_and_alias_overrides_keep_original_replay_contract(self):
        args = self.args(extra=['--only', '1'])
        with patch.object(app, 'worker_receipt') as worker:
            self.assertIsNone(app.fast_replay(self.task, args, {}))
        worker.assert_not_called()

    def test_freeform_instruction_saved_with_structured_variable_and_original_untouched(self):
        original = (self.folder / (self.task['name'] + '.json')).read_bytes()
        sentence = 'Family 값은 실행할 때 받은 값을 넣어줘'
        args = self.args('instruct', ['--only', '2', '--text', sentence, '--mode', 'required',
                                     '--argument', 'Family', '--save-as', '새 지시'])
        with patch.object(app, 'worker_receipt') as worker, patch.object(app, 'windows') as windows:
            result = app.run(args)
        self.assertEqual(result['status'], 'recorded')
        saved = app.tasks.load_task('새 지시', self.folder)
        self.assertEqual(saved['steps'][1]['instruction'], dict(text=sentence, mode='required', parameter='Family'))
        self.assertEqual((self.folder / (self.task['name'] + '.json')).read_bytes(), original)
        self.assertEqual(recorded_plan.load_plan(saved, self.folder)['status'], 'loaded')
        worker.assert_not_called()
        windows.assert_not_called()

    def test_same_name_instruction_edit_cannot_overwrite_original(self):
        args = self.args('instruct', ['--only', '1', '--text', 'MES Setup을 P로 설정',
                                     '--save-as', self.task['name']])
        with self.assertRaisesRegex(app.tasks.TaskError, 'task_already_exists'):
            app.run(args)
        self.assertEqual(app.tasks.load_task(self.task['name'], self.folder), self.task)

    def test_long_utf8_value_returns_blocked_json_before_dispatch(self):
        args = self.args(extra=['--set', 'Family=' + '가' * 4000])
        with patch.object(app, 'worker_receipt') as worker, redirect_stdout(StringIO()) as output:
            code = app.worker(args)
        self.assertEqual(code, 2)
        result = json.loads(output.getvalue())
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['diagnostic'], 'invalid_override')
        self.assertFalse(result['input_dispatched'])
        worker.assert_not_called()

    def test_wait_does_not_shift_original_step_used_for_repair(self):
        task = deepcopy(self.task)
        task['steps'][2]['wait_after'] = .1
        task['steps'].append(dict(action='open', role='button', target='export', match='automation_id',
                                  expand=False, command_name='Excel'))
        receipt = dict(status='blocked', stopped_at=5, input_dispatched=True,
                       steps=[dict(number=4, receipt=dict(status='waited', input_dispatched=False)),
                              dict(number=5, receipt=dict(status='blocked', input_dispatched=False))])
        with patch.object(app.tasks, 'load_task', return_value=task), patch.object(
                app, 'worker_receipt', return_value=receipt):
            result = app.run(self.args(extra=['--set', 'Family=Hybrid']))
        self.assertEqual(result['source_stopped_at'], 4)
        self.assertEqual(result['steps'][0]['source_step'], 3)
        self.assertEqual(result['steps'][0]['phase'], 'wait')
        self.assertEqual(result['steps'][1]['source_step'], 4)


if __name__ == '__main__':
    unittest.main()
