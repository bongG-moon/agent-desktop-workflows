"""Direct label/ID commands resolve once, then use one guarded native action."""
import argparse
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select as app
import native_targets
import registered_runtime as runtime
from test_select_targeting import node


class Item:
    def __init__(self, snapshot):
        self.node, self.value = snapshot, ''
        self.capabilities = ['SetValue'] if snapshot['role'] == 'combobox' else ['Click']

    def property(self, key):
        return {30005: self.node['name'], 30010: self.node['enabled'],
                30011: self.node['automation_id']}.get(key)


class DirectActionTests(unittest.TestCase):
    def setUp(self):
        self.nodes = [node('0', 'window', name='Business'),
                      node('0/0', identifier='psView'),
                      node('0/0/0', identifier='pnlConditionArea'),
                      node('0/0/0/0', name='MES Setup', rect=(10, 100, 88, 19)),
                      node('0/0/0/0/0', 'statictext', name='MES Setup', rect=(10, 100, 88, 19)),
                      node('0/0/0/1', 'combobox', identifier='gcboStatus', rect=(145, 100, 170, 19)),
                      node('0/0/0/2', 'button', name='조회', identifier='btnSearchData', rect=(400, 100, 80, 24))]
        self.elements = [Item(n) for n in self.nodes]
        self.root, self.combo, self.button = self.elements[0], self.elements[5], self.elements[6]
        self.owner, self.uia = Mock(pid=7, hwnd=123), Mock()
        self.uia.__enter__ = Mock(return_value=self.uia)
        self.uia.__exit__ = Mock(return_value=False)
        self.uia.same.side_effect = lambda a, b: a is b
        self.uia.from_handle.return_value = self.root
        self.uia.actions.side_effect = lambda item: item.capabilities
        self.calls, self.selectors = [], []
        replacements = [
            (app, 'restore_for_action', Mock(return_value=False)),
            (app, 'bind_surface', Mock(return_value=self.owner)),
            (app, 'guard', Mock()),
            (app.bridge, 'UIA', Mock(return_value=self.uia)),
            (app, 'observe', Mock(side_effect=lambda *a, **kw: (deepcopy(self.nodes), self.elements))),
            (app, 'execute', Mock(return_value={'status': 'legacy_path', 'input_dispatched': False})),
            (app, 'visible', Mock(return_value=True)),
            (native_targets, 'resolve', Mock(side_effect=self.resolve)),
            (native_targets, 'ancestry', Mock(side_effect=self.ancestry)),
            (runtime, '_wait', Mock()),
            (runtime.live, '_combo_visible', Mock(return_value=True)),
            (runtime.live, 'combo_readback', Mock(side_effect=self.readback)),
            (runtime.live, '_call', Mock(side_effect=self.dispatch)),
            (runtime.live, '_option_rows', Mock(side_effect=AssertionError('unexpected option inventory'))),
        ]
        for obj, key, value in replacements:
            handle = patch.object(obj, key, value)
            handle.start()
            self.addCleanup(handle.stop)

    def resolve(self, binding, uia, selector, **kwargs):
        self.selectors.append(deepcopy(selector))
        return next(item for item in self.elements if item.node['automation_id'] == selector['automation_id'])

    def ancestry(self, uia, item, root, **kwargs):
        by_path = {item.node['path']: item for item in self.elements}
        result = [item]
        while item.node['parent_path']:
            item = by_path[item.node['parent_path']]
            result.append(item)
        return result

    @staticmethod
    def readback(binding, uia, item):
        return {'value': item.value, 'value_evidence': {
            'status': 'observed' if item.value else 'unavailable',
            'source': 'corroborated' if item.value else 'none'}}

    def dispatch(self, item, method, value, authorize, dispatching):
        authorize()
        dispatching(method)
        self.calls.append((item.node['automation_id'], method, value))
        if method == 'SetValue':
            item.value = value

    def action(self, command='select', target='MES Setup', **kwargs):
        params = dict(command=command, target=target, role=None, window='Business',
                      value='N' if command == 'select' else None, expand=False, within=None, popup=None)
        params.update(kwargs)
        return app.action(argparse.Namespace(**params), self.owner)

    def test_wrapped_display_label_resolves_once_and_writes_literal_once(self):
        result = self.action()
        self.assertEqual((result['status'], result['effect_verification']), ('verified', 'matched'))
        self.assertEqual(result['execution_path'], 'direct_native')
        self.assertEqual(result['matched_by'], 'sibling_geometry')
        self.assertEqual(result['target']['automation_id'], 'gcboStatus')
        self.assertEqual(result['metrics'], {'full_tree_snapshots': 1, 'option_inventories': 0})
        self.assertEqual(self.calls, [('gcboStatus', 'SetValue', 'N')])
        app.observe.assert_called_once()
        app.execute.assert_not_called()
        runtime.live._option_rows.assert_not_called()
        self.assertEqual(self.selectors[0]['ancestors'], [
            {'role': 'group', 'automation_id': 'pnlConditionArea'},
            {'role': 'group', 'automation_id': 'psView'}])

    def test_exact_id_uses_same_native_path_with_original_case_preserved(self):
        result = self.action(target='GCBOSTATUS')
        self.assertEqual(result['matched_by'], 'automation_id')
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(self.selectors[0]['automation_id'], 'gcboStatus')
        app.observe.assert_called_once()

    def test_query_button_uses_invoke_once_and_keeps_business_result_unverified(self):
        result = self.action('open', '조회')
        self.assertEqual((result['status'], result['input_dispatched']), ('delivered_unverified', True))
        self.assertEqual(result['diagnostic'], 'business_result_not_verified')
        self.assertEqual(self.calls, [('btnSearchData', 'Invoke', None)])
        app.observe.assert_called_once()
        app.execute.assert_not_called()

    def test_first_observation_keeps_duplicate_id_ambiguity_contract(self):
        self.nodes.append(node('0/0/0/3', 'combobox', identifier='gcboStatus', rect=(700, 100, 100, 20)))
        self.elements.append(Item(self.nodes[-1]))
        with self.assertRaisesRegex(app.SelectionError, '^target_ambiguous$'):
            self.action(target='gcboStatus')
        native_targets.resolve.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_label_obstacle_and_within_mismatch_still_block_before_native_dispatch(self):
        self.nodes.append(node('0/0/0/3', 'button', name='Help', rect=(101, 100, 30, 18)))
        self.elements.append(Item(self.nodes[-1]))
        with self.assertRaisesRegex(app.SelectionError, '^target_missing$'):
            self.action()
        with self.assertRaisesRegex(app.SelectionError, '^scope_missing$'):
            self.action(target='gcboStatus', within='different form')
        self.assertEqual(self.calls, [])
        native_targets.resolve.assert_not_called()

    def test_within_region_is_preserved_and_revalidated_without_whole_view(self):
        result = self.action(within='conditions')
        self.assertEqual(result['status'], 'verified')
        native_targets.ancestry.assert_called()
        app.observe.assert_called_once()

    def test_target_leaving_observed_within_container_blocks(self):
        native_targets.ancestry.side_effect = lambda *a, **kw: [self.combo, self.root]
        result = self.action(within='conditions')
        self.assertEqual((result['status'], result['input_dispatched']), ('blocked', False))
        self.assertEqual(result['diagnostic'], 'target_changed')
        self.assertEqual(self.calls, [])

    def test_first_native_result_must_be_same_element_as_original_label_resolution(self):
        native_targets.resolve.side_effect = lambda *a, **kw: Item(self.nodes[5])
        result = self.action()
        self.assertEqual((result['status'], result['input_dispatched']), ('blocked', False))
        self.assertEqual(result['diagnostic'], 'registered_target_changed')
        self.assertEqual(self.calls, [])
        app.execute.assert_not_called()

    def test_native_missing_or_ambiguous_after_snapshot_does_not_fallback(self):
        for code in ('target_missing', 'target_ambiguous'):
            native_targets.resolve.side_effect = app.bridge.BridgeError(code)
            result = self.action()
            self.assertEqual((result['status'], result['input_dispatched']), ('blocked', False))
            self.assertEqual(result['diagnostic'], code)
        self.assertEqual(self.calls, [])
        app.execute.assert_not_called()

    def test_root_replacement_blocks_before_input(self):
        self.uia.from_handle.return_value = Item(self.nodes[0])
        result = self.action()
        self.assertEqual((result['status'], result['input_dispatched']), ('blocked', False))
        self.assertEqual(result['diagnostic'], 'target_changed')
        self.assertEqual(self.calls, [])

    def test_command_caption_changed_after_initial_choice_blocks_before_input(self):
        def change(*args, **kwargs):
            self.button.node['name'] = '삭제'
            return self.button
        native_targets.resolve.side_effect = change
        result = self.action('open', '조회')
        self.assertEqual((result['status'], result['input_dispatched']), ('blocked', False))
        self.assertEqual(result['diagnostic'], 'target_caption_changed')
        self.assertEqual(self.calls, [])

    def test_same_value_noop_still_checks_native_target_and_does_not_dispatch(self):
        self.combo.value = 'N'
        result = self.action()
        self.assertEqual((result['status'], result['input_dispatched']), ('verified', False))
        self.assertEqual(result['diagnostic'], 'value_already_selected')
        native_targets.resolve.assert_called()
        self.assertEqual(self.calls, [])

    def test_uncertain_write_keeps_unknown_delivery_without_retry(self):
        def uncertain(*args):
            self.dispatch(*args)
            raise OSError('native call failed after dispatch')
        runtime.live._call.side_effect = uncertain
        result = self.action()
        self.assertEqual((result['status'], result['input_dispatched']), ('uncertain', None))
        self.assertEqual(len(self.calls), 1)
        app.execute.assert_not_called()
        runtime.live._option_rows.assert_not_called()

    def test_unavailable_readback_keeps_delivered_without_retry(self):
        runtime.live.combo_readback.return_value = {'value': None, 'value_evidence': {'status': 'unavailable'}}
        runtime.live.combo_readback.side_effect = None
        result = self.action()
        self.assertEqual((result['status'], result['input_dispatched']), ('delivered_unverified', True))
        self.assertEqual(result['diagnostic'], 'value_not_verified')
        self.assertEqual(len(self.calls), 1)

    def test_invalid_direct_values_block_before_pattern_call(self):
        for value in ('', '  ', 'N\0', 'x' * 10001, '값' * 4000, None):
            with self.subTest(value=str(value)[:20]):
                result = self.action(value=value)
                self.assertEqual((result['status'], result['input_dispatched']), ('blocked', False))
                self.assertEqual(result['diagnostic'], 'invalid_combo_value')
        self.assertEqual(self.calls, [])
        native_targets.resolve.assert_not_called()

    def test_missing_id_or_setvalue_capability_uses_existing_path(self):
        self.nodes[5]['automation_id'] = ''
        self.assertEqual(self.action()['status'], 'legacy_path')
        self.nodes[5]['automation_id'] = 'gcboStatus'
        self.combo.capabilities = ['Expand']
        self.assertEqual(self.action()['status'], 'legacy_path')
        self.assertEqual(app.execute.call_count, 2)
        self.assertEqual(self.calls, [])

    def test_popup_expansion_and_choose_use_existing_path(self):
        self.assertEqual(self.action(popup=999)['status'], 'legacy_path')
        self.assertEqual(self.action('open', '조회', expand=True)['status'], 'legacy_path')
        self.nodes[6]['role'] = 'treeitem'
        self.assertEqual(self.action('open', '조회')['status'], 'legacy_path')
        self.assertEqual(app.execute.call_count, 3)
        self.assertEqual(self.calls, [])

    def test_learned_and_anchored_calls_keep_original_execution_contract(self):
        args = argparse.Namespace(command='select', target='gcboStatus', role='combobox',
                                  value='N', window='Business', within=None, popup=None, expand=False)
        learned = dict(role='combobox', match='automation_id', target='gcboStatus')
        self.assertEqual(app.action(args, self.owner, learned=learned)['status'], 'legacy_path')
        with patch.object(app, 'near_region', return_value=(self.nodes, list(range(len(self.nodes))), 5)), \
                patch.object(app, 'add_display_properties'):
            self.assertEqual(app.action(args, self.owner, anchor={'action':'intent'})['status'], 'legacy_path')
        self.assertEqual(self.calls, [])
        self.assertEqual(app.execute.call_count, 2)


if __name__ == '__main__':
    unittest.main()
