"""Whole-plan background policy survives runtime, replay and worker boundaries."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_python_context_workflow as context_fixture
import test_recorded_plan as plan_fixture
import test_row_action as row_fixture

flow = context_fixture.flow
app = flow.app


def field(*, parameter=None):
    step = dict(action='set-value', selector=dict(automation_id='filter', role='textfield'))
    step.update(parameter=parameter) if parameter else step.update(value='value')
    return step


def submit():
    return dict(action='submit', selector=dict(automation_id='filter', role='textfield'))


def anchored():
    return dict(action='set-value', value='value', selector=dict(
        anchor=dict(name='Panel', role='group'), point=[.5, .3], role='textfield', class_name='Edit'))


class _Stdout(StringIO):
    def reconfigure(self, **kwargs):
        pass


class BackgroundRuntimeTests(unittest.TestCase):
    def fixture(self):
        fixture = context_fixture.CompositionTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        configure = patch.object(flow.input_policy, 'configure_uia', return_value=None)
        configure.start()
        self.addCleanup(configure.stop)
        return fixture

    def test_later_submit_anchor_or_menu_rejects_entire_plan_before_binding(self):
        for later in (submit(), anchored(), context_fixture.context()):
            with self.subTest(action=later['action'], anchored='anchor' in later.get('selector', {})), \
                    patch.object(flow, 'bind') as bind, \
                    patch.object(app.bridge, 'UIA') as uia, \
                    patch.object(app.visibility, 'restore_for_action') as restore, \
                    patch.object(flow.live, 'execute') as execute:
                result = flow.run(context_fixture.recipe(field(), later), background_only=True)
                self.assertEqual('background_requires_desktop_interaction', result['diagnostic'])
                self.assertEqual([2], result['blocked_steps'])
                self.assertEqual([], result['steps'])
                self.assertIs(result['input_dispatched'], False)
                self.assertIs(result['physical_input'], False)
                for operation in (bind, uia, restore, execute):
                    operation.assert_not_called()

    def test_initial_minimized_window_stops_before_uia_baseline_or_restore(self):
        fixture = self.fixture()
        with patch.object(app.bridge.user32, 'IsIconic', return_value=True) as minimized:
            result = flow.run(context_fixture.recipe(field(), context_fixture.output()), background_only=True)
        self.assertEqual('background_window_minimized', result['diagnostic'])
        self.assertEqual('blocked', result['status'])
        self.assertIs(result['input_dispatched'], False)
        self.assertEqual([], result['steps'])
        minimized.assert_called_once_with(fixture.owner.hwnd)
        app.bridge.UIA.assert_not_called()
        app.visibility.restore_for_action.assert_not_called()
        flow.export_evidence.baseline.assert_not_called()
        flow.live.execute.assert_not_called()

    def test_accessibility_path_skips_restore_and_checks_window_inside_dispatch_guard(self):
        fixture = self.fixture()
        observed_inside_dispatch = []
        with patch.object(app.bridge.user32, 'IsIconic', return_value=False) as minimized:
            def execute(*args, **kwargs):
                before = minimized.call_count
                receipt = fixture.execute(*args, **kwargs)
                observed_inside_dispatch.append(minimized.call_count > before)
                return dict(receipt, input_mode='accessibility', physical_input=False)
            flow.live.execute.side_effect = execute
            result = flow.run(context_fixture.recipe(field()), background_only=True)
        self.assertEqual('verified', result['status'])
        self.assertEqual('background_only', result['input_policy'])
        self.assertFalse(result['window_restored'])
        self.assertEqual([True], observed_inside_dispatch)
        self.assertGreaterEqual(minimized.call_count, 4)
        self.assertEqual([('fill', 100, 'value')], fixture.events)
        app.visibility.restore_for_action.assert_not_called()
        app.bridge.UIA.assert_called_once()
        flow.input_policy.configure_uia.assert_called_once_with(fixture.uia)
        self.assertFalse(result['steps'][0]['receipt']['physical_input'])

    def test_unavailable_focus_suppression_stops_before_restore_or_first_action(self):
        self.fixture()
        flow.input_policy.configure_uia.side_effect = app.bridge.BridgeError('background_autofocus_unavailable')
        with patch.object(app.bridge.user32, 'IsIconic', return_value=False):
            result = flow.run(context_fixture.recipe(field()), background_only=True)
        self.assertEqual('blocked', result['status'])
        self.assertEqual('background_autofocus_unavailable', result['diagnostic'])
        self.assertIs(result['input_dispatched'], False)
        self.assertEqual([], result['steps'])
        app.visibility.restore_for_action.assert_not_called()
        flow.live.execute.assert_not_called()

    def test_readonly_does_not_configure_client_even_with_background_flag(self):
        self.fixture()
        result = flow.run(context_fixture.recipe(field()), read_only=True, background_only=True)
        self.assertEqual('observed', result['status'])
        self.assertIs(result['input_dispatched'], False)
        flow.input_policy.configure_uia.assert_not_called()
        app.visibility.restore_for_action.assert_not_called()
        flow.live.execute.assert_not_called()

    def test_default_policy_preserves_restore_and_original_execution(self):
        fixture = self.fixture()
        result = flow.run(context_fixture.recipe(field()))
        self.assertEqual('auto', result['input_policy'])
        self.assertEqual('verified', result['status'])
        self.assertTrue(result['input_dispatched'])
        self.assertEqual(['restore', ('fill', 100, 'value')], fixture.events)
        app.visibility.restore_for_action.assert_called_once_with(fixture.owner, 'Example window')
        flow.input_policy.configure_uia.assert_not_called()

    def test_auto_context_menu_propagates_physical_mechanism_to_workflow(self):
        fixture = self.fixture()
        def menu(*args, **kwargs):
            return dict(fixture.menu(*args, **kwargs), input_mode='physical_pointer', physical_input=True)
        flow.context_menu.execute.side_effect = menu
        result = flow.run(context_fixture.recipe(context_fixture.context()))
        self.assertEqual('completed_unverified', result['status'])
        self.assertIs(result['physical_input'], True)
        self.assertIs(result['input_dispatched'], True)
        self.assertEqual('physical_pointer', result['steps'][0]['receipt']['input_mode'])

    def test_direct_command_background_policy_refuses_minimized_before_observing(self):
        owner = Mock(hwnd=100)
        args = app.parser().parse_args(['fill', '--exe', 'Example.exe', '--window', 'Example window',
                                       '--target', 'Filter', '--value', 'value', '--background-only'])
        with patch.object(app.bridge.user32, 'IsIconic', return_value=True), \
                patch.object(app.bridge, 'UIA') as uia, \
                patch.object(app.visibility, 'restore_for_action') as restore, \
                self.assertRaisesRegex(app.bridge.BridgeError, 'background_window_minimized'):
            app.action(args, owner)
        uia.assert_not_called()
        restore.assert_not_called()


class BackgroundCLITests(unittest.TestCase):
    def test_input_plan_ignores_missing_parameters_and_never_binds_or_spawns(self):
        data = context_fixture.recipe(field(parameter='Search'), submit())
        with patch.object(flow, 'load', return_value=data), patch.object(flow, 'bind') as bind, \
                patch.object(flow.subprocess, 'run') as child, \
                patch.object(flow, 'values_for') as values, redirect_stdout(StringIO()) as output:
            code = flow.main(['run', '--recipe', 'example.json', '--input-plan'])
        receipt = json.loads(output.getvalue())
        self.assertEqual(0, code)
        self.assertEqual('observed', receipt['status'])
        self.assertFalse(receipt['input_dispatched'])
        self.assertTrue(receipt['input_requirements']['physical_input_required'])
        for operation in (bind, child, values):
            operation.assert_not_called()

    def test_strict_mixed_cli_refuses_before_missing_parameters_and_subprocess(self):
        data = context_fixture.recipe(field(parameter='Search'), submit())
        with patch.object(flow, 'load', return_value=data), patch.object(flow.subprocess, 'run') as child, \
                patch.object(flow, 'values_for') as values, redirect_stdout(StringIO()) as output:
            code = flow.main(['run', '--recipe', 'example.json', '--background-only'])
        receipt = json.loads(output.getvalue())
        self.assertEqual(2, code)
        self.assertEqual([2], receipt['blocked_steps'])
        self.assertIs(receipt['input_dispatched'], False)
        child.assert_not_called()
        values.assert_not_called()

    def test_background_flag_reaches_python_subprocess_and_worker_runtime(self):
        data = context_fixture.recipe(field())
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(dict(status='verified', input_dispatched=True)))
        with patch.object(flow, 'load', return_value=data), \
                patch.object(flow.subprocess, 'run', return_value=completed) as child, redirect_stdout(StringIO()):
            self.assertEqual(0, flow.main(['run', '--recipe', 'example.json', '--background-only']))
        self.assertIn('--background-only', child.call_args.args[0])
        self.assertIn('--_worker', child.call_args.args[0])
        with patch.object(flow, 'load', return_value=data), \
                patch.object(flow, 'run', return_value=dict(status='verified', input_dispatched=True)) as run, \
                redirect_stdout(StringIO()):
            self.assertEqual(0, flow.main(['--_worker', 'run', '--recipe', 'example.json', '--background-only']))
        self.assertIs(run.call_args.kwargs['background_only'], True)


class BackgroundReplayTests(unittest.TestCase):
    def setUp(self):
        self.task = plan_fixture.recording()
        for target, options in (
            ('tasks.load_task', dict(return_value=self.task)),
            ('task_directory', dict(return_value=Path(__file__).resolve().parent)),
            ('bridge.set_dpi_awareness', dict(return_value=None)),
        ):
            patcher = patch('desktop_select.' + target, **options)
            patcher.start()
            self.addCleanup(patcher.stop)

    def args(self, *extra):
        return app.parser().parse_args(['replay', '--task', self.task['name'], '--exe', self.task['exe'],
                                       '--window', self.task['window'], *extra])

    def test_recorded_input_plan_needs_no_values_window_or_saved_plan_write(self):
        with patch.object(app, 'windows') as windows, patch.object(app, 'worker_receipt') as worker, \
                patch.object(app, 'saved_execution_plan') as save:
            result = app.run(self.args('--input-plan'))
        self.assertEqual('observed', result['status'])
        self.assertFalse(result['input_dispatched'])
        self.assertTrue(result['input_requirements']['background_eligible'])
        for operation in (windows, worker, save):
            operation.assert_not_called()

    def test_unknown_recorded_step_blocks_whole_run_before_window_or_worker(self):
        self.task['steps'].append(dict(action='manual', reason='capture_target_unavailable'))
        with patch.object(app, 'windows') as windows, patch.object(app, 'worker_receipt') as worker:
            result = app.run(self.args('--background-only', '--set', 'Family=Hybrid'))
        self.assertEqual('background_requires_desktop_interaction', result['diagnostic'])
        self.assertEqual([4], result['blocked_steps'])
        windows.assert_not_called()
        worker.assert_not_called()

    def test_legacy_row_aggregates_mechanism_without_inventing_delivery(self):
        fixture = row_fixture.RowActionTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        task = app.tasks.from_recording('Row task', 'Example.exe', 'Device window', [
            dict(action='row-action', label='Start', row=fixture.spec)])
        receipt = fixture.execute(row_fixture.sample('after'), row_fixture.sample('after'))
        invoke = Mock(return_value=receipt)
        report = app.tasks.replay(task, task['exe'], task['window'], {}, invoke)
        self.assertIs(report['physical_input'], True)
        self.assertIs(report['input_dispatched'], False)
        self.assertEqual('verified', report['status'])
        fixture.send.assert_not_called()

    def test_only_scopes_profile_and_sends_background_policy_to_step_worker(self):
        self.task['steps'].append(dict(action='manual', reason='capture_target_unavailable'))
        binding = dict(hwnd=100, pid=11, title=self.task['window'])
        receipt = dict(status='verified', input_dispatched=True, effect_verification='matched',
                       input_mode='accessibility', physical_input=False)
        with patch.object(app, 'windows', return_value=[]), patch.object(app, 'pick_window', return_value=binding), \
                patch.object(app, 'worker_receipt', return_value=receipt) as worker:
            result = app.run(self.args('--background-only', '--only', '1'))
        self.assertEqual('verified', result['status'])
        self.assertEqual([1], [row['number'] for row in result['input_requirements']['steps']])
        self.assertEqual(['_step'], worker.call_args.args[0])
        self.assertIs(worker.call_args.kwargs['payload']['background_only'], True)
        self.assertEqual('N', worker.call_args.kwargs['payload']['step']['value'])

    def test_only_input_plan_preserves_selected_source_number_without_values(self):
        self.task['steps'][1]['alias'] = 'Family field'
        with patch.object(app, 'windows') as windows, patch.object(app, 'worker_receipt') as worker:
            result = app.run(self.args('--input-plan', '--only', 'Family field'))
        self.assertEqual([2], [row['number'] for row in result['input_requirements']['steps']])
        windows.assert_not_called()
        worker.assert_not_called()

    def test_fast_replay_carries_policy_into_plan_worker_payload(self):
        saved = plan_fixture.plans.compile_plan(self.task)
        with patch.object(app, 'saved_execution_plan', return_value=saved), \
                patch.object(app, 'worker_receipt', return_value=dict(status='verified', input_dispatched=True)) as worker:
            result = app.run(self.args('--background-only', '--set', 'Family=Hybrid'))
        self.assertEqual('saved_python_plan', result['execution_engine'])
        self.assertEqual(['_plan'], worker.call_args.args[0])
        self.assertIs(worker.call_args.kwargs['payload']['background_only'], True)
        self.assertEqual({'Family': 'Hybrid'}, worker.call_args.kwargs['payload']['overrides'])

    def test_step_worker_refuses_pointer_action_before_binding(self):
        request = dict(binding={}, step=dict(action='row-action'), background_only=True)
        with patch('sys.stdin', StringIO(json.dumps(request))), patch.object(app.bridge, 'Binding') as bind, \
                patch.object(app, 'action') as action:
            result = app.replay_step()
        self.assertEqual('background_requires_desktop_interaction', result['diagnostic'])
        self.assertFalse(result['input_dispatched'])
        bind.assert_not_called()
        action.assert_not_called()

    def test_step_worker_passes_policy_to_accessibility_action(self):
        request = dict(binding={'title': self.task['window']}, step=deepcopy(self.task['steps'][0]),
                       background_only=True)
        with patch('sys.stdin', StringIO(json.dumps(request))), patch.object(app.bridge, 'Binding'), \
                patch.object(app, 'action', return_value={'status': 'verified'}) as action:
            app.replay_step()
        self.assertIs(action.call_args.args[0].background_only, True)

    def test_plan_worker_passes_background_policy_and_recorded_selection(self):
        request = dict(recipe=context_fixture.recipe(field()), overrides={}, hwnd=100, background_only=True)
        with patch('sys.stdin', StringIO(json.dumps(request))), patch('sys.argv', ['desktop_select.py', '_plan']), \
                patch('sys.stdout', _Stdout()), \
                patch.object(flow, 'run', return_value=dict(status='verified', input_dispatched=True)) as run:
            self.assertEqual(0, app.main())
        self.assertIs(run.call_args.kwargs['background_only'], True)
        self.assertIs(run.call_args.kwargs['allow_recorded_selection'], True)
        self.assertEqual(100, run.call_args.kwargs['hwnd'])


if __name__ == '__main__':
    unittest.main()
