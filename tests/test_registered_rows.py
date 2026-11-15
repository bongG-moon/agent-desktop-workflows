"""Recorded and spoken row actions must survive export into a standalone Python skill."""
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import tempfile
from unittest import TestCase, mock

from test_ordinary_row_capture import sample
from test_recorded_plan import recording
import python_workflow as workflow
import learned_tasks as tasks
import recorded_plan
import row_capture


class RegisteredRowTests(TestCase):
    def setUp(self):
        base = [('a', 'stopped', 'start'), ('b', 'stopped', 'start')]
        click = row_capture.prepare(sample(base, 1), [157, 205], 1.2)
        self.spec = row_capture.infer(click, sample([('a', 'running', 'stop'), base[1]], 2))
        self.task = recording()
        self.task['steps'] = [
            dict(action='row-action', row=self.spec, label='녹화한 행 실행', alias='녹화'),
            dict(action='row-launch', ordinal=2, label='두 번째 요소 실행')]

    def runtime(self):
        patches = ExitStack()
        self.addCleanup(patches.close)
        for target in ('bind', 'app.guard', 'app.visibility.restore_for_action', 'app.bridge.UIA', 'app.task_directory'):
            patches.enter_context(mock.patch('python_workflow.' + target))

    def compile(self):
        return recorded_plan.compile_plan(self.task)['envelope']['recipe']

    def test_legacy_spoken_step_preserves_source_and_both_rows_register_without_clicks(self):
        self.task['steps'][1] = dict(action='manual', reason='stable_locator_unavailable', alias='두 번째 요소 실행')
        source = deepcopy(self.task)
        result = recorded_plan.compile_plan(source)['envelope']
        self.assertEqual(result['recipe']['steps'][1]['ordinal'], 2)
        self.assertEqual(result['source'], self.task)
        self.assertEqual(result['source_steps'], [1, 2])
        self.runtime()
        with (tempfile.TemporaryDirectory() as folder,
              mock.patch.object(workflow.row_vision, 'observe_row', return_value={'kind': 'after'}),
              mock.patch.object(workflow.row_intent, 'execute', return_value=dict(status='resolved', input_dispatched=False)) as launch,
              mock.patch.object(workflow.row_action, 'execute') as click):
            dest = Path(folder) / 'launch-instances'
            registered = workflow.register(result['recipe'], dest, source_task=source)
            self.assertEqual(registered['status'], 'registered')
            self.assertEqual(workflow.load(dest / 'workflow.json'), result['recipe'])
            self.assertEqual(json.loads((dest / 'source-task.json').read_text(encoding='utf8')), source)
            self.assertIn('import python_workflow', (dest / 'scripts/run.py').read_text(encoding='utf8'))
            launch.assert_called_once()
            self.assertIs(launch.call_args.kwargs['preview'], True)
            click.assert_not_called()

    def test_unverified_row_stops_the_sequence_and_is_never_retried(self):
        self.runtime()
        recipe = self.compile()
        uncertain = dict(status='delivered_unverified', input_dispatched=True, diagnostic='row_state_not_confirmed')
        with (mock.patch.object(workflow.row_action, 'execute', return_value=uncertain) as click,
              mock.patch.object(workflow.row_intent, 'execute') as launch):
            result = workflow.run(recipe)
        self.assertEqual((result['status'], result['stopped_at'], result['input_dispatched']), ('delivered_unverified', 1, True))
        click.assert_called_once()
        launch.assert_not_called()

    def test_mixed_plan_filters_required_inputs_for_only_and_blocks_background_before_binding(self):
        first = recording()['steps'][1]
        self.task['steps'].insert(0, first)
        recipe = self.compile()
        self.assertEqual([s['action'] for s in recipe['steps']], ['set-value', 'row-action', 'row-launch'])
        with mock.patch.object(workflow, 'bind') as bind:
            denied = workflow.run(recipe, {'Family': 'Hybrid'}, background_only=True)
            self.assertEqual(denied['blocked_steps'], [2, 3])
            bind.assert_not_called()
        self.runtime()
        with mock.patch.object(workflow.row_intent, 'execute', return_value=dict(status='verified', input_dispatched=False)) as run:
            result = workflow.run(recipe, only=3)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual([s['number'] for s in result['steps']], [3])
        self.assertIs(run.call_args.kwargs['preview'], False)
        self.assertEqual(workflow.worker_timeout(recipe), 330)

    def test_input_plan_never_contacts_application_and_validates_even_unselected_steps(self):
        recipe = self.compile()
        with (mock.patch.object(workflow, 'load', return_value=recipe),
              mock.patch.object(workflow.subprocess, 'run') as run, redirect_stdout(StringIO()) as out):
            code = workflow.main(['run', '--recipe', 'unused', '--input-plan', '--only', '녹화'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())['input_requirements']['steps'][0]['input_mode'], 'physical_pointer')
        run.assert_not_called()
        recipe['steps'][1]['ordinal'] = 1
        with self.assertRaisesRegex(workflow.WorkflowError, 'invalid_row_step'):
            workflow.run(recipe, only=1)

    def test_explicit_new_ordinal_keeps_original_visual_hint_private_and_does_not_pin_original_row(self):
        original = self.task['steps'][0]
        utterance = '두 번째 요소 실행'
        action = tasks.compile_instruction(original, utterance)
        interpreted = tasks.instructed_step(original, action)
        self.task['steps'] = [interpreted]
        self.assertEqual(interpreted['ordinal'], 2)
        self.assertEqual(interpreted['row_hint'], original['row'])
        self.assertIsNot(interpreted['row_hint'], original['row'])
        self.assertNotIn('row_hint', tasks.summary(self.task)['steps'][0])
        recipe = self.compile()
        self.assertEqual(recipe['steps'][0]['row_hint'], self.spec)
        self.runtime()
        with mock.patch.object(workflow.row_intent, 'execute', return_value=dict(status='resolved', input_dispatched=False)) as run:
            result = workflow.run(recipe, read_only=True)
        self.assertEqual(result['status'], 'observed')
        self.assertEqual(run.call_args.args[2], 2)
        self.assertEqual(run.call_args.kwargs['hint'], self.spec)
        recipe['steps'][0]['row_hint'] = {'untrusted': 'mask'}
        with self.assertRaises(workflow.app.bridge.BridgeError):
            workflow.validate(recipe)

    def test_cached_identity_that_already_moved_from_requested_ordinal_cannot_dispatch(self):
        first, second = [0, 0, 200, 40], [0, 40, 200, 40]
        with (mock.patch.object(workflow.row_vision, 'observe_row',
                                return_value=dict(kind='before', row_bounds=second, point=[180, 60])),
              mock.patch.object(workflow.row_action, 'foreground') as foreground,
              mock.patch.object(workflow.app.bridge, '_send_point') as send):
            result = workflow.row_action.execute(None, self.spec, lambda: None, required_bounds=first)
        self.assertEqual((result['status'], result['diagnostic'], result['input_dispatched']),
                         ('blocked', 'row_order_changed', False))
        foreground.assert_not_called()
        send.assert_not_called()
