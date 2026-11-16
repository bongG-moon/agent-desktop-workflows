"""Exercise registered-run consent, single-use continuation and the literal saved recipe."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
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
