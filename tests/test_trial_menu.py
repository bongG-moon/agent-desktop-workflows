"""Public first-trial menu repair keeps one query and persists only proven points."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select as app
import learned_tasks as tasks
import python_workflow as flow
import recorded_plan
import trial_menu
import workflow_repair as repair
from test_recorded_plan import recording
from test_python_context_workflow import context
import test_python_context_workflow as fixture


class MenuTrialTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.ui = fixture.CompositionTests()
        self.ui.setUp()
        self.addCleanup(self.ui.doCleanups)
        self.ui.owner.created = 1000
        self.ui.element.property.return_value = '조회'
        self.source = recording()
        self.source.update(name='Menu trial', window='Example window')
        self.source['steps'] = [self.source['steps'][-1]]
        self.source['steps'][0]['wait_after'] = 0.001
        self.spec = context()['spec']
        self.source['steps'].append(dict(action='result-menu', label='Export',
            context=dict(trigger=None, ready=None, output=None, menu=deepcopy(self.spec))))
        self.before, self.after = self.spec['point'], [.65, .45]
        self.path = tasks.save_task(self.source, self.folder)
        self.original = (self.folder / (self.source['name'] + '.json')).read_bytes()
        self.worker = Mock(side_effect=self.dispatch)
        self.menu_calls = []
        for target, options in [
                ('task_directory', dict(return_value=self.folder)),
                ('worker_receipt', dict(side_effect=self.worker))]:
            p = patch.object(app, target, **options)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(repair.tempfile, 'gettempdir', return_value=str(self.folder / 'temp'))
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(flow.result_menu, 'execute', side_effect=self.menu)
        p.start()
        self.addCleanup(p.stop)

    def cli(self, *argv):
        return app.run(app.parser().parse_args(list(argv)))

    def trial(self, *extra):
        return self.cli('try-recording', '--task', self.source['name'], '--allow-physical', *extra)

    def dispatch(self, argv, *, payload, **kwargs):
        self.assertEqual(argv, ['_plan'])
        if 'menu_trial' in payload:
            return trial_menu.execute(app, payload)
        return flow.run(payload['recipe'], payload['overrides'])

    def menu(self, binding, spec, guard, title, **kwargs):
        guard()
        self.menu_calls.append(deepcopy(spec))
        if spec['menu']['point'] == self.before:
            return dict(status='blocked', diagnostic='context_point_outside_region', input_dispatched=False,
                        leaf_dispatched=False, menu_items=[], menu_path_completed=False)
        return dict(status='delivered_unverified', diagnostic='menu_delivered_no_output_condition',
                    input_dispatched=True, leaf_dispatched=True, menu_path_completed=True,
                    menu_items=spec['menu']['path'])

    def adjust(self, flow, recipe, record, frame=None, point=None):
        if frame is None:
            return dict(status='observed', input_dispatched=False, image='local.png', frame='frame-one',
                        resume_args=['--resume-menu', record['ticket'], '--frame', 'frame-one'])
        return dict(self.spec, point=self.after)

    def test_failure_inspection_resume_then_cached_success_and_register_without_requery(self):
        with patch.object(repair, 'inspect_or_adjust', side_effect=self.adjust) as image:
            first = self.trial()
            self.assertEqual(first['diagnostic'], 'context_point_outside_region')
            self.assertEqual(first['host_action'], 'inspect_current_menu_without_replaying')
            self.worker.assert_called_once()
            image.assert_not_called()
            key = first['trial_receipt']
            observed = self.cli(*first['menu_repair']['frame_args'])
            self.assertEqual(observed['resume_args'][:5], ['resume-menu-trial', '--receipt', key, '--frame', 'frame-one'])
            resumed = self.cli('resume-menu-trial', '--receipt', key, '--frame', 'frame-one', '--point', '20', '30',
                               '--allow-physical')
            self.assertEqual(resumed['status'], 'delivered_unverified', resumed)
            self.assertEqual(resumed['resumed_at'], 3)  # Explicit wait remains original step 1.
            self.assertTrue(resumed['correction_saved'])
            self.assertFalse(resumed['prior_steps_replayed'])
            self.assertEqual(flow.live.execute.call_count, 1)
            self.assertEqual(len(self.menu_calls), 2)
            latest = self.trial()
            self.assertEqual(latest['effective_task'], resumed['effective_task'])
            self.assertTrue(latest['previous_trial'])
            self.assertEqual(self.worker.call_count, 3)
            self.assertEqual(flow.live.execute.call_count, 1)
            copy = tasks.load_task(resumed['effective_task'], self.folder)
            self.assertEqual(copy['steps'][1]['context']['menu']['point'], self.after)
            plan = recorded_plan.get_plan(copy, self.folder)
            self.assertEqual(plan['envelope']['recipe']['steps'][2]['context']['menu']['point'], self.after)
            with patch.object(flow, 'run', side_effect=AssertionError('duplicate preflight')):
                registered = flow.register(plan['envelope']['recipe'], self.folder / 'export-skill', source_task=copy)
            self.assertEqual(registered['status'], 'registered')
        self.assertEqual((self.folder / (self.source['name'] + '.json')).read_bytes(), self.original)

    def test_auto_frame_only_after_failed_menu_and_never_without_explicit_option(self):
        with patch.object(repair, 'inspect_or_adjust', side_effect=self.adjust) as image:
            first = self.trial('--auto-repair-frame')
            image.assert_called_once()
            self.assertEqual(first['menu_repair']['frame'], 'frame-one')
            self.assertEqual(first['menu_repair']['resume_args'][0], 'resume-menu-trial')
            self.worker.assert_called_once()

    def test_worker_desktop_lock_before_input_reuses_untouched_preparation(self):
        import input_policy
        refusal = dict(status='blocked', diagnostic='no_interactive_desktop', input_dispatched=False)
        with patch.object(input_policy, 'interactive_desktop_blocked', side_effect=[None, refusal, None, None]):
            first = self.trial()
            self.assertEqual(first['diagnostic'], 'no_interactive_desktop')
            self.assertFalse(first['input_dispatched'])
            flow.live.execute.assert_not_called()
            second = self.trial()
        self.assertEqual(second['diagnostic'], 'context_point_outside_region')
        self.assertIn('menu_repair', second)
        self.assertEqual(self.worker.call_count, 2)
        self.assertEqual(flow.live.execute.call_count, 1)
        self.assertEqual(len(self.menu_calls), 1)
        self.assertEqual((self.folder / (self.source['name'] + '.json')).read_bytes(), self.original)

    def test_preparation_reuse_rejects_changed_state_or_outstanding_attempt(self):
        import recovery
        envelope = recorded_plan.compile_plan(self.source)['envelope']
        for kind in ('source', 'workflow', 'attempt', 'pending'):
            with self.subTest(kind=kind):
                key = recovery._fingerprint(['retry-guard', kind])
                trial_menu.prepare(app, key, self.source, self.source['name'], envelope)
                folder = trial_menu._folder(app, key)
                if kind == 'source':
                    state = trial_menu._read(folder / 'source.json')
                    state['effective_task'] = 'different'
                    trial_menu._write(folder / 'source.json', state)
                elif kind == 'workflow':
                    changed = deepcopy(envelope['recipe'])
                    changed['steps'][-1]['context']['menu']['point'] = self.after
                    trial_menu._write(folder / 'workflow.json', changed)
                elif kind == 'attempt':
                    trial_menu._write(folder / 'last-attempt.json', 'a' * 64)
                else:
                    trial_menu._write(repair.RepairSession(folder / 'workflow.json').pending, {})
                with self.assertRaisesRegex(tasks.TaskError, 'prepared_recording_changed'):
                    trial_menu.prepare(app, key, self.source, self.source['name'], envelope)

    def test_uncertain_or_partially_sent_menu_never_gets_a_repair_ticket(self):
        with patch.object(flow.result_menu, 'execute', return_value=dict(status='uncertain',
                diagnostic='menu_delivery_unknown', input_dispatched=None, leaf_dispatched=None)), patch.object(
                repair, 'inspect_or_adjust') as image:
            first = self.trial('--auto-repair-frame')
            self.assertNotIn('menu_repair', first)
            self.assertEqual(first['host_action'], 'check_current_effect_before_continuing')
            again = self.trial()
            self.assertTrue(again['previous_trial'])
            self.worker.assert_called_once()
            image.assert_not_called()

    def test_changed_source_or_parameters_cannot_resume(self):
        first = self.trial()
        with self.assertRaisesRegex(tasks.TaskError, 'menu_repair_parameters_changed'):
            self.cli('resume-menu-trial', '--receipt', first['trial_receipt'], '--allow-physical', '--set', 'Other=A')
        self.assertEqual(self.worker.call_count, 1)
        path = self.folder / (self.source['name'] + '.json')
        changed = deepcopy(self.source)
        changed['window'] = 'Other window'
        trial_menu._write(path, changed)
        with self.assertRaisesRegex(tasks.TaskError, 'recording_draft_changed'):
            self.cli('inspect-menu-trial', '--receipt', first['trial_receipt'], '--allow-physical')
        self.assertEqual(self.worker.call_count, 1)

    def test_capture_needs_explicit_permission_and_success_does_not_capture(self):
        with patch.object(repair, 'inspect_or_adjust') as image:
            first = self.trial()
            denied = self.cli('inspect-menu-trial', '--receipt', first['trial_receipt'])
            self.assertEqual(denied['status'], 'needs_consent')
            self.worker.assert_called_once()
            image.assert_not_called()
        # A real automatic correction receipt is the only proof persisted.
        success = dict(status='verified', input_dispatched=True, leaf_dispatched=True,
            menu_path_completed=True, menu_items=self.spec['path'],
            point_correction=dict(**{'from': self.before, 'to': self.after}))
        with patch.object(flow.result_menu, 'execute', return_value=success), patch.object(
                repair, 'inspect_or_adjust') as image:
            done = self.trial('--trial-id', 'new-explicit-test', '--auto-repair-frame')
            self.assertTrue(done['correction_saved'])
            self.assertNotEqual(done['effective_task'], self.source['name'])
            image.assert_not_called()

    def test_interruption_after_resume_never_replays_prior_query_or_menu(self):
        first = self.trial()
        saved_dispatch = self.worker.side_effect
        def interrupted(*args, **kwargs):
            saved_dispatch(*args, **kwargs)
            raise KeyboardInterrupt()
        self.worker.side_effect = interrupted
        options = ['resume-menu-trial', '--receipt', first['trial_receipt'], '--frame', 'frame-one',
                   '--point', '20', '30', '--allow-physical']
        with patch.object(repair, 'inspect_or_adjust', side_effect=self.adjust):
            with self.assertRaises(KeyboardInterrupt):
                self.cli(*options)
            again = self.cli(*options)
            self.assertEqual(again['status'], 'uncertain')
            self.assertIsNone(again['input_dispatched'])
            self.assertTrue(again['previous_trial'])
            self.assertEqual(self.worker.call_count, 2)
            self.assertEqual(flow.live.execute.call_count, 1)
            self.assertEqual(len(self.menu_calls), 2)

    def test_input_pause_resumes_without_capture_and_preserves_embedded_query_once(self):
        source = deepcopy(self.source)
        source['steps'] = [source['steps'][1]]
        source['steps'][0]['context'].update(trigger=dict(name='Refresh', role='button'),
            ready=dict(kind='text', target=dict(name='Status', role='statictext'), expected='Done'))
        trial_menu._write(self.folder / (source['name'] + '.json'), source)
        pause = dict(status='blocked', diagnostic='foreground_or_input_changed', input_dispatched=True,
                     query_dispatched=True, menu_input_dispatched=False, leaf_dispatched=False, menu_items=[])
        done = dict(status='delivered_unverified', diagnostic='menu_delivered_no_output_condition',
                    input_dispatched=True, leaf_dispatched=True, menu_path_completed=True, menu_items=self.spec['path'])
        with patch.object(flow.result_menu, 'execute', side_effect=[pause, done]) as menu, patch.object(
                repair, 'inspect_or_adjust') as image:
            first = self.trial()
            self.assertEqual(first['host_action'], 'wait_for_user_idle_then_resume')
            resumed = self.cli(*first['input_pause']['resume_args'])
            self.assertFalse(resumed['prior_steps_replayed'])
            self.assertIsNone(menu.call_args.args[1]['trigger'])
            self.assertEqual(menu.call_count, 2)
            image.assert_not_called()
            self.assertEqual(self.worker.call_count, 2)

    def test_multiple_menu_corrections_accumulate_and_keep_original_window_dependency(self):
        import recovery
        # Compile the source with an inserted wait and a later child scope.
        source = deepcopy(self.source)
        source['steps'].append(deepcopy(source['steps'][1]))
        source['steps'].append(dict(action='read-value', label='Comment',
            selector=dict(automation_id='comment', role='textfield'),
            window_context=dict(exe=source['exe'], title='Detail', after_step=3, related=True)))
        envelope = recorded_plan.compile_plan(source)['envelope']
        key = recovery._fingerprint(['multiple', source])
        trial_menu.prepare(app, key, self.source, self.source['name'], envelope)
        folder, state, recipe = trial_menu._state(app, key)
        changed = deepcopy(recipe)
        changed['steps'][2]['context']['menu']['point'] = [.62, .42]
        changed['steps'][3]['context']['menu']['point'] = [.68, .48]
        trial_menu._write(folder / 'workflow.json', changed)
        report = {}
        trial_menu._save_correction(app, key, folder, state, report)
        copy = tasks.load_task(report['effective_task'], self.folder)
        self.assertEqual(copy['steps'][1]['context']['menu']['point'], [.62, .42])
        self.assertEqual(copy['steps'][2]['context']['menu']['point'], [.68, .48])
        self.assertEqual(copy['steps'][3]['window_context']['after_step'], 3)
        compiled = recorded_plan.compile_plan(copy)['envelope']['recipe']
        self.assertEqual(compiled['steps'][4]['window_context']['after_step'], 4)


if __name__ == '__main__':
    unittest.main()
