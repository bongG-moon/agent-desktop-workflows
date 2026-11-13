"""Literal text values share registered workflows without weakening combo checks."""
from copy import deepcopy
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'skills/desktop-select/scripts'), str(ROOT / 'scripts')]
import live_action as live
import python_workflow as flow
import win_bridge as win
from test_select_live_action import Element, Binding


class TextFieldActionTests(unittest.TestCase):
    def setUp(self):
        self.binding = Binding()
        self.root = Element(50032)
        self.field = Element(50004, self.root, ['SetValue'], **{ '30043': True, '30045': 'old', '30046': False })
        self.uia = object.__new__(win.UIA)
        self.uia.from_handle = lambda _: self.root
        self.uia.parent = lambda e: e.parent
        self.uia.same = lambda a, b: a is not None and a is b
        self.uia.actions = lambda e: list(e.actions)
        self.uia.bounds = lambda e: e.rect
        self.calls = []
        self.after = lambda item, op, value: item.props.update({30045: value})
        for target, kwargs in (
                ('VALUE_SETTLE_SECONDS', dict(new=0)),
                ('control_lookup._raw_parent', dict(side_effect=lambda _uia, element: element.parent)),
                ('_call', dict(side_effect=self.call))):
            p = patch('live_action.' + target, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def call(self, item, operation, argument, authorize, dispatching):
        authorize()
        dispatching(operation)
        self.calls.append((item, operation, argument))
        self.after(item, operation, argument)

    def run_field(self, value='00123', item=None, role='textfield'):
        return live.execute(self.binding, self.uia, item or self.field, {'role': role}, 'fill',
                            value, still_valid=lambda: None)

    def test_literal_unicode_quotes_and_leading_zero_values_are_written_once(self):
        for value in ('00123', '  검색어 A "B"  ', '가족/제품=(A)'):
            with self.subTest(value=value):
                self.calls.clear()
                result = self.run_field(value)
                self.assertEqual(result['status'], 'verified')
                self.assertEqual(self.calls, [(self.field, 'SetValue', value)])
                self.assertEqual(self.field.props[30045], value)

    def test_intentional_empty_is_verified_only_from_available_value_pattern(self):
        result = self.run_field('')
        self.assertEqual((result['status'], result['effect_verification']), ('verified', 'matched'))
        self.field.props[30043] = None
        observed = live.text_readback(self.binding, self.uia, self.field)
        self.assertIsNone(observed['value'])
        self.assertEqual(observed['value_evidence']['status'], 'unavailable')

    def test_missing_property_after_clear_is_not_mistaken_for_empty(self):
        self.after = lambda *args: self.field.props.update({30045: None})
        result = self.run_field('')
        self.assertEqual(result['status'], 'delivered_unverified')
        self.assertEqual(len(self.calls), 1)

    def test_mismatched_readback_is_not_retried(self):
        self.after = lambda *args: self.field.props.update({30045: 'different'})
        result = self.run_field('00123')
        self.assertEqual((result['status'], result['input_dispatched']), ('mismatch', True))
        self.assertEqual(len(self.calls), 1)

    def test_unknown_delivery_is_not_retried(self):
        def uncertain(*args):
            raise win.BridgeError('hresult_error')
        self.after = uncertain
        result = self.run_field()
        self.assertEqual((result['status'], result['input_dispatched']), ('uncertain', None))
        self.assertEqual(len(self.calls), 1)

    def test_readonly_disabled_hidden_and_missing_pattern_block_before_input(self):
        for properties, diagnostic in (({30046: True}, 'text_value_not_writable'),
                                       ({30010: False}, 'target_not_actionable'),
                                       ({30022: True}, 'target_offscreen'),
                                       ({30043: None}, 'text_value_not_writable')):
            original = dict(self.field.props)
            self.field.props.update(properties)
            with self.subTest(properties=properties):
                result = self.run_field()
                self.assertEqual((result['diagnostic'], result['input_dispatched']), (diagnostic, False))
                self.assertFalse(self.calls)
            self.field.props = original

    def test_protected_element_and_raw_wrapper_never_expose_value_or_receive_input(self):
        wrapper = Element(50026, self.root)
        self.field.parent = wrapper
        for protected in (self.field, wrapper):
            protected.props[30019] = True
            with self.subTest(role=protected.props[30003]):
                with self.assertRaises(win.BridgeError):
                    live.text_readback(self.binding, self.uia, self.field)
                self.assertEqual(self.run_field()['input_dispatched'], False)
                self.assertFalse(self.calls)
            protected.props[30019] = False

    def test_combo_inner_editor_must_use_the_combo_path(self):
        combo = Element(50003, self.root)
        self.field.parent = combo
        with self.assertRaisesRegex(win.BridgeError, 'textfield_requires_combo'):
            live.text_readback(self.binding, self.uia, self.field)
        result = self.run_field()
        self.assertEqual((result['diagnostic'], result['input_dispatched']), ('textfield_requires_combo', False))
        self.assertFalse(self.calls)

    def test_foreign_or_changed_role_does_not_send_input(self):
        self.field.props[30002] = 99
        self.assertEqual(self.run_field()['input_dispatched'], False)
        self.field.props[30002] = 42
        self.assertEqual(self.run_field(role='combobox')['diagnostic'], 'target_role_changed')
        self.assertFalse(self.calls)

    def test_invalid_control_characters_are_never_sent(self):
        for value in ('a\x00b', 'a\nb', '   ', '\ud800'):
            with self.subTest(value=repr(value)):
                self.assertEqual(self.run_field(value)['diagnostic'], 'invalid_text_value')
        self.assertFalse(self.calls)


def recipe():
    return dict(schema=1, name='Parameterized fields', exe='Example.exe', window='Example window', steps=[
        dict(action='set-value', selector=dict(automation_id='family', role='combobox'), parameter='Family'),
        dict(action='set-value', selector=dict(automation_id='query', role='textfield'), parameter='검색어'),
        dict(action='invoke', selector=dict(automation_id='search', role='button'))])


class TextFieldWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.uia = MagicMock()
        self.uia.__enter__.return_value = self.uia
        self.uia.same.return_value = True
        self.uia.actions.return_value = ['SetValue', 'Click']
        self.binding, self.element = MagicMock(), MagicMock()
        self.element.property.return_value = ''
        for target, kwargs in (
            ('bind', dict(return_value=self.binding)),
            ('app.bridge.UIA', dict(return_value=self.uia)),
            ('app.guard', dict(return_value=None)),
            ('app.visibility.restore_for_action', dict(return_value=False)),
            ('lookup.resolve', dict(return_value=self.element)),
            ('wait', dict(return_value=None))):
            p = patch('python_workflow.' + target, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def execute(self, *args, **kwargs):
        kwargs['still_valid']()
        if args[4] in ('fill', 'select'):
            return dict(status='verified', input_dispatched=True)
        return dict(status='delivered_unverified', input_dispatched=True, diagnostic='business_result_not_verified')

    def test_two_runs_change_choices_and_text_without_changing_saved_recipe(self):
        saved = recipe()
        before = deepcopy(saved)
        with patch.object(flow.live, 'execute', side_effect=self.execute) as execute:
            first = flow.run(saved, {'Family': 'Hybrid', '검색어': '00123'})
            second = flow.run(saved, {'Family': 'DRAM', '검색어': '검색어 "나"'})
        self.assertEqual([first['status'], second['status']], ['completed_unverified'] * 2)
        self.assertEqual([(call.args[4], call.args[5]) for call in execute.call_args_list],
                         [('select', 'Hybrid'), ('fill', '00123'), ('invoke', None),
                          ('select', 'DRAM'), ('fill', '검색어 "나"'), ('invoke', None)])
        self.assertEqual(saved, before)

    def test_missing_text_parameter_blocks_every_step_before_window_access(self):
        with patch.object(flow.live, 'execute') as execute:
            result = flow.run(recipe(), {'Family': 'Hybrid'})
        self.assertEqual(result['missing_parameters'], ['검색어'])
        self.assertFalse(result['input_dispatched'])
        flow.bind.assert_not_called()
        execute.assert_not_called()

    def test_empty_text_clears_but_empty_combo_fails_before_any_input(self):
        with patch.object(flow.live, 'execute', side_effect=self.execute) as execute:
            result = flow.run(recipe(), {'Family': 'Hybrid', '검색어': ''})
            self.assertEqual(result['status'], 'completed_unverified')
            self.assertEqual(execute.call_args_list[1].args[5], '')
        flow.bind.reset_mock()
        with patch.object(flow.live, 'execute') as execute, self.assertRaisesRegex(flow.WorkflowError, 'invalid_override'):
            flow.run(recipe(), {'Family': '', '검색어': 'valid'})
        flow.bind.assert_not_called()
        execute.assert_not_called()

    def test_shared_parameter_empty_rejects_if_any_target_is_combo(self):
        data = recipe()
        data['steps'][1]['parameter'] = 'Family'
        with self.assertRaisesRegex(flow.WorkflowError, 'invalid_override'):
            flow.run(data, {'Family': ''})
        flow.bind.assert_not_called()

    def test_fixed_empty_is_allowed_only_for_textfield(self):
        for role, accepted in (('textfield', True), ('combobox', False)):
            data = recipe()
            data['steps'] = [dict(action='set-value', selector=dict(automation_id='field', role=role), value='')]
            if accepted:
                self.assertEqual(flow.validate(data), data)
            else:
                with self.assertRaisesRegex(flow.WorkflowError, 'invalid_combo_value'):
                    flow.validate(data)

    def test_uncertain_text_write_stops_before_query_without_retry(self):
        receipts = [dict(status='verified', input_dispatched=True), dict(status='uncertain', input_dispatched=None)]
        with patch.object(flow.live, 'execute', side_effect=receipts) as execute:
            result = flow.run(recipe(), {'Family': 'Hybrid', '검색어': '00123'})
        self.assertEqual((result['status'], result['stopped_at']), ('uncertain', 2))
        self.assertEqual(execute.call_count, 2)

    def test_readonly_uses_text_readback_without_restore_or_dispatch(self):
        with patch.object(flow.live, 'execute') as execute, patch.object(flow.live, 'combo_readback', return_value={}), patch.object(
                flow.live, 'text_readback', return_value=dict(value='', value_evidence=dict(status='observed'))) as reader:
            result = flow.run(recipe(), read_only=True)
        self.assertEqual(result['status'], 'observed')
        reader.assert_called_once()
        flow.app.visibility.restore_for_action.assert_not_called()
        execute.assert_not_called()

    def test_dynamic_textfield_name_is_not_compared_as_command_caption(self):
        selector = dict(query='검색어', role='textfield')
        self.element.property.return_value = 'new value'
        self.assertIs(flow.revalidate_step(self.binding, self.uia, self.uia.from_handle(), selector,
                                          dict(automation_id='query', role='textfield'), dict(name='old value'),
                                          self.element), self.element)

    def test_registered_runner_keeps_text_variable_and_example_guide(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(flow.live, 'combo_readback', return_value={}), patch.object(
                flow.live, 'text_readback', return_value=dict(value='old demo')):
            target = Path(folder) / 'query-fields'
            result = flow.register(recipe(), target)
            self.assertEqual(result['required_parameters'], ['Family', '검색어'])
            self.assertEqual(flow.load(target / 'workflow.json')['steps'][1]['parameter'], '검색어')
            self.assertNotIn('value', flow.load(target / 'workflow.json')['steps'][1])
            self.assertIn('검색어=00123', (target / 'SKILL.md').read_text(encoding='utf8'))

    def test_cli_preflight_failure_reports_no_input_before_starting_worker(self):
        for worker in ([], ['--_worker']):
            for flags, diagnostic in ((['--set', 'Family=', '--set', '검색어=valid'], 'invalid_override'),
                                      (['--set', 'Unexpected=value'], 'unknown_parameter')):
                with self.subTest(worker=worker, flags=flags), patch.object(flow, 'load', return_value=recipe()), patch.object(
                        flow.subprocess, 'run') as subprocess_run, redirect_stdout(StringIO()) as output:
                    code = flow.main([*worker, 'run', '--recipe', 'unused.json', *flags])
                self.assertEqual(code, 2)
                result = json.loads(output.getvalue())
                self.assertEqual((result['status'], result['input_dispatched'], result['diagnostic']),
                                 ('blocked', False, diagnostic))
                subprocess_run.assert_not_called()
                flow.bind.assert_not_called()

    def test_cli_missing_values_report_all_required_inputs_without_starting_worker(self):
        with patch.object(flow, 'load', return_value=recipe()), patch.object(flow.subprocess, 'run') as launched, redirect_stdout(StringIO()) as output:
            code = flow.main(['run', '--recipe', 'unused.json'])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())['missing_parameters'], ['Family', '검색어'])
        launched.assert_not_called()


if __name__ == '__main__':
    unittest.main()
