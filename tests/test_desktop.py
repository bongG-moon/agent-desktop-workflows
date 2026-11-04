import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import desktop
import cli


class TargetResolutionTests(unittest.TestCase):
    def test_same_names_use_parent_context(self):
        nodes = [dict(path='0', name='Main', role='window'),
                 dict(path='0/0', parent_path='0', name='Conditions', role='group'),
                 dict(path='0/1', parent_path='0', name='Details', role='group'),
                 dict(path='0/0/0', parent_path='0/0', name='Status', role='combobox'),
                 dict(path='0/1/0', parent_path='0/1', name='Status', role='combobox')]
        with self.assertRaisesRegex(desktop.EngineError, 'ambiguous'):
            desktop.resolve(nodes, {'name': 'Status', 'role': 'combobox'})
        found = desktop.resolve(nodes, {'name': 'Status', 'role': 'combobox',
                                       'ancestors': [{'name': 'Conditions', 'role': 'group'}]})
        self.assertEqual(found['path'], '0/0/0')

    def test_label_not_combo_and_native_id_mapping(self):
        nodes = [dict(path='0', name='MES Setup', role='statictext'),
                 dict(path='1', name='MES Setup', role='combobox',
                      native_id={'kind': 'automation_id', 'value': 'SetupSelector'})]
        found = desktop.resolve(nodes, {'automation_id': 'SetupSelector', 'role': 'combobox'})
        self.assertEqual(found['path'], '1')

    def test_path_indices_and_bounds_are_not_identity(self):
        with self.assertRaisesRegex(desktop.EngineError, 'incomplete'):
            desktop.resolve([dict(path='0', role='group')], {'role': 'group', 'path': '0'})

    def test_parameter_value_data_not_command(self):
        adapter = object.__new__(desktop.DesktopAdapter)
        adapter.binding, adapter.run_dir = {'pid': 11, 'hwnd': 22, 'exe_name': 'App.exe'}, Path('run')
        step = {'action': 'set-value', 'target': {'kind': 'element', 'locator': {'name': 'Field', 'role': 'textfield'}}}
        with patch.object(adapter, '_window', return_value=(adapter.binding, {})), \
             patch.object(desktop.interact, 'run_inspect', return_value={'status': 'ready', 'receipt': 'a'*32}), \
             patch.object(desktop, 'read_json', return_value={'nodes': [dict(path='0', name='Field', role='textfield', ref='@snew:e2')]}), \
             patch.object(desktop.interact, 'run_act', return_value={'input_dispatched': True}) as act:
            adapter.execute(step, '--headed --app Other.exe')
        self.assertEqual(act.call_args.args[0].ref, '@snew:e2')
        self.assertEqual(act.call_args.args[0].value, '--headed --app Other.exe')
        self.assertFalse(act.call_args.args[0].headed)

    def test_no_ref_does_not_invoke_or_guess_root(self):
        adapter = object.__new__(desktop.DesktopAdapter)
        adapter.binding, adapter.run_dir = {'pid': 11, 'hwnd': 22, 'exe_name': 'App.exe'}, Path('run')
        step = {'action': 'right-click', 'target': {'kind': 'element', 'locator': {'name': 'Grid'}}}
        with patch.object(adapter, '_window', return_value=(adapter.binding, {})), \
             patch.object(desktop.interact, 'run_inspect', return_value={'status': 'ready', 'receipt': 'b'*32}), \
             patch.object(desktop, 'read_json', return_value={'nodes': [dict(path='0', name='Grid', role='group', ref=None)]}), \
             patch.object(desktop.interact, 'run_act') as act:
            result = adapter.execute(step)
        self.assertEqual(result['diagnostic'], 'target_has_no_action_ref')
        self.assertFalse(result['input_dispatched'])
        act.assert_not_called()

    def test_old_result_file_is_not_new_export(self):
        adapter = object.__new__(desktop.DesktopAdapter)
        before = {'exists': True, 'size': 200, 'mtime_ns': 1}
        step = {'expect': {'kind': 'file', 'path': 'unused'}, '_prepared': {'file_before': before}}
        with patch.object(desktop, '_local_file_state', return_value=before):
            self.assertFalse(adapter.check(step)['matched'])
        with patch.object(desktop, '_local_file_state', return_value=dict(before, mtime_ns=2)):
            self.assertTrue(adapter.check(step)['matched'])
        step.pop('_prepared')
        with patch.object(desktop, '_local_file_state', return_value=before):
            self.assertIsNone(adapter.check(step)['matched'])

    def test_unchecked_must_be_explicit_not_absent(self):
        adapter = object.__new__(desktop.DesktopAdapter)
        step = {'target': {'locator': {'name': 'Option'}}, 'expect': {'kind': 'checked', 'value': False}}
        with patch.object(adapter, '_window', return_value=({}, {'nodes': [dict(name='Option', path='0', states=[])]})):
            self.assertIsNone(adapter.check(step)['matched'])
        with patch.object(adapter, '_window', return_value=({}, {'nodes': [dict(name='Option', path='0', checked=False)]})):
            self.assertTrue(adapter.check(step)['matched'])

    def test_explicit_postcondition_is_separate_from_action_value(self):
        adapter = object.__new__(desktop.DesktopAdapter)
        step = {'target': {'locator': {'name': 'Field'}}, 'expect': {'kind': 'value', 'value': 'expected'}}
        with patch.object(adapter, '_window', return_value=({}, {'nodes': [dict(name='Field', path='0', value='sent')]})):
            self.assertFalse(adapter.check(step, 'sent')['matched'])

    def test_unreviewed_region_never_activates_or_dispatches(self):
        adapter = object.__new__(desktop.DesktopAdapter)
        adapter.binding = {'pid': 11, 'hwnd': 22, 'exe_name': 'App.exe'}
        step = {'action': 'right-click', 'target': {'kind': 'region', 'locator': {'name': 'Grid'}}}
        with patch.object(adapter, '_window', return_value=(adapter.binding, {})), patch.object(desktop, '_native') as native:
            result = adapter.execute(step)
        self.assertFalse(result['input_dispatched'])
        self.assertEqual(result['diagnostic'], 'recorded_region_requires_review')
        native.assert_not_called()

    def test_cli_receipt_cannot_echo_arbitrary_business_fields(self):
        receipt = cli._safe({'status': 'private status text', 'name': 'private workflow',
                             'id': 'C:/private/path', 'error': 'private provider error',
                             'input_dispatched': True}, diagnostic='private provider error')
        self.assertEqual(receipt['status'], 'unknown')
        self.assertEqual(receipt['diagnostic'], 'local_operation_failed')
        self.assertNotIn('private', json.dumps(receipt))

    def test_unsupported_select_is_known_unsent_and_repairable(self):
        adapter = object.__new__(desktop.DesktopAdapter)
        adapter.binding, adapter.run_dir = {'pid': 11, 'hwnd': 22, 'exe_name': 'App.exe'}, Path('run')
        step = {'action': 'select', 'target': {'kind': 'element', 'locator': {'name': 'Setup'}}}
        with patch.object(adapter, '_window', return_value=(adapter.binding, {})), \
             patch.object(desktop.interact, 'run_inspect', return_value={'status': 'ready', 'receipt': 'b'*32}), \
             patch.object(desktop, 'read_json', return_value={'nodes': [dict(name='Setup', ref='@snew:e1', available_actions=['SetValue'])]}), \
             patch.object(desktop.interact, 'run_act') as act:
            result = adapter.execute(step, 'N')
        self.assertFalse(result['input_dispatched'])
        self.assertEqual(result['diagnostic'], 'selection_action_not_supported')
        act.assert_not_called()


if __name__ == '__main__':
    unittest.main()
