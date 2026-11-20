"""Literal registered workflows preserve parameters and stop after uncertain input."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import python_workflow as flow


def recipe():
    return dict(schema=1, name='Example workflow', exe='Example.exe', window='Example window', steps=[
        dict(action='set-value', selector=dict(automation_id='status', role='combobox'), value='N'),
        dict(action='set-value', selector=dict(automation_id='family', role='combobox'), parameter='Family'),
        dict(action='invoke', selector=dict(automation_id='search', role='button')),
        dict(action='wait', seconds=0),
        dict(action='invoke', selector=dict(automation_id='export', role='button'))])


class RecipeTests(unittest.TestCase):
    def test_required_parameter_has_no_recorded_default(self):
        result = flow.run(recipe(), {})
        self.assertEqual(result['status'], 'needs_input')
        self.assertEqual(result['missing_parameters'], ['Family'])
        self.assertFalse(result['input_dispatched'])
        self.assertEqual(result['steps'], [])

    def test_no_implicit_default_or_arbitrary_code(self):
        for update in ({'default': 'Hybrid'}, {'value': 'Hybrid'}, {'action': 'shell'}):
            data = recipe()
            data['steps'][1].update(update)
            with self.subTest(update=update), self.assertRaises((flow.WorkflowError, ValueError)):
                flow.validate(data)

    def test_invalid_wait_and_unknown_values(self):
        for seconds in (-1, True, float('nan'), 121):
            data = recipe()
            data['steps'][3]['seconds'] = seconds
            with self.subTest(seconds=seconds), self.assertRaises(flow.WorkflowError):
                flow.validate(data)
        with self.assertRaisesRegex(flow.WorkflowError, 'unknown_parameter'):
            flow.values_for(recipe(), {'Unrelated': 'N'})

    def test_case_insensitive_parameter_but_conflicting_keys_fail(self):
        self.assertEqual(flow.values_for(recipe(), {'family': 'Hybrid'}), ({'family': 'Hybrid'}, []))
        with self.assertRaisesRegex(flow.WorkflowError, 'invalid_override'):
            flow.values_for(recipe(), {'family': 'Hybrid', 'Family': 'DRAM'})


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.uia = MagicMock()
        self.uia.__enter__.return_value = self.uia
        self.uia.same.return_value = True
        self.uia.actions.return_value = ['SetValue', 'Click']
        self.binding = MagicMock(hwnd=101, pid=11)
        self.uia.pid.return_value = self.binding.pid
        self.element = MagicMock()
        self.element.property.return_value = ''
        for name, kwargs in (
            ('bind', dict(return_value=self.binding)),
            ('app.bridge.UIA', dict(return_value=self.uia)),
            ('app.bridge.user32.IsIconic', dict(return_value=False)),
            ('app.guard', dict(return_value=None)),
            ('app.visibility.restore_for_action', dict(return_value=True)),
            ('lookup.resolve', dict(return_value=self.element)),
            ('wait', dict(return_value=None))):
            p = patch('python_workflow.' + name, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def execute(self, *args, **kwargs):
        kwargs['still_valid']()
        if args[4] == 'select':
            return dict(status='verified', input_dispatched=True)
        return dict(status='delivered_unverified', diagnostic='business_result_not_verified', input_dispatched=True)

    def test_single_session_literal_values_and_explicit_wait(self):
        with patch.object(flow.live, 'execute', side_effect=self.execute) as execute:
            result = flow.run(recipe(), {'Family': 'Hybrid'})
        self.assertEqual(result['status'], 'completed_unverified')
        self.assertEqual([c.args[5] for c in execute.call_args_list], ['N', 'Hybrid', None, None])
        self.assertEqual(flow.app.bridge.UIA.call_count, 1)
        self.assertEqual([s['receipt']['status'] for s in result['steps']],
                         ['verified', 'verified', 'delivered_unverified', 'waited', 'delivered_unverified'])

    def test_reviewed_result_point_requires_the_current_table_and_noneditable_data(self):
        import grounding
        target = MagicMock()
        group = MagicMock()
        sibling = MagicMock()
        group.property.side_effect = lambda key: {30003: 50026}.get(key)
        target.property.side_effect = lambda key: {30003: 50025}.get(key)
        self.uia.from_point.return_value = target
        self.uia.secure.return_value = False
        self.uia.same.side_effect = lambda a, b: a is b
        self.uia.bounds.return_value = dict(x=105, y=106, width=20, height=20)
        selector = dict(role='group', automation_id='results')
        confirmation = dict(receipt='1'*64, frame='2'*64, point=[10, 11], selector=selector)
        with (patch.object(grounding, '_directory'), patch.object(grounding, 'read_frame', return_value=(None, b'frame')),
              patch.object(grounding, '_capture', return_value=([100,100,80,70], b'frame')) as capture,
              patch.object(flow, 'result_surface', return_value=group) as surface,
              patch.object(flow.live, '_visible', return_value=True),
              patch.object(flow.lookup, '_raw_parent', side_effect=lambda _, node: group if node is target else None) as parent):
            def check():
                flow.check_result_frame(self.binding, confirmation, (110, 111), lambda: None)
            check()
            self.assertEqual(surface.call_args.args[-1], selector)
            for role in (50004, 50000, 50034, 50035):
                target.property.side_effect = lambda key: {30003: role}.get(key)
                with self.subTest(role=role), self.assertRaisesRegex(ValueError, 'result_point_not_data'):
                    check()
            target.property.side_effect = lambda key: {30003: 50025, 30043: True, 30046: False}.get(key)
            with self.assertRaisesRegex(ValueError, 'result_point_not_data'):
                check()
            target.property.side_effect = lambda key: {30003: 50029}.get(key)
            parent.side_effect = lambda _, node: sibling if node is target else None
            with self.assertRaisesRegex(flow.app.bridge.BridgeError, 'target_outside_window'):
                check()
            capture.return_value = ([100,100,80,70], b'changed')
            with self.assertRaisesRegex(ValueError, 'result_frame_changed'):
                check()

    def test_exact_verified_result_can_change_each_run_and_query_is_never_repeated(self):
        import result_cell
        data = recipe()
        data['steps'] = [
            dict(action='invoke', selector=dict(role='button', name='VIEW')),
            dict(action='verify-value', selector=dict(role='table'), result_column='Part', parameter='Part'),
            dict(action='double-click-result', label='Open matching row'),
        ]
        dispatch = dict(status='delivered_unverified', diagnostic='visual_double_click_dispatched',
                        input_dispatched=True, physical_input=True)
        with (patch.object(flow, 'result_surface', return_value=self.element),
              patch.object(flow, 'RESULT_VISUAL_TIMEOUT', 0),
              patch.object(result_cell, 'find') as find, patch.object(result_cell, 'execute', return_value=dispatch) as click,
              patch.object(flow.live, 'execute', side_effect=self.execute) as invoke):
            for number, value in enumerate(('LAB-42', 'A401'), 1):
                report = flow.run(data, {'Part': value})
                self.assertEqual(report['status'], 'completed_unverified', report)
                self.assertEqual(find.call_args.args[3], value)
                self.assertEqual(click.call_args.args[3], value)
                self.assertEqual(find.call_args.kwargs['column'], 'Part')
                self.assertEqual(invoke.call_count, number)
            find.side_effect = flow.app.bridge.BridgeError('result_cell_missing')
            report = flow.run(data, {'Part': 'absent'})
            self.assertEqual((report['status'], report['stopped_at']), ('blocked', 2), report)
            self.assertTrue(report['input_dispatched'])  # the single query was sent
            self.assertFalse(report['steps'][-1]['receipt']['input_dispatched'])
            self.assertEqual(click.call_count, 2)
            self.assertEqual(invoke.call_count, 3)
        self.assertEqual([n for n, _ in flow.selected_steps(data, 3)], [2, 3])
        with self.assertRaisesRegex(flow.WorkflowError, 'verified_result_required'):
            flow.validate(dict(data, steps=[data['steps'][2]]))

    def test_read_and_verify_observe_only_and_keep_literal_in_summary(self):
        selector = dict(role='textfield', automation_id='Details')
        data = dict(recipe(), steps=[dict(action='read-value', selector=selector, label='Details'),
                                     dict(action='verify-value', selector=selector, parameter='Expected')])
        with (patch.object(flow.live, 'text_readback', return_value={
                'value': '', 'value_evidence': {'status': 'observed', 'source': 'value_pattern'}}),
              patch.object(flow.live, 'execute') as mutate):
            report = flow.run(data, {'Expected': ''})
            self.assertEqual(report['status'], 'verified', report)
            self.assertFalse(report['input_dispatched'])
            self.assertEqual([r['input_mode'] for r in report['input_requirements']['steps']], ['observation'] * 2)
            summary = flow.summarize_execution(report, flow.selected_steps(data, None))
            self.assertEqual([r['value'] for r in summary['steps']], ['', ''])
            mismatch = flow.run(data, {'Expected': 'different'})
            self.assertEqual((mismatch['status'], mismatch['stopped_at']), ('mismatch', 2))
            self.assertFalse(mismatch['input_dispatched'])
            mutate.assert_not_called()
            flow.app.visibility.restore_for_action.assert_not_called()
        with patch.object(flow.live, 'text_readback', return_value={'value':None, 'value_evidence':{'status':'unavailable'}}):
            self.assertEqual(flow.run(data, {'Expected':''})['diagnostic'], 'value_observation_unavailable')

    def test_only_explicit_scoped_close_observes_its_disappearance(self):
        popup = dict(title='Details')
        close = dict(action='invoke', selector=dict(role='button', name='Exit'), popup=popup, verify_closed=True)
        with self.assertRaisesRegex(flow.WorkflowError, 'invalid_close_verification'):
            flow.validate(dict(recipe(), steps=[{k: v for k, v in close.items() if k != 'popup'}]))
        surface = MagicMock(hwnd=204, pid=self.binding.pid)
        with (patch.object(flow, 'popup_surface', return_value=surface),
              patch.object(flow, 'check_surface'), patch.object(flow, 'open_secondary_handles', return_value=[]),
              patch.object(flow.app, 'owned_popups', return_value=[]),
              patch.object(flow.app.bridge.user32, 'IsWindowVisible', return_value=False),
              patch.object(flow.live, 'execute', side_effect=self.execute) as invoke):
            report = flow.run(dict(recipe(), steps=[close]))
            self.assertEqual(report['steps'][0]['receipt']['verification_scope'], 'window_closed', report)
            self.assertTrue(report['input_dispatched'])
            invoke.assert_called_once()
            without_check = {k:v for k,v in close.items() if k != 'verify_closed'}
            report = flow.run(dict(recipe(), steps=[without_check]))
            self.assertEqual(report['status'], 'completed_unverified')
            self.assertNotIn('verification_scope', report['steps'][0]['receipt'])

    def test_query_waits_for_current_field_only_until_match_without_second_invoke(self):
        selector = dict(role='textfield', automation_id='ResultCode')
        data = dict(recipe(), steps=[dict(action='invoke', selector=dict(role='button', name='Search')),
                                     dict(action='verify-value', selector=selector, value='part07')])
        evidence = {'status':'observed', 'source':'value_pattern'}
        with (patch.object(flow.live, 'execute', side_effect=self.execute) as invoke,
              patch.object(flow.live, 'text_readback', side_effect=[
                  dict(value='stale', value_evidence=evidence), dict(value='part07', value_evidence=evidence)]) as read,
              patch.object(flow.time, 'sleep') as sleep):
            report = flow.run(data, read_only=True)
            self.assertEqual(report['steps'][1]['receipt']['status'], 'deferred')
            read.assert_not_called()
            report = flow.run(data)
            self.assertEqual(report['steps'][1]['receipt']['value'], 'part07', report)
            self.assertEqual(read.call_count, 2)
            sleep.assert_called_once()
            invoke.assert_called_once()

    def test_named_read_proves_unprotected_ancestor_before_accessing_name(self):
        with patch.object(flow.lookup, 'ancestry', side_effect=flow.app.bridge.BridgeError('target_protected')):
            with self.assertRaisesRegex(flow.app.bridge.BridgeError, 'target_protected'):
                flow.observe_field(self.binding, self.uia, self.element, 'statictext', 'read-value')
            self.element.property.assert_not_called()

    def test_implicit_table_selector_counts_visible_lists_and_rejects_duplicates(self):
        candidates = [MagicMock(), MagicMock()]
        root = self.uia.from_handle(self.binding.hwnd)
        with (patch.object(flow.lookup, 'find_all', return_value=candidates) as search,
              patch.object(flow.live, '_visible', return_value=True)):
            with self.assertRaisesRegex(flow.WorkflowError, 'result_table_ambiguous'):
                flow.result_surface(self.binding, self.uia, {'role':'table'})
            search.assert_called_once_with(self.uia, root, roles=('table','list'))
        for candidate in candidates:
            candidate.close.assert_called_once()

    def test_uncertain_combo_never_invokes_next_step_or_retries(self):
        with patch.object(flow.live, 'execute', return_value=dict(status='uncertain', input_dispatched=None)) as execute:
            result = flow.run(recipe(), {'Family': 'Hybrid'})
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(result['stopped_at'], 1)
        self.assertIsNone(result['input_dispatched'])
        execute.assert_called_once()

    def test_view_then_double_click_requires_a_new_target_or_independent_recheck(self):
        from test_visual_target import image, visual, VisualTargets
        marker = visual.from_frame(VisualTargets.window, image((35,42)), (235,162))
        data = recipe()
        data['steps'] = [dict(action='invoke', selector=dict(role='button', name='VIEW')),
                         dict(action='visual-click', label='결과 행 더블 클릭', visual=dict(marker, gesture='double-click'))]
        delivery = dict(status='delivered_unverified', diagnostic='visual_double_click_dispatched', input_dispatched=True)
        with (patch.object(visual,'observe') as observe, patch.object(visual,'execute', return_value=delivery) as click,
              patch.object(flow.live,'execute', side_effect=self.execute) as query):
            for stale in ('already_visible', 'visual_target_ambiguous'):
                observe.side_effect = None if stale == 'already_visible' else visual.bridge.BridgeError(stale)
                report = flow.run(data)
                self.assertEqual(report['diagnostic'], 'query_result_freshness_unproven', report)
                self.assertEqual(report['steps'][1]['receipt']['before_query'], stale)
                self.assertTrue(report['input_dispatched'])
                self.assertFalse(report['steps'][1]['receipt']['input_dispatched'])
                query.assert_called_once()
                query.reset_mock()
                click.assert_not_called()
            observe.side_effect = visual.bridge.BridgeError('visual_target_not_found')
            self.assertEqual(flow.run(data)['status'], 'completed_unverified')
            self.assertEqual(click.call_args.kwargs['first_match_timeout'], flow.RESULT_VISUAL_TIMEOUT)
            observe.reset_mock()
            query.reset_mock()
            self.assertEqual(flow.run(data, only=2)['status'], 'completed_unverified')
            self.assertEqual(click.call_args.kwargs['first_match_timeout'], 0)
            observe.assert_not_called()
            query.assert_not_called()

    def test_registered_command_caption_change_blocks_reused_id(self):
        data = recipe()
        data['steps'] = [dict(action='invoke', selector=dict(automation_id='search', role='button'),
                              command_name='Search')]
        self.element.property.return_value = 'Delete'
        with patch.object(flow.live, 'execute') as execute:
            result = flow.run(data)
        self.assertEqual(result['diagnostic'], 'command_name_changed')
        self.assertFalse(result['input_dispatched'])
        execute.assert_not_called()

    def test_failed_search_does_not_wait_or_export(self):
        receipts = [dict(status='verified', input_dispatched=True)] * 2
        receipts.append(dict(status='blocked', input_dispatched=False, diagnostic='target_disabled'))
        with patch.object(flow.live, 'execute', side_effect=receipts) as execute:
            result = flow.run(recipe(), {'Family': 'DRAM'})
        self.assertEqual(result['stopped_at'], 3)
        self.assertEqual(execute.call_count, 3)
        flow.wait.assert_not_called()

    def test_readonly_neither_restore_nor_dispatch(self):
        with patch.object(flow.live, 'execute') as execute, patch.object(flow.live, 'combo_readback', return_value={}):
            result = flow.run(recipe(), read_only=True)
        self.assertEqual(result['status'], 'observed')
        self.assertFalse(result['input_dispatched'])
        flow.app.visibility.restore_for_action.assert_not_called()
        execute.assert_not_called()
        flow.wait.assert_not_called()

    def test_readonly_minimized_window_diagnosed_before_recorded_or_grounding_lookup(self):
        data = recipe()
        recorded = dict(action='invoke', selector=dict(automation_id='run', role='button'))
        instructed = dict(action='invoke', selector=dict(query='Run', role='button'),
                          grounding_text='Run button')
        flow.app.bridge.user32.IsIconic.return_value = True
        for step in (recorded, instructed):
            with self.subTest(step=step), patch.object(flow.live, 'execute') as execute:
                data['steps'] = [step]
                result = flow.run(data, read_only=True)
                self.assertEqual((result['status'], result['diagnostic'], result['input_dispatched']),
                                 ('blocked', 'target_window_minimized', False))
                self.uia.from_handle.assert_not_called()
                flow.lookup.resolve.assert_not_called()
                flow.app.visibility.restore_for_action.assert_not_called()
                execute.assert_not_called()

    def test_register_creates_runner_recipe_and_two_guides_without_overwriting(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / 'query-skill'
            with patch.object(flow.live, 'combo_readback', return_value={}):
                result = flow.register(recipe(), destination)
            self.assertEqual(result['status'], 'registered')
            self.assertEqual(sorted(str(p.relative_to(destination)).replace('\\', '/')
                                    for p in destination.rglob('*') if p.is_file()),
                             ['SKILL.md', 'scripts/run.py', 'workflow-guide.md', 'workflow.json'])
            self.assertEqual(flow.load(destination / 'workflow.json'), recipe())
            compile((destination / 'scripts/run.py').read_text(encoding='utf8'), '<runner>', 'exec')
            with self.assertRaisesRegex(flow.WorkflowError, 'workflow_destination_exists'):
                flow.register(recipe(), destination)


class BoundaryTests(unittest.TestCase):
    def test_worker_timeout_never_means_safe_to_retry(self):
        for read_only in (True, False):
            supervisor = (flow.subprocess, 'run') if read_only else (flow.coordination, 'run_worker')
            with (self.subTest(read_only=read_only), patch.object(flow, 'load', return_value=recipe()), patch.object(
                *supervisor, side_effect=subprocess.TimeoutExpired('workflow', 90)) as launched,
                redirect_stdout(StringIO()) as output):
                flow.main(['run', '--recipe', 'example.json', '--set', 'Family=Hybrid', *(['--read-only'] if read_only else [])])
                result = json.loads(output.getvalue())
            launched.assert_called_once()
            self.assertEqual(result['status'], 'blocked' if read_only else 'uncertain')
            self.assertIs(result['input_dispatched'], False if read_only else None)


if __name__ == '__main__':
    unittest.main()
