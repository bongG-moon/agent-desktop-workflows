"""A requested first test composes registration and the existing runner once."""
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import python_workflow as flow
import recorded_plan


class RegisterFirstRunTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.source = self.folder / 'source.json'
        self.destination = self.folder / 'first-test'
        self.recipe = dict(schema=1, name='Report', exe='Example.exe', window='Example', steps=[
            dict(action='set-value', selector=dict(role='combobox', automation_id='status'), value='N'),
            dict(action='set-value', selector=dict(role='combobox', automation_id='family'), parameter='Family'),
            dict(action='invoke', selector=dict(role='button', automation_id='query'))])
        self.result = dict(status='verified', input_dispatched=True, steps=[
            dict(number=i, receipt=dict(status='verified', input_dispatched=True)) for i in range(1, 4)])
        self.write_source()

    def write_source(self):
        self.source.write_text(json.dumps(self.recipe), encoding='utf8')

    def cli(self, *args):
        with redirect_stdout(StringIO()) as stdout:
            code = flow.main(['register', '--recipe', str(self.source), '--out', str(self.destination), *args])
        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 1, stdout.getvalue())
        return code, json.loads(lines[0])

    def worker(self, argv, **kwargs):
        self.assertEqual(argv[1:4], ['-X', 'utf8', '-B'])
        self.assertEqual(Path(argv[4]).resolve(), Path(flow.__file__).resolve())
        self.assertEqual(argv[5], '--_worker')
        with redirect_stdout(StringIO()) as stdout:
            code = flow.main(argv[5:])
        return subprocess.CompletedProcess(argv, code, stdout.getvalue())

    def execute(self, recipe, *args, **kwargs):
        if kwargs.get('read_only'):
            return dict(status='observed', input_dispatched=False, steps=[
                dict(number=i, receipt=dict(status='observed', actions=['SetValue', 'Click']))
                for i in range(1, len(recipe['steps']) + 1)])
        return deepcopy(self.result)

    @contextmanager
    def workers(self):
        with (patch.object(flow.subprocess, 'run', side_effect=self.worker) as registration,
              patch.object(flow.coordination, 'run_worker', side_effect=self.worker) as execution,
              patch.object(flow, 'run', side_effect=self.execute) as engine):
            yield registration, execution, engine

    def test_one_command_registers_then_runs_once_and_keeps_original_parameters(self):
        original = self.source.read_bytes()
        with self.workers() as (registration, execution, engine):
            code, result = self.cli('--run', '--set', 'Family=Hybrid')
        self.assertEqual(code, 0)
        self.assertEqual({k: result[k] for k in self.result}, self.result)
        self.assertEqual(result['registration']['status'], 'registered')
        self.assertEqual(result['registration']['required_parameters'], ['Family'])
        self.assertEqual(result['registration']['run_args'],
                         [str(self.destination.resolve() / 'scripts/run.py'), '--summary'])
        registration.assert_called_once()
        execution.assert_called_once()
        self.assertEqual(execution.call_args.kwargs['timeout'], flow.worker_timeout(self.recipe))
        self.assertEqual(engine.call_count, 2)
        self.assertTrue(engine.call_args_list[0].kwargs['read_only'])
        self.assertFalse(engine.call_args_list[1].kwargs['read_only'])
        self.assertEqual(engine.call_args_list[1].args[1], {'Family': 'Hybrid'})
        self.assertEqual(flow.load(self.destination / 'workflow.json'), self.recipe)
        self.assertEqual(self.source.read_bytes(), original)

    def test_missing_or_unknown_parameter_stops_before_registration_or_any_worker(self):
        for options, diagnostic in [((), None), (('--set', 'Other=Hybrid'), 'unknown_parameter')]:
            with self.subTest(options=options), self.workers() as (registration, execution, engine):
                code, result = self.cli('--run', *options)
            self.assertEqual(code, 2)
            self.assertFalse(result['input_dispatched'])
            if diagnostic:
                self.assertEqual(result['diagnostic'], diagnostic)
                self.assertEqual(result['required_parameters'], ['Family'])
                self.assertNotIn('Hybrid', json.dumps(result))
            else:
                self.assertEqual(result['missing_parameters'], ['Family'])
            self.assertFalse(self.destination.exists())
            registration.assert_not_called()
            execution.assert_not_called()
            engine.assert_not_called()

    def test_physical_consent_and_background_policy_are_checked_before_registration(self):
        from test_python_context_workflow import context
        self.recipe['steps'].append(context())
        self.write_source()
        for options, diagnostic in [([], 'registered_physical_consent_required'),
                                    (['--background-only', '--allow-physical'], 'background_requires_desktop_interaction')]:
            with self.subTest(options=options), self.workers() as (registration, execution, engine):
                code, result = self.cli('--run', '--set', 'Family=Hybrid', *options)
            self.assertEqual((code, result['diagnostic'], result['input_dispatched']), (2, diagnostic, False))
            self.assertFalse(self.destination.exists())
            registration.assert_not_called()
            execution.assert_not_called()
            engine.assert_not_called()

    def test_missing_runtime_value_and_first_consent_are_reported_together_without_inspection(self):
        from test_python_context_workflow import context
        self.recipe['steps'].append(context())
        self.write_source()
        with self.workers() as (registration, execution, engine):
            code, first = self.cli('--run')
            self.assertFalse(self.destination.exists())
            (self.destination / 'scripts').mkdir(parents=True)
            (self.destination / 'scripts/run.py').write_text(flow.RUNNER.replace('RUNTIME_PARENT', '2'))
            recipe = self.destination / 'workflow.json'
            recipe.write_bytes(self.source.read_bytes())
            with redirect_stdout(StringIO()) as stdout:
                self.assertEqual(flow.main(['run', '--recipe', str(recipe)]), 2)
            for result in (first, json.loads(stdout.getvalue())):
                self.assertEqual(result['status'], 'needs_consent')
                self.assertEqual(result['missing_parameters'], ['Family'])
                self.assertFalse(result['input_dispatched'])
            self.assertEqual(code, 2)
            registration.assert_not_called()
            execution.assert_not_called()
            engine.assert_not_called()

    def test_existing_destination_never_runs_or_modifies_the_previous_registration(self):
        self.destination.mkdir()
        marker = self.destination / 'keep.txt'
        marker.write_text('existing work', encoding='utf8')
        with self.workers() as (registration, execution, engine):
            code, result = self.cli('--run', '--set', 'Family=Hybrid')
        self.assertEqual((code, result['diagnostic']), (2, 'workflow_destination_exists'))
        self.assertEqual(result['host_action'], 'check_existing_registration_without_rerunning')
        self.assertEqual(marker.read_text(encoding='utf8'), 'existing work')
        registration.assert_not_called()
        execution.assert_not_called()
        engine.assert_not_called()

    def test_registration_failure_never_starts_first_test(self):
        blocked = dict(status='blocked', diagnostic='target_missing', input_dispatched=False, steps=[])
        with self.workers() as (registration, execution, engine):
            engine.side_effect = None
            engine.return_value = blocked
            code, result = self.cli('--run', '--set', 'Family=Hybrid')
        self.assertEqual((code, {k: result[k] for k in blocked}), (2, blocked))
        registration.assert_called_once()
        execution.assert_not_called()
        engine.assert_called_once()
        self.assertFalse(self.destination.exists())

    def test_uncertain_first_test_preserves_dispatch_and_successful_registration_without_retry(self):
        self.result.update(status='uncertain', input_dispatched=None, diagnostic='input_delivery_unknown', stopped_at=3)
        with self.workers() as (registration, execution, engine):
            code, result = self.cli('--run', '--summary', '--set', 'Family=Hybrid')
        self.assertEqual(code, 3)
        self.assertEqual({k: result[k] for k in self.result}, self.result)
        self.assertEqual(result['registration']['status'], 'registered')
        self.assertTrue((self.destination / 'scripts/run.py').is_file())
        execution.assert_called_once()
        self.assertEqual(engine.call_count, 2)

    def test_lost_run_receipt_keeps_registration_and_reports_uncertainty(self):
        with self.workers() as (registration, execution, engine):
            execution.side_effect = subprocess.TimeoutExpired('workflow', 90)
            code, result = self.cli('--run', '--set', 'Family=Hybrid')
        self.assertEqual((code, result['status'], result['input_dispatched']), (3, 'uncertain', None))
        self.assertEqual(result['registration']['status'], 'registered')
        execution.assert_called_once()
        engine.assert_called_once()

    def test_menu_first_test_keeps_ticket_and_forwards_approved_frame_options(self):
        from test_python_context_workflow import context
        self.recipe['steps'].append(context())
        self.write_source()
        failure = dict(status='blocked', diagnostic='context_point_outside_region', input_dispatched=True,
                       steps=self.result['steps'], stopped_at=4,
                       menu_repair=dict(ticket='one-use', frame='fresh', resume_args=['--resume-menu', 'one-use']))
        with self.workers() as (registration, execution, engine), patch.object(
                flow.workflow_repair, 'run_registered', return_value=failure) as registered_run:
            code, result = self.cli('--run', '--allow-physical', '--auto-repair-frame',
                                    '--summary', '--set', 'Family=Hybrid')
        self.assertEqual(code, 2)
        self.assertEqual({k: result[k] for k in failure}, failure)
        self.assertEqual(result['registration']['status'], 'registered')
        registered_run.assert_called_once()
        args = registered_run.call_args.args[2]
        self.assertTrue(args.allow_physical)
        self.assertTrue(args.auto_repair_frame)
        self.assertFalse(args.background_only)
        self.assertEqual(args.set, ['Family=Hybrid'])
        self.assertEqual(registered_run.call_args.args[3], {'Family': 'Hybrid'})
        execution.assert_called_once()

    def test_plain_registration_stays_read_only_and_run_options_require_explicit_run(self):
        with self.workers() as (registration, execution, engine):
            code, result = self.cli()
        self.assertEqual((code, result['status'], result['input_dispatched']), (0, 'registered', False))
        execution.assert_not_called()
        engine.assert_called_once()
        self.assertTrue(engine.call_args.kwargs['read_only'])
        with self.workers() as (registration, execution, engine):
            code, result = self.cli('--set', 'Family=Hybrid')
        self.assertEqual((code, result['diagnostic']), (2, 'register_run_required'))
        registration.assert_not_called()
        execution.assert_not_called()

    def test_background_option_reaches_normal_accessibility_runner(self):
        with self.workers() as (registration, execution, engine):
            code, result = self.cli('--run', '--background-only', '--set', 'Family=Hybrid')
        self.assertEqual(code, 0)
        self.assertTrue(engine.call_args.kwargs['background_only'])
        self.assertIn('--background-only', execution.call_args.args[0])

    def test_summary_keeps_verified_output_and_success_exit_code(self):
        output = dict(kind='file', path=r'C:\Reports\example.xlsx')
        self.recipe['steps'].append(dict(action='verify-file', output=output))
        self.write_source()
        self.result['status'] = 'completed_unverified'
        self.result['steps'].append(dict(number=4, receipt=dict(status='verified', input_dispatched=False,
            file_verified=True, diagnostic='export_workbook_verified', verified_path=output['path'])))
        with self.workers():
            code, result = self.cli('--run', '--summary', '--set', 'Family=Hybrid')
        self.assertEqual((code, result['status'], result['outcome']),
                         (0, 'completed_unverified', 'workbook_verified'))
        self.assertEqual(result['verified_outputs'], [dict(number=4, path=output['path'])])
        self.assertEqual(result['registration']['status'], 'registered')

    def test_conditional_worker_keeps_repair_policy_and_variables_in_live_session(self):
        from test_conditional_action import ConditionalExecutionTests
        from test_python_context_workflow import context
        import scene_session
        self.recipe['steps'].extend([context(), deepcopy(ConditionalExecutionTests.step)])
        self.write_source()
        waiting = dict(status='needs_scene', input_dispatched=True, ticket='active-worker', steps=[])
        with self.workers() as (registration, execution, engine), patch.object(
                scene_session, 'start', return_value=waiting) as start:
            code, result = self.cli('--run', '--allow-physical', '--auto-repair-frame', '--set', 'Family=Hybrid')
        self.assertEqual((code, result['ticket']), (2, 'active-worker'))
        self.assertEqual(result['registration']['status'], 'registered')
        start.assert_called_once()
        session = start.call_args.args[2]['registered_session']
        self.assertEqual(session['set'], ['Family=Hybrid'])
        self.assertTrue(session['auto_repair_frame'])
        self.assertTrue(session['allow_physical'])
        self.assertFalse(session['background_only'])
        self.assertEqual(start.call_args.args[2]['overrides'], {'Family': 'Hybrid'})
        engine.assert_called_once()
        execution.assert_called_once()

    def test_task_registration_preserves_recording_and_uses_compiled_required_values(self):
        from test_recorded_plan import recording
        source_task = recording()
        compiled = recorded_plan.compile_plan(source_task)
        before = deepcopy(source_task)
        with (self.workers() as (registration, execution, engine),
              patch.object(flow.app.tasks, 'load_task', return_value=source_task),
              patch.object(recorded_plan, 'get_plan', return_value=compiled), redirect_stdout(StringIO()) as stdout):
            code = flow.main(['register', '--task', source_task['name'], '--out', str(self.destination),
                              '--run', '--set', 'Family=Hybrid'])
        result = json.loads(stdout.getvalue())
        self.assertEqual((code, result['registration']['status']), (0, 'registered'))
        self.assertEqual(json.loads((self.destination / 'source-task.json').read_text(encoding='utf8')), before)
        self.assertEqual(source_task, before)
        self.assertEqual(engine.call_args.args[1], {'Family': 'Hybrid'})
        registration.assert_not_called()
        execution.assert_called_once()
        engine.assert_called_once()
        self.assertFalse(engine.call_args.kwargs['read_only'])
        expected = deepcopy(compiled['envelope']['recipe'])
        for step in expected['steps']:
            if step['action'] == 'set-value' and step['selector']['role'] == 'combobox':
                step['allow_option_select'] = True
        self.assertEqual(flow.load(self.destination / 'workflow.json'), expected)

    def test_unresolved_draft_returns_the_step_and_direct_preparation_arguments(self):
        import review_drafts
        store = self.destination.parent / 'tasks'
        raw = dict(id='1', action='manual', label='unresolved', target={'kind': 'manual'},
                   recognition={'reason': 'capture_target_unavailable'})
        review_drafts.ReviewStorage('초안', 'A.exe', '업무', store).save_draft(
            [dict(step=raw, form=dict(alias='', text='나중에 필요한 내용', mode=None, argument=''))],
            'invalid_step_instruction', 1)
        with (patch.object(flow.app, 'task_directory', return_value=store),
              self.workers() as (registration, execution, engine), redirect_stdout(StringIO()) as stdout):
            code = flow.main(['register', '--task', '초안', '--out', str(self.destination)])
        result = json.loads(stdout.getvalue())
        self.assertEqual((code, result['status'], result['step']), (2, 'needs_grounding', 1))
        self.assertEqual(result['diagnostic'], 'target_grounding_required')
        self.assertTrue(result['grounding_steps'])
        self.assertEqual(result['recover_args'], ['recover', '--task', '초안', '--prepare'])
        self.assertFalse(result['input_dispatched'])
        self.assertFalse(self.destination.exists())
        registration.assert_not_called()
        execution.assert_not_called()
        engine.assert_not_called()

    def test_successful_trial_of_editable_draft_registers_offline_using_its_effective_name(self):
        import review_drafts
        from test_recorded_plan import combo
        store = self.destination.parent / 'tasks'
        raw = combo(label='Region', identifier='REG', sentence='입력받은 변수를 Region에 입력해줘')
        review_drafts.ReviewStorage('영역 초안', 'A.exe', '업무', store).save_draft(
            [dict(step=dict(raw, id='1', label='Region'), form=dict(alias='', text=raw['instruction']['text'],
                  mode=None, argument=''))], 'invalid_step_instruction', 1)
        source = store / '영역 초안.json'
        original = source.read_bytes()
        with (patch.object(flow.app, 'task_directory', return_value=store),
              patch.object(flow.app, 'worker_receipt', return_value=dict(
                  status='verified', input_dispatched=True, steps=[
                      dict(number=1, receipt=dict(status='verified', input_dispatched=True))])) as trial_worker,
              self.workers() as (registration, execution, engine)):
            tested = flow.app.run(flow.app.parser().parse_args(
                ['try-recording', '--task', '영역 초안', '--set', 'Region=North']))
            self.assertEqual(tested['status'], 'verified')
            with redirect_stdout(StringIO()) as stdout:
                code = flow.main(['register', '--task', tested['effective_task'], '--out', str(self.destination)])
            result = json.loads(stdout.getvalue())
            self.assertEqual((code, result['status']), (0, 'registered'), result)
            self.assertEqual(result['required_parameters'], ['Region'])
            self.assertEqual(result['target_validation'], 'recorded_evidence')
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(flow.load(self.destination / 'workflow.json')['steps'][0]['parameter'], 'Region')
            trial_worker.assert_called_once()
            registration.assert_not_called()
            execution.assert_not_called()
            engine.assert_not_called()


if __name__ == '__main__':
    unittest.main()
