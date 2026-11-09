"""Selection intent is historical evidence, never execution confirmation."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import desktop
from storage import StoreError, validate_workflow


def example():
    return {'schema_version': 1, 'id': 'a'*32, 'name': 'Generic combo', 'revision': 1,
            'target': {'exe_name': 'Fixture.exe', 'title': 'Example', 'class_name': 'Fixture'},
            'parameters': {}, 'steps': [{'id': 'b'*32, 'label': 'Category / N', 'action': 'set-value',
                'target': {'kind': 'element', 'locator': {'automation_id': 'Category', 'role': 'combobox'}},
                'value': 'N', 'expect': {'kind': 'value', 'value': 'N'},
                'recognition': {'target': 'identified', 'action': 'supported', 'result': 'unverified'},
                'selection': {'source': 'option_click', 'value': 'N', 'result': 'unavailable'}}]}


class SelectionContractTests(unittest.TestCase):
    def test_intent_roundtrips_without_claiming_observed_value(self):
        original = example()
        saved = validate_workflow(original)
        self.assertEqual(saved, original)
        self.assertNotIn('observed_value', saved['steps'][0]['selection'])
        self.assertEqual(saved['steps'][0]['expect']['value'], 'N')

    def test_unproven_blank_stale_or_forged_selection_evidence_rejected(self):
        mutations = [{'value': ''}, {'source': 'option_inventory'}, {'result': 'matched'},
                     {'result': 'matched', 'observed_value': 'Y'},
                     {'result': 'mismatch', 'observed_value': 'N'},
                     {'result': 'unavailable', 'observed_value': 'N'},
                     {'result': []}, {'source': []}]
        for change in mutations:
            with self.subTest(change=change):
                workflow = example()
                workflow['steps'][0]['selection'].update(change)
                with self.assertRaises(StoreError):
                    validate_workflow(workflow)

    def test_historical_recording_evidence_survives_parameterization(self):
        workflow = example()
        step = workflow['steps'][0]
        step.pop('value')
        step['parameter'] = 'Choice'
        workflow['parameters']['Choice'] = 'N'
        validate_workflow(workflow)
        workflow['parameters']['Choice'] = 'Y'
        saved = validate_workflow(workflow)
        self.assertEqual(saved['steps'][0]['selection']['value'], 'N')
        self.assertEqual(saved['parameters']['Choice'], 'Y')


class EngineValueFallbackTests(unittest.TestCase):
    def setUp(self):
        self.adapter = object.__new__(desktop.DesktopAdapter)
        self.adapter.run_dir = Path('synthetic-test-output')
        self.binding = {'pid': 123, 'hwnd': 456, 'exe_name': 'Fixture.exe'}
        self.step = example()['steps'][0]
        self.node = {'automation_id': 'Category', 'role': 'combobox', 'value': None, 'secure': False,
                     'value_evidence': {'status': 'unavailable', 'source': 'none'}}
        self.current = {'complete': True, 'nodes': [self.node]}
        self.engine_node = {**self.node, 'ref': '@stest:e1'}

    def check(self, actual='N', *, after=None, projection=None):
        self.adapter._window = Mock(side_effect=[(self.binding, self.current), (self.binding, after or self.current)])
        with patch.object(desktop.interact, 'run_inspect', return_value={'status': 'ready', 'receipt': 'c'*32}), \
             patch.object(desktop, 'read_json', return_value=projection or {'complete': True, 'nodes': [self.engine_node]}), \
             patch.object(desktop.interact, 'read_current_value', return_value={'value': actual, 'input_dispatched': False}), \
             patch.object(desktop.interact, 'run_act') as act:
            result = self.adapter.check(self.step, 'N')
        act.assert_not_called()
        return result

    def test_fresh_engine_get_recovers_native_unknown_only(self):
        result = self.check()
        self.assertIs(result['matched'], True)
        self.assertEqual(result['source'], 'fresh_engine_get_value')
        self.assertIs(result['input_dispatched'], False)

    def test_engine_empty_and_mismatch_remain_unverified_or_mismatched(self):
        self.assertIsNone(self.check('')['matched'])
        self.assertIs(self.check('Y')['matched'], False)

    def test_late_native_conflict_or_contradiction_blocks_engine_match(self):
        for evidence, value in [({'status': 'conflict'}, None),
                                ({'status': 'observed', 'source': 'selection'}, 'Y')]:
            after = {'complete': True, 'nodes': [dict(self.node, value=value, value_evidence=evidence)]}
            self.assertEqual(self.check(after=after)['diagnostic'], 'combo_value_conflict')

    def test_partial_or_ambiguous_engine_tree_never_proves_value(self):
        for projection in [{'complete': False, 'nodes': [self.engine_node]},
                           {'complete': True, 'nodes': [self.engine_node, self.engine_node]}]:
            self.assertIsNone(self.check(projection=projection)['matched'])

    def test_native_conflict_never_asks_another_backend_to_override_it(self):
        self.node['value_evidence']['status'] = 'conflict'
        self.adapter._window = Mock(return_value=(self.binding, self.current))
        with patch.object(desktop.interact, 'run_inspect') as inspect:
            self.assertEqual(self.adapter.check(self.step)['diagnostic'], 'combo_value_conflict')
        inspect.assert_not_called()

    def test_read_current_value_is_scoped_and_never_consumes_action(self):
        session = {'pid': 123, 'hwnd': 456}
        raw = {'ok': True, 'data': {'ref': '@stest:e1', 'property': 'value', 'value': 'N'}}
        states = {'ok': True, 'data': {'ref': '@stest:e1', 'property': 'states', 'value': []}}
        with patch.object(desktop.interact, 'validate_local_directory', return_value=Path('local')), \
             patch.object(desktop.interact, '_load_session', return_value=(session, {}, {})) as load, \
             patch.object(desktop.interact.observe, 'verify_binary'), \
             patch.object(desktop.interact, '_environment'), \
             patch.object(desktop.interact, '_call', side_effect=[states, raw]) as call, \
             patch.object(desktop.interact, '_same_identity') as identity, \
             patch.object(desktop.interact, '_write'):
            result = desktop.interact.read_current_value(Path('local'), '@stest:e1')
        load.assert_called_once_with(Path('local'), '@stest:e1')
        self.assertEqual([c.args[0] for c in call.call_args_list], [
            ['get', '@stest:e1', '--property', 'states'], ['get', '@stest:e1', '--property', 'value']])
        self.assertEqual(identity.call_count, 2)
        self.assertEqual(result, {'value': 'N', 'input_dispatched': False})

    def test_newly_protected_or_unreadable_states_prevent_value_read(self):
        for value in (['protected'], None):
            states = {'ok': True, 'data': {'ref': '@stest:e1', 'property': 'states', 'value': value}}
            with self.subTest(value=value), \
                 patch.object(desktop.interact, 'validate_local_directory', return_value=Path('local')), \
                 patch.object(desktop.interact, '_load_session', return_value=({}, {}, {})), \
                 patch.object(desktop.interact.observe, 'verify_binary'), \
                 patch.object(desktop.interact, '_environment'), \
                 patch.object(desktop.interact, '_call', return_value=states) as call:
                with self.assertRaises(desktop.EngineError):
                    desktop.interact.read_current_value(Path('local'), '@stest:e1')
            call.assert_called_once_with(['get', '@stest:e1', '--property', 'states'], Path('local'))


if __name__ == '__main__':
    unittest.main()
