"""Exercise registered-run consent, single-use continuation and the literal saved recipe."""
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import os
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_python_context_workflow import context, output, recipe
import test_python_context_workflow as fixture

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import python_workflow as flow
import workflow_repair as repair


class RegisteredRepairTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)
        (folder / 'scripts').mkdir()
        (folder / 'scripts/run.py').write_text(flow.RUNNER.replace('RUNTIME_PARENT', '2'), encoding='utf8')
        sandbox = patch.object(repair.tempfile, 'gettempdir', return_value=str(folder))
        sandbox.start()
        self.addCleanup(sandbox.stop)
        self.recipe = recipe(dict(action='invoke', label='Query', selector=dict(role='button', name='Query')), context())
        self.path = folder / 'workflow.json'
        self.write(self.recipe)
        self.original = folder / 'source-task.json'
        self.original.write_bytes(b'original recording')
        self.ui = fixture.CompositionTests()
        self.ui.setUp()
        self.addCleanup(self.ui.doCleanups)
        self.ui.owner.created = 4545
        self.before = [0.4, 0.4]
        self.after = [0.63, 0.45]

    def write(self, value):
        self.path.write_text(json.dumps(value), encoding='utf8')

    def test_saved_menu_scope_accepts_handle_alias_only_for_the_original_lifetime(self):
        hwnd = 0x81560002
        self.ui.owner.hwnd = hwnd
        record = dict(number=2, scope=[
            [hwnd - 2**32, self.ui.owner.pid, self.ui.owner.created],
            [0xffffffff00000000 | hwnd, self.ui.owner.pid, self.ui.owner.created],
        ])
        bound, guard = repair.recorded_surface(flow, self.recipe, record)
        self.assertIs(bound, self.ui.owner)
        guard()
        record['scope'][1][2] += 1
        with self.assertRaisesRegex(flow.app.tasks.TaskError, 'menu_repair_window_changed'):
            repair.recorded_surface(flow, self.recipe, record)

    def cli(self, *args):
        with redirect_stdout(StringIO()) as stdout:
            flow.main(['--_worker', 'run', '--recipe', str(self.path), *args])
        return json.loads(stdout.getvalue())

    def menu(self, binding, spec, guard):
        guard()
        self.ui.events.append('menu')
        if spec['point'] == self.before:
            return dict(status='blocked', diagnostic='context_point_outside_region', input_dispatched=False,
                        leaf_dispatched=False, menu_path_completed=False, menu_items=[])
        return dict(status='delivered_unverified', diagnostic='menu_leaf_sent', input_dispatched=True,
                    leaf_dispatched=True, menu_path_completed=True, menu_items=spec['path'])

    def test_opted_in_repaired_test_enables_only_same_full_workflow_then_can_forget(self):
        self.recipe['steps'].append(output())
        self.write(self.recipe)
        flow.context_menu.execute.side_effect = self.menu
        with patch.dict(os.environ, LOCALAPPDATA=str(self.path.parent / 'profile')):
            self.assertEqual(self.cli('--remember-physical')['input_dispatched'], False)
            first = self.cli('--allow-physical', '--remember-physical')
            self.assertFalse(repair.approved(self.path, self.recipe))
            adjusted = dict(self.recipe['steps'][1]['spec'], point=self.after)
            with patch.object(repair, 'inspect_or_adjust', return_value=adjusted):
                resumed = self.cli('--resume-menu', first['menu_repair']['ticket'], '--allow-physical',
                                   '--frame', 'probe', '--point', '30', '40')
            self.assertEqual(resumed['repeat_physical_input'], 'approved')
            self.assertFalse(repair.approved(self.path, self.recipe))
            current = flow.load(self.path)
            self.assertTrue(repair.approved(self.path, current))
            self.assertEqual(self.cli()['steps'][-1]['receipt']['status'], 'verified')
            self.assertEqual(self.cli('--background-only')['diagnostic'], 'background_requires_desktop_interaction')
            for extra in [('--only', '2'), ('--auto-repair-frame',), ('--repair-frame', 'pretend')]:
                self.assertEqual(self.cli(*extra)['status'], 'needs_consent')
            edited = deepcopy(current)
            edited['steps'][1]['spec']['path'] = ['Different command']
            self.write(edited)
            self.assertEqual(self.cli()['status'], 'needs_consent')
            self.write(current)
            self.assertEqual(self.cli('--forget-physical')['status'], 'forgotten')
            self.assertEqual(self.cli()['status'], 'needs_consent')

    def test_interference_after_query_continues_at_unsent_menu_without_screen_or_requery(self):
        self.recipe['steps'].append(output())
        self.write(self.recipe)
        normal = dict(status='delivered_unverified', diagnostic='menu_leaf_sent', input_dispatched=True,
                      leaf_dispatched=True, menu_path_completed=True, menu_items=self.recipe['steps'][1]['spec']['path'])
        paused = dict(status='blocked', diagnostic='foreground_or_input_changed', input_dispatched=False,
                      leaf_dispatched=False, menu_items=[])
        flow.context_menu.execute.side_effect = [paused, normal]
        first = self.cli('--allow-physical')
        pause = first['input_pause']
        self.assertTrue(first['input_dispatched'])
        self.assertEqual(pause['host_action'], 'wait_for_user_idle_then_resume')
        self.assertNotIn('menu_repair', first)
        with patch.object(repair, 'inspect_or_adjust') as inspect:
            result = self.cli(*pause['resume_args'])
            self.assertEqual(result['resumed_at'], 2)
            self.assertEqual(result['steps'][-1]['receipt']['status'], 'verified')
            self.assertEqual(self.cli(*pause['resume_args'])['diagnostic'], 'menu_repair_expired')
            inspect.assert_not_called()
        self.assertEqual(flow.context_menu.execute.call_count, 2)
        flow.live.execute.assert_called_once()
        self.assertEqual(self.cli()['status'], 'needs_consent')

    def test_activity_after_a_right_click_cannot_replay_the_menu_or_enroll_permission(self):
        partial = dict(status='blocked', diagnostic='foreground_or_input_changed', input_dispatched=True,
                       leaf_dispatched=False, menu_items=[], menu_path_completed=False)
        flow.context_menu.execute.side_effect = [partial]
        with patch.dict(os.environ, LOCALAPPDATA=str(self.path.parent / 'profile')):
            result = self.cli('--allow-physical', '--remember-physical')
            self.assertEqual(result['input_pause'], dict(host_action='check_current_effect_before_continuing'))
            self.assertNotIn('repeat_physical_input', result)
            self.assertEqual(self.cli()['status'], 'needs_consent')

    def test_forget_between_failure_and_resume_cancels_pending_repeat_permission(self):
        flow.context_menu.execute.side_effect = self.menu
        with patch.dict(os.environ, LOCALAPPDATA=str(self.path.parent / 'profile')):
            failure = self.cli('--allow-physical', '--remember-physical')
            self.assertEqual(self.cli('--forget-physical')['status'], 'forgotten')
            adjusted = dict(self.recipe['steps'][1]['spec'], point=self.after)
            with patch.object(repair, 'inspect_or_adjust', return_value=adjusted):
                resumed = self.cli('--resume-menu', failure['menu_repair']['ticket'], '--allow-physical',
                                   '--frame', 'fresh', '--point', '4', '9')
            self.assertEqual(resumed['steps'][-1]['receipt']['status'], 'delivered_unverified')
            self.assertNotIn('repeat_physical_input', resumed)
            self.assertEqual(self.cli()['status'], 'needs_consent')

    def test_input_interference_in_a_resumed_tail_never_suggests_replaying_prior_query(self):
        pause = dict(status='blocked', input_dispatched=False, steps=[], resumed_at=2,
                     diagnostic='foreground_or_input_changed')
        with patch.object(repair, 'run_registered', return_value=pause):
            result = self.cli('--resume-input', 'ticket')
        self.assertEqual(result['input_pause']['host_action'], 'check_current_effect_before_continuing')

    def test_before_click_failure_resumes_without_duplicate_query_and_updates_next_run(self):
        self.recipe['steps'].append(output())
        self.write(self.recipe)
        flow.context_menu.execute.side_effect = self.menu
        self.assertEqual(self.cli()['status'], 'needs_consent')
        flow.bind.assert_not_called()
        failure = self.cli('--allow-physical')
        ticket = failure['menu_repair']['ticket']
        self.assertEqual(failure['stopped_at'], 2)
        self.assertEqual(failure['readability_check']['count'], 1)
        revised = dict(self.recipe['steps'][1]['spec'], point=self.after)
        with patch.object(repair, 'inspect_or_adjust', return_value=revised) as adjust:
            resume = ['--resume-menu', ticket, '--frame', 'probe', '--point', '30', '40']
            self.assertEqual(self.cli(*resume)['status'], 'needs_consent')
            adjust.assert_not_called()
            result = self.cli(*resume, '--allow-physical')
            self.assertEqual(result['resumed_at'], 2)
            self.assertEqual([r['number'] for r in result['steps']], [2, 3])
            self.assertEqual(result['steps'][-1]['receipt']['baseline_scope'], 'before_resumed_menu')
            self.assertTrue(result['steps'][-1]['receipt']['file_verified'])
            self.assertEqual(result['menu_corrections'], [2])
            self.assertEqual(self.cli(*resume, '--allow-physical')['diagnostic'], 'menu_repair_expired')
            self.assertEqual(adjust.call_count, 1)
        flow.live.execute.assert_called_once()
        self.assertEqual(flow.load(self.path)['steps'][1]['spec']['point'], self.after)
        self.assertEqual(self.original.read_bytes(), b'original recording')
        again = self.cli('--allow-physical')
        self.assertEqual(again['steps'][1]['receipt']['diagnostic'], 'menu_leaf_sent')
        self.assertNotIn('menu_corrections', again)

    def test_registration_of_resolved_targets_uses_targeted_checks_without_another_window_inventory(self):
        group = self.recipe['steps'][1]['spec']['target']
        with (patch.object(flow.app, 'observe', side_effect=AssertionError('full inventory')),
              patch.object(flow.context_menu, 'observe', return_value=dict(
                  status='observed', input_dispatched=False, region_observed=True, point_observed=False,
                  point_diagnostic='context_point_outside_region', menu_verification='pending_execution')) as check):
            report = flow.register(self.recipe, self.path.parent / 'recovered')
        self.assertEqual(report['status'], 'registered')
        self.assertFalse(report['menu_checks'][0]['point_observed'])
        self.assertEqual(report['menu_checks'][0]['number'], 2)
        self.assertEqual(check.call_args.args[2]['target'], group)
        self.assertEqual(check.call_count, 1)

    def test_resumed_menu_claims_output_before_owning_window_and_releases_after_verification(self):
        self.recipe['steps'].append(output())
        self.write(self.recipe)
        flow.context_menu.execute.side_effect = self.menu
        ticket = self.cli('--allow-physical')['menu_repair']['ticket']
        events = self.ui.events
        events.clear()
        @contextmanager
        def lease(name, diagnostic):
            events.append(('acquired', diagnostic))
            try:
                yield
            finally:
                events.append(('released', diagnostic))
        with (patch.object(flow.coordination, '_lease', side_effect=lease),
              patch.object(repair, 'inspect_or_adjust', return_value=dict(self.recipe['steps'][1]['spec'], point=self.after))):
            result = self.cli('--allow-physical', '--resume-menu', ticket, '--frame', 'f', '--point', '20', '25')
        claimed = events.index(('acquired', 'export_queue_timeout'))
        owned = events.index(('acquired', 'window_queue_timeout'))
        released = max(i for i, event in enumerate(events) if event == ('released', 'export_queue_timeout'))
        self.assertTrue(claimed < owned < events.index('baseline') < events.index('menu') < events.index('file') < released)
        self.assertTrue(result['steps'][-1]['receipt']['file_verified'])

    def test_no_persistence_or_ticket_after_uncertain_leaf(self):
        recovery = dict(remaining_items=['Workbook'], observed_popup='Export')
        flow.context_menu.execute.side_effect = lambda *args: dict(
            status='uncertain', diagnostic='context_menu_delivery_uncertain', input_dispatched=None,
            leaf_dispatched=None, menu_path_completed=False, menu_items=['Export'],
            menu_recovery=recovery, point_correction={'from': self.before, 'to': self.after})
        report = self.cli('--allow-physical')
        summary = flow.summarize_execution(report, flow.selected_steps(self.recipe, None))
        self.assertEqual(summary['steps'][1]['receipt']['menu_recovery'], recovery)
        self.assertNotIn('menu_repair', report)
        self.assertNotIn('menu_corrections', report)
        self.assertEqual(flow.load(self.path), self.recipe)

    def test_changed_workflow_cannot_be_repaired_with_previous_ticket(self):
        flow.context_menu.execute.side_effect = self.menu
        ticket = self.cli('--allow-physical')['menu_repair']['ticket']
        self.write(dict(self.recipe, name='Revised'))
        with patch.object(repair, 'inspect_or_adjust') as adjust:
            observed = self.cli('--repair-frame', ticket, '--allow-physical')
        self.assertEqual(observed['diagnostic'], 'workflow_changed_during_repair')
        adjust.assert_not_called()

    def test_preinput_unreadable_root_preserves_original_claim_until_same_menu_resumes(self):
        flow.context_menu.execute.side_effect = self.menu
        ticket = self.cli('--allow-physical')['menu_repair']['ticket']
        session = repair.RepairSession(self.path)
        original = session.read(ticket, self.recipe)
        resume = ['--resume-menu', ticket, '--frame', 'frame', '--point', '30', '40', '--allow-physical']
        with patch.object(repair, 'inspect_or_adjust', return_value=dict(self.recipe['steps'][1]['spec'], point=self.after)):
            with patch.object(flow, 'check_window_readability', return_value=dict(
                    status='unavailable', diagnostic='window_root_unreadable', input_dispatched=False)):
                failure = self.cli(*resume)
            self.assertEqual(failure['menu_repair']['ticket'], ticket)
            self.assertFalse(failure['input_dispatched'])
            self.assertEqual(failure['steps'], [])
            self.assertEqual(original, session.read(ticket, self.recipe))
            self.assertEqual(self.cli(*resume)['menu_corrections'], [2])
            self.assertEqual(self.cli(*resume)['diagnostic'], 'menu_repair_expired')
        flow.live.execute.assert_called_once()

    def test_preinput_mismatched_root_consumes_claim_and_frame_inspection_requires_no_repeat_variables(self):
        self.recipe['steps'][0] = dict(action='set-value', selector=dict(role='textfield', name='Field'),
                                      parameter='Field')
        self.write(self.recipe)
        flow.context_menu.execute.side_effect = self.menu
        ticket = self.cli('--allow-physical', '--set', 'Field=Value')['menu_repair']['ticket']
        with patch.object(repair, 'inspect_or_adjust', return_value=dict(status='observed', input_dispatched=False)):
            self.assertEqual(self.cli('--allow-physical', '--repair-frame', ticket)['status'], 'observed')
        with (patch.object(repair, 'inspect_or_adjust', return_value=dict(self.recipe['steps'][1]['spec'], point=self.after)),
              patch.object(flow, 'check_window_readability', return_value=dict(
                  status='unavailable', diagnostic='window_root_process_mismatch', input_dispatched=False))):
            resume = ['--allow-physical', '--set', 'Field=Value', '--resume-menu', ticket,
                      '--frame', 'frame', '--point', '30', '40']
            self.assertNotIn('menu_repair', self.cli(*resume))
            self.assertEqual(self.cli(*resume)['diagnostic'], 'menu_repair_expired')

    def test_queued_run_loads_previous_workers_location_but_cannot_adopt_another_action(self):
        args = SimpleNamespace(recipe=self.path, repair_frame=None, resume_menu=None, only=None, hwnd=None)
        current = deepcopy(self.recipe)
        current['steps'][1]['spec']['point'] = self.after
        self.write(current)
        flow.context_menu.execute.side_effect = self.menu
        report = repair.run_registered(flow, self.recipe, args, {})
        self.assertTrue(report['steps'][-1]['receipt']['leaf_dispatched'])
        self.assertEqual(flow.context_menu.execute.call_args.args[1]['point'], self.after)
        current['steps'][1]['spec']['path'] = ['Another menu']
        self.write(current)
        report = repair.run_registered(flow, self.recipe, args, {})
        self.assertEqual(report['diagnostic'], 'workflow_changed_during_repair')
        self.assertFalse(report['input_dispatched'])
        flow.context_menu.execute.assert_called_once()

    def test_atomic_save_conflict_preserves_newer_content_and_successful_delivery(self):
        changed = deepcopy(self.recipe)
        changed['name'] = 'Edited during run'
        def perform(binding, spec, guard):
            self.write(changed)
            return dict(status='delivered_unverified', diagnostic='menu_leaf_sent', input_dispatched=True,
                        leaf_dispatched=True, menu_path_completed=True, menu_items=spec['path'],
                        point_correction={'from': self.before, 'to': self.after})
        flow.context_menu.execute.side_effect = perform
        report = self.cli('--allow-physical')
        self.assertEqual(report['repair_storage']['diagnostic'], 'workflow_changed_during_repair')
        self.assertTrue(report['steps'][1]['receipt']['leaf_dispatched'])
        self.assertEqual(flow.load(self.path), changed)

    def test_changed_result_menu_window_or_trigger_cannot_repeat_query(self):
        initial = self.recipe['steps'][1]
        self.recipe['steps'][1] = dict(action='result-menu', label='Results', context=dict(
            menu=initial['spec'], trigger=dict(role='button', name='Refresh'), output=None,
            ready=dict(kind='text', target=dict(role='statictext', name='State'), expected='Done')))
        self.write(self.recipe)
        blocked = dict(status='blocked', diagnostic='context_point_outside_region', input_dispatched=True,
                       query_dispatched=True, menu_input_dispatched=False, leaf_dispatched=False)
        confirmed = dict(status='delivered_unverified', diagnostic='menu_delivered_no_output_condition',
                         input_dispatched=True, leaf_dispatched=True, menu_path_completed=True,
                         menu_items=initial['spec']['path'])
        with (patch.object(flow.result_menu, 'execute', side_effect=[blocked, confirmed]) as execute,
              patch.object(repair, 'inspect_or_adjust', return_value=dict(initial['spec'], point=self.after))):
            failure = self.cli('--allow-physical')
            ticket = failure['menu_repair']['ticket']
            resumed = self.cli('--resume-menu', ticket, '--frame', 'f', '--point', '20', '25', '--allow-physical')
            self.assertIsNone(execute.call_args.args[1]['trigger'])
            self.assertEqual(resumed['menu_corrections'], [2])
        self.assertEqual(flow.load(self.path)['steps'][1]['context']['trigger'], self.recipe['steps'][1]['context']['trigger'])
