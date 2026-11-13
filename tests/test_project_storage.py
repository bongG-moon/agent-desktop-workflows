"""All named-task operations share the installed project's store, never old AppData by default."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select as app
import learned_tasks as tasks
import python_workflow as flow
import recorded_plan as plans
import storage_paths
from test_recorded_plan import recording


class ProjectStorageIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {'LOCALAPPDATA': str(self.root / 'old-user-data')})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('DESKTOP_SELECT_TASK_DIR', None)
        self.project_a, self.project_b = self.root / 'project-a', self.root / 'project-b'
        self.source = recording()

    def install(self, project):
        return patch.object(storage_paths, '__file__',
                            str(project / '.claude/skills/desktop-select/scripts/storage_paths.py'))

    def test_storage_and_empty_listing_do_not_create_files_or_observe_ui(self):
        with self.install(self.project_a), patch.object(app.bridge, 'set_dpi_awareness') as dpi, patch.object(app, 'windows') as windows:
            result = app.run(app.parser().parse_args(['storage']))
            self.assertEqual(result['status'], 'observed')
            self.assertEqual(Path(result['task_directory']), self.project_a / '.desktop-select/tasks')
            self.assertEqual(Path(result['execution_plan_directory']), self.project_a / '.desktop-select/tasks/execution-plans')
            self.assertFalse(result['input_dispatched'])
            dpi.assert_not_called()
            windows.assert_not_called()
            self.assertEqual(tasks.list_tasks(), [])
            self.assertFalse(self.project_a.exists())
            self.assertFalse((self.root / 'old-user-data').exists())

    def test_two_projects_same_name_keep_separate_source_plan_and_replay(self):
        with self.install(self.project_a):
            tasks.save_task(self.source)
            first = plans.get_plan(self.source)
        second_source = deepcopy(self.source)
        second_source['steps'][0]['instruction'] = dict(mode='fixed', text='MES Setup을 P로 설정', value='P')
        with self.install(self.project_b):
            self.assertEqual(tasks.list_tasks(), [])
            tasks.save_task(second_source)
            second = plans.get_plan(second_source)
        self.assertNotEqual(first['path'], second['path'])
        for project, value in ((self.project_a, 'N'), (self.project_b, 'P')):
            with self.install(project), patch.object(app.bridge, 'set_dpi_awareness'), patch.object(
                    app, 'worker_receipt', return_value=dict(status='verified', input_dispatched=True)) as worker:
                args = app.parser().parse_args(['replay', '--task', self.source['name'], '--exe', self.source['exe'],
                                               '--window', self.source['window'], '--set', 'Family=Hybrid'])
                app.run(args)
                self.assertEqual(worker.call_args.kwargs['payload']['recipe']['steps'][0]['value'], value)
                self.assertEqual(tasks.load_task(self.source['name'])['steps'][0]['instruction']['value'], value)

    def test_shared_data_is_not_listed_copied_or_modified_implicitly(self):
        shared = self.root / 'old-user-data/DesktopSelect/tasks'
        saved = tasks.save_task(self.source, shared)
        before = saved.read_bytes()
        with self.install(self.project_a):
            self.assertEqual(tasks.list_tasks(), [])
            with self.assertRaisesRegex(tasks.TaskError, 'task_not_found'):
                tasks.load_task(self.source['name'])
            self.assertFalse((self.project_a / '.desktop-select').exists())
            with patch.dict(os.environ, {'DESKTOP_SELECT_TASK_DIR': str(shared)}):
                self.assertEqual(tasks.load_task(self.source['name']), self.source)
                self.assertEqual(Path(app.run(app.parser().parse_args(['storage']))['task_directory']), shared)
        self.assertEqual(saved.read_bytes(), before)

    def test_python_registration_reads_the_same_project_source(self):
        with self.install(self.project_a):
            tasks.save_task(self.source)
            with patch.object(flow, 'register', return_value=dict(status='registered', input_dispatched=False)) as register, redirect_stdout(StringIO()):
                code = flow.main(['--_worker', 'register', '--task', self.source['name'],
                                  '--out', str(self.project_a / '.claude/skills/query-task')])
            self.assertEqual(code, 0)
            self.assertEqual(register.call_args.kwargs['source_task'], self.source)
            self.assertEqual(register.call_args.args[0]['steps'][0]['value'], 'N')
        with self.install(self.project_b), patch.object(flow, 'register') as register, redirect_stdout(StringIO()) as stdout:
            code = flow.main(['--_worker', 'register', '--task', self.source['name'], '--out', str(self.root / 'other-skill')])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(stdout.getvalue())['diagnostic'], 'task_not_found')
            register.assert_not_called()

    def test_store_resolution_errors_are_reported_without_any_write(self):
        with self.install(self.project_a), patch.dict(os.environ, {'DESKTOP_SELECT_TASK_DIR': 'relative-store'}), redirect_stdout(StringIO()) as stdout:
            code = app.worker(app.parser().parse_args(['storage']))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout.getvalue())['diagnostic'], 'task_directory_absolute_required')
        self.assertFalse(self.project_a.exists())


if __name__ == '__main__':
    unittest.main()
