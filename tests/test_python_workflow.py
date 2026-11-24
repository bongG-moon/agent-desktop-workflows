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

    def key_timeline(self, steps, **options):
        now = 0.0
        dispatched = []

        def advance(seconds):
            nonlocal now
            now += seconds

        def dispatch(surface, uia, element, node, action, value, *, still_valid, **kwargs):
            still_valid()
            dispatched.append((action, value, round(now, 4)))
            if action == 'submit':
                return dict(status='delivered_unverified', input_dispatched=True,
                            diagnostic='submit_enter_sent', keyboard_events_sent=2)
            if action == 'press-key':
                return dict(status='delivered_unverified', input_dispatched=True,
                            diagnostic='key_chord_sent', keys=value,
                            keyboard_events_sent=2 * len(flow.key_chords.virtual_keys(value)))
            return dict(status='verified', input_dispatched=True)

        self.uia.actions.return_value = ['SetValue', 'SetFocus']
        with (patch.object(flow, 'time') as clock, patch.object(flow.live, 'execute', side_effect=dispatch),
              patch.object(flow.live, 'submit_ready'), patch.object(flow.live, 'key_target_ready')):
            clock.monotonic.side_effect = lambda: now
            clock.sleep.side_effect = advance
            flow.wait.side_effect = lambda binding, title, seconds: advance(seconds)
            report = flow.run(dict(recipe(), steps=steps), **options)
        expected = 'observed' if options.get('read_only') else 'completed_unverified'
        self.assertEqual(report['status'], expected, report)
        return dispatched, round(now, 4)

    def test_default_intervals_apply_only_to_keys_and_adjacent_keys_share_one_gap(self):
        selector = dict(automation_id='entry', role='textfield')
        fixed = dict(action='set-value', selector=selector, value='first')
        variable = dict(action='set-value', selector=selector, parameter='Part')
        enter = dict(action='submit', selector=selector)
        tab = dict(action='press-key', selector=selector, keys='Tab')
        events, end = self.key_timeline([fixed, variable, enter, tab, fixed], overrides={'Part': 'next'})
        self.assertEqual(events, [('fill', 'first', 0), ('fill', 'next', 0), ('submit', None, 0.8),
                                  ('press-key', 'Tab', 1.6), ('fill', 'first', 2.4)])
        self.assertEqual(end, 2.4)

    def test_explicit_workflow_waits_replace_default_key_intervals_on_either_side(self):
        key = dict(action='press-key', selector=dict(automation_id='entry', role='textfield'), keys='F5')
        for before, between, after in ((0, 0, 0), (0.2, 0.3, 1.25), (1.5, 2.5, 0.1)):
            with self.subTest(before=before, between=between, after=after):
                steps = [dict(action='wait', seconds=before), key, dict(action='wait', seconds=between),
                         key, dict(action='wait', seconds=after)]
                events, end = self.key_timeline(steps)
                self.assertEqual(events, [('press-key', 'F5', before), ('press-key', 'F5', before + between)])
                self.assertAlmostEqual(end, before + between + after)

    def test_selected_enter_uses_both_intervals_when_neighboring_waits_are_not_executed(self):
        enter = dict(action='submit', selector=dict(automation_id='entry', role='textfield'))
        steps = [dict(action='wait', seconds=0.1), enter, dict(action='wait', seconds=0.2)]
        self.assertEqual(self.key_timeline(steps, only=2), ([('submit', None, 0.8)], 1.6))

    def test_read_only_keys_and_wait_have_no_delay(self):
        selector = dict(automation_id='entry', role='textfield')
        steps = [dict(action='submit', selector=selector), dict(action='wait', seconds=2),
                 dict(action='press-key', selector=selector, keys='Ctrl+S')]
        self.assertEqual(self.key_timeline(steps, read_only=True), ([], 0))

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

    def test_old_recording_pixels_never_choose_the_row_for_a_new_input(self):
        import recorded_plan
        import result_cell
        from test_visual_target import image, visual, VisualTargets
        marker = visual.from_frame(VisualTargets.window, image((35, 42)), (235, 162))
        source = dict(schema=1, name='Saved lookup', exe='Example.exe', window='Example window', steps=[
            dict(action='fill', target='code', role='textfield', match='automation_id', label='Code',
                 recorded_effect='unobserved', instruction=dict(mode='required', parameter='검색어', text='Code 입력')),
            dict(action='open', target='query', role='button', match='automation_id', expand=False,
                 command_name='VIEW', instruction=dict(mode='recorded', text='VIEW 클릭')),
            dict(action='visual-click', visual=marker, label='Code 일치 확인'),
            dict(action='visual-click', visual=dict(marker, gesture='double-click'), label='결과 행을 더블클릭해줘')])
        data = recorded_plan.compile_plan(source)['envelope']['recipe']
        self.element.property.side_effect = lambda key: 'VIEW' if key == 30005 else ''
        dispatch = dict(status='delivered_unverified', diagnostic='visual_double_click_dispatched',
                        input_dispatched=True, physical_input=True)
        def execute(*args, **kwargs):
            return dict(status='verified', input_dispatched=True) if args[4] == 'fill' else self.execute(*args, **kwargs)
        with (patch.object(flow, 'result_surface', return_value=self.element),
              patch.object(flow, 'RESULT_VISUAL_TIMEOUT', 0),
              patch.object(result_cell, 'find') as find, patch.object(result_cell, 'execute', return_value=dispatch) as click,
              patch.object(flow.live, 'execute', side_effect=execute) as inputs,
              patch.object(visual, 'execute', side_effect=AssertionError('recorded pixels must not be used'))):
            for number, value in enumerate(('LAB-42', 'PART-A'), 1):
                report = flow.run(data, {'검색어': value})
                self.assertEqual(report['status'], 'completed_unverified', report)
                self.assertEqual(find.call_args.args[3], value)
                self.assertEqual(click.call_args.args[3], value)
                self.assertEqual(find.call_args.kwargs['column'], 'Code')
                self.assertEqual(inputs.call_count, number * 2)
            find.side_effect = flow.app.bridge.BridgeError('result_value_not_found')
            report = flow.run(data, {'검색어': 'absent'})
            self.assertEqual(report['status'], 'blocked', report)
            self.assertEqual(click.call_count, 2)

    def test_confirmed_painted_group_hands_off_after_first_read_without_another_query(self):
        import result_cell
        table = dict(role='table')
        group = dict(role='group', automation_id='PaintedSurface')
        step = dict(action='verify-value', selector=table, result_column='Code', value='LAB-42')
        data = dict(recipe(), steps=[dict(action='invoke',selector=dict(role='button',name='VIEW')),step])
        with (patch.object(flow, 'result_surface', side_effect=[flow.WorkflowError('result_table_missing'),self.element]) as surface,
              patch.object(result_cell,'find',side_effect=flow.app.bridge.BridgeError('result_column_unavailable')),
              patch.object(result_cell,'bare_group',return_value=True),
              patch.object(flow.live,'execute',side_effect=self.execute) as invoke,
              patch.object(flow.time,'sleep') as sleep):
            result = flow.run(data, result_surface_hint=dict(number=2,selector=group))
            receipt = result['steps'][-1]['receipt']
            self.assertEqual(receipt['result_observation']['strategy'],'reviewed_group_pixels')
            self.assertEqual(receipt['result_observation']['attempts'],1)
            self.assertEqual(receipt['result_observation']['wait_seconds'],0)
            self.assertEqual(receipt['result_surface_selector'],group)
            self.assertFalse(receipt['input_dispatched'])
            self.assertEqual([call.args[-1] for call in surface.call_args_list],[table,group])
            invoke.assert_called_once()
            sleep.assert_not_called()

    def test_temporary_header_and_cell_absence_still_waits_until_uia_succeeds(self):
        import result_cell
        step = dict(action='verify-value', selector=dict(role='group',name='Loading results'),
                    result_column='Code', value='LAB-42')
        now = [200.]
        def sleep(seconds):
            now[0] += seconds
        missing_header = flow.app.bridge.BridgeError('result_column_unavailable')
        missing_cell = flow.app.bridge.BridgeError('result_cell_missing')
        with (patch.object(flow,'result_surface',return_value=self.element),
              patch.object(result_cell,'find',side_effect=[missing_header]*8+[missing_cell,self.element]),
              patch.object(result_cell,'bare_group') as structure,
              patch.object(flow.time,'monotonic',side_effect=lambda: now[0]),
              patch.object(flow.time,'sleep',side_effect=sleep)):
            receipt = flow.observe_result(self.binding,self.uia,step,'LAB-42',lambda: None,wait_seconds=5)
        self.assertEqual(receipt['status'],'verified')
        self.assertEqual(receipt['result_observation']['attempts'],10)
        self.assertAlmostEqual(receipt['result_observation']['wait_seconds'],1.8)
        structure.assert_not_called()

    def test_row_only_mode_opens_current_result_without_old_image_or_value_comparison(self):
        import result_cell
        import visual_target
        selector = dict(role='table', automation_id='Results')
        data = dict(recipe(), steps=[dict(action='invoke', selector=dict(role='button', name='VIEW')),
                     dict(action='double-click-result', result_row='single', selector=selector, label='Open current row')])
        self.uia.same.side_effect = lambda a, b: a is b
        sent = dict(status='delivered_unverified', diagnostic='visual_double_click_dispatched',
                    input_dispatched=True, physical_input=True)
        with (patch.object(flow, 'result_surface', return_value=self.element),
              patch.object(result_cell, 'find_row', return_value=self.element) as read,
              patch.object(result_cell, 'execute_row', return_value=sent) as click,
              patch.object(result_cell, 'find', side_effect=AssertionError('no requested equality')),
              patch.object(visual_target, 'observe', side_effect=AssertionError('no fixed image baseline')),
              patch.object(visual_target, 'execute', side_effect=AssertionError('no old image click')),
              patch.object(flow.live, 'execute', side_effect=self.execute) as invoke):
            result = flow.run(data)
        self.assertEqual(result['status'], 'completed_unverified', result)
        self.assertFalse(result['steps'][-1]['receipt']['value_comparison'])
        self.assertEqual(result['steps'][-1]['receipt']['query_freshness'], 'not_asserted')
        self.assertEqual(read.call_args.kwargs['mode'], 'single')
        self.assertEqual(click.call_args.kwargs['mode'], 'single')
        invoke.assert_called_once()
        self.assertEqual([n for n, _ in flow.selected_steps(data, 2)], [2])

    def test_row_only_opaque_surface_has_one_scoped_handoff_and_no_retry(self):
        import result_cell
        selector = dict(role='group', automation_id='PaintedResults')
        step = dict(action='double-click-result', result_row='single', selector=selector)
        box = dict(x=20, y=150, width=900, height=400)
        with (patch.object(flow, 'result_surface', return_value=self.element),
              patch.object(result_cell, 'find_row', side_effect=flow.app.bridge.BridgeError('result_row_unavailable')) as read,
              patch.object(result_cell, 'execute_row') as click,
              patch.object(self.uia, 'bounds', return_value=box),
              patch.object(flow.time, 'sleep') as sleep):
            result = flow.run(dict(recipe(), steps=[step]))
        receipt = result['steps'][-1]['receipt']
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(receipt['diagnostic'], 'result_row_unavailable')
        self.assertEqual(receipt['result_surface_selector'], selector)
        self.assertEqual(receipt['result_table_bounds'], box)
        self.assertIn('result_window', receipt)
        self.assertFalse(result['input_dispatched'])
        read.assert_called_once()
        click.assert_not_called()
        sleep.assert_not_called()

    def test_row_only_schema_rejects_value_and_unknown_row_policy(self):
        step = dict(action='double-click-result', result_row='first', selector=dict(role='table'))
        self.assertEqual(flow.validate(dict(recipe(), steps=[step]))['steps'], [step])
        for change in ({'result_row': 'any'}, {'value': 'old literal'}, {'selector': {'role': 'button', 'name':'VIEW'}}):
            with self.assertRaises(flow.WorkflowError):
                flow.validate(dict(recipe(), steps=[dict(step, **change)]))

    def test_waiting_for_result_reuses_only_unique_table_locator_not_value(self):
        import result_cell
        selector = dict(role='table', automation_id='CurrentResults')
        step = dict(action='verify-value', selector=dict(role='table'), result_column='Code', value='LAB-42')
        now = [200.]
        def sleep(seconds):
            now[0] += seconds
        with (patch.object(flow, 'result_surface', return_value=self.element) as resolve,
              patch.object(flow, '_stable_result_selector', return_value=selector) as stable,
              patch.object(result_cell, 'find', side_effect=[flow.app.bridge.BridgeError('result_cell_missing'), self.element]) as read,
              patch.object(flow.time, 'monotonic', side_effect=lambda: now[0]),
              patch.object(flow.time, 'sleep', side_effect=sleep)):
            receipt = flow.observe_result(self.binding, self.uia, step, 'LAB-42', lambda: None, wait_seconds=5)
        self.assertEqual(receipt['status'], 'verified')
        self.assertEqual([call.args[-1] for call in resolve.call_args_list], [{'role':'table'}, selector])
        stable.assert_called_once()
        self.assertEqual(read.call_count, 2)

    def test_row_only_waits_for_empty_grid_but_does_not_retry_ambiguous_rows(self):
        import result_cell
        step = dict(action='double-click-result', result_row='single', selector=dict(role='table'))
        now = [200.]
        def sleep(seconds):
            now[0] += seconds
        with (patch.object(flow, 'result_surface', return_value=self.element),
              patch.object(result_cell, 'find_row', side_effect=[flow.app.bridge.BridgeError('result_row_missing'), self.element]),
              patch.object(flow.time, 'monotonic', side_effect=lambda: now[0]),
              patch.object(flow.time, 'sleep', side_effect=sleep)):
            receipt = flow.observe_result_row(self.binding, self.uia, step, lambda: None, wait_seconds=5)
        self.assertEqual(receipt['status'], 'observed')
        self.assertEqual(receipt['result_observation']['attempts'], 2)
        with (patch.object(flow, 'result_surface', return_value=self.element),
              patch.object(result_cell, 'find_row', side_effect=flow.app.bridge.BridgeError('result_row_ambiguous')),
              patch.object(flow.time, 'sleep') as sleep):
            receipt = flow.observe_result_row(self.binding, self.uia, step, lambda: None, wait_seconds=5)
        self.assertEqual(receipt['diagnostic'], 'result_row_ambiguous')
        sleep.assert_not_called()

    def test_generic_surface_keeps_native_resolution_and_ambiguity_ahead_of_history(self):
        group = dict(role='group', automation_id='PaintedSurface')
        table = dict(role='table')
        with patch.object(flow,'result_surface',return_value=self.element) as surface:
            self.assertEqual(flow._result_target(self.binding,self.uia,table,group),(self.element,table))
            surface.assert_called_once_with(self.binding,self.uia,table)
            surface.side_effect = flow.WorkflowError('result_table_ambiguous')
            with self.assertRaisesRegex(flow.WorkflowError,'result_table_ambiguous'):
                flow._result_target(self.binding,self.uia,table,group)
            self.assertEqual(surface.call_count,2)
            surface.side_effect = [flow.WorkflowError('result_table_missing'),flow.app.bridge.BridgeError('target_missing')]
            with self.assertRaisesRegex(flow.WorkflowError,'result_table_missing'):
                flow._result_target(self.binding,self.uia,table,group)

    def test_proven_table_locator_uses_direct_lookup_and_recovers_only_on_absence(self):
        generic = dict(role='table')
        saved = dict(role='group', automation_id='CurrentGrid')
        with patch.object(flow,'result_surface', return_value=self.element) as get:
            actual = flow._result_target(self.binding, self.uia, generic, saved, column='Key', element_hint=True)
            self.assertEqual(actual,(self.element,saved))
            self.assertEqual([c.args[-1] for c in get.call_args_list], [saved])
            get.reset_mock()
            get.side_effect = [flow.app.bridge.BridgeError('target_missing'), self.element]
            self.assertEqual(flow._result_target(self.binding,self.uia,generic,saved,column='Key',element_hint=True),
                             (self.element,generic))
            self.assertEqual([c.args[-1] for c in get.call_args_list], [saved, generic])
            get.reset_mock()
            get.side_effect = flow.app.bridge.BridgeError('target_ambiguous')
            with self.assertRaisesRegex(flow.app.bridge.BridgeError,'target_ambiguous'):
                flow._result_target(self.binding,self.uia,generic,saved,column='Key',element_hint=True)
            self.assertEqual(get.call_count,1)

    def test_generic_selection_finds_only_groups_with_grid_pattern_or_header_and_rows(self):
        import result_cell
        self.uia.same.side_effect = lambda a,b: a is b
        with (patch.object(flow.lookup,'find_all', return_value=[self.element]) as find,
              patch.object(flow.live,'_visible',return_value=True),
              patch.object(result_cell,'_column',side_effect=lambda b,u,s,*rest: dict(element=object(),grid=s))):
            self.assertIs(flow._group_for_column(self.binding,self.uia,'Key'), self.element)
            self.assertTrue(find.call_args.kwargs['grid_group'])
            self.assertEqual(find.call_count,1)
            find.return_value = [self.element,MagicMock()]
            with self.assertRaisesRegex(flow.WorkflowError,'result_table_ambiguous'):
                flow._group_for_column(self.binding,self.uia,'Key')

    def test_generic_list_without_requested_column_finds_result_group_once(self):
        import result_cell
        unrelated, group = MagicMock(), MagicMock()
        self.uia.same.side_effect = lambda a,b: a is b
        selector = dict(role='group',automation_id='CurrentResult')
        step = dict(action='verify-value',selector=dict(role='table'),result_column='Part',value='P-3')
        def find(binding,uia,table,expected,**options):
            if table is unrelated:
                raise flow.app.bridge.BridgeError('result_column_not_found')
            self.assertIs(table,group)
            self.assertEqual(expected,'P-3')
        with (patch.object(flow,'result_surface',return_value=unrelated),
              patch.object(result_cell,'find',side_effect=find) as cells,
              patch.object(flow.lookup,'find_all',side_effect=lambda *a,**k: [group] if k.get('grid_group') else []),
              patch.object(flow.live,'_visible',return_value=True),
              patch.object(result_cell,'_column',side_effect=lambda b,u,s,*a: dict(element=object(),grid=s) if s is group else {}),
              patch.object(flow,'_stable_result_selector',return_value=selector)):
            report = flow.observe_result(self.binding,self.uia,step,'P-3',lambda: None,wait_seconds=0,remember=True)
        self.assertEqual(report['status'],'verified')
        self.assertEqual([c.args[2] for c in cells.call_args_list],[unrelated,group])
        self.assertEqual(report['result_element']['selector'],selector)

    def test_changed_result_table_after_same_value_was_verified_blocks_double_click(self):
        import result_cell
        data = dict(recipe(), steps=[
            dict(action='verify-value', selector=dict(role='table'), result_column='Part', value='A1'),
            dict(action='double-click-result', label='Open matching row')])
        self.uia.same.side_effect = lambda first, second: first is second
        with (patch.object(flow,'result_surface',side_effect=[self.element,MagicMock()]),
              patch.object(result_cell,'find',return_value=self.element), patch.object(result_cell,'execute') as click):
            result = flow.run(data)
        self.assertEqual((result['status'],result['diagnostic']),('blocked','result_table_changed'),result)
        self.assertFalse(result['input_dispatched'])
        click.assert_not_called()

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
