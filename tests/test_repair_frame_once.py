"""Opt-in menu frames share the original receipt and never dispatch a retry."""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_registered_menu_repair as registered_fixture
from test_python_context_workflow import output
import grounding

flow, repair = registered_fixture.flow, registered_fixture.repair


class RepairFrameOnceTests(unittest.TestCase):
    setUp = registered_fixture.RegisteredRepairTests.setUp
    write = registered_fixture.RegisteredRepairTests.write
    cli = registered_fixture.RegisteredRepairTests.cli
    menu = registered_fixture.RegisteredRepairTests.menu

    def run_once(self, **options):
        args = dict(recipe=self.path, repair_frame=None, resume_menu=None, only=None, hwnd=None,
                    auto_repair_frame=True, allow_physical=True, background_only=False, set=[])
        args.update(options)
        return repair.run_registered(flow, self.recipe, SimpleNamespace(**args),
                                     repair.tasks.parse_overrides(args['set']))

    def frame(self, *args):
        args[-1]()  # Exercise the current owner/surface guard before returning pixels.
        png = grounding._png(100, 100, bytes([90, 120, 150, 255]) * 10000)
        return (10, 20, 100, 100), png, (0, 0, 120, 140), png

    def with_parameter(self):
        self.recipe['steps'].insert(0, dict(action='set-value', selector=dict(role='textfield', name='Field'),
                                           parameter='Field'))
        self.recipe['steps'].append(output())
        self.write(self.recipe)

    def test_summary_returns_only_the_next_menu_action_when_the_frame_is_attached(self):
        flow.context_menu.execute.side_effect = self.menu
        with patch.object(repair, 'capture_region', side_effect=self.frame) as capture:
            result = self.cli('--allow-physical', '--auto-repair-frame', '--summary')
        handed = result['menu_repair']
        self.assertEqual(handed['host_action'], 'inspect_menu_repair_image')
        self.assertTrue(handed['image'])
        self.assertEqual(handed['resume_args'][0], '--resume-menu')
        self.assertNotIn('frame_args', handed)
        capture.assert_called_once()

    def test_one_response_frame_keeps_exact_variables_and_resume_does_not_repeat_query(self):
        self.with_parameter()
        runner = self.path.parent / 'scripts' / 'run.py'
        value = 'Hybrid 원문 = 값'
        flow.live.text_readback.return_value = dict(value=value)
        flow.context_menu.execute.side_effect = self.menu
        with patch.object(repair, 'capture_region', side_effect=self.frame) as capture:
            failed = self.run_once(set=['Field=' + value])
            handed = failed['menu_repair']
            self.assertEqual(failed['stopped_at'], 3)
            self.assertEqual(failed['diagnostic'], 'context_point_outside_region')
            self.assertEqual(handed['host_action'], 'inspect_menu_repair_image')
            self.assertEqual(handed['resume_script'], str(runner.resolve()))
            self.assertEqual(handed['resume_args'][-2:], ['--set', 'Field=' + value])
            self.assertEqual(handed['frame_args'][-2:], ['--set', 'Field=' + value])
            self.assertEqual(Path(handed['image']).read_bytes(), self.frame(None, lambda: None)[1])
            self.assertEqual(flow.context_menu.execute.call_count, 1)
            self.assertEqual(capture.call_count, 1)
            self.assertEqual(flow.load(self.path), self.recipe)
            session = repair.RepairSession(self.path)
            self.assertNotIn(value, session.pending.read_text(encoding='utf8'))
            self.assertNotIn('Field', session.pending.read_text(encoding='utf8'))
            resume_args = ['30' if token == '<x>' else '40' if token == '<y>' else token
                           for token in handed['resume_args']]
            revised = dict(self.recipe['steps'][2]['spec'], point=self.after)
            with patch.object(flow.context_menu, 'adjust_from_frame', return_value=revised):
                resumed = self.cli(*resume_args)
            self.assertEqual(resumed['resumed_at'], 3)
            self.assertEqual([row['number'] for row in resumed['steps']], [3, 4])
            self.assertTrue(resumed['steps'][-1]['receipt']['file_verified'])
            self.assertEqual(resumed['menu_corrections'], [3])
            self.assertEqual(sum(call.args[4] == 'invoke' for call in flow.live.execute.call_args_list), 1)
            self.assertEqual(flow.context_menu.execute.call_count, 2)
            self.assertFalse(session.pending.exists())
            self.assertEqual(self.cli(*resume_args)['diagnostic'], 'menu_repair_expired')

    def test_capture_failure_retains_action_receipt_ticket_and_parameterized_fallback(self):
        self.with_parameter()
        flow.context_menu.execute.side_effect = self.menu
        with patch.object(repair, 'capture_region', side_effect=repair.tasks.TaskError('grounding_window_moved')):
            report = self.run_once(set=['Field=Value'])
        self.assertEqual(report['diagnostic'], 'context_point_outside_region')
        self.assertEqual(report['steps'][2]['receipt']['diagnostic'], 'context_point_outside_region')
        self.assertTrue(report['input_dispatched'])
        handed = report['menu_repair']
        self.assertEqual(handed['frame_capture']['diagnostic'], 'grounding_window_moved')
        self.assertEqual(handed['frame_args'][-2:], ['--set', 'Field=Value'])
        self.assertNotIn('image', handed)
        self.assertEqual(repair.RepairSession(self.path).read(handed['ticket'], self.recipe)['number'], 3)
        self.assertEqual(flow.context_menu.execute.call_count, 1)

    def test_standalone_frame_uses_supplied_variables_without_requiring_them_for_inspection(self):
        self.with_parameter()
        flow.context_menu.execute.side_effect = self.menu
        ticket = self.run_once(auto_repair_frame=False, set=['Field=Value'])['menu_repair']['ticket']
        with patch.object(repair, 'capture_region', side_effect=self.frame):
            without = self.cli('--allow-physical', '--repair-frame', ticket)
            supplied = self.cli('--allow-physical', '--repair-frame', ticket, '--set', 'Field=Value')
        self.assertEqual(without['status'], 'observed')
        self.assertTrue(without['resume_requires_original_set'])
        self.assertNotIn('resume_requires_original_set', supplied)
        self.assertEqual(supplied['resume_args'][-2:], ['--set', 'Field=Value'])
        resume = ['30' if token == '<x>' else '40' if token == '<y>' else token
                  for token in without['resume_args']]
        with patch.object(repair, 'inspect_or_adjust') as adjust:
            self.assertEqual(self.cli(*resume)['diagnostic'], 'menu_repair_parameters_changed')
        adjust.assert_not_called()
        self.assertEqual(flow.context_menu.execute.call_count, 1)

    def test_capture_is_opt_in_requires_physical_approval_and_is_never_background(self):
        flow.context_menu.execute.side_effect = self.menu
        with patch.object(repair, 'inspect_or_adjust') as capture:
            for options in (dict(auto_repair_frame=False), dict(allow_physical=False), dict(background_only=True)):
                with self.subTest(options=options):
                    self.assertIn('menu_repair', self.run_once(**options))
            old_args = SimpleNamespace(recipe=self.path, repair_frame=None, resume_menu=None, only=None, hwnd=None)
            self.assertIn('menu_repair', repair.run_registered(flow, self.recipe, old_args, {}))
        capture.assert_not_called()

    def test_uncertain_or_dispatched_leaf_never_receives_ticket_or_frame(self):
        for receipt in (
            dict(status='uncertain', diagnostic='context_menu_delivery_uncertain', input_dispatched=None,
                 leaf_dispatched=None, menu_path_completed=False, menu_items=['Export']),
            dict(status='uncertain', diagnostic='context_point_outside_region', input_dispatched=False,
                 leaf_dispatched=False, menu_path_completed=False, menu_items=[]),
            dict(status='blocked', diagnostic='context_point_outside_region', input_dispatched=True,
                 leaf_dispatched=True, menu_path_completed=False, menu_items=[]),
        ):
            with self.subTest(receipt=receipt):
                flow.context_menu.execute.side_effect = lambda *args: deepcopy(receipt)
                with patch.object(repair, 'inspect_or_adjust') as capture:
                    report = self.run_once()
                self.assertNotIn('menu_repair', report)
                self.assertFalse(repair.RepairSession(self.path).pending.exists())
                capture.assert_not_called()

    def test_stale_scope_does_not_turn_uncertain_receipt_into_a_repair(self):
        report = dict(status='uncertain', diagnostic='context_menu_delivery_uncertain', input_dispatched=None,
                      stopped_at=2, menu_repair_scope=[[100, 11, 4545], [100, 11, 4545]],
                      steps=[dict(number=2, receipt=dict(status='uncertain', input_dispatched=None,
                                                        leaf_dispatched=None, menu_items=[]))])
        with patch.object(flow, 'run', return_value=report), patch.object(repair, 'inspect_or_adjust') as capture:
            result = self.run_once()
        self.assertNotIn('menu_repair', result)
        capture.assert_not_called()

    def test_auto_frame_holds_same_owner_declared_contexts_and_surface_scopes(self):
        self.recipe['steps'][1]['window_context'] = dict(exe='Child.exe', title='Child results', mode='existing')
        self.write(self.recipe)
        report = dict(status='blocked', diagnostic='context_point_outside_region', input_dispatched=True,
                      stopped_at=2, menu_repair_scope=[[100, 11, 4545], [200, 11, 4546]],
                      steps=[dict(number=2, receipt=dict(status='blocked', input_dispatched=False,
                                                        leaf_dispatched=False, menu_items=[],
                                                        diagnostic='context_point_outside_region'))])
        held = []
        @contextmanager
        def hold(name):
            held.append(name)
            try:
                yield
            finally:
                held.remove(name)
        def inspect(*args):
            self.assertEqual(held, ['owner', 'surface'])
            return dict(status='observed', input_dispatched=False, image='scope.png', frame='scope-frame')
        with (patch.object(flow, 'run', return_value=report),
              patch.object(repair.coordination, 'window', side_effect=lambda *args: hold('owner')) as window,
              patch.object(repair.coordination, 'handle', side_effect=lambda *args: hold('surface')) as handle,
              patch.object(repair, 'inspect_or_adjust', side_effect=inspect)):
            result = self.run_once()
        window.assert_called_once_with(dict(hwnd=100, exe_name=self.recipe['exe']), self.recipe['window'],
                                       [self.recipe['steps'][1]['window_context']])
        handle.assert_called_once_with(dict(hwnd=200))
        self.assertEqual(result['menu_repair']['frame'], 'scope-frame')
        self.assertEqual(held, [])


if __name__ == '__main__':
    unittest.main()
